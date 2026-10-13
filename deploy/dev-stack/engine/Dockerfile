# Builds the engine from this checkout (context: repository root; see /.dockerignore).
# CI builds this image and runs `lpb smoke` and a /readyz check in it (job `container`).
FROM python:3.12.7-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir ".[server,postgres]"
RUN useradd --system --uid 10001 --home-dir /data lpb && mkdir -p /data && chown lpb:lpb /data
USER lpb
WORKDIR /data
ENV LPB_DATABASE_URL=sqlite+aiosqlite:////data/livepeer-builder.db LPB_HTTP_HOST=0.0.0.0 LPB_HTTP_PORT=8080
EXPOSE 8080
CMD ["lpb", "serve"]
