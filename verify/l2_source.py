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
from pathlib import Path

PROXY = "http://127.0.0.1:7897"
GITHUB_URL_RE = re.compile(r"github\.com/([^/]+)/([^/]+)")


def set_proxy():
    os.environ["https_proxy"] = PROXY
    os.environ["http_proxy"] = PROXY


def api_get(url: str) -> dict:
    """通过可乐云代理调用 GitHub API"""
    set_proxy()
    proxy_handler = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github.v3+json", "User-Agent": "skill-forge"})
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


def check_source(url: str, token: str = None) -> dict:
    """检查一个 GitHub skill 来源的可靠性"""
    owner, repo = parse_github_url(url)
    if not owner or not repo:
        return {"verdict": "REJECT", "reason": f"无法解析 GitHub URL: {url}"}

    checks = []

    # 1. 仓库是否存在
    api_url = f"https://api.github.com/repos/{owner}/{repo}"
    result = api_get(api_url)

    if not result["ok"]:
        return {"verdict": "REJECT", "reason": f"仓库不可达: {result.get('reason', 'unknown')}"}

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
