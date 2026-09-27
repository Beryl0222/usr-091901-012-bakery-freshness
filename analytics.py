"""区域视角：缺货与损耗对比分析。"""

from __future__ import annotations

from ledger import CLEARANCE, SALE, STOCKOUT, WASTE


def stockout_waste_report(repo, store_id, start, end):
    """按门店×商品汇总销售、缺货与报损，便于区域负责人横向比较。"""
    store_ids = [store_id] if store_id else sorted(repo.store_ids())
    totals = {"销售量": 0, "销售额": 0.0, "缺货量": 0, "缺货损失": 0.0,
              "报损量": 0, "报损成本": 0.0, "清仓量": 0, "清仓营收": 0.0}
    stores = []
    for sid in store_ids:
        rows = []
        for pid, product in repo.products.items():
            sales = repo.ledger.events_for(sid, pid, types={SALE}, start=start, until=end)
            stockouts = repo.ledger.events_for(sid, pid, types={STOCKOUT}, start=start, until=end)
            wastes = repo.ledger.events_for(sid, pid, types={WASTE}, start=start, until=end)
            clearances = repo.ledger.events_for(sid, pid, types={CLEARANCE}, start=start, until=end)
            sold_units = sum(e.quantity for e in sales)
            stockout_units = sum(e.quantity for e in stockouts)
            waste_units = sum(e.quantity for e in wastes)
            clearance_units = sum(e.quantity for e in clearances)
            row = {
                "product_id": pid,
                "name": product.name,
                "销售量": sold_units,
                "销售额": round(sold_units * product.price, 2),
                "缺货量": stockout_units,
                "缺货损失": round(stockout_units * product.unit_margin, 2),
                "报损量": waste_units,
                "报损成本": round(waste_units * product.cost, 2),
                "清仓量": clearance_units,
                "清仓营收": round(sum(e.quantity * e.payload.get("price", 0) for e in clearances), 2),
            }
            rows.append(row)
            for key in totals:
                totals[key] += row[key]
        stores.append({"store_id": sid, "products": rows})
    totals = {key: round(value, 2) for key, value in totals.items()}
    comparison = {
        "缺货损失": totals["缺货损失"],
        "报损成本": totals["报损成本"],
        "损耗缺货比": round(totals["报损成本"] / totals["缺货损失"], 2) if totals["缺货损失"] else None,
    }
    return {
        "from": start.isoformat(),
        "to": end.isoformat(),
        "stores": stores,
        "totals": totals,
        "缺货与损耗对比": comparison,
    }
