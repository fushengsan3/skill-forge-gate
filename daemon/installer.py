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
# ⚠️ `precheck` 必须在**模块级**导入。
#
# 它原先只在 `install_skill()` 内部延迟导入（那里的注释解释的是"别为了 import
# 一下 installer 就把 verify/ 拉起来"）。但 `_discover_sub_skills()` 是另一个函数，
# 那里没这个名字 —— 2026-10-05 加子 skill 门控时忘了，于是**每个子 skill 都会
# 撞 NameError**，然后被新加的 `except Exception` 吞成"预检没能跑起来 → 跳过不装"。
#
# 表现是"一个子 skill 都装不上"，而日志里只有一行"预检没能跑起来"。
# （tests/test_precheck.py 第 8 节当场抓到了它。）
#
# 模块级导入**不会**拉起 verify/：precheck 自己的模块级只有 stdlib，
# `verify.*` 是它在 `verify_repo()` 里延迟导入的。
from daemon import precheck


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
        # errors="replace" 不是可有可无的：`text=True` 不带它时，子进程输出里只要
        # 有一个非 UTF-8 字节（git 在中文 Windows 上的报错就是 GBK），解码线程就会
        # 抛 UnicodeDecodeError —— 而 subprocess **不把它抛给你**，它让 stderr 变成
        # None、returncode 变成一个错的 1。于是一次**成功**的 clone 会因为这行
        # check=True 变成 CalledProcessError，报错还是"仓库不存在或网络不通"。
        # 2026-10-06 实测确认过这个失败形状。
        return subprocess.run(args, capture_output=True, text=True, errors="replace",
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
        # 多个 SKILL.md。**这里只装根目录那一个 skill** —— 嵌套的交给
        # `_discover_sub_skills()` 各自单独装成顶层 skill。
        #
        # ⚠️ 这里原先把**每个**父目录下的所有文件都平铺进 dest_dir，于是：
        #   · 嵌套的 `sub/SKILL.md` 会覆盖根目录的 `SKILL.md`（后写者赢）；
        #   · 子目录里的其它文件也被摊到主 skill 的根上。
        # 而 `_discover_sub_skills()` 紧接着又会把每个子 skill **单独**装一遍 ——
        # 所以那次覆盖是**纯粹的数据丢失，一点收益都没有**。
        # （2026-10-06 冻结树复核 #6 的证伪者在范围外实测到：装完一个 monorepo，
        #   主 SKILL.md 的内容变成了子目录 `zzz-pwn` 的。）
        parents = {sf.parent for sf in skill_files}
        if repo_dir in parents:
            parent = repo_dir
        else:
            # 根目录没有 SKILL.md —— 这个仓库的"主 skill"只能从嵌套里挑一个。
            # 挑**最浅**的那个，并且只装它一个；其余同样走子 skill 那条路。
            parent = min(parents, key=lambda p: (len(p.relative_to(repo_dir).parts), str(p)))
            log(f"仓库根目录没有 SKILL.md，改装最浅的一个：{parent.relative_to(repo_dir)}")

        if dest_dir.exists():
            # 与单 skill 分支保持一致：重装就是重装，不留上一次的残骸。
            shutil.rmtree(dest_dir)
        dest_dir.mkdir(parents=True)
        # 本身就是一个 skill 的子目录 —— 它们归 `_discover_sub_skills()` 管。
        skill_dirs = parents - {repo_dir}
        for item in parent.iterdir():
            if item.name.startswith(".git"):
                continue
            # ⚠️ 必须跳过：这些子目录在 `_discover_sub_skills()` 那条路上**要过预检门控**，
            # 而在这里照抄进去就等于**绕过那道门** ——
            # 一个 L3 判 REJECT 的子 skill 会被拒绝装成顶层 skill，
            # 文件却照样躺在主 skill 目录里，Claude 加载主 skill 时就把它一起读了。
            # （2026-10-06 实测：bad-sub 的 SKILL.md 含 `rm -rf /`，
            #   extra_skills 里没有它，但 monorepo/bad-sub/SKILL.md 确实在磁盘上 ——
            #   门控只拦住了"装成顶层"，没拦住"被抄进父 skill"。）
            if item.is_dir() and item in skill_dirs:
                continue
            dst = dest_dir / item.name
            if item.is_dir():
                shutil.copytree(item, dst, ignore=shutil.ignore_patterns(".git"))
            else:
                shutil.copy2(item, dst)

    return dest_dir


def _get_latest_sha(repo_dir: Path) -> str:
    """获取仓库最新 commit SHA"""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "HEAD"],
            capture_output=True, text=True, errors="replace", timeout=10
        )
        # 显式挡 None：解码失败时 stdout 会是 None（见上面 run() 那条注释），
        # 而这里外面是 `except Exception: return ""` —— 那会把"读不出来"和
        # "这个仓库没有 HEAD"混成同一个空字符串，静默写进 sources.json。
        if result.stdout is None:
            return ""
        return result.stdout.strip()
    except Exception:
        return ""


def _update_sources_json(skill_name: str, url: str, sha: str,
                         trust_level: str = "", subpath: str = "",
                         trust_note: str = ""):
    """注册 skill 到 sources.json。

    `trust_level` 是**这一轮新增落盘的**（2026-10-06，D1）。

    ## 为什么它必须落盘

    这个值以前只活在 `install_skill()` 的内存返回字典里 —— 装完之后就没了：
    面板读的是 `sources.json` → `installed_snapshot()`，而那份快照里没有它。
    于是「装完之后，这个 skill 到底验没验过」在界面上**根本无处可查**，
    而面板早先那个按来源名硬编码的绿勾，填的正是这个空位 —— 用一个没有依据的
    结论，去补一个本该有真数据的位置。（见 D1 的方案：删掉那个假徽章，
    改在**已安装列表**里显示这个真值。）

    ⚠️ 空字符串是**有含义的**，不能当成"验过了"：面板必须把它显示成
    「未记录」而不是默认 verified —— fail-closed，`_trust_level()` 的语义一致。
    历史条目（本字段出现之前装的）就是这种情况。
    """
    sources = {}
    if SOURCES_FILE.exists():
        try:
            sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass

    sources[skill_name] = {
        # ⚠️ 2026-10-07：这个值原先来自一个 `source_type` 形参（默认 "remote"，
        # 但没有任何调用点传过别的值）—— 形参已删，内联成常量。
        #
        # **字段本身必须留着**：`scripts/inventory.sh` 的更新探测会读它
        # （`open('$SOURCES_FILE')` 里按 type 判断是否去查远端）。
        # 删形参安全，删字段会让新装条目的更新探测静默失效。
        "type": "remote",
        "url": url,
        "branch": "main",
        # D3：以前这里恒为 ""，而**没有任何地方读过它** —— 一个写死空串、
        # 又没人看的字段。对子 skill 来说这个值是**已知的**（它在仓库里的相对路径），
        # 所以现在真的记下来：monorepo 里装的子 skill，靠它才能定位回仓库的哪一段。
        "subpath": subpath,
        "installed_sha": sha,
        "installed_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "skill-forge",
        "trust_level": trust_level,
        # 为什么是这个等级（哪一层、什么结论）。面板拿它做徽章的提示文字 ——
        # 只有等级没有理由的话，一枚「部分验证」读了等于没读。见 `_trust_note`。
        "trust_note": trust_note,
    }
    SOURCES_FILE.write_text(json.dumps(sources, ensure_ascii=False, indent=2), encoding="utf-8")


def _is_github_url(url: str) -> bool:
    """这个 URL 的**主机**是不是 github.com。

    ⚠️ 以前这里是 `"github.com" in url` —— **整串子串匹配**。于是
    `https://attacker.example/github.com/evil` 会被当成 GitHub 地址放行，
    然后真的去 clone 它。这些 URL 来自面板 / 安装队列，是**不受信输入**；
    而"这是不是 GitHub"这个问题，只有主机名能回答。

    （`a/b` 这种 owner/repo 简写、以及 `git@github.com:o/r.git` 这种 SSH 写法
      都认 —— 它们在 sources.json 和用户手填里都真实出现过。）
    """
    from urllib.parse import urlparse
    s = (url or "").strip()
    if not s:
        return False
    # SSH 简写：git@github.com:owner/repo.git —— urlparse 对无 scheme 的串
    # 取不到 hostname，所以先手工剥出 `@` 与 `:` 之间那一段。
    if "://" not in s and "@" in s:
        s = s.split("@", 1)[1]
    try:
        host = urlparse(s if "://" in s else "https://" + s).hostname or ""
    except ValueError:
        return False
    host = host.lower().rstrip(".")
    return host == "github.com" or host.endswith(".github.com")


def install_skill(name: str, url: str, full_name: str = "") -> dict:
    """
    安装单个 skill
    返回 {"ok": true/false, "error": "...", "sha": "..."}

    name 来自安装队列（可由面板经 bridge 写入），是**不受信输入** ——
    它决定要 rmtree 哪个目录，所以第一件事就是验它。

    ⚠️ 2026-10-07：签名里原先有个 `source` 形参，接收后**一次都没被读过**，已删除。
    它没有生产者（面板写队列时不带 `source` 键）、也没有消费者
    （函数体四道闸门 `check_name` / `is_forge_dir` / `_is_github_url` / `precheck`
     都不看它；落盘的 `"source": "skill-forge"` 是写死的常量）。
    做过安全性评估：无漏洞、无系统信息泄露，删除是行为保持的改动。
    """
    # 验名必须在确定 URL、clone 之前：早失败，才不用白 clone 一遍
    try:
        safe_paths.check_name(name)
    except safe_paths.UnsafeName as e:
        err = f"拒绝安装：{e}"
        log(err)
        return {"ok": False, "error": err}

    # 「覆盖安装自己」= 用别的仓库的内容**替换掉整个管理器**。
    # `check_name` 放行 `skill-forge`（它是合法的单分量名），所以这条必须独立存在。
    # 早失败：连 clone 都不用做。
    if safe_paths.is_forge_dir(SKILL_ROOT / name, SKILL_ROOT):
        err = "拒绝安装到 skill-forge 本体上 —— 那会把管理器自己覆盖掉"
        log(err)
        return {"ok": False, "error": err}

    # 确定 clone URL
    if url and _is_github_url(url):
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
        _update_sources_json(name, clone_url, sha,
                             trust_level=_trust_level(verdict),
                             trust_note=_trust_note(verdict))

        # 如果仓库中有多个 skill 目录，尝试发现子 skill（如 claude-skills-research 的 monorepo）
        sub_verify = []
        extra_skills = _discover_sub_skills(repo_dest, name, clone_url, sha,
                                            report=sub_verify)

        log(f"安装完成: {name} → {dest}  (SHA: {sha[:8] if sha else '?'}, {len(skill_files)} 文件, +{len(extra_skills)} 子skill)")

        return {
            "ok": True,
            "sha": sha[:8] if sha else "",
            "path": str(dest),
            "files": len(skill_files),
            "extra_skills": extra_skills,
            # 每个子 skill 各自的预检结论。面板/日志据此说明
            # "为什么这个子 skill 没装上" —— 以前它是无声消失的。
            "sub_skills_verify": sub_verify,
            # 把预检结论带回去 —— 面板据此显示真实结果，
            # 而不是装之前就瞎标一个"已审核"。
            #
            # ⚠️ `trust_level` 原先写死成常量 `"verified"`，可上面那句注释说的
            # 恰恰是"别瞎标"。于是无论 L1–L5 跑成什么样，值都是 verified ——
            # 包括**五层里跳过了四层**的情形。现在按真实结论算。
            "trust_level": _trust_level(verdict),
            "verify": verdict,
        }


def _trust_level(verdict) -> str:
    """把预检结论折算成一个**诚实的**信任等级。

    三档，都从 `verify` 的真实结果来，不是常量：

      - `verified`   —— 五层全部跑完，且没有跳过、没有警告
      - `partial`    —— 跑完了但有层被跳过（前置缺失）或有警告
      - `unverified` —— 没跑成结论（缺 verdict / 有层被拦）

    **为什么 `partial` 必须跟 `verified` 分开**：一套没配 Docker、没配密钥的
    部署，L5 每次都是 `SKIPPED`，而报告汇总写的是"4 层检查通过，1 层跳过"。
    把这种安装也标成 `verified`，就等于让"跳过"在标签层面消失 ——
    这正是本轮审计里反复出现的那类缺陷（值算对了，标签在说谎）。
    """
    if not isinstance(verdict, dict):
        return "unverified"
    layers = verdict.get("layers") or []
    if not layers:
        return "unverified"
    if verdict.get("blocked_by"):
        return "unverified"

    skipped = [L for L in layers if (L or {}).get("verdict") == "SKIPPED"]
    warned = [L for L in layers if (L or {}).get("verdict") == "WARN"]
    errored = [L for L in layers if (L or {}).get("verdict") == "ERROR"]
    # ⚠️ `REVIEW` 原先**没有**在这里处理（2026-10-07 补上）。它一直没暴露，
    # 是因为 REVIEW 会让 `blocked_by` 非空、上面第 420 行直接返回 unverified ——
    # 一个**被拦住**的技能本来也走不到安装。
    #
    # 但闸门一放开（见 precheck.BLOCKING_VERDICTS 的说明），REVIEW 的技能就会
    # 一路落到最后那个 `return "verified"` —— 于是"有层标了需要人工看一眼"
    # 的 skill 会在 sources.json 里被记成**已验**。那正是 D1 刚修掉的那个
    # "标签在说谎"，只不过换了个入口。
    reviewed = [L for L in layers if (L or {}).get("verdict") == "REVIEW"]
    if errored:
        return "unverified"
    if skipped or warned or reviewed:
        return "partial"
    return "verified"


def _trust_note(verdict) -> str:
    """一句话说清**为什么**是这个等级 —— 供面板显示（做徽章的提示文字）。

    ⚠️ 2026-10-07 加的。起因：闸门放开之后 `trust_level` 成了唯一的信号，
    但面板上只有一枚「部分验证」的徽章，**看不到是哪一层、因为什么**。
    而实测发现只要 L2 被限流跳过，**每个 skill 都会是 partial** ——
    一枚对所有输入说同一句话的徽章，等于没有信息。

    现在把"为什么"也落盘（`trust_note`），徽章悬停就能看见具体是哪层、
    什么结论。只列**不是 PASS** 的层；全 PASS 时返回空串。
    """
    if not isinstance(verdict, dict):
        return "没有预检结论"
    layers = verdict.get("layers") or []
    if not layers:
        return "没有预检结论"
    parts = []
    for L in layers:
        L = L or {}
        v = L.get("verdict")
        if not v or v == "PASS":
            continue
        reason = str(L.get("reason") or "").strip()
        parts.append(f"{L.get('name')} {v}" + (f"：{reason[:60]}" if reason else ""))
    return "；".join(parts)


def _discover_sub_skills(repo_dir: Path, primary_name: str, clone_url: str = "",
                         sha: str = "", report: list = None) -> list:
    """
    发现仓库中的其他 skill（monorepo 如 claude-skills-research）
    如果仓库包含多个子目录且各有 SKILL.md/CLAUDE.md，自动安装并注册这些子 skill

    每个子 skill 在被 copytree **之前**要单独过一遍预检（见下面的长注释）。

    `report`：可选的出参列表。函数返回的仍是名字列表（调用方依赖这个形状），
    而每个子 skill 的预检结论塞进 `report` —— 返回值形状不动是为了不把
    `extra_skills` 的消费方一起改掉。
    """
    extra = []
    skipped = set()
    sub_verify = report if report is not None else []
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

            # ⚠️ 「克隆下来的仓库里有个目录就叫 `skill-forge`」= 一个外部仓库
            # 想**覆盖掉管理器自己**。`skill-forge` 是合法的单分量名，
            # `resolve_within` 拦不住，所以这条必须独立存在。
            if safe_paths.is_forge_dir(dest_dir, SKILL_ROOT):
                skipped.add(skill_name)
                log(f"  跳过一个子 skill {skill_name!r}：它指向 skill-forge 本体，"
                    "装上去会把管理器覆盖掉")
                continue

            # ---- 子 skill 也要过预检 ----
            #
            # **以前它不过。** `precheck.verify_repo` 只在整仓上跑一次，而
            # L1/L4/L5 都只读**顶层** `SKILL.md`；子 skill 被 copytree 成
            # `~/.claude/skills/<子名>` 之后是个**独立的、会被 Claude 读指令的 skill**，
            # 它的 SKILL.md 却一次都没被看过。于是"主 skill 干净、恶意藏在子 skill
            # 指令文本里"的 monorepo，在面板路径上会被装进去并标成 verified。
            #
            # 逃逸的**只是指令文本**：L3 扫目录是递归的，子目录里的脚本本来就被覆盖。
            # 但指令文本正是 Claude 会当命令读的东西。
            #
            # 跳过 L2：来源是同一个仓库，整仓那次已经查过，重打 GitHub API
            # 会把未鉴权的 60 次/小时额度烧光（见 precheck.verify_repo 的 skip 说明）。
            sub_verdict = None
            try:
                sub_verdict = precheck.verify_repo(parent, clone_url, skip=("L2",))
            except Exception as e:
                # 验不了 ≠ 通过了。验不了就不装 —— 这条路径无人值守。
                skipped.add(skill_name)
                log(f"  跳过子 skill {skill_name}：预检没能跑起来"
                    f"（{type(e).__name__}: {e}）")
                sub_verify.append({"name": skill_name, "ok": False,
                                   "summary": f"预检异常：{type(e).__name__}: {e}"})
                continue

            if not sub_verdict.get("ok"):
                skipped.add(skill_name)
                log(f"  跳过子 skill {skill_name}：预检未通过 —— {sub_verdict.get('summary')}")
                sub_verify.append({"name": skill_name, "ok": False,
                                   "summary": sub_verdict.get("summary", ""),
                                   "verify": sub_verdict})
                continue

            # 安装额外发现的 skill
            try:
                if dest_dir.exists():
                    shutil.rmtree(dest_dir)
                shutil.copytree(parent, dest_dir, ignore=shutil.ignore_patterns(".git"))
                extra.append(skill_name)
                # 注册子 skill 到 sources.json
                # ⚠️ 这里以前是 `if clone_url and sha:`，而主 skill 那句
                # `_update_sources_json(name, clone_url, sha)` 是**无条件**的。
                # 两者不一致的后果很实在：sha 取不到时（克隆下来的目录不是 git 仓库、
                # 或者没有 HEAD），子 skill 会被**装到磁盘上却不登记** ——
                # 它出现在 skills/ 里、却不在 sources.json 里，
                # 于是面板显示"没装过"，而下一轮发现流程又会把它当成"新发现"。
                # 现在与主 skill 对齐：知道仓库地址就登记，sha 允许为空串。
                if clone_url:
                    # subpath = 这个子 skill 在仓库里的位置。装的时候就知道，
                    # 以前却写死成空串 —— 见 _update_sources_json 里的说明。
                    _update_sources_json(
                        skill_name, clone_url, sha,
                        trust_level=_trust_level(sub_verdict),
                        trust_note=_trust_note(sub_verdict),
                        subpath=parent.relative_to(repo_dir).as_posix())
                sub_verify.append({"name": skill_name, "ok": True,
                                   "summary": sub_verdict.get("summary", ""),
                                   "verify": sub_verdict})
                log(f"  发现子 skill: {skill_name} —— {sub_verdict.get('summary')}")
            except Exception as e:
                # **这里以前是裸的 `except Exception: pass`。**
                # rmtree / copytree / 写 sources.json 任一失败都无声消失 ——
                # 目录里有这个 skill、sources.json 里没有、日志里也没有。
                skipped.add(skill_name)
                sub_verify.append({"name": skill_name, "ok": False,
                                   "summary": f"安装失败：{type(e).__name__}: {e}"})
                log(f"  子 skill {skill_name} 安装失败：{type(e).__name__}: {e}")
    return extra


def process_install_queue() -> dict:
    """
    处理安装队列：遍历 pending → 逐个安装 → 移到 history
    返回 {"processed": N, "success": N, "failed": N, "results": [...]}
    """
    if not QUEUE_FILE.exists():
        return {"processed": 0, "success": 0, "failed": 0, "results": [], "error": ""}

    try:
        queue = json.loads(QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        # ⚠️ 读不出来 **≠** 队列是空的。
        # 以前这两种情况返回**逐一相同**的字典，于是 bridge 回 `ok:true`、
        # 面板弹"安装完成" —— 而其实一条都没处理，队列里的东西还躺在那个
        # 坏文件里。`error` 就是用来把这两者分开的。
        log(f"安装队列解析失败（{type(e).__name__}）：{QUEUE_FILE}")
        return {"processed": 0, "success": 0, "failed": 0, "results": [],
                "error": f"安装队列文件损坏，本次未处理任何条目（{type(e).__name__}）"}

    pending = queue.get("pending", [])
    if not pending:
        return {"processed": 0, "success": 0, "failed": 0, "results": [], "error": ""}

    results = []
    success = 0
    failed = 0

    for item in pending:
        name = item.get("name") or item.get("skill_name") or ""
        url = item.get("url") or item.get("repo_url") or ""
        full_name = item.get("full_name") or item.get("repo") or ""

        result = install_skill(name, url, full_name)
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
        "error": "",
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
