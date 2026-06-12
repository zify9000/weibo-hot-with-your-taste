"""凭据更新脚本：将用户提供的凭据写入 env 文件"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from common import SCRIPT_DIR, setup_logging

logger = setup_logging("llm_feishu")

LLM_ENV_PATH = SCRIPT_DIR / "env" / ".llm.env"
FEISHU_ENV_PATH = SCRIPT_DIR / "env" / ".feishu.env"


def write_env_file(path, entries: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{k}={v}" for k, v in entries.items() if v]
    content = "\n".join(lines) + "\n"
    path.write_text(content, encoding="utf-8")
    logger.info(f"已写入 {path}")


def main():
    parser = argparse.ArgumentParser(description="更新凭据配置")
    parser.add_argument("--llm-model", default="", help="LLM 模型名")
    parser.add_argument("--llm-base-url", default="", help="LLM API 地址")
    parser.add_argument("--llm-api-key", default="", help="LLM API 密钥")
    parser.add_argument("--feishu-app-id", default="", help="飞书应用 ID")
    parser.add_argument("--feishu-app-secret", default="", help="飞书应用密钥")
    parser.add_argument("--feishu-chat-id", default="", help="飞书群聊 ID")
    args = parser.parse_args()

    changed = False

    if args.llm_api_key:
        write_env_file(LLM_ENV_PATH, {
            "llm_model": args.llm_model,
            "llm_base_url": args.llm_base_url,
            "llm_api_key": args.llm_api_key,
        })
        changed = True

    if args.feishu_app_id:
        write_env_file(FEISHU_ENV_PATH, {
            "feishu_app_id": args.feishu_app_id,
            "feishu_app_secret": args.feishu_app_secret,
            "feishu_chat_id": args.feishu_chat_id,
        })
        changed = True

    if not changed:
        logger.info("未提供任何凭据，无操作")
        print("用法: python3 scripts/init/llm_feishu.py --llm-model ... --llm-base-url ... --llm-api-key ... [--feishu-* ...]")


if __name__ == "__main__":
    main()
