# Skill Forge Gate L5 沙箱镜像
#
# 这个镜像**真的会被运行** —— 在 2026-10-05 之前它只被 build 出来就扔了，
# 模型调用发生在宿主机进程里，于是文档里"在隔离容器中加载 skill"是假的。
#
# 运行方式见 verify/l5_sandbox.py 的 run_docker_sandbox()。那里的加固参数
# （--read-only / --cap-drop=ALL / --security-opt=no-new-privileges /
#   --memory / --cpus / --pids-limit / -v ...:ro）和这里的 USER 是一套，
# 改一边要想着另一边。

FROM python:3.11-slim

# 只要 curl 和证书 —— 沙箱里装得越少，能出的事越少
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 非 root 用户。容器里的一切都以它的身份跑
RUN useradd -m -s /bin/bash sandbox

# /skill 是 skill 目录的挂载点，运行时以 :ro 挂进来
# /tmp 单独给一块可写空间（根文件系统在运行时是 --read-only）
RUN mkdir -p /skill /tmp && chown sandbox:sandbox /tmp

# 执行体。它从 stdin 读 JSON、往 stdout 写 JSON —— 见 runner.py 顶部说明
COPY runner.py /opt/runner.py
RUN chmod 0444 /opt/runner.py && chown root:root /opt/runner.py

USER sandbox
WORKDIR /skill

ENTRYPOINT ["python3", "/opt/runner.py"]
