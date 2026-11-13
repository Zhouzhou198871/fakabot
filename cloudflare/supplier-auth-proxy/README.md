# Supplier authentication proxy

This Worker proxies the public Dahaoshang web application through `auth.lynli.me`.
After a successful `POST /api/user/loginjwt`, it captures only the returned access token and stores it encrypted in D1.

## Session protection

- The Worker encrypts the token with a fresh AES-256-GCM key.
- That AES key is wrapped with the server's RSA-OAEP-256 public key.
- Only the matching private key on the bot server can decrypt the token.
- The D1 row and `/_internal/session` endpoint contain ciphertext only.
- Logging out revokes and clears the stored session.
- Never commit the matching private key.

## Deploy

The existing Cloudflare D1 binding is `DB`, database `fakabot-procurement-db`.
The server's private key is `/root/.config/fakabot/supplier_session_private.pem` (mode 0600).
If replacing that key pair, regenerate the RSA key on the server and update `SESSION_PUBLIC_KEY` in `worker.js` with the corresponding public key before deploying.

From this directory, deploy with Wrangler using the existing Worker name and custom domain. After deployment, set `SUPPLIER_SESSION_URL=https://auth.lynli.me/_internal/session` in the bot's protected `.env`, then restart the bot.

The account owner completes one normal login at `https://auth.lynli.me/login`. The Worker persists the encrypted session automatically; no password or plaintext token is sent to the bot server.
