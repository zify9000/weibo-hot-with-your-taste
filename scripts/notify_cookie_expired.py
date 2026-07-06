"""Cookie 过期自动通知：生成二维码 → 上传飞书 → 推送图片消息

由 cron wrapper 在 fetch.py 检测到 Cookie 过期后调用（标记文件存在且 status=expired）。
节流策略：6 小时内不重复推送，避免 cron 每小时刷屏。

退出码约定：0 = 无需操作或成功；1 = 失败（下次 cron 自动重试）
"""
import sys
import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import requests

from common import (
    SCRIPT_DIR, setup_logging, load_feishu_env, get_feishu_creds,
    get_feishu_token, read_cookie_status, COOKIE_STATUS_PATH,
)

logger = setup_logging("notify_cookie_expired")

# weibo_get_qr.py 输出的二维码路径（与脚本内 QR_IMAGE_PATH 一致）
QR_IMAGE_PATH = Path("/tmp/weibo_login_qr.png")

# 节流阈值：两次推送的最小间隔。cron 每小时跑一次，6 小时保证用户上班/下班各看到一次
# spec 未要求外置到 base.yaml，先硬编码；如需调整可迁移到 config
NOTIFY_THROTTLE_HOURS = 6

# 二维码生成脚本路径
WEIBO_GET_QR_SCRIPT = SCRIPT_DIR / "init" / "weibo_get_qr.py"

# QR 生成失败时的兜底告警文案
_QR_FAIL_ALERT = "Cookie 过期且二维码生成失败，请回复'重登微博'重试，或检查浏览器环境"


def _check_throttle(status: dict) -> bool:
    """检查是否在节流期内（返回 True 表示应跳过本次推送）

    notified_at 为空（首次推送）不节流；否则距今不足 NOTIFY_THROTTLE_HOURS 小时则节流。
    兼容 naive / aware 两种 ISO 时间字符串（写入时用 naive，spec 示例带 +08:00）。
    """
    notified_at = status.get("notified_at")
    if not notified_at:
        return False  # 首次推送，不节流
    try:
        last = datetime.fromisoformat(notified_at)
    except ValueError:
        logger.warning(f"notified_at 格式异常: {notified_at}，不节流")
        return False
    # 与 last 的 tzinfo 对齐，避免 naive/aware 比较报错
    now = datetime.now(last.tzinfo) if last.tzinfo else datetime.now()
    elapsed = now - last
    return elapsed < timedelta(hours=NOTIFY_THROTTLE_HOURS)


def _generate_qr() -> bool:
    """subprocess 调用 weibo_get_qr.py 生成二维码，返回是否成功"""
    cmd = [sys.executable, str(WEIBO_GET_QR_SCRIPT)]
    logger.info(f"调用二维码生成脚本: {' '.join(cmd)}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except subprocess.TimeoutExpired:
        logger.error("二维码生成脚本超时（>90s）")
        return False
    if result.returncode != 0:
        logger.error(f"二维码生成失败（exit={result.returncode}）: {result.stderr.strip()}")
        return False
    if not QR_IMAGE_PATH.exists():
        logger.error(f"二维码脚本成功退出但图片未生成: {QR_IMAGE_PATH}")
        return False
    logger.info(f"二维码已生成: {QR_IMAGE_PATH}")
    return True


def _upload_image_to_feishu(token: str, image_path: Path) -> str:
    """上传图片到飞书，返回 image_key"""
    url = "https://open.feishu.cn/open-apis/im/v1/images"
    headers = {"Authorization": f"Bearer {token}"}
    with open(image_path, "rb") as f:
        # multipart/form-data：image_type=message + image 文件
        resp = requests.post(
            url, headers=headers,
            files={"image": f}, data={"image_type": "message"},
            timeout=30,
        )
    result = resp.json()
    if result.get("code") != 0:
        raise Exception(f"上传图片失败: code={result.get('code')} msg={result.get('msg')}")
    return result["data"]["image_key"]


def _send_feishu_image(token: str, chat_id: str, image_key: str) -> str:
    """发送飞书图片消息，返回 message_id"""
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "receive_id": chat_id,
        "msg_type": "image",
        "content": json.dumps({"image_key": image_key}),
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    result = resp.json()
    if result.get("code") != 0:
        raise Exception(f"发送图片消息失败: code={result.get('code')} msg={result.get('msg')}")
    return result["data"]["message_id"]


def _send_feishu_text(token: str, chat_id: str, text: str):
    """发送飞书纯文本消息（用于 QR 生成失败时的兜底告警）"""
    url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=15)
    result = resp.json()
    if result.get("code") != 0:
        raise Exception(f"发送文本消息失败: code={result.get('code')} msg={result.get('msg')}")
    return result["data"]["message_id"]


def _send_text_alert(text: str):
    """QR 生成失败时的兜底告警：尝试发送飞书文本消息（失败仅记日志，不影响退出码）"""
    try:
        load_feishu_env()
        app_id, app_secret, chat_id = get_feishu_creds()
        if not all([app_id, app_secret, chat_id]):
            logger.warning("飞书凭据不完整，无法发送兜底告警")
            return
        token = get_feishu_token(app_id, app_secret)
        _send_feishu_text(token, chat_id, text)
        logger.info("兜底告警已发送至飞书")
    except Exception as e:
        logger.warning(f"发送兜底告警失败: {e}")


def _update_notified_at():
    """更新标记文件的 notified_at 字段为当前时间（保留 status / detected_at 等其他字段）"""
    status = read_cookie_status() or {}
    status["notified_at"] = datetime.now().isoformat()
    try:
        COOKIE_STATUS_PATH.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning(f"更新 notified_at 失败: {e}")


def main():
    # Step 1: 读取标记文件，不存在或非 expired 则无需操作
    status = read_cookie_status()
    if not status or status.get("status") != "expired":
        logger.info("未检测到 Cookie 过期标记，无需通知")
        sys.exit(0)

    # Step 2: 节流检查
    if _check_throttle(status):
        logger.info(f"距上次推送不足 {NOTIFY_THROTTLE_HOURS} 小时，跳过本次通知")
        sys.exit(0)

    # Step 3: 生成二维码
    if not _generate_qr():
        # 兜底：QR 生成失败仍发文本告警，确保用户知晓
        _send_text_alert(_QR_FAIL_ALERT)
        sys.exit(1)

    # Step 4-5: 加载飞书凭据并获取 token
    load_feishu_env()
    app_id, app_secret, chat_id = get_feishu_creds()
    if not all([app_id, app_secret, chat_id]):
        logger.error("飞书凭据不完整，需要 feishu_app_id / feishu_app_secret / feishu_chat_id")
        sys.exit(1)

    try:
        token = get_feishu_token(app_id, app_secret)
    except Exception as e:
        logger.error(f"获取飞书 token 失败: {e}")
        sys.exit(1)

    # Step 6: 上传二维码图片
    try:
        image_key = _upload_image_to_feishu(token, QR_IMAGE_PATH)
    except Exception as e:
        logger.error(f"上传二维码图片失败: {e}")
        sys.exit(1)

    # Step 7: 发送图片消息
    try:
        _send_feishu_image(token, chat_id, image_key)
    except Exception as e:
        logger.error(f"发送飞书图片消息失败: {e}")
        sys.exit(1)

    # Step 8: 更新 notified_at（供下次节流判断）
    _update_notified_at()
    logger.info("Cookie 过期通知已推送至飞书，等待用户扫码后回复'已扫码'")
    sys.exit(0)


if __name__ == "__main__":
    main()
