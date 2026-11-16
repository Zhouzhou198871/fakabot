# FakaBot source snapshot — 2026-11-17

Sanitized source snapshot captured from the deployed `/root/fakabot` tree on 2026-11-17. It includes the current bot source and the MacroDroid notification handling adjustment (notifications are informational only and never constitute payment proof).

Included: Telegram bot, payment/order logic, Alipay RSA2 and VMQ verification, TRC20 watcher, supplier client, catalog source, Cloudflare Worker source, scripts, docs, and brand assets.

Excluded: `.env` and all credentials/secrets/private keys, virtualenv, `.git`, runtime databases and user/order data, logs/locks/caches, backup files, and local receiving QR/profile images. Runtime secrets must be provisioned separately.
