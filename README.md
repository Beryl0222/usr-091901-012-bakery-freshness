# 烘焙鲜度与补货决策

结合门店烤炉产能、中央工厂来货、配方批次、保质窗口、线上订单和分时销量，在同一决策链上给出**现烤 / 调拨 / 降价 / 停止制作**建议，并明确展示采用的数据与成本。项目以领域契约约定参与者、状态和不可破坏的业务原则，各模块围绕同一语义协作。

## 模块

- `domain.py` — 产品、配方版本、批次。批次状态（计划生产 → 制作中 → 可售 → 临期 → 停止销售 → 已报损）按发生时间推导，越过安全窗口只会走向停止销售/已报损，不可回退。
- `ledger.py` — 只增不改的事件台账。视图按**发生时间**回放，晚到的修正（预订取消、配送失败、临时闭店、夜间清仓）落在其发生时刻；重复 `event_id` / `scan_id` 直接返回原记录，不多入库。
- `decisions.py` — 决策引擎：分时需求 × FEFO 库存模拟 × 烤炉产能 × 闭店时段，生成建议并附 `data_used`（需求版本、当前可售、剩余需求、预计报损等）与 `cost`（生产成本、避免缺货损失、避免报损成本等）。
- `analytics.py` — 区域视角：按门店×商品汇总缺货损失与报损成本，支持横向比较。
- `app.py` — 应用服务：订单在购买时刻快照配方标签（营养与过敏原随批次生效，旧订单按购买时标签解释）；建议执行联动生成批次/降价/调拨事件。
- `service.py` — HTTP 接口层。

## 运行

```bash
python3 service.py --check        # 核对服务配置与契约一致性
python3 service.py --port 8000    # 启动服务
python3 -m unittest -v            # 运行全部测试
```

## 主要接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` `/contract` | 健康检查、领域契约 |
| POST | `/products` · `/products/{id}/recipes` | 商品建档、配方版本（营养/过敏原随生效时间切换） |
| GET | `/products/{id}/label?at=` | 某时刻生效的标签 |
| POST | `/stores/{id}/capacity` · `/forecasts` | 烤炉产能与烘焙时段、分时需求预测版本 |
| POST | `/batches/plan` · `/batches/{id}/start` · `/batches/receive` | 排产、开工、扫码入库（`scan_id` 幂等） |
| POST | `/events` | 销售/预订/预订取消/配送失败/临时闭店/重开/夜间清仓/报损/调出/降价/重新上架（过期拒绝） |
| GET | `/inventory?store_id=&product_id=&at=` | 任意时刻可售量、批次窗口与状态 |
| POST | `/recommendations/generate` | 生成现烤/调拨/降价/停止制作建议 |
| POST | `/recommendations/{id}/execute` · `/dismiss` | 执行（联动生成批次/事件）或忽略 |
| GET | `/recommendations/{id}/trace` | 追溯链：需求版本 → 实际执行 → 售罄时间 → 最终报损 |
| GET | `/orders/{id}/label` | 订单购买时标签 |
| GET | `/analytics/stockout-waste?store_id=&from=&to=` | 缺货与损耗对比（缺省汇总全部门店） |

时间均为 UTC ISO-8601；未带时区按 UTC 处理。数据保存在进程内存中，重启后需重新灌入。

## 不可破坏原则的实现位置

- **超过安全窗口不得恢复可售**：`Batch.state_at` 只随时间单向推导；`relist` 事件经 `ensure_relistable` 拦截（409）；决策引擎只建议新现烤/调拨，绝不复活过期库存；配送失败退回的过期商品直接进入待报损。
- **配方与过敏原标签随批次固定**：批次创建时钉住 `recipe_version`；订单在购买时刻快照标签，配方调整后旧订单解释不变。
- **重复收货或扫码不能增加可售数量**：台账按 `event_id` / `scan_id` 幂等，重复提交返回原记录与当前库存。
