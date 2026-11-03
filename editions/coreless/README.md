# Skill Forge Gate 无核版

**面向 Claude Code 与 Codex 的技能安装与管理工作台，不包含 L1–L5 安全验证内核。**

支持在线发现、第三方数据源、DeepSeek 辅助搜索与模块翻译、工具版本选择、子模块按需安装、失败重试、Codex 同步、技能更新、历史版本和备份回退。十套工作台配色与完整版一致，默认雾港蓝。

## 与完整版的区别

无核安装包不包含 `verify/`、`sandbox/`，不调用来源声誉、内容扫描、技能冲突审计或沙箱检查。安装、更新和回退记录标为「未做安全验证」，说明为「无核版：未执行安全验证」，不会生成“通过五层检查”的结论。

文件路径边界、同名目录保护、安装备份和回退属于安装器的基本能力，仍会执行。无核版仍可能因仓库不可访问、找不到技能入口、目录冲突或不安全的文件路径而拒绝操作。

## 安装环境

- Windows 10 / 11；Python 3.12 是已验证环境，`python` 与 `pythonw` 应来自同一套安装。
- Git for Windows、Windows PowerShell，以及可打开本地 HTML 的浏览器。
- 使用 `.sh` 脚本时需要 Git Bash 和可用的 `python3`。
- 密钥管理需安装 `pywin32`：`python -m pip install pywin32`。
- AI 翻译与搜索可选配置 DeepSeek API 密钥；无需本地模型。
- 日常使用不需要 Docker、`pyyaml`、Node.js；当前用户需可写安装目录，端口 `127.0.0.1:18970` 可用。

## 首次安装

从 [GitHub Releases](https://github.com/fushengsan3/skill-forge-gate/releases/latest) 下载 `skill-forge-gate-coreless.zip`，将其中的 `skill-forge` 文件夹放到 `%USERPROFILE%\.claude\skills\skill-forge`。目录名仍须为 `skill-forge`，不要选择 GitHub 自动生成的 Source code。

在 PowerShell 中执行：

```powershell
Set-Location "$env:USERPROFILE\.claude\skills\skill-forge"
python -m pip install pywin32
python daemon\watchdog.py
```

首次扫描完成后打开 `discover/latest.html`，确认面板明确显示「无核版」。安装、更新和设置需要本地服务在线。

配置常驻桥接服务：

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\.claude\skills\skill-forge\daemon\install-bridge-service.ps1"
```

登录后定期扫描可选注册 `daemon/install-service.ps1`，需要相应任务计划权限。Claude Code / Codex 请按实际使用的工具配置；密钥从面板「设置 → 密钥」录入，不放进 `.env`。

## 更新和版本选择

两个发行版使用相同运行目录和端口，同一用户账户选择一种安装。当前包没有完整版／无核版切换开关。

技能本身的更新、历史提交安装和本地备份回退均可在面板完成。管理器自身升级请下载新版 coreless ZIP；停止原后台服务后，从解压目录运行 `python scripts/deploy.py`，保留原有用户配置和发现记录，再启动桥接服务。

从完整版切换时，应先停止后台服务、备份原安装目录，再以干净目录安装无核包；按需恢复原目录中的用户配置与登记数据，保留原备份。不要直接覆盖混装。源码仓库默认是完整版，克隆默认分支不会得到无核安装包。

## 数据安全与 AI 代码风险

无核版不提供技能安全审计或运行隔离。第三方技能实际运行后可能按 Claude Code / Codex 授予的权限读取文件、执行命令或联网；请自行判断技能来源与内容。

GitHub Token 与 DeepSeek 密钥保存在本机 Windows 凭据管理器。认证时密钥仍需发往对应服务；DeepSeek 仅使用官方接口。搜索向模型发送通用标签与候选编号，翻译发送过滤后的公开模块描述，不附带本机文件、路径或完整已安装清单。规则过滤不能保证识别全部敏感内容，外部服务的留存受各自政策约束。

生成面板含本地桥接访问密钥；配置、日志、备份和发现存档可能包含本机信息。不要上传整个运行目录或生成的 `discover/latest.html`，分享错误信息前先脱敏。

项目由 AI 生成并持续迭代，测试和文档也包含 AI 生成内容，尚未经过独立专业安全审计。可能存在逻辑错误、兼容问题、误覆盖或回退缺陷。首次使用先以少量非关键技能验证，并为重要文件保留独立备份。安装成功不等于安全。

项目采用 [MIT License](LICENSE)。
