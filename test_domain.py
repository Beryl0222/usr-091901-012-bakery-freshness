"""领域规则测试：幂等入库、安全窗口、标签快照、修正事件与决策追溯。"""
import unittest

from app import BakeryService
from domain import DomainError, SafetyWindowViolation

DAY = "2026-09-27"
T = lambda hhmm: f"{DAY}T{hhmm}:00Z"  # noqa: E731


def hourly(peak=0.0, base=0.0):
    values = [base] * 24
    for hour in (7, 8, 9):
        values[hour] = peak
    return values


def make_service():
    svc = BakeryService()
    svc.register_product({
        "product_id": "low-sugar", "name": "低糖全麦吐司", "category": "低糖",
        "price": 12.0, "cost": 5.0,
        "shelf_life_hours": 24, "safety_window_hours": 10, "near_expiry_after_hours": 6,
        "transfer_fee": 1.0,
    })
    svc.register_product({
        "product_id": "sweet", "name": "奶酥甜面包", "category": "甜面包",
        "price": 9.0, "cost": 3.5,
        "shelf_life_hours": 20, "safety_window_hours": 8, "near_expiry_after_hours": 5,
    })
    svc.add_recipe("low-sugar", {
        "version": 1, "effective_from": f"{DAY}T00:00:00Z",
        "nutrition": {"热量": 220, "糖": 3}, "allergens": ["麸质"],
    })
    svc.add_recipe("sweet", {
        "version": 1, "effective_from": f"{DAY}T00:00:00Z",
        "nutrition": {"热量": 310, "糖": 18}, "allergens": ["麸质", "乳制品"],
    })
    svc.set_capacity("store-1", {"oven": {"low-sugar": 40, "sweet": 20}, "baking_hours": [5, 20]})
    svc.register_forecast({
        "store_id": "store-1", "version": "fc-0927",
        "hourly": {"low-sugar": hourly(peak=30, base=2), "sweet": hourly(peak=3, base=1)},
    })
    return svc


def receive(svc, product="low-sugar", qty=10, produced="06:00", scan="scan-1", at="06:30", store="store-1"):
    return svc.receive_batch({
        "scan_id": scan, "store_id": store, "product_id": product, "quantity": qty,
        "produced_at": T(produced), "occurred_at": T(at), "source": "中央工厂",
    })


class DuplicateScanTest(unittest.TestCase):
    def test_duplicate_scan_does_not_add_stock(self):
        svc = make_service()
        first = receive(svc, qty=10)
        self.assertTrue(first["recorded"])
        again = receive(svc, qty=10)
        self.assertFalse(again["recorded"])
        self.assertTrue(again["duplicate"])
        inv = svc.inventory("store-1", "low-sugar", T("07:00"))
        self.assertEqual(inv["on_hand"], 10)
        self.assertEqual(inv["sellable"], 10)

    def test_duplicate_event_id_is_idempotent(self):
        svc = make_service()
        receive(svc, qty=10)
        payload = {"type": "sale", "store_id": "store-1", "product_id": "low-sugar",
                   "quantity": 3, "occurred_at": T("08:00"), "event_id": "evt-sale-1"}
        svc.record_event(dict(payload))
        svc.record_event(dict(payload))
        inv = svc.inventory("store-1", "low-sugar", T("09:00"))
        self.assertEqual(inv["on_hand"], 7)


class SafetyWindowTest(unittest.TestCase):
    def test_expired_batch_never_returns_to_sellable(self):
        svc = make_service()
        receive(svc, qty=10, produced="06:00")  # 安全窗口至 16:00
        with self.assertRaises(SafetyWindowViolation):
            svc.record_event({"type": "relist", "store_id": "store-1", "product_id": "low-sugar",
                              "batch_id": self._batch_id(svc), "occurred_at": T("17:00")})
        inv = svc.inventory("store-1", "low-sugar", T("17:00"))
        self.assertEqual(inv["sellable"], 0)
        self.assertEqual(inv["expired_stock"], 10)

    def test_forecast_demand_does_not_resurrect_expired_stock(self):
        svc = make_service()
        receive(svc, qty=10, produced="06:00")
        svc.generate("store-1", T("17:00"))  # 预测缺口再大也只能建议现烤/调拨
        inv = svc.inventory("store-1", "low-sugar", T("17:30"))
        self.assertEqual(inv["sellable"], 0)
        self.assertEqual(inv["expired_stock"], 10)
        recs = svc.list_recommendations("store-1")
        self.assertTrue(recs)
        self.assertTrue(all(r["action"] in ("现烤", "调拨") for r in recs))

    def _batch_id(self, svc):
        return svc.inventory("store-1", "low-sugar", T("07:00"))["lots"][0]["batch_id"]


class CorrectionEventTest(unittest.TestCase):
    def test_reservation_cancel_restores_sellable_at_occurrence_time(self):
        svc = make_service()
        receive(svc, qty=10)
        svc.record_event({"type": "reservation", "store_id": "store-1", "product_id": "low-sugar",
                          "quantity": 4, "order_id": "order-1", "occurred_at": T("08:00")})
        self.assertEqual(svc.inventory("store-1", "low-sugar", T("08:30"))["sellable"], 6)
        svc.record_event({"type": "reservation_cancelled", "store_id": "store-1",
                          "product_id": "low-sugar", "quantity": 4,
                          "order_id": "order-1", "occurred_at": T("12:00")})
        self.assertEqual(svc.inventory("store-1", "low-sugar", T("10:00"))["reserved"], 4)
        self.assertEqual(svc.inventory("store-1", "low-sugar", T("13:00"))["sellable"], 10)

    def test_delivery_failed_after_window_returns_to_expired_not_sellable(self):
        svc = make_service()
        receive(svc, qty=10, produced="06:00")  # 窗口至 16:00
        svc.record_event({"type": "sale", "store_id": "store-1", "product_id": "low-sugar",
                          "quantity": 3, "order_id": "order-9", "occurred_at": T("10:00")})
        svc.record_event({"type": "delivery_failed", "store_id": "store-1", "product_id": "low-sugar",
                          "quantity": 3, "order_id": "order-9", "occurred_at": T("16:30")})
        inv = svc.inventory("store-1", "low-sugar", T("17:00"))
        self.assertEqual(inv["sellable"], 0)
        self.assertEqual(inv["expired_stock"], 10)  # 7 在架过期 + 3 退回过期
        self.assertEqual(svc.order_label("order-9")["status"], "配送失败")

    def test_night_clearance_deducts_at_occurrence_time(self):
        svc = make_service()
        receive(svc, qty=10, produced="12:00", at="12:30")  # 安全窗口至 22:00
        svc.record_event({"type": "clearance", "store_id": "store-1", "product_id": "low-sugar",
                          "quantity": 4, "occurred_at": T("21:00"), "payload": {"price": 5.0}})
        self.assertEqual(svc.inventory("store-1", "low-sugar", T("20:00"))["on_hand"], 10)
        self.assertEqual(svc.inventory("store-1", "low-sugar", T("21:30"))["on_hand"], 6)
        report = svc.stockout_waste("store-1", f"{DAY}T00:00:00Z", f"{DAY}T23:59:59Z")
        row = report["stores"][0]["products"][0]
        self.assertEqual(row["清仓量"], 4)
        self.assertEqual(row["清仓营收"], 20.0)

    def test_store_closure_zeroes_demand_in_recommendation(self):
        svc = make_service()
        svc.record_event({"type": "store_closed", "store_id": "store-1", "occurred_at": T("12:00")})
        svc.record_event({"type": "store_reopened", "store_id": "store-1", "occurred_at": T("14:00")})
        recs = svc.generate("store-1", T("11:00"), horizon_hours=6)
        low_sugar = [r for r in recs if r["product_id"] == "low-sugar"]
        self.assertTrue(low_sugar)
        closures = low_sugar[0]["data_used"]["闭店时段"]
        self.assertEqual(len(closures), 1)
        # 12:00-14:00 闭店，需求只剩 11:00-12:00 与 14:00-17:00
        self.assertEqual(low_sugar[0]["data_used"]["剩余时段需求"], 2 * 1 + 2 * 3)


class RecipeLabelTest(unittest.TestCase):
    def test_label_follows_batch_and_old_orders_keep_purchase_label(self):
        svc = make_service()
        svc.add_recipe("low-sugar", {
            "version": 2, "effective_from": T("12:00"),
            "nutrition": {"热量": 210, "糖": 2}, "allergens": ["麸质", "坚果"],
        })
        receive(svc, qty=5, produced="08:00", scan="scan-a", at="08:30")
        receive(svc, qty=5, produced="13:00", scan="scan-b", at="13:30")
        svc.record_event({"type": "sale", "store_id": "store-1", "product_id": "low-sugar",
                          "quantity": 5, "order_id": "order-old", "occurred_at": T("09:00")})
        svc.record_event({"type": "sale", "store_id": "store-1", "product_id": "low-sugar",
                          "quantity": 2, "order_id": "order-new", "occurred_at": T("14:00")})
        old_label = svc.order_label("order-old")["label"]
        new_label = svc.order_label("order-new")["label"]
        self.assertEqual(old_label["recipe_version"], 1)
        self.assertEqual(old_label["allergens"], ["麸质"])
        self.assertEqual(new_label["recipe_version"], 2)
        self.assertIn("坚果", new_label["allergens"])
        # 旧订单不随后续配方调整变化
        svc.add_recipe("low-sugar", {
            "version": 3, "effective_from": T("18:00"),
            "nutrition": {"热量": 200}, "allergens": [],
        })
        self.assertEqual(svc.order_label("order-old")["label"]["recipe_version"], 1)


class DecisionTest(unittest.TestCase):
    def test_gap_triggers_bake_with_data_and_cost(self):
        svc = make_service()
        recs = svc.generate("store-1", T("06:00"), horizon_hours=12)
        bake = [r for r in recs if r["action"] == "现烤" and r["product_id"] == "low-sugar"]
        self.assertTrue(bake)
        rec = bake[0]
        self.assertGreater(rec["quantity"], 0)
        self.assertEqual(rec["demand_version"], "fc-0927")
        self.assertEqual(rec["cost"]["避免缺货损失"], round(rec["quantity"] * 7.0, 2))
        self.assertIn("剩余时段需求", rec["data_used"])

    def test_surplus_triggers_markdown_and_stop_production(self):
        svc = make_service()
        receive(svc, product="sweet", qty=60, produced="06:00", scan="scan-sweet")
        svc.plan_batch({"store_id": "store-1", "product_id": "sweet", "quantity": 20,
                        "ready_at": T("15:00"), "batch_id": "plan-sweet-1"})
        recs = svc.generate("store-1", T("10:00"), horizon_hours=10)
        markdown = [r for r in recs if r["action"] == "降价" and r["product_id"] == "sweet"]
        stop = [r for r in recs if r["action"] == "停止制作" and r["product_id"] == "sweet"]
        self.assertTrue(markdown)
        self.assertTrue(stop)
        self.assertGreater(markdown[0]["cost"]["避免报损成本"], 0)
        self.assertEqual(stop[0]["data_used"]["取消明细"][0]["batch_id"], "plan-sweet-1")

    def test_gap_without_capacity_triggers_transfer(self):
        svc = make_service()
        svc.set_capacity("store-2", {"oven": {}, "baking_hours": [5, 20]})
        svc.register_forecast({"store_id": "store-2", "version": "fc-0927-s2",
                               "hourly": {"low-sugar": hourly(peak=1, base=0)}})
        receive(svc, qty=50, produced="05:00", scan="scan-s2", at="05:30", store="store-2")
        svc.set_capacity("store-1", {"oven": {}, "baking_hours": [5, 20]})  # 门店1无烤炉产能
        recs = svc.generate("store-1", T("06:00"), horizon_hours=12)
        transfer = [r for r in recs if r["action"] == "调拨"]
        self.assertTrue(transfer)
        rec = transfer[0]
        self.assertEqual(rec["data_used"]["调拨来源"][0]["store_id"], "store-2")
        result = svc.execute(rec["recommendation_id"], {"actor": "区域负责人", "occurred_at": T("06:30")})
        self.assertEqual(result["recommendation"]["status"], "已执行")
        inv2 = svc.inventory("store-2", "low-sugar", T("07:00"))
        self.assertEqual(inv2["sellable"], 50 - rec["quantity"])
        # 收货门店扫码入库，重复扫码不多入库
        svc.receive_batch({"scan_id": "scan-transfer", "store_id": "store-1", "product_id": "low-sugar",
                           "quantity": rec["quantity"], "produced_at": T("06:00"),
                           "occurred_at": T("08:00"), "source": "调拨"})
        svc.receive_batch({"scan_id": "scan-transfer", "store_id": "store-1", "product_id": "low-sugar",
                           "quantity": rec["quantity"], "produced_at": T("06:00"),
                           "occurred_at": T("08:00"), "source": "调拨"})
        self.assertEqual(svc.inventory("store-1", "low-sugar", T("09:00"))["sellable"], rec["quantity"])

    def test_trace_links_version_execution_sellout_and_waste(self):
        svc = make_service()
        rec = [r for r in svc.generate("store-1", T("06:00"), horizon_hours=12)
               if r["action"] == "现烤"][0]
        svc.execute(rec["recommendation_id"], {"actor": "店长", "occurred_at": T("06:10")})
        batch_id = [b for b in svc.repo.batches.values() if b.source == "现烤"][0].batch_id
        svc.receive_batch({"scan_id": "scan-bake", "batch_id": batch_id, "store_id": "store-1",
                           "product_id": "low-sugar", "quantity": rec["quantity"],
                           "occurred_at": T("07:10")})
        svc.record_event({"type": "sale", "store_id": "store-1", "product_id": "low-sugar",
                          "quantity": rec["quantity"], "order_id": "ord-trace",
                          "occurred_at": T("09:30")})
        svc.record_event({"type": "delivery_failed", "store_id": "store-1", "product_id": "low-sugar",
                          "quantity": 2, "order_id": "ord-trace", "occurred_at": T("10:00")})
        svc.record_event({"type": "waste", "store_id": "store-1", "product_id": "low-sugar",
                          "quantity": 2, "occurred_at": T("21:00")})
        trace = svc.trace(rec["recommendation_id"])
        self.assertEqual(trace["demand_version"], "fc-0927")
        self.assertEqual(trace["executions"][0]["actor"], "店长")
        self.assertEqual(trace["sellout_at"], T("09:30"))
        self.assertEqual(trace["waste"]["units"], 2)


class AnalyticsTest(unittest.TestCase):
    def test_stockout_and_waste_are_compared(self):
        svc = make_service()
        receive(svc, qty=5)
        receive(svc, product="sweet", qty=10, scan="scan-sweet-a")
        result = svc.record_event({"type": "sale", "store_id": "store-1", "product_id": "low-sugar",
                                   "quantity": 8, "occurred_at": T("08:00")})
        self.assertEqual(result["fulfilled"], 5)
        self.assertEqual(result["stockout"], 3)
        svc.record_event({"type": "waste", "store_id": "store-1", "product_id": "sweet",
                          "quantity": 4, "occurred_at": T("21:00")})
        report = svc.stockout_waste("store-1", f"{DAY}T00:00:00Z", f"{DAY}T23:59:59Z")
        rows = {r["product_id"]: r for r in report["stores"][0]["products"]}
        self.assertEqual(rows["low-sugar"]["缺货量"], 3)
        self.assertEqual(rows["low-sugar"]["缺货损失"], 21.0)
        self.assertEqual(rows["sweet"]["报损量"], 4)
        self.assertEqual(rows["sweet"]["报损成本"], 14.0)
        self.assertIn("损耗缺货比", report["缺货与损耗对比"])

    def test_reservation_beyond_sellable_fails_and_records_stockout(self):
        svc = make_service()
        receive(svc, qty=2)
        with self.assertRaises(DomainError):
            svc.record_event({"type": "reservation", "store_id": "store-1", "product_id": "low-sugar",
                              "quantity": 5, "order_id": "order-x", "occurred_at": T("08:00")})
        report = svc.stockout_waste("store-1", f"{DAY}T00:00:00Z", f"{DAY}T23:59:59Z")
        rows = {r["product_id"]: r for r in report["stores"][0]["products"]}
        self.assertEqual(rows["low-sugar"]["缺货量"], 5)


if __name__ == "__main__":
    unittest.main()
