#!/usr/bin/env bash
set -euo pipefail
TOKEN_FILE="/root/.config/fakabot/orders_api_tokens.env"
if [[ ! -r "$TOKEN_FILE" ]]; then
  echo "Missing $TOKEN_FILE" >&2
  exit 1
fi
if ! wrangler whoami >/dev/null 2>&1; then
  echo "Cloudflare Wrangler is not authenticated. Run: wrangler login" >&2
  exit 2
fi
set -a
# shellcheck disable=SC1090
source "$TOKEN_FILE"
set +a
for key in BOT_API_TOKEN PAYMENT_LISTENER_TOKEN PROCUREMENT_EXECUTOR_TOKEN; do
  value="${!key:-}"
  if [[ ${#value} -lt 64 ]]; then
    echo "Invalid or missing $key in $TOKEN_FILE" >&2
    exit 3
  fi
  printf '%s' "$value" | wrangler secret put "$key" --name fakabot-orders
done
echo "Three independent Worker secrets installed. Procurement remains paused.";