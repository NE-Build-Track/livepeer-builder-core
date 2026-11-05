# On-chain discovery for Railway.
# Context is the repository root. The Railway service's Dockerfile path stays this file.
# The binary is github.com/NE-Build-Track/livepeer-discovery at 737e520.
FROM golang:1.25-bookworm AS build
ENV GOTOOLCHAIN=auto CGO_ENABLED=0
RUN go install github.com/NE-Build-Track/livepeer-discovery/cmd/livepeer-onchain-discovery@737e5201e37b9ae4106183c6ccbaf32d698b5cfa

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /go/bin/livepeer-onchain-discovery /usr/local/bin/livepeer-onchain-discovery
COPY deploy/railway/discovery/entrypoint.sh /usr/local/bin/discovery-entrypoint
COPY deploy/railway/discovery/orchestrators.txt /etc/lpd/orchestrators.txt
RUN chmod 755 /usr/local/bin/livepeer-onchain-discovery /usr/local/bin/discovery-entrypoint \
    && useradd --system --uid 10001 --home-dir /data lpb \
    && mkdir -p /data \
    && chown lpb:lpb /data
USER lpb
WORKDIR /data
# [::] is Railway's private network. SplitHostPort accepts [::]:8939.
ENV LIVEPEER_ONCHAIN_DISCOVERY_LISTEN=[::]:8939 \
    LIVEPEER_ONCHAIN_DISCOVERY_NODES=/etc/lpd/orchestrators.txt \
    LIVEPEER_ONCHAIN_DISCOVERY_REFRESH_INTERVAL=30s
EXPOSE 8939
ENTRYPOINT ["/usr/local/bin/discovery-entrypoint"]
