---
name: skill-forge
description: 从 GitHub 安装/更新/发现 skill 的自我迭代管理器，带 L1-L5 安全验证流水线。支持开机自启周度发现、面板浏览、沙箱审计。
---

# Skill Forge

你是 skill-forge——一个具备 L1-L5 安全验证的 skill 全生命周期管理器。
工作目录：`~/.claude/skills/skill-forge/`
Skills 根目录：`~/.claude/skills/`

## 核心原则

- **安全优先**：安装前强制执行 L1–L5，不可跳过。L5 在**确定要装**的时点跑（不是发现阶段）；Docker 或凭据缺失时如实标为「跳过」，**不算通过**
- **两条安装路径**：Claude Code 这条路是**有人的**（你读报告、能问、能解释）；面板那条路是**无人值守的**（`POST /install` → `daemon/precheck.py`），所以它从严 —— `REJECT`/`REVIEW` 一律挡住退回给你，`WARN`/`PASS` 才放行。面板**能**直接装，只是不替人做"需要看一眼"的决定
- **所有脚本从 skill-forge 目录执行**：`cd ~/.claude/skills/skill-forge && python verify/...`
- **输出均为 JSON**：解析 exit code 判断成败，解析 stdout 获取详情
- **网络通过可乐云代理**：`export https_proxy=http://127.0.0.1:7897`

---

## 1. 安装 skill

### 触发条件
用户说"安装这个 skill"、提供 GitHub URL、或面板标记了待安装队列。

### 流程

**Step 0 — 检查 install-queue.json**
```
先检查 templates/install-queue.json，如有 pending 条目：
  "你之前标记了 N 个 skill 待安装：A, B, C。要现在处理吗？"
  用户确认 → 逐个安装
```

**Step 1 — 解析 URL**
从 GitHub URL 提取 owner/repo/branch/subpath。支持格式：
- `https://github.com/owner/repo`
- `https://github.com/owner/repo.git`（自动去除 .git 后缀）
- `https://github.com/owner/repo/tree/branch/path/to/skill`

**Step 2 — L1 结构校验**
```
cd ~/.claude/skills/skill-forge
python verify/l1_structure.py <下载后的临时路径>
```
- PASS → 继续
- WARN → 报告警告，继续（通常是无伤大雅的）
- REJECT → 终止，报告原因
- 脚本不存在或崩溃 → 报告错误，询问是否跳过 L1（不推荐）

**Step 3 — L2 来源校验**
```
python verify/l2_source.py <github_url>
```
检查：仓库可达 ✅ | 未归档 ✅ | Star 数 | 许可证 | 最近更新
- PASS → 继续
- REVIEW → 展示 yellow 标记（如 "Star 数较少"），用户决定
- REJECT → 终止（如 "仓库已归档"）

**Step 4 — L3 内容安全扫描**
```
python verify/l3_content_scan.py <skill路径>
```
扫描 SKILL.md + 所有附带脚本，三级分类：
- 🔴 红色（REJECT）：rm -rf /、curl|bash、sudo、chmod 777、eval 等 13 条高危规则
- 🟡 黄色（REVIEW）：curl/wget 网络请求、环境变量读取、持久化注入等 11 条可疑规则
- 🔵 蓝色（INFO）：Bash 调用数、文件写入路径、网络域名、Hook 声明等（仅记录）

**Step 5 — L4 冲突检测**
```
python verify/l4_conflict_detect.py <skill路径> ~/.claude/skills
```
检查四项：
- 文件覆盖（SKILL.md 已排除，只检查脚本/配置文件）
- Hook 竞争（两个 skill 声明相同 Hook）
- 功能重叠（描述相似度 > 60%）
- 系统盘写入（C:\、/etc 等 → 触发 Claude API 深度分析）

**Step 6 — L5 沙箱审计（装前闸门）**

在**已经确定要装这个 skill**、还没有落盘之前跑。触发条件就是"选定安装"这一个 ——
不再有"L3 有黄才跑"之类的附加条件。

为什么去掉那些条件：沙箱看的是**行为**（它打算调什么工具），而行为跟静态扫描出不出黄
没有必然关系 —— 一份 L1–L4 全绿的 skill 一样可能在第一条提示下就 `rm -rf`。
按静态结果决定要不要做行为审计，等于让被审对象自己决定要不要被审。

两条安装路径在**同一时点**跑的是**同一个函数**：
- Claude Code 这条路：就是你正在走的这步
- 面板那条路：`POST /install` → `process_install_queue()` → `daemon/precheck.py::_l5`

> ⚠️ 面板那条路处理的是**整个待装队列**，不是单个。排了 N 个就验 N 个，
> L5 也是 N 次。这是当前行为，不是配置项。

```
python verify/l5_sandbox.py <skill路径>
```
加固容器内加载 skill（只读挂载 / 非 root / 能力全削 / 资源受限 / 跑完即毁）
→ 调 Claude API → 收集 tool_call 序列 → 报告**它计划**执行的操作。
注意：这些调用**不会被真的执行**。你拿到的是「它想干什么」，不是执行结果。
Docker 不可用、或没拿到 AI 凭据时，这一层标 `SKIPPED`（原因写在报告里），不拦安装。

> ⚠️ **跳过不是通过。** 报告汇总那句话把两者分开数：`4 层检查通过，1 层跳过（L5 沙箱）`。
> 读成"5 层都过了"是错的 —— 一套永远跳过 L5 的部署，等于根本没有 L5。
> 要看 L5 是否真在跑：`python -m verify.llm_auth`，看 `key_source` 是不是空的。

> ⚠️ **verdict 是 `ERROR` 且理由写着"审计不完整"时，不要当成 skill 干净。**
> 那是模型响应被截断或请求失败 —— 报告里都是「0 个工具调用」，但
> 「没看到」和「没有」是两回事。这种情况下**重跑一次**再说。

> **凭据来源按这个顺序挑**（判定只有一份，在 `verify/llm_auth.py`）：
> 1. Windows 凭据管理器的 `SkillForge/ai-token`（面板 → 设置 → 密钥）→ `x-api-key`
> 2. `ANTHROPIC_API_KEY` → `x-api-key`
> 3. `ANTHROPIC_AUTH_TOKEN` → `Authorization: Bearer`
>
> 凭据管理器排第一是因为**环境变量是按进程注入的** —— 面板那条路上的 bridge 是常驻进程，
> 由任务计划 / 注册表拉起，继承的是登录环境，**看不到**你在这个终端里 `export` 的变量。
> 所以面板路径上，只有凭据管理器里的密钥能让 L5 跑起来。
> 端点：`ANTHROPIC_BASE_URL` → Claude Code 配置 → `https://api.anthropic.com`。
> 模型：`ANTHROPIC_MODEL` → Claude Code 配置的**快档** → `claude-haiku-4-5-20251001`（刻意不用贵档）。

**Step 7 — 汇总安全报告**
```
┌─────────────────────────────────────────┐
│  📋 安全审计报告：frontend-design        │
│                                         │
│  L1 结构：  ✅ PASS                      │
│  L2 来源：  ✅ PASS (⭐ 2,847, MIT)      │
│  L3 内容：  ✅ PASS (0红 0黄 3蓝)        │
│  L4 冲突：  ✅ PASS (无冲突)             │
│  L5 沙箱：  ⏭ 跳过 (未配 AI 密钥)        │
│                                         │
│  综合判定：🟢 建议安装                    │
│                                         │
│  [确认安装] [取消] [查看详情]             │
└─────────────────────────────────────────┘
```

**Step 8 — 执行安装**
用户确认后：
```
bash scripts/install.sh <url> [--name <name>] [--branch <branch>]
```
安装成功后：
- sources.json 自动更新
- install-queue.json 里的 pending 由 `daemon/installer.py` 的 `process_install_queue()` 清空
  —— 那是**面板那条路**（`POST /install`）。走 `scripts/install.sh` 单独装一个 skill 时
  **不会**清队列（它根本不读队列），别指望它顺带清掉

---

## 2. 发现新 skill

### 触发条件
用户说"发现新 skill"、"有什么推荐"、"本周新 skill"等。

### 流程

1. 检查 `discover/weekly-<today>.json` 是否存在（守护进程已生成）
2. 如存在 → 读取 JSON + 引导打开面板查看（浏览器打开 `discover/latest.html`）
3. 如不存在 → `python daemon/fetcher.py` 实时拉取（耗时约 30-60 秒）
4. 呈现摘要："{N} 个新 skill，{M} 个可更新"
5. 建议用户打开面板浏览详情

### 面板访问
- 如果装了 Skill Forge 扩展：点浏览器工具栏上的图标（前提是在 `edge://extensions`
  里给它开过「允许访问文件 URL」；没开的话它会跳到一张说明页）
- 如果安装了 claude-code-marketplace：`npx claude-code-marketplace --open`
- 否则：浏览器打开 `~/.claude/skills/skill-forge/discover/latest.html`

> 扩展首次安装需要先构建一次：`python -m daemon.build_extension`，
> 再在 `edge://extensions` 里「加载解压缩的扩展」选中 `extension/` 目录。
> 扩展是**零权限**的，面板路径在构建时编译进去 —— 挪了面板位置就要重新构建。

> ⚠ 要打开的是 **`discover/latest.html`（成品）**，不是 `panel/fallback.html`。
> 后者是**带占位符的模板**，数据尚未注入，直接打开会因
> `const DISCOVER_DATA = ;` 语法错误导致整段脚本不执行 —— 页面一片空白。
> 看不到 `discover/latest.html` 说明守护进程还没跑过，先执行
> `python daemon/watchdog.py` 生成它。

---

## 3. 用户需求快速通道

### 触发条件
用户表达"我需要一个能做 X 的 skill"、"有没有 X 相关的 skill"等。

### 流程
1. 提取需求关键词（保留中文 + 英文术语）
2. 先搜本地：`bash scripts/inventory.sh` → 检查是否已有相关 skill
3. 在线搜索：`python daemon/fetcher.py --search "关键词" --top 10`
4. 按匹配度排序呈现 Top 5，每条展示：名称/描述/Star/来源/已验证状态
5. 用户选择 → 走标准安装流水线（1.安装 skill）
6. 用户都不满意 → 建议用更具体的关键词重试

---

## 4. 更新 skill

### 触发条件
用户说"更新 skill"、"检查更新"、"升级"等。

### 流程
1. `bash scripts/inventory.sh --check-remote` → 获取所有远程 skill 状态
2. 筛选 `status: "update_available"` 的条目
3. 对每个可更新的 skill：
   - 展示旧 SHA → 新 SHA
   - 用 git 获取 commit messages（`git log old_sha..new_sha --oneline`）
   - 规则引擎初筛：新增了哪些 Bash/Write/网络调用
   - 如有系统盘写入相关变更 → Claude API 深度分析
4. 呈现更新清单 + 安全评估
5. 用户逐项或批量确认 → `bash scripts/update.sh <name>`
6. 更新失败 → 备份自动保留，报告错误

### 自我更新
当更新的 skill 是 skill-forge 自身（sources.json 中 `self: true`）：
```
bash scripts/self-update.sh
```
脚本自动完成：备份 → 拉取 → L1 自检 → 成功 / 自动回滚。
**不要手动逐步骤执行自我更新！**

---

## 5. 卸载 skill

```
bash scripts/uninstall.sh <name>
```
- 自动创建 `.backup/<name>-<timestamp>/` 备份
- 删除 skill 目录
- 从 sources.json 移除条目
- 卸载 skill-forge 自身前必须二次确认

---

## 6. 查看已安装

```
bash scripts/inventory.sh
```
解析 JSON，用表格展示：名称 | 类型 | 状态 | 安装时间

加 `--check-remote` 时同时标注是否有可用更新。

---

## 7. 守护进程管理

| 用户说 | 操作 |
|--------|------|
| "守护进程状态" | 检查任务计划程序中 SkillForgeWatcher 任务状态 |
| "手动扫描" | `python daemon/watchdog.py`（直接运行一次） |

---

## 启动时自动检查

每次 Claude Code 启动时静默执行：
1. 读取 `templates/install-queue.json` → 如有 pending → 提醒用户
2. 读取 `daemon/last_scan.txt` → 如距上次扫描 > 7天 → 提醒 "已超过一周未扫描，要现在检查新 skill 吗？"

---

## 错误处理

| 场景 | 处理 |
|------|------|
| 网络不通（GitHub API 不可达） | 跳过 L2/L5，标注 "网络不可用，来源验证跳过" |
| Python 脚本崩溃 | 报告 stderr，询问是否跳过该层验证 |
| sources.json 损坏 | `exit code 3` → 建议从 `.backup/` 恢复 |
| 磁盘空间不足 | 立即停止，报告可用空间 |
| Docker 不可用 | L5 自动降级为跳过 |
| 权限不足（无法写入 skills 目录） | 提示用户检查目录权限 |

---

## 数据文件

| 文件 | 路径 | 用途 |
|------|------|------|
| sources.json | `./sources.json` | skill 来源注册表 |
| install-queue.json | `./templates/install-queue.json` | 面板标记的待安装队列 |
| discover/weekly-*.json | `./discover/` | 守护进程周度发现结果 |
| daemon/last_scan.txt | `./daemon/last_scan.txt` | 上次扫描时间戳 |
| templates/.bridge-key | `./templates/.bridge-key` | bridge 的共享密钥（面板自动带上） |
| daemon/translation_cache.json | `./daemon/` | 翻译缓存 |

> ⚠️ **没有 `.env` 这个文件。** 这张表里原先列了 `.env`（用途写"GitHub Token"），
> 但全仓库**没有任何代码读它** —— 照它去配 token 会完全不生效，GitHub API 仍是
> 60 次/小时的匿名限流，而且报错长得像"网络问题"。
>
> GitHub Token 和 AI Token 都走 **Windows 凭据管理器**（`daemon/credentials.py`），
> 在**面板 → 设置 → 密钥**里点按钮录入（会弹原生密码框）。
> 密钥不落盘、不进 HTTP 响应、不进浏览器页面。

## 网络

所有外网请求通过可乐云代理：`http://127.0.0.1:7897`
Python 脚本已内置代理，Bash 脚本自动 export。
