#!/usr/bin/env python3
"""Panel simulation test — validates fallback.html rendering with mock data."""
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

# 与 daemon/watchdog.py 走同一条注入路径，测试才不会重新教坏写法
from daemon.safe_embed import json_for_script
from daemon.bridge_auth import KEY_PLACEHOLDER

# Create test data matching classify_and_sort output format
test_data = {
    "generated": "2026-08-11T22:00:00",
    "total": 8,
    "new_count": 4,
    "update_count": 2,
    "categories": {
        "前端开发": [
            {"name": "react-component-generator", "full_name": "acme/react-component-generator",
             "description": "Generate React components with Tailwind CSS styling automatically",
             "stars": 1240, "installs": 8500, "url": "https://github.com/acme/react-component-generator",
             "source": "github-topic", "status": "new", "category": "前端开发", "score": 3418.0, "author": "acme"},
            {"name": "vue-directive-kit", "full_name": "vue-studio/vue-directive-kit",
             "description": "A collection of useful Vue 3 custom directives",
             "stars": 890, "installs": 3200, "url": "https://github.com/vue-studio/vue-directive-kit",
             "source": "claudskills", "status": "new", "category": "前端开发", "score": 1583.0, "author": "vue-studio"}
        ],
        "DevOps": [
            {"name": "docker-compose-wizard", "full_name": "ops-tools/docker-compose-wizard",
             "description": "AI-powered Docker Compose file generator and optimizer",
             "stars": 3400, "installs": 25000, "url": "https://github.com/ops-tools/docker-compose-wizard",
             "source": "jeremylongshore", "status": "new", "category": "DevOps", "score": 9880.0, "author": "ops-tools"}
        ],
        "文档写作": [
            {"name": "readme-builder", "full_name": "doc-tools/readme-builder",
             "description": "Generate beautiful README.md files from templates with AI assistance",
             "stars": 1500, "installs": 9800, "url": "https://github.com/doc-tools/readme-builder",
             "source": "jeremylongshore", "status": "new", "category": "文档写作", "score": 3990.0, "author": "doc-tools"}
        ],
        "代码质量": [
            {"name": "eslint-ai-reviewer", "full_name": "code-quality/eslint-ai-reviewer",
             "description": "AI-powered ESLint rule suggestions and code review automation",
             "stars": 780, "installs": 4500, "url": "https://github.com/code-quality/eslint-ai-reviewer",
             "source": "github-topic", "status": "update_available", "category": "代码质量", "score": 1896.0,
             "author": "code-quality", "installed_sha": "11112222333344445555", "latest_sha": "22223333444455556666"}
        ],
        "后端开发": [
            {"name": "graphql-explorer", "full_name": "JaneSmith/graphql-explorer",
             "description": "Interactive GraphQL schema explorer and query builder for Claude",
             "stars": 560, "installs": 1800, "url": "https://github.com/JaneSmith/graphql-explorer",
             "source": "github-topic", "status": "new", "category": "后端开发", "score": 932.0, "author": "JaneSmith"}
        ],
        "其他": [
            {"name": "utility-belt", "full_name": "misc/utility-belt",
             "description": "A collection of miscellaneous utility functions for everyday coding",
             "stars": 320, "installs": 1100, "url": "https://github.com/misc/utility-belt",
             "source": "claudskills", "status": "new", "category": "其他", "score": 554.0, "author": "misc"}
        ]
    }
}

# Read template
template_path = os.path.join(os.path.dirname(__file__), "..", "panel", "fallback.html")
with open(template_path, "r", encoding="utf-8") as f:
    template = f.read()

# Simulate daemon's generate_panel_html
# 用固定假密钥 —— 测试不该去创建/读取用户真实的 ~/.claude/.../.bridge-key
TEST_BRIDGE_KEY = "test-key-not-real"
html_output = template.replace("/* __DATA_PLACEHOLDER__ */", json_for_script(test_data))
html_output = html_output.replace(KEY_PLACEHOLDER, json_for_script(TEST_BRIDGE_KEY))

# Write output
output_path = os.path.join(os.path.dirname(__file__), "panel-test-output.html")
with open(output_path, "w", encoding="utf-8") as f:
    f.write(html_output)

# === VALIDATION ===
errors = []
warnings = []
info = []

# 1. Placeholder replacement
if "/* __DATA_PLACEHOLDER__ */" in html_output:
    errors.append("DATA_PLACEHOLDER not replaced in output HTML")
else:
    info.append("DATA_PLACEHOLDER replaced successfully")

# 2. Template integrity checks
checks = {
    "flattenData function": "function flattenData()",
    "renderSkills function": "function renderSkills()",
    "skillCard function": "function skillCard(s)",
    # 这个签名原先钉的是 `(name, url, needsDeepAudit)` —— 那是"L5 只在
    # 需要深度审计时才跑"那套条件触发的残留。那套已经整个删掉了：
    # L5 在**装前闸门**上无条件跑（前置缺失才跳过），所以面板这边
    # 没有"要不要深审"这个开关，多一个参数反而是错的。
    "queueInstall function": "function queueInstall(name, url)",
    "showQueue function": "function showQueue()",
    "updateQueueBadge function": "function updateQueueBadge()",
    "installQueue init": "installQueue = JSON.parse(localStorage.getItem",
    "DISCOVER_DATA const": "const DISCOVER_DATA =",
    "BRIDGE_KEY const": "const BRIDGE_KEY =",
    "bridgeFetch 统一入口": "function bridgeFetch(path, options)",
    "card-title CSS class": ".card-title",
    "badge-new CSS class": ".badge-new",
    "badge-update CSS class": ".badge-update",
    "installed-panel CSS class": ".installed-panel",
}
for name, pattern in checks.items():
    if pattern in html_output:
        info.append(f"Template check PASS: {name}")
    else:
        errors.append(f"Template check FAIL: {name} — '{pattern}' not found")

# 2b. 零外部依赖 —— 面板是本地 file:// 页面，不得加载任何远程脚本
# （模板头部注释明写"全内联样式，零外部依赖"；这条守住它）
remote_refs = [p for p in ("cdn.tailwindcss.com", "daisyui@", "unpkg.com", "cdn.jsdelivr.net")
               if p in html_output]
if remote_refs:
    errors.append(f"零外部依赖 FAIL: 模板引用了远程资源 {remote_refs} —— 违背自包含设计")
else:
    info.append("零外部依赖 PASS: 未引用任何远程 CDN 资源")

# 3. Data structure compatibility
panel_keys = ["categories", "total", "new_count", "update_count"]
for k in panel_keys:
    if k not in test_data:
        errors.append(f"Data format FAIL: missing top-level key '{k}'")
    else:
        info.append(f"Data format PASS: key '{k}' present")

# 4. Skill object field compatibility
skill_fields = ["name", "description", "stars", "url", "status", "source", "score"]
for skill_list in test_data["categories"].values():
    for skill in skill_list:
        for f in skill_fields:
            if f not in skill:
                errors.append(f"Skill object FAIL: '{skill.get('name','?')}' missing field '{f}'")

if not any("Skill object FAIL" in e for e in errors):
    info.append("All skill objects have required fields (name, description, stars, url, status, source, score)")

# 5. Data type validation
for skill_list in test_data["categories"].values():
    for skill in skill_list:
        if not isinstance(skill.get("stars"), (int, float)):
            errors.append(f"Data type FAIL: stars not numeric in '{skill.get('name','?')}'")
        if not isinstance(skill.get("score"), (int, float)):
            errors.append(f"Data type FAIL: score not numeric in '{skill.get('name','?')}'")

info.append("Numeric types validated: stars and score are numbers")

# 6. JavaScript code quality checks
js_section = template.split("<script>")[1].split("</script>")[0]

# XSS: 模板中不得存在任何内联 on* 属性（R8-4）
if re.search(r'\son(click|change|input|error|load|submit)\s*=', template):
    errors.append("R8-4 FAIL: 模板中仍存在内联 on* 属性 —— 应改用 addEventListener")
else:
    info.append("R8-4 PASS: 模板中无内联 on* 属性")

# XSS: 卡片不得再用 data-* 传数据 + 拼 HTML 字符串（R8-2）
if "data-install data-name=" in js_section:
    warnings.append("R8-2 疑似残留：仍在用 data-* 属性传值并拼接 HTML")
else:
    info.append("R8-2 PASS: 卡片改用 DOM 构建，无 data-* 拼接")

# 发现卡上的「来源可信」徽章 —— 2026-10-06 已删除（D1），这里钉住它不再回来。
#
# ⚠️ 这条断言以前是**假通过**：它 grep 的是 `s.source === 'jeremylongshore'`，
# 而真实代码写的是 `verifiedSources.indexOf(s.source)` —— 两个字符串永远对不上，
# 所以它永远走 else 分支打印「configurable」，看起来一切正常。
# 一个从不匹配的 grep 不是"没发现问题"，是"没有在看"。
if "verifiedSources" in js_section or "badge-verified" in js_section:
    errors.append("发现卡上又出现了「来源可信」徽章 —— 它背后是硬编码白名单，"
                    "且发现阶段根本没有 L1–L5 结论（D1 已于 2026-10-06 删除）")
else:
    info.append("D1 PASS：发现卡上没有「来源可信」徽章（真实结论改在已安装列表里显示）")
# 反过来：真实的 trust_level 显示必须还在，别把功能一起删掉
if "trust_level" not in js_section:
    errors.append("已安装列表不再显示 trust_level —— D1 只是删了假徽章，真值要留着")
else:
    info.append("D1 PASS：已安装列表仍在读 trust_level")

# Dead CSS
if "--pct" in template and "stats-ring" in template:
    warnings.append("CSS class 'stats-ring' uses --pct custom property but it is never set via JS — dead CSS")

# Category placeholder
if "全部分类" in template:
    info.append("Category dropdown has 'all' default option")

# Search input
if "search-input" in template and "oninput" in template:
    info.append("Search input with oninput handler: present")

# Sort options
if "filter-sort" in template:
    info.append("Sort dropdown (stars/score): present")

# Empty state
if "empty-state" in template and "hidden" in template:
    info.append("Empty state with hidden class: present")

# Responsive design
if "flex-wrap" in template:
    info.append("Responsive flex-wrap: present")
if "max-w-5xl" in template:
    info.append("Responsive max-width: present")
if "container" in template and "mx-auto" in template:
    info.append("Responsive container: present")

# Install queue
if "skill-forge-queue" in template:
    info.append("Install queue localStorage key: present")
if "updateQueueBadge" in template:
    info.append("Queue badge update function: present")

# Toast notification
if "toast" in template.lower():
    info.append("Toast notification DOM creation: present")

# 7. Bridge assessment
info.append("")
info.append("=== BRIDGE ASSESSMENT ===")

# daemon -> panel data flow
info.append("Data flow: daemon/classifier.py:classify_and_sort() -> discover/weekly-*.json -> panel/fallback.html")
info.append("Field mapping: classifier outputs 'category', panel reads 'status' — both present")

# Missing fields in bridge
classifier_fields = {"name", "full_name", "description", "stars", "installs", "url", "updated_at",
                     "source", "status", "category", "score", "installed_sha", "latest_sha", "author", "plugin"}
panel_fields_in_code = {"name", "full_name", "description", "stars", "installs", "url", "html_url",
                        "status", "source", "_category", "author", "installed_sha", "latest_sha", "score"}
common = classifier_fields & panel_fields_in_code
classifier_only = classifier_fields - panel_fields_in_code
panel_only = panel_fields_in_code - classifier_fields
info.append(f"Common fields: {common}")
if classifier_only:
    warnings.append(f"Classifier outputs fields not read by panel: {classifier_only}")
if panel_only:
    info.append(f"Panel reads fields not in classifier output: {panel_only} (url fallback -> html_url is OK)")

# 8. R8-1 — <script> 上下文注入（服务端转义）
# 验收标准：description 含 </script><img src=x onerror=alert(1)> 的假 skill，
# 注入 HTML 后不能跳出脚本块。
info.append("")
info.append("=== R8-1: SCRIPT-CONTEXT INJECTION ===")

attack_payload = {
    "generated": "2026-01-01T00:00:00",
    "total": 1, "new_count": 1, "update_count": 0,
    "categories": {"恶意": [{
        "name": "evil",
        "full_name": "attacker/evil",
        "description": "</script><img src=x onerror=alert(1)>",
        "stars": 1,
        "url": "https://github.com/attacker/evil",
        "source": "github-topic", "status": "new", "category": "恶意", "score": 1.0,
    }]},
}
attack_html = template.replace("/* __DATA_PLACEHOLDER__ */", json_for_script(attack_payload))

if "</script><img" in attack_html:
    errors.append("R8-1 FAIL: description 里的 </script> 未被转义，可跳出脚本块执行任意脚本")
else:
    info.append("R8-1 PASS: </script> 已被转义，无法跳出 <script> 块")

if "\\u003c/script\\u003e" in attack_html:
    info.append("R8-1 PASS: 载荷以 \\u003c 转义形式保留，数据本身未损坏")
else:
    errors.append("R8-1 FAIL: 未找到 \\u003c 转义形式，转义可能改变了数据")

# 9. R7 — bridge 密钥注入与统一入口
info.append("")
info.append("=== R7: BRIDGE AUTH ===")

if "/* __BRIDGE_KEY__ */" in html_output:
    errors.append("R7 FAIL: BRIDGE_KEY 占位符未被替换，面板会带着 null 去请求 bridge")
elif 'const BRIDGE_KEY = "test-key-not-real";' not in html_output:
    errors.append("R7 FAIL: BRIDGE_KEY 未按预期注入")
else:
    info.append("R7 PASS: BRIDGE_KEY 已注入")

# 所有对 bridge 的请求都必须走 bridgeFetch，否则会漏带密钥被拒。
# 唯一允许出现 fetch(BRIDGE_URL 的地方，就是 bridgeFetch 自己的实现。
direct = re.findall(r'fetch\(\s*BRIDGE_URL', js_section)
if len(direct) != 1:
    errors.append(
        f"R7 FAIL: 有 {len(direct)} 处直接 fetch(BRIDGE_URL...)（应恰好 1 处，即 bridgeFetch 内部），"
        "漏调用会不带密钥而被 bridge 拒绝"
    )
else:
    info.append("R7 PASS: 对 bridge 的调用全部经由 bridgeFetch 统一带密钥")

# === REPORT ===
print("=" * 60)
print("PANEL SIMULATION TEST RESULTS")
print("=" * 60)
print(f"\nTest data: {test_data['total']} skills in {len(test_data['categories'])} categories")
print(f"Template:  {len(template):>6} chars")
print(f"Output:    {len(html_output):>6} chars")
print(f"Output written to: {output_path}")

print(f"\n--- ERRORS ({len(errors)}) ---")
for e in errors:
    print(f"  [ERROR] {e}")
if not errors:
    print("  (none)")

print(f"\n--- WARNINGS ({len(warnings)}) ---")
for w in warnings:
    print(f"  [WARN]  {w}")
if not warnings:
    print("  (none)")

print(f"\n--- INFO ({len(info)}) ---")
for i_entry in info:
    print(f"  [INFO]  {i_entry}")

print(f"\n--- SUMMARY ---")
total_checks = len(errors) + len(warnings) + len(info)
print(f"Total checks: {total_checks}")
print(f"Errors:   {len(errors)}")
print(f"Warnings: {len(warnings)}")
print(f"Info:     {len(info)}")
if errors:
    print(f"RESULT: FAIL — {len(errors)} error(s) found")
elif warnings:
    print(f"RESULT: PASS with {len(warnings)} warning(s)")
else:
    print("RESULT: ALL PASS")
