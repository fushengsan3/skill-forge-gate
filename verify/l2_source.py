#!/usr/bin/env python3
"""
L2 来源校验 — 检查 GitHub URL 可达性、仓库状态、维护迹象
输出 JSON 到 stdout
需要可乐云代理访问外网
"""
import json
import sys
import os
import re
import urllib.request

PROXY = "http://127.0.0.1:7897"
GITHUB_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)")


def set_proxy():
    os.environ["https_proxy"] = PROXY
    os.environ["http_proxy"] = PROXY


def api_get(url: str) -> dict:
    """通过可乐云代理调用 GitHub API。

    ⚠️ 请求头走 `daemon.fetcher.github_headers()` —— **不在这里另写一份**。
    配了 GitHub Token 就带上（限流 60/小时 → 5000/小时），没配就匿名。

    以前这里是一份**匿名硬编码**的头，后果实测到了（2026-10-07）：
    L2 在一台正常使用的机器上**永远被限流跳过**（`403 rate limit exceeded`），
    而"有层跳过 → `partial`"是 `_trust_level()` 的规则 ——
    于是**每个 skill 装完都是 partial**，那个字段彻底失去区分度。
    把 token 接上，L2 才从"装饰"变回"检查"。
    """
    set_proxy()
    proxy_handler = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
    opener = urllib.request.build_opener(proxy_handler)
    try:
        from daemon.fetcher import github_headers
        headers = github_headers()
    except Exception:
        # 取凭据失败就退回匿名 —— 与以前行为一致，降级不该让这一层崩掉
        headers = {"Accept": "application/vnd.github.v3+json", "User-Agent": "skill-forge"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=15) as resp:
            return {"ok": True, "data": json.loads(resp.read().decode())}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "reason": str(e)}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def parse_github_url(url: str) -> tuple:
    """从 URL 提取 owner/repo"""
    m = GITHUB_URL_RE.search(url)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def check_source(url: str) -> dict:
    """检查一个 GitHub skill 来源的可靠性。

    ⚠️ 2026-10-07：签名里原先有个 `token` 形参，**从不被读取**，已删除。
    它本意是给 GitHub API 提额度（60/小时 → 5000/小时），但没有任何调用方传它，
    函数体也一次都没引用 —— 真正用 token 的是 `daemon/fetcher.py::_github_headers`。

    做过安全性评估：无漏洞、无系统信息泄露。**L2 恒走匿名请求**是既成事实，
    不是删掉这个参数造成的 —— 限流时走的是"降级为 UNKNOWN → SKIPPED"那条路
    （见下面 `if not result["ok"]` 那段），而内容安全由 L3 / L5 独立承担，
    它们不依赖 GitHub 配额。
    """
    owner, repo = parse_github_url(url)
    if not owner or not repo:
        return {"verdict": "REJECT", "reason": f"无法解析 GitHub URL: {url}"}

    checks = []

    # 1. 仓库是否存在
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    result = api_get(api_url)

    if not result["ok"]:
        # **"问不到 GitHub" ≠ "这个仓库危险"。**
        #
        # 原先这里返回 REJECT，而预检把 REJECT 当"拒绝安装" —— 于是 GitHub API
        # 一限流（匿名只有 60 次/小时）或断网，**所有安装都会被拒**，理由还是
        # 一句"仓库不可达"，而调用方刚刚才从这个仓库 clone 成功过。
        #
        # 所以单列一个 UNKNOWN：核查没做成，不是核查没通过。
        # 预检对**外部层**的 UNKNOWN 按"跳过"处理（见 daemon/precheck.py）。
        return {"verdict": "UNKNOWN",
                "reason": f"无法向 GitHub 核实来源：{result.get('reason', 'unknown')}"}

    repo_data = result["data"]
    repo_full_name = repo_data.get("full_name", f"{owner}/{repo}")

    # 2. 是否归档
    if repo_data.get("archived"):
        checks.append({"level": "red", "msg": f"{repo_full_name} 已归档，不再维护"})

    # 3. 是否 fork（非原始仓库）
    if repo_data.get("fork"):
        checks.append({"level": "yellow", "msg": f"{repo_full_name} 是一个 fork"})

    # 4. Star 数
    stars = repo_data.get("stargazers_count", 0)
    if stars < 10:
        checks.append({"level": "yellow", "msg": f"Star 数较少 ({stars})"})
    elif stars >= 100:
        checks.append({"level": "green", "msg": f"Star 数: {stars}"})

    # 5. 最近更新
    updated = repo_data.get("updated_at", "")
    checks.append({"level": "blue", "msg": f"最近更新: {updated}", "updated_at": updated})

    # 6. 开源协议
    license_info = repo_data.get("license")
    if license_info:
        checks.append({"level": "blue", "msg": f"许可证: {license_info.get('spdx_id', 'unknown')}"})
    else:
        checks.append({"level": "yellow", "msg": "未声明开源许可证"})

    # 判决
    reds = [c for c in checks if c["level"] == "red"]
    yellows = [c for c in checks if c["level"] == "yellow"]

    if reds:
        return {"verdict": "REJECT", "repo": repo_full_name, "stars": stars, "checks": checks}
    if yellows:
        return {"verdict": "REVIEW", "repo": repo_full_name, "stars": stars, "checks": checks}
    return {"verdict": "PASS", "repo": repo_full_name, "stars": stars, "checks": checks}


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: l2-source.py <github_url>"}, ensure_ascii=False))
        sys.exit(2)
    report = check_source(sys.argv[1])
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report["verdict"] == "PASS" else 1)


if __name__ == "__main__":
    main()
