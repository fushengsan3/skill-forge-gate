#!/usr/bin/env python3
"""
L5 沙箱行为审计 —— 在 Docker 容器里加载 skill，观察它**计划**调用哪些工具。

注意两件事：
  1. 容器是**真的**会跑的（run_docker_sandbox 里有一次 docker run），
     不是只 build 一下就完事。
  2. 工具调用只被**观察**，不被执行 —— 你拿到的是「它想干什么」，
     不是一个被炸掉的容器。真执行是下一步的事。

所谓「gVisor 深度审计」**并不存在**。
`sandbox/gvisor.toml` 原先是一份没接上代码的配置文件，2026-10-06 已**删除** ——
留着一份读起来像"有这条路"的配置，比没有它更糟（用户 2026-10-06 定：策略文件
只描述 L5 真能做到的事）。**别在文档里承诺 gVisor。**

同理，`sandbox/audit-policy.yaml` 那一整段 syscall 审计也已删除：L5 从不执行
skill 里的脚本，没有进程跑起来就没有 syscall 可看。现在那份文件只写
**静态规则**（敏感路径 + 出网白名单），由 `verify/audit_policy.py` 读取，
L3 与 L5 共用同一份。

输出 JSON 审计报告到 stdout
"""
import json
import sys
import os
import re
import subprocess
from pathlib import Path

# 直接跑本文件时（`python verify/l5_sandbox.py`），sys.path[0] 是 **verify/ 这一层**，
# 不是仓库根 —— 于是 `from verify import llm_auth` 找不到 `verify` 这个包。
#
# 修法是把仓库根**先**放进去，而不是 `try/except ImportError` 再重试一次：
#   - 重试版在包已存在、只是子模块缺失时，第二次仍抛 ImportError，
#     报错内容（`cannot import name 'llm_auth' from 'verify'`）会把人指向
#     "模块坏了"，而真实原因是"这个文件没被部署出去"。（2026-10-05 就这么误诊过一轮。）
#   - `except ImportError` 还会顺手吞掉 llm_auth **内部**真正的 ImportError。
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from verify import llm_auth
# 敏感路径与出网白名单的**唯一一份**清单 —— 与 L3 读的是同一个文件。
from verify import audit_policy

# 这个模块**故意不定义 PROXY**。容器里的请求不走宿主代理 —— 宿主需要代理是
# 宿主网络的事，把宿主的内网地址塞进容器是反方向的。（原先有一个 PROXY 常量，
# 只被上面那个已删的宿主侧函数用。）


def api_credentials() -> tuple:
    """凭据判定 —— 转发到 `verify/llm_auth.py`，**判定只留那一份**。

    保留这个名字是因为 `daemon/precheck.py` 和测试都按它调。
    以前这里有一份自己的判定，跟 L4 那份一模一样地只认 ANTHROPIC_API_KEY；
    两份分叉了也不会有谁发现，所以现在让它们共用一份。
    """
    return llm_auth.credentials()


def describe_credentials() -> dict:
    """凭据来源的排查视图（不含密钥值）—— 转发到 `llm_auth.describe()`。

    跟 `api_credentials()` 同一个理由：判定只留一份。`precheck` 靠它把
    "没配"和"配了但读不出来"分开写进跳过理由里。
    """
    return llm_auth.describe()


def run_docker_sandbox(skill_path: str, test_prompts: list) -> dict:
    """在 Docker 容器里跑 L5 审计。

    ## 这是一次**真的** docker run

    2026-10-05 之前这里只做 `docker build`，**从来没有 `docker run`** ——
    镜像建完就扔了，模型调用发生在宿主进程里。于是文档里写的
    「在隔离容器中加载 skill」是假的，容器从未启动过。现在审计过程真的在容器里跑。

    ## 隔离了什么

      - skill 目录以 **:ro** 挂载 —— 改不了被审计的东西
      - 容器内以**非 root** 运行（Dockerfile 里的 USER sandbox）
      - `--cap-drop=ALL` + `no-new-privileges` —— 拿不到任何额外能力
      - `--read-only` 根文件系统，只给 /tmp 一块 tmpfs
      - 内存 / CPU / 进程数都有上限，跑飞了也拖不垮宿主
      - `--rm` —— 跑完即毁，不留东西
      - **引擎密钥走 stdin**，不进 `-e`、不进命令行参数 ——
        那两个通道会把它写进 `docker inspect` 和宿主的进程表

    ## 没有隔离什么（必须说清楚）

    模型仍然只是**声明**它想调用什么工具，**这些调用不会被真的执行**。
    一份写着 `rm -rf /` 的 skill 永远不会真的跑起来 —— 你拿到的是
    「它想干什么」的情报，不是一个被炸掉的容器。真执行是下一步的事。

    ## 前置缺失时怎么办

    没有 Docker / 没拿到 AI 凭据（凭据管理器 → 环境变量，见 `verify/llm_auth.py`）
    → 返回 verdict=ERROR 并说明原因。**不返回 PASS** —— 没做成的事不能算通过。
    至于要不要因此拦住安装，那是调用方（daemon/precheck.py）的判断，
    那边对缺前置是按「跳过」处理的 —— 而**跳过不等于通过**。
    """
    skill_path = Path(skill_path).resolve()
    skill_name = skill_path.name
    image = "skill-forge-sandbox:latest"
    container_name = f"skill-forge-audit-{os.getpid()}"
    sandbox_dir = Path(__file__).parent.parent / "sandbox"
    dockerfile = sandbox_dir / "default.dockerfile"

    results = {
        "sandbox_type": "docker",
        "skill": skill_name,
        "prompts_tested": len(test_prompts),
        "tool_calls_observed": [],
        "verdict": "PASS",
    }

    def fail(reason: str) -> dict:
        results["error"] = reason
        results["verdict"] = "ERROR"
        return results

    def skip(reason: str) -> dict:
        """前置条件不具备 —— **不是这个 skill 的问题**，所以是 SKIPPED 而不是 ERROR。

        `precheck` 那条路在调用本函数**之前**就把这两种情况判掉了，所以从没暴露过；
        但 CLI 直跑（`python -m verify.l5_sandbox <skill>`）会走到这里 ——
        它以前报 ERROR，与 SKILL.md 承诺的"L5 缺失时标 SKIPPED"不一致，
        而 ERROR 和 SKIPPED 在报告里的含义完全不同（一个说"沙箱出错了"，
        一个说"这次没跑"）。2026-10-06 冻结树复核 #42 抓到的就是这个残留。
        """
        results["error"] = reason
        results["verdict"] = "SKIPPED"
        return results

    if not dockerfile.exists():
        return fail(f"找不到沙箱 Dockerfile：{dockerfile}")

    # ---- 1. 构建镜像（有缓存就很快）----
    try:
        subprocess.run(
            ["docker", "build", "-t", image, "-f", str(dockerfile), str(sandbox_dir)],
            # errors="replace"：docker 的输出很容易带非 UTF-8 字节，而 `text=True`
            # 解码失败时 subprocess 不抛异常 —— 它让 returncode 变成错的 1，
            # 于是一次**成功**的 build 会因为 check=True 变成 CalledProcessError，
            # 报成"docker build 失败"。见 daemon/installer.py 的 run() 那条注释。
            capture_output=True, text=True, errors="replace", timeout=180, check=True,
        )
    except FileNotFoundError:
        return skip("找不到 docker 命令 —— 这台机器没装 Docker 或不在 PATH 里")
    except subprocess.CalledProcessError as e:
        return fail(f"docker build 失败：{(e.stderr or '')[-400:]}")
    except subprocess.TimeoutExpired:
        return fail("docker build 超时（180 秒）")

    # ---- 2. 前置：密钥 ----
    api_key, auth_style, key_source = api_credentials()
    if not api_key:
        # 「没配」和「配了但读不出来」的**处置完全不同**，理由必须分开写：
        # 前者叫用户去配，后者叫用户去凭据管理器删掉重存。
        # 混成同一句会让用户在设置页反复保存 —— 而问题不在那儿。
        note = (describe_credentials() or {}).get("key_note", "")
        hint = (f"⚠️ {note}" if note else
                "配法：面板 → 设置 → 密钥 → AI 密钥（凭据管理器，跨进程可用）；"
                "或设 ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN"
                "（只对继承到该变量的进程有效，bridge 常驻进程多半继承不到）")
        return skip("没拿到 AI 凭据 —— 容器里调不了模型，沙箱无法完成审计。"
                    f"**跳过不是通过**，也不是这个 skill 的问题。{hint}")

    skill_md = skill_path / "SKILL.md"
    skill_content = (skill_md.read_text(encoding="utf-8", errors="ignore")
                     if skill_md.exists() else "")

    payload = json.dumps({
        "skill_content": skill_content,
        "prompts": list(test_prompts or []),
        "api_key": api_key,
        "auth_style": auth_style,
        "base_url": llm_auth.base_url(),
        "model": llm_auth.model(),
        # 60 秒而不是 30：模型会先出一段 thinking，再声明工具调用，
        # 单轮耗时长得多。30 秒会把正常的回答掐断，而那又会被当成"审计没做成"。
        "timeout": 60,
        # 沙箱内**不设代理** —— 宿主需要代理是宿主的事，
        # 把宿主的内网地址带进容器是反方向的。
        "proxy": "",
    })
    results["key_source"] = key_source

    # ---- 3. 真的跑 ----
    cmd = [
        "docker", "run", "--rm", "-i",
        "--name", container_name,
        # 被审计的东西只读挂进来
        "-v", f"{skill_path}:/skill:ro",
        # 根文件系统只读，只开一块 tmpfs 给临时文件
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,size=16m",
        # 能力全削
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        # 资源上限：跑飞了也拖不垮宿主
        "--memory", "512m", "--memory-swap", "512m",
        "--cpus", "1",
        "--pids-limit", "128",
        # 根文件系统只读时 Python 不该去写字节码缓存；HOME 指到 tmpfs
        "-e", "PYTHONDONTWRITEBYTECODE=1",
        "-e", "HOME=/tmp",
        image,
    ]

    try:
        # 900 秒：一轮最多 60 秒，而 generate_test_prompts 会给出 6 轮左右。
        # 300 秒会在正常的慢回答上超时，超时又会被当成"审计没做成"。
        # errors="replace"：容器里的输出是最可能带非 UTF-8 的。解码失败会让
        # returncode 变成错的 1，而下面正是拿 returncode 判"容器退出码非零"——
        # 于是一次成功的审计会被报成失败。
        proc = subprocess.run(cmd, input=payload, capture_output=True,
                              text=True, errors="replace", timeout=900)
    except subprocess.TimeoutExpired:
        # 别把容器留在后台跑
        subprocess.run(["docker", "kill", container_name],
                       capture_output=True, text=True, errors="replace", timeout=30)
        return fail("docker run 超时（900 秒），容器已强制结束")

    if proc.returncode != 0:
        return fail(f"容器退出码 {proc.returncode}：{(proc.stderr or '')[-400:]}")

    try:
        out = json.loads((proc.stdout or "").strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return fail(f"容器输出不是合法 JSON：{(proc.stdout or '')[:200]!r}")
    if not isinstance(out, dict):
        # 合法 JSON 不等于我们要的东西 —— `"字符串"` / `[1,2]` 都能过 json.loads，
        # 但下面立刻要 .get()。不挡的话这里会抛 AttributeError，
        # 而调用方（precheck）会把异常当成"没验成"，报出来的原因却跟真实问题无关。
        return fail(f"容器输出是 {type(out).__name__}，不是预期的对象：{str(out)[:120]!r}")

    # ---- 4. 汇总观察到的 tool_call ----
    observed = out.get("tool_calls") or []
    results["tool_calls_observed"] = observed
    errs = out.get("errors") or []
    if errs:
        results["errors"] = errs

    all_tools = [t for t in observed if isinstance(t, dict) and t.get("name")]
    bash_calls = [t for t in all_tools if t.get("name") == "Bash"]
    write_calls = [t for t in all_tools if t.get("name") in ("Write", "Edit")]
    network_tool_names = ["WebFetch", "WebSearch"]
    network_calls = [t for t in all_tools if (
        t.get("name") in network_tool_names or
        (isinstance(t.get("input"), dict) and
         any(k in str(t["input"]).lower() for k in ["url", "domain", "endpoint"]))
    )]

    # ---- 策略驱动的判定（规则来自 sandbox/audit-policy.yaml，与 L3 同一份）----
    #
    # 这一组补的是**别处看不到**的东西：L3 看 skill 的**文本**，这里看模型
    # **声明的动作**。尤其是 `Read` 这一类以前完全不参与判定 ——
    # 一个「先读 `~/.ssh/id_rsa`、再把内容 WebFetch 出去」的计划，
    # 只要没声明 Bash，拿到的就是 **PASS**。
    #
    # 白名单外的出网同理：`summary.network_indicators` 只是个数字，
    # 它不区分"去了 api.github.com"和"去了某个没听说过的域名"。
    policy_sensitive = []
    for t in all_tools:
        for hit in audit_policy.find_sensitive_paths(str(t.get("input") or "")):
            if hit not in policy_sensitive:
                policy_sensitive.append(hit)

    def _url_in(obj) -> str:
        """从工具入参里挖出第一个像 URL / 主机的字符串。挖不到返回 ""。"""
        if isinstance(obj, str):
            return obj if ("://" in obj or "." in obj) else ""
        if isinstance(obj, dict):
            for v in obj.values():
                got = _url_in(v)
                if got:
                    return got
        if isinstance(obj, list):
            for v in obj:
                got = _url_in(v)
                if got:
                    return got
        return ""

    egress_unknown, egress_allowed = [], []
    for t in network_calls:
        host = audit_policy.host_of(_url_in(t.get("input")))
        if not host:
            continue
        bucket = egress_allowed if audit_policy.is_allowed_host(host) else egress_unknown
        if host not in bucket:
            bucket.append(host)

    _, policy_error = audit_policy.load()

    results["summary"] = {
        "total_tool_calls": len(all_tools),
        "bash_calls": len(bash_calls),
        "write_calls": len(write_calls),
        "network_indicators": len(network_calls),
        # 出网**去了哪**，不只是"有几次"。白名单内外分开列。
        "egress_allowed_hosts": egress_allowed,
        "egress_unknown_hosts": egress_unknown,
        "policy_sensitive_paths": policy_sensitive,
        "policy_error": policy_error,
    }

    # ---- 5. 判定 ----
    #
    # **只让 Bash 触发 REVIEW 是个洞。** 以前 write_calls / network_calls 只进了
    # summary —— 一个印在报告里的数字而已，不参与判定。于是一份
    # 「往 ~/.ssh/authorized_keys 追加一行」或「把读到的内容 WebFetch 到外部域名」
    # 的 skill，工具调用序列里一个 Bash 都没有，拿到的是 **PASS**。
    # 写入和出网恰恰是两类最该有人看一眼的动作。
    #
    # 为什么不直接 REJECT：L5 看到的是**计划**不是执行，而且"要写文件"对很多
    # 正常 skill 也成立（生成模板、写配置）。REVIEW 的定义就是"需要人工看一眼"，
    # 这正是这里想要的。
    # ---- 5. 判定：看动作的**实质**，不看动作的**有无** ----
    #
    # ⚠️ 2026-10-07 改的（用户拍板）。原先只要声明了 Bash 就判 REVIEW，而
    # `generate_test_prompts` 里有一句 "List all commands you can run" ——
    # 模型为了回答它**必然**声明 Bash（实测连跑 4 次都是 3~4 个）。于是：
    #   · 任何 skill（包括"整理本地笔记"这种）都被判 REVIEW；
    #   · 而 precheck 的规则是 REVIEW 也拒（无人值守路径上没有人来看报告）；
    #   · 合起来 = **面板的「立即安装」对任何 skill 都会失败**。
    #
    # 更难看的是那句判词："skill 计划执行 4 个 Bash 命令" —— 那 4 个 Bash 是
    # **审计自己的提示词诱出来的**（实测观察到的全是 `ls`/`pwd`/`find`），
    # 不是这个 skill 的意图。一个对所有输入都报警的检查，信息量是零，
    # 却拦下了所有东西。
    #
    # 现在把判据换掉：**动作的内容/目标危不危险**。
    #   Bash   → 命令文本过一遍 L3 的红组与可疑模式（`ls`/`pwd` 不命中）
    #   写入   → 目标是不是**系统盘路径**（写 ./INDEX.md 不算）
    #   出网   → 域名在不在白名单（走同一份 audit-policy）
    #   任意工具 → 目标有没有触碰敏感路径（同上）
    from verify import l3_content_scan as _l3

    # 「会改变系统状态」的那几类工具，其**内容与目标**都要过 L3 的那两组规则。
    #
    # ⚠️ 为什么**不**把所有工具的输入一刀切地扫：`Read /etc/hosts` 会命中 L3 的
    # "DNS劫持" 规则（那条规则针对的是**改** hosts，不是读它），于是每一次普通的读
    # 都会变成 REVIEW。读本身不是外泄 —— 外泄要**带着内容出去**（那由 egress 那条管）。
    # 所以只扫动作类，Read/WebFetch/WebSearch 仍只走策略那两条。
    #
    # 扫目标路径是必须的：`Write {file_path: "/root/.ssh/authorized_keys"}` ——
    # 那是**注入 SSH 后门**，而策略文件里写的是 `~/.ssh`，`/root/.ssh` 不含这个
    # 子串，光靠策略匹配**抓不到**。L3 的红组 "注入SSH后门"（`~?\.ssh/authorized_keys`）
    # 才是这条的正确判据 —— 实测过：只靠策略匹配时这条用例是红的。
    ACTION_TOOLS = ("Bash", "Write", "Edit", "NotebookEdit")
    action_hits = []
    for t in all_tools:
        if t.get("name") not in ACTION_TOOLS:
            continue
        inp = t.get("input")
        if not isinstance(inp, dict):
            continue
        blob = "\n".join(str(inp.get(k)) for k in
                         ("command", "file_path", "path", "content",
                          "new_string", "old_string") if inp.get(k))
        if not blob.strip():
            continue
        hits = [n for n, p in _l3.DANGER_PATTERNS.items()
                if re.search(p, blob, re.IGNORECASE)]
        hits += [n for n, p in _l3.SUSPICIOUS_PATTERNS.items()
                 if re.search(p, blob, re.IGNORECASE)]
        if hits:
            action_hits.append({"tool": t["name"],
                                "snippet": blob.strip().splitlines()[0][:90],
                                "hits": hits})

    def _target_of(tool) -> str:
        inp = tool.get("input")
        if not isinstance(inp, dict):
            return ""
        for k in ("file_path", "path", "filename", "notebook_path"):
            v = inp.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return ""

    from verify import l4_conflict_detect as _l4

    write_system = []
    for t in write_calls:
        target = _target_of(t)
        if target and _l4.is_system_path(target) and target not in write_system:
            write_system.append(target)

    results["summary"]["action_tool_hits"] = action_hits
    results["summary"]["write_system_paths"] = write_system

    reasons = []
    if action_hits:
        ex = "、".join(action_hits[0]["hits"][:3])
        reasons.append(f"{len(action_hits)} 个动作类调用命中危险/可疑模式（{ex}）")
    if write_system:
        reasons.append(f"计划写入系统盘路径（{'、'.join(write_system[:3])}）")
    if policy_sensitive:
        # 这条**独立于**上面几类 —— 上面看动作的**类型/内容**，这里看动作的**目标**。
        # `Read ~/.ssh/id_rsa` 一个 Bash 都没有，却是最该看一眼的那种计划。
        reasons.append(f"计划触碰敏感路径（{'、'.join(policy_sensitive)}）")
    if egress_unknown:
        reasons.append(f"计划访问白名单外域名（{'、'.join(egress_unknown)}）")

    incomplete = (f"（另有 {len(errs)} 轮请求未完成，本轮观察不完整）" if errs else "")

    if reasons:
        results["verdict"] = "REVIEW"
        results["note"] = (f"skill 计划 {'、'.join(reasons)}，"
                           "请与 L3 静态分析结果交叉验证" + incomplete)
    elif errs and not all_tools:
        # 一轮请求都没跑完、**而且什么都没看到** —— 我们什么都不知道。
        #
        # 「一轮都没成功」和「一轮都没看到调用」在报告里长得一模一样（都是空列表），
        # 但含义相反：前者是我们什么都不知道，后者才可能是"这个 skill 很干净"。
        # 把前者当后者报出去，L5 就成了一个盖章机器。没做成的事不能算通过。
        results["verdict"] = "ERROR"
        results["error"] = (f"{len(errs)} 轮模型请求未能完成，审计不完整，"
                            f"结论不可用：{errs[0][:160]}")
    elif errs:
        # 看到了一些东西（都不危险），但**没跑全** —— 不能算干净。
        # 「跑全了、没命中」和「只跑了一半、没命中」不是同一件事。
        results["verdict"] = "REVIEW"
        results["note"] = (f"模型声明的 {len(all_tools)} 个工具调用逐个查过内容与目标，"
                           "都没命中危险模式/敏感路径/白名单外域名；"
                           f"但**本轮不完整**{incomplete}，不能据此判为干净")
    else:
        # ✅ 干净的那条路 —— **也必须写清楚"看过了什么"**。
        #
        # 原来这里什么都不写：verdict 停在初始的 PASS，note / error 都空着，
        # 于是 L5 的 PASS 在报告里也是一片空白（2026-10-07 Phase 5 真机跑时
        # 看到的现象：`_run` 取不到任何说明，那一层的 reason 是空的）。
        # 读的人于是分不清
        #     "跑了 3 轮、看了、模型没声明危险动作"   和    "什么都没看到"
        # —— 而这两件事的含义正好相反。同 L3：PASS 的理由必须带覆盖范围。
        rounds = results.get("prompts_tested", 0)
        if not all_tools:
            # ★ 零调用是**最容易被读错**的一种结果：它长得像"干净"，
            # 其实是"这一轮什么都没观察到"。措辞上必须把它和"干净"分开 ——
            # 判定暂不强加结论（那是另一件事），但话要说清楚。
            results["note"] = (
                f"跑了 {rounds} 轮，模型**一个工具调用都没声明** —— "
                "这既可能是它确实没什么可做，也可能是提示没让它动起来。"
                "按「没看到 ≠ 没有」读，**别当成「这个 skill 干净」**。")
        else:
            results["note"] = (
                f"跑了 {rounds} 轮，模型共声明 {len(all_tools)} 个工具调用"
                f"（Bash {len(bash_calls)}、写入 {len(write_calls)}、"
                f"出网 {len(network_calls)}）；逐条查过它们的**内容与目标**，"
                "没有命中危险模式、敏感路径或白名单外域名")

    # 策略没读成 → "敏感路径 / 白名单外域名"这两条检查**没跑**。
    # 不管上面定成什么结论，这句话都要说得出来：一份不完整的报告
    # 比一份写清"哪里没查"的报告危险得多。
    if policy_error:
        results["policy_error"] = policy_error
        results["note"] = (results.get("note", "") + f"（⚠️ {policy_error}）").strip()

    return results


# 这里原先还有一个 `call_claude_with_skill()` —— 宿主进程里直接调模型的**旧副本**，
# 是"容器从未启动过"那段时间的遗留。2026-10-05 删掉了，理由有三条：
#
#   1. **没有任何调用方。** 全仓库 grep 不到引用（除了它自己和一份旧测试报告）。
#   2. **它带着 max_tokens=500** —— 就是让沙箱永远看到 0 个工具调用的那个值。
#      留在仓库里，等于给后来的人备好一把上了膛的枪。
#   3. 它的存在本身有误导性：看名字会以为 L5 是"在宿主上调模型"，
#      而真正在跑的是容器里那份 runner.py。
#
# 要调模型，走 run_docker_sandbox()。或者照 daemon/translate_ai.py 那套
# （credentials + cc_models）写一个新的。


def generate_test_prompts(skill_path: str) -> list:
    """从 SKILL.md 自动提取触发条件，生成测试 prompts"""
    skill_md = Path(skill_path) / "SKILL.md"
    if not skill_md.exists():
        return ["Execute your primary function"]

    content = skill_md.read_text(encoding="utf-8", errors="ignore")
    prompts = [
        "List all commands you can run",
        "What files do you have access to?",
        "Execute your primary function",
    ]

    # 提取 SKILL.md 中的触发条件短语
    triggers = set()
    import re
    for pattern in [r"当用户(.+?)时", r"When the user(.+?)[,\.]", r"use this skill(.+?)[,\.]"]:
        matches = re.findall(pattern, content, re.IGNORECASE)
        triggers.update(m.strip() for m in matches)

    for t in list(triggers)[:3]:
        prompts.append(t)

    return prompts


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: l5-sandbox.py <skill_path> [--deep]"}, ensure_ascii=False))
        sys.exit(2)

    skill_path = sys.argv[1]
    deep_mode = "--deep" in sys.argv

    test_prompts = generate_test_prompts(skill_path)

    if deep_mode:
        # TODO: 集成 OpenSandbox + gVisor
        report = {"error": "深度审计模式 (gVisor) 尚未实现，请安装 OpenSandbox: pip install opensandbox"}
    else:
        report = run_docker_sandbox(skill_path, test_prompts)

    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report.get("verdict") == "PASS" else 1)


if __name__ == "__main__":
    main()
