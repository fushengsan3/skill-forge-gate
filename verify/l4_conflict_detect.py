#!/usr/bin/env python3
"""
L4 冲突检测 — 检查新 skill 与已有 skill 的文件覆盖/Hook竞争/功能重叠/跨目录写入
普通目录写入 → 简单规则匹配
系统盘写入 → Claude API 语义分析（需 Token）
输出 JSON 到 stdout
"""
import json
import sys
import re
import os
from pathlib import Path

# 直接跑本文件时（`python verify/l4_conflict_detect.py`），sys.path[0] 是 verify/ 这一层，
# 不是仓库根，`from verify import llm_auth` 会找不到 `verify` 包。
# 先把仓库根放进去，别用 try/except 重试 —— 理由见 l5_sandbox.py 同一处的注释：
# 重试版的报错形状会把人引向"模块坏了"，而真实原因常常是"文件没部署"。
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from verify import llm_auth

# 系统盘路径（Windows + Unix）
#
# ## 这里踩过一个坑，别再踩回去
#
# 原先写的是 `r"C:\\Windows"`。在**原始字符串**里 `\\` 就是两个反斜杠 ——
# 那个字符串字面值是 `C:\\Windows`，跟任何真实路径都对不上。加上
# `is_system_path()` 会把路径的 `\` 统一换成 `/`，五条 Windows 条目
# **一条都匹配不上**。实测：`C:/Windows/System32/x.dll` → False。
#
# 后果不是"少标几个"，是**在 Windows 上一整条检查从不触发** ——
# 而它是 `requires_claude_analysis` 的开关，所以那句"系统盘写入 → Claude API
# 深度分析"在 Windows 上是双重失效。主战场恰恰是 Windows。
#
# ## 为什么没有裸的 `C:/`
#
# 想加回去的人先看这里：`is_system_path` 过滤的是 skill **声明要写**的路径。
# skill 自己就装在 `C:\Users\<你>\.claude\skills\` 下，裸 `C:/` 会让每个
# 往自己目录里写文件的 skill 都被标红 + 送进深度分析。那不是严格，是噪声。
# 要收的是 Windows / Program Files / System 这些**系统**目录。
#
# 一律用正斜杠写 —— `is_system_path()` 已经把路径规范化成正斜杠了。
SYSTEM_PATHS = [
    "C:/Windows", "C:/Program Files", "C:/Program Files (x86)",
    "C:/System",
    "/etc", "/usr", "/System", "/boot",
    "/sys", "/proc", "/dev",
]

# 网络代理
PROXY = "http://127.0.0.1:7897"


def is_system_path(path: str) -> bool:
    """判断路径是否涉及系统盘"""
    normalized = str(Path(path)).replace("\\", "/")
    for sp in SYSTEM_PATHS:
        if normalized.lower().startswith(sp.lower()):
            return True
    return False


def extract_hooks(skill_path: str) -> set:
    """提取 skill 中声明的 hook 类型"""
    skill_md = Path(skill_path) / "SKILL.md"
    if not skill_md.exists():
        return set()
    content = skill_md.read_text(encoding="utf-8", errors="ignore")
    hooks = set()
    for hook_type in ["PreToolUse", "PostToolUse", "PreMessage", "PostMessage",
                       "SessionStart", "SessionEnd", "Notification"]:
        if hook_type in content:
            hooks.add(hook_type)
    return hooks


def extract_declared_writes(skill_path: str) -> list:
    """提取 skill 中声明的文件写入路径"""
    skill_md = Path(skill_path) / "SKILL.md"
    if not skill_md.exists():
        return []
    content = skill_md.read_text(encoding="utf-8", errors="ignore")
    # ⚠️ 字符类里**必须**有 `:` —— 否则 Windows 的盘符路径会被截断：
    # 实测（2026-10-07）`save to C:/Users/x/skills/other/data.md` 只抽出 `'C'`，
    # 于是下面 `is_system_path()` 永远判 False —— **"系统盘写入"这条检查
    # 在 Windows 上一直是死的**，而本项目的目标平台就是 Windows。
    # （`/etc/hosts` 这种 POSIX 路径不受影响，所以之前的用例没暴露它。）
    write_patterns = re.findall(
        r"(?:write|save|output|create|install)\s+(?:to\s+)?([~/\w.\\:-]+(?:/\S+)?)",
        content, re.IGNORECASE
    )
    return [str(Path(p).expanduser()) for p in write_patterns]


def description_similarity(path_a: str, path_b: str) -> float:
    """简单关键词余弦相似度"""
    def get_keywords(p):
        md = Path(p) / "SKILL.md"
        if not md.exists():
            return set()
        text = md.read_text(encoding="utf-8", errors="ignore").lower()
        words = set(re.findall(r"[a-z\u4e00-\u9fff]{3,}", text))
        return words
    a_words = get_keywords(path_a)
    b_words = get_keywords(path_b)
    if not a_words or not b_words:
        return 0.0
    intersection = a_words & b_words
    return len(intersection) / min(len(a_words), len(b_words))


def detect_conflicts(new_skill_path: str, skills_root: str) -> dict:
    """主检测函数"""
    new_root = Path(new_skill_path)
    installed_root = Path(skills_root)
    report = {"conflicts": {"red": [], "yellow": [], "blue": []}}

    new_hooks = extract_hooks(new_skill_path)
    new_writes = extract_declared_writes(new_skill_path)

    for old_dir in installed_root.iterdir():
        if not old_dir.is_dir():
            continue
        if old_dir.name == new_root.name:
            continue  # 跳过自己
        if ".backup" in old_dir.parts or ".git" in old_dir.name:
            continue

        old_path = str(old_dir)

        # A: (见循环外 —— 这条不依赖 old_dir)

        # B: Hook 竞争
        old_hooks = extract_hooks(old_path)
        shared_hooks = new_hooks & old_hooks
        if shared_hooks:
            report["conflicts"]["yellow"].append({
                "type": "Hook 竞争",
                "skill": old_dir.name,
                "hooks": list(shared_hooks)
            })

        # C: 功能重叠
        sim = description_similarity(new_skill_path, old_path)
        if sim > 0.6:
            report["conflicts"]["blue"].append({
                "type": "功能相似",
                "skill": old_dir.name,
                "similarity": round(sim, 2)
            })

    # A: 声明的写入落到了**别人家里**
    #
    # ⚠️ 2026-10-07 重写。原先这里是 `find_path_overlap()` —— 拿两个 skill
    # 目录里**同名相对路径**当"文件覆盖"直接判 REJECT。那问的是错的问题：
    # 每个 skill 装进**自己的目录** `skills/<名字>/`，`A/references/x.md` 和
    # `B/references/x.md` 永远碰不到一起。它只排除了 `SKILL.md`（因为人人都有），
    # 而 `README.md` / `references/` / `scripts/` / `LICENSE` 同样人人都有。
    #
    # 实测（2026-10-07，拿本机 80 个**真实在用**的 skill 当语料）：
    # 13 个被这一条判 REJECT —— 它们全是正常 skill，只是文件结构撞名。
    #
    # 真正该问的是：**这个 skill 声明的写入，有没有落到它自己目录之外、
    # 却又在 skills 根里面的地方**（那就是真的会覆盖别人）。
    # 落在系统盘的由下面 D 那条管；落在自己目录里的完全正常。
    for w in new_writes:
        p = Path(w)
        if not p.is_absolute():
            continue
        try:
            rel = p.resolve().relative_to(installed_root.resolve())
        except (ValueError, OSError):
            continue           # 不在 skills 根下面 —— 那是 D 那条管的
        if rel.parts and rel.parts[0] != new_root.name:
            report["conflicts"]["red"].append({
                "type": "写入他人目录",
                "skill": str(rel.parts[0]),
                "path": w,
            })

    # D: 系统盘写入检查
    system_writes = [w for w in new_writes if is_system_path(w)]
    if system_writes:
        report["conflicts"]["red"].append({
            "type": "系统盘写入",
            "paths": system_writes,
            "requires_claude_analysis": True
        })

    # 判决
    if report["conflicts"]["red"]:
        report["verdict"] = "REVIEW" if any(
            c.get("requires_claude_analysis") for c in report["conflicts"]["red"]
        ) else "REJECT"
    elif report["conflicts"]["yellow"]:
        report["verdict"] = "REVIEW"
    else:
        report["verdict"] = "PASS"

    return report


def claude_deep_analysis(report: dict) -> dict:
    """对涉及系统盘写入的冲突，调用 Claude API 做深度分析"""
    system_writes = [
        c for c in report.get("conflicts", {}).get("red", [])
        if c.get("requires_claude_analysis")
    ]
    if not system_writes:
        return report

    # 凭据判定走 verify/llm_auth.py 那一份 —— 原先这里只认 ANTHROPIC_API_KEY，
    # 于是配第三方中转（Claude Code 默认就是）的机器上，这段深度分析
    # 一次都不会触发。跟 L5 是同一个毛病，同一处修。
    api_key, auth_style, key_source = llm_auth.credentials()
    if not api_key:
        report["claude_analysis"] = {
            "error": "未配置 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN，跳过深度分析"}
        return report

    import urllib.request
    paths = json.dumps([c.get("paths", []) for c in system_writes], ensure_ascii=False)
    prompt = f"""分析以下 skill 是否计划写入系统盘路径，是否存在安全风险：

系统盘写入路径：{paths}

请评估风险等级（safe/suspicious/dangerous）并给出简短理由。仅返回 JSON。"""

    body = json.dumps({
        "model": llm_auth.model(),
        # 300 太少：开了思考的模型（第三方中转默认开）会先出一大段 thinking
        # 把预算吃光，正文一个 token 都不剩。L5 那边被同一个量级的值坑过 ——
        # 现象是"模型好像什么都没说"，实际上是截断了。
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()

    # 宿主进程发请求，**要**走宿主代理。跟容器里正相反 —— 那边不能带，
    # 带了等于把宿主的内网地址带进沙箱。
    proxy_handler = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(
        llm_auth.base_url() + "/v1/messages",      # 不再写死 api.anthropic.com
        data=body,
        headers=llm_auth.auth_headers(api_key, auth_style),
    )
    try:
        with opener.open(req, timeout=60) as resp:
            result = json.loads(resp.read().decode())
            report["claude_analysis"] = {
                "model": llm_auth.model(),
                "key_source": key_source,
                # 用 first_text，**别**写 content[0]["text"] ——
                # 思考块排在最前面，取 [0] 会永远拿到"分析失败"。
                "assessment": llm_auth.first_text(result, "分析失败"),
            }
    except Exception as e:
        report["claude_analysis"] = {"error": f"{type(e).__name__}: {e}"}

    return report


def main():
    if len(sys.argv) < 3:
        print(json.dumps({"error": "Usage: l4-conflict-detect.py <new_skill_path> <skills_root>"}, ensure_ascii=False))
        sys.exit(2)

    report = detect_conflicts(sys.argv[1], sys.argv[2])

    # 有系统盘写入 → 调 Claude 深度分析
    if report["verdict"] == "REVIEW" and any(
        c.get("requires_claude_analysis")
        for c in report.get("conflicts", {}).get("red", [])
    ):
        report = claude_deep_analysis(report)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
