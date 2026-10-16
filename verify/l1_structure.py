#!/usr/bin/env python3
"""
L1 结构校验 — 检查 SKILL.md 存在性、frontmatter 合法性、必需字段
输出 JSON 到 stdout，exit code 0=通过 1=警告 2=失败
"""
import json
import sys
import re
from pathlib import Path

REQUIRED_FIELDS = ["name", "description"]
FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)

# L1 认哪些文件名 —— **必须与 daemon/installer.py 的 _find_skill_files 一致**。
#
# 这两份清单曾经不一致：installer 认 4 个（含 CLAUDE.md），L1 只认 SKILL.md。
# 后果是一整类仓库永远装不上，而且两边说的话正好互相打脸 ——
# installer 说"找到了 skill 文件"，紧接着 L1 说"缺少 SKILL.md 文件"。
# 2026-10-16 实测：mksglu/context-mode 的根目录只有 CLAUDE.md，
# 用户连试 4 次，每次都被 L1 拒，且**理由是空的**（见下面那条 return）。
#
# 改这份清单时请同时改 installer 那份，反之亦然 —— 它们是同一个判定的两半。
SKILL_FILENAMES = ("SKILL.md", "skill.md", "CLAUDE.md", "claude.md")


def find_skill_file(root: Path):
    """按固定顺序找 skill 的入口文件；一个都没有返回 None。

    顺序**固定**（而不是取目录里先遍历到的那个）是为了结果可复现：
    一个目录同时有 SKILL.md 和 CLAUDE.md 时，永远选 SKILL.md。
    """
    for name in SKILL_FILENAMES:
        p = root / name
        if p.is_file():
            return p
    return None


def check_skill(skill_path: str) -> dict:
    root = Path(skill_path)
    issues = []

    # 1. 入口文件必须存在（SKILL.md 或 CLAUDE.md，见 SKILL_FILENAMES）
    skill_md = find_skill_file(root)
    if skill_md is None:
        msg = ("缺少 skill 入口文件 —— 这四个名字都不认："
               + "、".join(SKILL_FILENAMES))
        issues.append({"severity": "error", "msg": msg})
        # ⚠️ 这条**提前返回**原先不带 `reason`，于是 precheck 拼出来的汇总成了
        # 「L1 结构 未通过（REJECT）：」—— 冒号后面什么都没有，用户看到的就是
        # "没说为什么"。本函数另外三条返回路径都带 reason，只有这条漏了。
        return {"verdict": "REJECT", "reason": msg, "issues": issues}

    entry = skill_md.name

    # 2. 非空文件
    content = skill_md.read_text(encoding="utf-8", errors="ignore")
    if len(content.strip()) < 10:
        issues.append({"severity": "error", "msg": f"{entry} 内容过短"})

    # 3. frontmatter 必须合法
    m = FRONTMATTER_RE.match(content)
    if not m:
        issues.append({"severity": "error", "msg": f"缺少合法 YAML frontmatter (--- ... ---)"})
    else:
        fm_text = m.group(1)
        for field in REQUIRED_FIELDS:
            if not re.search(rf"^{field}\s*:", fm_text, re.MULTILINE):
                issues.append({"severity": "error", "msg": f"frontmatter 缺少必需字段: {field}"})

    # 4. 目录不能为空（至少要有入口文件之外的内容声明）
    files = list(root.rglob("*"))
    non_entry = [f for f in files if f.name != entry and ".git" not in f.parts]
    if not non_entry:
        issues.append({"severity": "warning", "msg": f"skill 目录仅含 {entry}，无其他文件"})

    # 判决
    errors = [i for i in issues if i["severity"] == "error"]
    if errors:
        return {"verdict": "REJECT", "reason": f"{len(errors)} 个结构错误", "issues": issues}
    warnings = [i for i in issues if i["severity"] == "warning"]
    if warnings:
        return {"verdict": "WARN", "reason": f"{len(warnings)} 个结构警告", "issues": issues}
    return {"verdict": "PASS", "reason": "结构合法", "issues": []}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: l1-structure.py <skill_path>"}, ensure_ascii=False))
        sys.exit(2)
    report = check_skill(sys.argv[1])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
