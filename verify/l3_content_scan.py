#!/usr/bin/env python3
"""
L3 内容安全扫描 — 检测 SKILL.md 及附带脚本中的危险模式
输出 JSON 格式的扫描报告到 stdout
"""
import re
import json
import sys
from pathlib import Path

# ============================================================
# 规则组 A：高危模式 → 标红，建议拒绝安装
# ============================================================
DANGER_PATTERNS = {
    "sudo/doas提权": r"\bsudo\b|\bdoas\b",
    "毁灭性删除": r"rm\s+-rf\s+/(\*|\s|$)|rm\s+-rf\s+~(\s|$)|rm\s+-rf\s+/(dev|etc|usr|var|home|sys|proc)",
    "开放全部权限": r"chmod\s+777",
    "pipe-to-shell": r"curl\s+.*\|\s*(bash|sh|zsh)\b|wget\s+.*\|\s*(bash|sh|zsh)\b",
    "Bash eval注入": r"\beval\b",
    "写裸设备": r">\s*/dev/sd[a-z]|\bdd\s+if=.*of=/dev/",
    "fork炸弹": r":\(\)\s*\{\s*:\|:&\s*\}\s*;:",
    "格式化磁盘": r"\bmkfs\.\b|\bformat\b\s+[A-Z]:",
    "篡改用户系统": r"/etc/passwd|/etc/shadow|/etc/sudoers",
    "注入SSH后门": r"~?\.ssh/authorized_keys",
    "窃取云凭证": r"~?\.aws/|~?\.gcloud/|~?\.azure/|~?\.config/gcloud",
    "监听端口后门": r"\bnc\s+-l\b|\bncat\s+-l\b|\bnetcat\s+-l\b",
    "Python代码执行": r"\bexec\s*\(|\b__import__\s*\(|\bcompile\s*\(.*exec",
}

# ============================================================
# 规则组 B：可疑模式 → 标黄，需人工审核
# ============================================================
SUSPICIOUS_PATTERNS = {
    "网络请求": r"\bcurl\b|\bwget\b",
    "环境变量读取": r"\$(SECRET|TOKEN|KEY|PASSWORD|PASSWD|CREDENTIAL)",
    "强制推送": r"git\s+push\s+--force|git\s+push\s+-f",
    "发布上传": r"\bnpm\s+publish\b|\bpip\s+upload\b|\bdocker\s+push\b|\bcargo\s+publish\b",
    "持久化注入": r"~?\.(bashrc|zshrc|profile|bash_profile)",
    "修改Claude Code配置": r"~?\.claude/settings\.json",
    "DNS劫持": r"/etc/hosts",
    "读取SSH私钥": r"~?\.ssh/id_",
    "删除git仓库": r"rm\s+-rf\s+\.git",
    "修改启动项": r"shell:startup|Startup|/Library/LaunchAgents|systemd.*enable",
    "加密文件操作": r"\bgpg\b|\bopenssl\b.*enc|\bencrypt\b",
}

# ============================================================
# 规则组 C：信息收集 → 标蓝，仅记录
# ============================================================
INFO_PATTERNS = {
    "Bash调用": r"```bash\n(.+?)```|`([^`]+)`",
    "文件写入路径": r"(?:write|save|output|create)\s+(?:to\s+)?([~\w./\\-]+)",
    "网络域名": r"https?://([\w.-]+)",
    "Hook声明": r"(?:PreToolUse|PostToolUse|PreMessage|PostMessage|SessionStart|SessionEnd)",
    "MCP Server": r"mcp.*server|mcp__\w+",
}


def scan_file(filepath: Path) -> dict:
    """扫描单个文件，返回匹配结果"""
    try:
        content = filepath.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return {}

    result = {"file": str(filepath), "findings": {"red": [], "yellow": [], "blue": []}}

    for line_no, line in enumerate(content.split("\n"), 1):
        # 红组
        for name, pattern in DANGER_PATTERNS.items():
            if re.search(pattern, line, re.IGNORECASE):
                result["findings"]["red"].append({
                    "rule": name,
                    "line": line_no,
                    "snippet": line.strip()[:120]
                })

        # 黄组
        for name, pattern in SUSPICIOUS_PATTERNS.items():
            if re.search(pattern, line, re.IGNORECASE):
                result["findings"]["yellow"].append({
                    "rule": name,
                    "line": line_no,
                    "snippet": line.strip()[:120]
                })

    # 蓝组 — 全局搜索
    for name, pattern in INFO_PATTERNS.items():
        matches = re.findall(pattern, content, re.IGNORECASE)
        if matches:
            result["findings"]["blue"].append({
                "rule": name,
                "count": len(matches),
                "samples": matches[:5]
            })

    return result


def compute_verdict(findings_list: list) -> dict:
    """根据所有文件的扫描结果计算最终判决"""
    all_red = []
    all_yellow = []
    all_blue = []

    for f in findings_list:
        all_red.extend(f.get("findings", {}).get("red", []))
        all_yellow.extend(f.get("findings", {}).get("yellow", []))
        all_blue.extend(f.get("findings", {}).get("blue", []))

    if all_red:
        verdict = "REJECT"
        color = "red"
        reason = f"发现 {len(all_red)} 个高危模式，建议拒绝安装"
    elif all_yellow:
        verdict = "REVIEW"
        color = "yellow"
        reason = f"发现 {len(all_yellow)} 个可疑模式，需要人工审核"
    else:
        verdict = "PASS"
        color = "green"
        reason = "未发现危险或可疑模式"

    return {
        "verdict": verdict,
        "color": color,
        "reason": reason,
        "summary": {
            "red_count": len(all_red),
            "yellow_count": len(all_yellow),
            "blue_count": len(all_blue),
        },
        "red": all_red,
        "yellow": all_yellow,
        "blue": all_blue,
    }


def scan_skill(skill_path: str) -> dict:
    """扫描一个 skill 目录下的所有相关文件"""
    root = Path(skill_path)
    target_files = []

    # SKILL.md 必扫
    skill_md = root / "SKILL.md"
    if skill_md.exists():
        target_files.append(skill_md)

    # 附带脚本也扫
    for ext in ["*.sh", "*.py", "*.js", "*.ts", "*.ps1", "*.bat", "*.rb", "*.go"]:
        target_files.extend(root.rglob(ext))

    # 限制扫描范围（排除 .git 和 .backup）
    target_files = [f for f in target_files if ".git" not in f.parts and ".backup" not in f.parts]

    findings = []
    for f in target_files:
        result = scan_file(f)
        if result.get("findings", {}).get("red") or result.get("findings", {}).get("yellow"):
            findings.append(result)

    verdict = compute_verdict(findings)
    verdict["scanned_files"] = len(target_files)
    verdict["files_with_findings"] = len(findings)
    return verdict


def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "Usage: l3-content-scan.py <skill_path>"}, ensure_ascii=False))
        sys.exit(2)

    skill_path = sys.argv[1]
    if not Path(skill_path).exists():
        print(json.dumps({"error": f"Path not found: {skill_path}"}, ensure_ascii=False))
        sys.exit(2)

    report = scan_skill(skill_path)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    sys.exit(0 if report["verdict"] == "PASS" else (1 if report["verdict"] == "REVIEW" else 2))


if __name__ == "__main__":
    main()
