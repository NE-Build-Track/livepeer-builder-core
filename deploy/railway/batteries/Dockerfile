# Clearinghouse Batteries at the livepeer-builder M2 pin.
# https://github.com/eliteprox/clearinghouse-batteries/commit/ca09e47c067682fb48c3f15425546fb6aa5b6659
# Fetch by SHA. Cloning main would build a later commit than the pin.
FROM golang:1.27.2-bookworm AS build
ARG BATTERIES_REPO=https://github.com/eliteprox/clearinghouse-batteries.git
ARG BATTERIES_REF=ca09e47c067682fb48c3f15425546fb6aa5b6659
WORKDIR /src
RUN git init \
    && git remote add origin "$BATTERIES_REPO" \
    && git fetch --depth 1 origin "$BATTERIES_REF" \
    && git checkout --detach FETCH_HEAD \
    && test "$(git rev-parse HEAD)" = "$BATTERIES_REF"
RUN go mod download
RUN CGO_ENABLED=1 go build -trimpath -o /out/clearinghouse ./cmd/clearinghouse

FROM debian:bookworm-slim
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/clearinghouse /usr/local/bin/clearinghouse
COPY entrypoint.py /usr/local/bin/entrypoint.py
RUN chmod 0755 /usr/local/bin/entrypoint.py
EXPOSE 8080 8081 9092
ENTRYPOINT ["python3", "-u", "/usr/local/bin/entrypoint.py"]
