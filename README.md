# 烘焙鲜度与补货决策

面向社区面包店的鲜度与补货后端：把门店烤炉产能、中央工厂来货、配方批次、保质窗口、线上订单和分时销量放在同一条决策链中，建议何时**现烤、调拨、降价或停止制作**，并明确展示每条建议所采用的数据版本与成本。

## 运行

```bash
python3 service.py --check          # 契约自检
python3 seed.py                     # 灌入 2026-09-27 演示数据并打印汇总
python3 demo.py                     # 端到端演示：事件→建议→执行→追溯
python3 service.py --port 8000      # 启动 HTTP 服务
python3 -m unittest -v              # 全部测试（15 个）
```

## 架构

事件溯源 + 投影。所有库存变化都来自**事件**，投影只按 `occurred_at`（发生时间）重放，与登记顺序无关；晚到事件归位后重放即可修正可售量，但已发布的建议与需求版本保持原样。

| 模块 | 职责 |
|---|---|
| `events.py` | 追加式事件存储，按 `event_id` 幂等（重复扫码/重发不产生效果） |
| `model.py` | 领域投影：批次库存池、订单与标签快照、配方版本、闭店区间、售罄/拦截时间线 |
| `forecast.py` | 需求版本（不可变）：分时销量 × 日内衰减外推，闭店区间置 0 并标注数据缺口 |
| `replenish.py` | 建议引擎：现烤/调拨/降价/停止制作/拦截处置，证据含数据版本与全部成本项 |
| `app.py` | 应用门面：事件提交（含业务校验回滚）、库存查询、订单标签、区域缺货 vs 损耗 |
| `service.py` | HTTP 接口 |
| `config.py` | 门店、SKU、成本模型、配方基线 |
| `seed.py` / `demo.py` | 2026-09-27 演示数据与全链路演示 |

## 核心规则（domain_contract.json 的 invariants）

1. 超过安全窗口（`safe_until`）的商品**不得恢复为可售**——取消回补、配送失败带回、预测需求都不行，只能降价/清仓/报损。
2. 配方与过敏原标签**随生产批次固定**；配方调整后新批次用新版本，旧订单仍按购买时标签解释。
3. 重复收货或扫码**不能增加可售数量**（同一 `event_id` 幂等）。
4. 预订取消、配送失败、临时闭店、夜间清仓均**按发生时间**修正可售量。
5. 每条建议可完整追溯：**需求版本 → 实际执行 → 售罄时间 → 最终报损**。
6. 建议必须展示采用的数据版本与成本：原料、人工、烤炉机会成本、缺货机会损失、预期报损。

## 库存池

每个批次按数量分布在若干池中：`available`（可售）、`reserved`（预订锁定）、`in_delivery`（配送中）、`returning`（失败待带回）、`markdown`（降价中）、`clearance`（夜间清仓）、`blocked`（过窗拦截），以及沉没池 `sold*`、`transferred_out`、`waste`。
`blocked` 是单向的：任何事件都不能把数量从 `blocked` 移回 `available`。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/contract` | 领域契约 |
| POST | `/events` | 提交业务事件（幂等；业务校验失败回滚并返回 `rejected`） |
| POST | `/events/batch` | 批量提交 `{events: [...]}` |
| GET | `/stock?store=&sku=&at=` | 某时刻批次投影（`at` 可选，按发生时间过滤） |
| GET | `/stock/at-risk?store=` | 当前过窗被拦截、等待处置的批次 |
| POST | `/forecast/versions` | 生成需求版本 `{store_id, sku, day_start, horizon_end, note?}` |
| GET | `/forecast/versions?store=&sku=` | 列出需求版本 |
| POST | `/recommendations/generate` | 生成建议 `{at, horizon_end, store_id?, sku?}` |
| GET | `/recommendations?store=&sku=` | 列出建议（含证据与成本） |
| POST | `/recommendations/{id}/execute` | 记录执行 `{at, qty?, note?}` |
| GET | `/recommendations/{id}/trace` | 追溯链：需求版本→执行→售罄→报损 |
| POST | `/orders` | 创建线上订单（冻结购买时配方标签） |
| GET | `/orders/{id}/label` | 订单购买时标签 |
| GET | `/region/metrics?from=&to=` | 区域：缺货 vs 损耗（数量与金额） |
| GET | `/batches/{id}` | 批次详情（配方版本、安全窗口、各池数量） |

时间字段统一 `YYYY-MM-DDTHH:MM:SS`（门店本地时间）。

## 事件类型

`production_planned` / `production_cancelled` / `baked` / `received_from_plant` / `sale_recorded` / `reservation_placed` / `reservation_cancelled` / `delivery_dispatched` / `delivery_delivered` / `delivery_failed` / `return_restocked` / `transfer_out` / `transfer_in` / `store_closed` / `markdown_applied` / `night_clearance` / `stock_written_off` / `recipe_published`

事件公共字段：`event_id, type, occurred_at, store_id, sku, qty, payload`；订单类事件另带 `order_id`。批次/计划/调拨等引用可放顶层或 `payload`。

## 演示场景（seed.py，2026-09-27）

- 早高峰低糖贝果 06:38 售罄 → 中央来货 60 个（**同一事件连扫两次，第二次被幂等拦截**）
- 甜面包（黄油餐包）滞销积压：建议**停止** 08:00 加烤计划 + **降价**出清
- 贝果缺口：建议 **SH-02 调拨 27 个**（鲜度时钟随货走）+ **现烤** 20 个（v2 配方首批）
- SO-7781 预订取消 → 按取消时间回补可售
- SO-7782 可颂配送失败，07:42 带回时已过 07:30 安全窗口 → **拦截，不得回架**，最终报损
- 前一日批次夜间清仓 8 个、报损 3 个
- 08:00 配方 v2 生效：07:55 的 SO-7790 仍按 **v1 标签**解释，08:10 新批次固定 **v2**
- 09:42 才上传的 07:25 离线 POS 销量：**按发生时间归位**重放，已发布建议快照不变
- 13:00–15:00 临时闭店：需求版本中该区间需求置 0 并标注数据缺口
- 区域视角：SH-01 缺货损失 vs 报损成本同屏可比
