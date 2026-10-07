#!/usr/bin/env python3
"""
L3 内容安全扫描 — 检测 SKILL.md 及附带脚本中的危险模式
输出 JSON 格式的扫描报告到 stdout

## 2026-10-05：三个方向的矫正

这一层原先有三处"值算对了、结论在说谎"，都是同一个毛病 ——
**在没看过的东西上宣称结论**：

1. **读不出的文件静默消失。** `except Exception: return {}`，然后 `scan_skill`
   只收有命中的结果 —— 读失败的文件既不在 findings 里，也不在任何计数里，
   而 `scanned_files` 还照样把它算作"已扫描"。现在：读失败 = `REVIEW`（fail-closed）。
2. **只扫 8 种扩展名，却报"未发现危险或可疑模式"。** 白名单的默认方向是
   "我不认识就不看"，盲区正好落在没人会想到的地方（`Makefile`、`Dockerfile`、
   `package.json` 的 scripts、无扩展名脚本）。现在：**默认扫，除非确认是二进制**。
3. **文档里的命中和脚本里的命中同样计分。** README 里的 `curl | bash` 多半是
   示例，`postinstall` 里的同一个则不是一回事。现在按 `source_kind` 分开：
   文档/其它文本里的红 → 降到 `REVIEW`；可执行文件里的红 → `REJECT`。

**没改的（要说清楚）**：这些仍然是**正则匹配**，不是语义分析。
一份把命令拼起来（`"cu"+"rl"`）或从远端取来再执行的 skill 仍能绕过。
它的定位是"便宜的第一道筛子"，不是"证明干净"。
"""
import os
import re
import json
import sys
from pathlib import Path

# 策略文件是"敏感路径"的**唯一一份**清单（见 sandbox/audit-policy.yaml）。
# 硬编码规则里那几条（~/.aws/ 等）是红组，不动；策略补的是它们的盲区。
from verify import audit_policy

# ============================================================
# 规则组 A：高危模式 → 标红，建议拒绝安装
# ============================================================
# ⚠️ 2026-10-07 按**实测误报率**重排过一次（本机 80 个真实在用的 skill 当语料）。
# 判据是：**这条规则命中的是「在做」，还是「提到了」？**
#
# 红组的准入标准因此收紧成两条，缺一不可：
#   1. **没有正当用途** —— 正常的 skill 不会包含它；
#   2. **后果不可逆** —— 真发生了就不是"再看一眼"能补救的。
# 反例（原先在红组，已移到黄组）：
#   · `\bsudo\b` —— 命中 150 次，绝大多数是注释和文档里的"以 root/sudo 运行"
#   · `\beval\b` —— 命中 206 次，绝大多数是 **JS/Python 自己源码里的 `eval(`**
#   · `\bexec\s*\(` —— 命中 197 次，同上（`cp.exec(...)`）
#   · `/etc/passwd` 等 —— 命中 68 次，包括安全文档里"防止路径穿越（如 ../../etc/passwd）"
#   · `chmod 777` —— 命中 1 次，但那是文档里的反面教材
# 这些是**值得一提**，不是**据此拒绝**。
DANGER_PATTERNS = {
    # `rm -rf /` 与 `rm -rf ~`（裸根/家目录）—— 无正当用途
    # 原先还匹配 `/(dev|etc|usr|var|home|sys|proc)`，于是
    # `rm -rf /var/lib/apt/lists/*`（Dockerfile 里的标准写法）被判 REJECT。
    # 现在要求那个顶层目录**就是命令的终点**（后面不能再跟更深的路由）。
    # 结尾允许空白、行尾、或**引号** —— 最后一项是必须的：
    # `bash -c "rm -rf /"` 里那个 `/` 后面就是引号，只认空白的话整条规则不触发，
    # 于是一条**真的在执行的**破坏性命令反而没人管（实测：改之前它是 PASS）。
    "毁灭性删除": (r"rm\s+-rf\s+/(\s|$|[\"'\`])"      # rm -rf / 或 rm -rf / tmp ← 删的是**根**
                   r"|rm\s+-rf\s+~/?(\s|$|[\"'\`])"   # rm -rf ~
                   # 顶层目录本身、且到命令结尾：`rm -rf /var` 是灾难，
                   # `rm -rf /var/lib/apt/lists/*` 是 Dockerfile 的标准写法。
                   r"|rm\s+-rf\s+/(dev|etc|usr|var|home|sys|proc)/?\s*[\*;|&]*\s*$"),
    # `pipe-to-shell` **不在红组** —— 见下面 SUSPICIOUS_PATTERNS 里的说明：
    # 它是安装说明的常见写法（uv / rustup / bun 全是这么装的）。
    "写裸设备": r">\s*/dev/sd[a-z]|\bdd\s+if=.*of=/dev/",
    "fork炸弹": r":\(\)\s*\{\s*:\|:&\s*\}\s*;:",
    "格式化磁盘": r"\bmkfs\.\b|\bformat\b\s+[A-Z]:",
    "注入SSH后门": r"~?\.ssh/authorized_keys",
    "窃取云凭证": r"~?\.aws/credentials|~?\.gcloud/|~?\.azure/|~?\.config/gcloud",
    "监听端口后门": r"\bnc\s+-l\b|\bncat\s+-l\b|\bnetcat\s+-l\b",
}

# ============================================================
# 规则组 B：可疑模式 → 标黄，需人工审核
# ============================================================
SUSPICIOUS_PATTERNS = {
    # 从红组降下来的（dual-use：值得看一眼，但不该据此拒绝）
    "sudo/doas提权": r"\bsudo\s+\S|\bdoas\s+\S",
    "动态代码执行": r"\beval\s*[\(\"'$]|\bexec\s*\(|\b__import__\s*\(|\bcompile\s*\(.*exec",
    "系统账户文件": r"/etc/passwd|/etc/shadow|/etc/sudoers",
    "开放全部权限": r"chmod\s+777",
    # 原有的
    # ⚠️ 从红组降下来的。`curl ... | sh` 看着就吓人，但它是**安装说明的标准写法**
    # —— uv / rustup / bun / 这个项目自己的 README 都这么写。实测命中的那条是
    # `curl -LsSf https://astral.sh/uv/install.sh | sh`（uv 的官方安装命令）。
    # 在一个"给用户看的安装步骤"里出现它，和在一个脚本里**执行**它是两件事。
    "pipe-to-shell": r"(curl|wget)\s+[^\n|]*\|\s*(sudo\s+)?(bash|sh|zsh)\b",
    "网络请求": r"\bcurl\b|\bwget\b",
    "环境变量读取": r"\$(SECRET|TOKEN|KEY|PASSWORD|PASSWD|CREDENTIAL)",
    "强制推送": r"git\s+push\s+--force|git\s+push\s+-f",
    "发布上传": r"\bnpm\s+publish\b|\bpip\s+upload\b|\bdocker\s+push\b|\bcargo\s+publish\b",
    # ⚠️ 原先的 `~?\.(bashrc|zshrc|profile|bash_profile)` 里的 `profile` 是裸词，
    # 于是 `tokens.profile` / `getOauthProfile` 这类**普通标识符**全中（实测 42 次）。
    # 现在要求它是个真的点文件。
    "持久化注入": r"~/\.(bashrc|zshrc|bash_profile|profile)\b|/etc/(bash\.bashrc|profile\.d/)",
    "修改Claude Code配置": r"~?\.claude/settings\.json",
    "DNS劫持": r"/etc/hosts",
    "读取SSH私钥": r"~?\.ssh/id_",
    "删除git仓库": r"rm\s+-rf\s+\S*\.git\b",
    # ⚠️ 裸词 `Startup` 实测命中 **799 次**，几乎全是普通英文（"the startup JSON"）
    # 和标识符。现在要求它出现在**路径或命令**的形态里。
    "修改启动项": (r"shell:startup|/Library/LaunchAgents"
                   r"|systemd\s+\S*\s*(enable|start)"
                   r"|schtasks\s+/create|reg\s+add\s+\S*\\Run\b"),
    # `\bencrypt\b` 是个普通动词（实测命中 10 次，多在"encrypted storage"这类描述里）
    "加密文件操作": r"\bgpg\s+--|\bopenssl\s+\S*enc\b|\bopenssl\s+enc\b",
}

# ============================================================
# 规则组 C：信息收集 → 标蓝，仅记录
# ============================================================
INFO_PATTERNS = {
    "Bash调用": r"```bash\n(.+?)```|`([^`]+)`",
    "文件写入路径": r"(?:write|save|output|create)\s+(?:to\s+)?([~\w./\\-]+)",
    "网络域名": r"https?://([\w.-]+)",
    "Hook声明": r"(?:PreToolUse|PostToolUse|PreMessage|PostMessage|SessionStart|SessionEnd)",
    "MCP Server": r"mcp.*server|mcp__\w+",
}

# ---- 扫描范围：默认扫，除非**字节**说明它是二进制 ----
#
# 这里曾经有一份 `BINARY_EXTS` 扩展名清单，当作"快速通道"先于字节判定短路。
# 它已于 2026-10-06 删除 —— 因为那等于**让扩展名决定要不要看**，
# 而"让扩展名决定要不要看"正是这次重写要根除的那个机制
# （旧版是「扩展名在 8 种白名单里才看」，新版只是把白名单改小了，问题没变）。
#
# 当时的注释还写着「扩展名可以撒谎，字节不会」—— 代码却在做相反的事：
# 同一份 curl|bash 叫 `.png` 判 PASS、叫 `.sh` 判 REJECT。
# 冻结树复核 (#14) 用这个改名反例当场抓到。
#
# 现在的判定**只看 `_looks_binary()`**（开头 8 KB 里有没有 NUL）。
# 代价是每个候选文件都要开一次读 8 KB —— 但扫描本来就要读它们，
# 而超大文件已经先被 MAX_FILE_BYTES 挡掉了，所以这不构成额外负担。

# 能被当脚本跑的东西 —— 红组命中在这里 → REJECT
SCRIPT_EXTS = {
    ".sh", ".bash", ".zsh", ".fish", ".ksh", ".csh",
    ".py", ".pyw", ".rb", ".pl", ".php", ".lua", ".r", ".jl",
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".ps1", ".psm1", ".bat", ".cmd", ".vbs", ".wsf",
    ".go", ".rs", ".c", ".h", ".cc", ".cpp", ".hpp", ".java", ".kt", ".swift",
    ".mk", ".make", ".dockerfile", ".tf",
}

# 这些文件名没有扩展名，但内容是脚本/构建配置
EXEC_NAMES = {
    "makefile", "gnumakefile", "dockerfile", "containerfile", "rakefile",
    "justfile", "procfile", "vagrantfile", "gemfile", "brewfile",
    # package.json 单独列出来：它的 scripts 段会被 npm 直接执行，
    # 一份 `"postinstall": "curl … | bash"` 和脚本里的同一行没有区别。
    "package.json",
}

# 目录直接不进去（连走都不走，不是走完再过滤）
SKIP_DIRS = {
    ".git", ".hg", ".svn", ".backup", "__pycache__", "node_modules",
    ".venv", "venv", "env", "dist", "build", ".next", ".nuxt",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".eggs",
    "site-packages", ".terraform", "target", "vendor",
}

# 超过这个大小的文本文件不扫。**会如实计入报告**，不是静默截断。
# 1 MB 是权衡：真实 skill 里没有这么大的手写脚本，但压缩过的单行 JS 能到几 MB，
# 而正则跑那种文件又慢又没意义。
MAX_FILE_BYTES = 1_000_000

# 只读开头这么多字节判二进制。8 KB 足够越过任何常见的文件头。
SNIFF_BYTES = 8192


def _looks_binary(path: Path) -> bool:
    """开头有没有 NUL 字节。读不开时返回 False —— 读失败会在 scan_file 里
    如实报成 unreadable，不在这个地方把它吞掉变成"二进制跳过"。"""
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(SNIFF_BYTES)
    except OSError:
        return False


# 会被 Claude 当**指令**读的文本格式。
#
# ⚠️ **`.md` 必须算 script，不能算 doc。** 第一版把它归成文档，理由是
# "README 里的 `curl | bash` 多半是示例" —— 那是错的，而且错在要害上：
# 这个项目里 **`SKILL.md` 就是被执行的东西**。它写什么，Claude 就做什么。
# 把它降级成"文档命中"，等于对最该严的文件最宽松。
# （`tests/test_verify.py::test_danger_rm_rf` 当场抓到了这个错误。）
#
# README.md 里真的写了 `rm -rf /` 的例子会因此 REJECT —— 那也没错：
# 理由串里带着文件名和行号，人看一眼就知道是不是示例。
# 反过来（放过 SKILL.md）是没法靠看一眼补回来的。
INSTRUCTION_EXTS = {".md", ".markdown", ".mdx", ".mdc"}

# **只有这两个文件名**才算"Claude 会当指令读的东西"。
#
# ⚠️ 别把它扩大成"所有 .md" —— 上面 `_source_kind` 的注释解释了为什么：
# 那会让每一份 `references/*.md` 里的**引用**都升级成 REJECT 级。
INSTRUCTION_FILENAMES = {"skill.md", "claude.md"}


def _source_kind(path: Path) -> str:
    """`script` = 红组命中就 REJECT；`doc` = 红组命中只 REVIEW。

    分界不是"它是不是文本"，是"**它会不会被当命令执行、或当指令读**"。
    `.txt` 里的一行 `curl | bash` 多半是笔记；`SKILL.md` 里的同一行是命令。

    ## ⚠️ 2026-10-07：`.md` 不再一律算"指令"

    这里原先写的是 `suffix in INSTRUCTION_EXTS`（`.md`/`.markdown`/`.mdx`/`.mdc`
    全部当 script）。出发点是对的 —— `SKILL.md` 确实就是 Claude 会照着执行的东西
    （`tests/test_verify.py::test_danger_rm_rf` 就是为它加的）。

    但把**每一份 `.md`** 都提成 script 是过度推广：`references/notes.md` 里
    引用一句 `rm -rf /` 当反面教材，和一个 `SKILL.md` 里写着"运行 rm -rf /"
    是两件完全不同的事。后果实测到了（2026-10-07，拿本机 80 个**真实在用**的
    skill 当语料）：一大片正常 skill 因为文档里提了个词就被判 REJECT。

    现在的分界**只认那两个文件名** —— 它们是 Claude 会当指令读的：
    """
    suffix = path.suffix.lower()
    name = path.name.lower()
    if name in INSTRUCTION_FILENAMES:
        return "script"          # SKILL.md / CLAUDE.md：Claude 真的照它执行
    if name in EXEC_NAMES or suffix in SCRIPT_EXTS:
        return "script"          # 脚本 / 可执行
    # 无扩展名的文件（`install`、`configure`、`bootstrap` 这类）按脚本算 ——
    # 宁可误判成脚本（严格）也不要误判成文档（宽松）。
    if not suffix:
        return "script"
    return "doc"


# 注释行的开头形态。`*` 也收进来是有意的：Markdown 的条目（`* 例如 rm -rf /`）
# 和代码块注释一样，都是**在描述**而不是在执行。
_COMMENT_PREFIXES = ("#", "//", "*", "/*", "<!--", ";", "--", "rem ")


def _looks_like_comment(line: str) -> bool:
    """这一行是不是注释/说明文字（而不是会被执行的代码）。"""
    return line.lstrip().startswith(_COMMENT_PREFIXES)


# 同一行里出现这些 → 这行**真的在执行**东西，字符串里的危险内容也不能降级。
_EXEC_CONTEXT = (
    "bash -c", "bash-c", "sh -c", "sh-c", "zsh -c",
    "exec(", "eval(", "spawn(", "system(", "popen(", "subprocess", "os.system",
    "child_process", "Invoke-Expression", "iex ", "-Command",
)


def _hit_is_data(line: str, match_start: int, filename: str = "") -> bool:
    """命中落在**字符串字面量**里吗？→ 那它是数据，不是要执行的命令。

    实测动机（2026-10-07）：`context-mode` 是个**分析 shell 命令**的 skill，
    它的测试用例长这样：

        const parts = splitChainedCommands("git status $(sudo rm -rf / | cat) && echo done")
        assert.deepEqual(parts, [...])
        'os.system("rm -rf /")',

    `rm -rf /` 出现在**字符串里**，整条是一个表达式 —— 它没有在执行任何东西。
    而这种 skill 恰恰是安全工具，被拒掉是最讽刺的结果。

    ## 两个必须挡住的假阴性

    1. **`bash -c "rm -rf /"`** —— 也是"字符串里"，但**真的在执行**。
       所以同一行只要出现执行构造（`_EXEC_CONTEXT`）就不降级。
    2. **`package.json` 的 `scripts.postinstall`** —— 值也在字符串里，
       但 **npm 会去执行它**。所以对「本身就是被执行的东西」的文件
       （`EXEC_NAMES`）整条启发式不适用。
       （这条是 `tests/test_l3_scan.py` 当场抓到的：我把 curl|bash 从红组
        降下来时，那条用例红了，暴露出新启发式会放过 package.json。）
    """
    if filename and filename in EXEC_NAMES:
        return False           # 这类文件里的字符串**就是**要执行的命令
    # 执行构造必须出现在**引号外面**才算数。反例（实测）：context-mode 的夹具
    #   'os.system("sudo rm -rf /")',   ← 整行是个字符串元素，里面**写着**它
    # 不剥引号的话 `os.system` 会被当成"这行在执行"。
    unquoted = re.sub(r'"[^"]*"|\'[^\']*\'', '""', line).lower()
    if any(k.lower() in unquoted for k in _EXEC_CONTEXT):
        return False
    # 命中点之前若处在成对的引号之内（引号数为奇数），就是在字符串里
    return line[:match_start].count('"') % 2 == 1 or line[:match_start].count("'") % 2 == 1


def scan_file(filepath: Path, source_kind: str = "script") -> dict:
    """扫描单个文件。返回匹配结果，**读失败时如实标注**。

    以前读失败是 `return {}` —— 那个空字典会一路静默地消失在 `scan_skill` 里，
    而 `scanned_files` 还把它算作已扫描。于是报告在一个**从没打开过**的文件上
    宣称"未发现危险或可疑模式"。现在返回 `unreadable` 字段，由上层判 REVIEW。
    """
    try:
        content = filepath.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        return {
            "file": str(filepath),
            "source_kind": source_kind,
            "unreadable": f"{type(e).__name__}: {e}",
            "findings": {"red": [], "yellow": [], "blue": []},
        }

    result = {"file": str(filepath), "source_kind": source_kind,
              "findings": {"red": [], "yellow": [], "blue": []}}

    for line_no, line in enumerate(content.split("\n"), 1):
        # 红组
        #
        # ⚠️ **注释行上的红命中降级为黄**（2026-10-07）。理由是实测出来的：
        # 「提到」和「在做」是两件事，而这组规则分不出来。本机 80 个真实在用的
        # skill 里，剩下的红命中**全部**落在注释或文档上：
        #
        #   claude-code（一个专门讲安全边界的 skill）:
        #       * ~/.ssh/authorized_keys) would pass a resolve()-based containment check.
        #       //   rm -rf /
        #       'PowerShell Download-and-Execute: `iex (iwr ...)`'   ← 它在**检测**这个
        #   obsidian-second-brain:
        #       curl -LsSf https://astral.sh/uv/install.sh | sh      ← uv 的官方安装说明
        #
        # 不做这层区分，"提到即拒"就会**优先拒掉最懂安全的那些 skill** ——
        # 这个结果是实测出来的，不是推测。
        #
        # 判定：一行里 `rm -rf /` 前面有 `#`/`//`/`*`，说明写它的人在**描述**它；
        # 裸着的那行才是**要执行**的。
        is_comment = _looks_like_comment(line)
        red_this_line = False
        for name, pattern in DANGER_PATTERNS.items():
            m = re.search(pattern, line, re.IGNORECASE)
            if m:
                if is_comment:
                    result["findings"]["yellow"].append({
                        "rule": f"{name}（注释里）",
                        "line": line_no,
                        "snippet": line.strip()[:120]
                    })
                    continue
                if _hit_is_data(line, m.start(), filepath.name.lower()):
                    result["findings"]["yellow"].append({
                        "rule": f"{name}（字符串里）",
                        "line": line_no,
                        "snippet": line.strip()[:120]
                    })
                    continue
                red_this_line = True
                result["findings"]["red"].append({
                    "rule": name,
                    "line": line_no,
                    "snippet": line.strip()[:120]
                })

        # 黄组
        for name, pattern in SUSPICIOUS_PATTERNS.items():
            if re.search(pattern, line, re.IGNORECASE):
                result["findings"]["yellow"].append({
                    "rule": name,
                    "line": line_no,
                    "snippet": line.strip()[:120]
                })

        # 黄组（策略驱动）—— 规则来自 `sandbox/audit-policy.yaml`，不是硬编码。
        #
        # 这一组补的是硬编码规则的**盲区**：`~/.aws/` 之类已经在红组里了，
        # 但 `~/.docker/config.json` / `~/.kube/` / `~/.claude/settings.json`
        # 这些同样值钱的位置以前没人看。策略文件是它们的**唯一一份**清单。
        #
        # 红组已经命中的行不再补黄：同一条路径报两处只会把黄计数灌水，
        # 而判定由红组决定，多这一条不影响结论。只补盲区，不与红组抢功。
        if not red_this_line:
            for hit in audit_policy.find_sensitive_paths(line):
                result["findings"]["yellow"].append({
                    "rule": f"敏感路径（策略）：{hit}",
                    "line": line_no,
                    "snippet": line.strip()[:120]
                })

    # 蓝组 — 全局搜索
    for name, pattern in INFO_PATTERNS.items():
        matches = re.findall(pattern, content, re.IGNORECASE)
        if matches:
            result["findings"]["blue"].append({
                "rule": name,
                "count": len(matches),
                "samples": matches[:5]
            })

    return result


def compute_verdict(findings_list: list, coverage: dict = None,
                    unreadable: list = None) -> dict:
    """根据所有文件的扫描结果计算最终判决。

    判定顺序（**从重到轻，先命中先定**）：

      1. 可执行文件里有红          → REJECT
      2. 有文件读不出来            → REVIEW（fail-closed）
      3. 文档里才有红              → REVIEW（多半是示例命令）
      4. 有黄                      → REVIEW
      5. 都没有                    → PASS，且**理由里写清覆盖范围**
    """
    coverage = dict(coverage or {})
    unreadable = list(unreadable or [])

    red_script, red_doc = [], []
    all_yellow, all_blue = [], []

    for f in findings_list:
        if f.get("unreadable"):
            unreadable.append({"file": f.get("file", ""), "error": f["unreadable"]})
            continue
        findings = f.get("findings", {}) or {}
        red = findings.get("red", [])
        if f.get("source_kind") == "doc":
            red_doc.extend(red)
        else:
            red_script.extend(red)
        all_yellow.extend(findings.get("yellow", []))
        all_blue.extend(findings.get("blue", []))

    scanned = coverage.get("scanned_files", 0)
    binary = coverage.get("skipped_binary", 0)
    too_big = coverage.get("skipped_too_big", 0)
    dirs = coverage.get("skipped_dirs", 0)

    # 覆盖范围的如实描述。**PASS 的理由不能是一句无条件的"没发现问题"** ——
    # 读的人会把它理解成"这个 skill 是干净的"，而它实际的含义是
    # "在我看过的这些文件里没发现问题"。这两个说法差得远。
    cover = (f"已扫描 {scanned} 个文本文件，"
             f"跳过 {binary} 个二进制、{too_big} 个超大文件、{dirs} 个目录")

    # 策略没读成 → 那条"敏感路径"检查**没跑**。这句话必须出现在理由里，
    # 否则报告会读成"该查的都查了"。它**不**改变判定 ——
    # 缺 pyyaml 是这台机器的缺件，不是被审 skill 的问题（同 L5 缺 Docker → SKIPPED）。
    policy_error = coverage.get("policy_error") or ""
    if policy_error:
        cover += f"；⚠️ 敏感路径策略未生效（{policy_error}）"

    if red_script:
        verdict = "REJECT"
        color = "red"
        reason = (f"在可执行文件里发现 {len(red_script)} 个高危模式，建议拒绝安装"
                  f"（另有 {len(red_doc)} 个命中在文档里）" if red_doc
                  else f"在可执行文件里发现 {len(red_script)} 个高危模式，建议拒绝安装")
    elif unreadable:
        verdict = "REVIEW"
        color = "yellow"
        first = unreadable[0]
        reason = (f"{len(unreadable)} 个文件读不出来 —— **没读过的东西不能算干净**。"
                  f"首个：{Path(first['file']).name}（{first['error'][:80]}）。"
                  "要么修好权限/占用后重扫，要么人工确认这些文件的内容")
    elif red_doc:
        verdict = "REVIEW"
        color = "yellow"
        reason = (f"{len(red_doc)} 个高危模式命中在**文档/非脚本**文件里"
                  "（多半是示例命令，但需要人确认一次）")
    elif all_yellow:
        verdict = "REVIEW"
        color = "yellow"
        reason = f"发现 {len(all_yellow)} 个可疑模式，需要人工审核"
    else:
        verdict = "PASS"
        color = "green"
        reason = f"{cover}，均未命中危险或可疑模式"

    return {
        "verdict": verdict,
        "color": color,
        "reason": reason,
        "summary": {
            "red_count": len(red_script) + len(red_doc),
            "red_in_scripts": len(red_script),
            "red_in_docs": len(red_doc),
            "yellow_count": len(all_yellow),
            "blue_count": len(all_blue),
            "unreadable_count": len(unreadable),
        },
        "coverage": coverage,
        "unreadable": unreadable,
        "red": red_script,
        "red_doc": red_doc,
        "yellow": all_yellow,
        "blue": all_blue,
    }


def scan_skill(skill_path: str) -> dict:
    """扫描 skill 目录下**所有文本文件**。

    ## 为什么不再是扩展名白名单

    原先是 `for ext in ["*.sh","*.py",...]: rglob(ext)` —— **默认不扫**。
    没列进去的文件根本不会被打开，而报告照样写"未发现危险或可疑模式"。
    盲区正好落在没人会想到的地方：`Makefile`、`Dockerfile`、
    `package.json` 的 scripts、无扩展名的脚本。

    现在反过来：**默认扫，除非确认是二进制**（开头有 NUL 字节）。
    错误的默认方向从"我不认识就不看"变成"除非证明它是二进制，否则都看"。
    前者漏掉的东西不会有任何提示。

    ## 跳过了什么，报告里说得出来

    `coverage` 带着每个计数一起走：扫了几个、跳过几个二进制、几个超大、
    几个目录。**静默的截断读起来就像"全扫完了"** —— 那正是这一层最大的问题。
    """
    root = Path(skill_path)
    candidates = []
    skipped_binary = 0
    skipped_too_big = 0
    skipped_dirs = 0
    total_files = 0

    # 路径不存在 / 不是目录 → **绝不能 PASS**。
    #
    # `main()` 检查过 `exists()`，但 `scan_skill()` 是被 `daemon/precheck.py`
    # 直接调的，那里没有这一层。一条走错的路径会让 os.walk 一个文件都不产出，
    # 于是 `coverage` 全是 0、判定落到"均未命中危险或可疑模式" ——
    # **"什么都没扫"报成"干净"**，正是这一层刚修掉的那个毛病。
    # （测上面那个 Makefile 用例时，我把路径写成 MSYS 的 /tmp 就撞上了。）
    if not root.is_dir():
        return {
            "verdict": "REVIEW",
            "color": "yellow",
            "reason": (f"扫描目标不是一个目录：{skill_path} —— "
                       "没有扫过任何东西，所以既不能说干净也不能说危险"),
            "summary": {"red_count": 0, "red_in_scripts": 0, "red_in_docs": 0,
                        "yellow_count": 0, "blue_count": 0, "unreadable_count": 1},
            "coverage": {"total_files": 0, "scanned_files": 0,
                         "unreadable_files": 1, "skipped_binary": 0,
                         "skipped_too_big": 0, "skipped_dirs": 0},
            "unreadable": [{"file": str(skill_path), "error": "不是目录或不存在"}],
            "red": [], "red_doc": [], "yellow": [], "blue": [],
            "scanned_files": 0,
            "files_with_findings": 0,
        }

    # 策略只读一次（`audit_policy` 内部按 mtime 缓存），
    # 读不成的**原因**要跟着结论一路走到报告里。
    _, _policy_error = audit_policy.load()

    # 用 os.walk 而不是 rglob：**被排除的目录不该进去走**。
    # rglob 会一头扎进 node_modules 把每个文件都列出来再过滤 —— 慢，
    # 而且"跳过了多少"只能靠事后数，这里是在门口就掉头。
    for dirpath, dirnames, filenames in os.walk(root):
        keep = []
        for d in dirnames:
            if d in SKIP_DIRS:
                skipped_dirs += 1
            else:
                keep.append(d)
        dirnames[:] = keep

        for fn in filenames:
            p = Path(dirpath) / fn
            total_files += 1
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            if size > MAX_FILE_BYTES:
                skipped_too_big += 1
                continue
            # ⚠️ 判定**只看字节**，扩展名不参与。见文件上方那段注释：
            # 曾经这里有一道扩展名短路，让 `.png` 里的 shell 命令直接溜过去。
            if _looks_binary(p):
                skipped_binary += 1
                continue
            candidates.append(p)

    findings = []
    unreadable = []
    for p in candidates:
        r = scan_file(p, _source_kind(p))
        if r.get("unreadable"):
            unreadable.append({"file": r["file"], "error": r["unreadable"]})
            continue
        if (r["findings"]["red"] or r["findings"]["yellow"]):
            findings.append(r)

    coverage = {
        "total_files": total_files,
        # scanned_files 只数**真的读过**的 —— 读不出来不算"扫过了"。
        # precheck 和面板读的就是这个数，以前它把读失败的也算进去了。
        "scanned_files": len(candidates) - len(unreadable),
        "unreadable_files": len(unreadable),
        "skipped_binary": skipped_binary,
        "skipped_too_big": skipped_too_big,
        "skipped_dirs": skipped_dirs,
        # 策略没读成 = 那条"敏感路径"检查**没跑**。如实带出去，
        # 由 compute_verdict 写进理由 —— 静默的跳过读起来就像查过了。
        "policy_error": _policy_error,
    }

    verdict = compute_verdict(findings, coverage, unreadable)
    verdict["scanned_files"] = coverage["scanned_files"]
    verdict["files_with_findings"] = len(findings)
    return verdict


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: l3-content-scan.py <skill_path>"}, ensure_ascii=False))
        sys.exit(2)

    skill_path = sys.argv[1]
    if not Path(skill_path).exists():
        print(json.dumps({"error": f"Path not found: {skill_path}"}, ensure_ascii=False))
        sys.exit(2)

    report = scan_skill(skill_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report["verdict"] == "PASS" else (1 if report["verdict"] == "REVIEW" else 2))


if __name__ == "__main__":
    main()
