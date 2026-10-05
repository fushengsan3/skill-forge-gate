# Skill Forge 默认沙箱镜像
# 用于 L5 安全审计 — 轻量隔离环境
FROM python:3.11-slim

# 最小依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 创建受限用户
RUN useradd -m -s /bin/bash sandbox

# 只读挂载点：skill 目录挂载到 /skill（只读）
# 可写：/tmp
RUN mkdir -p /skill /tmp && chown sandbox:sandbox /tmp

USER sandbox
WORKDIR /skill

# 默认命令：打印环境信息
CMD ["python3", "-c", "import os,json; print(json.dumps({'user':os.environ.get('USER','sandbox'),'pwd':os.getcwd(),'home':os.path.expanduser('~')}))"]
