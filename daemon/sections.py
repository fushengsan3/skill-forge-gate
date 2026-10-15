#!/usr/bin/env python3
"""功能板块 —— 学术 / 文本 / 代码 三个板块的定义、归属判定与高星排行。

## 为什么另起一套，而不是复用 `daemon/classifier.py` 的类目

`classifier.py` 是**单标签**、**先命中先归类**的：`classify_skill` 一旦命中
某个类目就 `return`（见 classifier.py 里 `for category, keywords in ...` 那一行），
所以一个既写 "react" 又写 "pandas" 的 skill 只会落进"前端开发"。

板块要的不是这个。板块是**给用户挑东西用的横向视角**：
"我想找一个跟论文有关的工具" —— 它不该被"前端开发优先"吃掉。
所以这里：

  · **多标签**：一个 skill 可以同时属于学术和文本（论文检索 + 摘要生成）
  · **独立关键词**：不看 classifier 的类目，直接对 name/description 匹配
  · **不改 classifier 的行为**：现有 8 个类目和它的测试原样不动

两套并存是**有意**的，不是没收拾干净：类目回答"这属于哪个技术方向"，
板块回答"我想干这类事该看哪些"。它们本来就该给出不同答案。

## 为什么排行按 stars

用户要的是"高星项目"。`stars` 是这个仓库里唯一**外部可验证**的热度信号
（`installs` 只有部分源有，`score` 是本地算的复合值）。所以板块排行按
stars 降序，不看 score —— 免得读者以为那个分数有什么外部含义。

⚠️ 注意 `stars` **不保证存在**：claudskills 源根本没有 stars
（见 daemon/fetcher.py 里那段说明），`normalize_skill` 会兜底成 0。
所以无星源的条目会全部排在末尾 —— 这是如实反映，不是 bug。
"""
import re

# ⚠️ 板块的 `keywords` 与 `search_queries` 是**两个用途**，别混：
#   keywords        用来判断"一条已有记录属不属于这个板块"（本地匹配，零成本）
#   search_queries  用来去 GitHub 搜**更多**这个方向的项目（要花 API 配额）
# 前者可以很长（中文词也能收），后者必须短而英文（GitHub 搜索对中文几乎无召回）。
SECTIONS = {
    "学术相关": {
        "icon": "🎓",
        "keywords": [
            "paper", "arxiv", "academic", "citation", "cite",
            "literature", "bibliography", "scholar", "preprint", "journal",
            "论文", "文献", "学术", "引用", "综述", "科研",
        ],
        # 每条 query 会被单独搜一次（带翻页），所以条数直接等于配额开销。
        "search_queries": ["paper", "arxiv", "academic research", "citation", "literature review"],
    },
    "文本相关": {
        "icon": "📝",
        "keywords": [
            "text", "writing", "writer", "translate", "translation", "summar",
            "document", "docx", "markdown", "pdf", "ocr", "proofread",
            "文本", "写作", "翻译", "摘要", "文档", "校对", "排版",
        ],
        "search_queries": ["text processing", "writing", "translation", "summarization", "document"],
    },
    "代码相关": {
        "icon": "💻",
        "keywords": [
            "code", "coding", "refactor", "debug", "lint",
            "git", "compiler", "parser",
            "代码", "编程", "调试", "重构", "编译",
        ],
        "search_queries": ["code review", "refactoring", "debugging", "unit testing", "static analysis"],
    },
}

# ⚠️ 2026-10-15 从关键词表里**删掉**的词，别再好心加回来：
#   research  —— 满地的营销文案写 "Research-backed"（实测：一个交友资料优化器
#                因此进了学术板块）。它描述的是"我们做过功课"，不是"这工具做研究"。
#   api / sdk —— 几乎每个开发工具都会提，命中率极高、区分度为零。
#   test / testing —— "battle-tested"、"fully tested" 是形容词，不是用途。
# 这跟 L3 那次校准是同一个病：**关键词表里混进了装饰性词**，
# 命中的是"提到"而不是"在做"（见 verify/l3_content_scan.py 顶部那段）。
#
# 光删词不够 —— "research paper tool" 这种描述仍然会命中 paper。
# 所以再加一层**位置加权**，见下面的 NAME_HIT / DESC_HIT。

# 归属判定的加权规则（2026-10-15）。
#
# 为什么按位置给不同的分：仓库**名字**是作者对"这东西是干什么的"的声明，
# 描述是给搜索引擎和人类看的宣传语 —— 两者可信度差一个量级。
# 实测：`hinge-profile-optimizer`（交友资料优化器）的描述里写着
# "Research-backed"，按纯词匹配就进了学术板块。
NAME_HIT = 2
DESC_HIT = 1
MIN_SCORE = 2      # 名字命中一次即达标；描述则要命中**两个不同**的关键词

# 匹配前先抹掉的**产品名**（2026-10-15）。
#
# 为什么需要这个：这些产品名里恰好含有我们的关键词，而**几乎每条 skill 描述
# 都会提宿主工具** —— "for Claude Code · Codex · Cursor"。于是 `\bcode` 变成
# 一个白送的命中：实测 `paper-search-pro`（学术文献检索）就是靠描述里的
# "Claude Code · Codex" 混进了代码板块。
#
# 抹掉之后，`code` 在 "claude code" 里不再计入，但真正的用法
# （"code review" / "codebase" / "written in code"）照常命中。
# 只抹**短语**而不是删掉 `code` 关键词本身 —— 后者会连带丢掉
# "code-reviewer" 这类名字，那才是真正的代码工具。
_STOP_PHRASES = (
    "claude code", "claude-code", "claudecode",
    "codex", "vs code", "vscode",
)


def _strip_products(text: str) -> str:
    for phrase in _STOP_PHRASES:
        text = text.replace(phrase, " ")
    return text

# 每板块在高星榜里最多列几条。用户说"分别列出三项功能的高星项目"，
# 没有给条数 —— 8 条足够看出"这个板块有什么好东西"，又不至于把面板拉得很长。
TOP_N = 8

# 拉丁词用**词首前缀**匹配（`\bpaper`），中文只能子串匹配。
#
# 为什么是"词首前缀"而不是"整个 token 相等"：
#   描述里 papers / citations / documents 比单数还常见，
#   summary / summarize / summarization 更是同一个词根三种形态 ——
#   要求整词相等就得把每种变形都列进 keywords，列不全就**静默漏掉**
#   （自检时实测到过：`paper` 匹配不上 "papers"，一条同时属于学术和代码的
#   记录只归进了代码）。
# 为什么前缀前面要 `\b` 而不是裸子串：
#   裸子串会让 `test` 命中 "latest"、`code` 命中 "encode"。
_LATIN = re.compile(r"[a-z0-9]")


def _compile_patterns() -> dict:
    out = {}
    for name, cfg in SECTIONS.items():
        pats = []
        for kw in cfg["keywords"]:
            body = re.escape(kw)
            pats.append(re.compile(r"\b" + body) if _LATIN.search(kw) else re.compile(body))
        out[name] = pats
    return out


# 预先编译：每条记录都要对每个关键词试一次，现场 compile 是白费。
_PATTERNS = _compile_patterns()


def _fields(skill: dict):
    """返回 (名字文本, 描述文本)，都已小写。

    刻意分成两段 —— 判定要给它们不同权重（见 NAME_HIT / DESC_HIT）。
    拼成一段就没法区分"名字里写了什么"和"描述里蹭了一嘴"。
    """
    name = " ".join([
        skill.get("name") or "",
        skill.get("full_name") or "",
    ]).lower()
    desc = (skill.get("description") or "").lower()
    # 产品名先抹掉再匹配，否则 "for Claude Code · Codex" 会白送一次代码命中
    return _strip_products(name), _strip_products(desc)


def sections_of(skill: dict) -> list:
    """这条 skill 属于哪些板块。**多标签** —— 可能同时属于两个。

    判定规则（2026-10-15 起）：
      · 名字里命中一个关键词 → 直接归入（NAME_HIT=2 ≥ MIN_SCORE）
      · 名字没命中 → 描述里要命中**两个不同**的关键词才归入
      · 描述里只蹭到一个词 → **不归入**

    最后一条是关键：`... a Research-backed profile optimizer ...` 这种蹭词
    写法必须挡在门外，否则榜单会被营销文案污染（实测到过）。

    返回顺序按 SECTIONS 的书写顺序（学术 → 文本 → 代码），
    这样面板上的分组顺序稳定，不会随 dict 迭代顺序抖动。
    """
    name_text, desc_text = _fields(skill)
    out = []
    for name, pats in _PATTERNS.items():
        score = NAME_HIT if any(p.search(name_text) for p in pats) else 0
        if score < MIN_SCORE:
            score += sum(DESC_HIT for p in pats if p.search(desc_text))
        if score >= MIN_SCORE:
            out.append(name)
    return out


def tag_skills(skills: list) -> list:
    """就地给每条打上 `sections` 字段（多标签，可能是空列表）。"""
    for s in skills:
        if isinstance(s, dict):
            s["sections"] = sections_of(s)
    return skills


def _rank_key(skill: dict):
    """高星排行的排序键。

    `stars` 缺失/非数字时当 0 —— 但不扔 `None` 去比大小（那会 TypeError）。
    同星时用名字兜底，保证顺序**稳定**：否则两次生成的排行榜顺序可能不同，
    "上一期第 3 名这期不见了"会变成假象。
    """
    try:
        stars = int(skill.get("stars") or 0)
    except (TypeError, ValueError):
        stars = 0
    return (-stars, str(skill.get("name") or ""))


def build_rankings(skills: list, top_n: int = TOP_N) -> dict:
    """按板块产出高星榜。

    返回：
        {
          "学术相关": {"icon": "🎓", "count": 37, "items": [ {skill…}, … ]},
          "文本相关": {...},
          "代码相关": {...},
        }

    `count` 是该板块下的**总数**，`items` 只是前 `top_n` 条 ——
    两个数都给出来，面板才能说清"共 37 个，这里列前 8 个"，
    而不是让人以为整个板块只有 8 个。
    """
    buckets = {name: [] for name in SECTIONS}
    for s in skills:
        if not isinstance(s, dict):
            continue
        for name in (s.get("sections") or sections_of(s)):
            if name in buckets:
                buckets[name].append(s)

    out = {}
    for name, cfg in SECTIONS.items():
        items = sorted(buckets[name], key=_rank_key)[:top_n]
        out[name] = {
            "icon": cfg["icon"],
            "count": len(buckets[name]),
            "items": items,
        }
    return out


if __name__ == "__main__":
    # 手动看一眼归属判定合不合理：python -m daemon.sections
    import json
    import sys

    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if raw.strip():
        data = json.loads(raw)
        rows = data if isinstance(data, list) else data.get("skills", [])
    else:
        rows = [
            {"name": "arxiv-search", "description": "Search arXiv papers and cite them"},
            {"name": "code-reviewer", "description": "Review code and refactor"},
            {"name": "doc-summarizer", "description": "Summarize long documents into markdown"},
            {"name": "kitchen-sink", "description": "papers, code, and text, all of it"},
            {"name": "unrelated", "description": "deploy things"},
            # 下面两条是 2026-10-15 实测到的误报与正例，留着当回归样本
            {"name": "hinge-profile-optimizer",
             "description": "Research-backed Hinge profile optimizer for ChatGPT"},
            {"name": "paper-search-pro", "description": "Academic literature discovery"},
        ]
    tag_skills(rows)
    for r in rows:
        print(f"  {r.get('name'):20} → {r.get('sections')}")
    print()
    print(json.dumps({k: {"count": v["count"]} for k, v in build_rankings(rows).items()},
                     ensure_ascii=False))
