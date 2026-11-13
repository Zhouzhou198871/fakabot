# 商品系统运维说明

## Cloudflare 服务
- 目录/库存 Worker：`https://fakabot-procurement.chulingli1988.workers.dev`
- 对外读取 Worker：`https://fakabot-storefront.chulingli1988.workers.dev`
- 数据库：Cloudflare D1（商品快照与加密库存）
- 商品同步：Cloudflare Cron 每 30 分钟运行一次。
- 对外商品接口：
  - `GET /health`
  - `GET /v1/catalog`
  - `GET /v1/categories`
  - `GET /v1/inventory/summary`（仅返回数量和状态，不返回邮箱内容）

## 高并发策略
- 商品 GET 接口使用 Cloudflare Cache API，目录缓存按同步版本隔离。
- 商品响应设置边缘缓存头：目录 s-maxage=60，分类 s-maxage=120，允许 stale-while-revalidate。
- D1 为版本和分类查询建立索引。
- 机器人启动时从 Cloudflare 加载商品目录，之后后台每 5 分钟刷新；接口异常时保留最后一次成功目录/本地快照。
- 已执行 100 并发目录读取测试：100/100 HTTP 200，无失败；重复读取验证出现 Cloudflare 边缘缓存 HIT。
- 商品目录为公开数据；邮箱、密码、卡密和订单交付内容不得放入公开目录接口。
- 公开接口只提供商品标题、售价、库存状态和说明，不提供任何个人库存凭据。

## 库存迁移现状
- 433 条邮箱地址已迁移到 D1，并使用 Worker Secret 中的 AES-GCM 密钥加密。
- 这 433 条原始记录只有邮箱地址，没有密码/登录凭据，因此状态统一为 `needs_credentials`，暂不允许销售或自动交付。
- 旧 account_pool 另有 434 条记录，但 434 个关联 session 文件全部缺失；这批数据没有并入可售库存，待找回文件或重新核验。
- 不要删除原始来源文件，直到完成完整凭据补齐、解密恢复演练和交付链路验收。

## 运行检查
1. 查看 `/health`：数据库连接正常，catalog.last_sync 更新，last_run.status 为 success。
2. 查看 `/v1/catalog`：count 大于 0，价格按成本 ÷ 4，只有有货在售商品。
3. 查看 `/v1/categories`：分类 product_count 均大于 0。
4. 查看 `/v1/inventory/summary`：核对库存状态数量；不查询或打印密文以外的敏感字段。
5. 任何同步失败都保留上一版完整商品快照，不要手动清空 D1。

## 服务器清理边界
- 可以移除不再使用的节点商品目录代码。
- 不要删除 bot.py、payment_flow.py、payment_listener.py、telegram_storage.py、.env 或支付监听配置。
- 不要删除原始库存文件，直到 Cloudflare 恢复与订单交付链路完成测试。
- 机器人尚未切换到新 Worker 时，不要声称机器人已使用 Cloudflare 实时目录。