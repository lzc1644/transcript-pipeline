#!/bin/sh
set -eu
curl --fail --silent --show-error http://127.0.0.1:8000/api/health >/dev/null
case "${HTTPS_ENABLED:-true}" in
  false|0|no|off) exit 0 ;;
esac
# Verify the persisted CA and the configured host; never accept insecure TLS.
host="${HTTPS_DEFAULT_SNI:-localhost}"
curl --noproxy '*' --fail --silent --show-error \
  --cacert /data/caddy/pki/authorities/local/root.crt \
  --resolve "${host}:8443:127.0.0.1" \
  "https://${host}:8443/api/health" >/dev/null
