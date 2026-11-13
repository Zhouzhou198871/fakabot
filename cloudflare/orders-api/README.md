# Fakabot Orders API

Production Worker: `https://fakabot-orders.chulingli1988.workers.dev`

D1: `fakabot-procurement-db` (`fb8bf149-abc2-4f05-96e5-0735db7fa8fc`)

## Deployment status

- Worker deployed and bound to production D1.
- `GET /health` verified against production; D1 connection and catalog version are healthy.
- Write routes are protected by three independent Worker secrets. The generated credentials are stored on the server at `/root/.config/fakabot/orders_api_tokens.env` with mode 0600.
- Until those secrets are installed in the Worker, write routes intentionally return 401.
- `PROCUREMENT_ENABLED=false`; procurement is paused. No supplier purchase or payment was sent.
- This API currently creates orders, accepts trusted payment-listener confirmations, queues procurement jobs, and records executor results. It does not yet contain the live supplier purchase executor or a customer credential-decryption/delivery endpoint.

## Install the Worker secrets

On a trusted terminal with Wrangler authenticated to the same Cloudflare account:

```bash
wrangler login
bash /root/fakabot/cloudflare/orders-api/configure_secrets.sh
```

Alternatively, add these three secret names in Cloudflare Dashboard → Workers & Pages → `fakabot-orders` → Settings → Variables and Secrets, copying the values from the protected server file:

- `BOT_API_TOKEN`
- `PAYMENT_LISTENER_TOKEN`
- `PROCUREMENT_EXECUTOR_TOKEN`

Never paste the token file into chat or commit it to Git.

## API

All write/read-order endpoints use `Authorization: Bearer <BOT_API_TOKEN>`, except payment events (payment-listener token) and procurement executor routes (procurement-executor token).

### Health

`GET /health`

### Create order

`POST /v1/orders`

Headers:
- `Authorization: Bearer <BOT_API_TOKEN>`
- `Idempotency-Key: <unique-key-at-least-12-characters>`
- `Content-Type: application/json`

Body:
```json
{
  "telegram_user_id": "123456789",
  "supplier_product_id": "1247492380692922662",
  "quantity": 1
}
```

The Worker reads the latest active catalog snapshot, validates stock and quantity, snapshots the catalog version and price, and creates a 20-minute pending-payment order. It does not reserve supplier stock or charge funds. Repeated idempotency keys return the same order.

### Payment confirmation

`POST /v1/internal/payment-events`

Header: `Authorization: Bearer <PAYMENT_LISTENER_TOKEN>`

Body:
```json
{
  "event_id": "unique-provider-event-id",
  "order_id": "order-uuid",
  "provider": "payment-provider",
  "payment_ref": "unique-transaction-reference",
  "amount_usdt": "2.3250",
  "currency": "USDT",
  "verified": true,
  "payload_hash": "optional-event-payload-hash"
}
```

Only the independent payment listener should call this endpoint, after it has verified the payment itself. The Worker independently checks the exact order amount, pending state, expiry, event ID and transaction-reference uniqueness. A successful confirmation creates one procurement queue task.

### Read order

`GET /v1/orders/{order_id}?telegram_user_id={telegram_user_id}`

Requires BOT_API_TOKEN and enforces ownership.

### Procurement

`GET /v1/internal/procurement-jobs` requires PROCUREMENT_EXECUTOR_TOKEN and returns 423 while procurement is paused. The supplier executor is not deployed yet; do not set `PROCUREMENT_ENABLED=true`.

## Files

- `worker.js`: deployed Worker source.
- `migration.sql`: D1 schema migration.
- `configure_secrets.sh`: installs independent Worker secrets using Wrangler.

## Alipay sandbox asynchronous notifications

A separate Worker is deployed at `https://fakabot-alipay-notify.chulingli1988.workers.dev`.

- Public callback: `POST /notify` (Alipay form POST; RSA2 signature, app ID, seller ID and successful trade status are checked before persistence).
- Bot queue: `GET /internal/events` and `POST /internal/events/{event_id}/ack` (Bearer `ALIPAY_NOTIFY_TOKEN`).
- D1 queue: `alipay_notifications` (unique notify ID and trade number; duplicate callbacks are idempotent).
- Sandbox payment page uses `alipay.trade.page.pay` at `https://openapi.alipaydev.com/gateway.do`.
- Bot private application key stays on the server at `/root/.config/fakabot/alipay_sandbox_app_private.pem` (mode 0600).

Before accepting sandbox payments, configure Worker variables/secrets `ALIPAY_APP_ID`, `ALIPAY_SELLER_ID`, `ALIPAY_PUBLIC_KEY` (Alipay sandbox public key, not merchant app public key), and secret `ALIPAY_NOTIFY_TOKEN` (matching server `.env`). Set matching `ALIPAY_APP_ID` and `ALIPAY_SELLER_ID` in `/root/fakabot/.env`. Upload `/root/.config/fakabot/alipay_sandbox_app_public.pem` to the sandbox console as the app public key, and save the sandbox Alipay public key to `/root/.config/fakabot/alipay_sandbox_public.pem` (mode 0600). The merchant private key must never be uploaded to Alipay or pasted into chat.

The notify Worker health endpoint reports whether these values are configured. A missing configuration returns HTTP 503 from `/notify`; it never accepts unverified payment data.