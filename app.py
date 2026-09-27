"""应用门面：把事件存储、投影、需求版本、建议引擎组合成一组用例方法。"""

from config import COST_MODEL, SKUS
from events import DuplicateEvent, EventStore
from forecast import ForecastStore
from model import Inventory
from replenish import RecommendationStore


class BakeryApp:
    def __init__(self):
        self.events = EventStore()
        self.forecasts = ForecastStore()
        self.recs = RecommendationStore()

    # ---------- 事件 ----------

    def submit_event(self, event):
        """提交事件；重复 event_id 返回 duplicate=True，不产生效果。
        追加后试投影校验业务合法性（如超卖、报损超量），失败则回滚。"""
        saved, duplicate = self.events.try_append(event)
        if duplicate:
            return {"duplicate": True, "event": None}
        try:
            Inventory(self.events)
        except (ValueError, KeyError) as exc:
            self.events.discard(saved["event_id"])
            return {"duplicate": False, "event": None, "rejected": True, "reason": str(exc)}
        return {"duplicate": False, "event": saved}

    def inventory(self, at=None):
        return Inventory(self.events, at=at)

    # ---------- 库存查询 ----------

    def stock(self, store_id=None, sku=None, at=None):
        inv = self.inventory(at)
        return {"at": at or inv._last_event_at(), "batches": inv.stock_view(store_id, sku, at)}

    def at_risk(self, store_id=None, at=None):
        inv = self.inventory(at)
        rows = []
        for b in inv.batches.values():
            if store_id and b["store_id"] != store_id:
                continue
            blocked = b["pools"]["blocked"] + b["pools"]["clearance"]
            if blocked > 0:
                rows.append({
                    "batch_id": b["batch_id"], "store_id": b["store_id"], "sku": b["sku"],
                    "safe_until": b["safe_until"], "blocked_qty": blocked,
                    "note": "已过安全窗口，不得重新上架，仅可降价/清仓/报损",
                })
        return rows

    # ---------- 需求与建议 ----------

    def generate_forecast(self, store_id, sku, day_start, horizon_end, note="", at=None):
        return self.forecasts.generate(self.inventory(at), store_id, sku,
                                       day_start, horizon_end, note)

    def generate_recommendations(self, at, horizon_end, store_id=None, sku=None):
        inv = self.inventory(at)
        return self.recs.generate(inv, self.forecasts.latest, at, horizon_end,
                                  store_id=store_id, sku=sku)

    def execute(self, rec_id, at, qty=None, note=""):
        return self.recs.execute(rec_id, at, qty, note)

    def trace(self, rec_id):
        return self.recs.trace(rec_id, self.inventory())

    # ---------- 订单标签 ----------

    def order_label(self, order_id):
        order = self.inventory().order.get(order_id)
        if order is None:
            return None
        return {
            "order_id": order_id,
            "sku": order["sku"],
            "status": order["status"],
            "placed_at": order["placed_at"],
            "label": order["label_snapshot"],
            "note": "标签按下单时刻冻结；后续配方调整不影响本订单解释",
        }

    # ---------- 区域指标：缺货 vs 损耗 ----------

    def region_metrics(self, start, end):
        inv = self.inventory()
        per_store = {}
        for b in inv.batches.values():
            sid = b["store_id"]
            m = per_store.setdefault(sid, {"waste_qty": 0, "waste_cost": 0.0,
                                           "clearance_qty": 0, "sold_qty": 0,
                                           "shortage_qty": 0, "shortage_cost": 0.0})
            if not (start <= b["produced_at"] <= end or start <= b["safe_until"] <= end):
                continue
            sku = SKUS[b["sku"]]
            unit_cost = sku["ingredient_cost"] + sku["labor_cost"]
            m["waste_qty"] += b["pools"]["waste"]
            m["waste_cost"] += round(unit_cost * b["pools"]["waste"], 2)
            m["clearance_qty"] += b["pools"]["clearance"] + b["pools"]["sold_clearance"]
            m["sold_qty"] += b["pools"]["sold"] + b["pools"]["sold_markdown"]
        # 缺货：售罄持续时长占需求窗口的比例 × 该版本预期剩余需求
        from timeutil import minutes_between
        for s in inv.sellouts:
            if not (start <= s["sold_out_at"] <= end):
                continue
            sid = s["store_id"]
            m = per_store.setdefault(sid, {"waste_qty": 0, "waste_cost": 0.0,
                                           "clearance_qty": 0, "sold_qty": 0,
                                           "shortage_qty": 0, "shortage_cost": 0.0})
            fc = self.forecasts.latest(sid, s["sku"])
            if not fc or fc["expected_remaining_qty"] <= 0:
                continue
            horizon_end = fc["horizon"]["end"]
            window = max(minutes_between(fc["generated_at"], horizon_end), 1.0)
            soldout_end = s["restocked_at"] or horizon_end
            lost_minutes = max(0.0, minutes_between(s["sold_out_at"], soldout_end))
            est = fc["expected_remaining_qty"] * min(1.0, lost_minutes / window)
            m["shortage_qty"] += round(est)
            m["shortage_cost"] += round(SKUS[s["sku"]]["unit_price"]
                                        * COST_MODEL["lost_sale_rate"] * est, 2)
        for m in per_store.values():
            m["waste_cost"] = round(m["waste_cost"], 2)
            m["shortage_cost"] = round(m["shortage_cost"], 2)
        return {"window": {"start": start, "end": end}, "per_store": per_store}
