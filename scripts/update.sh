#!/bin/bash
# 更新一个 skill 到最新版本
# 用法: update.sh <skill_name> [--dry-run]
set -euo pipefail

export https_proxy="http://127.0.0.1:7897"
export http_proxy="http://127.0.0.1:7897"

SKILLS_ROOT="${SKILLS_ROOT:-$HOME/.claude/skills}"
SKILL_FORGE="$SKILLS_ROOT/skill-forge"
SOURCES_FILE="$SKILL_FORGE/sources.json"
BACKUP_DIR="$SKILL_FORGE/.backup"

SKILL_NAME="${1:-}"
DRY_RUN=false
[[ "${2:-}" == "--dry-run" ]] && DRY_RUN=true

if [[ -z "$SKILL_NAME" ]]; then
    echo '{"error": "Usage: update.sh <skill_name> [--dry-run]"}' >&2
    exit 2
fi

# ⚠️ 同 uninstall.sh：名字校验**必须在拼路径之前**，否则等于没验。
# 这个脚本此前零校验，而它最后是 `rm -rf "$TARGET_DIR"` 然后 `mv` ——
# 所以 `update.sh ..` 会删掉 $SKILLS_ROOT 的父目录。
# 规则与 `daemon/safe_paths.py::check_name` 保持同一套；改一处必须同时改另一处。
if [[ "$SKILL_NAME" == "." || "$SKILL_NAME" == ".." ]]; then
    echo "{\"error\": \"拒绝：名字不能是 '$SKILL_NAME' —— 它会指到别的目录去\"}" >&2
    exit 2
fi
if [[ "$SKILL_NAME" == */* || "$SKILL_NAME" == *\\* || "$SKILL_NAME" == *[\<\>\:\"\|\?\*]* ]]; then
    echo "{\"error\": \"拒绝：名字里有路径分隔符或 Windows 保留字符\"}" >&2
    exit 2
fi

# 更新自己 = 用**远端仓库的内容整体替换掉管理器**，包括正在运行的这个脚本。
# 这条路径目前**还没有接过预检**（见 SKILL.md 的已知欠账），所以更不该允许它
# 作用在自身上：那等于绕开全部检查换一整套代码进去。
if [[ "$SKILL_NAME" == "skill-forge" ]]; then
    echo "{\"error\": \"拒绝更新 skill-forge 本体（它没有预检路径，请手工处理）\"}" >&2
    exit 2
fi

TARGET_DIR="$SKILLS_ROOT/$SKILL_NAME"

if [[ ! -d "$TARGET_DIR" ]]; then
    echo "{\"error\": \"skill '$SKILL_NAME' 未安装\"}" >&2
    exit 2
fi

# 读取来源信息
SOURCE_INFO=$(python3 -c "
import json
data = json.loads(open('$SOURCES_FILE').read())
print(json.dumps(data.get('$SKILL_NAME', {})))
")

if [[ "$SOURCE_INFO" == "{}" ]]; then
    echo "{\"error\": \"skill '$SKILL_NAME' 在 sources.json 中无记录\"}" >&2
    exit 3
fi

SOURCE_URL=$(echo "$SOURCE_INFO" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('url',''))")
SOURCE_BRANCH=$(echo "$SOURCE_INFO" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('branch','main'))")
OLD_SHA=$(echo "$SOURCE_INFO" | python3 -c "import json,sys; print(json.loads(sys.stdin.read()).get('installed_sha',''))")

if [[ "$DRY_RUN" == true ]]; then
    # 仅检查更新
    REMOTE_SHA=$(timeout 15 git ls-remote "$(echo "$SOURCE_URL" | sed 's|tree/.*||')" "refs/heads/$SOURCE_BRANCH" 2>/dev/null | cut -f1 || echo "")
    if [[ -z "$REMOTE_SHA" ]]; then
        echo "{\"name\": \"$SKILL_NAME\", \"status\": \"unknown\", \"error\": \"无法检查远端\"}"
    elif [[ "$REMOTE_SHA" == "$OLD_SHA" ]]; then
        echo "{\"name\": \"$SKILL_NAME\", \"status\": \"up_to_date\", \"sha\": \"$OLD_SHA\"}"
    else
        echo "{\"name\": \"$SKILL_NAME\", \"status\": \"update_available\", \"old_sha\": \"$OLD_SHA\", \"new_sha\": \"$REMOTE_SHA\"}"
    fi
    exit 0
fi

# 创建备份
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_PATH="$BACKUP_DIR/$SKILL_NAME-$TIMESTAMP-$(uuidgen 2>/dev/null | cut -c1-8 || echo $RANDOM)"
mkdir -p "$BACKUP_PATH"
cp -r "$TARGET_DIR" "$BACKUP_PATH/"

# 执行更新（原子替换）
TMP_UPDATE="$SKILL_FORGE/.tmp/update-$SKILL_NAME-$$"
rm -rf "$TMP_UPDATE"

OWNER=$(echo "$SOURCE_URL" | sed -n 's|.*github\.com/\([^/]*\)/.*|\1|p')
REPO=$(echo "$SOURCE_URL" | sed -n 's|.*github\.com/[^/]*/\([^/]*\).*|\1|p')
SUBPATH=$(echo "$SOURCE_URL" | sed -n 's|.*github\.com/[^/]*/[^/]*/tree/[^/]*/\(.*\)|\1|p')

git clone --depth 1 --branch "$SOURCE_BRANCH" "https://github.com/$OWNER/$REPO.git" "$TMP_UPDATE" 2>/dev/null || {
    echo "{\"error\": \"更新失败: 无法克隆仓库\"}" >&2
    rm -rf "$TMP_UPDATE"
    exit 4
}

if [[ -n "$SUBPATH" ]]; then
    mv "$TMP_UPDATE/$SUBPATH" "$TMP_UPDATE-skill"
    rm -rf "$TMP_UPDATE"
    mv "$TMP_UPDATE-skill" "$TMP_UPDATE"
fi

# ---- 更新前预检：与 install.sh / 面板那条路**共用同一个模块** ----
#
# 这条路径以前**完全绕过** L1–L5：clone 完直接 `rm -rf "$TARGET_DIR"` 再 `mv` 进去。
# 而 SKILL.md 把 `bash scripts/update.sh <name>` 记为**官方更新入口** ——
# 于是「一个从没跑过预检的 skill，从没有预检的路径重装进来」是条可行链路。
# 更新路径甚至比安装更该拦：它要**覆盖**已经装好的东西。
#
# 判定与 install.sh / daemon/precheck.py 一致：**只拒 REJECT**，REVIEW 不拦
# （装上但标 partial）。别把 REVIEW 读成"被拦下了"。
#
# ⚠️ 必须在 `rm -rf "$TARGET_DIR"` **之前** —— 拒了就必须保证现有那份原封不动。
PRECHECK_JSON=$(cd "$SKILL_FORGE" && python3 -m daemon.precheck \
    "$TMP_UPDATE" "https://github.com/$OWNER/$REPO.git" 2>/dev/null) || PRECHECK_JSON=""

PRECHECK_OK=$(printf '%s' "$PRECHECK_JSON" | python3 -c "
import json, sys
try:
    print('yes' if json.load(sys.stdin).get('ok') else 'no')
except Exception:
    print('no')
" 2>/dev/null || echo "no")

if [[ "$PRECHECK_OK" != "yes" ]]; then
    REASON=$(printf '%s' "$PRECHECK_JSON" | python3 -c "
import json, sys
try:
    print(json.load(sys.stdin).get('summary', '预检未给出结论'))
except Exception:
    print('预检无法执行（daemon/precheck.py 跑不起来？）')
" 2>/dev/null || echo "预检无法执行")
    rm -rf "$TMP_UPDATE"
    echo "{\"error\": \"预检未通过，已拒绝更新：$REASON\"}" >&2
    exit 5
fi

# 原子替换（只在预检通过之后才落地）
rm -rf "$TARGET_DIR"
mv "$TMP_UPDATE" "$TARGET_DIR"

# 更新 SHA
NEW_SHA=$(cd "$TARGET_DIR" && git rev-parse HEAD 2>/dev/null || echo "unknown")
python3 -c "
import json
sf = open('$SOURCES_FILE'); data = json.load(sf); sf.close()
data['$SKILL_NAME']['installed_sha'] = '$NEW_SHA'
data['$SKILL_NAME']['updated_at'] = '$(date -u +%Y-%m-%dT%H:%M:%SZ)'
open('$SOURCES_FILE','w').write(json.dumps(data, ensure_ascii=False, indent=2))
"

echo "{\"ok\": true, \"name\": \"$SKILL_NAME\", \"old_sha\": \"$OLD_SHA\", \"new_sha\": \"$NEW_SHA\", \"backup\": \"$BACKUP_PATH\"}"
