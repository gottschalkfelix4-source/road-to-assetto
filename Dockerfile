# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS geographic-inputs
ARG DATASET_URL=""
COPY docker/fetch_dataset.py docker/dataset.json /dataset/
RUN python /dataset/fetch_dataset.py --destination /prepared-data --url "${DATASET_URL}"

FROM debian:bookworm-slim AS blender-download
ARG TARGETARCH
ARG BLENDER_VERSION=4.5.9
ARG BLENDER_SHA256=dcdc3eca6c9825bb35a8033b689c053f3cb5a9b0cd2a61b2eac2a49436b4ad3d
RUN test "${TARGETARCH:-amd64}" = amd64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl xz-utils \
    && curl --fail --location --retry 3 \
       "https://download.blender.org/release/Blender4.5/blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
       --output /tmp/blender.tar.xz \
    && echo "${BLENDER_SHA256}  /tmp/blender.tar.xz" | sha256sum --check --strict \
    && mkdir -p /opt/blender \
    && tar -xJf /tmp/blender.tar.xz --strip-components=1 -C /opt/blender \
    && rm /tmp/blender.tar.xz

FROM python:3.12-slim-bookworm
LABEL org.opencontainers.image.title="Road to Assetto" \
      org.opencontainers.image.description="Geographic track builder with bundled Altenhain data and CPU Blender previews" \
      org.opencontainers.image.source="https://github.com/gottschalkfelix4-source/road-to-assetto" \
      org.opencontainers.image.licenses="Apache-2.0"
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/data/home \
    XDG_CACHE_HOME=/data/cache \
    TMPDIR=/tmp \
    ROADTOASSETTO_DATA_DIR=/data \
    ROADTOASSETTO_BLENDER=/opt/blender/blender \
    OPENBLAS_NUM_THREADS=2 \
    OMP_NUM_THREADS=4 \
    PATH="/opt/blender:${PATH}"
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ca-certificates libxi6 libxfixes3 libxrender1 libxkbcommon0 libegl1 libgl1 \
       libsm6 libice6 libx11-6 libxxf86vm1 libxrandr2 libxcursor1 libxinerama1 \
       libgomp1 libfontconfig1 fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 roadtoassetto \
    && useradd --uid 10001 --gid 10001 --home-dir /data/home --no-create-home roadtoassetto \
    && mkdir -p /app /data/home /data/cache /data/ai-cache \
    && chown -R 10001:10001 /data
COPY --from=blender-download /opt/blender /opt/blender
COPY --from=geographic-inputs /prepared-data /app/data
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --only-binary=:all: -r requirements.txt \
    && blender --version | head -n 1
COPY roadtoassetto ./roadtoassetto
COPY webapp ./webapp
COPY scripts ./scripts
COPY tracks ./tracks
COPY licenses ./licenses
COPY LICENSE THIRD_PARTY.md ./
COPY docker/preview.png ./build/rtac_altenhain_langstrasse/ui/preview.png
COPY docker/entrypoint.py ./docker/entrypoint.py
USER 10001:10001
EXPOSE 8765
VOLUME ["/data"]
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/health',timeout=3).read()"
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "/app/docker/entrypoint.py"]
CMD ["--host", "0.0.0.0", "--port", "8765", "--data-dir", "/data"]
