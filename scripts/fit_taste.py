"""偏好特征自适应优化：根据用户反馈和分类变化，全自动优化 taste.yaml 全部七项配置"""
import sys
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import yaml
import openai
import curl_cffi

sys.path.insert(0, str(Path(__file__).parent))
from common import (
    DATA_DIR,
    TASTE_CONFIG_PATH, CATEGORY_STORE_PATH,
    setup_logging, load_base_config, load_rule_config, load_prompt, is_initialized,
    load_llm_env, get_llm_creds, validate_llm_creds,
    load_feishu_env, get_feishu_creds,
)

TASTED_TOPICS_PATH = DATA_DIR / "tasted_topics.jsonl"

logger = setup_logging("fit_taste")


def collect_feedback_data(last_optimized_at: str) -> dict:
    """读取 tasted_topics.jsonl 中上次优化后的新反馈，按类型分组返回"""
    false_positive = []
    true_positive = []
    info_insufficient = []
    seen = set()

    if not TASTED_TOPICS_PATH.exists():
        return {"false_positive": [], "true_positive": [], "info_insufficient": []}

    with open(TASTED_TOPICS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue

            # 仅取上次优化后的新记录
            # ISO 8601 格式的字符串比较等价于时间序（无时区偏移时）
            recorded_at = record.get("recorded_at", "")
            if last_optimized_at and recorded_at <= last_optimized_at:
                continue

            word = record.get("word", "")
            if not word or word in seen:
                continue
            seen.add(word)

            feedback_type = record.get("feedback_type", "")
            category = record.get("category", "")
            entry = {"word": word, "category": category}

            if feedback_type == "liked":
                true_positive.append(entry)
            elif feedback_type == "disliked":
                false_positive.append(entry)
            elif feedback_type == "info_insufficient":
                info_insufficient.append(entry)

    return {
        "false_positive": false_positive,
        "true_positive": true_positive,
        "info_insufficient": info_insufficient,
    }


def find_new_categories(rule_config: dict) -> list:
    """从 topic_category.json 找出不在 liked/disliked 中的新分类"""
    if not CATEGORY_STORE_PATH.exists():
        return []

    with open(CATEGORY_STORE_PATH, encoding="utf-8") as f:
        category_store = json.load(f)

    all_cats = set(category_store.get("categories", []))
    liked = set(rule_config.get("liked_categories", []))
    disliked = set(rule_config.get("disliked_categories", []))
    classified = liked | disliked

    return sorted(all_cats - classified)


def format_feedback_for_llm(feedback_data: dict) -> str:
    """将反馈数据格式化为 LLM prompt 可读文本"""
    sections = []

    fp = feedback_data["false_positive"]
    if fp:
        lines = [f'- "{t["word"]}"（{t["category"]}）→ 👎 用户不感兴趣' for t in fp]
        sections.append("假阳性（被推送但用户不感兴趣）：\n" + "\n".join(lines))

    tp = feedback_data["true_positive"]
    if tp:
        lines = [f'- "{t["word"]}"（{t["category"]}）→ 👍 用户感兴趣' for t in tp]
        sections.append("真阳性（被推送且用户感兴趣）：\n" + "\n".join(lines))

    ii = feedback_data.get("info_insufficient", [])
    if ii:
        lines = [f'- "{t["word"]}"（{t["category"]}）→ ℹ️ 话题正确，摘要不足' for t in ii]
        sections.append("信息不全（话题选对了但摘要补充不够充分，不应影响yes/no判断）：\n" + "\n".join(lines))

    if not sections:
        return "（暂无新反馈）"

    return "\n\n".join(sections)


def format_new_categories_for_llm(new_categories: list) -> str:
    """将新分类列表格式化为 LLM prompt 可读文本"""
    if not new_categories:
        return "（无新分类）"
    return "\n".join(f"- {c}" for c in new_categories)


def call_llm_optimize(rule_config: dict, feedback_text: str, new_categories_text: str,
                      llm_model="", base_url="", api_key="") -> str:
    """单次 LLM 调用，全面优化 taste.yaml 七项配置，返回 LLM 原始输出"""
    issues = validate_llm_creds(llm_model, base_url, api_key)
    if issues:
        logger.error(f"LLM 凭据异常: {'; '.join(issues)}")
        sys.exit(1)

    prompt_template = load_prompt("fit_taste_prompt")
    prompt = prompt_template.format(
        domain_keywords="、".join(rule_config.get("domain_keywords", [])),
        liked_categories="、".join(rule_config.get("liked_categories", [])),
        disliked_categories="、".join(rule_config.get("disliked_categories", [])),
        recall_keywords="、".join(rule_config.get("recall_keywords", [])),
        yes_criteria=rule_config.get("yes_criteria", ""),
        no_criteria=rule_config.get("no_criteria", ""),
        info_check_criteria=rule_config.get("info_check_criteria", ""),
        feedback_text=feedback_text,
        new_categories=new_categories_text,
    )

    client = openai.OpenAI(api_key=api_key, base_url=base_url)

    try:
        resp = client.chat.completions.create(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=8192,
            timeout=120,
        )
        content = resp.choices[0].message.content
        if not content:
            logger.error("LLM 返回为空")
            sys.exit(1)
        return content.strip()

    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        sys.exit(1)


def parse_llm_output(content: str) -> dict:
    """解析 LLM 输出的 ===section=== 格式，返回七项配置 + 变更摘要的 dict"""
    def _extract(section_name: str, text: str, next_sections: list) -> str:
        """提取两个 section 之间的文本"""
        if next_sections:
            boundary = "|".join(f"==={s}===" for s in next_sections)
            pattern = rf"==={section_name}===\s*\n(.*?)(?={boundary})"
        else:
            pattern = rf"==={section_name}===\s*\n(.*)"
        m = re.search(pattern, text, re.DOTALL)
        return m.group(1).strip() if m else ""

    # 按序提取各段
    domain_keywords_raw = _extract("domain_keywords", content,
                                    ["liked_categories", "disliked_categories", "recall_keywords",
                                     "yes_criteria", "no_criteria", "变更摘要"])
    liked_raw = _extract("liked_categories", content,
                          ["disliked_categories", "recall_keywords", "yes_criteria", "no_criteria", "变更摘要"])
    disliked_raw = _extract("disliked_categories", content,
                             ["recall_keywords", "yes_criteria", "no_criteria", "变更摘要"])
    recall_raw = _extract("recall_keywords", content,
                           ["yes_criteria", "no_criteria", "变更摘要"])
    yes_raw = _extract("yes_criteria", content,
                        ["no_criteria", "变更摘要"])
    no_raw = _extract("no_criteria", content,
                       ["info_check_criteria", "变更摘要"])
    info_check_raw = _extract("info_check_criteria", content,
                               ["变更摘要"])
    summary_raw = _extract("变更摘要", content, [])

    # 解析列表字段（逗号或换行分隔）
    def _parse_list(raw: str) -> list:
        if not raw:
            return []
        items = []
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            for item in line.split(","):
                item = item.strip()
                if item:
                    items.append(item)
        return items

    # 解析多行文本字段
    def _parse_text(raw: str) -> str:
        return raw.strip() if raw else ""

    result = {
        "domain_keywords": _parse_list(domain_keywords_raw),
        "liked_categories": _parse_list(liked_raw),
        "disliked_categories": _parse_list(disliked_raw),
        "recall_keywords": _parse_list(recall_raw),
        "yes_criteria": _parse_text(yes_raw),
        "no_criteria": _parse_text(no_raw),
        "info_check_criteria": _parse_text(info_check_raw),
        "change_summary": _parse_text(summary_raw),
    }

    # 验证必填字段
    if not result["yes_criteria"] or not result["no_criteria"]:
        logger.error("LLM 输出中缺少 yes_criteria 或 no_criteria")
        logger.error(f"原始输出: {content[:500]}")
        sys.exit(1)

    if not result["liked_categories"] or not result["disliked_categories"]:
        logger.error("LLM 输出中缺少 liked_categories 或 disliked_categories")
        logger.error(f"原始输出: {content[:500]}")
        sys.exit(1)

    return result


def safe_write_taste_yaml(new_config: dict, change_summary: str):
    """备份旧 taste.yaml，写入优化后的配置"""
    # 备份
    backup_path = TASTE_CONFIG_PATH.with_suffix(".yaml.bak")
    if TASTE_CONFIG_PATH.exists():
        try:
            shutil.copy2(TASTE_CONFIG_PATH, backup_path)
            logger.info(f"旧 taste.yaml 已备份为 {backup_path.name}")
        except OSError as e:
            logger.error(f"备份 taste.yaml 失败: {e}")
            sys.exit(1)

    # 写入新配置（含 last_optimized_at 时间戳）
    new_config["last_optimized_at"] = datetime.now().isoformat()
    with open(TASTE_CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(new_config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
    logger.info("taste.yaml 已更新")

    # 日志记录变更摘要
    logger.info("=== 变更摘要 ===")
    for line in change_summary.split("\n"):
        line = line.strip()
        if line:
            logger.info(f"  {line}")


def send_feishu_notification(change_summary: str, total_feedback: int, new_categories_count: int):
    """优化完成后发送飞书文本通知（变更摘要），凭据不完整或失败时仅记录日志，不中断流程"""
    load_feishu_env()
    app_id, app_secret, chat_id = get_feishu_creds()

    if not app_id or not app_secret or not chat_id:
        logger.warning("飞书凭据不完整，跳过通知")
        return

    try:
        sess = curl_cffi.Session(impersonate="chrome131")
        # 获取 tenant_access_token
        token_resp = sess.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={"app_id": app_id, "app_secret": app_secret},
            timeout=10,
        )
        token_data = token_resp.json()
        if token_data.get("code") != 0:
            logger.warning(f"获取飞书 token 失败: code={token_data.get('code')} msg={token_data.get('msg')}")
            return
        token = token_data["tenant_access_token"]

        # 构建通知文本
        lines = [
            "🎯 偏好特征自适应优化完成",
            f"处理反馈: {total_feedback} 条 | 新分类: {new_categories_count} 个",
        ]
        if change_summary:
            lines.append("")
            lines.append("变更摘要:")
            lines.append(change_summary)

        text = "\n".join(lines)

        # 发送文本消息
        url = "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=chat_id"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        msg_resp = sess.post(url, headers=headers, json=payload, timeout=15)
        result = msg_resp.json()
        if result.get("code") != 0:
            logger.warning(f"飞书通知发送失败: code={result.get('code')} msg={result.get('msg')}")
        else:
            logger.info("飞书通知已发送")
    except Exception as e:
        logger.warning(f"飞书通知异常: {e}")


def main():
    logger.info("=== 开始偏好特征自适应优化 ===")

    # 1. 前置检查
    if not is_initialized():
        logger.info("尚未初始化偏好（taste.yaml 中 yes_criteria 为空），跳过优化")
        return

    rule_config = load_rule_config()
    base_config = load_base_config()
    min_feedback_count = base_config.get("fit_taste", {}).get("min_feedback_count", 5)
    min_new_categories = base_config.get("fit_taste", {}).get("min_new_categories", 3)
    last_optimized_at = rule_config.get("last_optimized_at", "")

    # 2. 收集数据
    feedback_data = collect_feedback_data(last_optimized_at)
    total_feedback = (
        len(feedback_data["false_positive"])
        + len(feedback_data["true_positive"])
        + len(feedback_data.get("info_insufficient", []))
    )

    new_categories = find_new_categories(rule_config)

    logger.info(f"上次优化时间: {last_optimized_at or '从未优化'}")
    logger.info(f"新反馈: {total_feedback} 条 (liked={len(feedback_data['true_positive'])}, "
                f"disliked={len(feedback_data['false_positive'])}, "
                f"info_insufficient={len(feedback_data.get('info_insufficient', []))})")
    logger.info(f"新分类: {len(new_categories)} 个")

    # 3. 数据不足检查
    if total_feedback < min_feedback_count and len(new_categories) < min_new_categories:
        logger.info(f"数据不足，跳过优化（反馈 {total_feedback} 条/需 ≥{min_feedback_count}，"
                    f"新分类 {len(new_categories)} 个/需 ≥{min_new_categories}）")
        return

    # 4. LLM 优化
    load_llm_env()
    llm_model, llm_base_url, llm_api_key = get_llm_creds()

    feedback_text = format_feedback_for_llm(feedback_data)
    new_categories_text = format_new_categories_for_llm(new_categories)

    logger.info("调用 LLM 全面优化...")
    llm_output = call_llm_optimize(
        rule_config, feedback_text, new_categories_text,
        llm_model, llm_base_url, llm_api_key
    )

    # 5. 解析 LLM 输出
    parsed = parse_llm_output(llm_output)

    # 6. 安全写入
    new_config = {
        "domain_keywords": parsed["domain_keywords"],
        "liked_categories": parsed["liked_categories"],
        "disliked_categories": parsed["disliked_categories"],
        "recall_keywords": parsed["recall_keywords"],
        "yes_criteria": parsed["yes_criteria"],
        "no_criteria": parsed["no_criteria"],
        "info_check_criteria": parsed["info_check_criteria"],
    }
    safe_write_taste_yaml(new_config, parsed["change_summary"])

    # 7. 飞书通知
    send_feishu_notification(parsed["change_summary"], total_feedback, len(new_categories))

    logger.info(f"=== 优化完成: 反馈 {total_feedback} 条, 新分类 {len(new_categories)} 个 ===")


if __name__ == "__main__":
    main()
