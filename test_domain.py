"""领域不变量测试：安全窗口、幂等、发生时间修正、配方标签冻结、建议证据、追溯链、区域指标。"""

import unittest

from app import BakeryApp


def ev(event_id, typ, at, store, sku, qty, **payload):
    return {"event_id": event_id, "type": typ, "occurred_at": at,
            "store_id": store, "sku": sku, "qty": qty, "payload": payload}


def order(event_id, typ, at, store, sku, qty, order_id):
    return {"event_id": event_id, "type": typ, "occurred_at": at,
            "store_id": store, "sku": sku, "qty": qty, "order_id": order_id, "payload": {}}


class FreshnessInvariantTest(unittest.TestCase):
    def setUp(self):
        self.app = BakeryApp()
        self.sub = self.app.submit_event

    def test_01_duplicate_scan_does_not_increase_stock(self):
        """同一入库事件重复提交（扫码枪抖动）不得增加可售数量。"""
        e = ev("RCV-1", "received_from_plant", "2026-09-27T06:40:00",
               "SH-01", "BAGEL-LOW", 60, batch_id="B-1", produced_at="2026-09-27T06:00:00")
        r1 = self.sub(dict(e))
        r2 = self.sub(dict(e))
        self.assertFalse(r1["duplicate"])
        self.assertTrue(r2["duplicate"])
        self.assertEqual(len(self.app.events), 1)
        self.assertEqual(self.app.inventory().totals("SH-01", "BAGEL-LOW")["available"], 60)

    def test_02_reservation_cancel_restores_by_occurrence_time(self):
        """预订取消按发生时间回补可售量。"""
        self.sub(ev("B-1", "baked", "2026-09-27T08:00:00", "SH-01", "BAGEL-LOW", 10, batch_id="B-1"))
        self.sub(order("O-1", "reservation_placed", "2026-09-27T08:05:00",
                       "SH-01", "BAGEL-LOW", 4, "SO-1"))
        t = self.app.inventory().totals("SH-01", "BAGEL-LOW")
        self.assertEqual((t["available"], t["reserved"]), (6, 4))
        self.sub(order("O-2", "reservation_cancelled", "2026-09-27T08:20:00",
                       "SH-01", "BAGEL-LOW", 4, "SO-1"))
        t = self.app.inventory().totals("SH-01", "BAGEL-LOW")
        self.assertEqual((t["available"], t["reserved"]), (10, 0))

    def test_03_delivery_return_within_window_restocks_after_window_blocked(self):
        """配送失败：安全窗口内带回可回架；超过窗口必须拦截。"""
        # 可颂 08:00 出炉，安全窗口 4 小时 → 12:00 到期
        self.sub(ev("B-1", "baked", "2026-09-27T08:00:00", "SH-01", "CROISSANT", 8, batch_id="B-1"))
        self.sub(order("O-1", "reservation_placed", "2026-09-27T08:10:00",
                       "SH-01", "CROISSANT", 3, "SO-OK"))
        self.sub(order("O-2", "delivery_dispatched", "2026-09-27T08:20:00",
                       "SH-01", "CROISSANT", 3, "SO-OK"))
        self.sub(order("O-3", "delivery_failed", "2026-09-27T09:00:00",
                       "SH-01", "CROISSANT", 3, "SO-OK"))
        self.sub(order("O-4", "return_restocked", "2026-09-27T09:30:00",
                       "SH-01", "CROISSANT", 3, "SO-OK"))
        t = self.app.inventory().totals("SH-01", "CROISSANT")
        self.assertEqual(t["available"], 8)

        self.sub(order("P-1", "reservation_placed", "2026-09-27T10:00:00",
                       "SH-01", "CROISSANT", 4, "SO-LATE"))
        self.sub(order("P-2", "delivery_dispatched", "2026-09-27T10:10:00",
                       "SH-01", "CROISSANT", 4, "SO-LATE"))
        self.sub(order("P-3", "delivery_failed", "2026-09-27T11:50:00",
                       "SH-01", "CROISSANT", 4, "SO-LATE"))
        self.sub(order("P-4", "return_restocked", "2026-09-27T12:20:00",
                       "SH-01", "CROISSANT", 4, "SO-LATE"))
        t = self.app.inventory().totals("SH-01", "CROISSANT")
        # 余下 4 个 12:00 到窗自动下架，带回的 4 个被拦截：可售归零
        self.assertEqual(t["available"], 0)
        self.assertEqual(t["blocked"], 8)
        self.assertEqual(self.app.inventory().order["SO-LATE"]["status"],
                         "blocked_by_safe_window")

    def test_04_expired_batch_never_returns_to_sale_even_with_demand(self):
        """过安全窗口的批次不能因任何事件（入库/预测式回补）恢复可售。"""
        self.sub(ev("B-1", "baked", "2026-09-27T06:00:00", "SH-01", "BUN-BUTTER", 10, batch_id="B-1"))
        self.sub(ev("W-1", "stock_written_off", "2026-09-27T11:01:00",
                    "SH-01", "BUN-BUTTER", 10, batch_id="B-1"))
        # 再来一轮销量与预订，已报损数量绝不可能变成 available
        t = self.app.inventory().totals("SH-01", "BUN-BUTTER")
        self.assertEqual((t["available"], t["waste"]), (0, 10))
        # 已报损批次上的新预订在提交校验时被拒绝，事件不入库
        result = self.sub(order("X-1", "reservation_placed", "2026-09-27T11:30:00",
                                "SH-01", "BUN-BUTTER", 1, "SO-X"))
        self.assertTrue(result.get("rejected"))
        self.assertIsNone(self.app.events.by_id("X-1"))

    def test_05_night_clearance_reduces_sellable_and_cannot_return(self):
        """夜间清仓按发生时间移出可售，之后只能继续沉没。"""
        self.sub(ev("B-1", "baked", "2026-09-27T06:00:00", "SH-01", "BUN-BUTTER", 10, batch_id="B-1"))
        self.sub(ev("C-1", "night_clearance", "2026-09-27T06:30:00",
                    "SH-01", "BUN-BUTTER", 4, batch_id="B-1"))
        t = self.app.inventory().totals("SH-01", "BUN-BUTTER")
        self.assertEqual((t["available"], t["clearance"]), (6, 4))
        self.sub(ev("S-1", "sale_recorded", "2026-09-27T07:00:00",
                    "SH-01", "BUN-BUTTER", 4, price_level="clearance", batch_id="B-1"))
        t = self.app.inventory().totals("SH-01", "BUN-BUTTER")
        self.assertEqual((t["clearance"], t["sold_clearance"]), (0, 4))

    def test_06_recipe_change_binds_by_batch_old_order_keeps_old_label(self):
        """配方 v2 随新批次生效；旧订单仍按购买时 v1 标签解释。"""
        self.sub(ev("B-1", "baked", "2026-09-27T07:00:00", "SH-01", "BAGEL-LOW", 10, batch_id="B-1"))
        self.sub(order("O-1", "reservation_placed", "2026-09-27T07:55:00",
                       "SH-01", "BAGEL-LOW", 2, "SO-OLD"))
        self.sub(ev("R-1", "recipe_published", "2026-09-27T08:00:00", "SH-01", "BAGEL-LOW", 0,
                    version="v2", effective_from="2026-09-27T08:00:00",
                    nutrition={"糖_g": 2.0}, allergens=["麸质", "燕麦"], note="v2"))
        self.sub(ev("B-2", "baked", "2026-09-27T08:10:00", "SH-01", "BAGEL-LOW", 10, batch_id="B-2"))
        inv = self.app.inventory()
        self.assertEqual(inv.batches["B-1"]["recipe_version"], "v1")
        self.assertEqual(inv.batches["B-2"]["recipe_version"], "v2")
        label = self.app.order_label("SO-OLD")["label"]
        self.assertEqual(label["recipe_version"], "v1")
        self.assertNotIn("燕麦", label["allergens"])

    def test_07_late_event_replays_by_occurrence_time_but_history_immutable(self):
        """晚到事件按发生时间归位重放；已发布建议快照不被改写。"""
        self.sub(ev("B-1", "baked", "2026-09-27T06:00:00", "SH-01", "BAGEL-LOW", 20, batch_id="B-1"))
        self.sub(ev("S-1", "sale_recorded", "2026-09-27T07:00:00",
                    "SH-01", "BAGEL-LOW", 5))
        self.app.generate_forecast("SH-01", "BAGEL-LOW",
                                   "2026-09-27T06:00:00", "2026-09-27T12:00:00")
        recs = self.app.generate_recommendations("2026-09-27T07:30:00", "2026-09-27T12:00:00")
        snapshot = recs[0]["evidence"]["stock_snapshot"]["available"]
        # 09:00 才上传 06:30 发生的离线 POS 销量
        late = ev("S-2", "sale_recorded", "2026-09-27T06:30:00", "SH-01", "BAGEL-LOW", 3)
        late["recorded_at"] = "2026-09-27T09:00:00"
        self.sub(late)
        inv = self.app.inventory()
        sale_times = [s["at"] for s in inv.sales if s["event_id"] == "S-2"]
        self.assertEqual(sale_times, ["2026-09-27T06:30:00"])
        self.assertEqual(inv.totals("SH-01", "BAGEL-LOW")["sold"], 8)
        # 已发布建议的库存快照不变
        self.assertEqual(recs[0]["evidence"]["stock_snapshot"]["available"], snapshot)

    def test_08_recommendation_evidence_carries_data_version_and_costs(self):
        """建议证据必须含需求版本号、库存快照与全部成本项。"""
        self.sub(ev("B-1", "baked", "2026-09-27T07:00:00", "SH-01", "BAGEL-LOW", 4, batch_id="B-1"))
        self.sub(ev("S-1", "sale_recorded", "2026-09-27T07:20:00",
                    "SH-01", "BAGEL-LOW", 4))
        self.app.generate_forecast("SH-01", "BAGEL-LOW",
                                   "2026-09-27T06:00:00", "2026-09-27T12:00:00")
        recs = self.app.generate_recommendations("2026-09-27T07:30:00", "2026-09-27T12:00:00")
        bake = next(r for r in recs if r["type"] == "bake")
        e = bake["evidence"]
        self.assertEqual(e["forecast_version_id"], "FC-V1")
        self.assertIsNotNone(e["event_watermark"])
        for key in ("ingredient_cost", "labor_cost", "oven_opportunity_cost",
                    "shortage_cost_avoided", "expected_waste_cost"):
            self.assertIn(key, e["costs"])

    def test_09_trace_chain_forecast_execution_sellout_waste(self):
        """建议可追溯：需求版本 → 执行 → 售罄时间 → 最终报损。"""
        self.sub(ev("B-1", "baked", "2026-09-27T07:00:00", "SH-01", "BUN-BUTTER", 6, batch_id="B-1"))
        self.app.generate_forecast("SH-01", "BUN-BUTTER",
                                   "2026-09-27T06:00:00", "2026-09-27T12:00:00")
        recs = self.app.generate_recommendations("2026-09-27T07:30:00", "2026-09-27T12:00:00")
        md = next(r for r in recs if r["type"] in ("markdown", "block_restock"))
        self.app.execute(md["rec_id"], "2026-09-27T07:35:00", note="测试执行")
        self.sub(ev("S-1", "sale_recorded", "2026-09-27T08:00:00",
                    "SH-01", "BUN-BUTTER", 6))
        self.sub(ev("W-1", "stock_written_off", "2026-09-27T12:01:00",
                    "SH-01", "BUN-BUTTER", 0))
        trace = self.app.trace(md["rec_id"])
        self.assertEqual(trace["forecast_version_id"], "FC-V1")
        self.assertTrue(trace["executions"])
        self.assertEqual(trace["executions"][0]["note"], "测试执行")

    def test_10_region_metrics_compare_shortage_and_waste(self):
        """区域视角同时给出缺货与损耗，可直接比较。"""
        self.sub(ev("B-1", "baked", "2026-09-27T07:00:00", "SH-01", "BAGEL-LOW", 2, batch_id="B-1"))
        self.sub(ev("S-1", "sale_recorded", "2026-09-27T07:20:00",
                    "SH-01", "BAGEL-LOW", 2))  # 售罄
        self.sub(ev("B-2", "baked", "2026-09-27T07:00:00", "SH-01", "BUN-BUTTER", 5, batch_id="B-2"))
        self.sub(ev("W-1", "stock_written_off", "2026-09-27T12:01:00",
                    "SH-01", "BUN-BUTTER", 5))  # 全部报损
        self.app.generate_forecast("SH-01", "BAGEL-LOW",
                                   "2026-09-27T06:00:00", "2026-09-27T18:00:00")
        m = self.app.region_metrics("2026-09-27T00:00:00", "2026-09-27T23:59:59")
        sh01 = m["per_store"]["SH-01"]
        self.assertGreater(sh01["waste_qty"], 0)
        self.assertGreater(sh01["waste_cost"], 0)
        self.assertGreater(sh01["shortage_qty"], 0)
        self.assertGreater(sh01["shortage_cost"], 0)

    def test_11_closure_excluded_from_forecast_demand(self):
        """临时闭店区间在需求版本中置 0 并标注数据缺口。"""
        self.sub(ev("C-1", "store_closed", "2026-09-27T07:00:00", "SH-01", "-", 0,
                    start="2026-09-27T13:00:00", end="2026-09-27T15:00:00", reason="设备检修"))
        self.sub(ev("B-1", "baked", "2026-09-27T07:00:00", "SH-01", "BAGEL-LOW", 30, batch_id="B-1"))
        self.sub(ev("S-1", "sale_recorded", "2026-09-27T07:20:00",
                    "SH-01", "BAGEL-LOW", 20))
        fc = self.app.generate_forecast("SH-01", "BAGEL-LOW",
                                        "2026-09-27T06:00:00", "2026-09-27T18:00:00")
        closed_hours = [r for r in fc["remaining_demand"] if r["closed"]]
        self.assertTrue(closed_hours)
        self.assertTrue(all(r["expected_qty"] == 0 for r in closed_hours))
        self.assertTrue(fc["inputs"]["data_gaps"])

    def test_12_transfer_does_not_reset_freshness_clock(self):
        """调拨入店批次沿用原生产时间与配方版本。"""
        self.sub(ev("B-1", "baked", "2026-09-27T05:50:00", "SH-02", "BAGEL-LOW", 30, batch_id="B-1"))
        self.sub(ev("T-1", "transfer_out", "2026-09-27T07:50:00",
                    "SH-02", "BAGEL-LOW", 10, to_store="SH-01", batch_id="B-1",
                    transfer_id="TRF-1"))
        self.sub(ev("T-2", "transfer_in", "2026-09-27T07:58:00",
                    "SH-01", "BAGEL-LOW", 10, origin_batch="B-1",
                    batch_id="B-1T", transfer_id="TRF-1"))
        inv = self.app.inventory()
        b = inv.batches["B-1T"]
        self.assertEqual(b["produced_at"], "2026-09-27T05:50:00")
        self.assertEqual(b["safe_until"], "2026-09-27T11:50:00")
        self.assertEqual(b["origin"], "transfer")


if __name__ == "__main__":
    unittest.main(verbosity=2)
