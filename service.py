"""烘焙鲜度与补货决策的服务入口：健康检查、领域契约与业务 API。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import BakeryApp

SERVICE_ID = "bakery-freshness"
SERVICE_NAME = "烘焙鲜度与补货决策"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")

APP = BakeryApp()


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    return contract


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


class Handler(BaseHTTPRequestHandler):
    """健康检查、领域契约与补货决策业务接口。"""

    def do_GET(self):
        path, query = self._split()
        if path == "/health":
            return self._send_json(health_payload())
        if path == "/contract":
            return self._send_json(load_contract())
        if path == "/stock":
            return self._send_json(APP.stock(query.get("store"), query.get("sku"), query.get("at")))
        if path == "/stock/at-risk":
            return self._send_json({"at_risk": APP.at_risk(query.get("store"), query.get("at"))})
        if path == "/forecast/versions":
            return self._send_json({"versions": APP.forecasts.list(query.get("store"), query.get("sku"))})
        if path == "/recommendations":
            return self._send_json({"recommendations": APP.recs.list(query.get("store"), query.get("sku"))})
        if path.startswith("/recommendations/") and path.endswith("/trace"):
            rec_id = path.split("/")[2]
            try:
                return self._send_json(APP.trace(rec_id))
            except KeyError as e:
                return self._send_json({"error": str(e)}, 404)
        if path.startswith("/orders/") and path.endswith("/label"):
            order_id = path.split("/")[2]
            label = APP.order_label(order_id)
            if label is None:
                return self._send_json({"error": f"订单 {order_id} 不存在"}, 404)
            return self._send_json(label)
        if path == "/region/metrics":
            return self._send_json(APP.region_metrics(
                query.get("from", "0000-01-01T00:00:00"), query.get("to", "9999-12-31T23:59:59")))
        if path.startswith("/batches/"):
            batch_id = path.split("/")[2]
            inv = APP.inventory()
            b = inv.batches.get(batch_id)
            if b is None:
                return self._send_json({"error": f"批次 {batch_id} 不存在"}, 404)
            return self._send_json(inv.batch_view(b, inv._last_event_at()))
        self.send_error(404)

    def do_POST(self):
        path, _ = self._split()
        body = self._read_body()
        if path == "/events":
            return self._send_json(APP.submit_event(body), 201)
        if path == "/events/batch":
            results = [APP.submit_event(e) for e in body.get("events", [])]
            return self._send_json({"results": results}, 201)
        if path == "/forecast/versions":
            v = APP.generate_forecast(body["store_id"], body["sku"],
                                      body["day_start"], body["horizon_end"],
                                      body.get("note", ""), body.get("at"))
            return self._send_json(v, 201)
        if path == "/recommendations/generate":
            recs = APP.generate_recommendations(body["at"], body["horizon_end"],
                                                body.get("store_id"), body.get("sku"))
            return self._send_json({"recommendations": recs}, 201)
        if path.startswith("/recommendations/") and path.endswith("/execute"):
            rec_id = path.split("/")[2]
            try:
                exe = APP.execute(rec_id, body["at"], body.get("qty"), body.get("note", ""))
            except KeyError as e:
                return self._send_json({"error": str(e)}, 404)
            return self._send_json(exe, 201)
        if path == "/orders":
            event = {
                "event_id": body["event_id"],
                "type": "reservation_placed",
                "occurred_at": body["at"],
                "store_id": body["store_id"],
                "sku": body["sku"],
                "qty": body["qty"],
                "order_id": body["order_id"],
                "payload": body.get("payload", {}),
            }
            result = APP.submit_event(event)
            return self._send_json(result, 201)
        self.send_error(404)

    # ---------- 工具 ----------

    def _split(self):
        parsed = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        return parsed.path, query

    def _read_body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        return


def main():
    parser = argparse.ArgumentParser(description=SERVICE_NAME)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        contract = load_contract()
        assert contract["states"] and contract["invariants"]
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
