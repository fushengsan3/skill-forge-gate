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

        # ★ 装到「本体」上。`skill-forge` 是合法名字，`check_name` 放行 ——
        # 但 `install_skill` 最后是 rmtree 再 copytree，所以那是**用别人的仓库
        # 整体替换掉管理器**。这条判断必须独立于名字校验存在。
        r3 = installer.install_skill("skill-forge", "https://github.com/a/b")
        check(r3.get("ok") is False, "★ 拒绝安装到 skill-forge 本体上",
              str(r3.get("error"))[:70])
        check(cloned == [], "★ 同样在 clone 之前就拦下（不白 clone 一遍）")
    finally:
        installer._git_clone = real

    # is_forge_dir 本身：它判的是**解析后的路径**，不是名字字符串。
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "skills"
        root.mkdir()
        (root / "skill-forge").mkdir()
        (root / "ordinary").mkdir()
        check(sp.is_forge_dir(root / "skill-forge", root),
              "is_forge_dir 认得出本体目录")
        check(not sp.is_forge_dir(root / "ordinary", root),
              "普通 skill 目录不是本体")
        check(not sp.is_forge_dir(root, root),
              "skills 根目录本身不是本体（它是装着本体的那个）")
        check(not sp.is_forge_dir(object(), root),
              "拿到奇怪的东西（Path() 会抛 TypeError）时判 False 而不是崩 —— "
              "它是布尔判断，总该能返回；False 的含义是「不认为它是本体」，"
              "调用方随后仍会走正常校验")

    # ★ #35：仓库地址的判定必须比**主机**，不能比整串子串。
    #
    # 原先是 `"github.com" in url` —— 于是 `https://attacker.example/github.com/evil`
    # 会被当成 GitHub 地址**放行，然后真的去 clone 它**。
    # 这些 URL 来自面板 / 安装队列，是不受信输入。
    print("--- 6b. ★ 仓库地址判定：比主机，不比子串 ---")
    from daemon.installer import _is_github_url
    accepted = [
        ("https://github.com/a/b", "标准地址"),
        ("https://github.com/a/b/tree/main/sub", "子目录 URL"),
        ("https://github.com/a/b/blob/main/SKILL.md", "文件 URL"),
        ("github.com/a/b", "无 scheme 的简写"),
        ("git@github.com:a/b.git", "SSH 简写（sources.json 里真出现过）"),
        ("https://api.github.com/repos/a/b", "API 地址"),
    ]
    for url, why in accepted:
        check(_is_github_url(url), f"接受 {why}", url)
    rejected = [
        ("https://attacker.example/github.com/evil", "★ 把 github.com 埋进路径里"),
        ("https://evil.com/?x=github.com", "★ 埋进查询串里"),
        ("https://notgithub.com/a/b", "★ 后缀伪装"),
        ("https://github.com.evil.example/a", "★ 把真域名当前缀"),
        ("", "空串"),
    ]
    for url, why in rejected:
        check(not _is_github_url(url),
              f"拒绝 {why}（旧写法 `\"github.com\" in url` 会放行）", url or "(空)")


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

            # ★ 卸载「本体」。`skill-forge` 是**合法**的单分量名字，
            # 而且拼出来的路径确实落在 skills 里面 —— 所以上面那些名字校验
            # 一条都拦不住它。它指向的是**我们自己**。
            # 2026-10-06 之前这里没有任何判断：面板上一点（或往接口塞一条）
            # 就把管理器连同日志、队列、部署留档一起删掉，而执行删除的正是它自己。
            forge_dir = skills / "skill-forge"
            forge_dir.mkdir()
            forge_marker = forge_dir / "marker.txt"
            forge_marker.write_text("管理器自己", encoding="utf-8")
            code, body = uninstall("skill-forge")
            payload = json.loads(body) if body else {}
            check(code == 200 and payload.get("ok") is False,
                  "★ /uninstall skill-forge 被拒（合法名字，名字校验拦不住它）",
                  f"HTTP {code} {body[:80]}")
            check(forge_marker.exists(),
                  "★ 管理器本体还在磁盘上（这才是这条用例要证明的）")
            check(payload.get("error") == "self-uninstall-refused",
                  "★ 拒绝的原因是可判别的（前端能据此给专门的提示）",
                  str(payload.get("error")))

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
