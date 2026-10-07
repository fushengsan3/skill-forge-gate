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

# ⚠️ 名字校验**必须在拼路径之前** —— 顺序反了就等于没验。
# 这个脚本此前对 SKILL_NAME 零校验，于是：
#     uninstall.sh ..            → 删掉 .claude/（$SKILLS_ROOT 的父目录）
#     uninstall.sh .             → 删掉**全部** skills
#     uninstall.sh skill-forge   → 删掉管理器自己（备份目录还在它里面，一起没）
#
# 下面这几条与 `daemon/safe_paths.py::check_name` 是**同一套规则**。
# 两份规则必须同时改 —— 这里放宽了而那边没收紧，等于开了个后门。
# （2026-10-06 冻结树复核的完整性批评者发现：这条路径从未被任何清单记录过。）
if [[ "$SKILL_NAME" == "." || "$SKILL_NAME" == ".." ]]; then
    echo "{\"error\": \"拒绝：名字不能是 '$SKILL_NAME' —— 它会指到别的目录去\"}" >&2
    exit 2
fi
if [[ "$SKILL_NAME" == */* || "$SKILL_NAME" == *\\* || "$SKILL_NAME" == *[\<\>\:\"\|\?\*]* ]]; then
    echo "{\"error\": \"拒绝：名字里有路径分隔符或 Windows 保留字符\"}" >&2
    exit 2
fi

# `skill-forge` 是**合法**的名字，上面那条 case 拦不住它 —— 但它指向的是我们自己。
# 删它 = 把管理器连同它自己的脚本、日志、备份一起删掉。
if [[ "$SKILL_NAME" == "skill-forge" ]]; then
    echo "{\"error\": \"拒绝卸载 skill-forge 本体（要卸请手工处理）\"}" >&2
    exit 2
fi

TARGET_DIR="$SKILLS_ROOT/$SKILL_NAME"

# 拼完之后再确认一次结果确实落在 SKILLS_ROOT **里面**且不是它本身。
# 这不是重复：上面靠的是字符串规则，这里靠的是路径的最终形态。
REAL_ROOT=$(cd "$SKILLS_ROOT" 2>/dev/null && pwd -P) || REAL_ROOT=""
REAL_TARGET=$(cd "$TARGET_DIR" 2>/dev/null && pwd -P) || REAL_TARGET=""
if [[ -z "$REAL_ROOT" || -z "$REAL_TARGET" || "$REAL_TARGET" == "$REAL_ROOT" ]]; then
    echo "{\"error\": \"拒绝：'$SKILL_NAME' 解析后不在 skills 目录里面\"}" >&2
    exit 2
fi

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
