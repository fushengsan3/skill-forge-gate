#!/bin/bash
# 从 GitHub URL 安装 skill
# 用法: install.sh <github_url> [--name <name>] [--branch <branch>]
set -euo pipefail

export https_proxy="http://127.0.0.1:7897"
export http_proxy="http://127.0.0.1:7897"

SKILLS_ROOT="${SKILLS_ROOT:-$HOME/.claude/skills}"
SKILL_FORGE="$SKILLS_ROOT/skill-forge"
SOURCES_FILE="$SKILL_FORGE/sources.json"
BACKUP_DIR="$SKILL_FORGE/.backup"
TMP_DIR="$SKILL_FORGE/.tmp"

URL=""
SKILL_NAME=""
BRANCH="main"

# 解析参数
while [[ $# -gt 0 ]]; do
    case "$1" in
        --name) SKILL_NAME="$2"; shift 2 ;;
        --branch) BRANCH="$2"; shift 2 ;;
        *) URL="$1"; shift ;;
    esac
done

if [[ -z "$URL" ]]; then
    echo '{"error": "Usage: install.sh <github_url> [--name <name>] [--branch <branch>]"}' >&2
    exit 2
fi

# 提取 owner/repo/branch/subpath
# 支持格式: https://github.com/owner/repo/tree/branch/path/to/skill
OWNER=$(echo "$URL" | sed -n 's|.*github\.com/\([^/]*\)/.*|\1|p')
REPO=$(echo "$URL" | sed -n 's|.*github\.com/[^/]*/\([^/]*\).*|\1|p')
REPO="${REPO%.git}"  # 去除 .git 后缀，防止双重后缀
SUBPATH=$(echo "$URL" | sed -n 's|.*github\.com/[^/]*/[^/]*/tree/[^/]*/\(.*\)|\1|p')

# 从 URL 提取 branch（如果在 tree/ 中指定了）
URL_BRANCH=$(echo "$URL" | sed -n 's|.*github\.com/[^/]*/[^/]*/tree/\([^/]*\).*|\1|p')
if [[ -n "$URL_BRANCH" ]]; then
    BRANCH="$URL_BRANCH"
fi

# 如果没有指定名称，用 repo 或 subpath 的最后一段
if [[ -z "$SKILL_NAME" ]]; then
    if [[ -n "$SUBPATH" ]]; then
        SKILL_NAME=$(basename "$SUBPATH")
    else
        SKILL_NAME="$REPO"
    fi
fi

TARGET_DIR="$SKILLS_ROOT/$SKILL_NAME"

# 检查同名 skill 是否已存在
if [[ -d "$TARGET_DIR" ]]; then
    echo "{\"error\": \"skill '$SKILL_NAME' 已存在，请先卸载或改名\"}" >&2
    exit 2
fi

# 创建临时目录和备份目录
mkdir -p "$TMP_DIR" "$BACKUP_DIR"

# 克隆到临时目录
TMP_TARGET="$TMP_DIR/$SKILL_NAME-$$-$(date +%s)"
if ! git clone --depth 1 --branch "$BRANCH" "https://github.com/$OWNER/$REPO.git" "$TMP_TARGET" 2>/dev/null; then
    echo "{\"error\": \"克隆失败: https://github.com/$OWNER/$REPO.git (branch: $BRANCH)\"}" >&2
    rm -rf "$TMP_TARGET"
    exit 4
fi

# 如果有 subpath，只保留对应子目录
if [[ -n "$SUBPATH" ]]; then
    SUB_TARGET="$TMP_TARGET/$SUBPATH"
    if [[ ! -d "$SUB_TARGET" ]]; then
        echo "{\"error\": \"skill 子目录不存在: $SUBPATH\"}" >&2
        rm -rf "$TMP_TARGET"
        exit 4
    fi
    mv "$SUB_TARGET" "$TMP_TARGET-skill"
    rm -rf "$TMP_TARGET"
    mv "$TMP_TARGET-skill" "$TMP_TARGET"
fi

# 原子替换（rename swap）
mv "$TMP_TARGET" "$TARGET_DIR"

# 获取安装的 SHA
INSTALLED_SHA=$(cd "$TARGET_DIR" && git rev-parse HEAD 2>/dev/null || echo "unknown")

# 更新 sources.json
python3 -c "
import json, sys
from pathlib import Path

sf = Path('$SOURCES_FILE')
data = json.loads(sf.read_text()) if sf.exists() else {}
data['$SKILL_NAME'] = {
    'type': 'remote',
    'url': '$URL',
    'owner': '$OWNER',
    'repo': '$REPO',
    'branch': '$BRANCH',
    'subpath': '$SUBPATH',
    'installed_sha': '$INSTALLED_SHA',
    'installed_at': '$(date -u +%Y-%m-%dT%H:%M:%SZ)'
}
sf.write_text(json.dumps(data, ensure_ascii=False, indent=2))
"

echo "{\"ok\": true, \"name\": \"$SKILL_NAME\", \"path\": \"$TARGET_DIR\", \"sha\": \"$INSTALLED_SHA\"}"
