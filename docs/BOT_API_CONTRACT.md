# 机器人对接 API 合约（预留）

机器人暂不开发。本文件冻结后续对接的接口路径、字段和安全边界。只有标注“已上线”的接口当前可调用；“预留”接口是合同，不代表已部署。

## 服务根地址

- 商品读取：`https://fakabot-storefront.chulingli1988.workers.dev`
- 订单/采购服务：后续部署在 Cloudflare Worker，沿用 D1 订单、支付事件、采购任务和审计表。

## 已上线只读接口

### GET /health
返回服务、D1 和目录健康状态。

### GET /v1/catalog
返回当前版本的在售商品、库存状态、售价、商品说明和目录版本。公开数据，不含账号凭据。

### GET /v1/categories
返回有货分类及商品数量。

### GET /v1/inventory/summary
只返回库存类型、状态和数量，不返回凭据。

## 预留订单接口

### POST /v1/orders
由机器人在用户确认商品后创建待付款订单。

请求头：
- `Authorization: Bearer <BOT_API_TOKEN>`
- `Idempotency-Key: <unique-client-request-id>`
- `Content-Type: application/json`

请求体：
```json
{
  "telegram_user_id": "string",
  "supplier_product_id": "string",
  "variant_id": null,
  "quantity": 1,
  "catalog_version": "ISO-8601 version",
  "expected_sell_price_usdt": "decimal string"
}
```

服务端必须重新读取当前商品快照并核对货号、版本、价格、在售状态、库存和数量上限；不匹配时拒绝创建订单。成功后返回 `order_id`、`payment_status=pending`、应付金额和支付期限。重复幂等键返回同一订单，不重复建单。

### GET /v1/orders/{order_id}
只允许订单所属用户或有权限的内部服务读取。返回支付、采购和交付状态；绝不在列表/普通查询中泄露明文账号密码。

### POST /v1/internal/payment-events
仅支付监听服务可调用。使用独立的 `PAYMENT_LISTENER_TOKEN`。必须校验金额、币种、链/网络、收款地址、确认数和订单状态；`event_id` 与 `payment_ref` 唯一，重复回调幂等。

### POST /v1/internal/procurement-jobs/{order_id}/run
仅采购执行器可调用。支付确认后排队执行；先锁定订单/库存，再核对上游商品、规格、实时价格和余额支付方式。每个订单最多只有一个有效采购任务。网络超时后先查询原上游订单，不得直接重买。

### POST /v1/internal/orders/{order_id}/delivery/retry
仅内部服务可调用。只重试交付通知，不重新采购。交付凭据在 D1 加密存储，读取后只发送给订单所属用户，并写审计日志。

## 状态机

```text
pending_payment
  -> paid
  -> procurement_queued
  -> purchasing
  -> supplier_order_pending
  -> purchased
  -> delivered

异常分支：
payment_expired / payment_mismatch
procurement_failed / manual_review
delivery_pending -> delivery_retry -> delivered
```

## 安全要求
- 机器人 token、支付监听 token、采购执行器 token 分开；不复用目录同步 token。
- 所有写接口鉴权、限流、幂等、审计；用户只能访问自己的订单。
- 订单创建不代表支付成功；仅独立支付监听验证链上/支付提供方数据后才标记 paid。
- 不完整库存保持 `needs_credentials` 或 `quarantined`，永远不能被锁定为可售。
- 采购失败或响应超时必须人工核对上游订单状态，禁止盲目重试。
- 账号密码仅密文存储，交付响应不缓存；不写入应用日志。
- 采购功能必须有单笔金额上限、日限额、余额阈值和全局 kill switch。

## 当前部署状态
- 商品目录、分类、库存摘要：已上线。
- 订单写入、支付事件、采购执行、交付重试：接口合约已冻结，尚未部署为可调用路由。
- 机器人：本阶段不改动。


## 2026-11-14 部署更新：订单 API 第一阶段

独立 Worker 已部署：
- URL：`https://fakabot-orders.chulingli1988.workers.dev`
- Worker：`fakabot-orders`
- D1：继续使用现有 `fakabot-procurement-db`，新建独立 `faka_*` 订单表，不改旧表。
- `GET /health` 已通过生产验证，D1 connected，目录版本 1,561 products / 28 categories。
- `POST /v1/orders`：按最新目录、库存、数量和价格快照创建 20 分钟待支付订单；幂等键唯一。
- `POST /v1/internal/payment-events`：独立支付监听器 token；校验 verified 标志、币种、精确金额、订单状态、过期时间、event_id 与 payment_ref 唯一性；支付成功只创建一条采购任务。
- `GET /v1/orders/{order_id}`：按 Telegram 用户 ID 校验订单归属。
- `GET /v1/internal/procurement-jobs`：采购执行器专用，采购暂停时返回 423。
- 未认证的订单写入请求已实测返回 HTTP 401。

安全配置：
- 三个独立 token 已生成并保存在服务器 `/root/.config/fakabot/orders_api_tokens.env`，权限 600。
- 尚未将这三个 token 安装到 Worker Secrets；可在有 Wrangler 登录权限的终端运行 `bash /root/fakabot/cloudflare/orders-api/configure_secrets.sh`，或在 Cloudflare Dashboard 手动添加对应 secrets。
- `PROCUREMENT_ENABLED=false`，不得启用。

仍未完成的真实业务闭环：
- 供应商采购执行器尚未部署；不能把支付成功等同于已采购。
- 供应商会话解密目前依赖服务器私钥，尚未安全迁移到 Cloudflare Worker。
- 交付凭据的 Worker 端加密/解密与客户安全领取接口尚未上线。
- 支付监听器尚未接入新 webhook（本阶段按约定未修改支付监听器）。
- 真实交易、扣款和发货验收均未执行。