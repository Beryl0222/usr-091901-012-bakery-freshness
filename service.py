"""烘焙鲜度与补货决策的服务入口。"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import BakeryService
from domain import (ACTIONS, STATES, DomainError, NotFoundError,
                    SafetyWindowViolation)

SERVICE_ID = "bakery-freshness"
SERVICE_NAME = "烘焙鲜度与补货决策"
CONTRACT_PATH = Path(__file__).with_name("domain_contract.json")

SERVICE = BakeryService()


def load_contract():
    """读取并校验项目领域契约。"""
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    if contract.get("service_id") != SERVICE_ID:
        raise ValueError("领域契约与服务身份不一致")
    return contract


def health_payload():
    """返回服务运行状态。"""
    return {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME}


def _match(pattern, path):
    """按段匹配路由模式，提取 {参数}。"""
    pattern_parts = pattern.strip("/").split("/")
    path_parts = path.strip("/").split("/")
    if len(pattern_parts) != len(path_parts):
        return None
    params = {}
    for expected, actual in zip(pattern_parts, path_parts):
        if expected.startswith("{") and expected.endswith("}"):
            params[expected[1:-1]] = actual
        elif expected != actual:
            return None
    return params


ROUTES = {
    ("GET", "/health"): lambda p, q, b: health_payload(),
    ("GET", "/contract"): lambda p, q, b: load_contract(),
    ("POST", "/products"): lambda p, q, b: SERVICE.register_product(b),
    ("GET", "/products"): lambda p, q, b: SERVICE.list_products(),
    ("POST", "/products/{pid}/recipes"): lambda p, q, b: SERVICE.add_recipe(p["pid"], b),
    ("GET", "/products/{pid}/label"): lambda p, q, b: SERVICE.label_at(p["pid"], q.get("at")),
    ("POST", "/stores/{sid}/capacity"): lambda p, q, b: SERVICE.set_capacity(p["sid"], b),
    ("POST", "/forecasts"): lambda p, q, b: SERVICE.register_forecast(b),
    ("POST", "/batches/plan"): lambda p, q, b: SERVICE.plan_batch(b),
    ("POST", "/batches/{bid}/start"): lambda p, q, b: SERVICE.start_batch(p["bid"], b),
    ("POST", "/batches/receive"): lambda p, q, b: SERVICE.receive_batch(b),
    ("GET", "/batches/{bid}"): lambda p, q, b: SERVICE.batch_view(p["bid"], q.get("at")),
    ("POST", "/events"): lambda p, q, b: SERVICE.record_event(b),
    ("GET", "/inventory"): lambda p, q, b: SERVICE.inventory(q["store_id"], q["product_id"], q.get("at")),
    ("POST", "/recommendations/generate"): lambda p, q, b: SERVICE.generate(
        b.get("store_id"), b.get("at"), b.get("horizon_hours", 14)),
    ("GET", "/recommendations"): lambda p, q, b: SERVICE.list_recommendations(
        q.get("store_id"), q.get("status")),
    ("POST", "/recommendations/{rid}/execute"): lambda p, q, b: SERVICE.execute(p["rid"], b),
    ("POST", "/recommendations/{rid}/dismiss"): lambda p, q, b: SERVICE.dismiss(p["rid"]),
    ("GET", "/recommendations/{rid}/trace"): lambda p, q, b: SERVICE.trace(p["rid"]),
    ("GET", "/orders/{oid}/label"): lambda p, q, b: SERVICE.order_label(p["oid"]),
    ("GET", "/analytics/stockout-waste"): lambda p, q, b: SERVICE.stockout_waste(
        q.get("store_id"), q.get("from"), q.get("to")),
}


class Handler(BaseHTTPRequestHandler):
    """健康检查、领域契约与烘焙鲜度补货业务接口。"""

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def _dispatch(self, method):
        parsed = urlparse(self.path)
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        body = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                try:
                    body = json.loads(self.rfile.read(length).decode("utf-8"))
                except json.JSONDecodeError:
                    return self._send_json({"error": "请求体不是合法 JSON"}, 400)
        for (route_method, pattern), handler in ROUTES.items():
            if route_method != method:
                continue
            params = _match(pattern, parsed.path)
            if params is None:
                continue
            try:
                result = handler(params, query, body)
            except SafetyWindowViolation as exc:
                return self._send_json({"error": str(exc)}, 409)
            except NotFoundError as exc:
                return self._send_json({"error": str(exc)}, 404)
            except DomainError as exc:
                return self._send_json({"error": str(exc)}, 409)
            except (KeyError, TypeError, ValueError) as exc:
                return self._send_json({"error": f"参数错误：{exc}"}, 400)
            return self._send_json(result, 200)
        self.send_error(404)

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
        assert contract["states"] == STATES, "契约状态与领域模型不一致"
        assert contract["decision_actions"] == ACTIONS, "契约动作与决策引擎不一致"
        assert len(contract["invariants"]) >= 3, "契约不可破坏原则缺失"
        print("基础检查通过")
        return
    ThreadingHTTPServer(("0.0.0.0", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
