# -*- coding: utf-8 -*-
"""Authenticated Dahaoshang client.

Only use a verified local session file. Purchase is deliberately balance-only:
the client refuses to create an order if the supplier does not expose a usable
account-balance payment method. It never signs or broadcasts cryptocurrency.
"""
import base64
import json
import os
import stat
from pathlib import Path

import requests


# Route supplier API traffic through the Cloudflare Worker when configured.
# Restrict the override to the known origin or the user's authenticated proxy.
_BASE_URL_OVERRIDE = os.getenv("SUPPLIER_API_BASE_URL", "https://auth.lynli.me").strip().rstrip("/")
if _BASE_URL_OVERRIDE not in ("https://dahaoshang.com", "https://auth.lynli.me"):
    raise RuntimeError("SUPPLIER_API_BASE_URL is not an approved HTTPS supplier endpoint")
BASE_URL = _BASE_URL_OVERRIDE
DEFAULT_SESSION_FILE = Path("/root/.config/fakabot/dahaoshang_session.json")


class SupplierError(RuntimeError):
    pass


class SupplierDeliveryPending(SupplierError):
    def __init__(self, order_id):
        self.order_id = str(order_id)
        super().__init__("上游订单已创建，但尚未取得可交付内容；禁止重复采购。订单号：" + self.order_id)


class DahaoshangClient:
    def __init__(self, session_file=DEFAULT_SESSION_FILE, http=None, timeout=20):
        self.session_file = Path(session_file)
        self.timeout = timeout
        self.http = http or requests.Session()
        self._load_session()

    def _load_session(self):
        token = None
        if self.session_file.is_file():
            mode = stat.S_IMODE(self.session_file.stat().st_mode)
            if mode & 0o077:
                raise SupplierError("大号商会话文件权限过宽，拒绝读取。")
            try:
                payload = json.loads(self.session_file.read_text(encoding="utf-8"))
            except Exception as exc:
                raise SupplierError("大号商会话文件无法解析。") from exc
            token = payload.get("access_token") if isinstance(payload, dict) else None
            base = payload.get("base_url", BASE_URL) if isinstance(payload, dict) else BASE_URL
            if base.rstrip("/") != BASE_URL:
                raise SupplierError("会话目标站点与固定上游地址不一致，拒绝请求。")
        else:
            session_url = os.getenv("SUPPLIER_SESSION_URL", "https://auth.lynli.me/_internal/session").strip()
            private_key_path = Path(os.getenv(
                "SUPPLIER_SESSION_PRIVATE_KEY",
                "/root/.config/fakabot/supplier_session_private.pem",
            ))
            if not session_url:
                raise SupplierError("缺少大号商登录会话；请先完成本机安全登录验证。")
            try:
                response = requests.get(session_url, timeout=self.timeout)
                response.raise_for_status()
                remote = response.json()
                encrypted = remote.get("encrypted_state") if isinstance(remote, dict) else None
                if not remote.get("ok") or not isinstance(encrypted, dict):
                    raise SupplierError("大号商网页登录授权尚未完成或已过期。")
                expires_at = remote.get("expires_at")
                if expires_at:
                    from datetime import datetime, timezone
                    expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
                    if expiry.tzinfo is None:
                        expiry = expiry.replace(tzinfo=timezone.utc)
                    if expiry <= datetime.now(timezone.utc):
                        raise SupplierError("大号商网页登录会话已过期，请重新授权。")
                if not private_key_path.is_file():
                    raise SupplierError("服务器缺少会话解密私钥。")
                from cryptography.hazmat.primitives import hashes, serialization
                from cryptography.hazmat.primitives.asymmetric import padding
                from cryptography.hazmat.primitives.ciphers.aead import AESGCM
                private_key = serialization.load_pem_private_key(
                    private_key_path.read_bytes(), password=None
                )
                aes_key = private_key.decrypt(
                    base64.b64decode(encrypted["wrapped_key"]),
                    padding.OAEP(
                        mgf=padding.MGF1(algorithm=hashes.SHA256()),
                        algorithm=hashes.SHA256(),
                        label=None,
                    ),
                )
                token_bytes = AESGCM(aes_key).decrypt(
                    base64.b64decode(encrypted["iv"]),
                    base64.b64decode(encrypted["ciphertext"]),
                    None,
                )
                token = token_bytes.decode("utf-8")
            except SupplierError:
                raise
            except Exception as exc:
                raise SupplierError("无法安全恢复大号商网页登录会话。") from exc

        if not isinstance(token, str) or not token.strip():
            raise SupplierError("大号商会话中没有有效令牌。")
        self.base_url = BASE_URL
        self.http.headers.update({
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Authorization": "Bearer " + token.strip(),
            "User-Agent": "Mozilla/5.0 (compatible; FakabotSupplierConnector/1.0)",
        })


    def _request(self, method, path, *, params=None, body=None):
        try:
            response = self.http.request(
                method, self.base_url + path, params=params, json=body,
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except requests.RequestException as exc:
            raise SupplierError("大号商接口请求失败：" + type(exc).__name__) from exc
        except ValueError as exc:
            raise SupplierError("大号商接口返回非 JSON 数据。") from exc
        if not isinstance(payload, dict) or payload.get("Code") != 200:
            message = payload.get("Message", "未知接口错误") if isinstance(payload, dict) else "响应格式错误"
            raise SupplierError("大号商接口未成功：" + str(message)[:160])
        return payload.get("Data")

    def get_user_info(self):
        data = self._request("POST", "/api/user/getuserinfo", body={})
        if not isinstance(data, dict) or not data:
            raise SupplierError("大号商登录令牌校验失败。")
        return data

    def get_categories(self):
        data = self._request("GET", "/api/syscategory/getcategorylist")
        if not isinstance(data, list):
            raise SupplierError("大号商分类响应格式错误。")
        return data

    def get_product_list(self, category_id="0"):
        """Read the current upstream product list for one category (0 = all)."""
        data = self._request(
            "GET", "/api/sysproduct/getproductlist",
            params={"categoryId": str(category_id)},
        )
        if not isinstance(data, list):
            raise SupplierError("大号商商品列表响应格式错误。")
        return data

    def get_product(self, product_id):
        data = self._request(
            "GET", "/api/sysproduct/getproduct",
            params={"productId": str(product_id)},
        )
        if not isinstance(data, dict):
            raise SupplierError("大号商商品详情响应格式错误。")
        return data

    def preflight_purchase(self, product_id, expected_price_cny, quantity=1, model_id=None, email=""):
        """Read-only checkout validation; never creates a supplier order."""
        from decimal import Decimal, InvalidOperation
        self.get_user_info()
        product = self.get_product(product_id)
        if str(product.get("ID") or "") != str(product_id):
            raise SupplierError("上游商品 ID 与请求不一致，拒绝收款。")
        if product.get("IsOnSale") is not True:
            raise SupplierError("上游商品当前未上架，拒绝收款。")
        try:
            live_price = Decimal(str(product.get("Price") or "0")).quantize(Decimal("0.01"))
            expected_price = Decimal(str(expected_price_cny)).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError, ValueError):
            raise SupplierError("无法核实上游实时价格，拒绝收款。")
        if live_price <= 0 or live_price != expected_price:
            raise SupplierError("上游价格已变化，请刷新商品目录后重新下单。")
        quantity = int(quantity)
        if quantity < 1:
            raise SupplierError("采购数量无效。")
        min_buy = int(product.get("MinBuy") or 0)
        max_buy = int(product.get("MaxBuy") or 0)
        if min_buy > 0 and quantity < min_buy or max_buy > 0 and quantity > max_buy:
            raise SupplierError("当前采购数量不符合上游限购规则。")
        models = product.get("ProductModels") or []
        selected_model = None
        if model_id is not None:
            selected_model = next((m for m in models if str(m.get("ID")) == str(model_id)), None)
            if selected_model is None:
                raise SupplierError("指定的上游商品规格不存在。")
        elif len(models) == 1:
            selected_model = models[0]
        elif len(models) > 1:
            raise SupplierError("该商品有多个规格，必须先完成规格选择，暂不收款。")
        if selected_model is not None and int(selected_model.get("Qty") or 0) < quantity:
            raise SupplierError("所选上游规格库存不足。")
        if selected_model is None and int(product.get("InStock") or 0) < quantity:
            raise SupplierError("上游商品库存不足。")
        category_id = str(product.get("CategoryId") or "")
        if not category_id:
            raise SupplierError("上游商品缺少分类 ID，拒绝收款。")
        methods = self.get_payment_methods(category_id, email=email)
        self._choose_balance_method(methods)
        return {
            "supplier_product_id": str(product_id),
            "supplier_category_id": category_id,
            "supplier_model_id": str(selected_model.get("ID")) if selected_model else "",
            "supplier_price_cny": format(live_price, ".2f"),
            "supplier_stock": int(selected_model.get("Qty") if selected_model else product.get("InStock") or 0),
            "supplier_verified": True,
            "procurement_ready": True,
        }

    def get_payment_methods(self, category_id, email=""):
        data = self._request(
            "GET", "/api/syspaytype/getpaymethod",
            params={"categoryId": str(category_id), "email": str(email or "")},
        )
        if not isinstance(data, list):
            raise SupplierError("大号商支付方式响应格式错误。")
        return data

    def get_order(self, order_id):
        """Read an existing supplier order; this method never creates purchases."""
        order = self._request(
            "POST", "/api/sysorder/getorder",
            params={"orderId": str(order_id)}, body=None,
        )
        if not isinstance(order, dict):
            raise SupplierDeliveryPending(order_id)
        content = order.get("Content")
        if not isinstance(content, str) or not content.strip():
            raise SupplierDeliveryPending(order_id)
        return {
            "supplier_order_id": str(order_id),
            "content": content,
            "supplier_status": order.get("ShipStatus"),
        }

    @staticmethod
    def _choose_balance_method(methods):
        markers = ("余额支付", "账户余额", "余额付款", "balance", "wallet balance")
        matches = []
        for method in methods:
            if not isinstance(method, dict) or method.get("Disabled") is True:
                continue
            label = " ".join(str(method.get(k) or "") for k in ("Text", "Name", "Title")).strip().lower()
            # Dahaoshang currently labels its usable account balance as e.g. "余额(¥30.30)".
            # Match that explicit balance-method prefix without confusing unrelated payment types.
            is_balance_label = label.startswith("余额(") or any(marker in label for marker in markers)
            if is_balance_label:
                matches.append(method)
        if len(matches) != 1 or not matches[0].get("Value"):
            raise SupplierError("上游没有唯一且可用的账户余额支付方式；为避免产生待付款订单，已拒绝采购。")
        return matches[0]

    def buy_with_balance(self, product_id, quantity=1, model_id=None, email="", expected_price_cny=None):
        """Create one supplier purchase and return verified order content.

        Do not automatically retry this method after a timeout: the upstream
        order may have been created even if the response was lost.
        """
        quantity = int(quantity)
        if quantity < 1:
            raise ValueError("quantity 必须大于 0")
        # Validate the bearer session before any purchase-related request.
        self.get_user_info()
        product = self.get_product(product_id)
        if expected_price_cny is not None:
            from decimal import Decimal, InvalidOperation
            try:
                live_price = Decimal(str(product.get("Price") or "0")).quantize(Decimal("0.01"))
                expected_price = Decimal(str(expected_price_cny)).quantize(Decimal("0.01"))
            except (InvalidOperation, TypeError, ValueError):
                raise SupplierError("采购前无法核实上游实时价格，拒绝扣款。")
            if live_price <= 0 or live_price != expected_price:
                raise SupplierError("付款后上游价格发生变化，拒绝按新价格扣款；订单转人工核对。")
        if product.get("IsOnSale") is not True:
            raise SupplierError("上游商品当前未上架，拒绝采购。")
        min_buy = int(product.get("MinBuy") or 0)
        max_buy = int(product.get("MaxBuy") or 0)
        if min_buy > 0 and quantity < min_buy:
            raise SupplierError("采购数量低于上游商品最小购买数量。")
        if max_buy > 0 and quantity > max_buy:
            raise SupplierError("采购数量超过上游商品最大购买数量。")

        models = product.get("ProductModels") or []
        selected_model = None
        if model_id is not None:
            selected_model = next((m for m in models if str(m.get("ID")) == str(model_id)), None)
            if selected_model is None:
                raise SupplierError("指定的上游商品规格不存在。")
        elif len(models) == 1:
            selected_model = models[0]
        elif len(models) > 1:
            available = [m for m in models if int(m.get("Qty") or 0) >= quantity]
            if len(available) == 1:
                selected_model = available[0]
            else:
                raise SupplierError("上游商品有多个规格，必须明确指定规格后才能采购。")

        if selected_model is not None and int(selected_model.get("Qty") or 0) < quantity:
            raise SupplierError("所选上游规格库存不足。")
        if selected_model is None and int(product.get("InStock") or 0) < quantity:
            raise SupplierError("上游商品库存不足。")

        category_id = product.get("CategoryId")
        if not category_id:
            raise SupplierError("上游商品缺少分类 ID，无法确定支付方式。")
        methods = self.get_payment_methods(category_id, email=email)
        pay_method = self._choose_balance_method(methods)

        body = {
            "ProductId": product.get("ID"),
            "PayTypeId": pay_method["Value"],
            "Qty": quantity,
            "Email": str(email).strip() or None,
        }
        if selected_model is not None:
            body["ModelId"] = selected_model.get("ID")

        data = self._request("POST", "/api/sysorder/buy", body=body)
        if not isinstance(data, dict):
            raise SupplierError("上游下单响应格式错误；禁止自动重试。")
        entity = data.get("Entity") if isinstance(data.get("Entity"), dict) else {}
        order_id = entity.get("ID") or data.get("ID")
        if not order_id:
            raise SupplierError("上游下单响应缺少订单 ID；请人工核对，不要重试。")

        result = self.get_order(order_id)
        result["supplier_order_no"] = str(entity.get("OrderNo") or data.get("OrderNo") or "")
        return result