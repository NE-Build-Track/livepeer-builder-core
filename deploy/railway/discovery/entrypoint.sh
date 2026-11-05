#!/bin/sh
# The RPC URL is a sealed variable, or a mode-0600 file. Never print it.
set -eu
if [ -n "${LIVEPEER_ONCHAIN_DISCOVERY_RPC_URL_FILE:-}" ]; then
  LIVEPEER_ONCHAIN_DISCOVERY_RPC_URL=$(cat "$LIVEPEER_ONCHAIN_DISCOVERY_RPC_URL_FILE") || exit 1
  export LIVEPEER_ONCHAIN_DISCOVERY_RPC_URL
  unset LIVEPEER_ONCHAIN_DISCOVERY_RPC_URL_FILE
fi
if [ -z "${LIVEPEER_ONCHAIN_DISCOVERY_RPC_URL:-}" ]; then
  echo "LIVEPEER_ONCHAIN_DISCOVERY_RPC_URL is required" >&2
  exit 1
fi
exec /usr/local/bin/livepeer-onchain-discovery
