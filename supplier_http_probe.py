#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only inspection of the Dahaoshang login page.

This diagnostic deliberately does not submit forms, follow authenticated links,
or print cookie/token values. Run it on the VPS with the same network path as
the bot to identify whether a lightweight requests-based integration is viable.
"""
import json
import re
import sys
from html.parser import HTMLParser
from urllib.parse import urljoin

import requests

BASE_URL = "https://dahaoshang.com/"
TIMEOUT = 15


class PageInspector(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title_parts = []
        self.in_title = False
        self.forms = []
        self.scripts = []
        self.meta_names = []
        self._form = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self.in_title = True
        elif tag == "form":
            self._form = {
                "method": (attrs.get("method") or "get").lower(),
                "action": attrs.get("action") or "",
                "fields": [],
            }
            self.forms.append(self._form)
        elif tag == "input" and self._form is not None:
            self._form["fields"].append({
                "name": attrs.get("name") or "",
                "type": (attrs.get("type") or "text").lower(),
            })
        elif tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])
        elif tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            if name in ("csrf-token", "csrf_token", "_token", "x-csrf-token"):
                self.meta_names.append(name)

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        elif tag == "form":
            self._form = None

    def handle_data(self, data):
        if self.in_title:
            self.title_parts.append(data.strip())


def main():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; SupplierReadOnlyProbe/1.0)",
        "Accept": "text/html,application/xhtml+xml",
    })
    try:
        response = session.get(BASE_URL, timeout=TIMEOUT, allow_redirects=True)
        response.raise_for_status()
    except requests.RequestException as exc:
        print(json.dumps({
            "ok": False,
            "stage": "homepage_get",
            "error": type(exc).__name__,
            "message": str(exc)[:300],
        }, ensure_ascii=False, indent=2))
        return 2

    parser = PageInspector()
    parser.feed(response.text)
    cookie_names = sorted({cookie.name for cookie in session.cookies})
    result = {
        "ok": True,
        "requested_url": BASE_URL,
        "final_url": response.url,
        "http_status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "html_bytes": len(response.content),
        "title": " ".join(x for x in parser.title_parts if x),
        "forms": [
            {
                "method": form["method"],
                "action": urljoin(response.url, form["action"]),
                "fields": form["fields"],
            }
            for form in parser.forms
        ],
        "script_urls": [urljoin(response.url, src) for src in parser.scripts[:40]],
        "csrf_meta_names": parser.meta_names,
        "cookie_names_only": cookie_names,
        "notes": [
            "Read-only GET probe; no credentials were sent.",
            "Cookie values, form values, and response bodies are never printed.",
            "A browser challenge or JavaScript-only login may require a different approach.",
        ],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
