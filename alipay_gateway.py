
# -*- coding: utf-8 -*-
"""Alipay Open Platform RSA2 page-pay URL builder (sandbox by default)."""
import base64
import hashlib
import hmac
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode, quote
from decimal import Decimal, InvalidOperation

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

SANDBOX_GATEWAY = "https://openapi-sandbox.dl.alipaydev.com/gateway.do"
CHECKOUT_BASE_URL = "https://api.lynli.me/alipay/checkout"



def _pem_bytes(inline_name, file_name, default_path):
    """Load PEM material from an environment value or a permission-restricted file."""
    inline = os.getenv(inline_name, "").strip()
    if inline:
        return inline.replace("\\n", "\n").encode("utf-8")
    path = str(os.getenv(file_name, default_path)).strip()
    return Path(path).read_bytes()


def _sandbox_gateway(gateway):
    gateway = str(gateway or SANDBOX_GATEWAY).strip()
    if os.getenv("ALIPAY_ENV", "sandbox").strip().lower() == "sandbox" and gateway != SANDBOX_GATEWAY:
        raise RuntimeError("沙箱模式禁止使用非沙箱支付宝网关")
    return gateway


def build_checkout_url(order_id, gateway_url, expires_at_ms, base_url=None, qr_code=None, amount_cny=None, personal_qr=False):
    """Return a stateless HTTPS checkout link carrying the already-signed gateway request."""
    base = str(base_url or os.getenv("ALIPAY_CHECKOUT_BASE_URL", CHECKOUT_BASE_URL)).strip().rstrip("/")
    if not base.startswith("https://"):
        raise RuntimeError("支付宝收银台中转地址必须使用 HTTPS")
    payload = json.dumps({
        "order_id": str(order_id),
        "gateway_url": str(gateway_url or ""),
        "expires_at_ms": int(expires_at_ms),
        **({"qr_code": str(qr_code)} if qr_code else {}),
        **({"amount_cny": str(amount_cny)} if amount_cny is not None else {}),
        **({"personal_qr": True} if personal_qr else {}),
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    key_path = Path(os.getenv("ALIPAY_CHECKOUT_HMAC_KEY_FILE", "/root/.config/fakabot/alipay_checkout_hmac.key"))
    if key_path.is_file():
        payload_obj = json.loads(payload.decode("utf-8"))
        payload_obj["sig"] = hmac.new(key_path.read_bytes(), payload, hashlib.sha256).hexdigest()
        payload = json.dumps(payload_obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    elif qr_code or personal_qr:
        raise RuntimeError("支付宝收银台令牌签名密钥缺失")
    token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    return base + "/" + token



def build_page_pay_url(order_id, amount_cny, subject, app_id=None,
                       private_key_path=None, notify_url=None, gateway=None):
    app_id = str(app_id or os.getenv("ALIPAY_APP_ID", "")).strip()
    private_key_path = str(private_key_path or os.getenv(
        "ALIPAY_APP_PRIVATE_KEY_FILE", "/root/.config/fakabot/alipay_sandbox_app_private.pem"))
    notify_url = str(notify_url or os.getenv(
        "ALIPAY_NOTIFY_URL", "https://fakabot-alipay-notify.chulingli1988.workers.dev/notify")).strip()
    gateway = _sandbox_gateway(gateway or os.getenv("ALIPAY_GATEWAY", SANDBOX_GATEWAY))
    if not app_id:
        raise RuntimeError("缺少 ALIPAY_APP_ID；支付宝沙箱应用尚未配置")
    if not notify_url.startswith("https://"):
        raise RuntimeError("支付宝异步通知地址必须使用公网 HTTPS")
    try:
        amount = Decimal(str(amount_cny)).quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RuntimeError("支付宝订单金额无效") from exc
    if not amount.is_finite() or amount <= 0:
        raise RuntimeError("支付宝订单金额必须大于零")
    key_data = _pem_bytes("ALIPAY_APP_PRIVATE_KEY", "ALIPAY_APP_PRIVATE_KEY_FILE", private_key_path)
    private_key = serialization.load_pem_private_key(key_data, password=None)
    biz = {
        "out_trade_no": str(order_id),
        "product_code": "FAST_INSTANT_TRADE_PAY",
        "total_amount": format(amount, ".2f"),
        "subject": str(subject)[:128],
    }
    params = {
        "app_id": app_id,
        "method": "alipay.trade.page.pay",
        "format": "JSON",
        "charset": "utf-8",
        "sign_type": "RSA2",
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "version": "1.0",
        "notify_url": notify_url,
        "biz_content": json.dumps(biz, ensure_ascii=False, separators=(",", ":")),
    }
    # Gateway request signing includes sign_type; exclude only sign.
    signed_params = {k: v for k, v in params.items() if k != "sign" and v != ""}
    message = "&".join(f"{k}={signed_params[k]}" for k in sorted(signed_params))
    signature = private_key.sign(message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    params["sign"] = base64.b64encode(signature).decode("ascii")
    return gateway + "?" + urlencode(params)


def create_precreate_qr(order_id, amount_cny, subject, app_id=None,
                        private_key_path=None, alipay_public_key_path=None,
                        notify_url=None, gateway=None):
    """Create a real Alipay precreate QR and verify the signed gateway response."""
    import urllib.request
    from urllib.parse import urlencode
    app_id = str(app_id or os.getenv("ALIPAY_APP_ID", "")).strip()
    private_key_path = str(private_key_path or os.getenv(
        "ALIPAY_APP_PRIVATE_KEY_FILE", "/root/.config/fakabot/alipay_sandbox_app_private.pem"))
    alipay_public_key_path = str(alipay_public_key_path or os.getenv(
        "ALIPAY_PUBLIC_KEY_FILE", "/root/.config/fakabot/alipay_sandbox_public.pem"))
    notify_url = str(notify_url or os.getenv(
        "ALIPAY_NOTIFY_URL", "https://fakabot-alipay-notify.chulingli1988.workers.dev/notify")).strip()
    gateway = _sandbox_gateway(gateway or os.getenv("ALIPAY_GATEWAY", SANDBOX_GATEWAY))
    if not app_id or not notify_url.startswith("https://"):
        raise RuntimeError("支付宝 AppID 或 HTTPS 异步通知地址未配置")
    amount = format(float(amount_cny), ".2f")
    biz = {"out_trade_no": str(order_id), "total_amount": amount,
           "subject": str(subject)[:128], "timeout_express": "20m"}
    params = {"app_id": app_id, "method": "alipay.trade.precreate", "format": "JSON",
              "charset": "utf-8", "sign_type": "RSA2",
              "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
              "version": "1.0", "notify_url": notify_url,
              "biz_content": json.dumps(biz, ensure_ascii=False, separators=(",", ":"))}
    private_key = serialization.load_pem_private_key(
        _pem_bytes("ALIPAY_APP_PRIVATE_KEY", "ALIPAY_APP_PRIVATE_KEY_FILE", private_key_path), password=None)
    canonical = "&".join(f"{k}={params[k]}" for k in sorted(params) if params[k] != "" )
    params["sign"] = base64.b64encode(private_key.sign(
        canonical.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())).decode("ascii")
    request = urllib.request.Request(gateway, data=urlencode(params).encode("utf-8"),
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
                 "User-Agent": "QingtianMall-Alipay/1.0"}, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read(1024 * 128).decode("utf-8")
    payload = json.loads(raw)
    result = payload.get("alipay_trade_precreate_response")
    signature = payload.get("sign")
    if not isinstance(result, dict) or not signature:
        raise RuntimeError("支付宝预下单响应缺少签名或业务结果")
    public_key = serialization.load_pem_public_key(
        _pem_bytes("ALIPAY_PUBLIC_KEY", "ALIPAY_PUBLIC_KEY_FILE", alipay_public_key_path))
    signature_bytes = base64.b64decode(signature, validate=True)
    # Alipay OpenAPI response verification signs the response object JSON. Some
    # gateway responses contain URLs whose slashes must remain escaped (\\/).
    # Try the compact JSON representation first, then the documented escaped form.
    candidates = []
    # Preserve the exact JSON bytes of the signed response node; parsing and
    # re-serializing can change escaping/order and invalidate an otherwise good signature.
    marker = '"alipay_trade_precreate_response"'
    marker_at = raw.find(marker)
    if marker_at >= 0:
        colon_at = raw.find(":", marker_at + len(marker))
        if colon_at >= 0:
            raw_start = colon_at + 1
            while raw_start < len(raw) and raw[raw_start].isspace():
                raw_start += 1
            _, raw_end = json.JSONDecoder().raw_decode(raw, raw_start)
            candidates.append(raw[raw_start:raw_end])
    # Alipay signs compact response JSON while preserving the gateway's key order.
    compact_preserve_order = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    if compact_preserve_order not in candidates:
        candidates.append(compact_preserve_order)
    compact = json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if compact not in candidates:
        candidates.append(compact)
    escaped = compact.replace("/", "\\/")
    if escaped not in candidates:
        candidates.append(escaped)
    # The Alipay OpenAPI response-signature examples use lexicographically
    # sorted JSON keys and escaped forward slashes. Try that exact form too.
    sorted_ascii = json.dumps(result, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    if sorted_ascii not in candidates:
        candidates.append(sorted_ascii)
    sorted_ascii_escaped = sorted_ascii.replace("/", "\\/")
    if sorted_ascii_escaped not in candidates:
        candidates.append(sorted_ascii_escaped)
    verified = False
    for candidate in candidates:
        try:
            public_key.verify(signature_bytes, candidate.encode("utf-8"),
                              padding.PKCS1v15(), hashes.SHA256())
            verified = True
            break
        except Exception as exc:
            if type(exc).__name__ != "InvalidSignature":
                raise
    if not verified:
        # Include only non-sensitive provider status fields to distinguish a
        # key/configuration mismatch from a business-level gateway rejection.
        diagnostic = " / ".join(str(result.get(k) or "") for k in ("code", "msg", "sub_code", "sub_msg"))
        raise RuntimeError("支付宝预下单响应验签失败；响应状态：" + (diagnostic or "未提供"))
    if str(result.get("code")) != "10000":
        raise RuntimeError("支付宝预下单失败：" + str(result.get("sub_msg") or result.get("msg") or result.get("code")))
    if str(result.get("out_trade_no")) != str(order_id):
        raise RuntimeError("支付宝预下单返回订单号不匹配")
    qr_code = str(result.get("qr_code") or "")
    if not qr_code.startswith("https://"):
        raise RuntimeError("支付宝未返回有效的官方二维码内容")
    return qr_code, result
