# syntax=docker/dockerfile:1
# SPDX-License-Identifier: AGPL-3.0-only
# Build a clean public export with --build-context ldraw=/path/to/official/ldraw.
# Optional --secret id=proxy_ca,src=/path/to/combined-ca-bundle preserves TLS
# verification behind an HTTPS proxy without retaining session CA in the image.
FROM python:3.12.14-slim-trixie
WORKDIR /job
COPY --chmod=0444 pyproject.toml uv.lock /opt/nova/
# Install dependencies before copying the large immutable reference/library
# trees. This also avoids multiplying those trees on Docker's VFS driver.
RUN --mount=type=secret,id=proxy_ca \
    cd /opt/nova \
    && if [ -f /run/secrets/proxy_ca ]; then \
         export PIP_CERT=/run/secrets/proxy_ca SSL_CERT_FILE=/run/secrets/proxy_ca; \
         printf 'Acquire::https::CaInfo "/run/secrets/proxy_ca";\n' > /etc/apt/apt.conf.d/99-build-ca; \
       fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends leocad xvfb xauth libgl1-mesa-dri poppler-utils \
    && dpkg-query -W -f='${Version}\n' leocad | grep -Eq '^23\.03([+~-]|$)' \
    && python -m pip install --no-cache-dir uv==0.9.2 \
    && UV_NO_CACHE=1 uv sync --frozen --no-dev --no-install-project --python /usr/local/bin/python \
    && mv /usr/bin/leocad /usr/bin/leocad-real \
    && printf '#!/bin/sh\nexec xvfb-run -a -e /dev/stderr /usr/bin/leocad-real "$@"\n' > /usr/bin/leocad \
    && printf '#!/bin/sh\nexec /opt/nova/.venv/bin/python -m ldraw_tools.cli "$@"\n' > /usr/local/bin/ldraw-agent \
    && chmod 0555 /usr/bin/leocad /usr/local/bin/ldraw-agent \
    && leocad --help > /tmp/leocad-options.txt \
    && grep -q -- '--camera-position-ldraw' /tmp/leocad-options.txt \
    && grep -q -- '--export-csv' /tmp/leocad-options.txt \
    && grep -q -- '--highlight' /tmp/leocad-options.txt \
    && mkdir -p /job /opt/nova/.cache \
    && /opt/nova/.venv/bin/python -c 'import ldraw, numpy, jsonschema' \
    && rm -rf /var/lib/apt/lists/* /root/.cache /etc/apt/apt.conf.d/99-build-ca
ARG NOVA_SOURCE_REVISION
ARG NOVA_SOURCE_URL=https://github.com/noahgsolomon/ldraw-nova
LABEL org.opencontainers.image.source=$NOVA_SOURCE_URL \
      org.opencontainers.image.revision=$NOVA_SOURCE_REVISION \
      org.opencontainers.image.licenses=AGPL-3.0-only
# Copy both large trees in one final filesystem layer. Metadata below does not
# create another copy of the image on the VFS storage driver.
# Dockerfile.dockerignore is a strict public-source allowlist. The named LDraw
# context must be the official public library and retain its license notices.
RUN --mount=type=bind,source=.,target=/mnt/nova-source \
    --mount=type=bind,from=ldraw,source=/,target=/mnt/ldraw-source \
    test -n "$NOVA_SOURCE_REVISION" \
    && cp -a /mnt/nova-source/. /opt/nova/ \
    && mkdir -p /opt/ldraw \
    && cp -a /mnt/ldraw-source/. /opt/ldraw/ \
    && chmod -R a+rX,go-w /opt/nova /opt/ldraw \
    && test -f /opt/ldraw/LDConfig.ldr \
    && PYTHONPATH=/opt/nova LDRAW_DIR=/opt/ldraw /opt/nova/.venv/bin/python /opt/nova/second-brick/engine/seed_cache.py \
    && chmod -R a+rX,go-w /opt/nova-cache-seed \
    && PYTHONPATH=/opt/nova /opt/nova/.venv/bin/python -c 'import ldraw_tools.cli'
ENV PYTHONPATH=/opt/nova PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 \
    LDRAW_DIR=/opt/ldraw QT_QPA_PLATFORM=xcb LIBGL_ALWAYS_SOFTWARE=1
USER 65532:65532
ENTRYPOINT ["/opt/nova/.venv/bin/python", "-m", "ldraw_tools.cli"]
