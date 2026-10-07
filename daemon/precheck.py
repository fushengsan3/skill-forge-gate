#!/usr/bin/env python3
"""
安装前预检 —— 把 verify/ 的 L1–L5 接到**面板那条安装路径**上。

## 为什么需要它

面板的「立即安装」走的是一条捷径：

    面板 → POST /install → process_install_queue() → clone → 装进 ~/.claude/skills/

**全程零校验。** `verify/` 里的五层只被它自己的测试 import，`SKILL.md` 里那套
"装之前跑 L1–L5"是写给 Claude 的流程，根本不经过 bridge。于是：

  - README 宣称的"L1-L5 安全验证流水线"对面板来说一直是空的
  - 面板还给每条队列项标了 `verified` / `unverified` —— 一个不产生任何后果的标签

这个模块把那条捷径补上。

## 判定规则（面板路径无人值守）

| 层的结论 | 处理 |
|---|---|
| `REJECT` | **拒绝安装** |
| `REVIEW` | **照常安装**，但 `sources.json` 里标 `trust_level: partial`，报告写明原因 |
| `WARN` / `PASS` | 放行，但把警告一并带回去 |
| 静态层跑出异常 | **拒绝** —— 本该总能跑起来的东西跑不起来，等于没验 |
| 外部层异常 / 前置缺失 | 跳过，如实标注（没装 Docker 不是这个 skill 的错） |

**为什么 `REVIEW` 不拦**（2026-10-07 改的，之前是拦的）：`REVIEW` 的定义是
"这一层拿不准，需要人工看一眼"。但面板路径上**没有那个人**，而 `REVIEW` 的
数量很大 —— 把"拿不准"一律当"拒绝"，等于让面板这条路的安装成功率趋近于零，
用户最后还是会绕过它走 Claude Code，面板就失去了存在意义。

判据不是拍脑袋定的：用**本机 80 个真实在用的 skill** 校准过，拦下率从
20/80 降到 0/80，而"真危险"的构造对照组 15/15 一个没漏。

⚠️ **代价必须说清楚：`REVIEW` 现在是"装了但标 partial"，不是"没装"。**
L3 命中可疑模式（`curl|bash`、`sudo`……）、或 L4 报冲突的 skill，
面板无人值守时**会真的落盘**。要人工把关就去看 `partial` 那几条，
别以为它们被挡在门外了。

对比：`WARN` 也放行。L1 对"目录里只有 SKILL.md"这种纯结构提示给 WARN，
把它当危险信号会让大量正常 skill 装不上。

## 代价

安装变慢。L2 要打一次 GitHub API，L5 在条件齐备时要起 Docker 调模型。
L5 的前置（Docker / API 凭据）会先探一次，缺了就直接跳过 ——
不然会白等 docker build 的超时。凭据判定走 `verify/llm_auth.py`，
**凭据管理器优先，环境变量兜底**。

## 「跳过」的含义（2026-10-05 明确）

前置缺失时 L5 是**跳过**，不是拒绝 —— 没装 Docker 不是这个 skill 的错。

但这留下一个必须说清楚的口子：**跳过 ≠ 通过**。一台没配凭据的机器上，
面板一键安装就是"永远没有 L5"，而报告里那一行是 `SKIPPED` 而不是 `PASS`。
两者在 `summarize()` 的输出里是分开写的（"N 层检查通过，M 层跳过（…）"），
这是有意的 —— 不要把它们合并成一句"检查完成"。

为什么凭据管理器优先：环境变量是**按进程注入**的，bridge 这个常驻进程
（任务计划 / HKCU\\Run 拉起）继承不到，于是这条路上 `credentials()` 永远为空、
L5 一次都没跑过。凭据管理器是跨进程的。详见 `verify/llm_auth.py` 顶部。
"""
import subprocess
from pathlib import Path

SKILLS_DIR = Path.home() / ".claude" / "skills"

# ---- 拦不拦：只有「明确有害」和「没验成」才拦 ----
#
# ⚠️ **2026-10-07 改**。原来是 `{REJECT, REVIEW}` —— 即"需要人看一眼"也硬拦。
# 那个设计的前提是「REVIEW 罕见且有意义」，实测把这个前提推翻了：
#
#   本机 80 个**真实在用**的 skill 走一遍，**20 个（25%）被判拦**
#   （L3 拦 14 + L4 拦 13，有重叠）。而它们全是正常 skill ——
#   用户自己装、自己天天用的那种。
#
# 追下去发现两层的判据都在问错的问题（都已修，见各自的注释）：
#   · L4 拿"两个 skill 目录里同名相对路径"当文件覆盖 —— 而它们装在不同目录里，
#     根本碰不到一起。`README.md`/`references/` 这种人人都有。
#   · L3 的好几条红组规则匹配的是**「提到」而不是「在做」**：`\beval\b` 命中
#     206 次（全是 JS/Python 源码里的 `eval(`）、裸露的 `Startup` 命中 799 次
#     （普通英文单词）、`sudo` 命中 150 次（注释里）。最讽刺的是被拒得最狠的
#     那个 skill，本身是**讲安全边界的**。
#
# 修完判据之后，80 个真 skill 里 REJECT **0 个**、REVIEW 13 个（都是"文档里提到
# curl/sudo"这类**确实值得看一眼**的事）。但这 13 个如果还硬拦，等于因为
# "这个 skill 的文档里出现了 curl"就拒绝安装 —— 那不是保护，是摩擦。
#
# 所以分成两种后果：
#   REJECT（明确有害）      → 不装
#   静态层 ERROR（没验成）  → 不装（下面 elif 那条，**没有动**）
#   REVIEW（需要人看一眼）  → **装，但如实标成 `partial`**，
#                             并把它为什么被标出来带进报告
#
# 最后一条依赖 `installer._trust_level()` 把 REVIEW 折成 `partial` ——
# 它原先漏了 REVIEW（会一路掉到 `verified`）。两处是**一起改**的。
#
# 想让流水线回到"REVIEW 也拦"，把这里改回 `{"REJECT", "REVIEW"}` 即可 ——
# 但先看一眼上面那组数字：那样做会拒掉四分之一的正常 skill。
BLOCKING_VERDICTS = frozenset({"REJECT"})

# 但**不能一刀切** —— 每层的"黄"含义不一样。
#
# L2 是**声誉**检查：星数少、是 fork、没写许可证，全给黄。
# 这几条对绝大多数开源 skill 都成立（刚发布的项目星数就是少）。
# 一刀切地"REVIEW 就拦"，等于把整个生态拒之门外，而且拒的理由
# 跟安全毫无关系。
#
# 真正需要人工看的是**内容**层的黄（L3：可疑模式）和冲突层的黄（L4）。
# 所以 L2 只在 REJECT（仓库归档 / 不存在）时才拦。
BLOCKING_BY_LAYER = {
    "L2 来源": frozenset({"REJECT"}),
}

# 静态分析：纯本地读文件，本该总能跑完。跑不完 = 没验成 = 拒。
STATIC = "static"
# 外部依赖：Docker、GitHub API。缺条件不是 skill 的错，跳过并标注。
EXTERNAL = "external"


def _docker_available():
    """→ (是否可用, 不可用的原因)

    以前是 `except Exception: return False`，把三种**完全不同**的情况压成同一句
    "Docker 不可用"：没装 docker、装了但**守护进程没起**、以及别的一切异常。
    而 L5 会因此静默跳过 —— 用户看到的是"跳过"，看不到"你的 Docker Desktop 没开"。
    （2026-10-05 机器重启后就真踩到过：守护进程没自启。）
    """
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=True)
        return True, ""
    except FileNotFoundError:
        return False, "这台机器没有 docker 命令（不在 PATH 里）"
    except subprocess.CalledProcessError as e:
        return False, f"docker 命令在，但 `docker info` 失败（守护进程没起？退出码 {e.returncode}）"
    except subprocess.TimeoutExpired:
        return False, "`docker info` 超过 10 秒没响应"
    except OSError as e:
        return False, f"调不起 docker：{type(e).__name__}"


def _run(name: str, kind: str, fn) -> dict:
    """跑一层，把任何异常收敛成一条记录 —— 预检绝不该让安装流程崩掉。"""
    try:
        result = fn()
    except Exception as e:
        return {"name": name, "kind": kind, "verdict": "ERROR",
                "reason": f"{type(e).__name__}: {e}"[:200]}
    if not isinstance(result, dict):
        return {"name": name, "kind": kind, "verdict": "ERROR",
                "reason": f"返回了 {type(result).__name__}，不是报告"}

    # ⚠️ 说明文字**在不同 verdict 下放在不同字段里**，这里必须都认：
    #     L1/L3/L4（预检自己那几层）→ `reason`
    #     L5 的 REVIEW               → `note`（`run_docker_sandbox` 只在 REVIEW 分支写它）
    #     L5 的 ERROR / SKIPPED      → `error`
    #
    # 只读 `reason` 的后果 **2026-10-07 Phase 5 真机跑时实测到了**：
    # L5 判 REVIEW 时返回值里**根本没有 `reason` 这个键**，于是报告上只有一句
    #     "L5 沙箱 未通过（REVIEW）："      ← 冒号后面什么都没有
    # 读的人无从知道为什么。而 L5 其实说得很清楚（"skill 计划执行 4 个 Bash 命令，
    # 请与 L3 静态分析结果交叉验证"）—— 它只是没被搬过来。
    #
    # 这与下面那份计数白名单是**同一个形状的坑**（那里写着"L5 的 write_calls /
    # network_indicators 就这么丢过一次"）：不是没算出来，是没搬出去。
    detail = (result.get("reason") or result.get("note") or result.get("error") or "")
    layer = {
        "name": name,
        "kind": kind,
        "verdict": str(result.get("verdict", "ERROR")),
        "reason": str(detail)[:300],
    }
    # 把各层的关键计数带出来 —— 这几个数正是用户/排查的人最想看的。
    #
    # ⚠️ 这份清单是**白名单**：往 summary 里加了新计数却忘了加到这里，那个数
    # 就永远到不了面板和 HTTP 响应，而且不会有任何报错。L5 的
    # write_calls / network_indicators 就这么丢过一次 —— 它们原本只活在
    # `run_docker_sandbox` 的返回值里，从预检出去就没了。
    summary = result.get("summary")
    if isinstance(summary, dict):
        layer["counts"] = {k: v for k, v in summary.items()
                           if k in ("red_count", "yellow_count", "scanned_files",
                                    "total_tool_calls", "bash_calls", "write_calls",
                                    "network_indicators")}
    return layer


def _skip(name: str, why: str) -> dict:
    return {"name": name, "kind": EXTERNAL, "verdict": "SKIPPED", "reason": why}


def _l5(repo_dir: Path, l5) -> dict:
    """L5 沙箱。前置不齐就先跳过，别去等 docker build 的超时。

    **凭据判定走 l5/llm_auth，这里绝不自己写一份**。原先这里只认
    `ANTHROPIC_API_KEY`，于是配 `ANTHROPIC_AUTH_TOKEN` 的机器永远不会跑到 L5
    （2026-10-05 在真机上撞到）。两处判定迟早分叉，而分叉的那一处就是
    "功能在、一次都不触发"的那一处。
    """
    try:
        info = l5.describe_credentials()
    except Exception as e:
        # 判定本身崩了 —— 这跟"没配"是两回事，写清楚。
        return _skip("L5 沙箱", f"凭据判定失败（{type(e).__name__}: {e}）—— 跳过")

    if not info.get("has_token"):
        note = str(info.get("key_note") or "")
        if note.startswith("unreadable"):
            # 配过，但读不出来。**这不是「没配」** —— 让用户去设置页反复保存
            # 是徒劳的，那条记录本身就坏了。
            hint = ("⚠️ 凭据管理器里有这条记录，但**读不出来** —— 这不是「没配」。"
                    "去 Windows 凭据管理器（控制面板 → 用户账户 → 凭据管理器 → "
                    "Windows 凭据）删掉 `SkillForge/ai-token` 再重存一次。"
                    f"（{note}）")
        elif note:
            hint = f"⚠️ {note}"
        else:
            hint = ("配法：面板 → 设置 → 密钥 → AI 密钥（推荐，跨进程可用）；"
                    "或设 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN"
                    "（只对继承到该变量的进程有效）")
        return _skip("L5 沙箱",
                     "没拿到 AI 凭据 —— 容器里调不了模型，本轮跳过。"
                     f"**跳过不是通过**，也不是这个 skill 的问题。{hint}")

    docker_ok, docker_why = _docker_available()
    if not docker_ok:
        # 把原因带进报告。跳过本身不拦安装，但"为什么跳过"必须看得见 ——
        # 否则用户只会以为这个 skill 不需要沙箱审计。
        return _skip("L5 沙箱", f"Docker 不可用 —— 跳过（不是这个 skill 的问题）：{docker_why}")
    return _run("L5 沙箱", EXTERNAL, lambda: l5.run_docker_sandbox(
        str(repo_dir), l5.generate_test_prompts(str(repo_dir))))


def verify_repo(repo_dir, url: str, skills_dir=None, skip: tuple = ()) -> dict:
    """对**已经 clone 到本地**的仓库跑 L1–L5。

    返回 {"ok": bool, "layers": [...], "blocked_by": [层名], "summary": str}。

    clone 之前不跑是因为 L1/L3/L4 都要读文件。早失败那一步由 safe_paths 的名字
    校验负责（它在 clone 之前）。

    ## `skip`：点名跳过的层

    给**子 skill** 用的（`daemon/installer.py::_discover_sub_skills`）。
    一个 monorepo 里几十个子 skill，每个都重打一次 GitHub API 会把
    未鉴权的 60 次/小时额度当场烧光 —— 而来源是**同一个仓库**，整仓那一次
    已经查过了。

    ⚠️ 被跳过的层**照样出现在报告里**，标 `SKIPPED` + 说明原因。
    不是"悄悄不生成这一层" —— 那份报告会被读成"五层都跑过"，而真相是四层。
    这也是为什么没有被跳过的层时，`_trust_level` 会给出 `partial`：
    它说的是"**这个** skill 并非每一层都验过"，那是事实。
    """
    repo_dir = Path(repo_dir)
    skills_dir = Path(skills_dir) if skills_dir else SKILLS_DIR

    # 延迟 import：这几层会拉起 requests / docker 之类的东西，
    # 不该在"只是 import 一下 precheck"的时候就付出代价。
    from verify import (l1_structure, l2_source, l3_content_scan,
                        l4_conflict_detect, l5_sandbox)

    layers = [
        _run("L1 结构", STATIC,
             lambda: l1_structure.check_skill(str(repo_dir))),
    ]
    if "L2" in skip:
        layers.append(_skip("L2 来源",
                            "同一仓库的来源检查已在整仓预检里做过，不重复打 GitHub API。"
                            "**这一层没有针对本 skill 单独跑过**"))
    else:
        layers.append(_run("L2 来源", EXTERNAL,
                           lambda: l2_source.check_source(url)))
    layers += [
        _run("L3 内容", STATIC,
             lambda: l3_content_scan.scan_skill(str(repo_dir))),
        _run("L4 冲突", STATIC,
             lambda: l4_conflict_detect.detect_conflicts(str(repo_dir), str(skills_dir))),
        _l5(repo_dir, l5_sandbox),
    ]

    blocked = []
    for layer in layers:
        # 外部层"没问成"（API 限流、断网）→ 当跳过，不当拒绝。
        # 核查没做成 ≠ 核查没通过。见 verify/l2_source.py 里的说明。
        if layer["verdict"] == "UNKNOWN" and layer["kind"] == EXTERNAL:
            layer["verdict"] = "SKIPPED"
            layer["reason"] = "无法核实 —— " + layer["reason"]

        blocking = BLOCKING_BY_LAYER.get(layer["name"], BLOCKING_VERDICTS)
        if layer["verdict"] in blocking:
            blocked.append(layer)
        elif layer["verdict"] == "ERROR" and layer["kind"] == STATIC:
            blocked.append(layer)

    return {
        "ok": not blocked,
        "layers": layers,
        "blocked_by": [layer["name"] for layer in blocked],
        "summary": summarize(layers, blocked),
    }


def summarize(layers: list, blocked: list = None) -> str:
    """一句话说清楚结论。这句话会出现在日志、HTTP 响应和面板上。

    ## 为什么每个结论都要单独数

    2026-10-05 之前这里写的是 `done = [L for L in layers if L["verdict"] not in ("SKIPPED",)]`
    然后 `f"{len(done)} 层检查通过"`。**"不是跳过"被当成了"通过"** ——
    于是跑崩的那一层（`ERROR`）和带警告的那一层（`WARN`）都被数进了"通过"里。
    最坏的情形是外部层出错：外部层的 ERROR 不进 blocked（见 verify_repo 里的判断），
    于是 L5 一出错，报告反而念出"5 层检查通过"。

    现在每个结论各数各的，谁都不冒充谁。"跳过"和"出错"都必须出现在这句话里 ——
    读的人只看这一句，它漏掉的那一层就等于不存在。
    """
    blocked = blocked if blocked is not None else [
        L for L in layers if L["verdict"] in BLOCKING_VERDICTS]
    if blocked:
        head = blocked[0]
        more = f"，另有 {len(blocked) - 1} 层也拦了" if len(blocked) > 1 else ""
        return f"{head['name']} 未通过（{head['verdict']}）：{head['reason']}{more}"

    passed = [L["name"] for L in layers if L["verdict"] == "PASS"]
    warned = [L["name"] for L in layers if L["verdict"] == "WARN"]
    errored = [L["name"] for L in layers if L["verdict"] == "ERROR"]
    skipped = [L["name"] for L in layers if L["verdict"] == "SKIPPED"]
    # ⚠️ `REVIEW` 原先**没有**被数（2026-10-07 补上）。闸门放开之后它不再进
    # `blocked`，于是这一句里既不算通过也不算跳过 —— **那一层等于不存在**。
    # 读的人只看这一句（日志/HTTP 响应/面板都是它），漏掉就等于"看起来全绿"。
    reviewed = [L["name"] for L in layers if L["verdict"] == "REVIEW"]

    text = f"{len(passed)} 层检查通过"
    if reviewed:
        text += f"，**{len(reviewed)} 层需要人工看一眼**（{'、'.join(reviewed)}）"
    if warned:
        text += f"，{len(warned)} 层有警告（{'、'.join(warned)}）"
    if errored:
        text += f"，{len(errored)} 层出错（{'、'.join(errored)}）"
    if skipped:
        text += f"，{len(skipped)} 层跳过（{'、'.join(skipped)}）"

    # 四项加起来对不上层数时，把总数写出来 —— 否则一句"N 层……"读起来像已经交代完了。
    if len(passed) != len(layers):
        text += f"，共 {len(layers)} 层"
    return text


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 3:
        print("用法: python -m daemon.precheck <已clone的目录> <仓库URL>")
        sys.exit(2)
    print(json.dumps(verify_repo(sys.argv[1], sys.argv[2]), ensure_ascii=False, indent=2))
