# Fakabot

Telegram digital-goods storefront bot. Production checkout, payment confirmation, supplier procurement and delivery run on the existing servers; no service is moved by this release.

**Full sanitized source snapshot:** [fakabot-source-2026-11-17-r2.tar.gz](release-packages/2026-11-17-r2/fakabot-source-2026-11-17-r2.tar.gz)  
**Release notes and checksum:** [release package README](release-packages/2026-11-17-r2/README.md)

## Production topology

| Component | Existing location | Responsibility |
|---|---|---|
| Telegram bot and order business | Main server `/root/fakabot` | User interface, order records, payment confirmation, procurement orchestration and delivery |
| Supplier API | 167 server `167.253.96.220:8001` | Authenticated live catalog reads and upstream purchase using supplier balance |
| Alipay gateway | 167 server `127.0.0.1:8000` | Creates short-lived payment orders and sends authenticated callbacks |
| Gateway tunnel | Main server `127.0.0.1:18000` | Existing SSH tunnel to the 167 gateway; do not relocate it |
| Main payment callback | Main server `127.0.0.1:8011` | Validates gateway callback and persists confirmed payment state |
| TRC20 listener | Main server | Verifies on-chain USDT transfers and triggers the same fulfillment path |

## Customer purchase flow

1. The bot fetches categories/products directly from the authenticated supplier API on port `8001`.
2. Before accepting payment, `DahaoshangClient.preflight_purchase()` rechecks the exact supplier SKU, sale status, stock, quantity limits and CNY cost. A mismatch blocks checkout.
3. Alipay checkout creates a 60-second order through the existing 167 payment gateway. The callback is sent to `https://api.lynli.me/webhook/alipay_success`.
4. The main server checks the callback authentication, gateway order ID, business order ID, exact amount, pending status and expiry.
5. After payment is confirmed, the supplier client rechecks the live SKU and submits one purchase request to `http://167.253.96.220:8001/api/buy`.
6. The 167 API serializes supplier purchases and sends one upstream purchase request; it does not blindly retry an ambiguous result.
7. The returned card is saved to the order archive and sent to the buyer in Telegram. Failed/ambiguous supplier results are held for manual reconciliation.

USDT TRC20 payments use the existing main-server chain listener and the same order fulfillment function.

## Cloudflare D1 status

- The production Telegram bot no longer uses the Cloudflare Alipay notification Worker or its D1 `alipay_notifications` queue.
- The Alipay poller, old sandbox callback command, Worker URL settings and dormant Cloudflare catalog client have been removed from the active bot code.
- The deployed `fakabot-alipay-notify` Worker is retired. Its source is preserved under `archive/retired-cloudflare-alipay-notify/` for reference only; do not redeploy it.
- **The shared database `fakabot-procurement-db` is not deleted.** Cloudflare `fakabot-storefront`, `fakabot-procurement`, `fakabot-orders` and `supplier-auth-proxy` still have D1 bindings. Deleting the shared database would break those separate services. The bot's active product catalog and purchase path use the authenticated 167 supplier API instead.

## Fixed deployment paths and services

- Main server project: `/root/fakabot`
- Main bot service: `fakabot.service`
- 167 supplier API: `/root/my_shop_api.py`, `my-shop-api.service`, port `8001`
- 167 payment gateway: `/opt/pay-gateway`, `pay-gateway.service`, loopback port `8000`
- Main-server tunnel: `pay-gateway-tunnel.service`, local port `18000`

Do not move these services or change their listening locations as part of routine releases.

## 167 API source boundary

The live 167 API remains at `/root/my_shop_api.py` on that server. Keep supplier account credentials server-side; do not copy the live file into GitHub while it contains upstream authentication material. The main bot client and deployment contract are documented in `docs/SUPPLIER_BUY_API.md`.

## Configuration

Copy `.env.example` to `.env` and configure secrets locally. Never commit `.env`, supplier API keys, Telegram tokens, payment gateway tokens, private keys, account credentials, database files, or buyer order data.

Key names include `BOT_TOKEN`, `SHOP_BUY_API_KEY`, `SUPPLIER_AUTOPROCUREMENT_ENABLED`, `ALIPAY_PAYMENT_MODE=stateless_gateway`, `PAYMENT_GATEWAY_API_URL`, `PAYMENT_GATEWAY_API_TOKEN`, `PAYMENT_GATEWAY_CALLBACK_URL`, `PAYMENT_GATEWAY_WEBHOOK_TOKEN`, `TRON_RECEIVE_ADDRESS` and `TRONGRID_API_KEY`.

## Operations

```bash
systemctl status fakabot.service
systemctl restart fakabot.service
journalctl -u fakabot.service -n 100 --no-pager
curl -fsS http://127.0.0.1:18000/health
curl -fsS http://127.0.0.1:8011/health
```

The public callback is authenticated; a POST without `X-Payment-Gateway-Token` should return `401`. Do not expose the loopback listener directly to the public internet.

## Safe verification

- Python syntax: `./venv/bin/python -m compileall -q .`
- Live catalog read/preflight: read-only supplier API calls; no purchase is placed.
- Purchase flow tests must intercept the supplier `/api/buy` POST and use in-memory orders. Never test by spending supplier balance or making a real customer payment without separate explicit authorization.

See `docs/OPERATIONS.md`, `docs/SUPPLIER_BUY_API.md`, `docs/BUYER_GUIDE.md` and `docs/PRODUCTION_HANDOVER_2026-11-17.md`.
