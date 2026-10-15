#!/usr/bin/env python3
"""代理出口（daemon/net.py）的解析顺序与"不再硬编码"的回归测试。

## 这个文件钉的是什么

2026-10-15 之前，代理地址被**硬编码在 6 个模块 + 4 个脚本里**（共 14 处），
而这是个公开仓库 —— 别人 clone 下来，所有外网请求都会被指向一个
只存在于作者机器上的端口，报错还长得像"网络不通"。

改成一处解析（daemon/net.py）之后，要钉住两件事：

  1. **解析顺序**：SKILL_FORGE_PROXY → templates/network-config.json →
     标准 https_proxy/http_proxy → 直连。顺序错了会让"我配了却不生效"
     或者反过来"环境里有个旧值，把配置盖掉了"，都很难查。
  2. **仓库里不许再出现那个地址。** 这是本次改动的**回归钉** ——
     它防的不是逻辑错，而是"以后有人图省事又写死一次"。

用法：
    python tests/test_net.py
"""
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from daemon import net  # noqa: E402

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


def finish():
    failed = [l for l, ok in results if not ok]
    print()
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    for l in failed:
        print("  ✗ " + l)
    print("RESULT: " + ("FAIL" if failed else "PASS"))
    return 1 if failed else 0


# 那个地址本身**不能以字面量出现在本文件里** —— 否则下面的静态扫描会扫到它自己。
# 两个具体地址都不许出现：前者是作者本机那个，后者是早先换上去的示例值。
# **都从片段拼出来** —— 本文件自己也不能含这个字面量，否则扫描会扫到它自己。
HARDCODED = ["127.0.0.1:" + "78" + "97", "127.0.0.1:" + "78" + "90"]


class Env:
    """临时改环境变量 + 把配置文件指到临时路径，退出时全部还原。"""

    def __init__(self, tmp: Path):
        self.tmp = tmp
        self.cfg = tmp / "network-config.json"

    def __enter__(self):
        self._saved = {k: os.environ.get(k)
                       for k in (net.ENV_VAR, "https_proxy", "http_proxy",
                                 "no_proxy", "NO_PROXY", "HTTPS_PROXY")}
        for k in self._saved:
            os.environ.pop(k, None)
        self._saved_cfg = net.CONFIG_FILE
        net.CONFIG_FILE = self.cfg
        net.reset_cache()
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        net.CONFIG_FILE = self._saved_cfg
        net.reset_cache()
        return False


def main():
    print("=" * 60)
    print("代理出口测试（daemon/net.py）")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="sf-net-") as td:
        tmp = Path(td)
        with Env(tmp):
            # ---- 1. 默认直连 -----------------------------------------
            print("\n--- 1. 什么都不配 → 直连 ---")
            check(net.resolve_proxy() == "", "默认解析为空（直连）", repr(net.resolve_proxy()))
            check(net.proxy_handler() is None, "没有代理时不返回 ProxyHandler")
            check(net.build_opener() is not None, "没有代理时仍能造出 opener（默认 opener）")

            # ---- 2. 配置文件 -----------------------------------------
            print("\n--- 2. 配置文件 ---")
            net.save({"proxy": "http://127.0.0.1:1111", "no_proxy": "a.com,b.com"})
            check(net.resolve_proxy() == "http://127.0.0.1:1111",
                  "配置文件里的 proxy 生效", net.resolve_proxy())
            check(net.resolve_no_proxy() == "a.com,b.com", "no_proxy 也读得到")
            check(net.proxy_handler() is not None, "有代理时返回 ProxyHandler")
            cfg = json.loads(net.CONFIG_FILE.read_text(encoding="utf-8"))
            check(set(cfg) == {"proxy", "no_proxy"}, "配置文件只写已知字段", str(list(cfg)))

            # ---- 3. 环境变量优先于配置文件 ----------------------------
            print("\n--- 3. 优先级：SKILL_FORGE_PROXY > 配置文件 > https_proxy ---")
            os.environ["https_proxy"] = "http://127.0.0.1:2222"
            net.reset_cache()
            check(net.resolve_proxy() == "http://127.0.0.1:1111",
                  "配置文件盖过标准的 https_proxy", net.resolve_proxy())

            os.environ[net.ENV_VAR] = "http://127.0.0.1:3333"
            net.reset_cache()
            check(net.resolve_proxy() == "http://127.0.0.1:3333",
                  "SKILL_FORGE_PROXY 盖过配置文件", net.resolve_proxy())

            # ---- 4. 没有配置文件时回落到标准环境变量 ------------------
            print("\n--- 4. 没有配置文件 → 回落 https_proxy ---")
            net.CONFIG_FILE = tmp / "does-not-exist.json"
            net.reset_cache()
            check(net.resolve_proxy() == "http://127.0.0.1:3333",
                  "先取 SKILL_FORGE_PROXY", net.resolve_proxy())
            os.environ.pop(net.ENV_VAR, None)
            net.reset_cache()
            check(net.resolve_proxy() == "http://127.0.0.1:2222",
                  "没有 SKILL_FORGE_PROXY 时用标准 https_proxy", net.resolve_proxy())
            os.environ.pop("https_proxy", None)
            net.reset_cache()
            check(net.resolve_proxy() == "", "都没有 → 直连", net.resolve_proxy())

            # ---- 5. subprocess_env ------------------------------------
            print("\n--- 5. subprocess_env（给 git 等子进程）---")
            net.CONFIG_FILE = tmp / "network-config.json"
            net.save({"proxy": "http://127.0.0.1:1111", "no_proxy": "x.com"})
            env = net.subprocess_env({"PATH": "/usr/bin"})
            check(env.get("https_proxy") == "http://127.0.0.1:1111",
                  "有代理时注入 https_proxy")
            check(env.get("no_proxy") == "x.com" and env.get("NO_PROXY") == "x.com",
                  "no_proxy 两种拼法都注入（程序对大小写的认法不一致）")
            check(env.get("PATH") == "/usr/bin" and "PATH" in env,
                  "原有变量不被丢掉")
            net.save({"proxy": "", "no_proxy": ""})
            env2 = net.subprocess_env({"PATH": "/usr/bin"})
            check("https_proxy" not in env2 and "http_proxy" not in env2,
                  "★ 没配代理时**不注入**（以前是无条件写死一个地址）")

    # ---- 6. 回归钉：仓库里不许再出现具体代理地址 ----------------------
    print("\n--- 6. ★ 任何文件里都不许出现具体代理地址（本次改动的回归钉）---")
    files = subprocess.run(["git", "ls-files", "-z"], cwd=str(ROOT),
                           capture_output=True).stdout.split(b"\0")
    offenders = []
    for raw in files:
        if not raw:
            continue
        rel = raw.decode("utf-8")
        p = ROOT / rel
        if not p.is_file():
            continue
        try:
            body = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for needle in HARDCODED:
            if needle in body:
                offenders.append(f"{rel}（含 {needle}）")
                break
    # 文档**同样**不许有具体值 —— 那正是这一条收紧的原因：
    # 文档里给一个具体地址，读者会当默认值照抄，然后去撞一个不存在的端口。
    # 要举例就用 `<你的代理地址>` 这种占位符。
    check(not offenders,
          "被跟踪的文件里都没有具体代理地址（文档也一律用占位符）",
          "仍在：" + "; ".join(offenders) if offenders else "")

    return finish()


if __name__ == "__main__":
    sys.exit(main())
