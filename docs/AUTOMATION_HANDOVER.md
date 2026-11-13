# 自动化供货系统交付说明

更新时间：2026-11-14

## 本阶段架构

机器人本阶段不改动。商品采集、目录同步和供应商连接独立运行在 Cloudflare Worker / D1；后续机器人只通过稳定的 HTTP API 读取商品、创建订单和查询交付状态。

### 已实现：免供应商登录的公开目录自动化

上游公开只读接口：
- `GET https://dahaoshang.com/api/syscategory/getcategorylist`
- `GET https://dahaoshang.com/api/sysproduct/getproductlist?categoryId=0`
- `GET https://dahaoshang.com/api/sysproduct/getproduct?productId=...`
- `GET https://dahaoshang.com/api/syspaytype/getpaymethod?categoryId=...`

这些 GET 接口已在无 Authorization 令牌的情况下实测返回 HTTP 200。公开目录读取不需要供应商账号登录。

Cloudflare 生产同步：
- Worker：`fakabot-procurement`
- Cron：`*/30 * * * *`
- 定时任务直接读取公开目录接口，经 Cloudflare 代理/relay 获取上游数据，校验后写入 D1。
- 采购不在定时同步中执行；同步只读，不扣款、不创建供应商订单。
- 最近一次手动触发成功：1,561 个商品、28 个分类、1,526 个在售且有库存商品；Storefront 对外显示 19 个有货分类。
- Worker 保留版本化快照；异常时记录失败，不应把失败结果当作空目录。

服务器手动/回退脚本：
- `scripts/refresh_public_catalog.py`
- 只访问公开 GET 接口；校验分类、唯一货号、商品数量、价格和库存；原子更新 `products/upstream_catalog.json`。
- `--publish` 可触发 Cloudflare Worker 重新执行公开目录同步。
- 不需要供应商密码、登录令牌或 API 授权。

### 供应商采购的边界

商品查询不需要登录，但真实下单属于账户交易。供应商网站的下单接口必须有有效账户会话，并且付款方式需实际可用。当前客户端只允许识别明确的账户余额支付方式；不会自动改用 USDT、支付宝或微信付款。

已准备安全登录脚本：
- `scripts/supplier_login.py`
- 账号在服务器终端输入，密码使用 getpass 不回显。
- 不把明文密码写入文件或日志。
- 登录成功后由 `auth.lynli.me` 将 access token 使用 RSA-OAEP + AES-GCM 加密后保存到 Cloudflare D1。
- 服务器私钥权限为 600，并在登录前验证与 Cloudflare 公钥匹配。
- 不绕过验证码、MFA、风控或供应商访问控制；出现额外验证时应走供应商官方验证流程。

采购客户端：
- `supplier_client.py` 默认通过 `https://auth.lynli.me` 访问供应商 API。
- 默认从 `https://auth.lynli.me/_internal/session` 取回加密会话，并在服务器本地解密。
- `buy_with_balance()` 有商品在售、库存、数量、规格和唯一余额支付方式校验。
- 下单超时或订单内容未返回时，不自动重复采购；先用原供应商订单号查询恢复。
- `scripts/supplier_preflight.py` 可只读核验会话和余额支付方式，不会采购。
- `scripts/supplier_login.py` 用于现有会话过期后的本机安全重新登录。

最近一次只读预检已成功：会话解密成功、供应商账户已认证、28 个分类、1,561 条上游商品，检测到可用余额支付方式；未发出任何采购请求。

**当前仍未执行真实采购。** 采购总开关保持关闭。启用前还需要店主确定单笔和每日采购金额上限，并单独批准真实小额验收。

## 已存在的 Cloudflare API

Storefront Worker：`https://fakabot-storefront.chulingli1988.workers.dev`
- `GET /health`
- `GET /v1/catalog`
- `GET /v1/categories`
- `GET /v1/inventory/summary`

Procurement Worker：`https://fakabot-procurement.chulingli1988.workers.dev`
- `GET /health`
- 内部 `POST /_internal/refresh`：Bearer 保护，触发公开目录同步
- 内部 `POST /_internal/catalog-sync`：Bearer 保护，导入经校验的目录快照

目录公开接口不返回账号密码、邮箱凭据、库存密文或订单交付内容。

## 当前库存事实
- 433 条邮箱地址已加密存储，但没有密码/登录凭据，状态为 `needs_credentials`，不可出售。
- 旧账号池 434 条记录的 session 文件缺失。
- 23 条旧备份凭据尚未核实归属。
- 不得将不完整记录改为 available。

## 验收
- 公开 API 免登录读取：已实测。
- Cloudflare 30 分钟自动目录同步：已配置。
- 手动触发 Worker 同步：已成功。
- 服务器回退脚本：已运行并更新本地快照。
- 36 项现有单元测试：全部通过。
- 真实供应商登录、余额扣款、采购和账号交付：未做真实交易验收。

## 运维命令
```bash
cd /root/fakabot
./venv/bin/python scripts/refresh_public_catalog.py
./venv/bin/python scripts/refresh_public_catalog.py --publish
./venv/bin/python -m unittest discover -s tests -v
```

供应商登录（在服务器 SSH 终端执行；密码不会回显）：
```bash
cd /root/fakabot
./venv/bin/python scripts/supplier_login.py
```

登录完成后先做只读账户/支付能力验收，再由店主单独批准真实采购。不要在聊天中发送密码、令牌或私钥。


## 2026-11-14 订单服务部署状态

独立订单 Worker 已上线：`https://fakabot-orders.chulingli1988.workers.dev`。健康检查已验证，D1 正常连接；订单创建、支付事件接收、订单查询和采购任务队列代码已部署。订单写接口需要安装三个独立 Worker Secrets（`BOT_API_TOKEN`、`PAYMENT_LISTENER_TOKEN`、`PROCUREMENT_EXECUTOR_TOKEN`），密钥文件在服务器 `/root/.config/fakabot/orders_api_tokens.env`，权限 600。配置步骤见 `cloudflare/orders-api/README.md`。

**重要：这还不是完整自动采购/自动发货。** 采购开关保持 false；供应商执行器、Cloudflare 会话解密、交付加密/领取端点和支付监听器 webhook 接入尚未完成。没有发生真实采购或扣款。机器人和独立支付监听器均未修改。