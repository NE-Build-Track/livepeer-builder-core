#!/bin/sh
# The RPC URL is a sealed Railway variable. Refuse to start without it, and never print it.
set -eu
if [ -z "${LIVEPEER_ONCHAIN_DISCOVERY_RPC_URL:-}" ]; then
  echo "LIVEPEER_ONCHAIN_DISCOVERY_RPC_URL is required" >&2
  exit 1
fi
exec /usr/local/bin/livepeer-onchain-discovery
