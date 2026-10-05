#!/usr/bin/env python3
"""
bridge 鉴权回归测试（R7）。

对应决策记录 §6 的验收标准：
  - 从一个任意网站发起 POST http://127.0.0.1:18970/install  → 被拒绝
  - 面板发出的正常请求（密钥 + Origin 均通过）              → 成功
  - 重启后仍能连上（密钥不轮换）                            → 见"密钥稳定"一项

测试在临时端口上起一个真实的 bridge，用一个临时目录做 SKILL_ROOT，
所以不会碰到用户真实的安装、队列或密钥文件。
"""
import json
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from http.server import HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import daemon.bridge_auth as auth
import daemon.queue_bridge as qb

KEY_HEADER = auth.KEY_HEADER
EVIL_ORIGIN = "https://evil.example.com"

# 用户**真实**的密钥文件。测试全程只读它，用来证明"跑测试不会动生产密钥"。
REAL_KEY_FILE = Path.home() / ".claude" / "skills" / "skill-forge" / "templates" / ".bridge-key"

# 禁用代理：目标就是本机，走代理会连不上
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(port, method, path, headers=None, body=None):
    """发一个请求，返回 (状态码, 响应头, 响应体)。"""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers=headers or {},
    )
    try:
        with OPENER.open(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")


def main():
    results = []

    def check(name, ok, detail=""):
        results.append((name, bool(ok)))
        print(("  [PASS] " if ok else "  [FAIL] ") + name + (f" — {detail}" if detail else ""))

    tmp = tempfile.TemporaryDirectory()
    tmpdir = Path(tmp.name)

    # 运行前先记下真实密钥，最后一条断言会拿它比对
    real_key_before = auth._read_text(REAL_KEY_FILE)

    # SKILL_ROOT 指向临时目录 —— 密钥文件由它派生，所以整套鉴权都落在临时目录里，
    # 不会碰用户真实的密钥。**必须改这个常量而不是 KEY_FILE**：
    # check_request 走的 valid_keys() 是按 SKILL_ROOT 派生的，
    # 只改 KEY_FILE 的话，服务端那道校验读的仍是真实密钥。
    auth.SKILL_ROOT = tmpdir
    KEYF = tmpdir / "templates" / ".bridge-key"
    PREVF = tmpdir / "templates" / ".bridge-keys-prev.json"
    KEYF.parent.mkdir(parents=True, exist_ok=True)

    auth.reset_cache()
    key = auth.get_key()
    qb.SKILL_ROOT = tmpdir

    server = HTTPServer(("127.0.0.1", 0), qb.QueueHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()

    print("=" * 60)
    print("bridge 鉴权回归测试（R7）")
    print("=" * 60)
    print(f"临时端口: {port}")
    print(f"密钥文件: {auth.KEY_FILE}")
    print("")

    try:
        # --- 1. 跨站请求一律拒绝 ---
        code, _, _ = call(port, "POST", "/install", {"Origin": EVIL_ORIGIN})
        check("跨站 POST /install（无密钥）被拒", code == 403, f"HTTP {code}")

        code, _, _ = call(port, "POST", "/install",
                          {"Origin": EVIL_ORIGIN, KEY_HEADER: key})
        check("跨站 POST /install（带正确密钥）仍被拒 —— Origin 这一层独立生效",
              code == 403, f"HTTP {code}")

        code, _, _ = call(port, "POST", "/uninstall",
                          {"Origin": EVIL_ORIGIN, "Content-Type": "application/json"},
                          {"name": "anything"})
        check("跨站 POST /uninstall 被拒", code == 403, f"HTTP {code}")

        # --- 2. 本地 Origin 但密钥不对，也拒绝 ---
        code, _, _ = call(port, "POST", "/install", {"Origin": "null"})
        check("本地 Origin、无密钥 → 拒", code == 403, f"HTTP {code}")

        code, _, _ = call(port, "POST", "/install",
                          {"Origin": "null", KEY_HEADER: "wrong-key"})
        check("本地 Origin、错误密钥 → 拒", code == 403, f"HTTP {code}")

        # --- 3. 正常路径放行 ---
        code, _, body = call(port, "GET", "/installed",
                             {"Origin": "null", KEY_HEADER: key})
        ok = code == 200 and json.loads(body).get("ok") is True
        check("面板请求（Origin=null + 正确密钥）→ 通过", ok, f"HTTP {code}")

        code, _, body = call(port, "GET", "/installed", {KEY_HEADER: key})
        ok = code == 200 and json.loads(body).get("ok") is True
        check("本地脚本（无 Origin + 正确密钥）→ 通过", ok, f"HTTP {code}")

        # --- 4. CORS 头不再对全世界敞开 ---
        code, headers, _ = call(port, "GET", "/installed",
                                {"Origin": "null", KEY_HEADER: key})
        acao = headers.get("Access-Control-Allow-Origin", "")
        check("不再回 ACAO: *", acao != "*", f"ACAO={acao!r}")

        code, _, _ = call(port, "GET", "/installed", {"Origin": EVIL_ORIGIN, KEY_HEADER: key})
        check("跨站 GET 读不到数据（无 ACAO 回显）", code == 403, f"HTTP {code}")

        # --- 5. 预检请求 ---
        code, headers, _ = call(port, "OPTIONS", "/install", {"Origin": "null"})
        allow_headers = headers.get("Access-Control-Allow-Headers", "")
        ok = code == 200 and KEY_HEADER in allow_headers
        check("本地预检通过且允许密钥头", ok, f"HTTP {code}, Allow-Headers={allow_headers!r}")

        code, _, _ = call(port, "OPTIONS", "/install", {"Origin": EVIL_ORIGIN})
        check("跨站预检被拒", code == 403, f"HTTP {code}")

        # --- 6. 密钥轮换 + 宽限期（P0-6）---
        check("密钥已落盘", KEYF.exists())

        # 6a. ★ get_key 必须每次读盘。
        #     bridge 是**独立进程**，密钥由 watchdog 轮换。bridge 一旦缓存，
        #     轮换后它会拿旧密钥去比新面板带来的新密钥 —— 面板永远连不上，
        #     重启 bridge 才能恢复，而且表现和"密钥泄露了"一模一样。
        KEYF.write_text("changed-by-another-process", encoding="utf-8")
        check("★ get_key 每次读盘、不缓存（否则跨进程轮换根本不生效）",
              auth.get_key() == "changed-by-another-process")
        KEYF.write_text(key, encoding="utf-8")

        old_key = auth.get_key()
        new_key = auth.rotate()
        check("rotate 换出了一把不同的密钥", bool(new_key) and new_key != old_key)
        check("rotate 之后 get_key 返回新密钥", auth.get_key() == new_key)
        check("新密钥已落盘", KEYF.read_text(encoding="utf-8").strip() == new_key)

        # 6b. 新密钥立即可用
        code, _, _ = call(port, "GET", "/installed", {"Origin": "null", KEY_HEADER: new_key})
        check("新密钥立即可用", code == 200, f"HTTP {code}")

        # 6c. ★ 旧密钥在宽限期内仍可用 —— 已经开着的面板不能当场失效
        code, _, _ = call(port, "GET", "/installed", {"Origin": "null", KEY_HEADER: old_key})
        check("★ 旧密钥在宽限期内仍被接受（已打开的面板不会当场失效）",
              code == 200, f"HTTP {code}")

        # 6d. 宽限期一过就作废
        future = datetime.now() + timedelta(days=auth.GRACE_DAYS + 1)
        check("★ 宽限期过后旧密钥不再被接受（泄露最多能用到 GRACE_DAYS 天）",
              old_key not in auth.valid_keys(now=future))
        check("新密钥不受宽限期影响", new_key in auth.valid_keys(now=future))
        check("宽限期大于一个扫描周期（否则面板会在下次扫描前失效）",
              auth.GRACE_DAYS > 7, f"{auth.GRACE_DAYS} 天")

        # 6e. 过期条目会被清掉，表不会无限长
        auth._save_previous([
            {"key": "ancient", "until": (datetime.now() - timedelta(days=1)).isoformat()},
            {"key": "still-alive", "until": future.isoformat()},
        ])
        alive = [e["key"] for e in auth._load_previous()]
        check("读取时丢掉已过期的旧密钥", alive == ["still-alive"], str(alive))

        # 6f. 过期表坏了 → 退化为"只认当前密钥"，既不崩也不放行
        PREVF.write_text("{ 这不是 JSON", encoding="utf-8")
        check("宽限期表损坏时退化为只认当前密钥（不抛异常、不放行）",
              auth.valid_keys() == [new_key], str(auth.valid_keys()))

        # 6g. status 对外只给前缀
        st = auth.status()
        check("status 只回前缀，不回完整密钥",
              len(st.get("current_prefix", "")) == 6 and new_key not in json.dumps(st),
              json.dumps(st, ensure_ascii=False))

        # 6h. ★ 轮换必须落到调用方指定的 root 里。
        #     这条是踩过坑才加的：watchdog 的扫描流程会被 test_daemon 直接调用，
        #     而 rotate 最初用的是写死的绝对路径 —— 于是"跑一遍 pytest"
        #     就等于"轮换一次用户真实的密钥"。
        other = tmpdir / "另一个实例"
        auth.rotate(root=other)
        check("★ rotate(root=X) 把密钥写到 X 下面",
              (other / "templates" / ".bridge-key").exists())
        check("★ 用户真实目录里的密钥没被动过（跑测试不该改生产密钥）",
              auth._read_text(REAL_KEY_FILE) == real_key_before,
              f"真实密钥前缀 {auth._read_text(REAL_KEY_FILE)[:6]} vs 运行前 {real_key_before[:6]}")

        # 6i. ★ rotate 在**全新** root 上不能死锁。
        #     rotate() 持锁后会调 get_key()，而密钥文件不存在时 get_key() 也要
        #     拿同一把锁 —— 用普通 Lock 就会当场挂住，而且**没有任何输出**，
        #     表现是"测试跑着跑着就再也不结束"。这个坑实际踩到过。
        fresh = tmpdir / "全新实例"
        box = []
        th = threading.Thread(target=lambda: (box.append(auth.rotate(root=fresh)), None))
        th.daemon = True
        th.start()
        th.join(timeout=10)
        check("★ 全新 root 上 rotate 不死锁（那把锁必须可重入）",
              bool(box) and not th.is_alive(),
              "线程还挂着 = 死锁" if th.is_alive() else str(box)[:40])
        check("且 key 文件被建出来了", (fresh / "templates" / ".bridge-key").exists())

    finally:
        server.shutdown()
        server.server_close()
        tmp.cleanup()

    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
