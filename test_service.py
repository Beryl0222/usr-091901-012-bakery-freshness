"""验证基础服务与领域契约保持一致，并覆盖主要业务接口。"""
import json, threading, unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from service import Handler, SERVICE_ID, health_payload, load_contract

DAY = "2026-09-27"


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)

    def read_json(self, path):
        with urlopen(f"{self.base_url}{path}", timeout=2) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "application/json")
            return json.load(response)

    def test_health_identity(self):
        self.assertEqual(self.read_json("/health"), health_payload())

    def test_contract_identity_and_rules(self):
        contract = self.read_json("/contract")
        self.assertEqual(contract, load_contract())
        self.assertEqual(contract["service_id"], SERVICE_ID)
        self.assertGreaterEqual(len(contract["invariants"]), 3)

    def test_unknown_route_is_hidden(self):
        with self.assertRaises(HTTPError) as error:
            urlopen(f"{self.base_url}/unknown", timeout=2)
        self.assertEqual(error.exception.code, 404)
        error.exception.close()


class BakeryApiTest(unittest.TestCase):
    """端到端：建档 → 入库 → 销售 → 决策 → 执行 → 追溯 → 分析。"""

    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"
        cls.suffix = "api"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=2)

    def pid(self, name):
        return f"{name}-{self.suffix}"

    def store(self, name="s1"):
        return f"{name}-{self.suffix}"

    def get(self, path):
        with urlopen(f"{self.base_url}{path}", timeout=2) as response:
            self.assertEqual(response.status, 200)
            return json.load(response)

    def post(self, path, payload, expect=200):
        request = Request(f"{self.base_url}{path}", data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=2) as response:
                self.assertEqual(response.status, expect)
                return json.load(response)
        except HTTPError as error:
            raw = error.read()
            error.close()
            self.assertEqual(error.code, expect, raw.decode("utf-8"))
            return json.loads(raw)

    def test_full_business_flow(self):
        product = self.post("/products", {
            "product_id": self.pid("bagel"), "name": "原味贝果", "category": "贝果",
            "price": 10.0, "cost": 4.0, "shelf_life_hours": 24,
            "safety_window_hours": 10, "near_expiry_after_hours": 6})
        self.assertEqual(product["markdown_price"], 7.0)
        self.post(f"/products/{self.pid('bagel')}/recipes", {
            "version": 1, "effective_from": f"{DAY}T00:00:00Z",
            "nutrition": {"热量": 250}, "allergens": ["麸质", "芝麻"]})
        label = self.get(f"/products/{self.pid('bagel')}/label?at={DAY}T08:00:00Z")
        self.assertEqual(label["allergens"], ["麸质", "芝麻"])
        self.post(f"/stores/{self.store()}/capacity",
                  {"oven": {self.pid("bagel"): 30}, "baking_hours": [5, 20]})
        self.post("/forecasts", {"store_id": self.store(), "version": "fc-api",
                                 "hourly": {self.pid("bagel"): [20 if h in (7, 8, 9) else 1 for h in range(24)]}})
        received = self.post("/batches/receive", {
            "scan_id": f"scan-{self.suffix}-1", "store_id": self.store(),
            "product_id": self.pid("bagel"), "quantity": 12,
            "produced_at": f"{DAY}T06:00:00Z", "occurred_at": f"{DAY}T06:30:00Z"})
        self.assertTrue(received["recorded"])
        batch_id = received["batch"]["batch_id"]
        duplicate = self.post("/batches/receive", {
            "scan_id": f"scan-{self.suffix}-1", "store_id": self.store(),
            "product_id": self.pid("bagel"), "quantity": 12,
            "produced_at": f"{DAY}T06:00:00Z", "occurred_at": f"{DAY}T06:31:00Z"})
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["inventory"]["on_hand"], 12)
        sale = self.post("/events", {
            "type": "sale", "store_id": self.store(), "product_id": self.pid("bagel"),
            "quantity": 3, "order_id": f"order-{self.suffix}", "occurred_at": f"{DAY}T08:00:00Z"})
        self.assertEqual(sale["fulfilled"], 3)
        order = self.get(f"/orders/order-{self.suffix}/label")
        self.assertEqual(order["label"]["recipe_version"], 1)
        inventory = self.get(f"/inventory?store_id={self.store()}&product_id={self.pid('bagel')}&at={DAY}T09:00:00Z")
        self.assertEqual(inventory["sellable"], 9)
        recs = self.post("/recommendations/generate",
                         {"store_id": self.store(), "at": f"{DAY}T06:00:00Z", "horizon_hours": 12})
        bake = [r for r in recs if r["action"] == "现烤"]
        self.assertTrue(bake)
        executed = self.post(f"/recommendations/{bake[0]['recommendation_id']}/execute",
                             {"actor": "店长", "occurred_at": f"{DAY}T06:15:00Z"})
        self.assertEqual(executed["recommendation"]["status"], "已执行")
        trace = self.get(f"/recommendations/{bake[0]['recommendation_id']}/trace")
        self.assertEqual(trace["demand_version"], "fc-api")
        self.assertEqual(trace["executions"][0]["actor"], "店长")
        report = self.get(f"/analytics/stockout-waste?store_id={self.store()}&from={DAY}&to={DAY}T23:59:59Z")
        self.assertEqual(report["stores"][0]["store_id"], self.store())
        relist = self.post("/events", {
            "type": "relist", "store_id": self.store(), "product_id": self.pid("bagel"),
            "batch_id": batch_id, "occurred_at": f"{DAY}T17:00:00Z"}, expect=409)
        self.assertIn("安全窗口", relist["error"])
        missing = self.get_missing(f"/orders/no-such-order/label")
        self.assertEqual(missing, 404)

    def get_missing(self, path):
        try:
            urlopen(f"{self.base_url}{path}", timeout=2)
        except HTTPError as error:
            code = error.code
            error.close()
            return code
        return 200


if __name__ == "__main__":
    unittest.main()
