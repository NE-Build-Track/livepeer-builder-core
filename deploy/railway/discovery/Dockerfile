# The discovery service (`lpb discovery`, docs/contracts/discovery.md) for Railway.
# Context is the repository root (see /.dockerignore), like the engine's: the Python package has to be
# in the Docker context. Set the Railway service's RAILWAY_DOCKERFILE_PATH to this file.
FROM python:3.12.7-slim
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
WORKDIR /src
COPY . .
RUN pip install --no-cache-dir ".[server]"
COPY deploy/railway/discovery/orchestrators.txt /etc/lpd/orchestrators.txt
RUN useradd --system --uid 10001 --home-dir /data lpb
USER lpb
WORKDIR /data
# Static directory first (D43); set LPD_DIRECTORY=onchain and LPD_ARBITRUM_RPC_URL to use the chain.
# "::" listens on IPv6, which Railway's private network uses (the engine reaches discovery.railway.internal).
ENV LPD_DIRECTORY=static \
    LPD_ORCHESTRATORS=/etc/lpd/orchestrators.txt \
    LPD_HTTP_HOST=:: \
    LPD_HTTP_PORT=8939
EXPOSE 8939
CMD ["lpb", "discovery"]
