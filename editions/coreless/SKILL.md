---
name: skill-forge
description: Skill Forge Gate 无核版，面向 Claude Code 与 Codex 的技能安装与管理工作台。支持发现、按模块安装、同步、更新和回退，不包含 L1–L5 安全验证内核。
---

# Skill Forge Gate 无核版

工作目录：`~/.claude/skills/skill-forge/`。

本版本用于技能安装与管理，不执行 L1–L5 来源、内容、冲突或沙箱审计。安装成功不代表技能通过安全检查，不得称为“已验证”。

- 首次安装先使用面板「查看模块」，展示子模块说明与 Claude Code / Codex 版本，按用户选择安装。
- 支持安装、更新、历史提交和本地备份回退；失败模块可单独重试。
- 无核版保留目录边界、同名覆盖、链接与文件操作保护，以及密钥存储和外发数据限制。
- 安装结果应明确说明「无核版：未执行安全验证」。不要调用 `verify/` 或 `sandbox/`，它们不在本包内。
- GitHub 与 DeepSeek 密钥从面板「设置 → 密钥」配置，翻译与模糊搜索共用 DeepSeek 密钥。
- CLI 可使用 `bash scripts/install.sh <GitHub URL>`、`bash scripts/update.sh <技能名>` 和 `bash scripts/inventory.sh`；运行前进入工作目录。
- 管理器自身升级使用新版 coreless ZIP 安装包；不要用完整版源码覆盖，否则会切换发行版。

环境、安装步骤与数据安全说明见 [README](README.md)。
