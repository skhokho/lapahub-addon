# Supervisor supplies BUILD_ARCH; BUILD_FROM is intentionally unused.
# Keep every supported architecture pinned without deprecated build.yaml parsing.
ARG BUILD_ARCH=amd64
FROM ghcr.io/home-assistant/aarch64-base:3.22@sha256:0be4ef3dac95a32c7ca1ffd5379fac8f60fc45452dbd3a8851e789bdb95f51fd AS base-aarch64
FROM ghcr.io/home-assistant/amd64-base:3.22@sha256:5f49327a2760a009b1dfd5ae63950dea326ade0046020f54863a1f32ff486dca AS base-amd64
FROM ghcr.io/home-assistant/armhf-base:3.22@sha256:027b8d729bf62904919b8fd2cc8a2c1edaa63b9048526fabee407d50472cd4d0 AS base-armhf
FROM ghcr.io/home-assistant/armv7-base:3.22@sha256:ae2b8be31fee7bd01078d5ca51e7c48aa323ccbaca8085420a416e354b101246 AS base-armv7
FROM ghcr.io/home-assistant/i386-base:3.22@sha256:6ff095089b49e18e55609369b8d4d6bbf2e1cff4504c763f8a44a2a19ec6cd38 AS base-i386

FROM base-${BUILD_ARCH} AS pinned-base

FROM pinned-base AS runtime

# Use Alpine's matching binary packages; psutil must not need a compiler at install.
# PyYAML and config.yaml make version reporting work without Supervisor access.
RUN apk add --no-cache \
    python3 \
    py3-aiohttp \
    py3-dateutil \
    py3-psutil \
    py3-yaml \
    bash

COPY rootfs/ /
COPY config.yaml /config.yaml
RUN chmod a+x /app/run.py
WORKDIR /app
CMD ["python3", "/app/run.py"]
