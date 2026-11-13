# -*- coding: utf-8 -*-
"""Alipay Open Platform RSA2 page-pay URL builder (sandbox by default)."""
import base64
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

SANDBOX_GATEWAY = "https://openapi.alipaydev.com/gateway.do"


def build_page_pay_url(order_id, amount_cny, subject, app_id=None,
                       private_key_path=None, notify_url=None, gateway=None):
    app_id = str(app_id or os.getenv("ALIPAY_APP_ID", "")).strip()
    private_key_path = str(private_key_path or os.getenv(
        "ALIPAY_APP_PRIVATE_KEY_FILE", "/root/.config/fakabot/alipay_sandbox_app_private.pem"))
    notify_url = str(notify_url or os.getenv(
        "ALIPAY_NOTIFY_URL", "https://fakabot-alipay-notify.chulingli1988.workers.dev/notify")).strip()
    gateway = str(gateway or os.getenv("ALIPAY_GATEWAY", SANDBOX_GATEWAY)).strip()
    if not app_id:
        raise RuntimeError("缺少 ALIPAY_APP_ID；支付宝沙箱应用尚未配置")
    if not notify_url.startswith("https://"):
        raise RuntimeError("支付宝异步通知地址必须使用公网 HTTPS")
    key_data = Path(private_key_path).read_bytes()
    private_key = serialization.load_pem_private_key(key_data, password=None)
    biz = {
        "out_trade_no": str(order_id),
        "product_code": "FAST_INSTANT_TRADE_PAY",
        "total_amount": format(float(amount_cny), ".2f"),
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
    message = "&".join(f"{k}={params[k]}" for k in sorted(params))
    signature = private_key.sign(message.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    params["sign"] = base64.b64encode(signature).decode("ascii")
    return gateway + "?" + urlencode(params)