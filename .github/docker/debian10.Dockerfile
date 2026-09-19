# 模拟统信 UOS v20 的运行底座：Debian 10，GLIBC 2.28。
# 仅用于 CI 离线验证，不是分发镜像。buster 已归档，源需指向 archive.debian.org。
FROM debian:10-slim

RUN sed -i 's|deb.debian.org|archive.debian.org|g; s|security.debian.org|archive.debian.org|g; /buster-updates/d' /etc/apt/sources.list \
    && apt-get -o Acquire::Check-Valid-Until=false update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        libgl1 libglib2.0-0 libgomp1 \
        binutils \
    && rm -rf /var/lib/apt/lists/*

# 验证阶段以 --network none 运行，任何联网尝试都会失败并暴露出来。
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    UV_OFFLINE=1 \
    UV_NO_CACHE=1 \
    UV_PYTHON_PREFERENCE=only-managed
