#!/usr/bin/env python3
"""Publish the last complete read-only supplier snapshot to Cloudflare D1."""
import json
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = ROOT / "products" / "upstream_catalog.json"
TOKEN_FILE = ROOT / ".catalog_sync_token"
ENDPOINT = "https://fakabot-procurement.chulingli1988.workers.dev/_internal/catalog-sync"


def main():
    payload = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    if not token or len(token) < 48:
        raise SystemExit("catalog sync credential missing or invalid")
    if not payload.get("categories") or not payload.get("products"):
        raise SystemExit("refusing to publish an empty supplier catalog")
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(ENDPOINT, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + token,
        "User-Agent": "FakabotCatalogSync/1.0",
    })
    try:
        with urlopen(request, timeout=90) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", "replace")
        raise SystemExit(f"Cloudflare catalog sync failed: HTTP {exc.code}: {detail}")
    except URLError as exc:
        raise SystemExit(f"Cloudflare catalog sync network error: {type(exc).__name__}")
    if not result.get("ok"):
        raise SystemExit("Cloudflare catalog sync returned an unsuccessful result")
    print(json.dumps({k: result.get(k) for k in ("ok", "synced_at", "products", "categories", "active_products")}, ensure_ascii=False))


if __name__ == "__main__":
    main()