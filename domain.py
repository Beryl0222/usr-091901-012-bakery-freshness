"""烘焙鲜度领域模型：产品、配方版本、批次与保质窗口。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

STATES = ["计划生产", "制作中", "可售", "临期", "停止销售", "已报损"]
ACTIONS = ["现烤", "调拨", "降价", "停止制作"]

SOURCE_BAKE = "现烤"
SOURCE_CENTRAL = "中央工厂"
SOURCE_TRANSFER = "调拨"
SOURCES = [SOURCE_BAKE, SOURCE_CENTRAL, SOURCE_TRANSFER]


class DomainError(ValueError):
    """业务规则校验失败。"""


class NotFoundError(DomainError):
    """引用的资源不存在。"""


class SafetyWindowViolation(DomainError):
    """超过安全窗口的商品不得恢复为可售。"""


def parse_ts(value):
    """解析 ISO-8601 时间；缺省时区按 UTC 处理。"""
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def iso(moment):
    """统一输出 UTC ISO 字符串。"""
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def utcnow():
    return datetime.now(timezone.utc)


@dataclass
class RecipeVersion:
    """配方版本：营养与过敏原随生效时间切换，被生产批次固定引用。"""

    product_id: str
    version: int
    effective_from: datetime
    nutrition: dict
    allergens: list

    def label(self):
        return {
            "product_id": self.product_id,
            "recipe_version": self.version,
            "nutrition": self.nutrition,
            "allergens": self.allergens,
        }


@dataclass
class Product:
    product_id: str
    name: str
    category: str
    price: float
    cost: float
    shelf_life_hours: float
    safety_window_hours: float
    near_expiry_after_hours: float
    bakeable: bool = True
    markdown_price: float | None = None
    transfer_fee: float = 0.0

    def __post_init__(self):
        if not 0 < self.safety_window_hours <= self.shelf_life_hours:
            raise DomainError("安全窗口必须大于 0 且不超过保质时长")
        if not 0 <= self.near_expiry_after_hours <= self.safety_window_hours:
            raise DomainError("临期起点不能晚于安全窗口")
        if self.price < 0 or self.cost < 0:
            raise DomainError("价格与成本不能为负")
        if self.markdown_price is None:
            self.markdown_price = round(self.price * 0.7, 2)
        if not 0 <= self.markdown_price <= self.price:
            raise DomainError("降价金额必须介于 0 与售价之间")

    @property
    def unit_margin(self):
        return round(self.price - self.cost, 2)


@dataclass
class Batch:
    """生产/到货批次：保质窗口随出炉时间固定，状态按时间推导。"""

    batch_id: str
    store_id: str
    product_id: str
    source: str
    recipe_version: int
    quantity: int
    produced_at: datetime
    sellable_from: datetime
    near_expiry_at: datetime
    safety_until: datetime
    expires_at: datetime
    started: bool = False

    @classmethod
    def build(cls, batch_id, store_id, product, quantity, produced_at, source, recipe_version):
        produced = parse_ts(produced_at)
        if source not in SOURCES:
            raise DomainError(f"未知批次来源：{source}")
        if int(quantity) <= 0:
            raise DomainError("批次数量必须为正整数")
        return cls(
            batch_id=batch_id,
            store_id=store_id,
            product_id=product.product_id,
            source=source,
            recipe_version=recipe_version,
            quantity=int(quantity),
            produced_at=produced,
            sellable_from=produced,
            near_expiry_at=produced + timedelta(hours=product.near_expiry_after_hours),
            safety_until=produced + timedelta(hours=product.safety_window_hours),
            expires_at=produced + timedelta(hours=product.shelf_life_hours),
        )

    def state_at(self, received, wasted, moment):
        """按发生时间推导批次状态；越过安全窗口只会走向停止销售/已报损。"""
        if received <= 0:
            return "制作中" if self.started else "计划生产"
        if moment >= self.safety_until:
            return "已报损" if wasted >= received else "停止销售"
        if wasted >= received:
            return "已报损"
        if moment >= self.near_expiry_at:
            return "临期"
        return "可售"

    def ensure_relistable(self, moment):
        if moment >= self.safety_until:
            raise SafetyWindowViolation(f"批次 {self.batch_id} 已超过安全窗口，不得恢复为可售")

    def to_dict(self, received=0, remaining=0, wasted=0, moment=None):
        moment = moment or utcnow()
        return {
            "batch_id": self.batch_id,
            "store_id": self.store_id,
            "product_id": self.product_id,
            "source": self.source,
            "recipe_version": self.recipe_version,
            "planned_quantity": self.quantity,
            "received": received,
            "remaining": remaining,
            "wasted": wasted,
            "state": self.state_at(received, wasted, moment),
            "produced_at": iso(self.produced_at),
            "sellable_from": iso(self.sellable_from),
            "near_expiry_at": iso(self.near_expiry_at),
            "safety_until": iso(self.safety_until),
            "expires_at": iso(self.expires_at),
        }
