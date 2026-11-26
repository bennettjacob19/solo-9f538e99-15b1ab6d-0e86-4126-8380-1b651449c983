"""Flask entry point: single-process drill web UI plus JSON API."""

from __future__ import annotations

import os

from flask import Flask, jsonify, render_template, request

from .engine import MACHINES
from .store import Store

DEFAULT_CAP = int(os.environ.get("DEFAULT_LEDGER_CAP", "20"))


def create_app(initial_capacity: int | None = None) -> Flask:
    app = Flask(__name__)
    cap = DEFAULT_CAP if initial_capacity is None else initial_capacity
    store = Store({m: cap for m in MACHINES})
    app.config["STORE"] = store

    # ---------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return render_template("index.html", machines=MACHINES, default_cap=cap)

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"})

    # ---------------------------------------------------------------- state
    @app.get("/api/state")
    def api_state():
        return jsonify(store.snapshot())

    # -------------------------------------------------------------- capacity
    @app.post("/api/capacity")
    def api_capacity():
        data = request.get_json(force=True, silent=True) or {}
        machine = data.get("machine")
        try:
            value = int(data.get("capacity"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "容量必须为整数"}), 400
        try:
            store.set_capacity(machine, value)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "state": store.snapshot()})

    # ---------------------------------------------------------------- orders
    @app.post("/api/orders")
    def api_submit_order():
        data = request.get_json(force=True, silent=True) or {}
        try:
            quota = int(data.get("quota"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "跨机额度必须为整数"}), 400
        if quota < 0:
            return jsonify({"ok": False, "error": "跨机额度不能为负"}), 400

        alarms_text = {
            m: (data.get("alarms") or {}).get(m, "") for m in MACHINES
        }
        ledger_text = {
            m: (data.get("ledger") or {}).get(m, "") for m in MACHINES
        }
        pause = bool(data.get("pause_after_load"))

        order = store.submit(quota, alarms_text, ledger_text, pause)
        return jsonify(
            {
                "ok": order.status != "rejected",
                "order_id": order.order_id,
                "status": order.status,
                "reject_reason": order.reject_reason,
                "state": store.snapshot(),
            }
        ), (200 if order.status != "rejected" else 409)

    @app.post("/api/orders/<order_id>/resume")
    def api_resume(order_id: str):
        try:
            store.resume(order_id)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "state": store.snapshot()})

    @app.post("/api/orders/<order_id>/fail")
    def api_fail(order_id: str):
        data = request.get_json(force=True, silent=True) or {}
        machine = data.get("machine")
        try:
            store.arm_failure(order_id, machine)
        except ValueError as exc:
            return jsonify({"ok": False, "error": str(exc)}), 400
        return jsonify({"ok": True, "state": store.snapshot()})

    return app


if __name__ == "__main__":
    app = create_app()
    app.run(host="0.0.0.0", port=5000, threaded=True)
