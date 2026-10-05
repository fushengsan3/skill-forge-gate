#!/bin/bash
# 从 GitHub URL 安装 skill
# 用法: install.sh <github_url> [--name <name>] [--branch <branch>]
#
# ## 这个脚本原先漏掉的东西（2026-10-05 补）
#
# 1. **完全没有安全校验。** clone 完直接 `mv` 落地。也就是说 README 里写的
#    "L1-L5 安全验证流水线"在这条路上是空的 —— 只有面板/bridge 那条路
#    （daemon/installer.py）跑了预检。而 SKILL.md 让 Claude 走的正是**这条**。
#    现在两条路共用 daemon/precheck.py 同一个模块，免得各写一套慢慢走样。
#
# 2. **路径拼进 Python 源码。** `Path('$SOURCES_FILE')` 在 Git Bash 下是
#    `/c/Users/xxx/...`，Windows Python 打不开 —— 必然 FileNotFoundError。
#    更糟的是它发生在文件**已经落地之后**，于是留下"装了但没登记进
#    sources.json"的残局。和 self-update.sh 是同一个坑，一并改成 argv 传参。
#
# 3. **`--name` 没有路径校验。** 和 installer 那条路同一类问题：
#    名字会被拼进路径再 mv。现在走 daemon/safe_paths.py 同一个校验。
set -euo pipefail

export https_proxy="http://127.0.0.1:7897"
export http_proxy="http://127.0.0.1:7897"

SKILLS_ROOT="${SKILLS_ROOT:-$HOME/.claude/skills}"
SKILL_FORGE="$SKILLS_ROOT/skill-forge"
SOURCES_FILE="$SKILL_FORGE/sources.json"
BACKUP_DIR="$SKILL_FORGE/.backup"
TMP_DIR="$SKILL_FORGE/.tmp"

# python3 在 Windows 上常常是 Microsoft Store 的转发器，不一定真能跑
PYTHON=$(command -v python3 || command -v python || true)
[[ -n "$PYTHON" ]] || { echo '{"error": "找不到 python3 或 python"}' >&2; exit 3; }

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

# ---- 名字先过校验，再拿它拼任何路径 ----
# 顺序反了就等于没验。校验器和 installer 那条路共用同一个模块。
NAME_ERR=$("$PYTHON" -c "
import sys
sys.path.insert(0, sys.argv[1])
from daemon import safe_paths
try:
    print(safe_paths.check_name(sys.argv[2]))
except safe_paths.UnsafeName as e:
    print('ERR:' + str(e), file=sys.stderr)
    sys.exit(1)
" "$SKILL_FORGE" "$SKILL_NAME" 2>&1 >/dev/null) || {
    echo "{\"error\": \"skill 名不安全，拒绝安装：$SKILL_NAME（$NAME_ERR）\"}" >&2
    exit 2
}

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

# ---- 安装前预检：装了才算数，所以验在装之前 ----
#
# 与面板那条路（daemon/installer.py）**共用同一个模块** —— 两条路各写一套
# 判定，迟早会分叉，而分叉的那一边就是没人看着的那一边。
#
# 判定从严：REJECT / REVIEW 都拒（REVIEW 的定义是"需要人看一眼"，
# 而这条脚本路径上没有那个人 —— 退回给 Claude，由它读报告再决定）。
PRECHECK_JSON=$(cd "$SKILL_FORGE" && "$PYTHON" -m daemon.precheck \
    "$TMP_TARGET" "https://github.com/$OWNER/$REPO.git" 2>/dev/null) || PRECHECK_JSON=""

PRECHECK_OK=$(printf '%s' "$PRECHECK_JSON" | "$PYTHON" -c "
import json, sys
try:
    print('yes' if json.load(sys.stdin).get('ok') else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")

if [[ "$PRECHECK_OK" != "yes" ]]; then
    REASON=$(printf '%s' "$PRECHECK_JSON" | "$PYTHON" -c "
import json, sys
try:
    print(json.load(sys.stdin).get('summary', '预检未给出结论'))
except Exception:
    print('预检无法执行（daemon/precheck.py 跑不起来？）')
" 2>/dev/null || echo "预检无法执行")
    rm -rf "$TMP_TARGET"
    echo "{\"error\": \"预检未通过，已拒绝安装：$REASON\"}" >&2
    exit 5
fi

# 原子替换（rename swap）—— 只在预检通过之后才落地
mv "$TMP_TARGET" "$TARGET_DIR"

# 获取安装的 SHA
INSTALLED_SHA=$(cd "$TARGET_DIR" && git rev-parse HEAD 2>/dev/null || echo "unknown")

# 更新 sources.json
#
# 路径一律走 argv，**绝不**拼进 Python 源码：Git Bash 的 $HOME 是
# `/c/Users/xxx` 这种 POSIX 形式，写进源码后 Windows Python 打不开它。
# （这个坑 self-update.sh 也踩过。）
"$PYTHON" - "$SOURCES_FILE" "$SKILL_NAME" "$URL" "$OWNER" "$REPO" \
          "$BRANCH" "$SUBPATH" "$INSTALLED_SHA" <<'PYEOF'
import datetime, json, sys

(sf_path, name, url, owner, repo, branch, subpath, sha) = sys.argv[1:9]
with open(sf_path, encoding="utf-8") as f:
    data = json.load(f)
data[name] = {
    "type": "remote",
    "url": url,
    "owner": owner,
    "repo": repo,
    "branch": branch,
    "subpath": subpath,
    "installed_sha": sha,
    "installed_at": datetime.datetime.now(
        datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
with open(sf_path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
PYEOF

echo "{\"ok\": true, \"name\": \"$SKILL_NAME\", \"path\": \"$TARGET_DIR\", \"sha\": \"$INSTALLED_SHA\"}"
