# Builds the engine from this checkout for Railway preview.
# Context is the repository root (see /.dockerignore). The preview workflow copies
# railway.toml to that root before `railway up`, because the Python package has to
# be in the Docker context.
FROM python:3.12.7-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir ".[server,postgres]"
COPY deploy/railway/engine/entrypoint.py /usr/local/bin/entrypoint.py
RUN chmod 0755 /usr/local/bin/entrypoint.py \
    && useradd --system --uid 10001 --home-dir /data lpb \
    && mkdir -p /data \
    && chown lpb:lpb /data
USER lpb
WORKDIR /data
ENV LPB_AUTO_MIGRATE=false \
    LPB_HTTP_HOST=0.0.0.0 \
    LPB_HTTP_PORT=8080 \
    LPB_ALLOCATION_MODE=per_actor
EXPOSE 8080
ENTRYPOINT ["python3", "-u", "/usr/local/bin/entrypoint.py"]
