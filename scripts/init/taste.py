"""偏好初始化脚本：4步流水线
  Step 1 mention-taste    → 领域关键词 → LLM 匹配 → 审查分类 + 召回 → 偏好配置
  Step 2 grill-taste       → 深度偏好访谈 → 输出 Q&A 记录
  Step 3 build-criteria    → 偏好配置 + Q&A → LLM 生成判断标准
  Step 4 confirm-criteria  → 确认 → 写入 taste.yaml + init.log

步骤间通过 scripts/tmp/ 临时文件传递数据（固定文件名，断点续跑友好）。
"""
import sys
import json
import re
import argparse
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import yaml
import openai

from common import (
    SCRIPT_DIR, DATA_DIR, CONFIG_DIR, LOG_DIR, TMP_DIR,
    CATEGORY_STORE_PATH, TASTE_CONFIG_PATH, INIT_LOG_PATH,
    setup_logging, load_base_config, load_llm_env, load_prompt, resolve_llm_creds, validate_llm_creds,
)

logger = setup_logging("init")
CONFIG = load_base_config()

# ── 步骤间临时文件（固定路径，支持断点续跑）──

MENTION_TASTE_TMP = TMP_DIR / "init_mention_taste.json"
GRILL_TASTE_TMP = TMP_DIR / "init_grill_taste.json"
BUILD_CRITERIA_TMP = TMP_DIR / "init_build_criteria.json"


# ── Step 1: 领域关键词 → LLM 语义匹配 → 审查分类 → 配置召回 → 偏好配置 ──

def load_categories() -> list:
    """从 topic_category.json 加载全部分类"""
    if not CATEGORY_STORE_PATH.exists():
        logger.error("topic_category.json 不存在，请先运行 fetch.py 抓取数据")
        sys.exit(1)
    with open(CATEGORY_STORE_PATH, encoding="utf-8") as f:
        store = json.load(f)
    return store.get("categories", [])


def call_llm_match_categories(domain_keywords: list, categories: list, llm_model="", base_url="", api_key="") -> dict:
    """LLM 根据领域关键词从分类列表中选出 liked/disliked 各10个"""
    issues = validate_llm_creds(llm_model, base_url, api_key)
    if issues:
        logger.error(f"LLM 凭据异常: {'; '.join(issues)}")
        sys.exit(1)

    domain_kw_str = "、".join(domain_keywords)
    categories_list = "\n".join(f"{i+1}. {category}" for i, category in enumerate(categories))

    prompt_template = load_prompt("categories_for_init_recommand_prompt")
    prompt = prompt_template.format(domain_keywords=domain_kw_str, categories_list=categories_list)

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
            logger.error("LLM 返回为空")
            sys.exit(1)

        # JSON 格式解析
        try:
            result = json.loads(content.strip())
            liked = [c for c in result.get("liked", []) if c in categories]
            disliked = [c for c in result.get("disliked", []) if c in categories]
        except json.JSONDecodeError:
            # 兼容旧版逐行格式
            liked = []
            disliked = []
            for line in content.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                m = re.match(r"^(\d+):(liked|disliked):(.+)$", line)
                if m:
                    cat_name = m.group(3).strip()
                    if cat_name in categories:
                        if m.group(2) == "liked":
                            liked.append(cat_name)
                        else:
                            disliked.append(cat_name)

        if not liked and not disliked:
            logger.error("LLM 输出未匹配到任何分类")
            sys.exit(1)

        logger.info(f"语义匹配结果: liked={len(liked)}, disliked={len(disliked)}")
        return {"liked": liked, "disliked": disliked}

    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        sys.exit(1)


def step_mention_taste(args):
    """Step 1: 领域关键词 → LLM 匹配 → 审查分类 + 召回 → 偏好配置

    - 未提供 --liked/--disliked：LLM 语义匹配，输出建议
    - 提供 --liked/--disliked：跳过 LLM，直接使用用户指定的分类
    """
    domain_keywords = args.domain_kw
    if not (2 <= len(domain_keywords) <= 5):
        logger.error("请提供2-5个领域关键词")
        sys.exit(1)

    recall = [x.strip() for x in args.recall.split(",") if x.strip()] if args.recall else []

    liked = [x.strip() for x in args.liked.split(",") if x.strip()] if args.liked else []
    disliked_raw = [x.strip() for x in args.disliked.split(",") if x.strip()] if args.disliked else []

    if liked and disliked_raw:
        # 支持 --disliked "all"：自动取 liked 的补集
        if disliked_raw == ["all"]:
            categories = load_categories()
            liked_set = set(liked)
            disliked = [c for c in categories if c not in liked_set]
            logger.info(f"--disliked all: 自动补集 {len(disliked)} 个分类")
        else:
            disliked = disliked_raw
    else:
        disliked = disliked_raw

    if not (liked and disliked):
        # 缺少 liked 或 disliked → 调用 LLM 补充
        categories = load_categories()
        logger.info(f"加载 {len(categories)} 个分类，领域关键词: {domain_keywords}")

        llm_model, llm_base_url, llm_api_key = resolve_llm_creds(CONFIG)
        result = call_llm_match_categories(domain_keywords, categories, llm_model, llm_base_url, llm_api_key)
        if not liked:
            liked = result["liked"]
        if not disliked:
            disliked = result["disliked"]

    logger.info(f"偏好配置: liked={len(liked)}, disliked={len(disliked)}")

    if len(liked) < 5:
        logger.error(f"感兴趣的分类至少选5个，当前{len(liked)}个")
        sys.exit(1)
    if len(disliked) < 5:
        logger.error(f"不感兴趣的分类至少选5个，当前{len(disliked)}个")
        sys.exit(1)

    output = {
        "domain_keywords": domain_keywords,
        "liked": liked,
        "disliked": disliked,
        "recall": recall,
        "next_step": "python3 scripts/init/taste.py grill-taste",
    }
    _write_tmp(MENTION_TASTE_TMP, output)
    print(json.dumps(output, ensure_ascii=False))


# ── Step 2: 深度偏好访谈 ──

def call_llm_generate_question(domain_keywords, liked, disliked, recall, qa_rounds, round_number,
                               llm_model="", base_url="", api_key="") -> str:
    """LLM 根据用户偏好和历史 Q&A 生成下一轮追问"""
    issues = validate_llm_creds(llm_model, base_url, api_key)
    if issues:
        logger.error(f"LLM 凭据异常: {'; '.join(issues)}")
        sys.exit(1)

    domain_kw_str = "、".join(domain_keywords)
    liked_str = "、".join(liked)
    disliked_str = "、".join(disliked)
    recall_str = "、".join(recall) if recall else "无"

    if qa_rounds:
        qa_history = "\n".join(
            f"第{i+1}轮 - 问：{r['question']}\n第{i+1}轮 - 答：{r['answer']}"
            for i, r in enumerate(qa_rounds)
        )
    else:
        qa_history = "（首轮提问，无历史记录）"

    prompt_template = load_prompt("grill_taste_prompt")
    prompt = prompt_template.format(
        domain_keywords=domain_kw_str,
        liked_categories=liked_str,
        disliked_categories=disliked_str,
        recall_keywords=recall_str,
        qa_history=qa_history,
        round_number=round_number,
    )

    client = openai.OpenAI(api_key=api_key, base_url=base_url)

    try:
        resp = client.chat.completions.create(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=512,
            timeout=60,
        )
        content = resp.choices[0].message.content
        if not content:
            logger.error("LLM 返回为空")
            sys.exit(1)
        return content.strip()

    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        sys.exit(1)


def format_grill_qa(qa_rounds: list) -> str:
    """将 Q&A 轮次格式化为 prompt 可用的文本"""
    if not qa_rounds:
        return "（无深度访谈记录）"
    lines = []
    for i, r in enumerate(qa_rounds):
        lines.append(f"问{i+1}：{r['question']}")
        lines.append(f"答{i+1}：{r['answer']}")
    return "\n".join(lines)


def step_grill_taste(args):
    """Step 2: 深度偏好访谈 —— 无交互轮次调用

    每次调用只处理一轮：生成问题 或 接收答案。Agent 在外部编排循环。

    调用流程：
      grill-taste                         → 输出第1轮问题 JSON
      grill-taste --answer "用户回答"       → 记录答案，输出下一轮问题 JSON
      grill-taste --answer "done"          → 结束访谈，输出最终结果
    """
    max_rounds = 4

    # 恢复访谈进度（qa_rounds / pending_question）
    if GRILL_TASTE_TMP.exists():
        with open(GRILL_TASTE_TMP, encoding="utf-8") as f:
            state = json.load(f)
        qa_rounds = state.get("qa_rounds", [])
        pending_question = state.get("pending_question", "")
    else:
        qa_rounds = []
        pending_question = ""

    # 基础偏好数据始终从 mention-taste 读取最新值，避免 GRILL 残留旧数据
    pref = _read_tmp(MENTION_TASTE_TMP, "偏好配置", "mention-taste")
    domain_keywords = pref["domain_keywords"]
    liked = pref["liked"]
    disliked = pref["disliked"]
    recall = pref.get("recall", [])

    llm_model, llm_base_url, llm_api_key = resolve_llm_creds(CONFIG)

    # 处理本轮答案
    answer = getattr(args, "answer", None)
    if answer and pending_question:
        current_round = len(qa_rounds) + 1
        if answer.lower() == "done":
            # 用户主动结束
            output = _finalize_grill(domain_keywords, liked, disliked, recall, qa_rounds)
            print(json.dumps(output, ensure_ascii=False))
            return
        qa_rounds.append({"round": current_round, "question": pending_question, "answer": answer})
        logger.info(f"第{current_round}轮答案已记录")

    # 检查是否完成
    if len(qa_rounds) >= max_rounds:
        output = _finalize_grill(domain_keywords, liked, disliked, recall, qa_rounds)
        print(json.dumps(output, ensure_ascii=False))
        return

    # 生成下一轮问题
    next_round = len(qa_rounds) + 1
    question = call_llm_generate_question(
        domain_keywords, liked, disliked, recall, qa_rounds, next_round,
        llm_model, llm_base_url, llm_api_key
    )

    # 保存状态
    state = {
        "domain_keywords": domain_keywords,
        "liked": liked,
        "disliked": disliked,
        "recall": recall,
        "qa_rounds": qa_rounds,
        "pending_question": question,
    }
    _write_tmp(GRILL_TASTE_TMP, state)

    # 输出本轮问题（Agent 读取后展示给用户）
    print(json.dumps({"round": next_round, "max_rounds": max_rounds, "question": question}, ensure_ascii=False))


def _finalize_grill(domain_keywords, liked, disliked, recall, qa_rounds) -> dict:
    """结束访谈，输出最终结果"""
    output = {
        "domain_keywords": domain_keywords,
        "liked": liked,
        "disliked": disliked,
        "recall": recall,
        "qa_rounds": qa_rounds,
        "next_step": "python3 scripts/init/taste.py build-criteria",
    }
    _write_tmp(GRILL_TASTE_TMP, output)
    return output


# ── Step 3: 生成判断标准 ──

def call_llm_generate_criteria(domain_keywords, liked, disliked, recall, grill_qa="",
                               llm_model="", base_url="", api_key="") -> tuple:
    """LLM 根据用户偏好生成 yes/no 判断标准，返回 (yes_criteria, no_criteria)"""
    issues = validate_llm_creds(llm_model, base_url, api_key)
    if issues:
        logger.error(f"LLM 凭据异常: {'; '.join(issues)}")
        sys.exit(1)

    domain_kw_str = "、".join(domain_keywords)
    liked_str = "、".join(liked)
    disliked_str = "、".join(disliked)
    recall_str = "、".join(recall) if recall else "无"

    prompt_template = load_prompt("criteria_build_prompt")
    prompt = prompt_template.format(
        domain_keywords=domain_kw_str,
        liked_categories=liked_str,
        disliked_categories=disliked_str,
        recall_keywords=recall_str,
        grill_qa=grill_qa or "（无深度访谈记录）",
    )

    client = openai.OpenAI(api_key=api_key, base_url=base_url)

    try:
        resp = client.chat.completions.create(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=4096,
            timeout=120,
        )
        content = resp.choices[0].message.content
        if not content:
            logger.error("LLM 返回为空")
            sys.exit(1)

        yes_criteria = ""
        no_criteria = ""

        yes_match = re.search(r"===yes===\s*\n(.*?)(?=\n===no===)", content, re.DOTALL)
        if yes_match:
            yes_criteria = yes_match.group(1).strip()

        no_match = re.search(r"===no===\s*\n(.*)", content, re.DOTALL)
        if no_match:
            no_criteria = no_match.group(1).strip()

        if not yes_criteria or not no_criteria:
            logger.error("无法解析 LLM 输出中的判断标准")
            sys.exit(1)

        logger.info(f"判断标准生成完成: yes={len(yes_criteria)}字, no={len(no_criteria)}字")
        return yes_criteria, no_criteria

    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        sys.exit(1)


def generate_rule_yaml(domain_keywords, liked, disliked, recall, yes_criteria="", no_criteria=""):
    """生成 taste.yaml 内容（包含全部用户偏好配置）"""
    rule_config = {
        "domain_keywords": list(domain_keywords),
        "liked_categories": list(liked),
        "disliked_categories": list(disliked),
        "recall_keywords": list(recall),
        "yes_criteria": yes_criteria,
        "no_criteria": no_criteria,
    }

    if TASTE_CONFIG_PATH.exists():
        backup_path = TASTE_CONFIG_PATH.with_suffix(".yaml.bak")
        TASTE_CONFIG_PATH.rename(backup_path)
        logger.info(f"旧 taste.yaml 已备份为 {backup_path.name}")

    with open(TASTE_CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(rule_config, f, allow_unicode=True, default_flow_style=False, sort_keys=False)

    logger.info(f"taste.yaml 已生成: domain_kw={len(domain_keywords)}, liked={len(liked)}, disliked={len(disliked)}, recall={len(recall)}")
    return rule_config


def write_initialized(domain_keywords, liked, disliked):
    """写入 init.log 初始化记录"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "initialized_at": datetime.now().isoformat(),
        "domain_keywords": domain_keywords,
        "liked_categories": liked,
        "disliked_categories": disliked,
    }
    with open(INIT_LOG_PATH, "w", encoding="utf-8") as f:
        json.dump(record, f, ensure_ascii=False, indent=2)
    logger.info("init.log 初始化记录已写入")


def step_build_criteria(args):
    """Step 3: 偏好配置 + 深度访谈 → LLM 生成判断标准"""
    # 优先读 grill-taste 输出，回退到 mention-taste
    pref_path = GRILL_TASTE_TMP if GRILL_TASTE_TMP.exists() else MENTION_TASTE_TMP
    pref = _read_tmp(pref_path, "偏好配置", "mention-taste 或 grill-taste")

    domain_keywords = pref["domain_keywords"]
    liked = pref["liked"]
    disliked = pref["disliked"]
    recall = pref.get("recall", [])
    grill_qa = format_grill_qa(pref.get("qa_rounds", []))

    llm_model, llm_base_url, llm_api_key = resolve_llm_creds(CONFIG)
    yes_criteria, no_criteria = call_llm_generate_criteria(
        domain_keywords, liked, disliked, recall, grill_qa,
        llm_model, llm_base_url, llm_api_key
    )

    output = {
        "status": "pending_confirm",
        "domain_keywords": domain_keywords,
        "liked": liked,
        "disliked": disliked,
        "recall": recall,
        "yes_criteria": yes_criteria,
        "no_criteria": no_criteria,
        "next_step": "python3 scripts/init/taste.py confirm-criteria",
    }
    _write_tmp(BUILD_CRITERIA_TMP, output)
    print(json.dumps(output, ensure_ascii=False))


def step_confirm_criteria(args):
    """Step 4: 用户确认 → 写入配置"""
    data = _read_tmp(BUILD_CRITERIA_TMP, "判断标准", "build-criteria")

    domain_keywords = data["domain_keywords"]
    liked = data["liked"]
    disliked = data["disliked"]
    recall = data.get("recall", [])

    # 允许命令行覆盖 criteria（用户修改后直接传入）
    yes_criteria = args.yes if args.yes else data["yes_criteria"]
    no_criteria = args.no if args.no else data["no_criteria"]

    rule_config = generate_rule_yaml(domain_keywords, liked, disliked, recall, yes_criteria, no_criteria)
    write_initialized(domain_keywords, liked, disliked)

    output = {
        "status": "ok",
        "rule": rule_config,
    }
    print(json.dumps(output, ensure_ascii=False))


# ── 临时文件读写 ──

def _read_tmp(path: Path, label: str, source_step: str) -> dict:
    if not path.exists():
        logger.error(f"{label} 临时文件不存在 ({path})，请先执行 {source_step}")
        sys.exit(1)
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _write_tmp(path: Path, data: dict):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"{path.name} 已写入 {path}")


# ── CLI ──

def main():
    load_llm_env()
    parser = argparse.ArgumentParser(description="偏好初始化")
    subparsers = parser.add_subparsers(dest="step", required=True)

    # Step 1: mention-taste
    pm = subparsers.add_parser("mention-taste", help="领域关键词 + 喜欢/不喜欢话题类型 + 召回关键词")
    pm.add_argument("--domain-kw", nargs="*", required=True, help="领域关注关键词（2-5个）")
    pm.add_argument("--liked", default="", help="感兴趣分类（逗号分隔，不传则 LLM 自动匹配）")
    pm.add_argument("--disliked", default="", help="不感兴趣分类（逗号分隔，不传则 LLM 自动匹配）")
    pm.add_argument("--recall", default="", help="召回关键词（逗号分隔）")

    # Step 2: grill-taste
    pg = subparsers.add_parser("grill-taste", help="深度偏好访谈 → 输出 Q&A JSON")
    pg.add_argument("--answer", default="", help="本轮答案（Agent 模式），不传则生成首轮问题")

    # Step 3: build-criteria
    subparsers.add_parser("build-criteria", help="偏好配置 + 深度访谈 → LLM 生成判断标准")

    # Step 4: confirm-criteria
    pc = subparsers.add_parser("confirm-criteria", help="确认判断标准 → 写入配置")
    pc.add_argument("--yes", default="", help="覆盖 yes 判断标准")
    pc.add_argument("--no", default="", help="覆盖 no 判断标准")

    args = parser.parse_args()

    if args.step == "mention-taste":
        step_mention_taste(args)
    elif args.step == "grill-taste":
        step_grill_taste(args)
    elif args.step == "build-criteria":
        step_build_criteria(args)
    elif args.step == "confirm-criteria":
        step_confirm_criteria(args)


if __name__ == "__main__":
    main()
