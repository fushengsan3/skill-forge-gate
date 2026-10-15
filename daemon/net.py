#!/usr/bin/env python3
"""外网出口的唯一真源 —— 代理怎么定、怎么用。

## 为什么要有这个文件（2026-10-15 之前不是这样的）

在这之前，某个本机代理地址被**硬编码在 6 个模块里**（具体值见 git 历史 ——
本文刻意不抄一遍，抄一遍等于又把它发出去一次，这一点 `tests/test_net.py`
有一条静态扫描在盯着），而且每处都自己 `ProxyHandler({...PROXY...})`。
这带来三个问题：

  1. **它把作者本机的代理塞进了公开仓库。** 任何人 clone 下来，
     所有外网请求都会被指向一个只在他机器上存在的端口 ——
     而在别人那里，这个端口后面什么都没有，于是每一个请求都失败，
     报错还长得像"网络不通"。
  2. **它连带写出了作者用的是什么代理。** 一个本地端口号本身就是信息。
  3. **`daemon/watchdog.py` 在 import 时就把 `os.environ` 改了。**
     于是"只是导入一下这个模块"也会改掉调用进程的代理设置 ——
     副作用发生在 import 阶段，是最难查的那一类。

现在改成一处解析、处处使用：

    SKILL_FORGE_PROXY 环境变量
        ↓ 没有
    templates/network-config.json 的 "proxy"
        ↓ 没有
    标准的 https_proxy / http_proxy
        ↓ 没有
    直连（不发任何代理）

默认（干净的 clone）落在最后一条：**直连**。需要代理的人自己配 ——
配法写在 安装部署说明.md §3，那里同时给了 no_proxy 这个逃生口的实测结论。

## 为什么配置放文件而不是只认环境变量

守护进程是**任务计划程序**拉起来的，继承的是登录环境 ——
你在终端里 `export` 的变量它看不到。这和翻译后端配置放文件是同一个理由
（见 daemon/translate_config.py 顶部那段）。
"""
import json
import os
import urllib.request
from pathlib import Path

SKILL_ROOT = Path.home() / ".claude" / "skills" / "skill-forge"
CONFIG_FILE = SKILL_ROOT / "templates" / "network-config.json"

# 显式覆盖用的环境变量。用独立的名字而不是直接读 https_proxy，
# 是为了让"只对这个工具生效"成为可能 —— 不必为了它改掉全局代理。
ENV_VAR = "SKILL_FORGE_PROXY"

DEFAULTS = {
    "proxy": "",       # 空 = 直连
    "no_proxy": "",    # 逗号分隔的主机列表，绕过代理（能绕过，不能换成别的地址）
}

_cache = {"loaded": False, "proxy": "", "no_proxy": ""}


def load() -> dict:
    """读配置。文件不存在或损坏时返回默认值 —— 绝不能因此让扫描挂掉。"""
    cfg = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                cfg.update(raw)
        except Exception:
            pass
    for key in ("proxy", "no_proxy"):
        if not isinstance(cfg.get(key), str):
            cfg[key] = DEFAULTS[key]
    return cfg


def save(updates: dict) -> dict:
    """合并写入。只接受已知字段。"""
    cfg = load()
    for key in ("proxy", "no_proxy"):
        if key in updates and isinstance(updates[key], str):
            cfg[key] = updates[key]
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    reset_cache()
    return cfg


def _from_env(*names) -> str:
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


def resolve_proxy() -> str:
    """按优先级解析出要用的代理；返回空串表示**直连**。

    顺序见文件顶部。结果缓存 —— 这些函数在热路径上（每个请求都问一次），
    而配置在一次进程生命周期里不会变。改了配置记得 reset_cache()。
    """
    if not _cache["loaded"]:
        cfg = load()
        _cache["proxy"] = (
            _from_env(ENV_VAR)                        # 1. 显式覆盖，最高优先
            or cfg.get("proxy", "").strip()           # 2. 本工具的配置文件
            or _from_env("https_proxy", "http_proxy")  # 3. 环境本来就有的
        )
        _cache["no_proxy"] = (
            cfg.get("no_proxy", "").strip()
            or _from_env("no_proxy", "NO_PROXY")
        )
        _cache["loaded"] = True
    return _cache["proxy"]


def resolve_no_proxy() -> str:
    resolve_proxy()
    return _cache["no_proxy"]


def reset_cache() -> None:
    """改过配置或环境变量后调用。测试里也用得上。"""
    _cache.update({"loaded": False, "proxy": "", "no_proxy": ""})


def proxy_handler():
    """有代理时返回显式 ProxyHandler，否则返回 None。

    为什么不能只靠环境变量：显式 handler 的优先级**高于**环境变量，
    想让 no_proxy 生效就必须自己把它交给 ProxyHandler ——
    urllib 的 proxy_bypass() 只认这一条路径（实测过，见 安装部署说明.md §3）。
    """
    proxy = resolve_proxy()
    if not proxy:
        return None
    return urllib.request.ProxyHandler({
        "https": proxy,
        "http": proxy,
        "no": resolve_no_proxy(),
    })


def build_opener():
    """按当前配置造一个 opener。

    没有代理时返回**默认 opener** —— 那样它就按 urllib 的常规行为走，
    该读环境变量读环境变量，而不是被我们锁死。
    """
    handler = proxy_handler()
    if handler is None:
        return urllib.request.build_opener()
    return urllib.request.build_opener(handler)


def subprocess_env(base: dict = None) -> dict:
    """给子进程（git 之类）准备环境变量。

    只在本进程真的解析出代理时才注入 —— 不再无条件改写。
    以前 installer 传给 git 的环境里永远带着那个硬编码地址，
    在别人机器上等于让 git 也去撞一个空端口。
    """
    env = dict(base if base is not None else os.environ)
    proxy = resolve_proxy()
    if proxy:
        env["https_proxy"] = proxy
        env["http_proxy"] = proxy
    no_proxy = resolve_no_proxy()
    if no_proxy:
        env["no_proxy"] = no_proxy
        env["NO_PROXY"] = no_proxy
    return env


if __name__ == "__main__":
    # 诊断入口：python -m daemon.net
    print(json.dumps({
        "proxy": resolve_proxy() or "(直连)",
        "no_proxy": resolve_no_proxy() or "(无)",
        "config_file": str(CONFIG_FILE),
        "config_exists": CONFIG_FILE.exists(),
        "env_var": ENV_VAR,
        "env_var_set": bool(os.environ.get(ENV_VAR)),
        "config": load(),
    }, ensure_ascii=False, indent=2))
