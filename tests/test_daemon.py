#!/usr/bin/env python3
"""
Skill Forge 守护进程测试套件
测试 fetcher, classifier, notifier, watchdog 四大模块
"""
import unittest
import json
import sys
import os
import io
import subprocess
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

# 添加项目根目录
sys.path.insert(0, str(Path(__file__).parent.parent))

# ============================================================
# 辅助：模拟数据
# ============================================================

MOCK_GITHUB_TOPIC_RESPONSE = {
    "total_count": 3,
    "items": [
        {
            "name": "awesome-react-skill",
            "full_name": "dev/awesome-react-skill",
            "description": "A React component builder skill for Claude Code",
            "stargazers_count": 342,
            "html_url": "https://github.com/dev/awesome-react-skill",
            "updated_at": "2026-08-01T12:00:00Z",
        },
        {
            "name": "python-api-generator",
            "full_name": "coder/python-api-generator",
            "description": "Generate FastAPI endpoints from OpenAPI specs",
            "stargazers_count": 215,
            "html_url": "https://github.com/coder/python-api-generator",
            "updated_at": "2026-07-28T08:30:00Z",
        },
        {
            "name": "docker-deploy-skill",
            "full_name": "ops/docker-deploy-skill",
            "description": "Deploy apps with Docker and Kubernetes",
            "stargazers_count": 189,
            "html_url": "https://github.com/ops/docker-deploy-skill",
            "updated_at": "2026-07-20T16:00:00Z",
        },
    ],
}

SKILL_SAMPLES = [
    # 前端开发
    {"name": "react-component-builder", "description": "Build React components with Tailwind CSS", "stars": 500, "installs": 2000, "source": "github-topic"},
    # 后端开发
    {"name": "fastapi-crud-generator", "description": "Generate CRUD APIs with FastAPI and PostgreSQL", "stars": 400, "installs": 1500, "source": "github-topic"},
    # DevOps
    {"name": "k8s-deploy-helper", "description": "Deploy to Kubernetes with Helm charts and Docker", "stars": 300, "installs": 800, "source": "github-topic"},
    # 代码质量
    {"name": "pytest-coverage-runner", "description": "Run pytest with coverage and lint checks", "stars": 250, "installs": 900, "source": "github-topic"},
    # 文档写作
    {"name": "readme-generator", "description": "Auto-generate README and documentation from code", "stars": 180, "installs": 600, "source": "github-topic"},
    # 工作流
    {"name": "git-pr-workflow", "description": "Manage Git branches, PRs, and commit workflows", "stars": 220, "installs": 700, "source": "github-topic"},
    # 安全审计
    {"name": "secret-scanner", "description": "Scan for secrets, vulnerabilities and run security audit", "stars": 350, "installs": 1100, "source": "github-topic"},
    # 数据科学
    {"name": "pandas-data-analyzer", "description": "Analyze data with pandas and create charts", "stars": 160, "installs": 500, "source": "github-topic"},
    # 其他 (无法匹配任何分类)
    {"name": "misc-tool", "description": "A utility tool for miscellaneous tasks", "stars": 50, "installs": 100, "source": "github-topic"},
]


# ============================================================
# Test 1: Fetcher 数据源拉取测试
# ============================================================

class TestFetcher(unittest.TestCase):
    """数据源拉取器测试"""

    def setUp(self):
        """每个测试前设置代理环境变量"""
        os.environ["https_proxy"] = "http://127.0.0.1:7897"
        os.environ["http_proxy"] = "http://127.0.0.1:7897"

    # ---- 连通性测试 (需要代理) ----

    def test_github_api_connectivity(self):
        """验证 GitHub API 基本连通性（通过代理）"""
        from daemon.fetcher import api_get, set_proxy
        set_proxy()
        result = api_get("https://api.github.com")
        self.assertTrue(result["ok"], f"GitHub API 连通失败: {result.get('reason', 'unknown')}")

    def test_jeremylongshore_repo_reachable(self):
        """验证 jeremylongshore 仓库能否访问"""
        from daemon.fetcher import api_get, set_proxy
        set_proxy()
        result = api_get("https://api.github.com/repos/jeremylongshore/claude-code-plugins-plus-skills")
        self.assertTrue(result["ok"], f"jeremylongshore 仓库不可达: {result.get('reason', 'unknown')}")

    def test_jeremylongshore_plugins_list(self):
        """验证 jeremylongshore plugins 目录能否列出"""
        from daemon.fetcher import api_get, set_proxy
        set_proxy()
        result = api_get(
            "https://api.github.com/repos/jeremylongshore/"
            "claude-code-plugins-plus-skills/contents/plugins"
        )
        self.assertTrue(result["ok"], f"plugins 目录列表失败: {result.get('reason', 'unknown')}")
        if result["ok"]:
            self.assertIsInstance(result.get("data"), list,
                                  "plugins 目录应返回列表")

    def test_github_topic_search(self):
        """验证 GitHub Topic 搜索是否正常"""
        from daemon.fetcher import api_get, set_proxy
        set_proxy()
        url = ("https://api.github.com/search/repositories"
               "?q=topic:claude-code-skill&sort=stars&order=desc&per_page=5")
        result = api_get(url)
        self.assertTrue(result["ok"], f"GitHub Topic 搜索失败: {result.get('reason', 'unknown')}")
        if result["ok"]:
            self.assertIn("items", result.get("data", {}),
                          "搜索结果应包含 items 字段")

    def test_claudskills_connectivity(self):
        """验证 ClaudSkills.com 数据源连通性"""
        import urllib.request
        from daemon.fetcher import set_proxy, PROXY
        set_proxy()
        proxy_handler = urllib.request.ProxyHandler({"https": PROXY, "http": PROXY})
        opener = urllib.request.build_opener(proxy_handler)
        url = "https://claudskills.com/data/skills.json"
        req = urllib.request.Request(url, headers={"User-Agent": "skill-forge/1.0"})
        try:
            with opener.open(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
                self.assertTrue(isinstance(data, (list, dict)),
                                "ClaudSkills 数据格式应为 list 或 dict")
        except Exception as e:
            self.skipTest(f"ClaudSkills 不可达（可能服务端不可用）: {e}")

    # ---- 拉取函数测试（用 mock） ----

    @patch("daemon.fetcher.api_get")
    def test_fetch_github_topic_parsing(self, mock_api_get):
        """验证 fetch_github_topic 正确解析 API 响应"""
        mock_api_get.return_value = {"ok": True, "data": MOCK_GITHUB_TOPIC_RESPONSE}
        from daemon.fetcher import fetch_github_topic
        skills = fetch_github_topic()
        self.assertEqual(len(skills), 3)
        self.assertEqual(skills[0]["source"], "github-topic")
        self.assertEqual(skills[0]["stars"], 342)
        self.assertEqual(skills[0]["name"], "awesome-react-skill")

    @patch("daemon.fetcher.api_get")
    def test_fetch_github_topic_api_failure(self, mock_api_get):
        """验证 fetch_github_topic 在 API 失败时返回空列表"""
        mock_api_get.return_value = {"ok": False, "reason": "Network error"}
        from daemon.fetcher import fetch_github_topic
        skills = fetch_github_topic()
        self.assertEqual(skills, [], "API 失败时应返回空列表")

    @patch("daemon.fetcher.api_get")
    def test_fetch_github_topic_missing_items(self, mock_api_get):
        """验证 fetch_github_topic 处理缺失 items 字段"""
        mock_api_get.return_value = {"ok": True, "data": {}}
        from daemon.fetcher import fetch_github_topic
        skills = fetch_github_topic()
        self.assertEqual(skills, [], "无 items 时应返回空列表")

    @patch("daemon.fetcher.api_get")
    def test_search_skills_parsing(self, mock_api_get):
        """验证 search_skills 关键词搜索"""
        mock_api_get.return_value = {"ok": True, "data": MOCK_GITHUB_TOPIC_RESPONSE}
        from daemon.fetcher import search_skills
        skills = search_skills("react", top=5)
        self.assertGreater(len(skills), 0)
        self.assertEqual(skills[0]["source"], "search")

    @patch("daemon.fetcher.api_get")
    def test_search_skills_empty(self, mock_api_get):
        """验证 search_skills 在无结果时返回空列表"""
        mock_api_get.return_value = {"ok": True, "data": {"total_count": 0, "items": []}}
        from daemon.fetcher import search_skills
        skills = search_skills("nonexistent1234", top=5)
        self.assertEqual(skills, [])

    @patch("daemon.fetcher.api_get")
    def test_search_skills_failure(self, mock_api_get):
        """验证 search_skills API 失败时返回空列表"""
        mock_api_get.return_value = {"ok": False, "reason": "Timeout"}
        from daemon.fetcher import search_skills
        skills = search_skills("react", top=5)
        self.assertEqual(skills, [], "搜索失败时应返回空列表")

    # ---- api_get 错误处理 ----

    def test_api_get_invalid_url(self):
        """验证 api_get 对无效 URL 的错误处理"""
        from daemon.fetcher import api_get
        result = api_get("https://invalid.example.nonexistent/api")
        self.assertFalse(result["ok"], "无效 URL 应返回 ok=False")
        self.assertIn("reason", result, "应包含失败原因")

    @patch("urllib.request.OpenerDirector.open")
    def test_api_get_timeout_handling(self, mock_open):
        """验证 api_get 超时处理"""
        from daemon.fetcher import api_get
        import socket
        mock_open.side_effect = socket.timeout("timed out")
        result = api_get("https://httpbin.org/delay/60")
        self.assertFalse(result["ok"], "超时请求应返回 ok=False")

    # ---- 并行拉取 ----

    @patch("daemon.fetcher.fetch_github_topic")
    @patch("daemon.fetcher.fetch_claudskills")
    @patch("daemon.fetcher.fetch_jeremylongshore")
    def test_fetch_all_sources_merging(self, mock_jl, mock_cs, mock_gt):
        """验证 fetch_all_sources 合并三个数据源"""
        mock_jl.return_value = [
            {"name": "skill-a", "stars": 100, "url": "https://github.com/a/a", "source": "jeremylongshore"},
        ]
        mock_gt.return_value = [
            {"name": "skill-b", "stars": 200, "url": "https://github.com/b/b", "source": "github-topic"},
        ]
        mock_cs.return_value = [
            {"name": "skill-c", "stars": 150, "url": "https://github.com/c/c", "source": "claudskills"},
        ]
        from daemon.fetcher import fetch_all_sources
        skills = fetch_all_sources()
        self.assertEqual(len(skills), 3)
        # 验证按 stars 降序
        self.assertGreaterEqual(skills[0]["stars"], skills[1]["stars"])
        self.assertGreaterEqual(skills[1]["stars"], skills[2]["stars"])

    @patch("daemon.fetcher.fetch_github_topic")
    @patch("daemon.fetcher.fetch_claudskills")
    @patch("daemon.fetcher.fetch_jeremylongshore")
    def test_fetch_all_sources_dedup(self, mock_jl, mock_cs, mock_gt):
        """验证 fetch_all_sources 按 URL 去重"""
        mock_jl.return_value = [
            {"name": "shared-skill", "stars": 100, "url": "https://github.com/x/shared", "source": "jeremylongshore"},
        ]
        mock_gt.return_value = [
            {"name": "shared-skill", "stars": 200, "url": "https://github.com/x/shared", "source": "github-topic"},
        ]
        mock_cs.return_value = []
        from daemon.fetcher import fetch_all_sources
        skills = fetch_all_sources()
        self.assertEqual(len(skills), 1, "重复 URL 应被去重")

    @patch("daemon.fetcher.fetch_github_topic")
    @patch("daemon.fetcher.fetch_claudskills")
    @patch("daemon.fetcher.fetch_jeremylongshore")
    def test_fetch_all_sources_top_limit(self, mock_jl, mock_cs, mock_gt):
        """验证 fetch_all_sources top 参数限制"""
        mock_jl.return_value = [
            {"name": f"skill-{i}", "stars": 1000 - i, "url": f"https://github.com/x/{i}", "source": "jeremylongshore"}
            for i in range(20)
        ]
        mock_gt.return_value = []
        mock_cs.return_value = []
        from daemon.fetcher import fetch_all_sources
        skills = fetch_all_sources(top=5)
        self.assertEqual(len(skills), 5, "top=5 应返回最多 5 条")

    @patch("daemon.fetcher.fetch_github_topic")
    @patch("daemon.fetcher.fetch_claudskills")
    @patch("daemon.fetcher.fetch_jeremylongshore")
    def test_fetch_all_sources_one_source_fails(self, mock_jl, mock_cs, mock_gt):
        """验证一个数据源失败不影响其他数据源"""
        mock_jl.side_effect = Exception("Connection refused")
        mock_gt.return_value = [
            {"name": "skill-b", "stars": 200, "url": "https://github.com/b/b", "source": "github-topic"},
        ]
        mock_cs.return_value = []
        from daemon.fetcher import fetch_all_sources
        skills = fetch_all_sources()
        self.assertEqual(len(skills), 1, "一个源失败不应影响其他源")

    @patch("daemon.fetcher.fetch_github_topic")
    @patch("daemon.fetcher.fetch_claudskills")
    @patch("daemon.fetcher.fetch_jeremylongshore")
    def test_fetch_all_sources_all_sources_fail(self, mock_jl, mock_cs, mock_gt):
        """验证所有数据源都失败时返回空列表"""
        mock_jl.side_effect = Exception("Down")
        mock_gt.side_effect = Exception("Down")
        mock_cs.side_effect = Exception("Down")
        from daemon.fetcher import fetch_all_sources
        skills = fetch_all_sources()
        self.assertEqual(skills, [], "全部失败应返回空列表")


# ============================================================
# Test 2: Classifier 分类器和排序器测试
# ============================================================

class TestClassifier(unittest.TestCase):
    """分类器和排序器测试"""

    def setUp(self):
        from daemon.classifier import classify_skill, compute_score
        self.classify = classify_skill
        self.score = compute_score

    # ---- 分类测试 ----

    def test_frontend_classification(self):
        """验证前端开发分类"""
        skill = {"name": "react-component-builder", "description": "Build React components with Tailwind CSS"}
        self.assertEqual(self.classify(skill), "前端开发")

    def test_frontend_vue(self):
        """验证 Vue 前端分类"""
        skill = {"name": "vue-dashboard", "description": "Vue dashboard with animations and responsive design"}
        self.assertEqual(self.classify(skill), "前端开发")

    def test_frontend_svelte(self):
        """验证 Svelte 前端分类"""
        skill = {"name": "svelte-ui-kit", "description": "Svelte UI component kit"}
        self.assertEqual(self.classify(skill), "前端开发")

    def test_backend_classification(self):
        """验证后端开发分类"""
        skill = {"name": "fastapi-crud-generator", "description": "Generate CRUD APIs with FastAPI and PostgreSQL"}
        self.assertEqual(self.classify(skill), "后端开发")

    def test_backend_graphql(self):
        """验证 GraphQL 后端分类"""
        skill = {"name": "graphql-api", "description": "GraphQL API with Redis cache support"}
        self.assertEqual(self.classify(skill), "后端开发")

    def test_devops_docker(self):
        """验证 Docker DevOps 分类"""
        skill = {"name": "k8s-deploy-helper", "description": "Deploy to Kubernetes with Helm charts and Docker"}
        self.assertEqual(self.classify(skill), "DevOps")

    def test_devops_ci_cd(self):
        """验证 CI/CD DevOps 分类"""
        skill = {"name": "ci-pipeline", "description": "GitHub Actions CI/CD pipeline manager"}
        self.assertEqual(self.classify(skill), "DevOps")

    def test_devops_terraform(self):
        """验证 Terraform DevOps 分类（修复后：词边界匹配）"""
        skill = {"name": "terraform-aws", "description": "Terraform infrastructure for AWS cloud"}
        self.assertEqual(self.classify(skill), "DevOps")

    def test_code_quality_classification(self):
        """验证代码质量分类"""
        skill = {"name": "pytest-coverage-runner", "description": "Run pytest with coverage and lint checks"}
        self.assertEqual(self.classify(skill), "代码质量")

    def test_code_quality_sonar(self):
        """验证 Sonar 代码质量分类（修复后：词边界匹配）"""
        skill = {"name": "sonar-quality", "description": "SonarQube quality gate and format checker"}
        self.assertEqual(self.classify(skill), "代码质量")

    def test_code_quality_jest(self):
        """验证 Jest 代码质量分类"""
        skill = {"name": "jest-e2e", "description": "Jest E2E test runner with coverage reports"}
        self.assertEqual(self.classify(skill), "代码质量")

    def test_documentation_classification(self):
        """验证文档写作分类"""
        skill = {"name": "readme-generator", "description": "Auto-generate README and documentation from code"}
        self.assertEqual(self.classify(skill), "文档写作")

    def test_documentation_changelog(self):
        """验证 Changelog 文档分类（修复后：词边界匹配）"""
        skill = {"name": "changelog-builder", "description": "Build changelog and wiki from commits"}
        self.assertEqual(self.classify(skill), "文档写作")

    def test_documentation_tutorial(self):
        """验证 Tutorial 文档分类（修复后：词边界匹配）"""
        skill = {"name": "guide-builder", "description": "Build tutorials and guides"}
        self.assertEqual(self.classify(skill), "文档写作")

    def test_workflow_classification(self):
        """验证工作流分类"""
        skill = {"name": "git-pr-workflow", "description": "Manage Git branches, PRs, and commit workflows"}
        self.assertEqual(self.classify(skill), "工作流")

    def test_workflow_kanban(self):
        """验证 Kanban 工作流分类"""
        skill = {"name": "kanban-board", "description": "Project management kanban board with issue tracking"}
        self.assertEqual(self.classify(skill), "工作流")

    def test_security_classification(self):
        """验证安全审计分类"""
        skill = {"name": "secret-scanner", "description": "Scan for secrets, vulnerabilities and run security audit"}
        self.assertEqual(self.classify(skill), "安全审计")

    def test_security_oauth(self):
        """验证 OAuth 安全分类"""
        skill = {"name": "oauth-validator", "description": "OAuth token validator with encryption support"}
        self.assertEqual(self.classify(skill), "安全审计")

    def test_data_science_classification(self):
        """验证数据科学分类"""
        skill = {"name": "pandas-data-analyzer", "description": "Analyze data with pandas and create charts"}
        self.assertEqual(self.classify(skill), "数据科学")

    def test_data_science_ml(self):
        """验证 ML 数据科学分类"""
        skill = {"name": "tensorflow-model", "description": "TensorFlow model training with numpy statistics"}
        self.assertEqual(self.classify(skill), "数据科学")

    def test_other_classification(self):
        """验证无法匹配时归为'其他'"""
        skill = {"name": "misc-tool", "description": "A utility tool for miscellaneous tasks"}
        self.assertEqual(self.classify(skill), "其他")

    def test_empty_skill_classification(self):
        """验证空 skill 的分类"""
        skill = {"name": "", "description": ""}
        self.assertEqual(self.classify(skill), "其他")

    def test_none_fields_classification(self):
        """验证字段为 None 时的分类"""
        skill = {"name": None, "description": None, "full_name": None}
        self.assertEqual(self.classify(skill), "其他")

    def test_full_name_classification(self):
        """验证通过 full_name 分类"""
        skill = {"name": "tool", "full_name": "react-builder/react-builder", "description": ""}
        self.assertEqual(self.classify(skill), "前端开发")

    def test_case_insensitive_matching(self):
        """验证分类大小写不敏感"""
        skill = {"name": "React-Components", "description": "React components", "full_name": "Dev/React-Components"}
        self.assertEqual(self.classify(skill), "前端开发")

    # ---- 分类优先顺序和边界测试 ----

    def test_category_order_priority(self):
        """验证类别按字典插入顺序匹配"""
        skill = {"name": "docker-helper", "description": "Docker container manager"}
        self.assertEqual(self.classify(skill), "DevOps")

        skill2 = {"name": "python-helper", "description": "Python utility"}
        self.assertEqual(self.classify(skill2), "后端开发")

        # 同时有 api 和 test → api(后端开发) 先出现
        skill3 = {"name": "api-tester", "description": "API testing tool"}
        self.assertEqual(self.classify(skill3), "后端开发")

    def test_keyword_order_frontend_before_backend(self):
        """验证前端开发优先于后端开发: ui 先于 api"""
        skill = {"name": "ui-api-helper", "description": "UI and API helper"}
        self.assertEqual(self.classify(skill), "前端开发")

    # ---- 已知问题: 子串匹配导致误分类 ----
    # "test" 作为关键字会匹配到 "testing", 导致安全类 skill 被误分类为代码质量

    def test_substring_matching_fixed(self):
        """验证词边界修复后: 'test' 不再匹配 'testing'，正确归为安全审计"""
        skill = {"name": "firewall-auditor", "description": "Firewall compliance penetration testing tool"}
        # 修复后: \btest\b 不匹配 "testing"，安全审计的 "audit" 和 "firewall" 正确匹配
        self.assertEqual(self.classify(skill), "安全审计")

    # ---- 排序算法测试 ----

    def test_score_formula(self):
        """验证评分公式: stars*0.7 + installs*0.3"""
        skill = {"stars": 100, "installs": 50}
        expected = 100 * 0.7 + 50 * 0.3  # 70 + 15 = 85
        self.assertEqual(self.score(skill), expected)

    def test_score_zero_both(self):
        """验证 stars 和 installs 都为 0 时分数为 0"""
        skill = {"stars": 0, "installs": 0}
        self.assertEqual(self.score(skill), 0.0)

    def test_score_none_values(self):
        """验证 None 值被当作 0 处理"""
        skill = {"stars": None, "installs": None}
        self.assertEqual(self.score(skill), 0.0)

    def test_score_missing_installs(self):
        """验证缺少 installs 字段时分数计算"""
        skill = {"stars": 200}
        expected = 200 * 0.7 + 0  # 140
        self.assertEqual(self.score(skill), expected)

    def test_score_uses_downloads_fallback(self):
        """验证 installs 缺失时使用 downloads 字段"""
        skill = {"stars": 100, "downloads": 50}
        expected = 100 * 0.7 + 50 * 0.3
        self.assertEqual(self.score(skill), expected)

    def test_stars_weight_higher_than_installs(self):
        """验证 stars 权重(0.7)高于 installs(0.3)"""
        skill_a = {"stars": 1000, "installs": 100}   # 700 + 30 = 730
        skill_b = {"stars": 500, "installs": 10000}   # 350 + 3000 = 3350
        # skill_b 分数应更高，因为 installs 极高
        self.assertGreater(self.score(skill_b), self.score(skill_a))

    # ---- classify_and_sort 集成测试 ----

    def test_classify_and_sort_basic(self):
        """验证 classify_and_sort 基本流程"""
        from daemon.classifier import classify_and_sort
        skills = SKILL_SAMPLES[:]  # 拷贝
        result = classify_and_sort(skills)

        self.assertIn("categories", result)
        self.assertEqual(result["total"], len(skills))

        # 验证所有 8 个分类 + "其他"
        expected_categories = {"前端开发", "后端开发", "DevOps", "代码质量",
                               "文档写作", "工作流", "安全审计", "数据科学", "其他"}
        actual_categories = set(result["categories"].keys())
        self.assertEqual(actual_categories, expected_categories,
                         f"分类应包含 8 个 + 其他，实际: {actual_categories}")

    def test_classify_and_sort_each_category_has_entries(self):
        """验证 8 个分类 + 其他 各有至少一个 skill"""
        from daemon.classifier import classify_and_sort
        result = classify_and_sort(SKILL_SAMPLES[:])

        for cat in ["前端开发", "后端开发", "DevOps", "代码质量", "文档写作",
                     "工作流", "安全审计", "数据科学"]:
            self.assertIn(cat, result["categories"], f"应包含分类: {cat}")
            self.assertGreaterEqual(len(result["categories"][cat]), 1,
                                    f"{cat} 至少应有 1 个 skill")

        self.assertIn("其他", result["categories"])
        self.assertGreaterEqual(len(result["categories"]["其他"]), 1)

    def test_classify_and_sort_order_within_category(self):
        """验证每个分类内部按分数降序排列"""
        from daemon.classifier import classify_and_sort
        skills = [
            {"name": "react-basic", "description": "React basics", "stars": 100, "installs": 200},
            {"name": "react-pro", "description": "React pro components", "stars": 500, "installs": 1000},
            {"name": "react-lite", "description": "React minimal", "stars": 50, "installs": 100},
        ]
        result = classify_and_sort(skills)
        frontend = result["categories"]["前端开发"]
        scores = [s["score"] for s in frontend]
        self.assertEqual(scores, sorted(scores, reverse=True),
                         "分类内应按分数降序")

    def test_classify_and_sort_new_count(self):
        """验证 new_count 和 update_count 统计"""
        from daemon.classifier import classify_and_sort
        skills = [
            {"name": "new-skill", "description": "A new one", "stars": 10, "installs": 0, "status": "new"},
            {"name": "new-skill-2", "description": "Another new", "stars": 20, "installs": 0, "status": "new"},
            {"name": "update-skill", "description": "Needs update", "stars": 30, "installs": 0, "status": "update_available"},
        ]
        result = classify_and_sort(skills)
        self.assertEqual(result["new_count"], 2)
        self.assertEqual(result["update_count"], 1)

    def test_classify_and_sort_empty_list(self):
        """验证空列表输入"""
        from daemon.classifier import classify_and_sort
        result = classify_and_sort([])
        self.assertEqual(result["total"], 0)
        self.assertEqual(result["new_count"], 0)
        self.assertEqual(result["categories"], {})

    def test_classify_adds_category_and_score(self):
        """验证 classify_and_sort 会为每个 skill 添加 category 和 score 字段"""
        from daemon.classifier import classify_and_sort
        skills = [{"name": "react-app", "description": "React app builder", "stars": 100, "installs": 50}]
        result = classify_and_sort(skills)
        frontend_skills = result["categories"]["前端开发"]
        self.assertEqual(len(frontend_skills), 1)
        self.assertEqual(frontend_skills[0]["category"], "前端开发")
        self.assertIn("score", frontend_skills[0])

    def test_keyword_in_description_only(self):
        """验证仅描述中包含关键词也能正确分类"""
        skill = {"name": "cool-tool", "description": "A tool that helps with React component development"}
        self.assertEqual(self.classify(skill), "前端开发")


# ============================================================
# Test 3: Notifier 通知模块测试
# ============================================================

class TestNotifier(unittest.TestCase):
    """Windows 通知模块测试"""

    @patch("subprocess.run")
    def test_send_notification_calls_powershell(self, mock_run):
        """验证 send_notification 调用 PowerShell"""
        from daemon.notifier import send_notification
        send_notification("Test Title", "Test Message")

        mock_run.assert_called_once()
        # subprocess.run(["powershell", "-NoProfile", "-Command", ps_script], ...)
        cmd_args = mock_run.call_args[0][0]
        self.assertEqual(cmd_args[0], "powershell")
        self.assertEqual(cmd_args[1], "-NoProfile")
        self.assertEqual(cmd_args[2], "-Command")
        # ps_script 在 cmd_args[3]
        ps_script = cmd_args[3]
        # 标题/正文**不该**出现在脚本里 —— 这段脚本是拿 -Command 执行的，
        # PowerShell 双引号串里的 $(...) 会被求值，插值等于任意命令执行。
        # 它们走环境变量：那是数据，不会被当代码解析。
        self.assertNotIn("Test Title", ps_script)
        self.assertNotIn("Test Message", ps_script)
        self.assertIn("$env:SKILL_FORGE_TOAST_TITLE", ps_script)
        env = mock_run.call_args[1].get("env") or {}
        self.assertEqual(env.get("SKILL_FORGE_TOAST_TITLE"), "Test Title")
        self.assertEqual(env.get("SKILL_FORGE_TOAST_MESSAGE"), "Test Message")

    @patch("subprocess.run")
    def test_send_notification_powershell_formatting(self, mock_run):
        """验证 PowerShell 脚本包含正确的 Toast 模板参数"""
        from daemon.notifier import send_notification
        send_notification("Skill Forge Update", "3 new skills found")

        mock_run.assert_called_once()
        ps_script = mock_run.call_args[0][0][3]
        self.assertIn("ToastNotificationManager", ps_script)
        self.assertIn("ToastText02", ps_script)
        # 脚本是**固定模板** —— 一个字都不随入参变，所以不可能被注入
        self.assertNotIn("Skill Forge Update", ps_script)
        self.assertNotIn("3 new skills found", ps_script)
        env = mock_run.call_args[1].get("env") or {}
        self.assertEqual(env.get("SKILL_FORGE_TOAST_TITLE"), "Skill Forge Update")
        self.assertEqual(env.get("SKILL_FORGE_TOAST_MESSAGE"), "3 new skills found")

    @patch("subprocess.run")
    def test_send_notification_handles_subprocess_error(self, mock_run):
        """验证 send_notification 处理 subprocess 异常"""
        mock_run.side_effect = subprocess.SubprocessError("powershell not available")
        from daemon.notifier import send_notification
        try:
            send_notification("Title", "Message")
        except Exception as e:
            self.fail(f"send_notification 不应抛出异常: {e}")

    @patch("subprocess.run")
    def test_send_notification_handles_timeout(self, mock_run):
        """验证 send_notification 处理超时"""
        mock_run.side_effect = subprocess.TimeoutExpired("powershell", 10)
        from daemon.notifier import send_notification
        try:
            send_notification("Title", "Message")
        except Exception as e:
            self.fail(f"send_notification 不应抛出超时异常: {e}")

    @patch("subprocess.run")
    def test_send_notification_with_panel_path(self, mock_run):
        """验证 send_notification 接受 panel_path 参数"""
        from daemon.notifier import send_notification
        send_notification("Title", "Message", panel_path="/path/to/panel.html")
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_main_cli_with_args(self, mock_run):
        """验证命令行参数模式"""
        from daemon.notifier import send_notification
        send_notification("CLI Title", "CLI Message")
        mock_run.assert_called_once()
        ps_script = mock_run.call_args[0][0][3]
        self.assertNotIn("CLI Title", ps_script)
        env = mock_run.call_args[1].get("env") or {}
        self.assertEqual(env.get("SKILL_FORGE_TOAST_TITLE"), "CLI Title")

    # ---- 实际 Windows 通知测试 (需要桌面环境) ----
    def test_real_windows_notification(self):
        """发送真实 Windows 通知（仅在 Windows 桌面环境有效）"""
        if sys.platform != "win32":
            self.skipTest("非 Windows 环境，跳过真实通知测试")

        from daemon.notifier import send_notification
        try:
            send_notification(
                "Skill Forge - Test Notification",
                "This is a test notification from the daemon test suite. Found 3 new skills."
            )
        except Exception as e:
            self.skipTest(f"Windows Toast 通知不可用: {e}")


# ============================================================
# Test 4: Watchdog 主循环测试
# ============================================================

class TestWatchdog(unittest.TestCase):
    """守护进程主循环测试"""

    def setUp(self):
        """设置临时 skill root 并 mock SKILL_ROOT"""
        self.temp_dir = tempfile.TemporaryDirectory()
        self.skill_root = Path(self.temp_dir.name) / ".claude" / "skills" / "skill-forge"
        (self.skill_root / "daemon").mkdir(parents=True, exist_ok=True)

        # SKILL_ROOT 在模块加载时已计算，需要直接 patch
        self.skill_root_patcher = patch(
            "daemon.watchdog.SKILL_ROOT", self.skill_root
        )
        self.skill_root_patcher.start()

        # 同时 patch Path.home 以确保一致性
        self.home_patcher = patch(
            "pathlib.Path.home",
            return_value=Path(self.temp_dir.name) / ".claude"
        )
        self.home_patcher.start()

    def tearDown(self):
        self.skill_root_patcher.stop()
        self.home_patcher.stop()
        self.temp_dir.cleanup()

    # ---- check_connectivity 测试 ----

    @patch("urllib.request.OpenerDirector.open")
    def test_check_connectivity_success(self, mock_open):
        """验证网络连通性检查成功"""
        from daemon.watchdog import check_connectivity
        mock_open.return_value = MagicMock()
        result = check_connectivity()
        self.assertTrue(result)

    @patch("urllib.request.OpenerDirector.open")
    def test_check_connectivity_failure(self, mock_open):
        """验证网络连通性检查失败"""
        from daemon.watchdog import check_connectivity
        mock_open.side_effect = Exception("No network")
        result = check_connectivity()
        self.assertFalse(result)

    # ---- log 函数测试 ----

    def test_log_writes_to_file(self):
        """验证 log 函数写入日志文件"""
        from daemon.watchdog import log
        log("Test log message")

        log_file = self.skill_root / "daemon" / "watchdog.log"
        self.assertTrue(log_file.exists(), f"日志文件应被创建: {log_file}")

        content = log_file.read_text(encoding="utf-8")
        self.assertIn("Test log message", content)
        self.assertIn("[20", content)  # 包含时间戳

    def test_log_appends(self):
        """验证 log 追加写入而非覆盖"""
        from daemon.watchdog import log
        log("First message")
        log("Second message")

        log_file = self.skill_root / "daemon" / "watchdog.log"
        content = log_file.read_text(encoding="utf-8")
        self.assertIn("First message", content)
        self.assertIn("Second message", content)
        lines = [l for l in content.split("\n") if l]
        self.assertGreaterEqual(len(lines), 2)

    # ---- load_installed_skills 测试 ----

    def test_load_installed_skills_empty(self):
        """验证无 sources.json 时返回空字典"""
        from daemon.watchdog import load_installed_skills
        result = load_installed_skills()
        self.assertEqual(result, {})

    def test_load_installed_skills_with_data(self):
        """验证正确加载 sources.json"""
        sources_data = {
            "skill-a": {"installed_sha": "abc123", "url": "https://github.com/a/a"},
            "skill-b": {"installed_sha": "def456", "url": "https://github.com/b/b"},
        }
        sources_file = self.skill_root / "sources.json"
        sources_file.write_text(json.dumps(sources_data), encoding="utf-8")

        from daemon.watchdog import load_installed_skills
        result = load_installed_skills()
        self.assertEqual(len(result), 2)
        self.assertEqual(result["skill-a"]["installed_sha"], "abc123")

    def test_load_installed_skills_corrupt_json(self):
        """验证损坏的 JSON 返回空字典"""
        sources_file = self.skill_root / "sources.json"
        sources_file.write_text("{corrupt json!!!", encoding="utf-8")

        from daemon.watchdog import load_installed_skills
        result = load_installed_skills()
        self.assertEqual(result, {})

    # ---- filter_skills 测试 ----

    def test_filter_skills_new_skill(self):
        """验证新 skill 被标记为 'new'"""
        from daemon.watchdog import filter_skills
        skills = [{"name": "new-skill", "stars": 10}]
        installed = {}
        filtered = filter_skills(skills, installed)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["status"], "new")

    def test_filter_skills_no_update(self):
        """验证已安装且 SHA 一致的 skill 被过滤掉"""
        from daemon.watchdog import filter_skills
        skills = [{"name": "existing-skill", "stars": 10, "latest_sha": "a1b2c3d400000000000000000000000000000000"}]
        installed = {"existing-skill": {"installed_sha": "a1b2c3d400000000000000000000000000000000"}}
        filtered = filter_skills(skills, installed)
        self.assertEqual(len(filtered), 0, "SHA 一致的已安装 skill 应被过滤")

    def test_filter_skills_update_available(self):
        """验证有更新的 skill 被标记为 'update_available'"""
        from daemon.watchdog import filter_skills
        skills = [{"name": "update-skill", "stars": 10, "latest_sha": "b2c3d4e500000000000000000000000000000000"}]
        installed = {"update-skill": {"installed_sha": "a1b2c3d400000000000000000000000000000000"}}
        filtered = filter_skills(skills, installed)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0]["status"], "update_available")

    def test_filter_skills_no_sha(self):
        """验证无 SHA 信息时不匹配"""
        from daemon.watchdog import filter_skills
        skills = [{"name": "no-sha-skill", "stars": 10}]
        installed = {"no-sha-skill": {"installed_sha": "a1b2c3d400000000000000000000000000000000"}}
        filtered = filter_skills(skills, installed)
        self.assertEqual(len(filtered), 0)

    def test_filter_skills_mixed(self):
        """验证混合场景：新、更新、无变化"""
        from daemon.watchdog import filter_skills
        skills = [
            {"name": "new-one", "stars": 50},
            {"name": "updated-one", "stars": 100, "latest_sha": "b2c3d4e500000000000000000000000000000000"},
            {"name": "unchanged", "stars": 75, "latest_sha": "a1b2c3d400000000000000000000000000000000"},
        ]
        installed = {
            "updated-one": {"installed_sha": "a1b2c3d400000000000000000000000000000000"},
            "unchanged": {"installed_sha": "a1b2c3d400000000000000000000000000000000"},
        }
        filtered = filter_skills(skills, installed)
        self.assertEqual(len(filtered), 2, "应保留 1 个新 + 1 个更新")
        statuses = {s["name"]: s["status"] for s in filtered}
        self.assertEqual(statuses["new-one"], "new")
        self.assertEqual(statuses["updated-one"], "update_available")

    # ---- try_start 测试 ----

    @patch("daemon.watchdog.generate_panel_html")
    @patch("daemon.notifier.send_notification")
    @patch("daemon.watchdog.load_installed_skills")
    @patch("daemon.watchdog.filter_skills")
    @patch("daemon.classifier.classify_and_sort")
    @patch("daemon.fetcher.fetch_all_sources")
    @patch("daemon.watchdog.check_connectivity")
    def test_try_start_success_flow(self, mock_conn, mock_fetch, mock_classify,
                                     mock_filter, mock_load, mock_notify, mock_panel):
        """验证 try_start 成功流程"""
        from daemon.watchdog import try_start
        mock_conn.return_value = True
        mock_fetch.return_value = [{"name": "test-skill", "stars": 100}]
        mock_load.return_value = {}
        mock_filter.return_value = [{"name": "test-skill", "stars": 100, "status": "new"}]
        mock_classify.return_value = {
            "total": 1, "new_count": 1, "update_count": 0,
            "categories": {"Other": [{"name": "test-skill", "stars": 100, "category": "Other", "score": 70.0}]}
        }

        result = try_start()
        self.assertTrue(result)
        mock_notify.assert_called_once()
        mock_panel.assert_called_once()

    @patch("daemon.watchdog.check_connectivity")
    def test_try_start_no_connectivity(self, mock_conn):
        """验证无网络时 try_start 返回 False"""
        from daemon.watchdog import try_start
        mock_conn.return_value = False
        result = try_start()
        self.assertFalse(result)

    @patch("daemon.watchdog.check_connectivity")
    @patch("daemon.fetcher.fetch_all_sources")
    def test_try_start_fetch_exception(self, mock_fetch, mock_conn):
        """验证拉取异常时 try_start 返回 False"""
        from daemon.watchdog import try_start
        mock_conn.return_value = True
        mock_fetch.side_effect = Exception("Fetch failed")
        result = try_start()
        self.assertFalse(result)

    # ---- main 循环测试 ----

    # 注：main() 成功路径最后会 time.sleep(BRIDGE_KEEPALIVE)（300 秒）保持桥接存活。
    # 不 patch 掉的话，下面每个用例都要真睡 5 分钟 —— 整个文件跑不完。
    @patch("daemon.watchdog.time.sleep")
    @patch("daemon.watchdog.try_start")
    def test_main_skip_if_recent_scan(self, mock_try, mock_sleep):
        """验证距上次扫描不足 7 天时跳过"""
        from daemon.watchdog import main
        from datetime import datetime, timedelta

        timestamp_file = self.skill_root / "daemon" / "last_scan.txt"
        recent_time = (datetime.now() - timedelta(hours=1)).isoformat()
        timestamp_file.write_text(recent_time)

        main()
        mock_try.assert_not_called()

    @patch("daemon.watchdog.time.sleep")
    @patch("daemon.watchdog.try_start")
    def test_main_scans_if_no_timestamp(self, mock_try, mock_sleep):
        """验证无时间戳时执行扫描"""
        from daemon.watchdog import main
        mock_try.return_value = True
        main()
        mock_try.assert_called_once()

    @patch("daemon.watchdog.time.sleep")
    @patch("daemon.watchdog.try_start")
    def test_main_scans_if_old_timestamp(self, mock_try, mock_sleep):
        """验证超过 7 天时执行扫描"""
        from daemon.watchdog import main
        from datetime import datetime, timedelta

        timestamp_file = self.skill_root / "daemon" / "last_scan.txt"
        old_time = (datetime.now() - timedelta(days=8)).isoformat()
        timestamp_file.write_text(old_time)

        mock_try.return_value = True
        main()
        mock_try.assert_called_once()

    @patch("daemon.watchdog.time.sleep")
    @patch("daemon.watchdog.try_start")
    def test_main_retry_on_failure(self, mock_try, mock_sleep):
        """验证失败时进行重试"""
        from daemon.watchdog import main, MAX_RETRIES

        # 确保无时间戳
        timestamp_file = self.skill_root / "daemon" / "last_scan.txt"
        if timestamp_file.exists():
            timestamp_file.unlink()

        mock_try.side_effect = [False] * (MAX_RETRIES - 1) + [True]

        main()
        self.assertEqual(mock_try.call_count, MAX_RETRIES,
                         f"应重试 {MAX_RETRIES} 次，实际 {mock_try.call_count}")

    @patch("daemon.watchdog.show_error_dialog")
    @patch("daemon.watchdog.time.sleep")
    @patch("daemon.watchdog.try_start")
    def test_main_all_retries_fail_shows_dialog(self, mock_try, mock_sleep, mock_dialog):
        """验证全部重试失败后弹出对话框"""
        from daemon.watchdog import main, IDABORT, MAX_RETRIES

        timestamp_file = self.skill_root / "daemon" / "last_scan.txt"
        if timestamp_file.exists():
            timestamp_file.unlink()

        mock_try.return_value = False
        mock_dialog.return_value = IDABORT

        main()
        mock_dialog.assert_called_once()
        self.assertEqual(mock_try.call_count, MAX_RETRIES)

    @patch("daemon.watchdog.show_error_dialog")
    @patch("daemon.watchdog.time.sleep")
    @patch("daemon.watchdog.try_start")
    def test_main_retry_from_dialog(self, mock_try, mock_sleep, mock_dialog):
        """验证对话框中重试按钮功能"""
        from daemon.watchdog import main, IDRETRY, IDABORT, MAX_RETRIES

        timestamp_file = self.skill_root / "daemon" / "last_scan.txt"
        if timestamp_file.exists():
            timestamp_file.unlink()

        mock_try.return_value = False
        mock_dialog.side_effect = [IDRETRY, IDABORT]

        main()

        self.assertEqual(mock_dialog.call_count, 2,
                         f"应弹出 2 次对话框，实际 {mock_dialog.call_count}")
        self.assertEqual(mock_try.call_count, MAX_RETRIES + 1,
                         f"try_start 应被调用 {MAX_RETRIES + 1} 次，实际 {mock_try.call_count}")

    # ---- 常量测试 ----

    def test_mb_constants_defined(self):
        """验证 MessageBox 常量正确定义"""
        from daemon.watchdog import MB_ABORTRETRYIGNORE, MB_ICONWARNING, IDABORT, IDRETRY, IDIGNORE
        self.assertEqual(IDABORT, 3)
        self.assertEqual(IDRETRY, 4)
        self.assertEqual(IDIGNORE, 5)

    @patch("daemon.watchdog.show_error_dialog")
    @patch("daemon.watchdog.time.sleep")
    @patch("daemon.watchdog.try_start")
    def test_main_user_snooze_ignores(self, mock_try, mock_sleep, mock_dialog):
        """验证用户选择 1 小时后提醒时，走 snooze 路径"""
        from daemon.watchdog import main, IDIGNORE, IDABORT, MAX_RETRIES

        timestamp_file = self.skill_root / "daemon" / "last_scan.txt"
        if timestamp_file.exists():
            timestamp_file.unlink()

        mock_try.return_value = False
        # 先弹出对话框，用户选 IDIGNORE → sleep 1 小时
        # 再选 IDABORT 退出循环
        mock_dialog.side_effect = [IDIGNORE, IDABORT]

        main()

        # 应调用了 sleep (SNOOZE_INTERVAL)
        self.assertTrue(any(
            call_args[0][0] == 3600 if call_args[0] else False
            for call_args in mock_sleep.call_args_list
        ), "应有 3600 秒的 snooze sleep")

        self.assertEqual(mock_dialog.call_count, 2)


# ============================================================
# 运行
# ============================================================

if __name__ == "__main__":
    unittest.main(verbosity=2)
