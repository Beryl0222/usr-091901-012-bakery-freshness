"""应用服务：把领域模型、台账与决策引擎组装成用例。"""

from __future__ import annotations

from collections import defaultdict
from datetime import timedelta
from threading import RLock
from uuid import uuid4

import decisions
from analytics import stockout_waste_report
from decisions import Execution, Forecast
from domain import (ACTIONS, SOURCES, SOURCE_BAKE, Batch, DomainError, NotFoundError,
                    Product, RecipeVersion, iso, parse_ts, utcnow)
from ledger import (CLEARANCE, DELIVERY_FAILED, EVENT_TYPES, MARKDOWN,
                    PRODUCTION_CANCELLED, RECEIVE, RELIST, RESERVATION,
                    RESERVATION_CANCELLED, SALE, STOCKOUT, STORE_CLOSED,
                    STORE_REOPENED, TRANSFER_OUT, WASTE, Event, Ledger)

# 允许通过 /events 直接写入的事件类型；其余走专用入口
_PUBLIC_EVENT_TYPES = {
    SALE, RESERVATION, RESERVATION_CANCELLED, DELIVERY_FAILED,
    STORE_CLOSED, STORE_REOPENED, CLEARANCE, WASTE, TRANSFER_OUT,
    MARKDOWN, RELIST,
}


class Repository:
    """内存仓储：进程内唯一事实来源，重启后需重新灌入数据。"""

    def __init__(self):
        self.lock = RLock()
        self.products = {}
        self.recipes = defaultdict(list)
        self.batches = {}
        self.orders = {}
        self.forecasts = defaultdict(list)
        self.capacity = {}
        self.recommendations = {}
        self.executions = defaultdict(list)
        self.ledger = Ledger()
        self._stores = set()

    def store_ids(self):
        return set(self._stores)

    def touch_store(self, store_id):
        self._stores.add(store_id)

    def recipe_at(self, product_id, when):
        """生效时间 <= when 的最新配方版本。"""
        current = None
        for version in sorted(self.recipes.get(product_id, []), key=lambda v: v.effective_from):
            if version.effective_from <= when:
                current = version
        if current is None:
            raise DomainError(f"商品 {product_id} 在 {iso(when)} 没有生效的配方")
        return current


def product_dict(product):
    return {
        "product_id": product.product_id,
        "name": product.name,
        "category": product.category,
        "price": product.price,
        "cost": product.cost,
        "unit_margin": product.unit_margin,
        "shelf_life_hours": product.shelf_life_hours,
        "safety_window_hours": product.safety_window_hours,
        "near_expiry_after_hours": product.near_expiry_after_hours,
        "bakeable": product.bakeable,
        "markdown_price": product.markdown_price,
        "transfer_fee": product.transfer_fee,
    }


class BakeryService:
    """烘焙鲜度与补货用例入口；所有写操作在仓储锁内完成。"""

    def __init__(self, repo=None):
        self.repo = repo or Repository()

    # ---- 商品与配方 ----

    def register_product(self, data):
        with self.repo.lock:
            markdown = data.get("markdown_price")
            product = Product(
                product_id=data["product_id"],
                name=data["name"],
                category=data.get("category", "其他"),
                price=float(data["price"]),
                cost=float(data["cost"]),
                shelf_life_hours=float(data["shelf_life_hours"]),
                safety_window_hours=float(data["safety_window_hours"]),
                near_expiry_after_hours=float(data["near_expiry_after_hours"]),
                bakeable=bool(data.get("bakeable", True)),
                markdown_price=float(markdown) if markdown is not None else None,
                transfer_fee=float(data.get("transfer_fee", 0.0)),
            )
            if product.product_id in self.repo.products:
                raise DomainError(f"商品已存在：{product.product_id}")
            self.repo.products[product.product_id] = product
            return product_dict(product)

    def list_products(self):
        return [product_dict(p) for p in self.repo.products.values()]

    def add_recipe(self, product_id, data):
        with self.repo.lock:
            self._product(product_id)
            version = RecipeVersion(
                product_id=product_id,
                version=int(data["version"]),
                effective_from=parse_ts(data["effective_from"]),
                nutrition=data.get("nutrition", {}),
                allergens=list(data.get("allergens", [])),
            )
            versions = self.repo.recipes[product_id]
            if any(v.version == version.version for v in versions):
                raise DomainError(f"配方版本已存在：{product_id} v{version.version}")
            versions.append(version)
            result = version.label()
            result["effective_from"] = iso(version.effective_from)
            return result

    def label_at(self, product_id, at):
        with self.repo.lock:
            self._product(product_id)
            moment = parse_ts(at) if at else utcnow()
            version = self.repo.recipe_at(product_id, moment)
            result = version.label()
            result["effective_from"] = iso(version.effective_from)
            result["queried_at"] = iso(moment)
            return result

    # ---- 产能与预测 ----

    def set_capacity(self, store_id, data):
        with self.repo.lock:
            oven = {pid: float(rate) for pid, rate in (data.get("oven") or {}).items()}
            window = data.get("baking_hours") or [0, 24]
            if len(window) != 2 or not 0 <= window[0] < window[1] <= 24:
                raise DomainError("baking_hours 必须是 [开始小时, 结束小时]")
            self.repo.capacity[store_id] = {"oven": oven, "baking_hours": window}
            self.repo.touch_store(store_id)
            return {"store_id": store_id, **self.repo.capacity[store_id]}

    def register_forecast(self, data):
        with self.repo.lock:
            store_id = data["store_id"]
            hourly = {}
            for pid, values in (data.get("hourly") or {}).items():
                values = [float(v) for v in values]
                if len(values) != 24 or any(v < 0 for v in values):
                    raise DomainError(f"商品 {pid} 的分时需求必须是 24 个非负数")
                hourly[pid] = values
            if not hourly:
                raise DomainError("分时需求不能为空")
            forecast = Forecast(
                store_id=store_id,
                version=str(data["version"]),
                hourly=hourly,
                created_at=parse_ts(data["created_at"]) if data.get("created_at") else utcnow(),
            )
            self.repo.forecasts[store_id].append(forecast)
            self.repo.touch_store(store_id)
            return forecast.to_dict()

    # ---- 批次 ----

    def plan_batch(self, data):
        """计划生产：现烤排产或中央工厂发货计划。"""
        with self.repo.lock:
            product = self._product(data["product_id"])
            store_id = data["store_id"]
            ready_at = parse_ts(data["ready_at"])
            recipe = self.repo.recipe_at(product.product_id, ready_at)
            batch = Batch.build(
                batch_id=data.get("batch_id") or uuid4().hex,
                store_id=store_id,
                product=product,
                quantity=data["quantity"],
                produced_at=ready_at,
                source=data.get("source", SOURCE_BAKE),
                recipe_version=recipe.version,
            )
            if batch.batch_id in self.repo.batches:
                raise DomainError(f"批次已存在：{batch.batch_id}")
            self.repo.batches[batch.batch_id] = batch
            self.repo.touch_store(store_id)
            return batch.to_dict()

    def start_batch(self, batch_id, data):
        with self.repo.lock:
            batch = self._batch(batch_id)
            if batch.started:
                raise DomainError(f"批次 {batch_id} 已开工")
            batch.started = True
            return batch.to_dict(moment=parse_ts(data.get("occurred_at")) if data.get("occurred_at") else utcnow())

    def receive_batch(self, data):
        """扫码入库：同一 scan_id 重复提交直接返回原记录，不重复计数。"""
        with self.repo.lock:
            scan_id = data.get("scan_id")
            if not scan_id:
                raise DomainError("缺少 scan_id")
            occurred_at = parse_ts(data["occurred_at"]) if data.get("occurred_at") else utcnow()
            existing = self.repo.ledger.find_by_scan(scan_id)
            if data.get("event_id"):
                existing = existing or self.repo.ledger.get(data["event_id"])
            if existing is not None:
                return self._duplicate_result(existing, occurred_at)
            quantity = int(data["quantity"])
            if data.get("batch_id") and data["batch_id"] in self.repo.batches:
                batch = self.repo.batches[data["batch_id"]]
                if data.get("produced_at"):
                    self._rebuild_windows(batch, data["produced_at"])
            else:
                product = self._product(data["product_id"])
                produced_at = parse_ts(data["produced_at"]) if data.get("produced_at") else occurred_at
                recipe = self.repo.recipe_at(product.product_id, produced_at)
                batch = Batch.build(
                    batch_id=data.get("batch_id") or uuid4().hex,
                    store_id=data["store_id"],
                    product=product,
                    quantity=quantity,
                    produced_at=produced_at,
                    source=data.get("source", "中央工厂"),
                    recipe_version=recipe.version,
                )
                if batch.batch_id in self.repo.batches:
                    raise DomainError(f"批次已存在：{batch.batch_id}")
                self.repo.batches[batch.batch_id] = batch
            self.repo.touch_store(batch.store_id)
            event = Event(
                event_id=data.get("event_id") or uuid4().hex,
                type=RECEIVE,
                store_id=batch.store_id,
                product_id=batch.product_id,
                quantity=quantity,
                occurred_at=occurred_at,
                batch_id=batch.batch_id,
                scan_id=scan_id,
            )
            self.repo.ledger.append(event)
            return {
                "recorded": True,
                "duplicate": False,
                "event": event.to_dict(),
                "batch": self.batch_view(batch.batch_id, occurred_at),
            }

    def batch_view(self, batch_id, at=None):
        with self.repo.lock:
            batch = self._batch(batch_id)
            moment = parse_ts(at) if at else utcnow()
            remaining, received, wasted, _ = self.repo.ledger.project_stock(
                self.repo.batches, batch.store_id, batch.product_id, moment)
            return batch.to_dict(
                received=received.get(batch_id, 0),
                remaining=remaining.get(batch_id, 0),
                wasted=wasted.get(batch_id, 0),
                moment=moment,
            )

    # ---- 事件 ----

    def record_event(self, data):
        """写入业务事件；校验按事件发生时刻的可售量进行。"""
        kind = data.get("type")
        if kind not in EVENT_TYPES or kind not in _PUBLIC_EVENT_TYPES:
            raise DomainError(f"不支持的事件类型：{kind}")
        with self.repo.lock:
            if data.get("event_id"):
                existing = self.repo.ledger.get(data["event_id"])
                if existing is not None:
                    return {"recorded": False, "duplicate": True, "event": existing.to_dict()}
            store_id = data["store_id"]
            occurred_at = parse_ts(data["occurred_at"]) if data.get("occurred_at") else utcnow()
            product_id = data.get("product_id")
            quantity = int(data.get("quantity") or 0)
            event = Event(
                event_id=data.get("event_id") or uuid4().hex,
                type=kind,
                store_id=store_id,
                product_id=product_id,
                quantity=quantity,
                occurred_at=occurred_at,
                batch_id=data.get("batch_id"),
                order_id=data.get("order_id"),
                payload=dict(data.get("payload") or {}),
            )
            handler = getattr(self, f"_on_{kind}")
            extra = handler(event)
            self.repo.ledger.append(event)
            self.repo.touch_store(store_id)
            result = {"recorded": True, "duplicate": False, "event": event.to_dict()}
            if extra:
                result.update(extra)
            if product_id:
                result["inventory"] = self.repo.ledger.inventory_view(
                    self.repo.batches, store_id, product_id, occurred_at)
            return result

    def _on_sale(self, event):
        self._product(event.product_id)
        if event.quantity <= 0:
            raise DomainError("销售数量必须为正整数")
        view = self.repo.ledger.inventory_view(
            self.repo.batches, event.store_id, event.product_id, event.occurred_at)
        own_reserved = view["reserved_detail"].get(event.order_id, 0)
        available = view["sellable"] + own_reserved
        requested = event.quantity
        fulfilled = min(requested, available)
        allocations = self.repo.ledger.allocate_preview(
            self.repo.batches, event.store_id, event.product_id, fulfilled, event.occurred_at)
        is_new_order = event.order_id is None or event.order_id not in self.repo.orders
        event.quantity = fulfilled
        order = self._open_order(event, "已售")
        order["allocations"] = [
            {"batch_id": bid, "quantity": qty,
             "recipe_version": self.repo.batches[bid].recipe_version}
            for bid, qty in allocations
        ]
        if is_new_order and allocations:
            order["label"] = self._label_from_allocations(order, allocations)
        order["status"] = "已售"
        short = requested - fulfilled
        if short > 0:
            self.repo.ledger.append(Event(
                event_id=uuid4().hex, type=STOCKOUT, store_id=event.store_id,
                product_id=event.product_id, quantity=short,
                occurred_at=event.occurred_at, order_id=event.order_id,
                payload={"reason": "可售不足"},
            ))
        return {"fulfilled": fulfilled, "stockout": short}

    def _on_reservation(self, event):
        self._product(event.product_id)
        if event.quantity <= 0 or not event.order_id:
            raise DomainError("预订需要正整数数量与 order_id")
        view = self.repo.ledger.inventory_view(
            self.repo.batches, event.store_id, event.product_id, event.occurred_at)
        if event.quantity > view["sellable"]:
            self.repo.ledger.append(Event(
                event_id=uuid4().hex, type=STOCKOUT, store_id=event.store_id,
                product_id=event.product_id, quantity=event.quantity,
                occurred_at=event.occurred_at, order_id=event.order_id,
                payload={"reason": "预订超出可售"},
            ))
            raise DomainError("可售不足，预订失败")
        order = self._open_order(event, "预订")
        order["status"] = "预订"

    def _on_reservation_cancelled(self, event):
        order = self._order(event.order_id)
        view = self.repo.ledger.inventory_view(
            self.repo.batches, event.store_id, event.product_id, event.occurred_at)
        open_qty = view["reserved_detail"].get(event.order_id, 0)
        if event.quantity <= 0 or event.quantity > open_qty:
            raise DomainError(f"订单 {event.order_id} 可取消的预订量不足")
        if event.quantity == open_qty:
            order["status"] = "已取消"

    def _on_delivery_failed(self, event):
        self._product(event.product_id)
        if event.quantity <= 0:
            raise DomainError("配送失败数量必须为正整数")
        event.payload["return_to"] = self._return_targets(event)
        if event.order_id and event.order_id in self.repo.orders:
            self.repo.orders[event.order_id]["status"] = "配送失败"

    def _on_store_closed(self, event):
        return None

    def _on_store_reopened(self, event):
        return None

    def _on_clearance(self, event):
        product = self._product(event.product_id)
        if event.quantity <= 0:
            raise DomainError("清仓数量必须为正整数")
        view = self.repo.ledger.inventory_view(
            self.repo.batches, event.store_id, event.product_id, event.occurred_at)
        if event.quantity > view["sellable"]:
            raise DomainError("清仓数量超过可售量")
        event.payload.setdefault("price", product.markdown_price)

    def _on_waste(self, event):
        self._product(event.product_id)
        if event.quantity <= 0:
            raise DomainError("报损数量必须为正整数")
        view = self.repo.ledger.inventory_view(
            self.repo.batches, event.store_id, event.product_id, event.occurred_at)
        if event.quantity > view["on_hand"]:
            raise DomainError("报损数量超过在库量")

    def _on_transfer_out(self, event):
        self._product(event.product_id)
        if event.quantity <= 0:
            raise DomainError("调拨数量必须为正整数")
        view = self.repo.ledger.inventory_view(
            self.repo.batches, event.store_id, event.product_id, event.occurred_at)
        if event.quantity > view["sellable"]:
            raise DomainError("调出数量超过可售量，过期商品不得调拨")

    def _on_markdown(self, event):
        product = self._product(event.product_id)
        price = event.payload.get("price")
        if price is None or not 0 <= float(price) < product.price:
            raise DomainError("降价金额必须低于售价且不为负")
        event.payload["price"] = float(price)

    def _on_relist(self, event):
        batch = self._batch(event.batch_id)
        batch.ensure_relistable(event.occurred_at)

    # ---- 库存视图 ----

    def inventory(self, store_id, product_id, at=None):
        with self.repo.lock:
            product = self._product(product_id)
            moment = parse_ts(at) if at else utcnow()
            view = self.repo.ledger.inventory_view(self.repo.batches, store_id, product_id, moment)
            view["name"] = product.name
            return view

    # ---- 决策与追溯 ----

    def generate(self, store_id, at=None, horizon_hours=14):
        with self.repo.lock:
            moment = parse_ts(at) if at else utcnow()
            recs = decisions.generate_recommendations(
                self.repo, store_id, moment, int(horizon_hours))
            return [rec.to_dict() for rec in recs]

    def list_recommendations(self, store_id=None, status=None):
        with self.repo.lock:
            result = []
            for rec in self.repo.recommendations.values():
                if store_id and rec.store_id != store_id:
                    continue
                if status and rec.status != status:
                    continue
                result.append(rec.to_dict())
            result.sort(key=lambda r: r["created_at"])
            return result

    def execute(self, recommendation_id, data):
        with self.repo.lock:
            rec = self.repo.recommendations.get(recommendation_id)
            if rec is None:
                raise NotFoundError(f"建议不存在：{recommendation_id}")
            if rec.status != "待执行":
                raise DomainError(f"建议已处理（{rec.status}），不能重复执行")
            actor = data.get("actor") or "系统"
            at = parse_ts(data["occurred_at"]) if data.get("occurred_at") else utcnow()
            qty = int(data.get("actual_quantity") or rec.quantity)
            if qty <= 0:
                raise DomainError("执行数量必须为正整数")
            note = self._apply_execution(rec, qty, at)
            rec.status = "已执行"
            execution = Execution(uuid4().hex, recommendation_id, actor, at, qty, note)
            self.repo.executions[recommendation_id].append(execution)
            return {"recommendation": rec.to_dict(), "execution": execution.to_dict()}

    def dismiss(self, recommendation_id):
        with self.repo.lock:
            rec = self.repo.recommendations.get(recommendation_id)
            if rec is None:
                raise NotFoundError(f"建议不存在：{recommendation_id}")
            if rec.status != "待执行":
                raise DomainError(f"建议已处理（{rec.status}）")
            rec.status = "已忽略"
            return rec.to_dict()

    def trace(self, recommendation_id):
        with self.repo.lock:
            if recommendation_id not in self.repo.recommendations:
                raise NotFoundError(f"建议不存在：{recommendation_id}")
            return decisions.build_trace(self.repo, recommendation_id)

    # ---- 订单与分析 ----

    def order_label(self, order_id):
        with self.repo.lock:
            order = self._order(order_id)
            return {
                "order_id": order["order_id"],
                "store_id": order["store_id"],
                "product_id": order["product_id"],
                "quantity": order["quantity"],
                "status": order["status"],
                "purchased_at": iso(order["ordered_at"]),
                "label": order["label"],
                "allocations": order["allocations"],
                "说明": "营养与过敏原按购买时标签解释，不随后续配方调整变化",
            }

    def stockout_waste(self, store_id=None, start=None, end=None):
        with self.repo.lock:
            start_ts = parse_ts(start) if start else utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            end_ts = parse_ts(end) if end else utcnow()
            return stockout_waste_report(self.repo, store_id, start_ts, end_ts)

    # ---- 内部 ----

    def _product(self, product_id):
        product = self.repo.products.get(product_id)
        if product is None:
            raise NotFoundError(f"商品不存在：{product_id}")
        return product

    def _batch(self, batch_id):
        batch = self.repo.batches.get(batch_id)
        if batch is None:
            raise NotFoundError(f"批次不存在：{batch_id}")
        return batch

    def _order(self, order_id):
        order = self.repo.orders.get(order_id)
        if order is None:
            raise NotFoundError(f"订单不存在：{order_id}")
        return order

    def _open_order(self, event, status):
        """按购买时刻快照配方标签；已有订单保留原标签。"""
        order = self.repo.orders.get(event.order_id)
        if order is None:
            recipe = self.repo.recipe_at(event.product_id, event.occurred_at)
            order = {
                "order_id": event.order_id or uuid4().hex,
                "store_id": event.store_id,
                "product_id": event.product_id,
                "quantity": event.quantity,
                "ordered_at": event.occurred_at,
                "status": status,
                "label": recipe.label(),
                "allocations": [],
            }
            self.repo.orders[order["order_id"]] = order
        return order

    def _label_from_allocations(self, order, allocations):
        """销售履约时以实际批次的配方版本为准(多数批次取数量最大者)。"""
        main = max(allocations, key=lambda item: item[1])
        batch = self.repo.batches[main[0]]
        for version in self.repo.recipes.get(order["product_id"], []):
            if version.product_id == batch.product_id and version.version == batch.recipe_version:
                return version.label()
        return order["label"]

    def _return_targets(self, event):
        """配送失败退回：优先退回原订单履约批次，否则退回最新批次。"""
        order = self.repo.orders.get(event.order_id) if event.order_id else None
        if order and order["allocations"]:
            targets = []
            left = event.quantity
            for alloc in order["allocations"]:
                take = min(left, alloc["quantity"])
                targets.append([alloc["batch_id"], take])
                left -= take
                if left <= 0:
                    break
            if left > 0:
                targets[-1][1] += left
            return targets
        if event.batch_id:
            self._batch(event.batch_id)
            return [[event.batch_id, event.quantity]]
        candidates = [b for b in self.repo.batches.values()
                      if b.store_id == event.store_id and b.product_id == event.product_id]
        if not candidates:
            raise DomainError("没有可退回的批次")
        newest = max(candidates, key=lambda b: b.produced_at)
        return [[newest.batch_id, event.quantity]]

    def _rebuild_windows(self, batch, produced_at):
        """计划批次实际出炉时间更新后，按商品窗口重算时点，配方版本保持不变。"""
        product = self._product(batch.product_id)
        rebuilt = Batch.build(batch.batch_id, batch.store_id, product, batch.quantity,
                              produced_at, batch.source, batch.recipe_version)
        batch.produced_at = rebuilt.produced_at
        batch.sellable_from = rebuilt.sellable_from
        batch.near_expiry_at = rebuilt.near_expiry_at
        batch.safety_until = rebuilt.safety_until
        batch.expires_at = rebuilt.expires_at

    def _duplicate_result(self, existing, at):
        result = {"recorded": False, "duplicate": True, "event": existing.to_dict()}
        if existing.product_id:
            result["inventory"] = self.repo.ledger.inventory_view(
                self.repo.batches, existing.store_id, existing.product_id, at)
        return result

    def _apply_execution(self, rec, qty, at):
        if rec.action == "现烤":
            ready_at = at + timedelta(hours=1)
            batch = self.plan_batch({
                "store_id": rec.store_id,
                "product_id": rec.product_id,
                "quantity": qty,
                "ready_at": iso(ready_at),
                "source": SOURCE_BAKE,
            })
            return f"已生成现烤批次 {batch['batch_id']}，预计 {batch['produced_at']} 出炉"
        if rec.action == "停止制作":
            for item in rec.data_used.get("取消明细", []):
                batch = self.repo.batches[item["batch_id"]]
                take = min(item["quantity"], batch.quantity)
                if take <= 0:
                    continue
                batch.quantity -= take
                self.repo.ledger.append(Event(
                    event_id=uuid4().hex, type=PRODUCTION_CANCELLED,
                    store_id=rec.store_id, product_id=rec.product_id,
                    quantity=take, occurred_at=at, batch_id=batch.batch_id,
                ))
            return "已取消相应计划产量"
        if rec.action == "降价":
            product = self._product(rec.product_id)
            self.repo.ledger.append(Event(
                event_id=uuid4().hex, type=MARKDOWN,
                store_id=rec.store_id, product_id=rec.product_id,
                quantity=qty, occurred_at=at,
                payload={"price": product.markdown_price},
            ))
            return f"已按 {product.markdown_price} 降价"
        if rec.action == "调拨":
            left = qty
            for donor in rec.data_used.get("调拨来源", []):
                view = self.repo.ledger.inventory_view(
                    self.repo.batches, donor["store_id"], rec.product_id, at)
                take = min(left, donor["可调拨"], view["sellable"])
                if take <= 0:
                    continue
                self.repo.ledger.append(Event(
                    event_id=uuid4().hex, type=TRANSFER_OUT,
                    store_id=donor["store_id"], product_id=rec.product_id,
                    quantity=take, occurred_at=at,
                    payload={"destination": rec.store_id},
                ))
                left -= take
            if left > 0:
                raise DomainError("调拨来源可售不足，执行失败")
            return "调出完成，待收货门店扫码入库"
        raise DomainError(f"未知建议动作：{rec.action}")
