# Builds the engine from this checkout for Railway.
# Context is the repository root (see /.dockerignore), because the Python package
# has to be in the Docker context.
FROM python:3.12.7-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir ".[server,postgres]"
COPY deploy/railway/engine/entrypoint.py /usr/local/bin/entrypoint.py
# The engine probes the orchestrators in this list once, at startup (D42). For a live catalog, set the
# Railway variables LPB_ORCHESTRATOR_LIST= (empty) and LPB_DISCOVERY_URL=<a /discover-orchestrators URL>.
COPY deploy/railway/engine/orchestrators.json /etc/livepeer-builder/orchestrators.json
RUN chmod 0755 /usr/local/bin/entrypoint.py \
    && useradd --system --uid 10001 --home-dir /data lpb \
    && mkdir -p /data \
    && chown lpb:lpb /data
USER lpb
WORKDIR /data
ENV LPB_AUTO_MIGRATE=false \
    LPB_HTTP_HOST=0.0.0.0 \
    LPB_HTTP_PORT=8080 \
    LPB_ALLOCATION_MODE=per_actor \
    LPB_ORCHESTRATOR_LIST=/etc/livepeer-builder/orchestrators.json
EXPOSE 8080
ENTRYPOINT ["python3", "-u", "/usr/local/bin/entrypoint.py"]
