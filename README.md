# 🔧 Skill Forge

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB.svg)](https://www.python.org/)

**从 GitHub 安装/发现/管理 skill 的自我迭代模块，带 L1-L5 安全验证流水线。**

## 为什么选 Skill Forge？

市面上已有 8+ 个 skill 管理器，但它们只做**运维**（安装、更新、卸载），不做**安全**。

Skill Forge 在运维之上增加了五层安全验证：

| 层级 | 检查内容 | 方式 |
|------|---------|------|
| **L1 结构** | SKILL.md 存在、frontmatter 合法、必需字段 | Python 规则引擎 |
| **L2 来源** | GitHub 仓库可达、未归档、Star 数、维护迹象 | GitHub API |
| **L3 内容安全** | 危险命令（rm -rf、curl\|bash、sudo 等）、可疑模式 | 正则规则引擎（红/黄/蓝三级） |
| **L4 冲突检测** | 文件覆盖、Hook 竞争、功能重叠、系统盘写入 | 简单规则 + Claude API（系统盘写入） |
| **L5 沙箱审计** | 在隔离容器中加载 skill，收集 tool_call，评估行为 | Docker / gVisor |

## 快速安装

```bash
git clone https://github.com/<your-account>/skill-forge.git ~/.claude/skills/skill-forge
```

重启 Claude Code 即可生效。

安装后的运行目录是 `~/.claude/skills/skill-forge/`，所有脚本都从该目录执行。

> **要部署到另一台设备？** 见 [安装部署说明.md](安装部署说明.md) —— 前置依赖、代理配置的 15 处位置清单、部署自检命令、首次运行、开机自启、排错对照表。

### 注册开机自启（可选）

```powershell
powershell -ExecutionPolicy Bypass -File ~/.claude/skills/skill-forge/daemon/install-service.ps1
```

## 使用方式

在 Claude Code 对话中：

| 你说 | 它做 |
|------|------|
| "安装这个 skill: https://github.com/..." | 走 L1→L5 验证 → 安全报告 → 安装 |
| "本周有什么新 skill？" | 读取周度发现报告，呈现面板 |
| "我需要一个能做 API 文档的 skill" | 实时搜索 → Top 5 推荐 |
| "检查更新" | 扫描所有远程 skill → 展示 diff + 安全评估 |
| "更新 skill-forge 自身" | 备份 → 拉取 → 自检 → 成功/回滚 |
| "卸载 X skill" | 备份 → 删除 → 清理 sources.json |

## 项目结构

### 根目录

| 文件 | 作用 |
|------|------|
| `SKILL.md` | **给 Claude 读的说明书**。定义何时该做什么：装 skill 走哪几步、更新走哪几步、如何解析 `sources.json`。整套工具的入口，其余文件都是它调用的工具。 |
| `README.md` | 给人看的项目介绍（本文件）。 |
| `安装部署说明.md` | **新设备部署指南**：前置依赖、放置位置、代理配置的 15 处位置清单、部署自检命令、首次运行、开机自启、排错对照表。 |
| `PROGRESS.md` | 开发进度快照：8 个任务完成 7 个，累计修复 22 个缺陷、97+ 测试通过。未完成项为 Task 8（实际使用中调优 SKILL.md）。 |
| `sources.json` | **已安装 skill 的注册表**。每条记录来源 URL、分支、安装时的 git SHA、安装时间。更新时靠 `installed_sha` 与远程 HEAD 对比判断有无新提交。 |
| `templates/install-queue.json` | **待安装队列**。面板点"安装"不会直接安装，只往这里塞一条记录；下次读取到 pending 才提示处理。这是"Claude Code 是唯一安装入口"的落地方式。 |

### verify/ — 五层安全验证

安装任何 skill 前依次执行的检查，每层输出 JSON，靠 exit code 判断成败。

| 文件 | 作用 |
|------|------|
| `l1_structure.py` | **查结构**：SKILL.md 是否存在、frontmatter 是否合法、必需字段是否齐全。 |
| `l2_source.py` | **查来源**：GitHub 仓库是否可达、是否已归档、Star 数、最近维护迹象。走 GitHub API。 |
| `l3_content_scan.py` | **查内容危险度**。扫描 SKILL.md 及所有附带脚本，三级分类：🔴 红色（`rm -rf /`、`curl\|bash`、`sudo` 等 13 条，直接拒绝）、🟡 黄色（网络请求、读取环境变量等 11 条，需人工复核）、🔵 蓝色（仅记录，不参与判定）。 |
| `l4_conflict_detect.py` | **查冲突**：是否覆盖已有 skill 的文件、两个 skill 是否争抢同一 Hook、功能描述相似度是否超 60%、是否向系统盘写入。系统盘写入会额外调用 Claude API 深度分析。 |
| `l5_sandbox.py` | **沙箱试跑**。仅在 L3 出黄、L2 需复核、或来源不在信任列表时触发。在 Docker 隔离环境中加载 skill，观察其实际想执行的 tool_call 序列。Docker 不可用时自动跳过。 |

### daemon/ — 后台常驻

| 文件 | 作用 |
|------|------|
| `watchdog.py` | **主循环**。开机由任务计划程序拉起，判断距上次扫描是否满 7 天，不满则跳过。扫描时按序串起：拉数据 → 过滤已装 → 分类 → 翻译 → 存 JSON → 生成 HTML → 发通知。失败重试 3 次，仍失败弹 Windows 原生对话框，供选择重试/一小时后提醒/跳过本周。 |
| `fetcher.py` | **拉数据**。从 jeremylongshore、GitHub Topics、ClaudSkills 三个源并行抓取 skill 列表。主要耗时在此（30–60 秒）。 |
| `classifier.py` | **分类排序**。将拉到的 skill 按规则归入前端/后端/数据科学/文档写作等类目，并按热度排序。 |
| `translate.py` | **翻译**。将英文简介批量翻译为中文，结果缓存于 `translation_cache.json`，因此重复扫描同一批数据很快。 |
| `translate_ai.py` | **AI 翻译后端**。走 `ANTHROPIC_BASE_URL` + `/v1/messages`，`temperature=0`。**失败一律静默退回 Google** —— 密钥过期/余额不足/模型下线都不该让整轮扫描跟着失败。 |
| `translate_config.py` | 翻译配置（后端 + 模型 + 提示词），落 `templates/translate-config.json`。**面板读不到 localStorage，所以配置必须存在磁盘上**。默认 `google`。 |
| `translate_prompts.py` | 内置翻译提示词清单。只对外暴露 id/名称/许可证，**不暴露文件路径**。 |
| `notifier.py` | **发送 Windows Toast 通知**，点击通知直接打开面板。 |
| `queue_bridge.py` | **面板与磁盘之间的 HTTP 桥**，监听 `127.0.0.1:18970`。浏览器中的面板是静态页，无法直接写文件，因此"标记安装/卸载/修改外部数据源/改配置"都交由它代写。用 `ThreadingHTTPServer`：收密钥要弹原生框（最长阻塞 180 秒），单线程会让整个面板在这期间看起来像崩了。 |
| `bridge_auth.py` | **bridge 的锁**。共享密钥（落盘固定不轮换）+ Origin 白名单。不锁的话，用户浏览器里**任何网站**都能 POST `/install` 或调 `/uninstall` 删本地文件。 |
| `installed.py` | 已安装名单的**唯一**形状定义。bridge 的 `GET /installed` 与 daemon 生成面板时的嵌入数据共用它，在线/离线才不会给出两种格式。 |
| `periods.py` | 历史各期数据的两层拆分（catalog + periods），避免嵌入面板的体积每周线性增长。 |
| `safe_embed.py` | `json_for_script()` —— 把 JSON 安全地放进 `<script>` 块（转义 `</`）。 |
| `credentials.py` | **密钥读写**，走 Windows 凭据管理器（`win32cred`，不是 `keyring`）。对外只暴露"配没配"的布尔值，**绝不返回值本身**。 |
| `secret_prompt.py` | 拉起原生 PowerShell 密码框收密钥。值的路径是 `输入框 → 进程内存 → 凭据管理器`，**不经过 HTTP、不经过浏览器、明文不落盘**。 |
| `cc_models.py` | 从 Claude Code 配置里提取可用模型清单。**白名单**匹配（只认 `ANTHROPIC_MODEL` 等有限几个键），避免顺手把 `ANTHROPIC_AUTH_TOKEN` 一起读出来发给页面。 |
| `build_extension.py` | **构建浏览器扩展**（见下）。把面板绝对路径编译进代码，并**强制**清单零权限 —— 加任何权限字段都会让构建失败。 |
| `installer.py` | **执行安装**：读队列 → clone → 注册进 `sources.json` → 清理队列条目。支持单个仓库内含多个子 skill 的情况。 |
| `install-service.ps1` | 将守护进程注册为开机自启的 Windows 任务计划。 |
| `install-bridge-service.ps1` | 将 **bridge** 注册为登录自启（任务计划被组策略拒绝时自动退回 `HKCU\...\Run`）。 |

### scripts/ — 运维脚本

| 文件 | 作用 |
|------|------|
| `install.sh` | 从 GitHub URL 安装一个 skill。 |
| `update.sh` | 更新指定 skill，更新前自动备份。 |
| `uninstall.sh` | 卸载：备份到 `.backup/<名称>-<时间戳>/` → 删除目录 → 从 `sources.json` 移除条目。 |
| `inventory.sh` | 列出已安装的 skill；加 `--check-remote` 时同时查询是否有可用更新。 |
| `self-update.sh` | skill-forge 自我更新：备份 → 拉取 → 自检 → 失败自动回滚。 |

### panel/ 与 discover/ — 界面与产出

| 文件 | 作用 |
|------|------|
| `panel/fallback.html` | **面板模板**（单文件，零外部依赖，样式与脚本全内联）。不含数据，是一个带 `/* __DATA_PLACEHOLDER__ */` 占位符的空壳。⚠ **不要直接打开它** —— 占位符未替换会触发 `const DISCOVER_DATA = ;` 语法错误，整段脚本不执行，页面空白。 |
| `discover/latest.html` | **运行时生成**。模板注入当周数据后的成品，**这才是浏览器里要打开的面板页面**。 |
| `discover/weekly-<日期>.json` | **运行时生成**。每次扫描的原始结果存档。 |
| `discover/simple.html`、`discover/test.html` | 早期测试遗留页面，非正常产物。 |

### extension/ — 浏览器扩展（零权限启动器）

一个只做一件事的扩展：把 `discover/latest.html` 当成普通本地文件在新标签页里打开。

**它没有任何权限**，也不声明 `host_permissions`、`content_scripts`。这不是偷懒，是设计。

面板渲染的是从 GitHub 拉来的、**别人写的**描述文本。这类文本永远该当作不受信输入。
面板内部已经有多层转义防线，但"防线很多"和"不可能被绕过"是两回事。

> ⚠️ **一处更正（2026-10-05）**。这里原先写的是"待在普通标签页里，最坏是关掉一个
> 本地网页"。**那句是错的。** 面板 HTML 里嵌着 bridge 的共享密钥（R7 的设计使然），
> 所以面板里任何一次成功的 XSS 都直接继承**完整 bridge 权限** —— 而 bridge 能删目录、
> 能装代码。面板不是"一个可以关掉的页面"，它握着本地写操作的钥匙。

那这个零权限决定还成立吗？成立，但它守的**不是**面板的爆炸半径，而是"别再往上加一层"。
同一次 XSS：

| 面板宿主 | 攻击者拿到什么 |
|---|---|
| 普通标签页 | bridge 的 HTTP 接口 —— 能力有边界，可以逐条枚举、逐条加固 |
| 扩展上下文 | `chrome.*` —— 读所有标签页、以扩展身份发跨域请求、改别的扩展 |

少一个标签页的便利，不值得换来下面那一行。

**真正决定"最坏能坏到哪"的是 bridge 侧**：R7 的鉴权、`daemon/safe_paths.py` 的路径校验、
面板 CSP 的外发闸门。扩展只是那套守卫之外最外面的一圈。

| 文件 | 作用 |
|------|------|
| `templates/extension/manifest.json` | MV3 清单，**零权限**，无 `default_popup`（设了它点击图标就不触发）。 |
| `templates/extension/background.js` | 点击图标 → 探测"文件访问"开关 → 开面板，或开说明页并挂 `!` 角标。 |
| `templates/extension/help.html` + `help.js` | 引导用户勾开关的说明页，同时挂在 `options_ui` 上（右键图标 → 扩展选项）。⚠ 脚本**必须外链**：MV3 扩展页的 CSP 会静默拦掉内联 `<script>`，表现是"点了没反应"。 |
| `daemon/build_extension.py` | 构建：注入面板绝对路径 + 强制零权限 + 输出到 `extension/`。 |
| `extension/` | **构建产物**（`~/.claude/skills/skill-forge/extension/`），不是模板。edge://extensions 里加载的是它。 |

**安装**：

```bash
python -m daemon.build_extension      # 产物在 ~/.claude/skills/skill-forge/extension/
```

然后 `edge://extensions` → 打开「开发人员模式」→「加载解压缩的扩展」→ 选该目录。

**还需要手动做一次的**：在扩展详情里打开「**允许访问文件 URL**」。
`file://` 的门槛是**每扩展的用户开关**，manifest 里声明什么都换不来。
没打开时点图标会跳到说明页并挂上红色 `!` 角标。

**为什么不加 `storage` 权限来存面板路径**：面板路径必须在扩展加载前就知道，
零权限意味着既不能存配置也读不到 `USERPROFILE`。唯一的路是编译进去。
允许"加个权限方便点"的口子一开，下一个"方便点"就是 `host_permissions` ——
所以它被写成了构建守卫：清单里出现任何权限字段，构建直接失败（`tests/test_extension_launcher.py` 钉住）。

### sandbox/ 与 tests/

| 文件 | 作用 |
|------|------|
| `sandbox/default.dockerfile` | L5 沙箱使用的 Docker 镜像定义。 |
| `tests/test_verify.py` | 验证引擎契约测试。 |
| `tests/test_daemon.py` | 守护进程测试套件（最大的测试文件），覆盖 fetcher/classifier/notifier/watchdog。 |
| `tests/test_panel_sim.py` | 以模拟数据渲染面板，验证 HTML 不会崩溃。 |
| `tests/*-test-report.md` | 五份测试报告：运维脚本、守护进程、面板桥接、L5 沙箱、开机自启。 |

## 数据流

一次周度扫描的完整链路：

```
任务计划程序（开机）
      │
      ▼
watchdog.py ──检查 last_scan.txt──▶ 不足 7 天则跳过
      │
      ▼
fetcher.py   并行拉取三个数据源
      │
      ▼
过滤已安装（读 sources.json，保留有更新的）
      │
      ▼
classifier.py 分类排序 ──▶ translate.py 翻译（带缓存）
      │
      ├──▶ discover/weekly-<日期>.json   原始数据存档
      ├──▶ discover/latest.html          面板成品
      └──▶ notifier.py                   Toast 通知（点击打开面板）

面板交互 ◀──HTTP──▶ queue_bridge.py (127.0.0.1:18970) ──▶ install-queue.json
                                                              │
                                              用户确认后 ◀────┘
                                                              ▼
                                         verify/L1-L5 ──▶ scripts/install.sh
```

## 开发阶段

| Phase | 内容 | 状态 |
|-------|------|------|
| Phase 1 | 核心验证引擎 (L1-L4) | ✅ 完成 |
| Phase 2 | 运维脚本 (install/update/uninstall/inventory) | ✅ 完成 |
| Phase 3 | 守护进程 + 发现引擎 | ✅ 完成 |
| Phase 4 | 面板 HTML + 沙箱集成 | ✅ 完成 |
| Phase 5 | SKILL.md Claude 指令 | 🚧 待实际使用中调优 |

## 依赖

- Python 3.9+
- Git
- Docker（仅 L5 沙箱需要）
- Windows（Toast 通知与开机自启依赖任务计划程序；核心验证逻辑跨平台）

## 网络

所有外网请求通过可乐云代理：`http://127.0.0.1:7897`

Python 脚本已内置代理，Bash 脚本自动 export。

## License

MIT
