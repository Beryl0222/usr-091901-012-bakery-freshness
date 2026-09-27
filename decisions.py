"""补货与鲜度决策：现烤、调拨、降价、停止制作，建议附数据与成本且可追溯。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from math import ceil, floor
from uuid import uuid4

from domain import DomainError, iso, parse_ts


@dataclass
class Forecast:
    """分时需求预测版本：建议的需求依据，追溯链的起点。"""

    store_id: str
    version: str
    hourly: dict
    created_at: datetime

    def to_dict(self):
        return {
            "store_id": self.store_id,
            "version": self.version,
            "created_at": iso(self.created_at),
            "hourly": self.hourly,
        }


@dataclass
class Recommendation:
    recommendation_id: str
    store_id: str
    product_id: str
    action: str
    quantity: int
    created_at: datetime
    valid_until: datetime
    demand_version: str
    data_used: dict
    cost: dict
    status: str = "待执行"

    def to_dict(self):
        return {
            "recommendation_id": self.recommendation_id,
            "store_id": self.store_id,
            "product_id": self.product_id,
            "action": self.action,
            "quantity": self.quantity,
            "status": self.status,
            "created_at": iso(self.created_at),
            "valid_until": iso(self.valid_until),
            "demand_version": self.demand_version,
            "data_used": self.data_used,
            "cost": self.cost,
        }


@dataclass
class Execution:
    execution_id: str
    recommendation_id: str
    actor: str
    executed_at: datetime
    actual_quantity: int
    note: str = ""

    def to_dict(self):
        return {
            "execution_id": self.execution_id,
            "recommendation_id": self.recommendation_id,
            "actor": self.actor,
            "executed_at": iso(self.executed_at),
            "actual_quantity": self.actual_quantity,
            "note": self.note,
        }


def _open_fraction(start, end, closures):
    """时段内非闭店比例。"""
    span = (end - start).total_seconds()
    if span <= 0:
        return 0.0
    closed = 0.0
    for cs, ce in closures:
        closed += max(0.0, (min(end, ce) - max(start, cs)).total_seconds())
    return max(0.0, 1.0 - closed / span)


def demand_slots(hourly, start, end, closures):
    """把 24 小时分时需求切成 [start, end) 的时段槽，闭店时段需求归零。"""
    slots = []
    cursor = start
    while cursor < end:
        nxt = min(end, cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        span_hours = (nxt - cursor).total_seconds() / 3600
        qty = hourly[cursor.hour] * span_hours * _open_fraction(cursor, nxt, closures)
        if qty > 0:
            slots.append((cursor, qty))
        cursor = nxt
    return slots


def simulate_sales(lots, slots, end):
    """FEFO 模拟：返回 (可成交, 预计报损, 报损明细[(时点, 数量)])。"""
    pool = sorted([[deadline, qty] for deadline, qty in lots if qty > 0])
    sold = 0.0
    waste = 0.0
    expiries = []
    for slot_start, qty in slots:
        while pool and pool[0][0] <= slot_start:
            waste += pool[0][1]
            expiries.append((pool[0][0], pool[0][1]))
            pool.pop(0)
        need = qty
        while need > 1e-9 and pool:
            take = min(need, pool[0][1])
            pool[0][1] -= take
            need -= take
            sold += take
            if pool[0][1] <= 1e-9:
                pool.pop(0)
    for deadline, qty in pool:
        if deadline <= end:
            waste += qty
            expiries.append((deadline, qty))
    return sold, waste, expiries


def _baking_open_hours(capacity, start, end, closures):
    window = capacity.get("baking_hours") or [0, 24]
    hours = 0.0
    cursor = start
    while cursor < end:
        nxt = min(end, cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1))
        if window[0] <= cursor.hour < window[1]:
            hours += (nxt - cursor).total_seconds() / 3600 * _open_fraction(cursor, nxt, closures)
        cursor = nxt
    return hours


def _donor_surplus(repo, store_id, product_id, now, end):
    """其他门店同一商品超出自身剩余需求、可供调拨的在架量。"""
    donors = []
    for other in sorted(repo.store_ids()):
        if other == store_id:
            continue
        forecasts = repo.forecasts.get(other)
        if not forecasts:
            continue
        hourly = forecasts[-1].hourly.get(product_id)
        if not hourly:
            continue
        closures = repo.ledger.closure_intervals(other, now, end)
        demand = sum(qty for _, qty in demand_slots(hourly, now, end, closures))
        view = repo.ledger.inventory_view(repo.batches, other, product_id, now)
        surplus = floor(view["sellable"] - demand)
        if surplus > 0:
            donors.append({"store_id": other, "可调拨": surplus})
    return donors


def generate_recommendations(repo, store_id, now, horizon_hours=14):
    """对门店每个商品生成现烤/调拨/降价/停止制作建议，附数据与成本。"""
    forecasts = repo.forecasts.get(store_id)
    if not forecasts:
        raise DomainError(f"门店 {store_id} 缺少需求预测版本，无法生成建议")
    forecast = forecasts[-1]
    end = now + timedelta(hours=horizon_hours)
    closures = repo.ledger.closure_intervals(store_id, now, end)
    capacity = repo.capacity.get(store_id, {})
    created = []
    for product in repo.products.values():
        pid = product.product_id
        hourly = forecast.hourly.get(pid)
        if not hourly or len(hourly) != 24:
            continue
        view = repo.ledger.inventory_view(repo.batches, store_id, pid, now)
        slots = demand_slots(hourly, now, end, closures)
        demand_total = sum(qty for _, qty in slots)
        lots = [
            (parse_ts(lot["safety_until"]), lot["remaining"])
            for lot in view["lots"]
            if lot["state"] in ("可售", "临期") and lot["remaining"] > 0
        ]
        sold, projected_waste, expiries = simulate_sales(lots, slots, end)
        _, received_map, _, _ = repo.ledger.project_stock(repo.batches, store_id, pid, now)
        planned = [
            b for b in repo.batches.values()
            if b.store_id == store_id and b.product_id == pid
            and received_map.get(b.batch_id, 0) == 0
            and b.quantity > 0 and b.produced_at <= end
        ]
        planned_qty = sum(b.quantity for b in planned)
        unmet = max(0.0, demand_total - sold)
        gap = unmet - planned_qty
        data_used = {
            "需求版本": forecast.version,
            "当前可售": view["sellable"],
            "已预订": view["reserved"],
            "剩余时段需求": round(demand_total, 1),
            "预计可满足": round(sold, 1),
            "预计报损": round(projected_waste, 1),
            "计划产量": planned_qty,
            "闭店时段": [[iso(s), iso(e)] for s, e in closures],
        }
        if gap > 0.5:
            created.extend(_bake_or_transfer(
                repo, store_id, product, gap, capacity, now, end, closures, forecast, data_used))
        if projected_waste >= 1:
            created.append(_markdown_recommendation(
                store_id, product, projected_waste, expiries, now, end, forecast, data_used))
            if planned_qty > 0:
                created.append(_stop_recommendation(
                    store_id, product, planned, projected_waste, now, end, forecast, data_used))
    for rec in created:
        repo.recommendations[rec.recommendation_id] = rec
    return created


def _bake_or_transfer(repo, store_id, product, gap, capacity, now, end, closures, forecast, data_used):
    pid = product.product_id
    recs = []
    remaining_gap = gap
    if product.bakeable:
        per_hour = (capacity.get("oven") or {}).get(pid, 0)
        capacity_left = floor(per_hour * _baking_open_hours(capacity, now, end, closures))
        data_used["烤炉剩余产能"] = capacity_left
        bake_qty = min(ceil(remaining_gap), capacity_left)
        if bake_qty > 0:
            cost = {
                "单位成本": product.cost,
                "生产成本": round(bake_qty * product.cost, 2),
                "预期营收": round(bake_qty * product.price, 2),
                "避免缺货损失": round(bake_qty * product.unit_margin, 2),
            }
            recs.append(Recommendation(
                uuid4().hex, store_id, pid, "现烤", bake_qty, now, end,
                forecast.version, dict(data_used), cost))
            remaining_gap -= bake_qty
    if remaining_gap > 0.5:
        donors = _donor_surplus(repo, store_id, pid, now, end)
        if donors:
            qty = min(ceil(remaining_gap), sum(d["可调拨"] for d in donors))
            fee = product.transfer_fee
            data = dict(data_used)
            data["调拨来源"] = donors
            cost = {
                "调拨费用": round(qty * fee, 2),
                "保住毛利": round(qty * product.unit_margin, 2),
                "净收益": round(qty * (product.unit_margin - fee), 2),
            }
            recs.append(Recommendation(
                uuid4().hex, store_id, pid, "调拨", qty, now, end,
                forecast.version, data, cost))
        else:
            data_used["未满足缺口"] = round(remaining_gap, 1)
    return recs


def _markdown_recommendation(store_id, product, projected_waste, expiries, now, end, forecast, data_used):
    qty = floor(projected_waste)
    first_expiry = min((deadline for deadline, _ in expiries), default=end)
    data = dict(data_used)
    data["报损时点"] = [iso(deadline) for deadline, _ in expiries]
    data["假设"] = "降价后可在安全窗口内售完"
    cost = {
        "降价单价": product.markdown_price,
        "营收损失": round((product.price - product.markdown_price) * qty, 2),
        "避免报损成本": round(product.cost * qty, 2),
    }
    return Recommendation(
        uuid4().hex, store_id, product.product_id, "降价", qty, now, first_expiry,
        forecast.version, data, cost)


def _stop_recommendation(store_id, product, planned, projected_waste, now, end, forecast, data_used):
    qty = min(sum(b.quantity for b in planned), floor(projected_waste))
    targets = []
    left = qty
    for batch in sorted(planned, key=lambda b: b.produced_at, reverse=True):
        take = min(left, batch.quantity)
        targets.append({"batch_id": batch.batch_id, "quantity": take})
        left -= take
        if left <= 0:
            break
    data = dict(data_used)
    data["取消明细"] = targets
    cost = {
        "避免报损成本": round(product.cost * qty, 2),
        "涉及批次": [t["batch_id"] for t in targets],
    }
    return Recommendation(
        uuid4().hex, store_id, product.product_id, "停止制作", qty, now, end,
        forecast.version, data, cost)


def build_trace(repo, recommendation_id):
    """追溯链：建议 → 需求版本与输入 → 实际执行 → 售罄时间 → 最终报损。"""
    rec = repo.recommendations.get(recommendation_id)
    if rec is None:
        raise DomainError(f"建议不存在：{recommendation_id}")
    product = repo.products[rec.product_id]
    day_end = rec.created_at.replace(hour=23, minute=59, second=59, microsecond=0)
    sellout_at = repo.ledger.first_sellout(
        repo.batches, rec.store_id, rec.product_id, rec.created_at, day_end)
    waste = repo.ledger.waste_summary(
        rec.store_id, rec.product_id, rec.created_at, day_end, product.cost)
    return {
        "recommendation": rec.to_dict(),
        "demand_version": rec.demand_version,
        "executions": [e.to_dict() for e in repo.executions.get(recommendation_id, [])],
        "sellout_at": iso(sellout_at) if sellout_at else None,
        "waste": waste,
    }
