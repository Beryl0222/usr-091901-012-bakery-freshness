"""静态主数据：门店、SKU、成本模型、配方基线。

成本模型是建议引擎的计价依据，所有金额单位为人民币元：
  ingredient_cost   单位原料成本
  labor_cost        单位人工成本（现烤分摊）
  oven_cost_per_slot 烤炉一炉次的机会成本（占用产能即放弃其他品类）
  transfer_cost_per_unit 调拨单位物流成本
  lost_sale_rate    缺货时估计的成交流失率（缺货机会损失 = 售价 × 流失率）
  waste_prob_*      不同处置下的预期报损概率
"""

STORES = {
    "SH-01": {"name": "人民广场店", "oven_slots": 2, "bake_minutes": 40},
    "SH-02": {"name": "中山公园店", "oven_slots": 2, "bake_minutes": 40},
}

SKUS = {
    "BAGEL-LOW": {
        "name": "低糖全麦贝果",
        "category": "低糖",
        "unit_price": 12.0,
        "ingredient_cost": 3.2,
        "labor_cost": 1.1,
        "shelf_hours": 10,
        "safe_hours": 6,
        "bake_batch": 20,
    },
    "BUN-BUTTER": {
        "name": "黄油餐包",
        "category": "甜面包",
        "unit_price": 8.0,
        "ingredient_cost": 2.0,
        "labor_cost": 0.8,
        "shelf_hours": 8,
        "safe_hours": 5,
        "bake_batch": 20,
    },
    "CROISSANT": {
        "name": "经典可颂",
        "category": "酥皮",
        "unit_price": 14.0,
        "ingredient_cost": 4.0,
        "labor_cost": 1.5,
        "shelf_hours": 8,
        "safe_hours": 4,
        "bake_batch": 16,
    },
}

COST_MODEL = {
    "oven_cost_per_slot": 6.0,
    "transfer_cost_per_unit": 0.5,
    "lost_sale_rate": 0.6,
    "waste_prob_if_bake_over": 0.7,
    "waste_prob_if_no_action": 0.85,
    "markdown_recovery_rate": 0.5,
    "clearance_recovery_rate": 0.25,
}

# 日内需求衰减：早餐高峰后逐小时回落，用于剩余时段需求外推
DEMAND_DECAY = {6: 0.8, 7: 1.0, 8: 0.7, 9: 0.5, 10: 0.35, 11: 0.25}
DEMAND_DECAY_DEFAULT = 0.2

# 配方基线：recipe_published 事件可在此之上追加新版本。
# 批次按生产时刻解析生效版本；订单按下单时刻冻结标签。
BASELINE_RECIPES = [
    {
        "sku": "BAGEL-LOW",
        "version": "v1",
        "effective_from": "2026-01-01T00:00:00",
        "nutrition": {"热量_kcal": 245, "蛋白质_g": 9.0, "糖_g": 3.5, "脂肪_g": 2.8},
        "allergens": ["麸质", "芝麻"],
        "note": "初代低糖配方",
    },
    {
        "sku": "BUN-BUTTER",
        "version": "v1",
        "effective_from": "2026-01-01T00:00:00",
        "nutrition": {"热量_kcal": 310, "蛋白质_g": 6.2, "糖_g": 14.0, "脂肪_g": 12.5},
        "allergens": ["麸质", "乳制品", "鸡蛋"],
        "note": "经典甜面包",
    },
    {
        "sku": "CROISSANT",
        "version": "v1",
        "effective_from": "2026-01-01T00:00:00",
        "nutrition": {"热量_kcal": 280, "蛋白质_g": 5.1, "糖_g": 6.0, "脂肪_g": 16.0},
        "allergens": ["麸质", "乳制品"],
        "note": "黄油酥皮",
    },
]
