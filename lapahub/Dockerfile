# amd64/arm64 fallback also supports Supervisor invocations without BUILD_FROM.
ARG BUILD_FROM=ghcr.io/home-assistant/base:3.22@sha256:4abe1e6f72ad026f6e654a7128900c89da0da1f6aa3de5e647d2bfa8b93c95d9
FROM $BUILD_FROM

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
