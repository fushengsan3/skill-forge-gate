#!/usr/bin/env python3
"""
安装路径安全测试（P0-1 路径穿越 + P0-4 clone 证书校验）。

背景：`name` 从面板 / 安装队列来，直接拼成路径后紧跟一句 `rmtree`。
实测（见 daemon/safe_paths.py 顶部）：

    SKILLS_DIR / "../../.."                  → C:\\Users
    SKILLS_DIR / "C:/Users/<you>/Documents"  → C:\\Users\\<you>\\Documents

所以这个测试的重点不是"合法名能不能用"，而是**不安全的名字到底有没有被拦住**，
以及**诱饵目录事后是否完好** —— 断言"返回了错误"是不够的，
真正要证明的是"那个目录还在"。

全程用临时目录，不碰用户真实的 skills 目录。

用法：
    python tests/test_safe_paths.py
"""
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import daemon.bridge_auth as auth
import daemon.installer as installer
import daemon.queue_bridge as qb
import daemon.safe_paths as sp

KEY_HEADER = auth.KEY_HEADER
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


def call(port, method, path, headers=None, body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method, headers=headers or {},
    )
    try:
        with OPENER.open(req, timeout=5) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main():
    run_unit_checks()
    run_installer_checks()
    run_bridge_checks()
    run_clone_checks()

    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


# ---------------------------------------------------------------- 校验函数

def run_unit_checks():
    print("=" * 60)
    print("路径穿越防护测试（P0-1）")
    print("=" * 60)
    print("--- 1. 合法名字照常通过 ---")

    for good in ["my-skill", "obsidian-second-brain", "a", "skill with spaces",
                 "中文技能名", "foo.bar", "v1.2.3", "_private"]:
        check(sp.is_safe_name(good), f"接受 {good!r}")

    print("--- 2. 危险名字一律拒绝 ---")
    dangerous = [
        ("..", "父目录"),
        (".", "当前目录"),
        ("../../..", "多级上跳"),
        ("../sibling", "跳到兄弟目录"),
        ("a/b", "正斜杠"),
        ("a\\b", "反斜杠"),
        ("C:/Users/<you>/Documents", "绝对路径（盘符）"),
        ("C:\\Users\\<you>\\Documents", "绝对路径（反斜杠）"),
        ("\\\\server\\share", "UNC 路径"),
        ("/etc/passwd", "POSIX 绝对路径"),
        ("...", "三个点（不是 .. 但也不该当目录名用）"),
    ]
    for bad, why in dangerous:
        try:
            sp.check_name(bad)
            check(False, f"拒绝 {bad!r}（{why}）", "竟然通过了")
        except sp.UnsafeName as e:
            check(True, f"拒绝 {bad!r}（{why}）", str(e)[:52])

    print("--- 3. 静默变形的名字也要拒绝 ---")
    for bad, why in [(" foo", "首空格（Windows 会去掉）"),
                     ("foo ", "尾空格"),
                     ("foo.", "尾点（Windows 会去掉）"),
                     ("", "空串"),
                     ("   ", "纯空白"),
                     ("nul", "保留设备名"),
                     ("COM1", "保留设备名（大写）"),
                     ("con.txt", "设备名带扩展名")]:
        check(not sp.is_safe_name(bad), f"拒绝 {bad!r}（{why}）")

    check(sp.is_safe_name("console"), "但不误伤 console —— 它不是保留设备名")
    check(not sp.is_safe_name("x" * (sp.MAX_NAME_LEN + 1)), "拒绝超长名字")
    check(sp.is_safe_name("x" * sp.MAX_NAME_LEN), "长度上限本身可以通过")

    print("--- 4. 非字符串输入不能把函数打崩 ---")
    for bad in [None, 123, [".."], {"name": ".."}, object()]:
        try:
            sp.check_name(bad)
            check(False, f"拒绝非字符串 {type(bad).__name__}", "竟然通过了")
        except sp.UnsafeName:
            check(True, f"拒绝非字符串 {type(bad).__name__}")
        except Exception as e:
            check(False, f"拒绝非字符串 {type(bad).__name__}", f"抛了 {type(e).__name__} 而不是 UnsafeName")

    print("--- 5. 包含断言本身是有效的（不依赖字符白名单）---")
    with tempfile.TemporaryDirectory() as td:
        base = Path(td) / "skills"
        base.mkdir()
        escaped = base / ".." / ".." / "outer"
        try:
            sp._assert_within(base, escaped)
            check(False, "包含断言挡住越界路径", "竟然通过了")
        except sp.UnsafeName:
            check(True, "包含断言挡住越界路径（白名单被放宽也不会漏）")
        try:
            sp._assert_within(base, base)
            check(False, "包含断言拒绝 base 自身", "竟然通过了")
        except sp.UnsafeName:
            check(True, "包含断言拒绝 base 自身（否则 rmtree 会删掉整个 skills 目录）")
        ok = sp.resolve_within(base, "good-skill") == base / "good-skill"
        check(ok, "resolve_within 对合法名返回 base/name")


# ---------------------------------------------------------------- 安装路径

def run_installer_checks():
    print("--- 6. 安装路径：验名发生在 clone 之前 ---")
    cloned = []

    def fake_clone(url, dest, branch="main"):
        cloned.append(url)
        raise AssertionError("不该走到 clone")

    real = installer._git_clone
    installer._git_clone = fake_clone
    try:
        r = installer.install_skill("../../..", "https://github.com/a/b")
        check(r.get("ok") is False, "install_skill 拒绝 '../../..'", str(r.get("error"))[:60])
        check(cloned == [], "拒绝时**没有**发起 clone（早失败，不白clone一次）",
              str(cloned))

        r2 = installer.install_skill("C:/Users/<you>/Documents", "https://github.com/a/b")
        check(r2.get("ok") is False, "install_skill 拒绝绝对路径", str(r2.get("error"))[:60])
        check(cloned == [], "绝对路径同样在 clone 之前被拦")
    finally:
        installer._git_clone = real


# ---------------------------------------------------------------- 真跑 bridge

def run_bridge_checks():
    print("--- 7. 真起一个 bridge 打 /uninstall ---")

    with tempfile.TemporaryDirectory() as td:
        outer = Path(td)
        skills = outer / "skills"
        skills.mkdir()
        (skills / "real-skill").mkdir()
        (skills / "real-skill" / "SKILL.md").write_text("# real", encoding="utf-8")

        # 诱饵：在 skills 目录**外面**。路径穿越成功的话它会被删掉。
        decoy = outer / "decoy"
        decoy.mkdir()
        marker = decoy / "do-not-delete.txt"
        marker.write_text("如果这个文件没了，说明路径穿越没被挡住", encoding="utf-8")

        auth.SKILL_ROOT = outer
        (outer / "templates").mkdir(parents=True, exist_ok=True)
        auth.reset_cache()
        key = auth.get_key()
        qb.SKILLS_DIR = skills
        qb.SKILL_ROOT = skills          # sources.json 在临时目录里也不存在，正好

        server = HTTPServer(("127.0.0.1", 0), qb.QueueHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def uninstall(name):
            return call(port, "POST", "/uninstall",
                        {"Origin": "null", KEY_HEADER: key,
                         "Content-Type": "application/json"},
                        {"name": name})

        try:
            for evil, why in [("..", "上一级即 outer"),
                              ("../decoy", "直奔诱饵"),
                              ("../../decoy", "多绕一级"),
                              (str(decoy), "绝对路径直接点名诱饵")]:
                code, body = uninstall(evil)
                payload = json.loads(body) if body else {}
                check(code == 200 and payload.get("ok") is False,
                      f"/uninstall {evil!r} 被拒（{why}）", f"HTTP {code} {body[:70]}")
                check(marker.exists(), f"      诱饵文件仍在（{why}）")

            check(decoy.exists() and any(decoy.iterdir()),
                  "诱饵目录整体完好 —— 这才是真正要证明的")

            # 合法卸载必须照常工作，别把功能一起关掉了
            code, body = uninstall("real-skill")
            payload = json.loads(body) if body else {}
            gone = not (skills / "real-skill").exists()
            check(code == 200 and payload.get("ok") is True and gone,
                  "合法名字仍能正常卸载（没有把功能一并关掉）",
                  f"HTTP {code} {body[:70]}")

            # 不存在但不危险的名字：维持原行为（成功，什么都没删）
            code, body = uninstall("not-installed")
            payload = json.loads(body) if body else {}
            check(code == 200 and payload.get("ok") is True,
                  "不存在的合法名字仍返回成功（保持原行为）", f"HTTP {code} {body[:70]}")
        finally:
            server.shutdown()
            server.server_close()


# ---------------------------------------------------------------- clone 校验

def run_clone_checks():
    print("--- 8. clone 不关证书校验（P0-4）---")
    import os as _os
    import subprocess as _sp

    safe_dest = Path(tempfile.gettempdir()) / "__sf_no_such_clone_target__"
    assert not safe_dest.exists()

    captured = []

    class FakeProc:
        returncode = 0
        stdout = ""
        stderr = ""

    def ok_run(args, **kw):
        captured.append({"args": args, "env": dict(kw.get("env") or {})})
        return FakeProc()

    def fail_run(args, **kw):
        captured.append({"args": args, "env": dict(kw.get("env") or {})})
        raise _sp.CalledProcessError(128, args, output="",
                                     stderr="fatal: repository 'x' not found\n")

    real_run = installer.subprocess.run
    had_it = "GIT_SSL_NO_VERIFY" in _os.environ
    try:
        installer.subprocess.run = ok_run
        ok, detail = installer._git_clone("https://github.com/a/b.git", safe_dest)
        check(ok and detail == "", "clone 成功时返回 (True, '')", repr(detail))

        env = captured[0]["env"]
        check("GIT_SSL_NO_VERIFY" not in env,
              "★ 传给 git 的环境里没有 GIT_SSL_NO_VERIFY（不再关掉证书校验）",
              str({k: v for k, v in env.items() if "SSL" in k.upper()}))

        # 用户环境里本来就有的话，也必须被显式清掉
        _os.environ["GIT_SSL_NO_VERIFY"] = "1"
        captured.clear()
        installer._git_clone("https://github.com/a/b.git", safe_dest)
        check("GIT_SSL_NO_VERIFY" not in captured[0]["env"],
              "★ 环境里本来就有的 GIT_SSL_NO_VERIFY 也被清掉（不靠用户自觉）")

        check("https_proxy" in captured[0]["env"],
              "但代理仍然照设（清的是校验开关，不是代理）")

        # 失败原因要能看出来
        captured.clear()
        installer.subprocess.run = fail_run
        ok, detail = installer._git_clone("https://github.com/a/b.git", safe_dest)
        check(not ok and "not found" in detail,
              "clone 失败时带出 git 的真实报错（不再笼统说'网络不通'）", detail)
        check("https://github.com/a/b.git" in detail or "not found" in detail,
              "报错内容确实来自 git 的 stderr")
    finally:
        installer.subprocess.run = real_run
        if not had_it:
            _os.environ.pop("GIT_SSL_NO_VERIFY", None)

    redacted = installer._redact("https://user:ghp_secrettoken@github.com/a/b.git")
    check("ghp_secrettoken" not in redacted and "***@" in redacted,
          "_redact 抹掉 URL 里的凭据（错误信息会进日志和 HTTP 响应）", redacted)


if __name__ == "__main__":
    sys.exit(main())
