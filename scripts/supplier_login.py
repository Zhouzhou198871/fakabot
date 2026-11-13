#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Interactive supplier login that stores only an encrypted session in Cloudflare.

The account and password are entered locally at the terminal. They are never
written to disk or printed. This does not bypass login, MFA, CAPTCHA, or any
supplier access control. If the site requires an additional challenge, stop
and complete it through the official website.
"""
import getpass
import json
import re
import sys
from pathlib import Path

import requests
from cryptography.hazmat.primitives import serialization

PROXY = "https://auth.lynli.me"
TIMEOUT = 25
PRIVATE_KEY = Path("/root/.config/fakabot/supplier_session_private.pem")
WORKER_SOURCE = Path(__file__).resolve().parents[1] / "cloudflare" / "supplier-auth-proxy" / "worker.js"


def verify_key_pair():
    source = WORKER_SOURCE.read_text(encoding="utf-8")
    match = re.search(r'const SESSION_PUBLIC_KEY = "(.*?)";', source, re.S)
    if not match:
        return False
    public_pem = match.group(1).encode().decode("unicode_escape")
    private_key = serialization.load_pem_private_key(PRIVATE_KEY.read_bytes(), password=None)
    public_key = serialization.load_pem_public_key(public_pem.encode())
    private_der = private_key.public_key().public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    public_der = public_key.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_der == public_der


def main():
    if not PRIVATE_KEY.is_file():
        print("缺少服务器会话解密私钥；请先在服务器安全配置密钥。", file=sys.stderr)
        return 2
    if PRIVATE_KEY.stat().st_mode & 0o077:
        print("会话私钥权限必须为 600；拒绝继续。", file=sys.stderr)
        return 2
    try:
        if not verify_key_pair():
            print("服务器私钥与 Cloudflare 公钥不匹配；拒绝登录。", file=sys.stderr)
            return 2
    except Exception:
        print("无法验证会话加密密钥；拒绝登录。", file=sys.stderr)
        return 2

    account = input("供应商账号：").strip()
    if not account:
        print("账号不能为空。", file=sys.stderr)
        return 2
    password = getpass.getpass("供应商密码（输入不回显）：")
    if not password:
        print("密码不能为空。", file=sys.stderr)
        return 2

    session = requests.Session()
    session.headers.update({
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Origin": "https://dahaoshang.com",
        "Referer": "https://dahaoshang.com/",
        "User-Agent": "Mozilla/5.0 (compatible; FakabotOwnerLogin/1.0)",
    })
    try:
        response = session.post(
            PROXY + "/api/user/loginjwt",
            json={"Account": account, "Password": password},
            timeout=TIMEOUT,
        )
        password = None
        response.raise_for_status()
        payload = response.json()
        data = payload.get("Data") if isinstance(payload, dict) else None
        token = data.get("access_token") if isinstance(data, dict) else None
        if payload.get("Code") != 200 or not isinstance(token, str) or len(token) < 20:
            print("登录未成功；请通过供应商官网核对账号、密码或额外验证要求。", file=sys.stderr)
            return 1

        verify = session.post(
            PROXY + "/api/user/getuserinfo",
            json={},
            headers={"Authorization": "Bearer " + token},
            timeout=TIMEOUT,
        )
        verify.raise_for_status()
        verify_payload = verify.json()
        if not isinstance(verify_payload, dict) or verify_payload.get("Code") != 200 or not isinstance(verify_payload.get("Data"), dict):
            print("登录响应已收到，但账户会话校验失败。", file=sys.stderr)
            return 1

        status_response = session.get(PROXY + "/_internal_session_status", timeout=TIMEOUT)
        status_response.raise_for_status()
        status = status_response.json()
        if status.get("status") != "authorized" or status.get("usable") is not True:
            print("会话未能安全保存到 Cloudflare；未启用采购。", file=sys.stderr)
            return 1

        print(json.dumps({
            "login": "success",
            "session_storage": "Cloudflare D1 encrypted",
            "session_usable": True,
            "expires_at": status.get("expires_at"),
            "password_persisted": False,
            "token_printed": False,
        }, ensure_ascii=False))
        return 0
    except requests.RequestException as exc:
        print("登录请求失败：" + type(exc).__name__, file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError):
        print("供应商返回格式异常；未确认会话状态。", file=sys.stderr)
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())