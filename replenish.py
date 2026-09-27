"""补货建议引擎：现烤 / 调拨 / 降价 / 停止制作 / 拦截重新上架。

每条建议都是不可变快照，显式携带：
  evidence.forecast_version_id  采用的需求版本
  evidence.stock_snapshot       决策时各池库存
  evidence.costs                全部成本项（原料/人工/烤炉机会成本/缺货损失/预期报损）
  expected_net_benefit          建议相对"不动作"的净收益
建议发布后不回写；晚到事件只影响下一条建议。
"""

from config import COST_MODEL, SKUS, STORES
from timeutil import add_minutes


class RecommendationStore:
    def __init__(self):
        self._recs = []
        self._execs = []
        self._seq = 0
        self._exec_seq = 0

    # ---------- 生成 ----------

    def generate(self, inventory, forecast_of, at, horizon_end, store_id=None, sku=None):
        """对每家门店每个 SKU 评估一次；forecast_of(store, sku) 返回需求版本或 None。"""
        recs = []
        stores = sorted({b["store_id"] for b in inventory.batches.values()} | set(STORES.keys()))
        if store_id:
            stores = [store_id]
        skus = [sku] if sku else list(SKUS.keys())
        # 先算全区域缺口，供调拨配对
        gaps = {}
        for sid in stores:
            for k in skus:
                fc = forecast_of(sid, k)
                remaining = fc["expected_remaining_qty"] if fc else 0.0
                t = inventory.totals(sid, k)
                planned = sum(p["qty"] for p in inventory.pending_plans(sid, k, at))
                gaps[(sid, k)] = remaining - t["sellable"] - planned
        for sid in stores:
            for k in skus:
                recs.extend(self._evaluate(inventory, forecast_of(sid, k),
                                           sid, k, at, horizon_end, gaps))
        return recs

    def _evaluate(self, inv, fc, store_id, sku, at, horizon_end, gaps):
        out = []
        t = inv.totals(store_id, sku)
        sellable = t["sellable"]
        blocked = t["blocked"] + t["clearance"]
        remaining = fc["expected_remaining_qty"] if fc else 0.0
        gap = remaining - sellable          # >0 缺货；<0 过剩
        planned = sum(p["qty"] for p in inv.pending_plans(store_id, sku, at))
        gap_after_plans = gap - planned

        stock_snapshot = {k: t[k] for k in ("available", "reserved", "in_delivery", "returning",
                                            "markdown", "clearance", "blocked", "sold", "waste")}
        evidence_base = {
            "forecast_version_id": fc["version_id"] if fc else None,
            "forecast_expected_remaining": remaining,
            "forecast_data_gaps": fc["inputs"]["data_gaps"] if fc else [],
            "stock_snapshot": stock_snapshot,
            "planned_production_qty": planned,
            "event_watermark": dict(inv.watermark) if inv.watermark else None,
        }

        # 1) 已拦截数量：永远不能回架，只能降价/清仓/报损
        if blocked > 0:
            out.append(self._make("block_restock", store_id, sku, blocked, at, evidence_base,
                                  self._costs(sku, qty=blocked, mode="dispose"),
                                  reason="存在已过安全窗口被拦截的数量，不得重新上架，"
                                         "请按降价/夜间清仓/报损处置"))

        # 2) 缺口：优先跨店调拨（到店更快），烤炉只补调拨覆盖不了的部分
        if gap_after_plans > 0:
            need = int(gap_after_plans + 0.5)
            oven_free, free_slots = inv.oven_free_at(store_id, at)
            donor = self._find_donor(inv, store_id, sku, need, gaps)
            transfer_qty = min(need, donor["available_qty"]) if donor else 0
            if donor:
                transfer_costs = self._costs(sku, qty=transfer_qty, mode="transfer")
                out.append(self._make("transfer_in", store_id, sku, transfer_qty, at, evidence_base,
                                      transfer_costs, donor=donor,
                                      reason=f"本店缺口 {need}，{donor['store_id']} 预测有余量 "
                                             f"{donor['surplus_qty']}、可调 {transfer_qty}，优先调拨"))
            bake_qty = need - transfer_qty
            if bake_qty > 0:
                # 现烤按烤炉批量向上取整
                batch = SKUS[sku]["bake_batch"]
                bake_qty = ((bake_qty + batch - 1) // batch) * batch
                bake_costs = self._costs(sku, qty=bake_qty, mode="bake")
                if oven_free:
                    out.append(self._make("bake", store_id, sku, bake_qty, at, evidence_base,
                                          bake_costs,
                                          ready_by=add_minutes(at, STORES[store_id]["bake_minutes"]),
                                          reason=f"调拨 {transfer_qty} 后仍缺 {need - transfer_qty}，"
                                                 f"现烤一炉 {bake_qty}"))
                else:
                    out.append(self._make("bake", store_id, sku, bake_qty, at, evidence_base,
                                          bake_costs, ready_by=None,
                                          reason=f"调拨后仍缺 {bake_qty} 但烤炉已满，请排产或接受缺货"))

        # 3) 过剩：安全窗口内预计卖不完 → 停止制作 + 降价 / 调出
        surplus = sellable + planned - remaining
        if surplus > 0 and remaining >= 0:
            waste_costs = self._costs(sku, qty=surplus, mode="waste_if_no_action")
            if planned > 0:
                stop_qty = min(planned, int(surplus))
                out.append(self._make("stop_production", store_id, sku, stop_qty, at,
                                      evidence_base, waste_costs,
                                      reason=f"可售 {sellable} + 在产 {planned} 已覆盖剩余需求 "
                                             f"{remaining}，继续制作将产生预期报损"))
            deficit_store = next((o for o in STORES if o != store_id and gaps.get((o, sku), 0) > 0),
                                 None)
            if deficit_store and sellable > remaining:
                out_qty = min(int(sellable - remaining), int(gaps[(deficit_store, sku)]),
                              t["available"])
                if out_qty > 0:
                    out.append(self._make("transfer_out", store_id, sku, out_qty, at,
                                          evidence_base, self._costs(sku, qty=out_qty, mode="transfer"),
                                          donor={"store_id": deficit_store},
                                          reason=f"本店预测过剩，{deficit_store} 预测缺口 "
                                                 f"{int(gaps[(deficit_store, sku)])}，建议调出 {out_qty}"))
            if sellable > remaining and sellable > 0 and deficit_store is None:
                md_qty = int(sellable - remaining)
                if md_qty > 0:
                    out.append(self._make("markdown", store_id, sku, md_qty, at, evidence_base,
                                          self._costs(sku, qty=md_qty, mode="markdown"),
                                          reason=f"可售 {sellable} 超过剩余需求 {remaining}，"
                                                 f"建议降价出清 {md_qty} 以降低报损"))
        return out

    # ---------- 成本 ----------

    def _costs(self, sku, qty, mode, donor=None):
        s = SKUS[sku]
        c = COST_MODEL
        unit_ing, unit_lab = s["ingredient_cost"], s["labor_cost"]
        price = s["unit_price"]
        costs = {
            "ingredient_cost": round(unit_ing * qty, 2),
            "labor_cost": round(unit_lab * qty, 2),
            "oven_opportunity_cost": 0.0,
            "transfer_cost": 0.0,
            "shortage_cost": 0.0,
            "expected_waste_cost": 0.0,
            "expected_revenue": 0.0,
        }
        if mode == "bake":
            costs["oven_opportunity_cost"] = round(c["oven_cost_per_slot"], 2)
            costs["expected_revenue"] = round(price * qty * (1 - c["lost_sale_rate"] * 0), 2)
            costs["shortage_cost_avoided"] = round(price * c["lost_sale_rate"] * qty, 2)
        elif mode == "transfer":
            costs["transfer_cost"] = round(c["transfer_cost_per_unit"] * qty, 2)
            costs["ingredient_cost"] = 0.0
            costs["labor_cost"] = 0.0
            costs["expected_revenue"] = round(price * qty, 2)
            costs["shortage_cost_avoided"] = round(price * c["lost_sale_rate"] * qty, 2)
        elif mode == "markdown":
            costs["ingredient_cost"] = 0.0
            costs["labor_cost"] = 0.0
            costs["expected_revenue"] = round(price * c["markdown_recovery_rate"] * qty, 2)
            costs["expected_waste_cost"] = round((unit_ing + unit_lab) * qty *
                                                 (1 - c["markdown_recovery_rate"]), 2)
            costs["waste_cost_if_no_action"] = round((unit_ing + unit_lab) * qty *
                                                     c["waste_prob_if_no_action"], 2)
        elif mode == "waste_if_no_action":
            # 停止制作：收益 = 不再投入的生产成本；代价 = 放弃这些数量的预期收入
            costs["ingredient_cost"] = 0.0
            costs["labor_cost"] = 0.0
            costs["avoided_production_cost"] = round((unit_ing + unit_lab) * qty, 2)
            costs["forgone_revenue"] = round(price * qty *
                                             (1 - c["waste_prob_if_bake_over"]), 2)
        elif mode == "dispose":
            # 拦截处置：报损已沉没，收益 = 清仓回收，无新支出
            costs["ingredient_cost"] = 0.0
            costs["labor_cost"] = 0.0
            costs["expected_revenue"] = round(price * c["clearance_recovery_rate"] * qty, 2)
            costs["sunk_waste_cost"] = round((unit_ing + unit_lab) * qty, 2)
        total_cost = (costs["ingredient_cost"] + costs["labor_cost"]
                      + costs["oven_opportunity_cost"] + costs["transfer_cost"]
                      + costs["expected_waste_cost"] + costs.get("forgone_revenue", 0.0))
        # 净收益 = 预期收入 + 避免的缺货损失 + 避免的报损/投入 - 总成本
        gross = (costs["expected_revenue"] + costs.get("shortage_cost_avoided", 0.0)
                 + costs.get("waste_cost_if_no_action", 0.0)
                 + costs.get("avoided_production_cost", 0.0))
        costs["total_cost"] = round(total_cost, 2)
        return costs, round(gross - total_cost, 2)

    def _find_donor(self, inv, store_id, sku, qty, gaps):
        best = None
        for other in STORES:
            if other == store_id:
                continue
            t = inv.totals(other, sku)
            surplus = -gaps.get((other, sku), 0)  # 对端缺口为负即预测过剩
            if t["available"] > 0 and surplus > 0 and (best is None or surplus > best["surplus_qty"]):
                best = {"store_id": other, "available_qty": min(t["available"], int(surplus)),
                        "surplus_qty": int(surplus)}
        return best

    def _make(self, rtype, store_id, sku, qty, at, evidence_base, cost_benefit, reason,
              ready_by=None, donor=None):
        costs, net_benefit = cost_benefit
        self._seq += 1
        rec = {
            "rec_id": f"REC-{at[:10].replace('-', '')}-{self._seq:02d}",
            "type": rtype,
            "store_id": store_id,
            "sku": sku,
            "qty": qty,
            "created_at": at,
            "ready_by": ready_by,
            "donor": donor,
            "reason": reason,
            "evidence": {**evidence_base, "costs": costs},
            "expected_net_benefit": net_benefit,
            "status": "issued",
            "executions": [],
        }
        self._recs.append(rec)
        return rec

    # ---------- 执行与追溯 ----------

    def execute(self, rec_id, at, qty=None, note=""):
        rec = self.get(rec_id)
        if rec is None:
            raise KeyError(f"建议 {rec_id} 不存在")
        self._exec_seq += 1
        exe = {
            "exec_id": f"EXE-{self._exec_seq:02d}",
            "rec_id": rec_id,
            "executed_at": at,
            "qty": qty if qty is not None else rec["qty"],
            "note": note,
        }
        self._execs.append(exe)
        rec["executions"].append(exe["exec_id"])
        rec["status"] = "executed"
        return exe

    def trace(self, rec_id, inventory):
        """建议 → 需求版本 → 实际执行 → 售罄时间 → 最终报损。"""
        rec = self.get(rec_id)
        if rec is None:
            raise KeyError(f"建议 {rec_id} 不存在")
        execs = [e for e in self._execs if e["rec_id"] == rec_id]
        sellouts = [s for s in inventory.sellouts
                    if s["store_id"] == rec["store_id"] and s["sku"] == rec["sku"]
                    and s["sold_out_at"] >= rec["created_at"]]
        waste_batches = []
        for b in inventory.batches.values():
            if b["store_id"] == rec["store_id"] and b["sku"] == rec["sku"] and b["pools"]["waste"]:
                waste_batches.append({"batch_id": b["batch_id"], "waste_qty": b["pools"]["waste"]})
        return {
            "rec_id": rec_id,
            "recommendation": rec,
            "forecast_version_id": rec["evidence"]["forecast_version_id"],
            "executions": execs,
            "sellouts_after": sellouts,
            "waste": waste_batches,
            "total_waste_qty": sum(w["waste_qty"] for w in waste_batches),
        }

    def get(self, rec_id):
        for r in self._recs:
            if r["rec_id"] == rec_id:
                return r
        return None

    def list(self, store_id=None, sku=None):
        return [r for r in self._recs
                if (store_id is None or r["store_id"] == store_id)
                and (sku is None or r["sku"] == sku)]
