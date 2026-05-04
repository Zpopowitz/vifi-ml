# Pinning by digest for reproducibility (I109). Bump deliberately;
# the digest pins us to a specific OS + libssl + libpython snapshot.
# To update: `docker pull python:3.11-slim` then `docker images
# --digests` for the new digest.
FROM python:3.11-slim@sha256:6d85378d88a19cd4d76079817532d62232be95757cb45945a99fec8e8084b9c2 AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libgomp1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt
# The message bus extra (Redis Streams) is optional in requirements.txt;
# install it explicitly here so the API + workers can talk to Redis.
RUN pip install --no-cache-dir --prefix=/install "redis==5.0.8"

COPY data_gen.py preprocess.py train.py ./
RUN PYTHONPATH=/install/lib/python3.11/site-packages \
    python train.py -n 3000 --model-dir models

# ---- runtime ----
FROM python:3.11-slim@sha256:6d85378d88a19cd4d76079817532d62232be95757cb45945a99fec8e8084b9c2

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/install/bin:$PATH" \
    PYTHONPATH="/install/lib/python3.11/site-packages:/app"

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 curl \
    && rm -rf /var/lib/apt/lists/* \
    # Pinned UID/GID (I112) for predictable file ownership on
    # bind-mounted volumes and across hosts.
    && groupadd --gid 10001 vifi \
    && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash vifi

COPY --from=builder /install /install
COPY --from=builder /build/models /app/models
COPY data_gen.py preprocess.py train.py calibration.py quality.py audit.py ./
COPY security.py pseudonymize.py ./
COPY api.py dashboard.py ./
COPY modules/ ./modules/
COPY tools/ ./tools/

# Pre-create the audit log directory with vifi ownership BEFORE
# `USER vifi`. Docker copies the directory's contents (and ownership)
# into the named volume on first mount, so the audit_subscriber's
# vifi user can write to it without manual chown.
RUN mkdir -p /app/data/audit \
    && chown -R vifi:vifi /app/data

USER vifi

EXPOSE 8000

# Note: no HEALTHCHECK in the image. Each service (api, inference_worker,
# audit_subscriber, dashboard) defines its own healthcheck in
# docker-compose.yml because they have different liveness signals: the
# api answers HTTP /health, the workers ping Redis, the dashboard hits
# its Streamlit health endpoint.

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
