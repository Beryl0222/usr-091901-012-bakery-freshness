"""端到端演示：2026-09-27 早高峰，从事件到建议、执行、追溯的完整链路。

运行：python3 demo.py
"""

import json

from seed import build


def show(title, payload):
    print(f"\n=== {title} ===")
    print(json.dumps(payload, ensure_ascii=False, indent=1))


def main():
    result = build()
    app = result["app"]
    rec_ids = result["rec_ids"]

    print("=== 2026-09-27 烘焙鲜度与补货决策演示 ===")
    print(f"事件总数 {len(app.events)}；重复扫码被幂等拦截: {result['duplicate_scan']['duplicate']}")

    inv = app.inventory()
    show("07:45 库存快照（SH-01，按发生时间投影）", [
        {k: v for k, v in b.items() if k != "pools"} | {"pools": {p: q for p, q in b["pools"].items() if q}}
        for b in app.stock("SH-01", at="2026-09-27T07:45:00")["batches"]
    ])

    show("需求版本 FC-V1（低糖全麦贝果）", {
        "version_id": result["forecasts"]["bagel"]["version_id"],
        "hourly_sales": result["forecasts"]["bagel"]["hourly_sales"],
        "expected_remaining_qty": result["forecasts"]["bagel"]["expected_remaining_qty"],
        "data_gaps": result["forecasts"]["bagel"]["inputs"]["data_gaps"],
    })

    show("补货建议（含数据版本与成本）", [
        {
            "rec_id": r["rec_id"], "type": r["type"], "sku": r["sku"], "qty": r["qty"],
            "reason": r["reason"],
            "forecast_version": r["evidence"]["forecast_version_id"],
            "costs": r["evidence"]["costs"],
            "expected_net_benefit": r["expected_net_benefit"],
        }
        for r in result["recommendations"]
    ])

    show("配送失败拦截（SO-7782 可颂，07:42 带回已过安全窗口）", [
        i for i in inv.interceptions if i.get("order_id") == "SO-7782"
    ])

    show("订单标签：SO-7790（07:55 下单，配方 v2 08:00 才生效 → 仍按 v1 解释）",
         app.order_label("SO-7790"))

    show("新批次 BAGEL-SH01-B（08:10 现烤 → 固定 v2 配方）",
         inv.batch_view(inv.batches["BAGEL-SH01-B"], "2026-09-27T09:00:00"))

    show("调拨批次 BAGEL-TRF-0927-1（鲜度时钟随货走，不重置安全窗口）",
         inv.batch_view(inv.batches["BAGEL-TRF-0927-1"], "2026-09-27T09:00:00"))

    show(f"追溯链 {rec_ids['transfer']}：需求版本→执行→售罄→报损",
         app.trace(rec_ids["transfer"]))

    show("区域视角：缺货 vs 损耗", app.region_metrics("2026-09-27T00:00:00", "2026-09-27T23:59:59"))

    show("过安全窗口拦截流水（不得重新上架；当日拦截已全数报损处置）", {
        "interceptions": inv.interceptions,
        "at_risk_now": app.at_risk("SH-01"),
    })


if __name__ == "__main__":
    main()
