"""领域投影：把事件流按发生时间重放为批次库存、订单、配方版本、闭店区间。

核心纪律：
1. 投影只依赖 occurred_at，与登记顺序无关；晚到事件归位后重放即可。
2. 超过 safe_until（安全窗口）的数量只能进入 blocked/clearance/waste，
   任何事件都不能让它回到 available —— 预测需求也不行。
3. 批次在生产瞬间固定配方版本；订单在下单瞬间冻结标签。
4. 重复 event_id 在 EventStore 层已被拦截，投影天然只处理一次。
"""

from collections import defaultdict

from config import BASELINE_RECIPES, SKUS
from events import EventStore
from timeutil import fmt, parse

# 库存池：可动用量与沉没量分开记录
LIVE_POOLS = ["available", "reserved", "in_delivery", "returning", "markdown", "clearance", "blocked"]
SINK_POOLS = ["sold", "sold_markdown", "sold_clearance", "transferred_out", "waste"]


def safe_until(batch):
    return fmt(parse(batch["produced_at"]) + _safe_delta(batch["sku"]))


def shelf_until(batch):
    return fmt(parse(batch["produced_at"]) + _shelf_delta(batch["sku"]))


def _safe_delta(sku):
    from datetime import timedelta
    return timedelta(hours=SKUS[sku]["safe_hours"])


def _shelf_delta(sku):
    from datetime import timedelta
    return timedelta(hours=SKUS[sku]["shelf_hours"])


class Inventory:
    def __init__(self, store, at=None):
        self.store = store
        self.at = at
        self.batches = {}                 # batch_id -> batch 记录
        self.order = {}                   # order_id -> 订单（含标签快照）
        self.recipes = defaultdict(list)  # sku -> [版本...]
        self.plans = []                   # 生产计划
        self.closures = []                # 闭店区间
        self.sales = []                   # 销量明细（分时）
        self.sellouts = []                # 售罄/回补时间线
        self.interceptions = []           # 安全窗口拦截记录
        self.transfers = []               # 调拨台账
        self.watermark = None
        self._build()

    # ---------- 构建 ----------

    def _build(self):
        for r in BASELINE_RECIPES:
            self.recipes[r["sku"]].append(dict(r))
        events = self.store.all()
        if self.at is not None:
            events = [e for e in events if e["occurred_at"] <= self.at]
        for e in events:
            self._apply(e)
            self._expire(e["occurred_at"])
        if events:
            self.watermark = {"occurred_at": events[-1]["occurred_at"], "event_count": len(events)}

    def _apply(self, e):
        t, typ = e["occurred_at"], e["type"]
        p = e.get("payload", {})
        handler = getattr(self, f"_on_{typ}", None)
        if handler:
            handler(e, t, p)

    @staticmethod
    def _ref(e, p, key, default=None):
        """业务引用（批次/计划/调拨号）可放在事件顶层或 payload。"""
        return e.get(key, p.get(key, default))

    # ---------- 配方 ----------

    def recipe_at(self, sku, t):
        """t 时刻对 sku 生效的配方版本。"""
        versions = [r for r in self.recipes[sku] if r["effective_from"] <= t]
        return max(versions, key=lambda r: r["effective_from"])

    def _on_recipe_published(self, e, t, p):
        sku = p.get("sku") or e["sku"]
        rec = {
            "sku": sku,
            "version": p["version"],
            "effective_from": p.get("effective_from", t),
            "nutrition": p["nutrition"],
            "allergens": p["allergens"],
            "note": p.get("note", ""),
            "event_id": e["event_id"],
        }
        self.recipes[sku].append(rec)

    # ---------- 批次产生 ----------

    def _new_batch(self, batch_id, store_id, sku, produced_at, qty, origin, recipe_version, source=None):
        self.batches[batch_id] = {
            "batch_id": batch_id,
            "store_id": store_id,
            "sku": sku,
            "produced_at": produced_at,
            "safe_until": None,  # 延迟计算
            "origin": origin,
            "recipe_version": recipe_version,
            "source": source or {},
            "pools": {k: 0 for k in LIVE_POOLS + SINK_POOLS},
        }
        b = self.batches[batch_id]
        b["safe_until"] = safe_until(b)
        b["shelf_until"] = shelf_until(b)
        return b

    def _on_baked(self, e, t, p):
        sku = e["sku"]
        batch_id = self._ref(e, p, "batch_id") or f"BAKE-{e['event_id']}"
        recipe = self.recipe_at(sku, t)["version"]
        b = self._new_batch(batch_id, e["store_id"], sku, t, e["qty"], "bake", recipe,
                            {"event_id": e["event_id"]})
        b["pools"]["available"] += e["qty"]
        self._maybe_restock(e["store_id"], sku, t)

    def _on_received_from_plant(self, e, t, p):
        sku = e["sku"]
        produced_at = p["produced_at"]
        # 工厂来货按工厂生产时刻解析配方并固定到批次
        recipe = self.recipe_at(sku, produced_at)["version"]
        b = self._new_batch(self._ref(e, p, "batch_id"), e["store_id"], sku, produced_at,
                            e["qty"], "plant",
                            recipe, {"plant_batch": p.get("plant_batch"), "event_id": e["event_id"]})
        b["pools"]["available"] += e["qty"]
        self._maybe_restock(e["store_id"], sku, t)

    # ---------- 销量 ----------

    def _fifo(self, store_id, sku, pool, qty, t, batch_id=None):
        """从指定池按最早过安全窗口的批次先扣，返回 [(batch_id, qty)]。不足时整体拒绝。"""
        candidates = [
            b for b in self.batches.values()
            if b["store_id"] == store_id and b["sku"] == sku and b["pools"][pool] > 0
            and (batch_id is None or b["batch_id"] == batch_id)
        ]
        candidates.sort(key=lambda b: b["safe_until"])
        if sum(b["pools"][pool] for b in candidates) < qty:
            raise ValueError(f"{store_id}/{sku} 可扣数量不足（池 {pool}，需 {qty}）")
        taken, remain = [], qty
        for b in candidates:
            if remain <= 0:
                break
            n = min(remain, b["pools"][pool])
            b["pools"][pool] -= n
            remain -= n
            taken.append((b["batch_id"], n))
        return taken

    def _add(self, batch_id, pool, qty):
        self.batches[batch_id]["pools"][pool] += qty

    def _on_sale_recorded(self, e, t, p):
        store_id, sku, qty = e["store_id"], e["sku"], e["qty"]
        price_level = p.get("price_level", "regular")
        pool = {"regular": "available", "markdown": "markdown", "clearance": "clearance"}[price_level]
        sink = {"regular": "sold", "markdown": "sold_markdown", "clearance": "sold_clearance"}[price_level]
        before = self.totals(store_id, sku)["sellable"]
        taken = self._fifo(store_id, sku, pool, qty, t, self._ref(e, p, "batch_id"))
        for bid, n in taken:
            self._add(bid, sink, n)
        self.sales.append({
            "at": t, "store_id": store_id, "sku": sku, "qty": qty,
            "price_level": price_level, "channel": p.get("channel", "pos"),
            "batches": taken, "event_id": e["event_id"],
        })
        after = self.totals(store_id, sku)["sellable"]
        self._note_sellout(store_id, sku, t, before, after)

    def _note_sellout(self, store_id, sku, t, before, after):
        last = next((s for s in reversed(self.sellouts)
                     if s["store_id"] == store_id and s["sku"] == sku), None)
        if before > 0 and after == 0 and (last is None or last.get("restocked_at")):
            self.sellouts.append({"store_id": store_id, "sku": sku, "sold_out_at": t,
                                  "restocked_at": None})
        elif after > 0:
            self._maybe_restock(store_id, sku, t)

    def _maybe_restock(self, store_id, sku, t):
        """入库类事件后若可售由 0 转正，记录回补时间。"""
        last = next((s for s in reversed(self.sellouts)
                     if s["store_id"] == store_id and s["sku"] == sku), None)
        if (last is not None and last.get("restocked_at") is None
                and last["sold_out_at"] < t
                and self.totals(store_id, sku)["sellable"] > 0):
            last["restocked_at"] = t

    # ---------- 预订 / 配送 ----------

    def _on_reservation_placed(self, e, t, p):
        store_id, sku, qty = e["store_id"], e["sku"], e["qty"]
        taken = self._fifo(store_id, sku, "available", qty, t, self._ref(e, p, "batch_id"))
        for bid, n in taken:
            self._add(bid, "reserved", n)
        label = self.recipe_at(sku, t)
        self.order[e["order_id"]] = {
            "order_id": e["order_id"], "store_id": store_id, "sku": sku, "qty": qty,
            "placed_at": t, "status": "reserved",
            "allocations": taken,
            "label_snapshot": {
                "recipe_version": label["version"],
                "nutrition": label["nutrition"],
                "allergens": label["allergens"],
                "note": label["note"],
                "frozen_at": t,
            },
        }

    def _order_allocations(self, order_id):
        return self.order[order_id]["allocations"]

    def _on_reservation_cancelled(self, e, t, p):
        order = self.order[e["order_id"]]
        order["status"] = "cancelled"
        order["cancelled_at"] = t
        # 按取消发生时间回补：批次仍在安全窗口内才可回可售；
        # 若预留期间批次已过期，数量已被 _expire 迁入 blocked，保持拦截。
        for bid, n in self._order_allocations(e["order_id"]):
            b = self.batches[bid]
            still_reserved = min(n, b["pools"]["reserved"])
            if still_reserved <= 0:
                continue
            b["pools"]["reserved"] -= still_reserved
            target = "available" if b["safe_until"] > t else "blocked"
            b["pools"][target] += still_reserved
            if target == "blocked":
                self.interceptions.append({
                    "at": t, "store_id": b["store_id"], "sku": b["sku"], "batch_id": bid,
                    "qty": still_reserved, "reason": "取消回补时已过安全窗口",
                    "order_id": e["order_id"],
                })
            else:
                self._maybe_restock(b["store_id"], b["sku"], t)

    def _on_delivery_dispatched(self, e, t, p):
        order = self.order[e["order_id"]]
        order["status"] = "in_delivery"
        order["dispatched_at"] = t
        for bid, n in self._order_allocations(e["order_id"]):
            b = self.batches[bid]
            b["pools"]["reserved"] -= n
            b["pools"]["in_delivery"] += n

    def _on_delivery_delivered(self, e, t, p):
        order = self.order[e["order_id"]]
        order["status"] = "delivered"
        order["delivered_at"] = t
        for bid, n in self._order_allocations(e["order_id"]):
            b = self.batches[bid]
            b["pools"]["in_delivery"] -= n
            b["pools"]["sold"] += n

    def _on_delivery_failed(self, e, t, p):
        order = self.order[e["order_id"]]
        order["status"] = "delivery_failed"
        order["failed_at"] = t
        for bid, n in self._order_allocations(e["order_id"]):
            b = self.batches[bid]
            b["pools"]["in_delivery"] -= n
            b["pools"]["returning"] += n

    def _on_return_restocked(self, e, t, p):
        """配送失败后实物带回门店：按带回时间对照安全窗口决定去向。"""
        order = self.order[e["order_id"]]
        order["returned_at"] = t
        for bid, n in self._order_allocations(e["order_id"]):
            b = self.batches[bid]
            b["pools"]["returning"] -= n
            if b["safe_until"] > t:
                b["pools"]["available"] += n
                order["status"] = "restocked"
                self._maybe_restock(b["store_id"], b["sku"], t)
            else:
                # 超过安全窗口：绝不能因预测需求重新上架
                b["pools"]["blocked"] += n
                order["status"] = "blocked_by_safe_window"
                self.interceptions.append({
                    "at": t, "store_id": b["store_id"], "sku": b["sku"], "batch_id": bid,
                    "qty": n, "reason": "配送失败带回时已过安全窗口，拦截重新上架",
                    "order_id": e["order_id"],
                })

    # ---------- 调拨 ----------

    def _on_transfer_out(self, e, t, p):
        taken = self._fifo(e["store_id"], e["sku"], "available", e["qty"], t,
                           self._ref(e, p, "batch_id"))
        for bid, n in taken:
            self._add(bid, "transferred_out", n)
        self.transfers.append({"transfer_id": self._ref(e, p, "transfer_id") or e["event_id"],
                               "from_store": e["store_id"], "to_store": p["to_store"],
                               "sku": e["sku"], "qty": e["qty"], "at": t, "batches": taken})

    def _on_transfer_in(self, e, t, p):
        # 鲜度时钟随货走：沿用原批次的生产时间，不因调拨重置安全窗口
        origin = self.batches[p["origin_batch"]]
        transfer_id = self._ref(e, p, "transfer_id")
        batch_id = self._ref(e, p, "batch_id") or f"{p['origin_batch']}@{transfer_id or e['event_id']}"
        b = self._new_batch(batch_id, e["store_id"], e["sku"], origin["produced_at"], e["qty"],
                            "transfer", origin["recipe_version"],
                            {"origin_batch": p["origin_batch"], "transfer_id": transfer_id})
        b["pools"]["available"] += e["qty"]
        self._maybe_restock(e["store_id"], e["sku"], t)

    # ---------- 闭店 / 降价 / 清仓 / 报损 ----------

    def _on_store_closed(self, e, t, p):
        self.closures.append({"store_id": e["store_id"], "start": p["start"], "end": p["end"],
                              "reason": p.get("reason", "临时闭店"), "event_id": e["event_id"]})

    def _on_markdown_applied(self, e, t, p):
        taken = self._fifo(e["store_id"], e["sku"], "available", e["qty"], t,
                           self._ref(e, p, "batch_id"))
        for bid, n in taken:
            self._add(bid, "markdown", n)

    def _on_night_clearance(self, e, t, p):
        """夜间清仓按发生时间把数量移岀可售；优先清已拦截/临期批次。"""
        remain = e["qty"]
        for pool in ("blocked", "markdown", "available"):
            if remain <= 0:
                break
            taken = self._fifo(e["store_id"], e["sku"], pool,
                               min(remain, self._pool_total(e["store_id"], e["sku"], pool)),
                               t, self._ref(e, p, "batch_id"))
            for bid, n in taken:
                self._add(bid, "clearance", n)
            remain -= sum(n for _, n in taken)

    def _pool_total(self, store_id, sku, pool):
        return sum(b["pools"][pool] for b in self.batches.values()
                   if b["store_id"] == store_id and b["sku"] == sku)

    def _on_stock_written_off(self, e, t, p):
        remain = e["qty"]
        for pool in ("blocked", "clearance", "markdown", "available", "returning"):
            if remain <= 0:
                break
            taken = self._fifo(e["store_id"], e["sku"], pool,
                               min(remain, self._pool_total(e["store_id"], e["sku"], pool)),
                               t, self._ref(e, p, "batch_id"))
            for bid, n in taken:
                self._add(bid, "waste", n)
            remain -= sum(n for _, n in taken)
        if remain > 0:
            raise ValueError(f"报损数量超过在店数量，余 {remain}")

    # ---------- 生产计划 ----------

    def _on_production_planned(self, e, t, p):
        self.plans.append({"plan_id": self._ref(e, p, "plan_id") or e["event_id"],
                           "store_id": e["store_id"],
                           "sku": e["sku"], "qty": e["qty"], "planned_at": p["planned_at"],
                           "status": "planned", "event_id": e["event_id"]})

    def _on_production_cancelled(self, e, t, p):
        plan = next(x for x in self.plans if x["plan_id"] == self._ref(e, p, "plan_id"))
        plan["status"] = "cancelled"
        plan["cancelled_at"] = t
        plan["cancel_event_id"] = e["event_id"]

    # ---------- 安全窗口到期迁移 ----------

    def _expire(self, now):
        """把已过安全窗口仍在可动池的数量迁入 blocked（在每个事件时点推进）。"""
        for b in self.batches.values():
            if b["safe_until"] <= now:
                for pool in ("available", "markdown", "reserved"):
                    n = b["pools"][pool]
                    if n:
                        b["pools"][pool] -= n
                        b["pools"]["blocked"] += n
                        self.interceptions.append({
                            "at": b["safe_until"], "store_id": b["store_id"], "sku": b["sku"],
                            "batch_id": b["batch_id"], "qty": n,
                            "reason": f"安全窗口到期自动下架（原池 {pool}）",
                        })

    # ---------- 查询 ----------

    def totals(self, store_id, sku):
        agg = {k: 0 for k in LIVE_POOLS + SINK_POOLS}
        for b in self.batches.values():
            if b["store_id"] == store_id and b["sku"] == sku:
                for k, v in b["pools"].items():
                    agg[k] += v
        agg["sellable"] = agg["available"] + agg["markdown"]
        return agg

    def batch_view(self, b, at):
        from timeutil import minutes_between
        remaining_min = minutes_between(at, b["safe_until"])
        if b["pools"]["waste"] and not any(b["pools"][k] for k in
                                           ("available", "reserved", "in_delivery",
                                            "returning", "markdown", "clearance", "blocked")):
            state = "已报损"
        elif b["pools"]["clearance"]:
            state = "夜间清仓"
        elif b["pools"]["blocked"]:
            state = "停止销售"
        elif remaining_min <= 0:
            state = "停止销售"
        elif remaining_min <= 60:
            state = "临期"
        elif any(b["pools"][k] for k in ("available", "reserved", "in_delivery", "markdown")):
            state = "可售"
        else:
            state = "已完结"
        return {
            "batch_id": b["batch_id"], "store_id": b["store_id"], "sku": b["sku"],
            "origin": b["origin"], "produced_at": b["produced_at"],
            "safe_until": b["safe_until"], "shelf_until": b["shelf_until"],
            "remaining_safe_minutes": max(0, int(remaining_min)),
            "recipe_version": b["recipe_version"], "state": state,
            "pools": dict(b["pools"]),
        }

    def stock_view(self, store_id=None, sku=None, at=None):
        at = at or self.at or self._last_event_at()
        out = []
        for b in self.batches.values():
            if store_id and b["store_id"] != store_id:
                continue
            if sku and b["sku"] != sku:
                continue
            out.append(self.batch_view(b, at))
        return sorted(out, key=lambda x: (x["store_id"], x["sku"], x["produced_at"]))

    def _last_event_at(self):
        events = self.store.all()
        return events[-1]["occurred_at"] if events else None

    def pending_plans(self, store_id, sku, at):
        return [p for p in self.plans if p["store_id"] == store_id and p["sku"] == sku
                and p["status"] == "planned" and p["planned_at"] >= at]

    def oven_free_at(self, store_id, at):
        """粗略烤炉占用：同一时刻计划/现烤中的炉次数是否超过炉位。"""
        from config import STORES
        import timeutil as tu
        slots = STORES[store_id]["oven_slots"]
        bake_min = STORES[store_id]["bake_minutes"]
        used = 0
        for p in self.plans:
            if p["store_id"] != store_id or p["status"] != "planned":
                continue
            end = tu.add_minutes(p["planned_at"], bake_min)
            if tu.overlaps(at, tu.add_minutes(at, 1), p["planned_at"], end):
                used += 1
        return used < slots, slots - used
