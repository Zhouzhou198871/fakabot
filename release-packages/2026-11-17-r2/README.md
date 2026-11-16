# Fakabot source snapshot — 2026-11-17 r2

This release package contains the complete sanitized source snapshot of the deployed main-server bot project after retiring the Alipay D1 queue and fixing supplier live preflight.

- Archive: `fakabot-source-2026-11-17-r2.tar.gz`
- Size: 224,866 bytes
- SHA-256: `6f432cd00323b3ab00987d4e53a9eb0243c326e977a4c3843c818d77f2fb4129`
- Files: 93

Included: bot/payment/supplier client code, Cloudflare legacy modules and retained retirement source, scripts, assets, buyer/operations documentation and deployment examples.

Excluded: populated `.env`, catalog sync token, supplier credentials, private keys, QR payment material, buyer/order data, local inventory data, runtime product snapshots, logs, virtualenv, Python caches, database files and backups.

Validation completed:
- Python compileall: PASS
- Live supplier catalog refresh: PASS (29 categories / 1,186 active products at deployment check)
- Supplier preflight: PASS (read-only)
- Mock payment-to-purchase-to-delivery flow: PASS
- Real supplier purchases: 0
- Real customer payments: 0

The shared Cloudflare D1 database is retained for separate Workers; the Alipay notification Worker and the bot's D1 queue consumer are retired.
