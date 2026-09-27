"""库存台账：事件只增不改，视图按发生时间回放，重复扫码幂等。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from domain import iso, utcnow

RECEIVE = "batch_received"
SALE = "sale"
RESERVATION = "reservation"
RESERVATION_CANCELLED = "reservation_cancelled"
DELIVERY_FAILED = "delivery_failed"
STORE_CLOSED = "store_closed"
STORE_REOPENED = "store_reopened"
CLEARANCE = "clearance"
WASTE = "waste"
TRANSFER_OUT = "transfer_out"
MARKDOWN = "markdown"
PRODUCTION_CANCELLED = "production_cancelled"
STOCKOUT = "stockout"
RELIST = "relist"

EVENT_TYPES = [
    RECEIVE, SALE, RESERVATION, RESERVATION_CANCELLED, DELIVERY_FAILED,
    STORE_CLOSED, STORE_REOPENED, CLEARANCE, WASTE, TRANSFER_OUT,
    MARKDOWN, PRODUCTION_CANCELLED, STOCKOUT, RELIST,
]

# 消耗可售库存、需要按批次 FEFO 分配的事件
_CONSUME_SELLABLE = {SALE, CLEARANCE, TRANSFER_OUT}
# 会改变库存或可售状态、用于定位售罄时点的事件
_MOVEMENT = {RECEIVE, SALE, CLEARANCE, WASTE, TRANSFER_OUT, DELIVERY_FAILED,
             RESERVATION, RESERVATION_CANCELLED}


@dataclass
class Event:
    event_id: str
    type: str
    store_id: str
    occurred_at: datetime
    product_id: str | None = None
    quantity: int = 0
    batch_id: str | None = None
    order_id: str | None = None
    scan_id: str | None = None
    payload: dict = field(default_factory=dict)
    recorded_at: datetime = field(default_factory=utcnow)
    seq: int = 0

    def to_dict(self):
        return {
            "event_id": self.event_id,
            "type": self.type,
            "store_id": self.store_id,
            "product_id": self.product_id,
            "quantity": self.quantity,
            "batch_id": self.batch_id,
            "order_id": self.order_id,
            "scan_id": self.scan_id,
            "occurred_at": iso(self.occurred_at),
            "recorded_at": iso(self.recorded_at),
            "payload": self.payload,
        }


class Ledger:
    """append-only 台账：重复 event_id / scan_id 直接返回原事件，不重复计数。"""

    def __init__(self):
        self._events = []
        self._by_id = {}
        self._by_scan = {}

    def append(self, event):
        existing = self._by_id.get(event.event_id)
        if existing is not None:
            return existing, False
        if event.scan_id:
            existing = self._by_scan.get(event.scan_id)
            if existing is not None:
                return existing, False
        event.seq = len(self._events) + 1
        self._events.append(event)
        self._by_id[event.event_id] = event
        if event.scan_id:
            self._by_scan[event.scan_id] = event
        return event, True

    def get(self, event_id):
        return self._by_id.get(event_id)

    def find_by_scan(self, scan_id):
        return self._by_scan.get(scan_id)

    def events_for(self, store_id=None, product_id=None, types=None, start=None, until=None):
        """按发生时间(并列按写入顺序)返回事件；晚到的修正落在其发生时刻。"""
        result = []
        for event in self._events:
            if store_id is not None and event.store_id != store_id:
                continue
            if product_id is not None and event.product_id != product_id:
                continue
            if types is not None and event.type not in types:
                continue
            if start is not None and event.occurred_at < start:
                continue
            if until is not None and event.occurred_at > until:
                continue
            result.append(event)
        result.sort(key=lambda e: (e.occurred_at, e.seq))
        return result

    # ---- 投影 ----

    def project_stock(self, batches, store_id, product_id, at):
        """回放到 at 时刻：批次剩余、收货/报损累计与未履约预订。"""
        remaining = {}
        received = {}
        wasted = {}
        reserved_by_order = {}
        for event in self.events_for(store_id, product_id, until=at):
            kind = event.type
            if kind == RECEIVE:
                remaining[event.batch_id] = remaining.get(event.batch_id, 0) + event.quantity
                received[event.batch_id] = received.get(event.batch_id, 0) + event.quantity
            elif kind in _CONSUME_SELLABLE:
                self._allocate(remaining, batches, event.quantity, event.occurred_at, sellable_only=True)
                if kind == SALE and event.order_id and event.order_id in reserved_by_order:
                    reserved_by_order[event.order_id] = max(0, reserved_by_order[event.order_id] - event.quantity)
            elif kind == WASTE:
                allocations = self._allocate(remaining, batches, event.quantity, event.occurred_at, sellable_only=False)
                for batch_id, qty in allocations:
                    wasted[batch_id] = wasted.get(batch_id, 0) + qty
            elif kind == RESERVATION:
                reserved_by_order[event.order_id] = reserved_by_order.get(event.order_id, 0) + event.quantity
            elif kind == RESERVATION_CANCELLED:
                if event.order_id in reserved_by_order:
                    reserved_by_order[event.order_id] = max(0, reserved_by_order[event.order_id] - event.quantity)
            elif kind == DELIVERY_FAILED:
                for batch_id, qty in event.payload.get("return_to", []):
                    remaining[batch_id] = remaining.get(batch_id, 0) + qty
        return remaining, received, wasted, reserved_by_order

    @staticmethod
    def _allocate(remaining, batches, quantity, at, sellable_only):
        """FEFO 分配：先消耗安全窗口最早的批次；返回实际扣减明细。"""
        candidates = []
        for batch_id, qty in remaining.items():
            if qty <= 0 or batch_id not in batches:
                continue
            batch = batches[batch_id]
            if sellable_only and batch.safety_until <= at:
                continue
            candidates.append((batch.safety_until, batch_id))
        candidates.sort()
        allocations = []
        left = quantity
        for _, batch_id in candidates:
            if left <= 0:
                break
            take = min(left, remaining[batch_id])
            remaining[batch_id] -= take
            left -= take
            allocations.append((batch_id, take))
        return allocations

    def allocate_preview(self, batches, store_id, product_id, quantity, at):
        """不写入事件地预演一次销售的批次分配(用于订单标签快照)。"""
        remaining, _, _, _ = self.project_stock(batches, store_id, product_id, at)
        return self._allocate(remaining, batches, quantity, at, sellable_only=True)

    def inventory_view(self, batches, store_id, product_id, at):
        """at 时刻的库存视图：可售 = 窗口内在架量 - 未履约预订。"""
        remaining, received, wasted, reserved_by_order = self.project_stock(batches, store_id, product_id, at)
        lots = []
        sellable_stock = 0
        expired_stock = 0
        for batch_id, batch in batches.items():
            if batch.store_id != store_id or batch.product_id != product_id:
                continue
            got = received.get(batch_id, 0)
            left = remaining.get(batch_id, 0)
            gone = wasted.get(batch_id, 0)
            state = batch.state_at(got, gone, at)
            lots.append({
                "batch_id": batch_id,
                "state": state,
                "remaining": left,
                "received": got,
                "wasted": gone,
                "planned_quantity": batch.quantity,
                "source": batch.source,
                "recipe_version": batch.recipe_version,
                "safety_until": iso(batch.safety_until),
                "expires_at": iso(batch.expires_at),
            })
            if state in ("可售", "临期"):
                sellable_stock += left
            elif state == "停止销售":
                expired_stock += left
        reserved = sum(qty for qty in reserved_by_order.values() if qty > 0)
        return {
            "store_id": store_id,
            "product_id": product_id,
            "at": iso(at),
            "on_hand": sum(remaining.values()),
            "reserved": reserved,
            "reserved_detail": {k: v for k, v in reserved_by_order.items() if v > 0},
            "sellable": max(0, sellable_stock - reserved),
            "sellable_stock": sellable_stock,
            "expired_stock": expired_stock,
            "lots": lots,
        }

    def closure_intervals(self, store_id, start, end):
        """临时闭店时段：[start, end] 内闭店未重开则视为闭到 end。"""
        intervals = []
        closed_at = None
        for event in self.events_for(store_id, types={STORE_CLOSED, STORE_REOPENED}, until=end):
            if event.type == STORE_CLOSED and closed_at is None:
                closed_at = event.occurred_at
            elif event.type == STORE_REOPENED and closed_at is not None:
                intervals.append((max(closed_at, start), min(event.occurred_at, end)))
                closed_at = None
        if closed_at is not None:
            intervals.append((max(closed_at, start), end))
        return [(s, e) for s, e in intervals if e > s]

    def first_sellout(self, batches, store_id, product_id, after, until):
        """after 之后在架可售库存从有到无、首次归零的发生时间(售罄时间)。"""
        had_stock = self.inventory_view(batches, store_id, product_id, after)["sellable_stock"] > 0
        for event in self.events_for(store_id, product_id, types=_MOVEMENT, start=after, until=until):
            stock = self.inventory_view(batches, store_id, product_id, event.occurred_at)["sellable_stock"]
            if stock > 0:
                had_stock = True
            elif had_stock:
                return event.occurred_at
        return None

    def waste_summary(self, store_id, product_id, start, until, unit_cost):
        """时段内报损量与成本，附事件明细。"""
        events = self.events_for(store_id, product_id, types={WASTE}, start=start, until=until)
        units = sum(e.quantity for e in events)
        return {
            "units": units,
            "cost": round(units * unit_cost, 2),
            "events": [e.to_dict() for e in events],
        }
