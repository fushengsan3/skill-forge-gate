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

# 系统盘路径（Windows + Unix）
SYSTEM_PATHS = [
    r"C:\\Windows", r"C:\\Program Files", r"C:\\Program Files (x86)",
    r"C:\\System", r"C:\\", r"/etc", r"/usr", r"/System", r"/boot",
    r"/sys", r"/proc", r"/dev"
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
    write_patterns = re.findall(
        r"(?:write|save|output|create|install)\s+(?:to\s+)?([~/\w.\\-]+(?:/\S+)?)",
        content, re.IGNORECASE
    )
    return [str(Path(p).expanduser()) for p in write_patterns]


def find_path_overlap(new_path: str, existing_path: str) -> list:
    """找出两个目录中文件名+相对路径相同的文件（排除 SKILL.md，每个 skill 都有）"""
    overlaps = []
    new_root = Path(new_path)
    existing_root = Path(existing_path)
    if not new_root.exists() or not existing_root.exists():
        return overlaps
    for f in new_root.rglob("*"):
        if f.is_file() and ".git" not in f.parts and f.name != "SKILL.md":
            rel = f.relative_to(new_root)
            existing_file = existing_root / rel
            if existing_file.exists():
                overlaps.append(str(rel))
    return overlaps


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

        # A: 文件路径覆盖
        overlaps = find_path_overlap(new_skill_path, old_path)
        if overlaps:
            report["conflicts"]["red"].append({
                "type": "文件覆盖",
                "skill": old_dir.name,
                "files": overlaps
            })

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

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        report["claude_analysis"] = {"error": "未设置 ANTHROPIC_API_KEY，跳过深度分析"}
        return report

    import urllib.request
    paths = json.dumps([c.get("paths", []) for c in system_writes], ensure_ascii=False)
    prompt = f"""分析以下 skill 是否计划写入系统盘路径，是否存在安全风险：

系统盘写入路径：{paths}

请评估风险等级（safe/suspicious/dangerous）并给出简短理由。仅返回 JSON。"""

    body = json.dumps({
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 300,
        "messages": [{"role": "user", "content": prompt}]
    }).encode()

    proxy_handler = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
    )
    try:
        with opener.open(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            report["claude_analysis"] = {
                "model": "claude-haiku-4-5-20251001",
                "assessment": result.get("content", [{}])[0].get("text", "分析失败")
            }
    except Exception as e:
        report["claude_analysis"] = {"error": str(e)}

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
