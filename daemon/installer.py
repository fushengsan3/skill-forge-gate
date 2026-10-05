#!/usr/bin/env python3
"""
安装模块 — 从 install-queue.json 读取待安装 skill → clone → 注册到 sources.json
支持 GitHub 仓库（主流程）和 jeremylongshore 插件（子路径提取）
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

PROXY = "http://127.0.0.1:7897"
SKILL_ROOT = Path.home() / ".claude" / "skills"
FORGE_ROOT = Path.home() / ".claude" / "skills" / "skill-forge"
QUEUE_FILE = FORGE_ROOT / "templates" / "install-queue.json"
SOURCES_FILE = FORGE_ROOT / "sources.json"

# 本模块既能被 `from daemon.installer import ...` 用，也能被
# `python daemon/installer.py` 直接跑。后者的 sys.path[0] 是 daemon/，
# 看不见 `daemon` 这个包，所以补一次父目录。用 __file__ 定位而不是
# FORGE_ROOT —— 这样在仓库副本里跑也 import 到仓库自己的那份。
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from daemon import safe_paths


def log(msg: str):
    log_file = FORGE_ROOT / "daemon" / "watchdog.log"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] [installer] {msg}\n")


def _redact(s: str) -> str:
    """把 URL 里的凭据抹掉再往外带 —— 错误信息会进日志和 HTTP 响应。"""
    import re
    return re.sub(r"://[^/@\s]*@", "://***@", s or "")


def _tail(s: str, limit: int = 200) -> str:
    """取错误输出的最后一行有内容的。git 的报错重点在最后一行。"""
    lines = [ln.strip() for ln in _redact(s).splitlines() if ln.strip()]
    return lines[-1][:limit] if lines else ""


def _git_clone(url: str, dest: Path, branch: str = "main"):
    """通过代理 clone 仓库（优先 shallow clone）。返回 (是否成功, 失败原因)。

    **刻意不设 GIT_SSL_NO_VERIFY。**（2026-10-05 之前是设的。）

    关掉证书校验等于把"从 GitHub 拿到的内容"降级成"从网络路径上随便谁拿到的内容"，
    而这些东西会被装进 ~/.claude/skills/ 并且**被 Claude 当指令读**。
    传输层完整性是这条链上仅有的保证 —— 不能用"省事"换掉它。
    直连（不经代理）不关校验可以正常 clone，已实测。
    代理（可乐云 127.0.0.1:7897）当时没开，那条路径**尚未验证**：
    如果将来在代理下出现证书错误，正确做法是查代理的 CA，而不是把这行加回来。

    失败原因要往外带：以前证书错误和"仓库不存在"给的是同一句
    "（仓库不存在或网络不通）"，等于把可诊断性也一起关掉了。
    """
    env = os.environ.copy()
    env["https_proxy"] = PROXY
    env["http_proxy"] = PROXY
    # 显式清掉：万一是用户环境里本来就有的，也不能让它悄悄关掉校验
    env.pop("GIT_SSL_NO_VERIFY", None)

    def run(args, timeout):
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, env=env, check=True)

    try:
        run(["git", "clone", "--depth", "1", "--single-branch", "-b", branch,
             url, str(dest)], 120)
        return True, ""
    except subprocess.CalledProcessError as e:
        reason = _tail(e.stderr)
        # shallow clone 可能仅因 branch 不存在而失败，回退到全 clone
        try:
            if dest.exists():
                shutil.rmtree(dest)
            run(["git", "clone", url, str(dest)], 180)
            return True, ""
        except subprocess.CalledProcessError as e2:
            return False, _tail(e2.stderr) or reason
        except Exception as e2:
            return False, _tail(str(e2)) or reason
    except Exception as e:
        return False, _tail(str(e))


def _find_skill_files(repo_dir: Path) -> list:
    """在 clone 的仓库中查找 SKILL.md 文件"""
    patterns = ["SKILL.md", "skill.md", "CLAUDE.md", "claude.md"]
    results = []
    for pattern in patterns:
        for path in repo_dir.rglob(pattern):
            # 排除 node_modules, .git 等
            if any(p in path.parts for p in [".git", "node_modules", "__pycache__", ".venv"]):
                continue
            results.append(path)
    return results


def _install_skill_files(skill_name: str, skill_files: list, repo_dir: Path) -> Path:
    """
    安装 skill 文件到 ~/.claude/skills/<skill_name>/
    如果仓库只有一个 SKILL.md 在根目录，复制整个仓库
    如果有多个 skill 文件或嵌套目录，提取 skill 所在目录

    调用方**必须先**用 safe_paths.check_name 验过 skill_name（install_skill 里做了）。
    这里再拼一次是为了纵深：下面第一件事就是 rmtree。
    """
    dest_dir = safe_paths.resolve_within(SKILL_ROOT, skill_name)

    if len(skill_files) == 1 and skill_files[0].parent == repo_dir:
        # 单个 SKILL.md 在仓库根目录 → 复制整个目录（保留相关资源）
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        shutil.copytree(repo_dir, dest_dir, ignore=shutil.ignore_patterns(".git", ".github", "node_modules"))
    else:
        # 多个 skill 或嵌套结构 → 按目录分组安装
        if not dest_dir.exists():
            dest_dir.mkdir(parents=True)
        seen_dirs = set()
        for sf in skill_files:
            parent = sf.parent
            if parent in seen_dirs:
                continue
            seen_dirs.add(parent)
            # 复制 skill 文件及其同级资源到目标目录
            for item in parent.iterdir():
                if item.name.startswith(".git"):
                    continue
                src = item
                dst = dest_dir / item.name
                if src.is_dir():
                    if not dst.exists():
                        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git"))
                else:
                    shutil.copy2(src, dst)

    return dest_dir


def _get_latest_sha(repo_dir: Path) -> str:
    """获取仓库最新 commit SHA"""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _update_sources_json(skill_name: str, url: str, sha: str, source_type: str = "remote"):
    """注册 skill 到 sources.json"""
    sources = {}
    if SOURCES_FILE.exists():
        try:
            sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    sources[skill_name] = {
        "type": source_type,
        "url": url,
        "branch": "main",
        "subpath": "",
        "installed_sha": sha,
        "installed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "skill-forge",
    }
    SOURCES_FILE.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")


def install_skill(name: str, url: str, source: str = "", full_name: str = "") -> dict:
    """
    安装单个 skill
    返回 {"ok": true/false, "error": "...", "sha": "..."}

    name 来自安装队列（可由面板经 bridge 写入），是**不受信输入** ——
    它决定要 rmtree 哪个目录，所以第一件事就是验它。
    """
    # 验名必须在确定 URL、clone 之前：早失败，才不用白 clone 一遍
    try:
        safe_paths.check_name(name)
    except safe_paths.UnsafeName as e:
        err = f"拒绝安装：{e}"
        log(err)
        return {"ok": False, "error": err}

    # 确定 clone URL
    if url and "github.com" in url:
        clone_url = url
        if not clone_url.endswith(".git"):
            clone_url = clone_url.rstrip("/")
            if "/tree/" in clone_url:
                # GitHub 子目录 URL → 取仓库根
                clone_url = clone_url.split("/tree/")[0] + ".git"
            elif "/blob/" in clone_url:
                clone_url = clone_url.split("/blob/")[0] + ".git"
            else:
                if not clone_url.endswith(".git"):
                    clone_url += ".git"
    elif full_name and "/" in full_name:
        clone_url = f"https://github.com/{full_name}.git"
    else:
        err = f"无法确定 {name} 的仓库地址 (url={url[:80] if url else '无'}, full_name={full_name if full_name else '无'})"
        log(err)
        return {"ok": False, "error": err}

    log(f"安装 {name} ← {clone_url}")

    with tempfile.TemporaryDirectory(prefix="sf-install-") as tmpdir:
        tmp = Path(tmpdir)
        repo_dest = tmp / "repo"

        cloned, detail = _git_clone(clone_url, repo_dest)
        if not cloned:
            # 带上 git 自己的最后一句报错 —— 不猜"网络不通"，让用户看得见真因
            err = f"Clone 失败: {clone_url}" + (f" —— {detail}" if detail else "")
            log(err)
            return {"ok": False, "error": err}

        skill_files = _find_skill_files(repo_dest)
        if not skill_files:
            return {"ok": False, "error": f"仓库中未找到 SKILL.md 或 CLAUDE.md"}

        # 装了才算数，所以验在装之前。
        # 这是原先整条安装链上唯一缺的那道闸：面板「立即安装」直通 clone，
        # verify/ 的 L1–L5 根本没被 import 过。详见 daemon/precheck.py 顶部。
        try:
            from daemon import precheck
            verdict = precheck.verify_repo(repo_dest, clone_url, skills_dir=SKILL_ROOT)
        except Exception as e:
            # 预检自己崩了 = 没验成。按"不放行"处理，宁可装不上也不要静默装进去。
            err = f"安装前预检无法执行，已中止：{type(e).__name__}: {e}"
            log(err)
            return {"ok": False, "error": err}

        if not verdict["ok"]:
            err = f"预检未通过，已拒绝安装：{verdict['summary']}"
            log(err)
            return {"ok": False, "error": err, "verify": verdict}

        log(f"预检通过：{verdict['summary']}")

        sha = _get_latest_sha(repo_dest)
        dest = _install_skill_files(name, skill_files, repo_dest)

        # 注册
        _update_sources_json(name, clone_url, sha)

        # 如果仓库中有多个 skill 目录，尝试发现子 skill（如 claude-skills-research 的 monorepo）
        extra_skills = _discover_sub_skills(repo_dest, name, clone_url, sha)

        log(f"安装完成: {name} → {dest}  (SHA: {sha[:8] if sha else '?'}, {len(skill_files)} 文件, +{len(extra_skills)} 子skill)")

        return {
            "ok": True,
            "sha": sha[:8] if sha else "",
            "path": str(dest),
            "files": len(skill_files),
            "extra_skills": extra_skills,
            # 把预检结论带回去 —— 面板据此显示真实结果，
            # 而不是装之前就瞎标一个"已审核"。
            "trust_level": "verified",
            "verify": verdict,
        }


def _discover_sub_skills(repo_dir: Path, primary_name: str, clone_url: str = "", sha: str = "") -> list:
    """
    发现仓库中的其他 skill（monorepo 如 claude-skills-research）
    如果仓库包含多个子目录且各有 SKILL.md/CLAUDE.md，自动安装并注册这些子 skill
    """
    extra = []
    skipped = set()
    # 匹配所有常见的 skill 文件名
    patterns = ["SKILL.md", "skill.md", "CLAUDE.md", "claude.md"]
    for pattern in patterns:
        for skill_file in repo_dir.rglob(pattern):
            parent = skill_file.parent
            if parent == repo_dir:
                continue
            skill_name = parent.name
            if skill_name == primary_name:
                continue
            if skill_name in extra or skill_name in skipped:
                continue

            # 这里的名字来自**克隆下来的仓库目录名**，理论上单个路径分量不会带分隔符，
            # 但它终究是外部内容。同一个仓库里通常是几十个文件指向同一个坏目录名，
            # 所以失败要记账（skipped），不然日志会被同一句话刷屏。
            try:
                dest_dir = safe_paths.resolve_within(SKILL_ROOT, skill_name)
            except safe_paths.UnsafeName as e:
                skipped.add(skill_name)
                log(f"  跳过一个子 skill {skill_name!r}：{e}")
                continue

            # 安装额外发现的 skill
            try:
                if dest_dir.exists():
                    shutil.rmtree(dest_dir)
                shutil.copytree(parent, dest_dir, ignore=shutil.ignore_patterns(".git"))
                extra.append(skill_name)
                # 注册子 skill 到 sources.json
                if clone_url and sha:
                    _update_sources_json(skill_name, clone_url, sha)
                log(f"  发现子 skill: {skill_name}")
            except Exception:
                pass
    return extra


def process_install_queue() -> dict:
    """
    处理安装队列：遍历 pending → 逐个安装 → 移到 history
    返回 {"processed": N, "success": N, "failed": N, "results": [...]}
    """
    if not QUEUE_FILE.exists():
        return {"processed": 0, "success": 0, "failed": 0, "results": []}

    try:
        queue = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"processed": 0, "success": 0, "failed": 0, "results": []}

    pending = queue.get("pending", [])
    if not pending:
        return {"processed": 0, "success": 0, "failed": 0, "results": []}

    results = []
    success = 0
    failed = 0

    for item in pending:
        name = item.get("name") or item.get("skill_name") or ""
        url = item.get("url") or item.get("repo_url") or ""
        source = item.get("source", "")
        full_name = item.get("full_name") or item.get("repo") or ""

        result = install_skill(name, url, source, full_name)
        result["name"] = name
        results.append(result)

        if result["ok"]:
            success += 1
            queue.setdefault("history", []).append({
                "name": name,
                "url": url,
                "sha": result.get("sha", ""),
                "installed_at": datetime.now().isoformat(),
            })
        else:
            failed += 1

    # 清空 pending，保存
    queue["pending"] = []
    queue["last_processed"] = datetime.now().isoformat()
    try:
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        QUEUE_FILE.write_text(json.dumps(queue, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass

    log(f"队列处理完成: {success} 成功, {failed} 失败")
    return {
        "processed": len(pending),
        "success": success,
        "failed": failed,
        "results": results,
    }


if __name__ == "__main__":
    import sys
    if "--queue" in sys.argv:
        result = process_install_queue()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    elif len(sys.argv) >= 3:
        name, url = sys.argv[1], sys.argv[2]
        result = install_skill(name, url)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("用法: python installer.py <name> <url>  或  python installer.py --queue")
