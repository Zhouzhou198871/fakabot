# Fakabot

Telegram 数字商品商城机器人。生产环境中的下单、支付确认、供应商采购和发货均运行在现有服务器上；本次发布不会迁移任何服务。

**完整脱敏源码快照：** [fakabot-source-2026-11-17-r2.tar.gz](release-packages/2026-11-17-r2/fakabot-source-2026-11-17-r2.tar.gz)  
**发布说明与校验和：** [发布包说明](release-packages/2026-11-17-r2/README.md)

## 生产环境架构

| 组件 | 现有位置 | 职责 |
|---|---|---|
| Telegram 机器人及订单业务 | 主服务器 `/root/fakabot` | 用户界面、订单记录、支付确认、采购调度和发货 |
| 供应商 API | 167 服务器 `167.253.96.220:8001` | 通过身份验证读取实时商品目录，并使用供应商余额向上游下单 |
| 支付宝网关 | 167 服务器 `127.0.0.1:8000` | 创建短时有效的支付订单，并发送经过身份验证的回调 |
| 网关隧道 | 主服务器 `127.0.0.1:18000` | 连接 167 支付网关的现有 SSH 隧道；不要迁移 |
| 主支付回调服务 | 主服务器 `127.0.0.1:8011` | 验证网关回调并保存已确认的支付状态 |
| TRC20 监听器 | 主服务器 | 验证链上 USDT 转账，并触发相同的履约流程 |

## 用户购买流程

1. 机器人通过已完成身份验证的供应商 API（端口 `8001`）直接获取商品分类和商品信息。
2. 在接受付款前，`DahaoshangClient.preflight_purchase()` 会重新核验准确的供应商 SKU、销售状态、库存、数量限制和人民币成本。任何信息不匹配都会阻止创建支付订单。
3. 支付宝结账通过现有的 167 支付网关创建一笔有效期为 60 秒的订单。回调发送至 `https://api.lynli.me/webhook/alipay_success`。
4. 主服务器检查回调身份验证信息、网关订单 ID、业务订单 ID、准确金额、待支付状态和订单有效期。
5. 确认付款后，供应商客户端会再次核验实时 SKU，并向 `http://167.253.96.220:8001/api/buy` 提交一次采购请求。
6. 167 API 会对供应商采购请求进行串行处理，并向上游发送一次采购请求；对于结果不明确的请求，不会盲目重试。
7. 返回的卡密会保存到订单归档，并通过 Telegram 发送给买家。供应商采购失败或结果不明确的订单会暂存，等待人工核对处理。

USDT TRC20 付款由主服务器现有的链上监听器处理，并使用相同的订单履约函数。

## Cloudflare D1 状态

- 生产环境 Telegram 机器人已不再使用 Cloudflare 支付宝通知 Worker，也不再使用其 D1 数据库队列 `alipay_notifications`。
- 支付宝轮询器、旧版沙盒回调命令、Worker URL 配置以及休眠状态的 Cloudflare 商品目录客户端，均已从机器人当前启用的代码中移除。
- 已部署的 `fakabot-alipay-notify` Worker 已停用。其源码仅作为参考保存在 `archive/retired-cloudflare-alipay-notify/` 中；不要重新部署。
- **共享数据库 `fakabot-procurement-db` 并未删除。** Cloudflare 的 `fakabot-storefront`、`fakabot-procurement`、`fakabot-orders` 和 `supplier-auth-proxy` 仍绑定了该 D1 数据库。删除这个共享数据库会导致其他独立服务故障。机器人当前启用的商品目录和采购流程改为使用经过身份验证的 167 供应商 API。

## 固定部署路径与服务

- 主服务器项目：`/root/fakabot`
- 主机器人服务：`fakabot.service`
- 167 供应商 API：`/root/my_shop_api.py`、`my-shop-api.service`，端口 `8001`
- 167 支付网关：`/opt/pay-gateway`、`pay-gateway.service`，本机回环端口 `8000`
- 主服务器隧道：`pay-gateway-tunnel.service`，本地端口 `18000`

日常发布过程中，不要迁移这些服务，也不要更改它们的监听地址。

## 167 API 源码与安全边界

正在运行的 167 API 位于该服务器的 `/root/my_shop_api.py`。供应商账户凭据必须保留在服务器端；如果该文件包含上游身份验证信息，不要将其复制到 GitHub。主机器人客户端及部署约定记录在 `docs/SUPPLIER_BUY_API.md` 中。

## 配置

将 `.env.example` 复制为 `.env`，并在本地配置密钥。严禁提交 `.env`、供应商 API 密钥、Telegram Token、支付网关 Token、私钥、账户凭据、数据库文件或买家订单数据。

主要配置项包括：`BOT_TOKEN`、`SHOP_BUY_API_KEY`、`SUPPLIER_AUTOPROCUREMENT_ENABLED`、`ALIPAY_PAYMENT_MODE=stateless_gateway`、`PAYMENT_GATEWAY_API_URL`、`PAYMENT_GATEWAY_API_TOKEN`、`PAYMENT_GATEWAY_CALLBACK_URL`、`PAYMENT_GATEWAY_WEBHOOK_TOKEN`、`TRON_RECEIVE_ADDRESS` 和 `TRONGRID_API_KEY`。

## 运维命令

```bash
systemctl status fakabot.service
systemctl restart fakabot.service
journalctl -u fakabot.service -n 100 --no-pager
curl -fsS http://127.0.0.1:18000/health
curl -fsS http://127.0.0.1:8011/health
```

公网回调接口需要身份验证；不带 `X-Payment-Gateway-Token` 的 POST 请求应返回 `401`。不要将仅监听本机回环地址的服务直接暴露到公网。

## 安全验证

- **Python 语法检查：** `./venv/bin/python -m compileall -q .`
- **实时商品目录读取与下单前核验：** 仅调用只读供应商 API，不会创建采购订单。
- **采购流程测试：** 必须拦截供应商 `/api/buy` POST 请求，并使用内存订单进行测试。未经另行明确授权，严禁通过消耗供应商余额或发起真实客户付款来测试。

另请参阅：`docs/OPERATIONS.md`、`docs/SUPPLIER_BUY_API.md`、`docs/BUYER_GUIDE.md` 和 `docs/PRODUCTION_HANDOVER_2026-11-17.md`。
