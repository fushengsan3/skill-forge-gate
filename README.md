# 🔧 Skill Forge

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB.svg)](https://www.python.org/)

**从 GitHub 安装/发现/管理 skill 的自我迭代模块，带 L1-L5 安全验证流水线。**

---

> # ⚠️ 用之前先读这一段
>
> **这是一个学习项目，是一个真实能跑的工具，但不是一个生产级工具。**
> 用它之前请确认你接受下面全部四条。
>
> ### 1. 代码 **100% 由 AI 生成**
>
> 从第一行到最后一行 —— **包括安全流水线本身、测试、以及这些文档** ——
> 全部由 AI 编写。它**没有经过任何专业安全审计**，作者也不是安全从业者。
> 把它当成一个"可以读、可以改、可以拿来学"的样本，而不是"可以信"的组件。
>
> ### 2. 仅限**学习与研究**使用
>
> 不要在企业环境、生产机器、或任何你输不起的地方跑它。
>
> ### 3. 风险自负
>
> 它会做这些事：`git clone` 你指定的仓库、把文件写进 `~/.claude/skills/`、
> 在 Docker 容器里调用模型、**读写 Windows 凭据管理器**。
> 任何一环出问题，后果由使用者承担。
>
> ### 4. 安全流水线**不是保险箱**
>
> L1–L5 的定位是"**便宜的第一道筛子**"，不是"证明这个 skill 干净"。
> 它做的是正则匹配和模型提示观察，**能被绕过** —— 具体怎么绕、
> 以及它在哪些地方会误报，都写在 [§风险与局限](#-风险与局限) 里。
> **不要把它当成唯一的防线。**

---

## 为什么选 Skill Forge？

市面上已有 8+ 个 skill 管理器，但它们只做**运维**（安装、更新、卸载），不做**安全**。

Skill Forge 在运维之上增加了五层安全验证：

| 层级 | 检查内容 | 方式 |
|------|---------|------|
| **L1 结构** | SKILL.md 存在、frontmatter 合法、必需字段 | Python 规则引擎 |
| **L2 来源** | GitHub 仓库可达、未归档、Star 数、维护迹象 | GitHub API |
| **L3 内容安全** | 危险命令（rm -rf、curl\|bash、sudo 等）、可疑模式 | 正则规则引擎（红/黄/蓝三级） |
| **L4 冲突检测** | 文件覆盖、Hook 竞争、功能重叠、系统盘写入 | 简单规则 + Claude API（系统盘写入） |
| **L5 沙箱审计** | 在加固过的 Docker 容器里加载 skill，收集它**计划**执行的 tool_call | Docker（gVisor 尚未接上） |

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
| `安装部署说明.md` | **新设备部署指南**：前置依赖、放置位置、代理配置的 14 处位置清单、部署自检命令、首次运行、开机自启、排错对照表，以及 L5「跳过 ≠ 通过」的部署注意事项。 |
| `PROGRESS.md` | 开发进度快照：8 个任务完成 7 个，累计修复 22 个缺陷、97+ 测试通过。未完成项为 Task 8（实际使用中调优 SKILL.md）。 |
| `sources.json` | **已安装 skill 的注册表**。每条记录来源 URL、分支、安装时的 git SHA、安装时间。更新时靠 `installed_sha` 与远程 HEAD 对比判断有无新提交。 |
| `templates/install-queue.json` | **待安装队列**。面板点"安装"会由 bridge 直接处理（`POST /install` → `process_install_queue()` → `daemon/precheck.py` 跑 L1–L5 → 落盘）。队列文件既是触发点、也是断点续做的凭据。 |

### verify/ — 五层安全验证

安装任何 skill 前依次执行的检查，每层输出 JSON，靠 exit code 判断成败。

| 文件 | 作用 |
|------|------|
| `l1_structure.py` | **查结构**：SKILL.md 是否存在、frontmatter 是否合法、必需字段是否齐全。 |
| `l2_source.py` | **查来源**：GitHub 仓库是否可达、是否已归档、Star 数、最近维护迹象。走 GitHub API。 |
| `l3_content_scan.py` | **查内容危险度**。扫描 SKILL.md 及所有附带脚本，三级分类：🔴 红色（`rm -rf /`、`curl\|bash`、`sudo` 等 13 条，直接拒绝）、🟡 黄色（网络请求、读取环境变量等 11 条，需人工复核）、🔵 蓝色（仅记录，不参与判定）。 |
| `l4_conflict_detect.py` | **查冲突**：是否覆盖已有 skill 的文件、两个 skill 是否争抢同一 Hook、功能描述相似度是否超 60%、是否向系统盘写入。系统盘写入会额外调用 Claude API 深度分析。 |
| `l5_sandbox.py` | **沙箱试跑**。在加固过的 Docker 容器里（skill 只读挂载、非 root、能力全削、资源受限、跑完即毁）加载 skill，收集它**计划**执行的 tool_call 序列。⚠️ **这些调用不会被真的执行** —— 拿到的是「它想干什么」。<br>⚠️ Docker 不可用、或没拿到 AI 凭据时**标记 `SKIPPED`，不拦安装** —— 但**跳过不是通过**：报告汇总把"通过"和"跳过"分开数，一套永远跳过 L5 的部署等于没有 L5。凭据判定见 `verify/llm_auth.py`（**凭据管理器优先**，环境变量兜底）。<br>⚠️ 模型响应被截断时**不报 PASS** —— 「一轮都没成功」和「一轮都没看到调用」在报告里都是空列表，但含义相反。 |

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
| `notifier.py` | **发送 Windows Toast 通知**，随后**自动**用默认浏览器打开面板（不等点击 —— Toast 上没有绑任何激活动作，这是已知限制，不是 bug）。 |
| `queue_bridge.py` | **面板与磁盘之间的 HTTP 桥**，监听 `127.0.0.1:18970`。浏览器中的面板是静态页，无法直接写文件，因此"标记安装/卸载/修改外部数据源/改配置"都交由它代写。用 `ThreadingHTTPServer`：收密钥要弹原生框（最长阻塞 180 秒），单线程会让整个面板在这期间看起来像崩了。 |
| `bridge_auth.py` | **bridge 的锁**。共享密钥（**每 4 个扫描周期轮换一次**，即约 28 天；旧密钥保留 **35 天**宽限期）+ Origin 白名单。不锁的话，用户浏览器里**任何网站**都能 POST `/install` 或调 `/uninstall` 删本地文件。<br>为什么宽限期（35 天）比轮换间隔（28 天）长：宽限期短于间隔时，两次轮换之间会出现"新密钥已生效、旧密钥已过期"的窗口，而面板页面可能还持有旧的 —— 那不是安全收紧，是随机断连。 |
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
      └──▶ notifier.py                   Toast 通知 + 自动打开面板

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
- `pyyaml`（读 L3/L5 的**审计策略** `sandbox/audit-policy.yaml`；没装时那条"敏感路径"检查
  会**带着原因标为未生效**，不会被静默当成通过）
- `pywin32`（仅"密钥"功能需要：读写 Windows 凭据管理器）
- Docker（仅 L5 沙箱需要）
- Windows（Toast 通知与开机自启依赖任务计划程序；核心验证逻辑跨平台）

## 网络

所有外网请求通过可乐云代理：`http://127.0.0.1:7897`

Python 脚本已内置代理，Bash 脚本自动 export。

## ⚠️ 风险与局限

> 这一节不是免责声明，是**如实说明它做不到什么**。
> 写在 README 里而不是藏进文档深处，因为"**以为验过了**"比"知道没验"更危险。

### 安全流水线能被绕过

L1–L5 是**文本层面的筛子**，不是行为层面的保证。逐层说清楚：

| 层 | 它实际做什么 | 它做不到什么 |
|---|---|---|
| **L1 结构** | 检查 `SKILL.md` 存在、frontmatter 合法 | 与内容是否危险无关 |
| **L2 来源** | 查仓库是否被归档、星数、许可证 | 是**声誉**检查，不是安全检查；匿名 API 限流时会跳过 |
| **L3 内容** | 正则匹配危险/可疑文本模式 | 把命令拼起来（`"cu"+"rl"`）、从远端取来再执行，都能绕过。它认的是**文本形态**，不是行为 |
| **L4 冲突** | 与已装 skill 比文件覆盖、系统盘写入 | 它的 Claude 深度分析只在**单独手跑**该模块时才触发，面板安装路径**不走** |
| **L5 沙箱** | 把 `SKILL.md` 当提示发给模型，看它**声称**要做什么 | **不执行被审 skill 的代码**。观察的是"计划"不是"事实"，而且**模型可能什么都不声明** —— 没看到 ≠ 干净 |

**没有任何一层做语义分析或符号执行。** 反过来说，正因为如此它也会**误报**：
规则分不清「**提到**」和「**在做**」—— 文档里写一句 `rm -rf /` 当反面教材，
和脚本里真的要执行它，在正则眼里是同一件事。

### 权限与影响面

安装一个 skill 意味着它的内容会落进 `~/.claude/skills/<名字>/`，
而 **Claude Code 会读取并执行其中的指令**。这个工具的边界是：

- 它**不会**替你去执行被审 skill 的脚本（L5 只发提示，从不运行）；
- 但装完之后，**是 Claude Code 在读那个 `SKILL.md`** —— 那才是真正的执行者。
  这个工具能拦下的是"你在装之前有没有机会看一眼"，不是"装了以后会不会出事"。

### 隔离性说明

**大部分逻辑在本地运行**，不依赖任何服务端；数据（已装名单、发现存档、队列、
部署留档）都在你自己的机器上。外发只有三处，且都走你自己配置的通道：

| 去向 | 用途 | 能不能不用 |
|---|---|---|
| GitHub（`git clone` / REST API） | 拉取 skill、查来源 | 不能（核心功能） |
| **你配置的**模型端点 | L4 深度分析、L5 沙箱、AI 翻译后端 | 能 —— 不配密钥时 L4/L5 标「跳过」 |
| Google Translate | 翻译后端（未配 AI 时的回退） | 能 —— 不翻译即可 |

L5 的容器做过加固：`--read-only`、`--cap-drop ALL`、`--no-new-privileges`、
内存/CPU/pids 上限、根文件系统只读。

> **但要看清这个"隔离"是什么：** 它是"**跑我们自己的审计脚本**"时的加固，
> **不是**"把恶意代码关住"的沙箱 —— 因为 L5 从来**不执行**被审 skill 的代码。
> 真正的隔离边界是"逻辑跑在本地 + 容器只读 + 不执行被审代码"，
> 不是"我们把危险的东西关起来了"。

### 已知的功能性缺口

- `scripts/update.sh` 的预检是**后加的** —— 在那之前更新路径完全绕过 L1–L5。
- 面板的「立即安装」在没有 Docker 或没有模型密钥时，L5 会**跳过**，报告里会标出来。
- **「跳过」和「通过」在早期版本的报告里长得一样**，这是本项目反复修的一类缺陷。
  现在的规矩是"跳过必须带原因"，但**旧的存档与文档里仍留有修之前的说法**。

### 依赖

除 Python 标准库外：`pywin32`（读写 Windows 凭据管理器）、`pyyaml`（读审计策略）。
Docker 仅 L5 需要，缺了会标「跳过」。详见 [§依赖](#依赖)。

---

## License

**MIT** —— 见 [LICENSE](LICENSE)。

你可以自由使用、修改、分发，**但作者不提供任何担保**（MIT 原文照录）。
结合上面第 1、3 条读：这是一份 AI 生成、未经审计的代码，
用它的风险由你承担。
