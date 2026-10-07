#!/bin/bash
# Skill Forge Gate 自我更新
#
# 流程：留档（带用途标注）→ 拉取 → 替换 → 自检 → 成功 / 回滚
#
# ## 这个脚本的危险之处，以及现在怎么处理的
#
# 它**会删除** skill-forge 目录下除 `.backup` / `.tmp` 外的所有条目，再用新版本
# 填回来。而这个目录里既有代码，也有**用户数据** —— 数据是新版本不会带的，
# 删掉就没了。
#
# 2026-10-05 之前的版本正是这么丢的：只把 `sources.json` 抢救了回来，
# 于是 `discover/`（全部周度存档 + 面板）、`templates/install-queue.json`、
# `templates/.bridge-key` 等等在每次自更新后**静默消失**。
#
# 现在做三件事：
#   1. 替换**之前**先把每一项数据的**用途**写进留档（见 DATA_PATHS 与 MANIFEST.md）
#   2. 替换时把数据**搬出去再搬回来**，而不是删掉后只恢复一个文件
#   3. 每份留档都带一个 `ROLLBACK.sh`，回滚是一条命令的事
#
# 用法：
#   self-update.sh                    执行自我更新
#   self-update.sh --list             列出可用的留档
#   self-update.sh --rollback         回滚到最近一次自更新之前
#   self-update.sh --rollback <目录>  回滚到指定的留档
set -euo pipefail

export https_proxy="http://127.0.0.1:7897"
export http_proxy="http://127.0.0.1:7897"

SKILL_FORGE="${SKILL_FORGE_DIR:-$HOME/.claude/skills/skill-forge}"
BACKUP_DIR="$SKILL_FORGE/.backup"
SOURCES_FILE="$SKILL_FORGE/sources.json"

# python3 在 Windows 上常常是 Microsoft Store 的转发器，不一定真能跑；
# 真要跑不起来就退回 python。两个都没有就不用往下走了。
PYTHON=$(command -v python3 || command -v python || true)
[[ -n "$PYTHON" ]] || { echo '{"error": "找不到 python3 或 python"}' >&2; exit 3; }

# 替换时要跳过的顶层条目（留档目录和暂存区不能被自己删掉）
KEEP_TOP='^\.backup$|^\.tmp$'

# 保留几份自更新留档。超出就删最旧的 —— 会打印出来，不静默。
KEEP_BACKUPS="${SKILL_FORGE_KEEP_BACKUPS:-5}"

# ---------------------------------------------------------------- 工具

json_out() { printf '%s\n' "$1"; }

die() {
    json_out "{\"error\": \"$1\"}" >&2
    exit "${2:-1}"
}

abs_path() {  # 尽量给出绝对路径，方便写进回滚脚本
    ( cd "$1" 2>/dev/null && pwd ) || printf '%s' "$1"
}

# 生成留档里的说明文件。**在删除任何东西之前**调用。
write_manifest() {
    local dir="$1" stamp="$2" old_sha="$3"
    local manifest="$dir/MANIFEST.md"
    local json="$dir/manifest.json"

    {
        echo "# Skill Forge Gate 自更新留档"
        echo
        echo "- 留档时间：$stamp"
        echo "- 更新前版本：\`${old_sha:-未知}\`"
        echo "- 快照位置：\`$dir/snapshot/\`（更新**之前**的完整目录）"
        echo
        echo "## 这份留档是干什么的"
        echo
        echo "自更新会删掉 \`$SKILL_FORGE\` 下除 \`.backup\` / \`.tmp\` 外的所有条目，"
        echo "再用新版本填回来。**代码删掉没关系，数据删掉就没了。**"
        echo "所以替换之前先把下面这些项搬出去，替换完再搬回来；万一失败，"
        echo "整份快照原样拷回去即可。"
        echo
        echo "## 数据项（必须保住，逐项标注用途）"
        echo
        echo "| 相对路径 | 用途 | 现状 |"
        echo "|---|---|---|"
        for entry in "${DATA_PATHS[@]}"; do
            # 清单里目录写成 `discover/` 也行，这里把尾斜杠归一化掉
            local rel="${entry%%|*}"; rel="${rel%/}"
            local why="${entry#*|}"
            local state="不存在"
            [[ -e "$SKILL_FORGE/$rel" ]] && state="已备份"
            echo "| \`$rel\` | $why | $state |"
        done
        echo
        echo "## 不在清单里的（代码）"
        echo
        echo "$CODE_HINT"
        echo
        echo "## 怎么回滚"
        echo
        echo '```bash'
        echo "bash \"$dir/ROLLBACK.sh\""
        echo '```'
        echo
        echo "或者用主脚本：\`scripts/self-update.sh --rollback \"$dir\"\`"
        echo
        echo "回滚会把 \`snapshot/\` 整个拷回去（代码 + 数据一起），"
        echo "所以它恢复到的是**更新前那一刻的完整状态**。"
    } > "$manifest"

    # 机器可读的一份，方便别的工具消费
    {
        echo "{"
        echo "  \"kind\": \"skill-forge-self-update\","
        echo "  \"created\": \"$stamp\","
        echo "  \"previous_sha\": \"$(json_escape "$old_sha")\","
        echo "  \"skill_forge\": \"$(json_escape "$(abs_path "$SKILL_FORGE")")\","
        echo "  \"snapshot\": \"snapshot\","
        echo "  \"data_paths\": ["
        local first=1
        for entry in "${DATA_PATHS[@]}"; do
            local rel="${entry%%|*}"; rel="${rel%/}"
            local why="${entry#*|}"
            [[ $first -eq 0 ]] && echo ","
            first=0
            printf '    {"path": "%s", "purpose": "%s", "present": %s}' \
                "$(json_escape "$rel")" "$(json_escape "$why")" \
                "$([[ -e "$SKILL_FORGE/$rel" ]] && echo true || echo false)"
        done
        echo
        echo "  ]"
        echo "}"
    } > "$json"
}

json_escape() {  # 够用的转义：反斜杠、双引号，并删掉所有控制字符
    # `tr -d '\n\r'` 里的 **\r 不是可有可无的**：\r 在 JSON 里是非法控制字符，
    # 漏删就会生成一个 json.loads 打不开的 manifest.json。
    # 什么时候会漏进来：文件被检出成 CRLF 时（Git for Windows 默认行为），
    # 下面 `while read` 读到的每一行行尾都挂着一个 \r。
    # .gitattributes 已经把行尾钉死成 LF，但那份防护管不到
    # "用户用记事本改过 data-paths.txt" 这种情况，所以这里再兜一层。
    # \t 一并换成空格，理由是同一个。
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' \
        | tr -d '\n\r' | tr '\t' ' '
}

# 把 snapshot 拷回 SKILL_FORGE（回滚用）
restore_snapshot() {
    local snap="$1"
    [[ -d "$snap" ]] || die "快照目录不存在：$snap" 6
    local f
    for f in $(ls -A "$SKILL_FORGE" | grep -Ev "$KEEP_TOP"); do
        rm -rf "$SKILL_FORGE/$f"
    done
    cp -r "$snap"/* "$SKILL_FORGE/" 2>/dev/null || true
    cp -r "$snap"/.[!.]* "$SKILL_FORGE/" 2>/dev/null || true
}

# ---------------------------------------------------------------- 数据清单
#
# 「数据」= 新版本**不会**带的东西：用户自己的状态。替换时必须原样保住。
#
# 清单本体在 scripts/data-paths.txt —— 那里面逐项写了用途，也是"删除之前标注用途"
# 的原始依据。这里只是把它读进来。
#
# 为什么抽成文件：这份清单现在被 self-update.sh 和 deploy.py 两个脚本共用。
# 以前"哪些算数据"散在流程里，改一处漏一处 —— 自更新把 discover/ 和
# .bridge-key 静默删掉，就是这么来的。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_PATHS_FILE="$SCRIPT_DIR/data-paths.txt"
[[ -f "$DATA_PATHS_FILE" ]] || DATA_PATHS_FILE="$SKILL_FORGE/scripts/data-paths.txt"
[[ -f "$DATA_PATHS_FILE" ]] || die "找不到数据清单 scripts/data-paths.txt，不敢动任何东西" 3

DATA_PATHS=()
while IFS= read -r line; do
    # 先砍行尾的 \r。这一步**必须**在空行判断之前 ——
    # CRLF 文件里的空行读出来是 "\r" 而不是 ""，`${line// }` 只吃空格、
    # 不吃 \r，于是空行判不成空行，变成一条"数据项"。它的路径和用途都是
    # 那个裸 \r，最后写出一个 `"path": "\r"` 的非法 manifest.json。
    # （2026-10-07 实测：test_self_update 就是这么炸的。）
    line="${line%$'\r'}"
    [[ -z "${line// }" || "$line" == \#* ]] && continue
    # 清单格式只有一种：`相对路径|用途`。没有 `|` 的行不是数据项 ——
    # 这里宁可停下来报错，也不要把半行东西当路径拿去 cp/rm。
    [[ "$line" == *"|"* ]] || die "数据清单这行格式不对（缺少 '|'）：$line" 8
    DATA_PATHS+=("$line")
done < "$DATA_PATHS_FILE"
[[ ${#DATA_PATHS[@]} -gt 0 ]] || die "数据清单是空的，不敢动任何东西" 3

# 下面这些是**代码**（新版本会带），列出来只是为了说清楚为什么它们不在上面的清单里 ——
# 留档的 MANIFEST.md 会把这段话写进去，免得下次有人以为漏了。
CODE_HINT="verify/ daemon/*.py panel/fallback.html SKILL.md README.md 等都由仓库提供，\
新版本会带过来，所以**不**在数据清单里。它们被删掉是正常的，也是这次更新的目的。"

# ---------------------------------------------------------------- 参数

MODE="update"
ROLLBACK_TARGET=""
case "${1:-}" in
    --rollback)
        MODE="rollback"
        ROLLBACK_TARGET="${2:-}"
        ;;
    --list)
        MODE="list"
        ;;
    -h|--help)
        sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'
        exit 0
        ;;
esac

if [[ "$MODE" == "list" ]]; then
    echo "可用的自更新留档（$BACKUP_DIR）："
    found=0
    for d in $(ls -dt "$BACKUP_DIR"/self-update-* 2>/dev/null || true); do
        [[ -d "$d/snapshot" ]] || continue
        found=1
        printf '  %s\n' "$d"
    done
    [[ $found -eq 1 ]] || echo "  （还没有）"
    exit 0
fi

if [[ "$MODE" == "rollback" ]]; then
    if [[ -z "$ROLLBACK_TARGET" ]]; then
        ROLLBACK_TARGET=$(ls -dt "$BACKUP_DIR"/self-update-* 2>/dev/null | head -1 || true)
        [[ -n "$ROLLBACK_TARGET" ]] || die "没有可回滚的留档（$BACKUP_DIR 下没有 self-update-*）" 6
    fi
    # 允许直接给留档目录，也允许给它的 snapshot 子目录
    SNAP="$ROLLBACK_TARGET"
    [[ -d "$ROLLBACK_TARGET/snapshot" ]] && SNAP="$ROLLBACK_TARGET/snapshot"
    json_out "{\"phase\": \"rollback\", \"from\": \"$(json_escape "$SNAP")\"}"
    restore_snapshot "$SNAP"
    json_out "{\"ok\": true, \"phase\": \"rolled-back\", \"snapshot\": \"$(json_escape "$SNAP")\"}"
    echo "已回滚到：$SNAP" >&2
    exit 0
fi

# ---------------------------------------------------------------- 自我更新

# 读取自身来源信息
#
# 路径一律走 argv 传进去，**绝不**拼进 Python 源码：
# Windows 路径里有反斜杠，`'C:\Users\...'` 里的 `\U` 会被 Python 当成
# Unicode 转义，直接 SyntaxError —— 而且报错信息看起来像"python 坏了"。
# 2026-10-05 之前这里就是拼字符串的，脚本在本机跑不起来。
SOURCE_INFO=$("$PYTHON" - "$SOURCES_FILE" <<'PYEOF'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    data = json.load(f)
self_info = next((v for v in data.values() if isinstance(v, dict) and v.get("self")), None)
print(json.dumps(self_info) if self_info else "{}")
PYEOF
)

if [[ "$SOURCE_INFO" == "{}" ]]; then
    die "sources.json 中未找到 self:true 条目，无法自我更新" 2
fi

SOURCE_URL=$(echo "$SOURCE_INFO" | "$PYTHON" -c "import json,sys; print(json.loads(sys.stdin.read()).get('url',''))")
SOURCE_BRANCH=$(echo "$SOURCE_INFO" | "$PYTHON" -c "import json,sys; print(json.loads(sys.stdin.read()).get('branch','main'))")
OLD_SHA=$(echo "$SOURCE_INFO" | "$PYTHON" -c "import json,sys; print(json.loads(sys.stdin.read()).get('installed_sha',''))")

OWNER=$(echo "$SOURCE_URL" | sed -n 's|.*github\.com/\([^/]*\)/.*|\1|p')
REPO=$(echo "$SOURCE_URL" | sed -n 's|.*github\.com/[^/]*/\([^/]*\).*|\1|p')

json_out "{\"phase\": \"pre-update\", \"self\": true, \"old_sha\": \"$OLD_SHA\"}"

# ---- Step 1: 留档（**先标注用途，再动任何东西**）----
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_PATH="$BACKUP_DIR/self-update-$TIMESTAMP-$RANDOM"
mkdir -p "$BACKUP_PATH/snapshot"
(
    cd "$SKILL_FORGE"
    for f in $(ls -A | grep -Ev "$KEEP_TOP"); do
        cp -r "$f" "$BACKUP_PATH/snapshot/" 2>/dev/null || true
    done
)
write_manifest "$BACKUP_PATH" "$TIMESTAMP" "$OLD_SHA"
json_out "{\"phase\": \"backup\", \"path\": \"$BACKUP_PATH\", \"manifest\": \"$BACKUP_PATH/MANIFEST.md\"}"

# 回滚脚本：一条命令恢复到更新前
cat > "$BACKUP_PATH/ROLLBACK.sh" <<ROLLBACK_EOF
#!/bin/bash
# 一键回滚 Skill Forge Gate 到 $TIMESTAMP 那次自更新之前的状态。
# 由 scripts/self-update.sh 生成。
set -euo pipefail
SKILL_FORGE="\${SKILL_FORGE_DIR:-$(abs_path "$SKILL_FORGE")}"
SNAP="\$(cd "\$(dirname "\${BASH_SOURCE[0]}")" && pwd)/snapshot"

echo "把 \$SNAP 恢复到 \$SKILL_FORGE ..."
for f in \$(ls -A "\$SKILL_FORGE" | grep -Ev '^\.backup\$|^\.tmp\$'); do
    rm -rf "\$SKILL_FORGE/\$f"
done
cp -r "\$SNAP"/* "\$SKILL_FORGE/" 2>/dev/null || true
cp -r "\$SNAP"/.[!.]* "\$SKILL_FORGE/" 2>/dev/null || true
echo "完成。"
ROLLBACK_EOF
chmod +x "$BACKUP_PATH/ROLLBACK.sh" 2>/dev/null || true

# ---- Step 2: 拉取新版本 ----
# 暂存区要在 .tmp 下面，而 .tmp 不一定存在（全新安装时就没有）——
# 不建的话 clone/拷贝会失败，而失败会表现成"替换完之后 verify/ 不见了"，
# 排查起来要绕一大圈。这里先建出来。
mkdir -p "$SKILL_FORGE/.tmp"
TMP_NEW="$SKILL_FORGE/.tmp/self-update-$$"
rm -rf "$TMP_NEW"
git clone --depth 1 --branch "$SOURCE_BRANCH" "https://github.com/$OWNER/$REPO.git" "$TMP_NEW" 2>/dev/null || {
    rm -rf "$TMP_NEW"
    die "自我更新失败: 无法从 GitHub 拉取。留档仍在：$BACKUP_PATH" 4
}

# ---- Step 3: 替换（数据搬出去 → 清代码 → 新版本进来 → 数据搬回来）----
#
# 顺序很重要：**先搬出数据，再删**。反过来就是这次要修的那个 bug。
STAGE="$SKILL_FORGE/.tmp/self-update-data-$$"
rm -rf "$STAGE"
mkdir -p "$STAGE"

moved=0
for entry in "${DATA_PATHS[@]}"; do
    rel="${entry%%|*}"; rel="${rel%/}"
    if [[ -e "$SKILL_FORGE/$rel" ]]; then
        mkdir -p "$STAGE/$(dirname "$rel")"
        mv "$SKILL_FORGE/$rel" "$STAGE/$rel"
        moved=$((moved + 1))
    fi
done
json_out "{\"phase\": \"data-stashed\", \"count\": $moved}"

# 现在可以安全地清空代码了（数据已经不在里面）
for f in $(ls -A "$SKILL_FORGE" | grep -Ev "$KEEP_TOP"); do
    rm -rf "$SKILL_FORGE/$f"
done

cp -r "$TMP_NEW"/* "$SKILL_FORGE/" 2>/dev/null || true
cp -r "$TMP_NEW"/.[!.]* "$SKILL_FORGE/" 2>/dev/null || true
rm -rf "$TMP_NEW"

# 拷完立刻验一次：代码真的进来了吗？
# 此刻目录已经清空过，所以"新代码没拷进来"= 一棵空树。
# 不等自检是因为自检依赖的 verify/ 本身也在新代码里 —— 空树会让自检以
# 一个含糊的理由失败，而真正的原因（拷贝没成功）反而被盖住了。
if [[ ! -e "$SKILL_FORGE/SKILL.md" ]]; then
    restore_snapshot "$BACKUP_PATH/snapshot"
    die "替换失败：新版本没有拷进 $SKILL_FORGE（暂存区 $TMP_NEW 是空的？）。已用留档回滚。" 7
fi

# 数据搬回来。新版本里如果有同名文件/目录，以**数据**为准 —— 数据是不可再生的。
restored=0
for entry in "${DATA_PATHS[@]}"; do
    rel="${entry%%|*}"; rel="${rel%/}"
    if [[ -e "$STAGE/$rel" ]]; then
        rm -rf "$SKILL_FORGE/$rel"
        mkdir -p "$SKILL_FORGE/$(dirname "$rel")"
        mv "$STAGE/$rel" "$SKILL_FORGE/$rel"
        restored=$((restored + 1))
    fi
done
rm -rf "$STAGE"
json_out "{\"phase\": \"replaced\", \"data_restored\": $restored}"
[[ "$restored" -eq "$moved" ]] || echo "警告：搬出 $moved 项、搬回 $restored 项，数量对不上，请检查 $BACKUP_PATH" >&2

# ---- Step 4: 自检 ----
cd "$SKILL_FORGE"
if "$PYTHON" verify/l1_structure.py "$SKILL_FORGE" > /dev/null 2>&1; then
    json_out "{\"phase\": \"self-check\", \"result\": \"pass\"}"

    NEW_SHA=$(git rev-parse HEAD 2>/dev/null || echo "unknown")
    "$PYTHON" - "$SOURCES_FILE" "$NEW_SHA" <<'PYEOF'
import datetime, json, sys

path, sha = sys.argv[1], sys.argv[2]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
for v in data.values():
    if isinstance(v, dict) and v.get("self"):
        v["installed_sha"] = sha
        v["updated_at"] = datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        break
with open(path, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
PYEOF

    # 清理旧留档（保留最近 N 份）。删之前把要删的打出来 —— 静默删备份很危险。
    OLD_BACKUPS=$(ls -dt "$BACKUP_DIR"/self-update-* 2>/dev/null | tail -n +$((KEEP_BACKUPS + 1)) || true)
    if [[ -n "$OLD_BACKUPS" ]]; then
        for d in $OLD_BACKUPS; do
            [[ -d "$d" ]] || continue
            echo "清理旧留档：$d" >&2
            rm -rf "$d"
        done
    fi

    json_out "{\"ok\": true, \"phase\": \"complete\", \"old_sha\": \"$OLD_SHA\", \"new_sha\": \"$NEW_SHA\", \"backup\": \"$BACKUP_PATH\"}"
else
    json_out "{\"phase\": \"self-check\", \"result\": \"FAILED — 正在回滚...\"}" >&2

    # ---- Step 5: 自检失败 → 用留档整体回滚 ----
    restore_snapshot "$BACKUP_PATH/snapshot"
    die "自我更新失败，已回滚到留档：$BACKUP_PATH" 5
fi
