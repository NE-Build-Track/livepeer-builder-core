# NE-Build-Track/node releases/final (VERSION 1.0.2). Includes f970be6 (TicketParams.Recipient).
# The dev stack builds the same commit from docker/Dockerfile.
# This image adds the Railway entrypoint. The dev stack bind-mounts signer.toml.
FROM golang:1.27.2@sha256:5bc7f572bbaa98885a3a1fd9c0aa76b59e3e14e8628bfc316bbfd0c701e4818c AS build
ARG NODE_REPO=https://github.com/NE-Build-Track/node.git
ARG NODE_REF=406b248f6cb8728e6e7eba1809901f589060bc6b
ENV CGO_ENABLED=0 GOTOOLCHAIN=local GOENV=off GOWORK=off \
    GOFLAGS=-mod=readonly GOAMD64=v1 GOARM64=v8.0
WORKDIR /src
RUN git init \
    && git remote add origin "$NODE_REPO" \
    && git fetch --depth 1 origin "$NODE_REF" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$NODE_REF"
RUN sha256sum go.mod go.sum > /tmp/modules.sha256 \
    && test "$(go env GOVERSION)" = go1.27.2 \
    && go mod download && go mod verify \
    && sha256sum -c /tmp/modules.sha256
ARG TARGETOS
ARG TARGETARCH
RUN version="v$(cat VERSION)-dev" \
    && GOOS="${TARGETOS:-$(go env GOOS)}" GOARCH="${TARGETARCH:-$(go env GOARCH)}" \
    go build -trimpath -buildvcs=false \
    -ldflags "-s -w -buildid= -X github.com/livepeer/node/cmd/version.Version=${version} -X github.com/livepeer/node/cmd/version.Commit=${NODE_REF}" \
    -o /out/livepeer-signer ./cmd/livepeer-signer \
    && sha256sum -c /tmp/modules.sha256 \
    && go version -m /out/livepeer-signer | grep -q 'CGO_ENABLED=0'

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/livepeer-signer /usr/local/bin/livepeer-signer
COPY entrypoint.py /usr/local/bin/entrypoint.py
COPY orchestrators.txt /etc/orchestrators.txt
RUN chmod 0755 /usr/local/bin/entrypoint.py
EXPOSE 8937
ENTRYPOINT ["python3", "-u", "/usr/local/bin/entrypoint.py"]
