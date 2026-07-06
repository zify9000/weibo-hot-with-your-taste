"""公共工具：配置加载、日志、格式化"""
import json as _json
import os
import sys
import time as time_module
import logging
from datetime import datetime, timedelta
from pathlib import Path

import requests

os.environ["TZ"] = "Asia/Shanghai"
time_module.tzset()

SCRIPT_DIR = Path(__file__).parent
CONFIG_DIR = SCRIPT_DIR / "config"
DATA_DIR = SCRIPT_DIR / "data"
TMP_DIR = SCRIPT_DIR / "tmp"
TMP_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR = SCRIPT_DIR / "log"

BASE_CONFIG_PATH = CONFIG_DIR / "base.yaml"
TASTE_CONFIG_PATH = CONFIG_DIR / "taste.yaml"
PROMPT_PATH = CONFIG_DIR / "prompt.yaml"
ALL_TOPICS_PATH = DATA_DIR / "all_topics.jsonl"
RULE_FILTERED_TOPICS_PATH = DATA_DIR / "ruleFiltered_topics.jsonl"
PUSHED_TOPICS_PATH = DATA_DIR / "pushed_topics.jsonl"
CATEGORY_STORE_PATH = DATA_DIR / "topic_category.json"
FETCH_META_PATH = TMP_DIR / "fetch_meta.jsonl"
FETCH_TOPICS_PATH = TMP_DIR / "fetch_topics.jsonl"
# 以下路径已废弃，保留向后兼容
CACHED_TOPICS_PATH = DATA_DIR / "cached_topics.jsonl"
INIT_LOG_PATH = LOG_DIR / "init.log"

# Cookie 过期标记文件：fetch.py 写入，notify_cookie_expired.py / weibo_wait_login.py 读写
# 放 /tmp 是因为该信号短时有效，重启清空不影响正确性（下次 fetch 重新检测写入）
COOKIE_STATUS_PATH = Path("/tmp/weibo_cookie_status.json")

# common 模块自身日志器（clear_cookie_status 等函数使用）
logger = logging.getLogger("common")


def setup_logging(name: str) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_level = os.environ.get("WEIBO_HOT_NEWS_LOG_LEVEL", "INFO").upper()
    today = datetime.now().strftime("%Y%m%d")
    fmt = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 清理超过 7 天的旧日志
    _cleanup_old_logs(LOG_DIR, name, keep_days=7)

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, log_level, logging.INFO))
    logger.propagate = False

    if not any(isinstance(h, logging.FileHandler) for h in logger.handlers):
        file_handler = logging.FileHandler(
            LOG_DIR / f"{name}_{today}.log", encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        logger.addHandler(file_handler)

    if not any(isinstance(h, logging.StreamHandler) for h in logger.handlers):
        stream_handler = logging.StreamHandler(sys.stderr)
        stream_handler.setFormatter(fmt)
        logger.addHandler(stream_handler)

    return logger


def _cleanup_old_logs(log_dir: Path, name: str, keep_days: int = 7):
    """删除超过 keep_days 天的旧日志文件"""
    cutoff = datetime.now() - timedelta(days=keep_days)
    pattern = f"{name}_*.log"
    for log_file in sorted(log_dir.glob(pattern)):
        try:
            # 从文件名提取日期，如 fetch_20260520.log → 20260520
            mtime = datetime.fromtimestamp(log_file.stat().st_mtime)
            if mtime < cutoff:
                log_file.unlink()
        except (ValueError, OSError):
            pass


def _load_env_file(filename: str):
    """加载 env 文件中的 key=value 到 os.environ，自动展开 ${VAR} 引用"""
    env_path = SCRIPT_DIR / "env" / filename
    if not env_path.exists():
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            sep = "=" if "=" in line else (":" if ":" in line else None)
            if sep:
                k, v = line.split(sep, 1)
                raw = v.strip().strip('"').strip("'")
                os.environ[k.strip()] = os.path.expandvars(raw)


def load_llm_env():
    """加载 .llm.env"""
    _load_env_file(".llm.env")


def load_feishu_env():
    """加载 .feishu.env"""
    _load_env_file(".feishu.env")


def load_weibo_env():
    """加载 .weibo.env"""
    _load_env_file(".weibo.env")


def get_weibo_cookie() -> str:
    """从环境变量读取微博 Cookie（SUB 字段）"""
    return os.environ.get("weibo_sub", "")


def get_weibo_cookies() -> dict:
    """从环境变量读取微博完整 Cookie 字典，浏览器登录后可用"""
    raw = os.environ.get("weibo_cookies_json", "")
    if raw:
        try:
            return _json.loads(raw)
        except _json.JSONDecodeError:
            pass
    # 回退：仅 SUB cookie
    sub = get_weibo_cookie()
    return {"SUB": sub} if sub else {}


def resolve_llm_creds(config: dict, cli_model="", cli_base_url="", cli_api_key="") -> tuple:
    """解析 LLM 凭据：CLI 参数优先，否则从环境变量读取"""
    env_model, env_base_url, env_api_key = get_llm_creds()
    return (
        cli_model or env_model,
        cli_base_url or env_base_url,
        cli_api_key or env_api_key,
    )


def get_llm_creds() -> tuple:
    """从环境变量读取 LLM 凭据"""
    return (
        os.environ.get("llm_model", ""),
        os.environ.get("llm_base_url", ""),
        os.environ.get("llm_api_key", ""),
    )


def validate_llm_creds(llm_model: str, llm_base_url: str, llm_api_key: str) -> list:
    """校验 LLM 凭据，返回问题列表。空列表表示无问题。"""
    issues = []
    if not llm_model:
        issues.append("llm_model 为空")
    if not llm_base_url:
        issues.append("llm_base_url 为空")
    if not llm_api_key:
        issues.append("llm_api_key 为空")
    elif "${" in llm_api_key:
        issues.append("llm_api_key 包含未展开的 ${...} 变量引用")
    return issues


def get_feishu_creds() -> tuple:
    """从环境变量读取飞书凭据"""
    return (
        os.environ.get("feishu_app_id", ""),
        os.environ.get("feishu_app_secret", ""),
        os.environ.get("feishu_chat_id", ""),
    )


def load_base_config() -> dict:
    import yaml

    cfg = {}
    if BASE_CONFIG_PATH.exists():
        with open(BASE_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}

    return cfg


def load_rule_config() -> dict:
    import yaml

    if not TASTE_CONFIG_PATH.exists():
        return {"domain_keywords": [], "liked_categories": [], "disliked_categories": [], "recall_keywords": []}
    with open(TASTE_CONFIG_PATH) as f:
        return yaml.safe_load(f) or {}


def load_prompt(key: str) -> str:
    import yaml

    if not PROMPT_PATH.exists():
        raise FileNotFoundError(f"prompt.yaml 不存在: {PROMPT_PATH}")
    with open(PROMPT_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data[key]


def is_initialized() -> bool:
    """检查是否已完成初始化：taste.yaml 中存在 yes_criteria 且非空"""
    rule = load_rule_config()
    return bool(rule.get("yes_criteria"))


def load_init_log() -> dict | None:
    """加载初始化记录（从 init.log），未初始化返回 None"""
    if not INIT_LOG_PATH.exists():
        return None
    import json
    with open(INIT_LOG_PATH, encoding="utf-8") as f:
        return json.load(f)


def load_topics_for_taste_judge_prompt() -> str:
    """加载 topics_for_taste_judge_prompt 模板并填充用户偏好变量（全部从 taste.yaml 读取），返回仅剩 {topics_list}/{topics_count} 的模板"""
    template = load_prompt("topics_for_taste_judge_prompt")
    rule = load_rule_config()

    domain_keywords = rule.get("domain_keywords", [])
    liked_categories = rule.get("liked_categories", [])
    disliked_categories = rule.get("disliked_categories", [])
    recall_keywords = rule.get("recall_keywords", [])
    yes_criteria = rule.get("yes_criteria", "")
    no_criteria = rule.get("no_criteria", "")

    return template.format(
        domain_keywords="/".join(domain_keywords) or "未设置",
        liked_categories="、".join(liked_categories) or "未设置",
        disliked_categories="、".join(disliked_categories) or "未设置",
        recall_keywords="、".join(recall_keywords) or "无",
        yes_criteria=yes_criteria or "（未设置判断标准，请先运行初始化）",
        no_criteria=no_criteria or "（未设置判断标准，请先运行初始化）",
        topics_list="{topics_list}",
        topics_count="{topics_count}",
    )


def retry(times=3, delay=5, backoff=2, logger=None):
    def decorator(func):
        def wrapper(*args, **kwargs):
            _logger = logger or logging.getLogger(func.__module__)
            current_delay = delay
            for attempt in range(1, times + 1):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    if attempt == times:
                        raise
                    _logger.warning(f"第{attempt}次失败: {e}，{current_delay}秒后重试")
                    time_module.sleep(current_delay)
                    current_delay *= backoff
            return None
        return wrapper
    return decorator


def format_hotness(raw_hot) -> str:
    if raw_hot >= 10_000_000:
        return f"{raw_hot / 10_000_000:.1f}千万"
    elif raw_hot >= 10_000:
        return f"{raw_hot / 10_000:.1f}万"
    elif raw_hot >= 1000:
        return f"{raw_hot / 1000:.1f}千"
    return str(raw_hot)


def clean_word(w: str) -> str:
    return w.strip("#") if w else ""


# ── 飞书 / Cookie 状态 公共辅助 ──

@retry(times=3, delay=2, backoff=2, logger=logger)
def get_feishu_token(app_id: str, app_secret: str) -> str:
    """获取飞书 tenant_access_token（供 push.py / notify_cookie_expired.py 共用）

    使用 requests 而非 curl_cffi：飞书开放平台 API 无反爬需求，保持依赖最小。
    """
    resp = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    result = resp.json()
    if result.get("code") != 0:
        raise Exception(f"获取飞书 token 失败: code={result.get('code')} msg={result.get('msg')}")
    return result["tenant_access_token"]


def read_cookie_status() -> dict | None:
    """读取 Cookie 状态标记文件，不存在或解析失败返回 None"""
    if not COOKIE_STATUS_PATH.exists():
        return None
    try:
        return _json.loads(COOKIE_STATUS_PATH.read_text(encoding="utf-8"))
    except (_json.JSONDecodeError, OSError) as e:
        logger.warning(f"读取 Cookie 状态文件失败: {e}")
        return None


def clear_cookie_status():
    """清除 Cookie 过期标记文件（登录成功后调用，失败不抛异常仅记日志）"""
    try:
        COOKIE_STATUS_PATH.unlink(missing_ok=True)
        logger.info("已清除 Cookie 过期标记文件")
    except OSError as e:
        logger.warning(f"清除 Cookie 状态文件失败: {e}")
