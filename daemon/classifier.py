#!/usr/bin/env python3
"""
分类器和排序器 — 将拉取到的 skill 按规则分类，按热度排序
"""
import json
import sys
import re
from collections import defaultdict

CATEGORIES = {
    "前端开发": ["react", "vue", "tailwind", "css", "component", "ui", "frontend", "design", "html",
              "svelte", "angular", "next", "nuxt", "layout", "style", "responsive", "animation"],
    "后端开发": ["api", "database", "sql", "server", "backend", "go", "rust", "python", "node",
              "express", "fastapi", "django", "graphql", "rest", "orm", "redis", "postgres"],
    "DevOps": ["docker", "kubernetes", "deploy", "ci/cd", "terraform", "aws", "cloud", "k8s",
               "ansible", "helm", "jenkins", "github actions", "pipeline", "infrastructure"],
    "代码质量": ["test", "lint", "review", "refactor", "debug", "quality", "tdd", "coverage",
               "unit test", "e2e", "jest", "pytest", "sonar", "format", "prettier"],
    "文档写作": ["doc", "readme", "write", "article", "blog", "documentation", "markdown",
               "changelog", "wiki", "guide", "tutorial"],
    "工作流": ["git", "workflow", "pr", "issue", "branch", "commit", "agile", "scrum",
               "kanban", "jira", "project management", "release", "version"],
    "安全审计": ["security", "audit", "vuln", "secret", "scan", "auth", "oauth",
               "encrypt", "ssl", "tls", "firewall", "penetration", "compliance"],
    "数据科学": ["data", "ml", "ai", "notebook", "pandas", "visualization", "chart",
               "graph", "analytics", "statistics", "numpy", "tensorflow", "pytorch"],
}


def classify_skill(skill: dict) -> str:
    """根据 skill 的名称和描述分类（使用词边界匹配避免子串误判）"""
    text = (
        (skill.get("name") or "") + " " +
        (skill.get("description") or "") + " " +
        (skill.get("full_name") or "")
    ).lower()

    for category, keywords in CATEGORIES.items():
        for kw in keywords:
            # 使用 \b 词边界匹配，避免子串误判
            # 例如 "ui" 不应匹配 "build"，"orm" 不应匹配 "format"
            if re.search(r'\b' + re.escape(kw) + r'\b', text):
                return category

    return "其他"


def compute_score(skill: dict) -> float:
    """计算综合热度分数"""
    stars = skill.get("stars", 0) or 0
    installs = skill.get("installs", 0) or skill.get("downloads", 0) or 0
    return stars * 0.7 + installs * 0.3


def classify_and_sort(skills: list) -> dict:
    """分类 + 排序，返回结构化结果"""
    categorized = defaultdict(list)

    for skill in skills:
        skill["category"] = classify_skill(skill)
        skill["score"] = compute_score(skill)
        categorized[skill["category"]].append(skill)

    # 每个分类内部按分数降序
    for cat in categorized:
        categorized[cat].sort(key=lambda s: s["score"], reverse=True)

    # 统计
    new_skills = [s for s in skills if s.get("status") == "new"]
    update_skills = [s for s in skills if s.get("status") == "update_available"]

    return {
        "generated": None,  # 由调用者填充
        "total": len(skills),
        "new_count": len(new_skills),
        "update_count": len(update_skills),
        "categories": {cat: skills_list for cat, skills_list in categorized.items()},
    }


if __name__ == "__main__":
    data = json.loads(sys.stdin.read())
    result = classify_and_sort(data.get("skills", data))
    print(json.dumps(result, ensure_ascii=False, indent=2))
