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

# 原子替换
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
