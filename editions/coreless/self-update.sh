#!/bin/bash
# Coreless must never replace itself with the full repository distribution.
printf '%s\n' '{"error":"无核版请在面板选择「检查工作台更新」，或下载新版 coreless 安装包更新管理器。"}' >&2
exit 2
