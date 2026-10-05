#!/usr/bin/env python3
"""
数据源拉取器 — 从 jeremylongshore、GitHub Topics、ClaudSkills 并行拉取 skill 数据
通过可乐云代理访问外网
"""
import json
import os
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

PROXY = "http://127.0.0.1:7897"
SKILL_ROOT = Path.home() / ".claude" / "skills" / "skill-forge"


def set_proxy():
    os.environ["https_proxy"] = PROXY
    os.environ["http_proxy"] = PROXY


def _github_headers() -> dict:
    """GitHub API 请求头。

    R6：配了 GitHub Token 就带上 —— 限流从 60 次/小时提到 5000 次/小时。
    没配也能正常工作，只是容易被限流（实测跑一遍测试就会耗光 60 次）。

    密钥来自 Windows 凭据管理器，不落盘、不进日志。
    """
    headers = {
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "skill-forge/1.0",
    }
    try:
        from daemon import credentials
        token = credentials.get_secret(credentials.GITHUB_TOKEN)
        if token:
            headers["Authorization"] = "Bearer " + token
    except Exception:
        # 凭据模块不可用（非 Windows / 缺 pywin32）不该让拉取失败 —— 退回匿名请求
        pass
    return headers


def api_get(url: str) -> dict:
    set_proxy()
    proxy_handler = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
    opener = urllib.request.build_opener(proxy_handler)
    req = urllib.request.Request(url, headers=_github_headers())
    try:
        with opener.open(req, timeout=30) as resp:
            return {"ok": True, "data": json.loads(resp.read().decode())}
    except Exception as e:
        return {"ok": False, "reason": str(e)}


def fetch_jeremylongshore() -> list:
    """从 jeremylongshore/claude-code-plugins-plus-skills 拉取热门插件"""
    url = "https://api.github.com/repos/jeremylongshore/claude-code-plugins-plus-skills/contents/plugins"
    result = api_get(url)
    if not result["ok"]:
        return []

    skills = []
    for item in result.get("data", [])[:100]:
        if item.get("type") == "dir":
            # 读取每个插件的 plugin.json（通过 raw.githubusercontent.com）
            plugin_url = f"https://raw.githubusercontent.com/jeremylongshore/claude-code-plugins-plus-skills/main/plugins/{item['name']}/plugin.json"
            try:
                set_proxy()
                proxy_handler = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
                opener = urllib.request.build_opener(proxy_handler)
                req = urllib.request.Request(plugin_url, headers={"User-Agent": "skill-forge/1.0"})
                with opener.open(req, timeout=15) as resp:
                    plugin = json.loads(resp.read().decode())
                    for skill in plugin.get("skills", []):
                        skill["source"] = "jeremylongshore"
                        skill["plugin"] = item["name"]
                        skills.append(skill)
            except Exception:
                continue

    return skills


def fetch_github_topic() -> list:
    """从 GitHub Topics 搜索热门 claude-code-skill"""
    url = "https://api.github.com/search/repositories?q=topic:claude-code-skill&sort=stars&order=desc&per_page=30"
    result = api_get(url)
    if not result["ok"]:
        return []

    skills = []
    for repo in result.get("data", {}).get("items", []):
        skills.append({
            "name": repo.get("name"),
            "full_name": repo.get("full_name"),
            "description": repo.get("description", ""),
            "stars": repo.get("stargazers_count", 0),
            "url": repo.get("html_url"),
            # R1：三个时间字段各是各的，**不要混用**（决策记录 §3.2 R1）——
            #   created_at  仓库创建时间   = "发布时间"
            #   pushed_at   最后一次代码推送 = "更新"
            #   updated_at  仓库元数据变动（改描述、涨 star 都算）—— 不等于代码更新
            # search 响应里三个都现成，多读两个是零成本；以前只读了 updated_at。
            "created_at": repo.get("created_at"),
            "pushed_at": repo.get("pushed_at"),
            "updated_at": repo.get("updated_at"),
            "source": "github-topic",
        })
    return skills


def fetch_claudskills() -> list:
    """从 ClaudSkills.com 拉取公开数据集"""
    url = "https://claudskills.com/data/skills.json"
    try:
        set_proxy()
        proxy_handler = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
        opener = urllib.request.build_opener(proxy_handler)
        req = urllib.request.Request(url, headers={"User-Agent": "skill-forge/1.0"})
        with opener.open(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            skills = data.get("skills", []) if isinstance(data, dict) else []

            # 取 top 50。
            #
            # 原先这里按 qualityScore / stars 排序 —— 但这两个字段在响应里
            # **根本不存在**（站点已移除），所以键恒为 0，排序是空操作，
            # 实际拿到的是**接口原始顺序的前 50 条**，而不是"最好的 50 条"。
            #
            # 改用真实存在的字段：featured 是站点自己的精选标记，拿它当首要信号。
            # 刻意**不**拿 security_grade 参与排序 —— 它的取值域没探明，
            # 当成序数用可能反而是错的（比如 'A' > 'B' 但 'F' 又该怎么算）。
            skills.sort(key=lambda s: 1 if s.get("featured") else 0, reverse=True)

            selected = skills[:50]
            for s in selected:
                s["source"] = "claudskills"
                # 从 source_url 提取 GitHub 仓库地址
                source_url = s.get("source_url", "")
                if source_url and "github.com" in source_url and not s.get("url"):
                    s["url"] = _extract_github_repo(source_url)
                    s["full_name"] = _extract_github_fullname(source_url)
                # 顺带保留两个真实存在的质量信号，供后续排序/筛选用
                s["featured"] = bool(s.get("featured"))
                s["security_grade"] = s.get("security_grade") or ""
            return selected
    except Exception:
        return []


def _extract_github_repo(source_url: str) -> str:
    """从 GitHub source_url 提取仓库根地址（例如 blob URL → repo URL）"""
    import re
    # https://github.com/owner/repo/blob/... → https://github.com/owner/repo
    m = re.match(r'(https://github\.com/[^/]+/[^/]+)', source_url)
    return m.group(1) if m else source_url


def _extract_github_fullname(source_url: str) -> str:
    """从 GitHub source_url 提取 owner/repo。

    **要剥掉 `.git` 后缀**：sources.json 里存的是 clone URL（`owner/repo.git`），
    而 GitHub 的 API 路径是 `/repos/owner/repo` —— 带上 `.git` 会 404。
    2026-10-05 实测：19 个已装 skill 里有 10 个因为这一条查不到上游 SHA，
    白白当成"判断不了更新"。
    """
    import re
    m = re.match(r'https://github\.com/([^/]+/[^/#?]+)', source_url)
    if not m:
        return ""
    return m.group(1)[:-4] if m.group(1).endswith(".git") else m.group(1)


def fetch_latest_shas(installed: dict, branch_default: str = "main") -> dict:
    """给**已安装**的 skill 查上游 HEAD 的 SHA，返回 {名字: sha}。

    ## 为什么只查已装的

    `watchdog.filter_skills` 只在"这个名字出现在 sources.json 里"时才用
    latest_sha 去比对。给几百个**没装**的 skill 也各查一次 API，等于每周白白
    烧掉几百次配额，而结果根本没人看。所以这里只遍历 install 过的那些 ——
    通常十几个。

    ## 之前为什么是坏的

    这个函数以前**不存在**。fetcher 从来不产出 latest_sha，于是
    `filter_skills` 里 `upstream = skill.get("latest_sha", "")` 恒为空串，
    那个 `if upstream and ...` 分支永远进不去。后果有两个：

      1. 面板上的"可更新"统计、"🔄 有更新"筛选、更新角标全是死的（恒为 0）
      2. 更糟：已装 skill 即使上游真有新提交，也会落进"SHA 相同 → 排除"
         被**静默丢掉**，用户从面板上根本看不到更新

    查不到就**不放进返回字典**（而不是塞个空串）—— 让调用方能区分
    "确认没更新"和"没查成"。
    """
    from concurrent.futures import ThreadPoolExecutor

    targets = []
    for name, info in (installed or {}).items():
        if not isinstance(info, dict) or info.get("self"):
            continue                      # 自己不查自己
        url = info.get("url") or ""
        full = info.get("full_name") or _extract_github_fullname(url)
        if not full or "/" not in full:
            continue
        targets.append((name, full, info.get("branch") or branch_default))

    def one(item):
        name, full, branch = item
        # 再剥一次 .git（full_name 字段可能是手写进 sources.json 的）
        if full.endswith(".git"):
            full = full[:-4]
        url = f"https://api.github.com/repos/{full}/commits/{branch}"
        result = api_get(url)
        if not result["ok"]:
            return name, ""
        sha = (result.get("data") or {}).get("sha", "")
        return name, sha

    out = {}
    if not targets:
        return out
    # 几个线程就够 —— 数量是"装过的 skill 数"，不是发现总数
    with ThreadPoolExecutor(max_workers=4) as ex:
        for name, sha in ex.map(one, targets):
            if sha:
                out[name] = sha
    return out


def fetch_external_sources() -> list:
    """从外部数据源拉取 skill（读取 templates/external_sources.json）"""
    import json as _json
    sources_file = SKILL_ROOT / "templates" / "external_sources.json"
    if not sources_file.exists():
        return []

    try:
        config = _json.loads(sources_file.read_text(encoding="utf-8"))
    except Exception:
        return []

    ext_sources = config.get("sources", [])
    if not ext_sources:
        return []

    skills = []
    for es in ext_sources:
        url = es.get("url", "")
        label = es.get("label", "")
        try:
            result = api_get(url)
            if not result["ok"]:
                continue
            data = result["data"]
            # 支持两种格式：GitHub API repo 对象 或 通用 skill 列表
            if isinstance(data, list):
                items = data
            elif isinstance(data, dict):
                items = data.get("items", data.get("skills", data.get("repos", [])))
            else:
                continue

            for item in items[:30]:  # 每个源最多 30 条
                skill = normalize_skill(item, source="external", label=label)
                if skill:
                    skills.append(skill)
        except Exception:
            continue

    return skills


def normalize_skill(raw: dict, source: str = "external", label: str = "") -> dict:
    """将不同来源的原始数据规范化为统一 skill 格式"""
    return {
        "name": raw.get("name") or raw.get("skill_name") or raw.get("repo") or "",
        "full_name": raw.get("full_name") or raw.get("repo") or raw.get("url") or "",
        "description": raw.get("description") or raw.get("desc") or "",
        "stars": raw.get("stars") or raw.get("stargazers_count") or raw.get("score") or 0,
        "url": raw.get("url") or raw.get("html_url") or raw.get("repo_url") or "",
        # R1：三个时间字段分开保留。
        # 这里原先是 `updated_at or pushed_at` —— 把"代码推送"和"元数据变动"
        # 混成了同一个值，正是决策记录要求区分的两个量，所以拆开。
        "created_at": raw.get("created_at") or "",
        "pushed_at": raw.get("pushed_at") or "",
        "updated_at": raw.get("updated_at") or "",
        "source": source,
        "source_label": label,
        "installs": raw.get("installs") or raw.get("downloads") or 0,
        "qualityScore": raw.get("qualityScore") or raw.get("quality_score") or None,
    }


def fetch_all_sources(search_query: str = None, top: int = None, include_external: bool = True) -> list:
    """
    并行拉取所有数据源，去重合并
    如果指定 search_query，则做关键词搜索
    如果指定 top，限制返回数量
    如果 include_external，包含外部数据源
    """
    from concurrent.futures import ThreadPoolExecutor

    if search_query:
        return search_skills(search_query, top or 10)

    all_skills = []

    fetchers = {
        "jeremylongshore": fetch_jeremylongshore,
        "github-topic": fetch_github_topic,
        "claudskills": fetch_claudskills,
    }
    if include_external:
        fetchers["external"] = fetch_external_sources

    with ThreadPoolExecutor(max_workers=len(fetchers)) as executor:
        futures = {executor.submit(fn): name for name, fn in fetchers.items()}
        for future in futures:
            try:
                result = future.result(timeout=60)
                all_skills.extend(result)
            except Exception:
                pass

    # 按 GitHub URL 去重
    seen = set()
    deduped = []
    for s in all_skills:
        key = s.get("url") or s.get("full_name") or s.get("name")
        if key not in seen:
            seen.add(key)
            deduped.append(s)

    # 按 stars 降序排序
    deduped.sort(key=lambda s: s.get("stars", 0), reverse=True)

    if top:
        deduped = deduped[:top]

    return deduped


def search_skills(query: str, top: int = 10) -> list:
    """关键词搜索 skill（用于用户需求快速通道）"""
    import urllib.parse
    url = f"https://api.github.com/search/repositories?q={urllib.parse.quote(query)}+topic:claude-code-skill&sort=stars&order=desc&per_page={top}"
    result = api_get(url)

    if not result["ok"]:
        return []

    skills = []
    for repo in result.get("data", {}).get("items", []):
        skills.append({
            "name": repo.get("name"),
            "full_name": repo.get("full_name"),
            "description": repo.get("description", ""),
            "stars": repo.get("stargazers_count", 0),
            "url": repo.get("html_url"),
            "source": "search",
            "match_score": 1.0,
        })
    return skills


if __name__ == "__main__":
    if "--search" in sys.argv:
        idx = sys.argv.index("--search")
        query = sys.argv[idx + 1] if idx + 1 < len(sys.argv) else ""
        top = int(sys.argv[idx + 3]) if "--top" in sys.argv and sys.argv.index("--top") + 1 < len(sys.argv) else 10
        skills = fetch_all_sources(search_query=query, top=top)
    else:
        skills = fetch_all_sources()

    print(json.dumps({"fetched_at": datetime.now().isoformat(), "total": len(skills), "skills": skills},
                     ensure_ascii=False, indent=2))
