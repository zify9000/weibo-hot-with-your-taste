"""
调研脚本：从未推送新闻中筛选候选，输出 JSON 供 agent 展示
"""
import sys
import json
import os
import re
from datetime import date, datetime

import yaml
import openai

from common import (
    SCRIPT_DIR, DATA_DIR, CONFIG_DIR,
    ALL_TOPICS_PATH, PUSHED_TOPICS_PATH, PROMPT_PATH,
    setup_logging, load_base_config,
)

logger = setup_logging("survey")

MIN_UNPUSHED_COUNT = 5


def collect_unpushed_topics() -> tuple:
    """从 all_topics.jsonl 和 pushed_topics.jsonl 计算今天未推送的话题"""
    if not ALL_TOPICS_PATH.exists():
        return [], 0

    today = date.today().isoformat()

    pushed_words = set()
    if PUSHED_TOPICS_PATH.exists():
        with open(PUSHED_TOPICS_PATH, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not record.get("ts", "").startswith(today):
                    continue
                for n in record.get("topics", []):
                    pushed_words.add(n.get("word", ""))

    unpushed = {}
    with open(ALL_TOPICS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not record.get("ts", "").startswith(today):
                continue
            for n in record.get("topics", []):
                word = n.get("word", "")
                if word and word not in pushed_words and word not in unpushed:
                    unpushed[word] = {"word": word, "category": n.get("category", "")}

    result = list(unpushed.values())
    return result, len(pushed_words)


def call_llm_survey(unpushed: list, pushed_count: int, llm_model="", base_url="", api_key="") -> list:
    """LLM 从未推送话题中判断用户可能感兴趣的，返回全部候选（带 llm_recommended 标记）"""
    if not api_key:
        logger.error("未找到 API_KEY")
        sys.exit(1)

    if not llm_model or not base_url:
        logger.error("未配置 llm_model 或 llm_base_url")
        sys.exit(1)

    with open(PROMPT_PATH, encoding="utf-8") as f:
        prompt_data = yaml.safe_load(f)
    current_prompt = prompt_data["topics_for_taste_judge_prompt"]

    criteria_section = ""
    important_match = re.search(r"【yes】范围：(.*?)(?=\n\n【no】)", current_prompt, re.DOTALL)
    if important_match:
        criteria_section = important_match.group(1).strip()

    target_count = max(1, pushed_count)

    topics_list_rows = "\n".join(
        f"{i+1}. {n['word']} | 分类:{n.get('category', '')}"
        for i, n in enumerate(unpushed)
    )

    prompt = f"""你是一个新闻重要性评估专家。以下微博热搜之前未被推送，请从中选出用户可能感兴趣的内容，数量约 {target_count} 条。

当前判断标准：
{criteria_section}

=== 未推送的新闻列表 ===
{topics_list_rows}

=== 输出格式 ===
每行格式："序号:选/不选"，严格按序号输出：
1:选
2:不选
3:选
...

必须包含全部 {len(unpushed)} 条新闻的判断。"""

    client = openai.OpenAI(api_key=api_key, base_url=base_url)

    try:
        resp = client.chat.completions.create(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=4096,
            timeout=60,
        )
        content = resp.choices[0].message.content
        if not content:
            logger.warning("LLM 返回为空")
            return [{"word": n["word"], "category": n.get("category", ""), "llm_recommended": False} for n in unpushed]

        selections = {}
        for line in content.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            m = re.match(r"^(\d+):(选|不选)", line)
            if m:
                idx = int(m.group(1))
                selected = m.group(2) == "选"
                if 1 <= idx <= len(unpushed):
                    selections[unpushed[idx - 1]["word"]] = selected

        if not selections:
            logger.warning("LLM 回复未匹配到任何选/不选行，所有候选默认不推荐")

        result = []
        for n in unpushed:
            result.append({
                "word": n["word"],
                "category": n.get("category", ""),
                "llm_recommended": selections.get(n["word"], False),
            })
        return result

    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        return [{"word": n["word"], "category": n.get("category", ""), "llm_recommended": False} for n in unpushed]


def main():
    CONFIG = load_base_config()

    unpushed, pushed_count = collect_unpushed_topics()

    if len(unpushed) < MIN_UNPUSHED_COUNT:
        result = {
            "ready": False,
            "message": f"未推送新闻不足：{len(unpushed)} 条，需至少 {MIN_UNPUSHED_COUNT} 条",
            "total_unpushed": len(unpushed),
            "pushed_count": pushed_count,
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    logger.info(f"当天未推送 {len(unpushed)} 条，已推送 {pushed_count} 条")

    llm_model = os.environ.get("llm_model", "")
    llm_base_url = os.environ.get("llm_base_url", "")
    llm_api_key = os.environ.get("llm_api_key", "")
    candidates = call_llm_survey(unpushed, pushed_count, llm_model, llm_base_url, llm_api_key)

    selected_count = sum(1 for c in candidates if c["llm_recommended"])
    logger.info(f"LLM 推荐调研 {selected_count} 条")

    result = {
        "ready": True,
        "candidates": candidates,
        "total_unpushed": len(unpushed),
        "pushed_count": pushed_count,
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
