#!/bin/bash
# 卸载 skill（先备份再删除）
# 用法: uninstall.sh <skill_name> [--no-backup]
set -euo pipefail

SKILLS_ROOT="${SKILLS_ROOT:-$HOME/.claude/skills}"
SKILL_FORGE="$SKILLS_ROOT/skill-forge"
SOURCES_FILE="$SKILL_FORGE/sources.json"
BACKUP_DIR="$SKILL_FORGE/.backup"

SKILL_NAME="${1:-}"
NO_BACKUP=false
[[ "${2:-}" == "--no-backup" ]] && NO_BACKUP=true

if [[ -z "$SKILL_NAME" ]]; then
    echo '{"error": "Usage: uninstall.sh <skill_name> [--no-backup]"}' >&2
    exit 2
fi

TARGET_DIR="$SKILLS_ROOT/$SKILL_NAME"

if [[ ! -d "$TARGET_DIR" ]]; then
    echo "{\"error\": \"skill '$SKILL_NAME' 未安装\"}" >&2
    exit 2
fi

# 创建备份
if [[ "$NO_BACKUP" != true ]]; then
    TIMESTAMP=$(date +%Y%m%d-%H%M%S)
    BACKUP_PATH="$BACKUP_DIR/$SKILL_NAME-$TIMESTAMP-$(uuidgen 2>/dev/null | cut -c1-8 || echo $RANDOM)"
    mkdir -p "$BACKUP_PATH"
    cp -r "$TARGET_DIR" "$BACKUP_PATH/"
    echo "{\"phase\": \"backup\", \"path\": \"$BACKUP_PATH\"}"
fi

# 删除
rm -rf "$TARGET_DIR"

# 更新 sources.json
python3 -c "
import json
data = json.loads(open('$SOURCES_FILE').read())
if '$SKILL_NAME' in data:
    removed = data.pop('$SKILL_NAME')
    open('$SOURCES_FILE','w').write(json.dumps(data, ensure_ascii=False, indent=2))
    print(json.dumps({'ok': True, 'name': '$SKILL_NAME', 'removed_source': removed.get('url', '')}, ensure_ascii=False))
else:
    print(json.dumps({'ok': True, 'name': '$SKILL_NAME', 'note': 'no sources.json entry'}, ensure_ascii=False))
"
