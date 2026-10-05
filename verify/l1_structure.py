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


def check_skill(skill_path: str) -> dict:
    root = Path(skill_path)
    issues = []

    # 1. SKILL.md 必须存在
    skill_md = root / "SKILL.md"
    if not skill_md.exists():
        issues.append({"severity": "error", "msg": "缺少 SKILL.md 文件"})
        return {"verdict": "REJECT", "issues": issues}

    # 2. 非空文件
    content = skill_md.read_text(encoding="utf-8", errors="ignore")
    if len(content.strip()) < 10:
        issues.append({"severity": "error", "msg": "SKILL.md 内容过短"})

    # 3. frontmatter 必须合法
    m = FRONTMATTER_RE.match(content)
    if not m:
        issues.append({"severity": "error", "msg": "缺少合法 YAML frontmatter (--- ... ---)"})
    else:
        fm_text = m.group(1)
        for field in REQUIRED_FIELDS:
            if not re.search(rf"^{field}\s*:", fm_text, re.MULTILINE):
                issues.append({"severity": "error", "msg": f"frontmatter 缺少必需字段: {field}"})

    # 4. 目录不能为空（至少要有 SKILL.md 之外的内容声明）
    files = list(root.rglob("*"))
    non_skill_md = [f for f in files if f.name != "SKILL.md" and ".git" not in f.parts]
    if not non_skill_md:
        issues.append({"severity": "warning", "msg": "skill 目录仅含 SKILL.md，无其他文件"})

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
