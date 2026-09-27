"""2026-09-27 演示数据：甜面包积压、低糖缺货、重复扫码、预订取消、
配送失败超窗拦截、临时闭店、夜间清仓、配方 v2 切换、调拨与现烤决策链。

运行：python3 seed.py
"""

from app import BakeryApp
from config import SKUS


def _ev(event_id, typ, at, store, sku, qty, **payload):
    return {"event_id": event_id, "type": typ, "occurred_at": at,
            "store_id": store, "sku": sku, "qty": qty, "payload": payload}


def build(app=None):
    app = app or BakeryApp()
    sub = app.submit_event

    # ---------- 凌晨/清晨：生产与来货 ----------
    sub(_ev("E-CRO-BAKE-1", "baked", "2026-09-27T03:30:00", "SH-01", "CROISSANT", 12,
            batch_id="CRO-SH01-A"))
    # 前一日工厂批次（黄油餐包），05:40 到店时早已过安全窗口（昨 22:00 生产 +5h）
    sub(_ev("E-RCV-BUN-OLD-1", "received_from_plant", "2026-09-27T05:40:00",
            "SH-01", "BUN-BUTTER", 11, batch_id="BUN-0926-X",
            produced_at="2026-09-26T22:00:00", plant_batch="PLANT-0926-X"))
    sub(_ev("E-BUN-BAKE-1", "baked", "2026-09-27T06:00:00", "SH-01", "BUN-BUTTER", 40,
            batch_id="BUN-0927-A"))
    sub(_ev("E-BAGEL-BAKE-SH02-1", "baked", "2026-09-27T05:50:00", "SH-02", "BAGEL-LOW", 30,
            batch_id="BAGEL-SH02-A"))
    sub(_ev("E-BAGEL-BAKE-1", "baked", "2026-09-27T06:10:00", "SH-01", "BAGEL-LOW", 20,
            batch_id="BAGEL-SH01-A"))

    # 中央来货 60 个低糖贝果，扫码枪抖动：同一 event_id 连扫两次
    rcv = _ev("E-RCV-BAGEL-1", "received_from_plant", "2026-09-27T06:40:00",
              "SH-01", "BAGEL-LOW", 60, batch_id="RCV-BAGEL-0927-A",
              produced_at="2026-09-27T06:00:00", plant_batch="PLANT-0927-A")
    sub(rcv)
    dup = sub(dict(rcv))  # 重复扫码：必须 duplicate=True，库存不变

    # ---------- 夜间清仓与昨批报损 ----------
    sub(_ev("E-CLEAR-OLD-1", "night_clearance", "2026-09-27T06:30:00",
            "SH-01", "BUN-BUTTER", 8, batch_id="BUN-0926-X"))
    sub(_ev("E-WO-OLD-1", "stock_written_off", "2026-09-27T07:00:00",
            "SH-01", "BUN-BUTTER", 3, batch_id="BUN-0926-X"))
    sub(_ev("E-SALE-CLEAR-1", "sale_recorded", "2026-09-27T07:25:00",
            "SH-01", "BUN-BUTTER", 8, batch_id="BUN-0926-X", price_level="clearance"))

    # ---------- 早高峰低糖贝果：先售罄，来货回补 ----------
    sub(_ev("E-SALE-BGL-1", "sale_recorded", "2026-09-27T06:25:00",
            "SH-01", "BAGEL-LOW", 12, channel="pos"))
    sub(_ev("E-SALE-BGL-2", "sale_recorded", "2026-09-27T06:38:00",
            "SH-01", "BAGEL-LOW", 8, channel="pos"))  # 06:38 售罄

    # ---------- 线上订单：取消（回补）与配送失败（超窗拦截）----------
    sub({"event_id": "E-ORD-7781-1", "type": "reservation_placed",
         "occurred_at": "2026-09-27T07:05:00", "store_id": "SH-01",
         "sku": "BAGEL-LOW", "qty": 6, "order_id": "SO-7781", "payload": {}})
    sub({"event_id": "E-ORD-7781-2", "type": "reservation_cancelled",
         "occurred_at": "2026-09-27T07:12:00", "store_id": "SH-01",
         "sku": "BAGEL-LOW", "qty": 6, "order_id": "SO-7781", "payload": {}})
    sub({"event_id": "E-ORD-7782-1", "type": "reservation_placed",
         "occurred_at": "2026-09-27T07:00:00", "store_id": "SH-01",
         "sku": "CROISSANT", "qty": 4, "order_id": "SO-7782", "payload": {}})
    sub({"event_id": "E-ORD-7782-2", "type": "delivery_dispatched",
         "occurred_at": "2026-09-27T07:10:00", "store_id": "SH-01",
         "sku": "CROISSANT", "qty": 4, "order_id": "SO-7782", "payload": {}})
    sub({"event_id": "E-ORD-7782-3", "type": "delivery_failed",
         "occurred_at": "2026-09-27T07:20:00", "store_id": "SH-01",
         "sku": "CROISSANT", "qty": 4, "order_id": "SO-7782", "payload": {}})

    # ---------- 甜面包滞销，07:25 仍排了 08:00 的加烤计划 ----------
    sub(_ev("E-SALE-BUN-1", "sale_recorded", "2026-09-27T07:20:00",
            "SH-01", "BUN-BUTTER", 4, channel="pos"))
    sub(_ev("E-SALE-BUN-2", "sale_recorded", "2026-09-27T07:30:00",
            "SH-01", "BUN-BUTTER", 2, channel="pos"))
    sub(_ev("E-PLAN-BUN-1", "production_planned", "2026-09-27T07:25:00",
            "SH-01", "BUN-BUTTER", 20, plan_id="PLAN-BUN-0800", planned_at="2026-09-27T08:00:00"))

    # ---------- 贝果继续热销 ----------
    sub(_ev("E-SALE-BGL-3", "sale_recorded", "2026-09-27T07:20:00",
            "SH-01", "BAGEL-LOW", 25, channel="pos"))
    sub(_ev("E-SALE-BGL-4", "sale_recorded", "2026-09-27T07:30:00",
            "SH-01", "BAGEL-LOW", 20, channel="pos"))
    sub(_ev("E-SALE-BGL-SH02-1", "sale_recorded", "2026-09-27T07:10:00",
            "SH-02", "BAGEL-LOW", 1, channel="pos"))

    # ---------- 13:00–15:00 临时闭店（提前登记）----------
    sub(_ev("E-CLOSE-1", "store_closed", "2026-09-27T07:00:00", "SH-01", "-", 0,
            start="2026-09-27T13:00:00", end="2026-09-27T15:00:00", reason="设备检修临时闭店"))

    # ---------- 07:35 形成需求版本 ----------
    fc_bagel = app.generate_forecast("SH-01", "BAGEL-LOW",
                                     "2026-09-27T06:00:00", "2026-09-27T12:00:00",
                                     note="早高峰滚动版")
    fc_bun = app.generate_forecast("SH-01", "BUN-BUTTER",
                                   "2026-09-27T06:00:00", "2026-09-27T12:00:00")
    app.generate_forecast("SH-01", "CROISSANT",
                          "2026-09-27T06:00:00", "2026-09-27T12:00:00")
    app.generate_forecast("SH-02", "BAGEL-LOW",
                          "2026-09-27T06:00:00", "2026-09-27T12:00:00",
                          note="中山公园店客流平稳")

    # ---------- 07:42 失败配送带回：可颂 07:30 已过安全窗口 → 拦截 ----------
    sub({"event_id": "E-ORD-7782-4", "type": "return_restocked",
         "occurred_at": "2026-09-27T07:42:00", "store_id": "SH-01",
         "sku": "CROISSANT", "qty": 4, "order_id": "SO-7782", "payload": {}})

    # ---------- 07:45 发布补货建议 ----------
    recs = app.generate_recommendations("2026-09-27T07:45:00", "2026-09-27T12:00:00")

    # ---------- 按建议执行：停止加烤、降价、调拨；保留一条现烤建议 ----------
    rec_by_type = {}
    for r in recs:
        rec_by_type.setdefault((r["store_id"], r["type"]), r)

    stop_rec = rec_by_type[("SH-01", "stop_production")]
    md_rec = rec_by_type[("SH-01", "markdown")]
    tr_rec = rec_by_type[("SH-01", "transfer_in")]
    block_rec = rec_by_type[("SH-01", "block_restock")]

    # 停止 08:00 加烤计划
    sub(_ev("E-PLAN-BUN-CXL-1", "production_cancelled", "2026-09-27T07:50:00",
            "SH-01", "BUN-BUTTER", 20, plan_id="PLAN-BUN-0800"))
    app.execute(stop_rec["rec_id"], "2026-09-27T07:50:00", note="取消 08:00 加烤计划")

    # SH-02 调出 → SH-01（鲜度时钟随货走，不重置安全窗口）
    qty_tr = tr_rec["qty"]
    sub(_ev("E-TRF-1", "transfer_out", "2026-09-27T07:50:00",
            "SH-02", "BAGEL-LOW", qty_tr, to_store="SH-01",
            batch_id="BAGEL-SH02-A", transfer_id="TRF-0927-1"))
    sub(_ev("E-TRF-2", "transfer_in", "2026-09-27T07:58:00",
            "SH-01", "BAGEL-LOW", qty_tr, to_store="SH-02",
            origin_batch="BAGEL-SH02-A", batch_id="BAGEL-TRF-0927-1",
            transfer_id="TRF-0927-1"))
    app.execute(tr_rec["rec_id"], "2026-09-27T07:58:00", qty=qty_tr,
                note="中山公园店调拨到店")

    # 黄油餐包降价 20 个
    sub(_ev("E-MD-BUN-1", "markdown_applied", "2026-09-27T08:00:00",
            "SH-01", "BUN-BUTTER", 20, batch_id="BUN-0927-A"))
    app.execute(md_rec["rec_id"], "2026-09-27T08:00:00", qty=20, note="早市八折")

    # 拦截可颂不回架，转报损处置
    app.execute(block_rec["rec_id"], "2026-09-27T11:05:00", note="到期可颂不回架，报损")

    # ---------- 07:55 旧配方订单（标签冻结 v1），08:00 配方 v2 生效 ----------
    sub({"event_id": "E-ORD-7790-1", "type": "reservation_placed",
         "occurred_at": "2026-09-27T07:55:00", "store_id": "SH-01",
         "sku": "BAGEL-LOW", "qty": 2, "order_id": "SO-7790", "payload": {}})
    sub({"event_id": "E-RECIPE-BAGEL-V2", "type": "recipe_published",
         "occurred_at": "2026-09-27T08:00:00", "store_id": "SH-01",
         "sku": "BAGEL-LOW", "qty": 0, "payload": {
             "sku": "BAGEL-LOW", "version": "v2",
             "effective_from": "2026-09-27T08:00:00",
             "nutrition": {"热量_kcal": 252, "蛋白质_g": 9.6, "糖_g": 3.2,
                           "脂肪_g": 3.0, "膳食纤维_g": 4.8},
             "allergens": ["麸质", "芝麻", "燕麦"],
             "note": "提高燕麦含量的 v2 配方"}})

    # 现烤建议执行：08:10 出炉，批次固定 v2 配方
    bake_rec = rec_by_type.get(("SH-01", "bake"))
    sub(_ev("E-BAGEL-BAKE-2", "baked", "2026-09-27T08:10:00",
            "SH-01", "BAGEL-LOW", bake_rec["qty"] if bake_rec else 20,
            batch_id="BAGEL-SH01-B"))
    if bake_rec:
        app.execute(bake_rec["rec_id"], "2026-09-27T08:10:00",
                    note="v2 配方首批现烤")
    sub({"event_id": "E-ORD-7790-2", "type": "delivery_dispatched",
         "occurred_at": "2026-09-27T08:15:00", "store_id": "SH-01",
         "sku": "BAGEL-LOW", "qty": 2, "order_id": "SO-7790", "payload": {}})
    sub({"event_id": "E-ORD-7790-3", "type": "delivery_delivered",
         "occurred_at": "2026-09-27T08:20:00", "store_id": "SH-01",
         "sku": "BAGEL-LOW", "qty": 2, "order_id": "SO-7790", "payload": {}})

    # ---------- 贝果 09:40 售罄 ----------
    sub(_ev("E-SALE-BGL-5", "sale_recorded", "2026-09-27T08:35:00",
            "SH-01", "BAGEL-LOW", 25, channel="pos"))
    sub(_ev("E-SALE-BGL-6", "sale_recorded", "2026-09-27T09:05:00",
            "SH-01", "BAGEL-LOW", 20, channel="pos"))
    sub(_ev("E-SALE-BGL-7", "sale_recorded", "2026-09-27T09:40:00",
            "SH-01", "BAGEL-LOW", 12, channel="pos"))  # 09:40 售罄

    # 降价餐包售出
    sub(_ev("E-SALE-BUN-MD-1", "sale_recorded", "2026-09-27T09:00:00",
            "SH-01", "BUN-BUTTER", 12, price_level="markdown"))
    sub(_ev("E-SALE-BUN-MD-2", "sale_recorded", "2026-09-27T10:30:00",
            "SH-01", "BUN-BUTTER", 8, price_level="markdown"))

    # ---------- 晚到 POS 事件：发生在 07:25，09:42 才上传 ----------
    late = _ev("E-SALE-BGL-LATE-1", "sale_recorded", "2026-09-27T07:25:00",
               "SH-01", "BAGEL-LOW", 3, channel="pos-offline")
    late["recorded_at"] = "2026-09-27T09:42:00"
    # 09:40 已售罄，07:25 实际还有库存：FIFO 按发生时间重放成立
    sub(late)

    # ---------- 安全窗口到期后的处置 ----------
    sub(_ev("E-WO-CRO-1", "stock_written_off", "2026-09-27T11:05:00",
            "SH-01", "CROISSANT", 12))
    sub(_ev("E-WO-BUN-1", "stock_written_off", "2026-09-27T11:30:00",
            "SH-01", "BUN-BUTTER", 14))

    return {
        "app": app,
        "duplicate_scan": dup,
        "forecasts": {"bagel": fc_bagel, "bun": fc_bun},
        "recommendations": recs,
        "rec_ids": {
            "transfer": tr_rec["rec_id"],
            "stop": stop_rec["rec_id"],
            "markdown": md_rec["rec_id"],
            "bake": bake_rec["rec_id"] if bake_rec else None,
            "block": block_rec["rec_id"],
        },
    }


def main():
    result = build()
    app = result["app"]
    print(f"事件总数: {len(app.events)}（重复扫码 duplicate={result['duplicate_scan']['duplicate']}）")
    inv = app.inventory()
    for sid in ("SH-01", "SH-02"):
        for sku in SKUS:
            t = inv.totals(sid, sku)
            print(f"{sid} {SKUS[sku]['name']}: 可售 {t['sellable']}，"
                  f"已售 {t['sold'] + t['sold_markdown'] + t['sold_clearance']}，报损 {t['waste']}")
    print(f"建议条数: {len(result['recommendations'])}")


if __name__ == "__main__":
    main()
