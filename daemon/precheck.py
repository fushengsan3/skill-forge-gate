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

## 判定规则（面板路径无人值守，所以从严）

| 层的结论 | 处理 |
|---|---|
| `REJECT` / `REVIEW` | **拒绝安装** |
| `WARN` / `PASS` | 放行，但把警告一并带回去 |
| 静态层跑出异常 | **拒绝** —— 本该总能跑起来的东西跑不起来，等于没验 |
| 外部层异常 / 前置缺失 | 跳过，如实标注（没装 Docker 不是这个 skill 的错） |

**为什么连 REVIEW 也拒**：REVIEW 的定义就是"需要人工看一眼"。面板路径上没有那个
"人"。所以它退回给用户，让用户走 Claude Code 那条路 —— 那里 Claude 会读完整报告、
会问、会解释。这不是"更严"，是**承认这条路径没有资格替人做决定**。

对比：`WARN` 放行。L1 对"目录里只有 SKILL.md"这种纯结构提示也给 WARN，
把它当危险信号会让大量正常 skill 装不上。

## 代价

安装变慢。L2 要打一次 GitHub API，L5 在条件齐备时要起 Docker 调模型。
L5 的前置（Docker / ANTHROPIC_API_KEY）会先探一次，缺了就直接跳过 ——
不然会白等 docker build 的超时。
"""
import os
import subprocess
from pathlib import Path

SKILLS_DIR = Path.home() / ".claude" / "skills"

# 默认：这两种结论挡住安装
BLOCKING_VERDICTS = frozenset({"REJECT", "REVIEW"})

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


def _docker_available() -> bool:
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=True)
        return True
    except Exception:
        return False


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

    layer = {
        "name": name,
        "kind": kind,
        "verdict": str(result.get("verdict", "ERROR")),
        "reason": str(result.get("reason", ""))[:300],
    }
    # 把 L3 的红黄计数带出来 —— 这是用户最想看到的那两个数
    summary = result.get("summary")
    if isinstance(summary, dict):
        layer["counts"] = {k: v for k, v in summary.items()
                           if k in ("red_count", "yellow_count", "scanned_files")}
    return layer


def _skip(name: str, why: str) -> dict:
    return {"name": name, "kind": EXTERNAL, "verdict": "SKIPPED", "reason": why}


def _l5(repo_dir: Path, l5) -> dict:
    """L5 沙箱。前置不齐就先跳过，别去等 docker build 的超时。"""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return _skip("L5 沙箱", "未配置 ANTHROPIC_API_KEY，容器里没法调模型 —— 跳过（不是这个 skill 的问题）")
    if not _docker_available():
        return _skip("L5 沙箱", "Docker 不可用 —— 跳过（不是这个 skill 的问题）")
    return _run("L5 沙箱", EXTERNAL, lambda: l5.run_docker_sandbox(
        str(repo_dir), l5.generate_test_prompts(str(repo_dir))))


def verify_repo(repo_dir, url: str, skills_dir=None) -> dict:
    """对**已经 clone 到本地**的仓库跑 L1–L5。

    返回 {"ok": bool, "layers": [...], "blocked_by": [层名], "summary": str}。

    clone 之前不跑是因为 L1/L3/L4 都要读文件。早失败那一步由 safe_paths 的名字
    校验负责（它在 clone 之前）。
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
        _run("L2 来源", EXTERNAL,
             lambda: l2_source.check_source(url)),
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
    """一句话说清楚结论。这句话会出现在日志、HTTP 响应和面板上。"""
    blocked = blocked if blocked is not None else [
        L for L in layers if L["verdict"] in BLOCKING_VERDICTS]
    if blocked:
        head = blocked[0]
        more = f"，另有 {len(blocked) - 1} 层也拦了" if len(blocked) > 1 else ""
        return f"{head['name']} 未通过（{head['verdict']}）：{head['reason']}{more}"

    done = [L for L in layers if L["verdict"] not in ("SKIPPED",)]
    skipped = [L["name"] for L in layers if L["verdict"] == "SKIPPED"]
    warned = [L["name"] for L in layers if L["verdict"] == "WARN"]
    text = f"{len(done)} 层检查通过"
    if warned:
        text += f"，{len(warned)} 层有警告（{'、'.join(warned)}）"
    if skipped:
        text += f"，{len(skipped)} 层跳过（{'、'.join(skipped)}）"
    return text


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 3:
        print("用法: python -m daemon.precheck <已clone的目录> <仓库URL>")
        sys.exit(2)
    print(json.dumps(verify_repo(sys.argv[1], sys.argv[2]), ensure_ascii=False, indent=2))
