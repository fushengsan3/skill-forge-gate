#!/usr/bin/env python3
"""功能板块（学术 / 文本 / 代码）的归属判定与高星排行测试。

## 这个文件钉的是什么

板块是**给用户挑东西用的横向视角**，和 `classifier.py` 的类目是两套东西
（类目单标签、先命中先归类；板块多标签、独立关键词）。所以这里要钉住
几件容易退化的性质：

  1. **多标签**：一条记录可以同时属于三个板块，不能被某个板块"抢走"
  2. **词首前缀匹配**：`paper` 要命中 "papers"（复数），
     但 `test` 不能命中 "latest"、`code` 不能命中 "encode"（词边界）
  3. **排行的 count 与 items 是两个数**：count 是全量、items 是前 N 条。
     混成一个数会让面板把"前 8 个"说成"一共 8 个"
  4. **排序稳定**：同星时必须按名字兜底，否则两轮跑出来的榜单顺序不同，
     "上期第 3 名这期不见了"就变成假象

用法：
    python tests/test_sections.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from daemon.sections import (  # noqa: E402
    SECTIONS, TOP_N, build_rankings, sections_of, tag_skills,
)

results = []


def check(ok, label, detail=""):
    results.append((label, bool(ok)))
    print(("  [PASS] " if ok else "  [FAIL] ") + label + (f" — {detail}" if detail else ""))


def finish():
    failed = [l for l, ok in results if not ok]
    print()
    print(f"总计 {len(results)} 项，失败 {len(failed)} 项")
    if failed:
        for l in failed:
            print("  ✗ " + l)
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


def main():
    print("=" * 60)
    print("功能板块测试（学术 / 文本 / 代码）")
    print("=" * 60)

    # ---- 1. 三个板块都在，且各有搜索词 ------------------------------
    print("\n--- 1. 板块定义 ---")
    for name in ("学术相关", "文本相关", "代码相关"):
        check(name in SECTIONS, f"板块「{name}」已定义")
    for name, cfg in SECTIONS.items():
        check(bool(cfg.get("keywords")), f"「{name}」有关键词")
        check(bool(cfg.get("search_queries")), f"「{name}」有搜索词")

    # ---- 2. 归属判定：多标签 ---------------------------------------
    print("\n--- 2. 归属判定（多标签）---")
    multi = {"name": "kitchen-sink",
             "description": "papers citation engine, code refactor tools, text writing helper"}
    got = sections_of(multi)
    check(set(got) == {"学术相关", "文本相关", "代码相关"},
          "一条真正横跨三个方向的记录同时归入三个板块", str(got))
    # 每个板块只蹭到一个词 → 一个板块都不够格（2026-10-15 收紧后的行为）
    thin = {"name": "kitchen-sink", "description": "papers, code, and text, all of it"}
    check(sections_of(thin) == [],
          "每个板块只蹭到一个词 → 都不归入（宁可漏，不要被营销文案污染）",
          str(sections_of(thin)))

    check(sections_of({"name": "arxiv-search", "description": "Search arXiv papers"}) == ["学术相关"],
          "纯学术记录只归学术")
    check(sections_of({"name": "code-reviewer", "description": "Review code"}) == ["代码相关"],
          "纯代码记录只归代码")
    check(sections_of({"name": "nothing", "description": "deploy things"}) == [],
          "无关记录不归任何板块（返回空列表，不是 None）")

    # 顺序必须按 SECTIONS 的书写顺序，面板分组才稳定
    order = list(SECTIONS.keys())
    idx = [order.index(x) for x in got]
    check(idx == sorted(idx), "多标签的返回顺序与 SECTIONS 书写顺序一致", str(got))

    # ---- 3. 词首前缀 + 词边界（这是自检时真踩到的坑）-----------------
    print("\n--- 3. 词边界与词形（关键词放在**名字**里，命中一次即达标）---")
    check("学术相关" in sections_of({"name": "papers-tool"}),
          "`paper` 命中复数 papers（整词相等会漏掉它）")
    check("学术相关" in sections_of({"name": "citation-manager"}),
          "`citation` 命中")
    check("文本相关" in sections_of({"name": "summarizer"}),
          "`summar` 命中 summarizer（词首前缀）")
    check("代码相关" not in sections_of({"name": "latest-widget"}),
          "`test` 不命中 latest（词边界）")
    check("代码相关" not in sections_of({"name": "encoder"}),
          "`code` 不命中 encode（词边界）")
    check("学术相关" in sections_of({"name": "论文检索工具"}),
          "中文关键词子串匹配可用（中文没有词边界，只能子串）")

    # ---- 3b. 位置加权：这是本次修误报的核心 ------------------------
    print("\n--- 3b. 名字加权(2分) vs 描述(1分) ---")
    lone = {"name": "my-tools", "description": "search papers"}
    check(sections_of(lone) == [],
          "★ 只在描述里蹭到一个关键词 → 不归入（原先会误报）", str(sections_of(lone)))
    check("学术相关" in sections_of({"name": "my-tools",
                                     "description": "papers and citation tools"}),
          "描述里命中两个**不同**关键词 → 归入")
    real = {"name": "hinge-profile-optimizer",
            "description": "Research-backed Hinge profile optimizer for ChatGPT"}
    check(sections_of(real) == [],
          "★ 实测误报案例：交友资料优化器不再进学术板块", str(sections_of(real)))
    check("学术相关" in sections_of({"name": "paper-search-pro", "description": "any text"}),
          "★ 同名真实案例：paper-search-pro 仍进学术板块（名字命中）")

    # ---- 3c. 产品名不算命中（宿主工具名里含有我们的关键词）------------
    print("\n--- 3c. 产品名（Claude Code / Codex）要被抹掉 ---")
    real2 = {"name": "paper-search-pro",
             "description": "Academic literature discovery as a Skill — Claude Code · Codex · any agent"}
    check("代码相关" not in sections_of(real2),
          "★ 实测误报案例：描述里提宿主工具不再算代码命中", str(sections_of(real2)))
    check("代码相关" in sections_of({"name": "code-reviewer", "description": "for Claude Code"}),
          "抹掉产品名后，真正的 code 用法照常命中（名字里的 code-reviewer）")
    check("代码相关" in sections_of({"name": "tool", "description": "code review and codebase refactor"}),
          "描述里两处真实的 code 用法仍然够格")

    # ---- 4. 大小写 ------------------------------------------------
    print("\n--- 4. 大小写 ---")
    check(sections_of({"name": "ARXIV-SEARCH"}) == ["学术相关"],
          "大小写不影响判定")

    # ---- 5. tally / 排行 ------------------------------------------
    print("\n--- 5. 高星排行 ---")
    rows = [
        {"name": "paper-low", "description": "tool", "stars": 5},
        {"name": "paper-high", "description": "tool", "stars": 900},
        {"name": "paper-mid", "description": "tool", "stars": 50},
        {"name": "nomatch", "description": "deploy", "stars": 9999},
    ]
    tag_skills(rows)
    rank = build_rankings(rows)
    ac = rank["学术相关"]
    check(ac["count"] == 3, "count 数的是**全量**，不是前 N 条", str(ac["count"]))
    check([x["name"] for x in ac["items"]] == ["paper-high", "paper-mid", "paper-low"],
          "items 按 stars 降序", str([x["name"] for x in ac["items"]]))
    check("nomatch" not in [x["name"] for x in ac["items"]],
          "不属于该板块的记录不会混进榜单")
    check(rank["代码相关"]["count"] == 0 and rank["代码相关"]["items"] == [],
          "空板块给出 count=0 与空 items（面板据此整块不渲染）")

    # ---- 6. 排序稳定性（同星）-------------------------------------
    print("\n--- 6. 排序稳定性 ---")
    tie = [{"name": "paper-b", "description": "", "stars": 10},
           {"name": "paper-a", "description": "", "stars": 10}]
    r1 = [x["name"] for x in build_rankings(tag_skills(tie))["学术相关"]["items"]]
    r2 = [x["name"] for x in build_rankings(tag_skills(list(reversed(tie))))["学术相关"]["items"]]
    check(r1 == r2, "同星时顺序稳定（按名字兜底，不随输入顺序变）", f"{r1} vs {r2}")

    # ---- 7. 垃圾输入不炸 ------------------------------------------
    print("\n--- 7. 健壮性 ---")
    check(build_rankings([])["学术相关"]["count"] == 0, "空输入不炸")
    check(build_rankings([None, 42, "x"])["文本相关"]["count"] == 0,
          "非 dict 元素被跳过，不抛异常")
    check(sections_of({"description": None}) == [], "description 为 None 不炸")
    weird = build_rankings([{"name": "paper-w", "description": "", "stars": "not-a-number"}])
    check(weird["学术相关"]["count"] == 1, "stars 是非数字时不炸（按 0 处理）")
    big = build_rankings([{"name": f"paper-{i}", "description": "", "stars": i}
                          for i in range(50)])
    check(len(big["学术相关"]["items"]) == TOP_N,
          f"榜单最多列 TOP_N={TOP_N} 条，count 仍报全量",
          f"{len(big['学术相关']['items'])} / {big['学术相关']['count']}")

    # ---- 8. 模板里确实接上了 --------------------------------------
    print("\n--- 8. 面板接线（静态检查）---")
    tpl = (ROOT / "panel" / "fallback.html").read_text(encoding="utf-8")
    check("id=\"sections-panel\"" in tpl, "面板里有板块容器 #sections-panel")
    check("const SECTIONS_DATA = DISCOVER_DATA._sections" in tpl, "面板读 _sections")
    check("function renderSections()" in tpl, "renderSections() 已定义")
    check("renderSections();" in tpl, "renderSections() 被调用")
    check("'section':" in tpl, "SOURCE_CONFIG 里有 section 源（否则评级列会显示错的东西）")
    bridge = (ROOT / "daemon" / "queue_bridge.py").read_text(encoding="utf-8")
    check('"/updates"' in bridge, "bridge 有 GET /updates 路由")
    check('"/update"' in bridge, "bridge 有 POST /update 路由")

    return finish()


if __name__ == "__main__":
    sys.exit(main())
