#!/usr/bin/env python3
"""L5 静态审计策略的读取端 —— 读 `sandbox/audit-policy.yaml`。

## 为什么会有这个模块

那份 yaml 在 2026-10-05 的审计里被记为「**没有任何代码读它**」（#15）。
当时的它描述的是一套 syscall 级审计，而 L5 **从不执行 skill 里的脚本** ——
所以那一段不只是没人读，是**根本读不了**。现在文件被改写成只描述
L5 真能做的事（见文件顶部那段说明），这个模块负责读它。

它是 L3 与 L5 共用的**唯一一份**规则源：以前 L3 的「网络域名」「文件写入路径」
是硬编码正则，而策略文件另有一份名单，两份必然走样。现在敏感路径来自这里。

## 读不出来时怎么办（fail-closed 的那一半）

**绝不能退化成"没有策略"。** 那等于把一条检查悄悄关掉，而报告照样说
"未发现危险或可疑模式" —— 这个项目已经在同一个坑里摔过（`except: return {}`）。

所以 `load()` 返回 `(policy, error)`：

    error == ""  → 读成功，policy 可用
    error != ""  → **这次没查成**，调用方必须把这句话如实写进报告

注意它**不**把整个判定打成 REVIEW：缺 pyyaml 是**这台机器**的缺件，
不是被审 skill 的问题 —— 与 L5 缺 Docker 时判 SKIPPED 同一个道理
（跳过要标注，但不因此拒绝别人的 skill）。

依赖：`pyyaml`。装在 `安装部署说明.md` 的前置依赖里。
"""
import os
from pathlib import Path

POLICY_FILE = Path(__file__).resolve().parent.parent / "sandbox" / "audit-policy.yaml"

# 归一化用的最小集：路径比对时统一成小写 + 正斜杠 + 去掉结尾斜杠
_cache = {"mtime": None, "policy": None, "error": None}


def _normalize_path(p: str) -> str:
    p = (p or "").strip().replace("\\", "/").lower()
    if p.startswith("~") and not p.startswith("~/"):
        p = "~/" + p[1:].lstrip("/")
    return p.rstrip("/")


def _normalize_host(h: str) -> str:
    h = (h or "").strip().lower()
    if h.startswith("["):
        # IPv6 字面量：`[::1]:443` → `::1`。**取完就返回** ——
        # 下面那句按冒号切分会把 IPv6 地址本身切成空串。
        # （第一版就是先剥括号再切冒号，`[::1]:443` 变成 ""，测试当场抓到。）
        return h[1:].split("]", 1)[0]
    if ":" in h:                     # example.com:443
        h = h.split(":")[0]
    return h.rstrip(".")


def _extract(policy: dict):
    """从已解析的 yaml 里取出两个列表。形状不对就当读失败 —— 不要猜。"""
    rules = (policy or {}).get("audit_rules") or {}
    paths = ((rules.get("files") or {}).get("sensitive_paths")) or []
    targets = ((rules.get("network") or {}).get("allowed_targets")) or []
    if not isinstance(paths, list) or not isinstance(targets, list):
        raise ValueError("audit_rules.files.sensitive_paths / network.allowed_targets 不是列表")
    return ([_normalize_path(p) for p in paths if isinstance(p, str) and p.strip()],
            [_normalize_host(t) for t in targets if isinstance(t, str) and t.strip()])


def load():
    """→ ({"sensitive_paths": [...], "allowed_targets": [...]}, error)

    `error` 非空 = 这次没读成。调用方**必须**把它写进报告。
    """
    if not POLICY_FILE.exists():
        return {"sensitive_paths": [], "allowed_targets": []}, f"找不到策略文件：{POLICY_FILE}"
    try:
        mtime = os.path.getmtime(POLICY_FILE)
    except OSError as e:
        return {"sensitive_paths": [], "allowed_targets": []}, f"读不到策略文件的属性：{type(e).__name__}"

    if _cache["mtime"] == mtime and _cache["policy"] is not None:
        return _cache["policy"], _cache["error"]

    try:
        import yaml
    except ImportError:
        # 不静默：这个"跳过"要一路走到报告里
        return ({"sensitive_paths": [], "allowed_targets": []},
                "策略未生效：没装 pyyaml（见 安装部署说明.md 的前置依赖）")

    try:
        raw = yaml.safe_load(POLICY_FILE.read_text(encoding="utf-8"))
        paths, targets = _extract(raw)
    except Exception as e:
        return ({"sensitive_paths": [], "allowed_targets": []},
                f"策略未生效：解析失败（{type(e).__name__}: {e}）")

    if not paths or not targets:
        # 空列表几乎一定是文件被改坏了 —— 而"空 = 没有要盯的"是这个模块
        # 最不能有的含义。宁可报错。
        return ({"sensitive_paths": [], "allowed_targets": []},
                "策略未生效：sensitive_paths 或 allowed_targets 是空的（文件被改坏了？）")

    policy = {"sensitive_paths": paths, "allowed_targets": targets}
    _cache.update(mtime=mtime, policy=policy, error="")
    return policy, ""


# ---------------------------------------------------------------- 判定

def find_sensitive_paths(text: str, policy: dict = None) -> list:
    """文本里出现了哪些敏感路径。返回命中列表（可能为空）。

    **纯文本匹配**：不做路径解析、不管它是不是真的会被访问 ——
    这层的用途是"值不值得人看一眼"，不是"证明它要偷东西"。
    """
    if policy is None:
        policy, _ = load()
    blob = (text or "").replace("\\", "/").lower()
    hits = []
    for p in policy.get("sensitive_paths") or []:
        if p and p in blob:
            hits.append(p)
    return hits


def host_of(url: str) -> str:
    """从 URL / 主机串里取出归一化的主机名。取不出来返回 ""。"""
    s = (url or "").strip()
    if not s:
        return ""
    if "://" in s:
        s = s.split("://", 1)[1]
    s = s.split("/", 1)[0].split("?", 1)[0]
    if "@" in s:
        s = s.rsplit("@", 1)[1]
    return _normalize_host(s)


def is_allowed_host(host: str, policy: dict = None) -> bool:
    """这个主机在不在白名单里。子域算通过（`api.github.com` 匹配 `github.com`）。"""
    if policy is None:
        policy, _ = load()
    h = _normalize_host(host)
    if not h:
        return False
    for t in policy.get("allowed_targets") or []:
        if h == t or h.endswith("." + t):
            return True
    return False


if __name__ == "__main__":
    import json
    pol, err = load()
    print(json.dumps({"policy_file": str(POLICY_FILE), "error": err,
                      "sensitive_paths": pol["sensitive_paths"],
                      "allowed_targets": pol["allowed_targets"]},
                     ensure_ascii=False, indent=2))
