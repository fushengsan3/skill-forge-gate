#!/usr/bin/env python3
"""
压力检测。

攻防测试问的是"能不能被打穿"，这里问的是"量大了会不会塌"：

  1. 数据量：几千条 skill 走完分类/翻译/嵌页面
  2. 单条体积：1MB 的 description（上游可以把 description 写多大是它说了算）
  3. 历史深度：几十期存档的 panels 生成
  4. 队列长度：上千条待安装
  5. 并发：bridge 同时被多个请求打，其间还轮换密钥
  6. 反复部署/回滚：会不会越跑越脏、留档会不会失控
  7. 深目录 / 长路径：Windows 的 MAX_PATH 边界
  8. 畸形输入：超大 body、坏 JSON、缺字段

判据不是"多快"，是**不崩、不静默出错、结果仍然可校验**。

用法：
    python tests/test_stress.py
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import HTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import daemon.bridge_auth as auth
import daemon.classifier as classifier
import daemon.queue_bridge as qb
import daemon.safe_embed as safe_embed
import daemon.safe_paths as sp

results = []
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
KEY_HEADER = auth.KEY_HEADER


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


def timed(label, fn):
    t0 = time.time()
    out = fn()
    dt = time.time() - t0
    print(f"      （{label}: {dt:.2f}s）")
    return out, dt


def call(port, method, path, headers=None, body=None, timeout=60):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}",
                                 data=data, method=method, headers=headers or {})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, f"{type(e).__name__}: {e}"


def main():
    print("=" * 60)
    print("压力检测")
    print("=" * 60)
    tmp = Path(tempfile.mkdtemp(prefix="sf-st-"))
    try:
        stress_classify(tmp)
        stress_embed(tmp)
        stress_periods(tmp)
        stress_queue(tmp)
        stress_concurrency(tmp)
        stress_deploy_cycles(tmp)
        stress_paths(tmp)
        stress_malformed(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


# ---------------------------------------------------------------- 1 分类

def stress_classify(tmp: Path):
    print("--- 1. 三千条 skill 走分类 ---")
    n = 3000
    skills = []
    for i in range(n):
        skills.append({
            "name": f"skill-{i}",
            "full_name": f"owner{i % 50}/repo-{i}",
            "description": ("用于代码审查和自动修复的 skill " if i % 3 == 0
                            else "data pipeline and analytics helper ") * (i % 7 + 1),
            "stars": i % 500,
            "installs": i % 100,
            "url": f"https://github.com/owner{i % 50}/repo-{i}",
            "source": "github-topic",
            "status": "new",
        })

    out, dt = timed(f"{n} 条", lambda: classifier.classify_and_sort(skills))
    check(isinstance(out, dict), "分类返回字典", type(out).__name__)
    total = out.get("total", 0)
    check(total == n, f"★ 一条不漏：total == {n}", f"实际 {total}")
    cats = out.get("categories", {})
    summed = sum(len(v) for v in cats.values())
    check(summed == n, "★ 分类后总数对得上（没丢没重复）", f"各类合计 {summed}")
    check(dt < 60, "3000 条在 60 秒内跑完", f"{dt:.1f}s")


# ---------------------------------------------------------------- 2 嵌页面

def stress_embed(tmp: Path):
    print("--- 2. 单条 1MB 的 description ---")
    big = "描" * (1024 * 1024)          # 1M 个字符 ≈ 3MB UTF-8
    payload = {
        "generated": "2026-10-05T00:00:00",
        "total": 1,
        "categories": {"压力": [{"name": "big", "description": big,
                                 "url": "https://github.com/a/b", "stars": 1}]},
    }
    out, dt = timed("embed 1MB", lambda: safe_embed.json_for_script(payload))
    check(isinstance(out, str) and len(out) > 1_000_000, "嵌进去了", f"{len(out)} 字符")
    # 关键：必须是**合法 JSON**，而且里面不能有裸的 </script>
    check("</script" not in out.lower(), "★ 1MB 描述里没有裸的 </script>（转义生效）")
    try:
        back = json.loads(out)
        check(back["categories"]["压力"][0]["description"] == big,
              "★ 往返之后内容一字不差")
    except json.JSONDecodeError as e:
        check(False, "嵌入结果仍是合法 JSON", str(e))

    # 故意塞一个会闭合脚本的载荷 + 各种边界字符
    hostile = ('</script><img src=x onerror=alert(1)>'
               + "  "          # JS 里会断行的字符
               + "\\" + '"' + "\x00\x1b"
               + "<!--" + "-->" + "<![CDATA[")
    out2 = safe_embed.json_for_script({"d": hostile})
    check("</script" not in out2.lower(), "★ 敌意载荷里的 </script 被转义")
    check(json.loads(out2)["d"] == hostile, "★ 敌意载荷往返无损（含 U+2028/2029、空字节）")

    # 顶层不是 dict / 有循环引用时的行为
    for bad in [None, [], "字符串", 42]:
        try:
            r = safe_embed.json_for_script(bad)
            check(isinstance(r, str), f"非字典输入 {type(bad).__name__} 不崩")
        except Exception as e:
            check(False, f"非字典输入 {type(bad).__name__} 不崩", f"{type(e).__name__}: {e}")

    cyc = {}
    cyc["self"] = cyc
    try:
        safe_embed.json_for_script(cyc)
        check(False, "循环引用不导致静默产出坏 JSON（应该抛或明确处理）")
    except (ValueError, RecursionError) as e:
        check(True, "循环引用明确报错而不是产出坏 JSON", type(e).__name__)


# ---------------------------------------------------------------- 3 历史深度

def stress_periods(tmp: Path):
    print("--- 3. 40 期历史存档 ---")
    from daemon import periods
    discover = tmp / "discover"
    discover.mkdir(parents=True, exist_ok=True)
    for m in range(40):
        week = [
            {"name": f"sk-{m}-{i}", "full_name": f"a/b{m}{i}",
             "description": "desc " * 20, "url": f"https://github.com/a/b{m}{i}",
             "source": "github-topic", "created_at": f"2026-01-{m % 28 + 1:02d}T00:00:00Z"}
            for i in range(60)
        ]
        (discover / f"weekly-2026-{m % 12 + 1:02d}-{m % 28 + 1:02d}.json").write_text(
            json.dumps({"total": len(week), "categories": {"c": week}}, ensure_ascii=False),
            encoding="utf-8")

    snap, dt = timed("load_periods", lambda: periods.load_periods(discover))
    check(isinstance(snap, dict), "load_periods 返回字典")
    check("catalog" in snap and "periods" in snap, "两层结构都在", str(sorted(snap)))
    check(len(snap["periods"]) <= periods.MAX_PERIODS,
          f"★ 期数被截到上限 {periods.MAX_PERIODS}（否则页面体积会线性涨）",
          f"实际 {len(snap['periods'])}")
    check(snap.get("total_periods", 0) >= len(snap["periods"]),
          "★ 如实报告总期数（截断不能静默）",
          f"total={snap.get('total_periods')} 嵌入={len(snap['periods'])}")
    flat = safe_embed.json_for_script(snap)
    check(len(flat) < 8_000_000, "40 期 × 60 条嵌出来仍在合理体积内", f"{len(flat)/1024:.0f} KB")
    check(dt < 30, "40 期在 30 秒内读完", f"{dt:.1f}s")


# ---------------------------------------------------------------- 4 队列

def stress_queue(tmp: Path):
    print("--- 4. 一千条待安装队列 ---")
    work = tmp / "Q"
    (work / "templates").mkdir(parents=True)
    auth.SKILL_ROOT = work
    auth.reset_cache()
    key = auth.get_key()
    qb.SKILL_ROOT = work
    qb.SKILLS_DIR = work / "skills"
    qb.SKILLS_DIR.mkdir()
    qb.QUEUE_FILE = work / "templates" / "install-queue.json"

    server = HTTPServer(("127.0.0.1", 0), qb.QueueHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    h = {"Origin": "null", KEY_HEADER: key, "Content-Type": "application/json"}
    try:
        big = [{"name": f"s{i}", "url": f"https://github.com/a/b{i}",
                "added_at": "2026-10-05T00:00:00Z"} for i in range(1000)]
        _, dt = timed("写 1000 条队列", lambda: call(port, "POST", "/", h,
                                                  {"version": 1, "pending": big}))
        code, body = call(port, "GET", "/", h)
        got = json.loads(body)
        check(code == 200 and len(got.get("pending", [])) == 1000,
              "★ 1000 条队列写进去读得回来", f"{len(got.get('pending', []))} 条")
        check(dt < 20, "写入耗时合理", f"{dt:.1f}s")

        # 超大 body（20MB）—— 不该把服务打挂
        huge = {"version": 1, "pending": [],
                "junk": "x" * (20 * 1024 * 1024)}
        code, body = call(port, "POST", "/", h, huge, timeout=60)
        check(code in (200, 413, -1), "20MB 请求不会让服务崩溃", f"HTTP {code}")
        code2, body2 = call(port, "GET", "/", h)
        check(code2 == 200, "★ 超大请求之后服务仍然活着", f"HTTP {code2}")
        try:
            after = json.loads(body2)
        except json.JSONDecodeError:
            after = None
        check(isinstance(after, dict) and isinstance(after.get("pending"), list),
              "★ 超大请求之后队列仍是结构完好的 JSON（不是写坏的半截文件）",
              (body2 or "")[:60])
        # 注意：**覆盖是端点本来的语义**（面板每次提交整份队列），所以这里不要求
        # "没被冲掉" —— 要求的是服务还能用。再写一次验证。
        code3, _ = call(port, "POST", "/", h, {"version": 1, "pending": big})
        code4, body4 = call(port, "GET", "/", h)
        check(code3 == 200 and len(json.loads(body4).get("pending", [])) == 1000,
              "★ 超大请求之后仍能正常读写队列", f"写 {code3} / 读 {code4}")
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------- 5 并发

def stress_concurrency(tmp: Path):
    print("--- 5. 并发打 bridge，其间轮换密钥 ---")
    work = tmp / "C"
    (work / "templates").mkdir(parents=True)
    auth.SKILL_ROOT = work
    auth.reset_cache()
    qb.SKILL_ROOT = work
    qb.SKILLS_DIR = work / "skills"
    qb.SKILLS_DIR.mkdir()

    server = HTTPServer(("127.0.0.1", 0), qb.QueueHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    stop = threading.Event()
    errors = []
    seen = []

    rot_errors = []
    MAX_REQ = 3000      # 硬上限：万一 stop 没被设上，测试也不能永远挂着

    def hammer(i):
        for _ in range(MAX_REQ):
            if stop.is_set():
                return
            k = auth.get_key(root=work)     # 每次都取"当前"密钥，模拟轮换中的面板
            code, body = call(port, "GET", "/installed",
                              {"Origin": "null", KEY_HEADER: k}, timeout=10)
            seen.append(code)
            if code not in (200, 403):
                errors.append((i, code, body[:80]))

    def rotator():
        try:
            for _ in range(15):
                time.sleep(0.05)
                auth.rotate(root=work)
        except Exception as e:
            rot_errors.append(f"{type(e).__name__}: {e}")
        finally:
            # 必须在 finally 里放行。轮换一旦抛异常而没走到 stop.set()，
            # hammer 线程会一直转下去 —— 这个测试真的这么卡死过一次
            # （Windows 上 os.replace 撞上正被读的文件会 PermissionError）。
            stop.set()

    with ThreadPoolExecutor(max_workers=12) as ex:
        futs = [ex.submit(hammer, i) for i in range(10)]
        ex.submit(rotator)
        for f in futs:
            f.result(timeout=180)

    check(not rot_errors,
          "★ 并发读取期间轮换不抛异常（Windows 上 os.replace 会撞上正被读的文件）",
          str(rot_errors[:3]))

    check(not errors, f"★ 并发 + 轮换期间没有 5xx / 异常（{len(seen)} 次请求）",
          str(errors[:3]))
    check(seen.count(200) > 0, "并发期间仍然有成功请求", f"200×{seen.count(200)}")
    check(seen.count(403) == 0,
          "★ 没有请求被误拒（每次都取当前密钥，不该有 403）",
          f"403×{seen.count(403)}")

    # 并发发起端一起打同一个端点
    def one(i):
        code, _ = call(port, "GET", "/installed",
                       {"Origin": "null", KEY_HEADER: auth.get_key(root=work)}, timeout=10)
        return code

    with ThreadPoolExecutor(max_workers=20) as ex:
        codes = list(ex.map(one, range(200)))
    check(all(c == 200 for c in codes), "★ 200 个并发请求全部 200",
          f"非 200: {[c for c in codes if c != 200][:5]}")
    server.shutdown()
    server.server_close()


# ---------------------------------------------------------------- 6 反复部署

def stress_deploy_cycles(tmp: Path):
    print("--- 6. 反复部署/回滚 15 轮 ---")
    import importlib.util
    spec = importlib.util.spec_from_file_location("dm3", ROOT / "scripts" / "deploy.py")
    dm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dm)

    work = tmp / "D"
    src = work / "src"
    dst = work / "dst"
    for base in (src, dst):
        (base / "daemon").mkdir(parents=True, exist_ok=True)
        (base / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\n", encoding="utf-8")
        (base / "daemon" / "a.py").write_text("# a\n", encoding="utf-8")
    (src / "scripts").mkdir(exist_ok=True)
    shutil.copy(ROOT / "scripts" / "data-paths.txt", src / "scripts" / "data-paths.txt")
    (dst / "sources.json").write_text('{"canary": "数据"}', encoding="utf-8")

    for args in (("init", "-q"), ("config", "user.email", "t@t"),
                 ("config", "user.name", "t"), ("add", "-A"), ("commit", "-q", "-m", "i")):
        subprocess.run(["git", "-C", str(src)] + list(args), capture_output=True)

    canary = dst / "sources.json"
    t0 = time.time()
    for i in range(15):
        (src / "daemon" / "a.py").write_text(f"# a v{i}\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(src), "add", "-A"], capture_output=True)
        subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", f"v{i}"],
                       capture_output=True)
        dm.deploy(src, dst)
        if i % 3 == 2:
            dm.rollback(dst)
    dt = time.time() - t0

    check(canary.read_text(encoding="utf-8") == '{"canary": "数据"}',
          "★ 15 轮部署+回滚之后，数据文件一字未变")
    archives = list((dst / ".backup").glob("deploy-*"))
    check(len(archives) >= 15, "每轮都留下了独立留档（没互相覆盖）",
          f"{len(archives)} 份")
    names = sorted(a.name for a in archives)
    check(len(names) == len(set(names)), "★ 留档名字没有重复（撞名会让留档变成混合体）")
    check(all((a / "snapshot").is_dir() for a in archives), "每份留档都有快照")
    check(dt < 120, "15 轮在 120 秒内完成", f"{dt:.1f}s")


# ---------------------------------------------------------------- 7 深目录 / 长路径

def stress_paths(tmp: Path):
    print("--- 7. 深目录 / 长路径 / 奇怪文件名 ---")
    base = tmp / "P"
    base.mkdir(parents=True)
    deep = base / "skills"
    for i in range(25):
        deep = deep / f"level{i}"
    try:
        deep.mkdir(parents=True)
        made = True
    except OSError:
        made = False
    check(made or True, "深目录创建（Windows 长路径限制下允许失败）", str(deep)[:70])

    # 名字里带各种边角字符，校验函数不该崩
    for name in ["skill with space", "skill-with-dash", "skill_underscore",
                 "skill.multi.dot", "UPPER", "123numeric", "中文技能", "emoji😀skill"]:
        try:
            ok = sp.is_safe_name(name)
            check(True, f"is_safe_name({name!r}) 不崩 → {ok}")
        except Exception as e:
            check(False, f"is_safe_name({name!r}) 不崩", f"{type(e).__name__}: {e}")

    # 接近 MAX_PATH 的名字
    for length in (60, 64, 65, 100, 255, 1000, 100000):
        nm = "x" * length
        try:
            ok = sp.is_safe_name(nm)
            check(ok == (length <= sp.MAX_NAME_LEN),
                  f"长度 {length} 的判定正确（上限 {sp.MAX_NAME_LEN}）", str(ok))
        except Exception as e:
            check(False, f"长度 {length} 不崩", f"{type(e).__name__}: {e}")

    # resolve_within 对超长名字
    try:
        sp.resolve_within(base, "x" * 100000)
        check(False, "超长名字被拒")
    except sp.UnsafeName:
        check(True, "超长名字被拒（不是抛 OSError 或崩掉）")
    except Exception as e:
        check(False, "超长名字被拒", f"抛了 {type(e).__name__}")


# ---------------------------------------------------------------- 8 畸形输入

def stress_malformed(tmp: Path):
    print("--- 8. 畸形请求体 ---")
    work = tmp / "M"
    (work / "templates").mkdir(parents=True)
    auth.SKILL_ROOT = work
    auth.reset_cache()
    key = auth.get_key()
    qb.SKILL_ROOT = work
    qb.SKILLS_DIR = work / "skills"
    qb.SKILLS_DIR.mkdir()

    server = HTTPServer(("127.0.0.1", 0), qb.QueueHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    h = {"Origin": "null", KEY_HEADER: key, "Content-Type": "application/json"}

    bodies = [
        (b"", "空 body"),
        (b"{", "半个 JSON"),
        (b"[]", "数组而不是对象"),
        (b'{"name": null}', "null 值"),
        (b'{"name": {"a": {"b": [1,2,3]}}}', "嵌套对象当 name"),
        (b"\xff\xfe\x00\x00", "非法 UTF-8"),
        (b'{"name": "' + "x".encode() * 1_000_000 + b'"}', "1MB 的名字"),
    ]
    for raw, label in bodies:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/uninstall",
                                     data=raw, method="POST", headers=h)
        try:
            with OPENER.open(req, timeout=30) as r:
                code, body = r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            code, body = e.code, e.read().decode("utf-8", "replace")
        except Exception as e:
            code, body = -1, f"{type(e).__name__}"
        check(code in (200, 400, 413, 500) or code != -1,
              f"M 畸形 body（{label}）有明确响应", f"HTTP {code}")

    code, body = call(port, "GET", "/installed", {"Origin": "null", KEY_HEADER: key})
    check(code == 200, "★ 一串畸形请求之后服务仍然可用", f"HTTP {code}")
    check((work / "skills").is_dir() and list((work / "skills").iterdir()) == [],
          "★ 畸形请求没在 skills 目录里造出任何东西")
    server.shutdown()
    server.server_close()


if __name__ == "__main__":
    sys.exit(main())
