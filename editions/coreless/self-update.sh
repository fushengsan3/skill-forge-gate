#!/bin/bash
# Coreless must never replace itself with the full repository distribution.
printf '%s\n' '{"error":"无核版请下载新版 coreless 安装包更新管理器；技能的安装、更新与回退仍可正常使用。"}' >&2
exit 2
