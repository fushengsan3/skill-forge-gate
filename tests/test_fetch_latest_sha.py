#!/usr/bin/env python3
"""
上游 SHA 拉取 + 更新判定测试。

修的是一个**整块功能失效**的 bug：

  fetcher 从来不产出 `latest_sha`，而 `watchdog.filter_skills` 用它判断
  "已装的 skill 上游有没有新提交"。字段永远不存在 → 那个分支恒假 → 两个后果：

    1. 面板的"可更新"统计、"🔄 有更新"筛选、更新角标全是死的（恒为 0）
    2. 已装 skill 即使上游真有新提交，也会落进"SHA 相同 → 排除"被静默丢掉

全程 mock，不发真实请求。

用法：
    python tests/test_fetch_latest_sha.py
"""
import sys
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from daemon import fetcher
from daemon import watchdog

results = []

# 夹具用**真实形态**的十六进制 SHA —— 别的形状会被 _looks_like_sha 正确拒掉，
# 那样测的就不是"更新判定"而是"形状校验"了。
SHA_A_LOCAL  = "aaaa1111" + "0" * 32
SHA_A_REMOTE = "bbbb2222" + "0" * 32
SHA_B        = "cccc3333" + "0" * 32


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


INSTALLED = {
    "alpha": {"type": "remote", "url": "https://github.com/a/alpha",
              "branch": "main", "installed_sha": SHA_A_LOCAL},
    "beta": {"type": "remote", "full_name": "b/beta",
             "branch": "dev", "installed_sha": SHA_B},
    "self": {"type": "remote", "url": "https://github.com/me/skill-forge",
             "self": True, "installed_sha": SHA_A_LOCAL},
    "broken": {"type": "remote", "url": "不是 github 地址", "installed_sha": SHA_A_LOCAL},
}


def main():
    print("=" * 60)
    print("上游 SHA 拉取 + 更新判定测试")
    print("=" * 60)

    seen_urls = []

    def fake_api_get(url, *a, **k):
        seen_urls.append(url)
        if "/a/alpha/" in url:
            return {"ok": True, "data": {"sha": SHA_A_REMOTE}}
        if "/b/beta/" in url:
            return {"ok": True, "data": {"sha": SHA_B}}   # 没变
        return {"ok": False, "reason": "boom"}

    real = fetcher.api_get
    fetcher.api_get = fake_api_get
    try:
        run_checks(seen_urls)
    finally:
        fetcher.api_get = real

    failed = [n for n, ok in results if not ok]
    print("")
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    print("RESULT: " + ("PASS" if not failed else "FAIL"))
    return 0 if not failed else 1


def run_checks(seen_urls):
    print("--- 1. 只查该查的 ---")
    shas = fetcher.fetch_latest_shas(INSTALLED)
    check(set(shas) == {"alpha", "beta"},
          "★ 只返回查成功的（查不到的不塞空串，让调用方能区分'没更新'和'没查成'）",
          str(shas))
    check(shas.get("alpha") == SHA_A_REMOTE, "alpha 拿到新 SHA", shas.get("alpha"))
    check("self" not in shas, "★ 不查自己（self 条目跳过）")
    check("broken" not in shas, "URL 解析不出 owner/repo 的跳过")
    check(len(seen_urls) == 2, "★ 只发了 2 次 API 请求（不是发现总数）", f"{len(seen_urls)} 次")
    check(any("/commits/dev" in u for u in seen_urls),
          "★ 用 sources.json 里记的 branch，不是写死 main",
          str([u.split("repos/")[-1] for u in seen_urls]))
    check(any("/commits/main" in u for u in seen_urls), "缺 branch 时回退 main")

    print("--- 2. 更新判定：现在真的能判出来了 ---")
    skills = [{"name": "alpha", "url": "https://github.com/a/alpha"},
              {"name": "beta", "url": "https://github.com/b/beta"},
              {"name": "gamma", "url": "https://github.com/c/gamma"}]

    got = watchdog.filter_skills(skills, INSTALLED, shas)
    names = {s["name"]: s.get("status") for s in got}
    check(names.get("alpha") == "update_available",
          "★ alpha 上游变了 → 标 update_available（以前永远标不出来）", str(names))
    check("beta" not in names, "beta 上游没变 → 排除（不占面板）")
    check(names.get("gamma") == "new", "没装过的 → 标 new")
    alpha = [s for s in got if s["name"] == "alpha"][0]
    check(alpha.get("installed_sha") == SHA_A_LOCAL,
          "把本地已装的 SHA 一起带上（面板要显示对比）", alpha.get("installed_sha"))

    print("--- 3. 查不到上游时不能假装'没更新' ---")
    got2 = watchdog.filter_skills(skills, INSTALLED, {})     # 一个都没查成
    names2 = {s["name"]: s.get("status") for s in got2}
    check(names2 == {"gamma": "new"},
          "★ 查不到时保守排除（不误报更新），但 watchdog 会在日志里说明有多少个判断不了",
          str(names2))

    print("--- 4. 向后兼容：skill 自带 latest_sha 仍然认 ---")
    inline = [{"name": "alpha", "latest_sha": SHA_A_REMOTE},
              {"name": "beta", "latest_sha": SHA_B}]
    got3 = watchdog.filter_skills(inline, INSTALLED)
    check([s["name"] for s in got3] == ["alpha"],
          "★ 没有 latest_shas 参数时，仍读 skill 字典里的 latest_sha（老测试靠这个）",
          str([s["name"] for s in got3]))

    print("--- 5. 显式传入的优先于 skill 自带的 ---")
    both = [{"name": "alpha", "latest_sha": SHA_B}]     # 自带的等于本地SHA
    got4 = watchdog.filter_skills(both, INSTALLED, {"alpha": SHA_A_REMOTE})
    check([s["name"] for s in got4] == ["alpha"],
          "★ latest_shas 覆盖 skill 自带的 latest_sha", str([s["name"] for s in got4]))

    print("--- 6. watchdog 真的接上了没有 ---")
    src = (ROOT / "daemon" / "watchdog.py").read_text(encoding="utf-8")
    check("fetch_latest_shas" in src, "★ watchdog 里调用了 fetch_latest_shas")
    check("filter_skills(skills, installed, latest_shas)" in src,
          "★ 把结果传给了 filter_skills（否则查了也白查）")
    check("判断不了更新" in src, "查不到时会记日志说明有多少个判断不了（不静默）")

    print("--- 7. owner/repo 要剥掉 .git（实测踩过）---")
    # sources.json 里存的是 clone URL（.../repo.git），而 GitHub API 路径是
    # /repos/owner/repo —— 带 .git 会 404。真实环境实测：19 个里 10 个因此查不到。
    for url, want in [("https://github.com/wuyoscar/GPT-Image2-Skill.git",
                       "wuyoscar/GPT-Image2-Skill"),
                      ("https://github.com/a/b", "a/b"),
                      ("https://github.com/a/b/tree/main/sub/dir", "a/b"),
                      ("https://github.com/o/r/blob/main/f.md", "o/r")]:
        got = fetcher._extract_github_fullname(url)
        check(got == want, f"★ {url[:46]} → {want}", got)

    print("--- 8. 本地 SHA 不是 SHA 时不能报「有更新」---")
    # 实测 sources.json 里有一条 installed_sha 的值是字面字符串 "installed"。
    # 拿它跟真 SHA 比永远不等 → 那个 skill **每一轮**都被报成有更新，
    # 点进去又更新不了。这是假警报，比漏报更烦人。
    for bad in ["installed", "", "unknown", "  ", "main", "v1.2.3"]:
        got = watchdog.filter_skills(
            [{"name": "alpha"}],
            {"alpha": {"installed_sha": bad}},
            {"alpha": SHA_A_REMOTE})
        check(not got, f"★ 本地 SHA = {bad!r} 时不报更新（判定不了就不报）",
              str([s.get('status') for s in got]))
    got = watchdog.filter_skills(
        [{"name": "alpha"}],
        {"alpha": {"installed_sha": "INSTALLED"}},      # 大写变体
        {"alpha": SHA_A_REMOTE})
    check(not got, "★ 大写 'INSTALLED' 同样不报")

    print("--- 9. 上游 SHA 不是 SHA 时也不报 ---")
    got = watchdog.filter_skills(
        [{"name": "alpha"}],
        {"alpha": {"installed_sha": SHA_A_LOCAL}},
        {"alpha": "not-a-sha"})
    check(not got, "★ 上游返回值不像 SHA 时不报更新", str(got))

    print("--- 10. 真 SHA 的短写/长写能正确比对 ---")
    got = watchdog.filter_skills(
        [{"name": "alpha"}],
        {"alpha": {"installed_sha": "AAAA1111bbbb"}},
        {"alpha": "AAAA1111cccc"})           # 前 8 位相同
    check(not got, "★ 前 8 位相同 → 视为没变（和面板显示粒度一致）", str(got))


if __name__ == "__main__":
    sys.exit(main())
