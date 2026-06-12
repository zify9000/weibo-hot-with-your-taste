"""反馈记录脚本：将用户反馈写入 tasted_topics.jsonl，"信息不全"反馈即时触发二次搜索补充"""
import json
import fcntl
import argparse
from datetime import datetime

from common import (
    DATA_DIR, setup_logging,
    load_llm_env, load_weibo_env, load_feishu_env,
    get_llm_creds, get_weibo_cookies, get_feishu_creds,
)

TASTED_TOPICS_PATH = DATA_DIR / "tasted_topics.jsonl"

logger = setup_logging("feedback")

FEEDBACK_TYPES = ("liked", "disliked", "info_insufficient")


def _send_supplement_card(word: str, summary: str):
    """通过飞书推送话题补充信息卡片"""
    import curl_cffi

    load_feishu_env()
    app_id, app_secret, chat_id = get_feishu_creds()
    if not app_id or not app_secret or not chat_id:
        logger.warning("飞书凭据不完整，跳过补充信息推送")
        return

    sess = curl_cffi.Session(impersonate="chrome131")
    resp = sess.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    result = resp.json()
    if result.get("code") != 0:
        logger.warning(f"获取飞书 token 失败: {result.get('msg')}")
        return
    token = result["tenant_access_token"]

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "📌 话题补充信息"},
            "template": "blue",
        },
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**{word}**"}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": summary}},
        ],
    }
    payload = {
        "receive_id": chat_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    }
    resp = sess.post(
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=payload,
        timeout=15,
    )
    result = resp.json()
    if result.get("code") != 0:
        logger.warning(f"推送补充信息失败: code={result.get('code')} msg={result.get('msg')}")
        return

    logger.info(f"补充信息已推送: {word}")


def _handle_info_insufficient(word: str):
    """处理"信息不全"反馈：二次搜索 → 生成摘要 → 推送补充信息"""
    from fetch import fetch_topic_detail, generate_summary

    load_weibo_env()
    cookies = get_weibo_cookies()
    if not cookies or not cookies.get("SUB"):
        logger.warning("未配置微博 Cookie，无法补充信息")
        return

    load_llm_env()
    llm_model, llm_base_url, llm_api_key = get_llm_creds()

    logger.info(f"开始二次检索: {word}")
    contents = fetch_topic_detail(word, cookies)
    if not contents:
        logger.warning(f"未获取到话题详情: {word}")
        return

    summary = generate_summary(word, contents, llm_model, llm_base_url, llm_api_key)
    if not summary:
        logger.warning(f"生成摘要失败: {word}")
        return

    logger.info(f"二次检索完成: {word} → {summary}")
    _send_supplement_card(word, summary)


def main():
    parser = argparse.ArgumentParser(description="记录用户对话题的反馈")
    parser.add_argument("--word", required=True, help="话题名称")
    parser.add_argument("--feedback", required=True, choices=FEEDBACK_TYPES, help="反馈类型")
    parser.add_argument("--category", default="", help="话题分类")
    parser.add_argument("--ts", default="", help="推送时间戳")
    parser.add_argument("--comment", default="", help="用户原话")
    args = parser.parse_args()

    record = {
        "ts": args.ts or datetime.now().isoformat(),
        "word": args.word,
        "feedback_type": args.feedback,
        "category": args.category,
        "comment": args.comment,
        "recorded_at": datetime.now().isoformat(),
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = open(TASTED_TOPICS_PATH, "a", encoding="utf-8")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        fd.write(json.dumps(record, ensure_ascii=False) + "\n")
        icon = {"liked": "👍", "disliked": "👎", "info_insufficient": "ℹ️"}.get(args.feedback, "?")
        logger.info(f"反馈已写入: {args.word} → {icon}")
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()

    # "信息不全"反馈：即时触发二次搜索补充
    if args.feedback == "info_insufficient":
        _handle_info_insufficient(args.word)

    print(json.dumps({"status": "ok", "word": args.word, "feedback_type": args.feedback}, ensure_ascii=False))


if __name__ == "__main__":
    main()
