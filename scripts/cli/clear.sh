#!/usr/bin/env bash
# 清理所有临时文件、缓存数据和用户配置，回到初始状态
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
TMP_DIR="$SCRIPT_DIR/tmp"
DATA_DIR="$SCRIPT_DIR/data"
LOG_DIR="$SCRIPT_DIR/log"
TASTE_FILE="$SCRIPT_DIR/config/taste.yaml"

echo "=== 清理开始 ==="

# 1. 清空 tmp/ 目录中的临时文件
if [ -d "$TMP_DIR" ]; then
    rm -rf "$TMP_DIR"/*
    echo "✓ tmp/ 已清空"
else
    echo "✓ tmp/ 目录不存在，跳过"
fi

# 2. 清空 data/ 中的缓存文件（保留文件但清空内容）
for f in all_topics.jsonl pushed_topics.jsonl ruleFiltered_topics.jsonl tasted_topics.jsonl; do
    target="$DATA_DIR/$f"
    if [ -f "$target" ]; then
        :> "$target"
        echo "✓ $f 已清空"
    else
        echo "✓ $f 不存在，跳过"
    fi
done

# 3. 清理日志文件
if [ -d "$LOG_DIR" ]; then
    count=$(find "$LOG_DIR" -name "*.log" -type f | wc -l)
    if [ "$count" -gt 0 ]; then
        rm -f "$LOG_DIR"/*.log
        echo "✓ log/ 已清空 ($count 个文件)"
    else
        echo "✓ log/ 为空，跳过"
    fi
else
    echo "✓ log/ 目录不存在，跳过"
fi

# 4. 重置 taste.yaml 为默认空值
cat > "$TASTE_FILE" << 'EOF'
# 领域关键词：用户关注的核心领域
domain_keywords: []

# 感兴趣的分类：初始化时 LLM 语义匹配得出
liked_categories: []

# 不感兴趣的分类：初始化时 LLM 语义匹配得出，同时用于规则过滤和 LLM 判断
disliked_categories: []

# 召回关键词：被排除分类中的话题，若包含这些关键词则救回
recall_keywords: []

# LLM 判断标准：推送/不推送的规则描述，由初始化或 fit_taste 生成
yes_criteria: ""
no_criteria: ""
EOF
echo "✓ taste.yaml 已重置为默认值"

echo "=== 清理完成 ==="
