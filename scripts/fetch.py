"""微博热榜抓取：抓取 → 规则过滤 → 规则反写 → LLM核校 → 缓存重要话题"""
import sys
import json
import re
import fcntl
import logging
from datetime import datetime
from urllib.parse import quote

import requests as req

from common import (
    SCRIPT_DIR, DATA_DIR,
    ALL_TOPICS_PATH, RULE_FILTERED_TOPICS_PATH, CATEGORY_STORE_PATH,
    FETCH_META_PATH, FETCH_TOPICS_PATH,
    setup_logging, load_base_config, load_llm_env, load_weibo_env, load_rule_config, load_topics_for_taste_judge_prompt, load_prompt,
    get_llm_creds, validate_llm_creds, get_weibo_cookies, format_hotness, clean_word,
)

logger = setup_logging("fetch")

BASE_CONFIG = load_base_config()
RULE_CONFIG = load_rule_config()

EXCLUDE_CATEGORIES = set(RULE_CONFIG.get("disliked_categories", []))
RECALL_KEYWORDS = set(RULE_CONFIG.get("recall_keywords", []))

SUMMARY_CONFIG = BASE_CONFIG.get("enrich", {})
SHORT_TOPIC_MAX_LEN = SUMMARY_CONFIG.get("short_topic_max_len", 5)
MAX_SUMMARY_LEN = SUMMARY_CONFIG.get("max_summary_len", 20)
TOP_WEIBO_COUNT = SUMMARY_CONFIG.get("top_weibo_count", 10)


def fetch_weibo_hot() -> list:
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://weibo.com",
    }
    r = req.get("https://weibo.com/ajax/statuses/hot_band", headers=headers, timeout=10)
    r.raise_for_status()
    d = r.json()
    return d.get("data", {}).get("band_list", [])


def save_topics(all_raw: list):
    """保存原始抓取数据到 all_topics.jsonl"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now()
    record = {
        "ts": now.isoformat(),
        "total": len(all_raw),
        "topics": [{"word": clean_word(item.get("word", "")), "category": item.get("category", "")} for item in all_raw],
    }

    with open(ALL_TOPICS_PATH, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    logger.info(f"all_topics.jsonl 已追加: {len(all_raw)} 条")


def apply_rules(all_raw: list) -> tuple:
    """规则过滤 + 反写，返回 (candidates, excluded)"""
    candidates = []
    excluded = []

    for item in all_raw:
        rank = item.get("realpos", 0)
        if rank == 0:
            continue

        word = clean_word(item.get("word_scheme", item.get("word", "")))
        category = item.get("category", "")
        field_tag = item.get("field_tag", "")
        raw_hot = item.get("num", item.get("raw_hot", 0))
        note = item.get("note", "")

        entry = {
            "rank": rank,
            "word": word,
            "category": category,
            "field_tag": field_tag,
            "raw_hot": raw_hot,
            "hot_str": format_hotness(raw_hot),
            "note": note,
        }

        combined_text = f"{category} {field_tag} {word} {note}"
        excluded_by_cat = any(excluded_cat in (category or "") or excluded_cat in (field_tag or "") for excluded_cat in EXCLUDE_CATEGORIES)

        if excluded_by_cat:
            rescued = any(kw in combined_text for kw in RECALL_KEYWORDS)
            if rescued:
                candidates.append(entry)
                logger.debug(f"反写救回: {word}")
            else:
                excluded.append(entry)
        else:
            candidates.append(entry)

    logger.info(f"规则过滤: {len(candidates)} 候选, {len(excluded)} 排除")
    return candidates, excluded


def fetch_topic_detail(word: str, cookies: dict) -> list:
    """获取话题下热度最高的微博内容列表（m.weibo.cn API），返回 [str, ...]"""
    import requests as req

    if not cookies or not cookies.get("SUB"):
        return []

    cookie_str = "; ".join(f"{k}={v}" for k, v in cookies.items())
    try:
        resp = req.get(
            f"https://m.weibo.cn/api/container/getIndex?containerid=100103type%3D1%26q%3D{quote(word)}&page_type=searchall",
            headers={
                "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148",
                "Cookie": cookie_str,
                "Referer": "https://m.weibo.cn/",
            },
            timeout=15,
        )
        data = resp.json()
        if data.get("ok") != 1:
            logger.warning(f"m.weibo 搜索失败: {word} - ok={data.get('ok')}")
            return []

        cards = data.get("data", {}).get("cards", []) or []
        contents = []
        for card in cards:
            if card.get("card_type") != 9:
                continue
            mblog = card.get("mblog", {})
            if mblog:
                text = mblog.get("text", "")
                clean_text = re.sub(r"<[^>]+>", "", text).strip()
                if clean_text:
                    contents.append(clean_text)
            if len(contents) >= TOP_WEIBO_COUNT:
                break

        return contents

    except json.JSONDecodeError:
        # JSON 解析失败通常是 Cookie 过期导致 API 返回登录页面 HTML
        # 检查响应内容是否确实是 HTML 登录页
        try:
            raw = resp.text[:200].lower()
            if "<html" in raw or "<!doctype" in raw:
                logger.warning(f"获取话题详情失败（疑似 Cookie 已过期，API 返回登录页面）: {word}")
            else:
                logger.warning(f"获取话题详情失败（非 JSON 响应）: {word} - {resp.text[:100]}")
        except Exception:
            logger.warning(f"获取话题详情失败（JSON 解析异常）: {word}")
        return []
    except Exception as e:
        logger.warning(f"获取话题详情异常: {word} - {e}")
        return []


COOKIE_STATUS_PATH = Path("/tmp/weibo_cookie_status.json")


def _write_cookie_status(status: str):
    """写入 Cookie 状态标记文件，供 cron wrapper 或 agent 检测"""
    try:
        COOKIE_STATUS_PATH.write_text(
            json.dumps({"status": status, "checked_at": datetime.now().isoformat()}, ensure_ascii=False)
        )
    except OSError as e:
        logger.warning(f"写入 Cookie 状态文件失败: {e}")


def _truncate_to_sentence(summary: str, max_len: int, word: str) -> str:
    """智能截断：优先在句末标点处切断，无标点时降级为硬截断"""
    if len(summary) <= max_len:
        return summary

    # 在 max_len 范围内从后往前找最后一个句子结束标点
    window = summary[:max_len]
    for i in range(len(window) - 1, max(0, max_len // 2), -1):
        if window[i] in "。！？；\n":
            result = summary[:i+1]
            logger.info(f"摘要超长已智能截断: {word} ({len(summary)}→{len(result)}字)")
            return result

    # 无合适断点，硬截断并警告
    logger.warning(f"摘要超长({len(summary)}字)且无句末标点，硬截断: {word}")
    return summary[:max_len]


def generate_summary(word: str, weibo_contents: list, llm_model="", base_url="", api_key="") -> str:
    """用 LLM 根据微博内容生成一句话摘要"""
    import openai

    if not weibo_contents or not api_key:
        return ""

    content_text = "\n".join(f"- {c}" for c in weibo_contents)
    prompt_template = load_prompt("summary_prompt")
    prompt = prompt_template.format(
        topic_name=word,
        weibo_content=content_text,
        max_len=MAX_SUMMARY_LEN,
    )

    client = openai.OpenAI(api_key=api_key, base_url=base_url)
    try:
        resp = client.chat.completions.create(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=100,
            timeout=30,
        )
        summary = resp.choices[0].message.content.strip()
        return _truncate_to_sentence(summary, MAX_SUMMARY_LEN, word)
    except Exception as e:
        logger.warning(f"生成摘要失败: {word} - {e}")
        return ""


def check_info_sufficiency(topics: list, llm_model: str, base_url: str, api_key: str) -> set:
    """批量调用 LLM 判断话题信息量是否充足，返回信息不足的话题 word 集合"""
    import openai

    if not topics or not api_key:
        return set()

    rule_config = load_rule_config()
    info_check_criteria = rule_config.get("info_check_criteria", "")

    topics_list = "\n".join(f'{i+1}. {n["word"]}' for i, n in enumerate(topics))
    prompt_template = load_prompt("info_check_prompt")
    prompt = prompt_template.format(topics_list=topics_list, info_check_criteria=info_check_criteria)

    client = openai.OpenAI(api_key=api_key, base_url=base_url)
    try:
        resp = client.chat.completions.create(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
            timeout=30,
        )
        raw = resp.choices[0].message.content.strip()
        # 解析序号：每行一个数字
        insufficient_indices = set()
        for line in raw.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                idx = int(line)
                if 1 <= idx <= len(topics):
                    insufficient_indices.add(idx - 1)
            except ValueError:
                continue

        insufficient = {topics[i]["word"] for i in insufficient_indices}
        logger.info(f"信息量判断: {len(topics)} 个话题中 {len(insufficient)} 个信息不足")
        return insufficient
    except Exception as e:
        logger.warning(f"信息量判断失败: {e}")
        return set()


def enrich_insufficient_topics(candidates: list, llm_model="", base_url="", api_key="") -> list:
    """对信息量不足的话题补充摘要

    短话题（≤ short_topic_max_len）：直接搜索微博内容 → LLM 生成摘要
    长话题（> short_topic_max_len）：LLM 批量判断信息量 → 不足的搜索补充
    """
    cookies = get_weibo_cookies()
    if not cookies or not cookies.get("SUB"):
        logger.info("未配置微博 Cookie，跳过话题摘要补充")
        return candidates

    need_check = [n for n in candidates if not n.get("summary")]
    if not need_check:
        return candidates

    short_topics = [n for n in need_check if len(n.get("word", "")) <= SHORT_TOPIC_MAX_LEN]
    long_topics = [n for n in need_check if len(n.get("word", "")) > SHORT_TOPIC_MAX_LEN]

    topics_to_enrich = list(short_topics)

    if long_topics and api_key:
        insufficient_words = check_info_sufficiency(long_topics, llm_model, base_url, api_key)
        llm_insufficient = [n for n in long_topics if n["word"] in insufficient_words]
        topics_to_enrich.extend(llm_insufficient)
        logger.info(f"信息量判断: {len(long_topics)} 个长话题中 {len(llm_insufficient)} 个信息不足")

    if not topics_to_enrich:
        logger.info("无需补充摘要的话题")
        return candidates

    logger.info(f"开始补充摘要: {len(topics_to_enrich)} 个话题")

    success_count = 0
    fail_count = 0
    for n in topics_to_enrich:
        word = n["word"]
        contents = fetch_topic_detail(word, cookies)
        if contents:
            success_count += 1
            summary = generate_summary(word, contents, llm_model, base_url, api_key)
            if summary:
                n["summary"] = summary
                n["enrich_source"] = contents
                logger.info(f"摘要: {word} → {summary}")
            else:
                logger.info(f"摘要生成失败: {word}")
        else:
            fail_count += 1
            logger.info(f"未获取到话题详情: {word}")

    # 汇总统计
    total_attempted = success_count + fail_count
    if total_attempted > 0:
        logger.info(f"摘要补充完成: 成功 {success_count}/{total_attempted}, 失败 {fail_count}/{total_attempted}")
        if fail_count == total_attempted:
            logger.warning(
                "⚠️  所有话题详情获取均失败，微博 Cookie 可能已过期。"
                "请重新执行登录流程：python3 scripts/init/weibo_get_qr.py → 扫码 → python3 scripts/init/weibo_wait_login.py"
            )
            # 写标记文件，供 cron wrapper 检测
            _write_cookie_status("expired")
        elif fail_count > 0:
            logger.warning(f"部分话题详情获取失败 ({fail_count}/{total_attempted})，可能是网络问题或个别话题受限")

    return candidates


def save_rule_checked_topics(candidates: list):
    """将规则过滤后的候选写入 ruleFiltered_topics.jsonl"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat()

    record = {
        "ts": now,
        "total": len(candidates),
        "topics": [
            {
                "rank": n.get("rank", 0),
                "word": n["word"],
                "category": n.get("category", ""),
                "raw_hot": n.get("raw_hot", 0),
                "hot_str": n.get("hot_str", ""),
            }
            for n in candidates
        ],
    }

    with open(RULE_FILTERED_TOPICS_PATH, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    logger.info(f"ruleFiltered_topics.jsonl 已追加: {len(candidates)} 条")


def call_llm_judge(topic_items: list, llm_model="", base_url="", api_key="") -> list:
    """LLM 首次核校"""
    import openai

    issues = validate_llm_creds(llm_model, base_url, api_key)
    if issues:
        logger.warning(f"LLM 凭据异常，跳过评估: {'; '.join(issues)}")
        return None

    client = openai.OpenAI(api_key=api_key, base_url=base_url)

    topic_lines = []
    for i, n in enumerate(topic_items):
        category = n.get("category") or n.get("field_tag") or ""
        topic_lines.append(f"{i+1}. {n.get('word','')} | 分类:{category} | 热度:{n.get('hot_str','')}")

    topics_list = "\n".join(topic_lines)
    prompt_template = load_topics_for_taste_judge_prompt()
    prompt = prompt_template.format(topics_list=topics_list, topics_count=len(topic_items))

    try:
        stream = client.chat.completions.create(
            model=llm_model,
            messages=[{"role": "user", "content": prompt}],
            temperature=BASE_CONFIG["llm"]["temperature"],
            max_tokens=BASE_CONFIG["llm"]["max_tokens"],
            timeout=BASE_CONFIG["llm"]["timeout"],
            stream=True,
        )
        content = ""
        for chunk in stream:
            if chunk.choices and chunk.choices[0].delta.content:
                content += chunk.choices[0].delta.content
                print(chunk.choices[0].delta.content, end="", flush=True, file=sys.stderr)
        print(file=sys.stderr)
        if not content:
            logger.warning("LLM 返回内容为空")
            return None

        result_text = content.strip()
        logger.info(f"LLM 评估完成，响应长度: {len(result_text)} 字")

        important_map = {}
        for line in result_text.split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                idx = int(line)
                if 1 <= idx <= len(topic_items):
                    important_map[idx] = True
            except ValueError:
                continue

        logger.info(f"解析出 {len(important_map)} 条判断")

        for i, n in enumerate(topic_items):
            n["important"] = important_map.get(i + 1, False)

        return topic_items

    except Exception as e:
        logger.error(f"LLM 调用失败: {e}")
        return None


def update_category_store(all_raw: list):
    """将本次热榜的 category 写入 topic_category.json"""
    if not all_raw:
        return

    store = {"categories": [], "last_updated": ""}
    if CATEGORY_STORE_PATH.exists():
        try:
            with open(CATEGORY_STORE_PATH, encoding="utf-8") as f:
                store = json.load(f)
                if isinstance(store.get("categories"), dict):
                    store["categories"] = list(store["categories"].keys())
        except Exception as e:
            logger.warning(f"读取 topic_category.json 失败，将重建: {e}")

    now = datetime.now()
    new_cats = 0

    for item in all_raw:
        for category in (item.get("category") or "").split(","):
            category = category.strip()
            if category and category not in store["categories"]:
                store["categories"].append(category)
                new_cats += 1

    store["last_updated"] = now.isoformat()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd = open(CATEGORY_STORE_PATH, "w", encoding="utf-8")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX)
        json.dump(store, fd, ensure_ascii=False, indent=2)
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()

    if new_cats:
        logger.info(f"topic_category.json 已更新: +{new_cats} 新分类 (共 {len(store['categories'])} 分类)")


def save_fetch_result(candidates: list, judged: list | None, llm_ok: bool):
    """写入缓存 meta + topics

    - meta: 记录本轮抓取状态，携带完整候选话题数据
      - llm_ok → candidates + important_idx
      - llm_failed → candidates（供 push 阶段补跑 judge）
    - topics: 扁平话题列表
      - llm_ok → 仅写 important 子集
      - llm_failed → 不写（候选在 meta 中，push 阶段补跑 judge 后写入）
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now().isoformat()

    # ── 构建候选数据 ──
    def _strip(n: dict) -> dict:
        result = {
            "rank": n.get("rank", 0),
            "word": n["word"],
            "category": n.get("category", ""),
            "field_tag": n.get("field_tag", ""),
            "raw_hot": n.get("raw_hot", 0),
            "hot_str": n.get("hot_str", ""),
            "note": n.get("note", ""),
        }
        if n.get("summary"):
            result["summary"] = n["summary"]
        if n.get("enrich_source"):
            result["enrich_source"] = n["enrich_source"]
        return result

    # ── 写 meta ──
    meta = {
        "ts": now,
        "llm": "ok" if llm_ok else "failed",
        "candidates": [_strip(n) for n in candidates],
    }
    if llm_ok and judged:
        important_idx = [i for i, n in enumerate(judged) if n.get("important")]
        meta["important_idx"] = important_idx

    with open(FETCH_META_PATH, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    n_important = len(meta.get("important_idx", []))
    logger.info(f"fetch_meta.jsonl 已写入: llm={meta['llm']}, candidates={len(candidates)}, important={n_important}")

    # ── 写 topics ──
    if llm_ok and judged:
        topics_to_write = [n for n in judged if n.get("important")]
    else:
        # LLM 失败时不写 topics，候选在 meta 中，push 阶段补跑 judge
        topics_to_write = []

    if not topics_to_write:
        if llm_ok:
            logger.info("无 important 话题可缓存")
        return

    count = 0
    with open(FETCH_TOPICS_PATH, "a", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        for n in topics_to_write:
            record = {
                "cycle_ts": now,
                "rank": n.get("rank", 0),
                "word": n["word"],
                "category": n.get("category", ""),
                "field_tag": n.get("field_tag", ""),
                "raw_hot": n.get("raw_hot", 0),
                "hot_str": n.get("hot_str", ""),
                "note": n.get("note", ""),
            }
            if n.get("summary"):
                record["summary"] = n["summary"]
            if n.get("enrich_source"):
                record["enrich_source"] = n["enrich_source"]
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    logger.info(f"fetch_topics.jsonl 已缓存 {count} 条话题")


def main():
    logger.info("=== 开始抓取 ===")

    load_llm_env()
    llm_model, llm_base_url, llm_api_key = get_llm_creds()

    llm_issues = validate_llm_creds(llm_model, llm_base_url, llm_api_key)
    if llm_issues:
        logger.warning(f"LLM 凭据异常，将跳过 LLM 核校: {', '.join(llm_issues)}")

    try:
        all_raw = fetch_weibo_hot()
        logger.info(f"抓取到 {len(all_raw)} 条热搜")
    except Exception as e:
        logger.error(f"抓取微博热榜失败: {e}")
        sys.exit(1)

    save_topics(all_raw)
    update_category_store(all_raw)

    candidates, excluded = apply_rules(all_raw)
    if not candidates:
        logger.info("规则过滤后无候选，跳过")
        return

    save_rule_checked_topics(candidates)

    # 对信息量不足的话题补充摘要信息
    load_weibo_env()
    candidates = enrich_insufficient_topics(candidates, llm_model, llm_base_url, llm_api_key)

    judged = call_llm_judge(candidates, llm_model, llm_base_url, llm_api_key)
    if judged is None:
        logger.warning("LLM 判断失败，回退为规则过滤全量缓存")
        save_fetch_result(candidates, None, llm_ok=False)
        logger.info(f"=== 抓取完成: 候选 {len(candidates)}, 排除 {len(excluded)}, LLM=failed (规则兜底) ===")
    else:
        save_fetch_result(candidates, judged, llm_ok=True)
        important_count = sum(1 for n in judged if n.get("important"))
        logger.info(f"=== 抓取完成: 候选 {len(candidates)}, 排除 {len(excluded)}, 缓存 {important_count} ===")


if __name__ == "__main__":
    main()
