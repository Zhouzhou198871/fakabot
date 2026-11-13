# 晴天号铺项目交付验收单
日期：2026-11-14
项目目录：/root/fakabot

## 已交付并已部署
- Cloudflare D1 商品目录及加密库存表。
- fakabot-procurement Worker：上游商品同步，Cron 每 30 分钟；采购开关保持 paused。
- fakabot-storefront Worker：公开商品目录、分类和不含凭据的库存摘要。
- 主机器人已接入 Cloudflare 商品目录；启动时加载、后台每 5 分钟刷新。
- 账号商城节点商品入口已移除；独立机器人和支付监听未被本次商品界面改造替换。
- 商品格式、买家教程、售后规则和运维说明见 docs/。
- 当前 36 项 unittest 全部通过（2026-11-14 本次验收执行）。
- Cloudflare 当前健康检查：D1 connected；商品快照 1,571 条，其中 active 1,535；有货分类 19；库存 433 条 mailbox_email / needs_credentials；采购 paused。

## 未达到生产销售验收，禁止误称全部上线
1. 供应商登录会话未配置：/root/.config/fakabot/dahaoshang_session.json 不存在；只读授权检查失败。自动采购不得开启。
2. 433 条邮箱记录只有邮箱地址，没有密码/登录凭据；状态 needs_credentials，不可销售或自动发货。
3. 旧 account_pool 的 434 条记录缺失对应 session 文件；尚未找回、验证或迁移。
4. 23 条旧备份凭据尚未核实归属；未经核实不得并入可售库存。
5. 未执行真实供应商采购、真实 USDT 收款或真实订单交付验收；测试均为模拟，不涉及真实转账或扣款。
6. 真实收款按钮/商品付款保持安全闸门；SUPPLIER_AUTOPROCUREMENT_ENABLED 不得开启，直到下列验收全部完成。

## 上线前必须完成的门槛
- 由店主在服务器本机安全登录供应商账号，建立有效且权限受限的会话；不要把密码、token、私钥贴到聊天。
- 对每一条库存记录补齐并核验完整交付凭据、账号类型和商品描述；标记来源、状态和唯一库存 ID；不完整数据始终保持不可售。
- 核验供应商余额支付方式、商品规格、实时库存和单笔采购成本；设定采购金额上限及每日限额。
- 完成订单/库存原子锁定、支付交易唯一性校验、重复 webhook 幂等、过期订单处理、支付少付/多付/超时对账、供应商采购超时人工核对、已购内容安全归档、Telegram 交付失败补发测试。
- 由店主明确批准真实小额测试预算和测试商品；测试成功后再单独批准逐步开放收款。
- 保持独立支付监听、独立机器人及其他服务不变，除非店主另行明确授权。

## 当前生产安全状态
- 供应商采购：paused。
- 自动采购开关：保持关闭。
- 未验证商品：禁止付款。
- 433 邮箱：needs_credentials。
- 旧账号池：待找回 session。
- 本次测试：36/36 通过；这不等于真实交易验收。

## 代码和文档位置
- 代码：/root/fakabot/
- 文档：/root/fakabot/docs/
- 主机器人：bot.py
- 支付流程：payment_flow.py
- 支付监听：payment_listener.py
- 供应商接口：supplier_client.py
- Cloudflare 目录接入：products/cloudflare_catalog.py

## 维护与安全
- 不要复制或公开 .env、.catalog_sync_token、供应商会话文件、私钥、Telegram 归档附件或任何账号凭据。
- 不要删除 /root/cloud_emails.txt，直到凭据补齐、恢复演练及交付链路验收通过。
- 不要将 needs_credentials 或缺少 session 的记录改成 available。
- 任何采购异常或超时均先查询原供应商订单，不得盲目重复购买。