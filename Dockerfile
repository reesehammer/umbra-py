# One-command self-hosting of the read-only Umbra STAC API (`umbra serve`).
#
# Umbra publishes a static STAC catalog and no search API; this image turns
# umbra-py into that missing search API bridge with a single `docker run`.
# On first boot the entrypoint fetches the published catalog index snapshot
# (no multi-minute S3 crawl) and then serves `/search`, `/collections`, the
# OpenAPI docs at `/docs`, and a `/healthz` probe.
#
# Build (default: the lean `serve` extra -- STAC API only):
#     docker build -t umbra-py .
# Build with on-demand render endpoints (adds the `viz` stack -- rasterio etc.):
#     docker build --build-arg UMBRA_EXTRAS=serve,viz -t umbra-py:full .
# Hosted STAC + MCP (Railway uses Dockerfile.mcp, extras baked in):
#     docker build -f Dockerfile.mcp -t umbra-py:public .
#     docker run -p 8000:8000 -v umbra-data:/data umbra-py:public
# Add `load` for the numeric `/artifacts/stats` endpoint, and `dask` if you also
# want to run it lazily (`UMBRA_SERVE_ARGS="--stack-lazy"`, see deploy.md):
#     docker build --build-arg UMBRA_EXTRAS=serve,viz,load,dask -t umbra-py:full .
FROM python:3.12-slim AS runtime

# Which optional extras to install. Default keeps the image small and the STAC
# API fast; `serve,viz` adds the on-demand `/artifacts/...` render endpoints.
ARG UMBRA_EXTRAS=serve

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # The catalog index, artifact cache and any fetched snapshot live here, on a
    # volume, so they survive `docker run` restarts and `umbra index fetch` runs
    # only once. `default_index_path()` honours $XDG_CACHE_HOME.
    XDG_CACHE_HOME=/data \
    UMBRA_HOST=0.0.0.0 \
    UMBRA_PORT=8000

WORKDIR /app

# Copy only what the wheel build needs (see .dockerignore) so the layer caches
# on source changes, not on unrelated repo churn.
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
# The published JSON contracts, which the wheel force-includes as package data
# so `umbra serve` can put them in its generated OpenAPI document. An installed
# package has no checkout to fall back to, so these are part of what the build
# needs, not documentation.
COPY docs/schemas ./docs/schemas

RUN pip install ".[${UMBRA_EXTRAS}]"

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Create the umbra user (uid 10001). Stay root at start: a Railway Volume
# mounted at /data is root-owned and hides the image chown. The entrypoint
# chowns /data then drops to umbra before any umbra command.
# Do not declare VOLUME: Railway's Metal builder rejects it.
RUN useradd --create-home --uid 10001 umbra \
    && mkdir -p /data \
    && chown -R umbra:umbra /data

EXPOSE 8000

# Liveness/readiness for orchestrators. `/healthz` returns 200 once the HTTP
# server is up; the entrypoint fetches the index before starting uvicorn, so a
# healthy container is a ready one.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('UMBRA_PORT','8000'), timeout=4).status==200 else 1)"]

ENTRYPOINT ["docker-entrypoint.sh"]
