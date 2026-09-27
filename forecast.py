"""需求版本：把分时销量、闭店区间、在店库存固化为一个不可变的需求快照。

每次生成产生新版本（FC-V1、FC-V2…），版本一旦发布不修改；
晚到事件只会体现在下一个版本中，已发布版本与基于它的建议保持原样。
"""

from collections import defaultdict
from datetime import timedelta

from config import DEMAND_DECAY, DEMAND_DECAY_DEFAULT
from timeutil import fmt, parse


class ForecastStore:
    def __init__(self):
        self._versions = []
        self._seq = 0

    def generate(self, inventory, store_id, sku, day_start, horizon_end, note=""):
        """基于当前事件水位生成需求版本。

        分时销量取 [day_start, 生成时刻]；剩余需求从生成时刻外推到 horizon_end，
        按日内衰减曲线折算，闭店区间置 0 并标注数据缺口。
        """
        self._seq += 1
        version_id = f"FC-V{self._seq}"
        generated_at = inventory.watermark["occurred_at"] if inventory.watermark else day_start

        # 分时销量聚合（小时桶）；仅正价销量作为鲜品需求信号，
        # 降价/清仓销量反映的是残值出清，不外推为次日式需求
        hourly = defaultdict(int)
        for s in inventory.sales:
            if (s["store_id"] == store_id and s["sku"] == sku
                    and s["at"] <= generated_at and s["price_level"] == "regular"):
                hourly[s["at"][:13]] += s["qty"]

        # 闭店区间与数据缺口
        closures = [c for c in inventory.closures if c["store_id"] == store_id
                    and c["start"] < horizon_end and c["end"] > generated_at]
        gaps = [{"start": c["start"], "end": c["end"], "reason": c["reason"]} for c in closures]

        # 剩余时段外推：已售小时均值 × 日内衰减，闭店时段置 0
        sold_hours = [h for h in hourly if day_start[:13] <= h]
        avg_per_hour = (sum(hourly[h] for h in sold_hours) / len(sold_hours)) if sold_hours else 0.0
        remaining = []
        cursor = parse(generated_at).replace(minute=0, second=0) + timedelta(hours=1)
        end = parse(horizon_end)
        while cursor < end:
            h = fmt(cursor)
            hour_end = fmt(cursor + timedelta(hours=1))
            in_closure = any(g["start"] < hour_end and g["end"] > h for g in gaps)
            decay = DEMAND_DECAY.get(cursor.hour, DEMAND_DECAY_DEFAULT)
            remaining.append({
                "hour": h[:13],
                "expected_qty": 0 if in_closure else round(avg_per_hour * decay, 2),
                "closed": in_closure,
                "decay": decay,
            })
            cursor += timedelta(hours=1)

        version = {
            "version_id": version_id,
            "store_id": store_id,
            "sku": sku,
            "generated_at": generated_at,
            "horizon": {"start": generated_at, "end": horizon_end},
            "inputs": {
                "event_watermark": dict(inventory.watermark) if inventory.watermark else None,
                "sales_window": {"start": day_start, "end": generated_at},
                "hourly_sales": dict(sorted(hourly.items())),
                "closures": gaps,
                "data_gaps": [f"{g['start']}~{g['end']} {g['reason']}，该时段无销量数据" for g in gaps],
            },
            "hourly_sales": dict(sorted(hourly.items())),
            "remaining_demand": remaining,
            "expected_remaining_qty": round(sum(r["expected_qty"] for r in remaining), 2),
            "note": note,
        }
        self._versions.append(version)
        return version

    def list(self, store_id=None, sku=None):
        return [v for v in self._versions
                if (store_id is None or v["store_id"] == store_id)
                and (sku is None or v["sku"] == sku)]

    def get(self, version_id):
        for v in self._versions:
            if v["version_id"] == version_id:
                return v
        return None

    def latest(self, store_id, sku):
        versions = self.list(store_id, sku)
        return versions[-1] if versions else None
