#!/usr/bin/env python3
"""`verify/audit_policy.py` —— 策略文件的读取端与两条不变式。

## 这个模块为什么值得单独测

`audit-policy.yaml` 在 2026-10-05 的审计里是「**没有任何代码读它**」（#15）。
现在它被读着了（L3 与 L5 共用同一份），于是有了新的风险：
**读失败时会不会退化成"没有策略"**。

那个退化是这一整个项目最反复踩的坑：`except: return {}` → 检查悄悄关掉 →
报告照样说"未发现危险或可疑模式"。所以这里钉两件事：

1. **读失败必须报到调用方**（`load()` 的第二个返回值非空），不能返回一个空策略
   让调用方以为"没有要盯的东西"。
2. **空列表不是合法策略**。`sensitive_paths: []` 的含义是"什么都不用看" ——
   那是文件被改坏的症状，不是配置。宁可报错。

用法：
    python tests/test_audit_policy.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from verify import audit_policy

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


class with_policy_file:
    """把 POLICY_FILE 指到临时文件上，并清掉 mtime 缓存。"""

    def __init__(self, text=None, missing=False):
        self.text = text
        self.missing = missing

    def __enter__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="sf-ap-"))
        self.real = audit_policy.POLICY_FILE
        target = self.tmp / "audit-policy.yaml"
        audit_policy.POLICY_FILE = target
        if not self.missing:
            target.write_text(self.text, encoding="utf-8")
        audit_policy._cache.update(mtime=None, policy=None, error=None)
        return target

    def __exit__(self, *a):
        audit_policy.POLICY_FILE = self.real
        audit_policy._cache.update(mtime=None, policy=None, error=None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        return False


GOOD = """
audit_rules:
  files:
    sensitive_paths:
      - ~/.ssh/
      - /etc/shadow
  network:
    allowed_targets:
      - api.github.com
      - github.com
"""


def main():
    print("=" * 60)
    print("策略读取端（verify/audit_policy.py）")
    print("=" * 60)

    print("--- 1. 真实的那份策略读得出来、而且不是空的 ---")
    pol, err = audit_policy.load()
    check(err == "", "真实策略文件读成功", err or "（无错误）")
    check(len(pol["sensitive_paths"]) >= 10 and len(pol["allowed_targets"]) >= 5,
          "★ 两份清单都有内容（空清单会被下面第 4 节判成读失败）",
          f"{len(pol['sensitive_paths'])} 路径 / {len(pol['allowed_targets'])} 域名")

    print("--- 2. 归一化：结尾斜杠 / 大小写 / 反斜杠 ---")
    check("~/.ssh" in pol["sensitive_paths"] and "~/.ssh/" not in pol["sensitive_paths"],
          "★ 结尾斜杠被去掉（否则 `~/.ssh` 不带斜杠就匹配不上）",
          str([p for p in pol["sensitive_paths"] if "ssh" in p]))
    hits = audit_policy.find_sensitive_paths("把 ~/.SSH/id_rsa 拷走")
    check(hits == ["~/.ssh"], "★ 大小写不敏感（Windows 上路径本来就不分大小写）", str(hits))
    hits_bs = audit_policy.find_sensitive_paths(r"读 ~\.ssh\id_rsa")
    check(hits_bs == ["~/.ssh"], "★ 反斜杠写法也认（skill 文本里两种都会出现）", str(hits_bs))
    check(audit_policy.find_sensitive_paths("读 ~/.kube/config") == ["~/.kube"],
          "策略里的其它路径同样生效", str(audit_policy.find_sensitive_paths("读 ~/.kube/config")))
    check(audit_policy.find_sensitive_paths("读 README.md 和 src/main.py") == [],
          "无关文本不误报")

    print("--- 3. host_of / is_allowed_host ---")
    cases = [
        ("https://api.github.com/repos/a/b", "api.github.com"),
        ("http://example.com:8080/x?y=1", "example.com"),
        # ⚠️ 刻意**不带** `user:pw@` 那种口令形式：`host_of` 对 `@` 的处理是
        # "取最后一段"，用户名有没有口令走的是同一条路。而带口令的写法会被
        # `test_no_local_leakage.py` 的「连接串里带明文口令」规则命中 ——
        # 那是个**真的**该报的规则，不该为了测试去放宽它。
        ("https://user@evil.example/drop", "evil.example"),
        ("github.com", "github.com"),
        ("https://[::1]:443/x", "::1"),
        ("", ""),
    ]
    for raw, want in cases:
        got = audit_policy.host_of(raw)
        check(got == want, f"host_of({raw!r}) → {want!r}", got)

    check(audit_policy.is_allowed_host("api.github.com"), "白名单里的域名 → 通过")
    check(audit_policy.is_allowed_host("raw.githubusercontent.com"),
          "白名单里显式列出的域名 → 通过")
    check(audit_policy.is_allowed_host("anything.github.com"),
          "★ 子域匹配：`x.github.com` 命中白名单里的 `github.com`")
    check(not audit_policy.is_allowed_host("exfil.example.com"), "白名单外的域名 → 不通过")
    check(not audit_policy.is_allowed_host(""), "空主机 → 不通过（不是「默认放行」）")
    check(not audit_policy.is_allowed_host("notgithub.com"),
          "★ 后缀伪装不算：`notgithub.com` **不**匹配 `github.com`（拼接比较会中招）")

    print("--- 4. ★ fail-loud：读不出来必须报到调用方 ---")
    with with_policy_file(missing=True):
        p4, e4 = audit_policy.load()
        check(e4 != "" and p4["sensitive_paths"] == [],
              "★ 文件不存在 → 返回错误，**不是**一个空荡荡的「没有策略」",
              e4[:70])
    with with_policy_file("这不是: [合法的 yaml: {{{"):
        p5, e5 = audit_policy.load()
        check(e5 != "", "★ yaml 语法坏了 → 返回错误", e5[:70])
    with with_policy_file("audit_rules:\n  files:\n    sensitive_paths: []\n"
                          "  network:\n    allowed_targets: []\n"):
        p6, e6 = audit_policy.load()
        check(e6 != "" and "空" in e6,
              "★ 空清单 → 当成读失败（「空」= 「什么都不用看」，是最危险的默认值）", e6[:70])
    with with_policy_file("audit_rules:\n  files: 这不是个字典\n"):
        p7, e7 = audit_policy.load()
        check(e7 != "", "★ 形状不对 → 报错，不猜", e7[:70])

    print("--- 5. 正常路径仍然读得对（别把守卫做成死角）---")
    with with_policy_file(GOOD):
        p8, e8 = audit_policy.load()
        check(e8 == "" and p8["sensitive_paths"] == ["~/.ssh", "/etc/shadow"],
              "临时策略文件正常读出", f"{e8!r} {p8}")
        check(audit_policy.is_allowed_host("api.github.com"), "白名单照常生效")
        check(audit_policy.find_sensitive_paths("cat /etc/shadow") == ["/etc/shadow"],
              "敏感路径照常命中")

    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
