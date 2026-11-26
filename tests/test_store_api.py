"""Tests for the Flask API layer (skipped when Flask is not installed)."""

import time
import unittest

from app import store as store_mod

try:
    from app.server import create_app
    _FLASK = True
except ModuleNotFoundError:  # pragma: no cover - environment without Flask
    _FLASK = False

# Speed the simulated pipeline up for tests.
store_mod.LOAD_STEP = 0.01
store_mod.ASSOC_STEP = 0.01
store_mod.PAUSE_POLL = 0.02


def _find_order(state, oid):
    return next(o for o in state["orders"] if o["order_id"] == oid)


@unittest.skipUnless(_FLASK, "Flask 未安装，跳过 API 测试")
class ApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(initial_capacity=10)
        self.client = self.app.test_client()

    def test_health_and_state(self):
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["machines"]["M1"]["capacity"], 10)

    def test_submit_and_poll_to_success(self):
        resp = self.client.post("/api/orders", json={
            "quota": 3,
            "pause_after_load": False,
            "alarms": {"M1": "A1,D1", "M2": "A2,D2"},
            "ledger": {"M1": "D1,甲班", "M2": "D2,乙班"},
        })
        body = resp.get_json()
        self.assertTrue(body["ok"])
        oid = body["order_id"]
        deadline = time.time() + 5
        while time.time() < deadline:
            order = _find_order(self.client.get("/api/state").get_json(), oid)
            if order["status"] in ("succeeded", "failed", "rejected"):
                break
            time.sleep(0.05)
        self.assertEqual(order["status"], "succeeded")
        self.assertEqual(len(order["assignments"]), 2)
        self.assertEqual(order["actual_cross"], 0)

    def test_rejected_returns_409_and_message(self):
        resp = self.client.post("/api/orders", json={
            "quota": 0,
            "pause_after_load": False,
            "alarms": {"M1": "A1,D2", "M2": ""},
            "ledger": {"M1": "", "M2": "D2,乙班"},
        })
        self.assertEqual(resp.status_code, 409)
        body = resp.get_json()
        self.assertFalse(body["ok"])
        self.assertIn("额度不足", body["reject_reason"])

    def test_capacity_change_blocked_with_active_order(self):
        resp = self.client.post("/api/orders", json={
            "quota": 9,
            "pause_after_load": True,
            "alarms": {"M1": "A1,D1", "M2": ""},
            "ledger": {"M1": "D1,甲班", "M2": ""},
        })
        oid = resp.get_json()["order_id"]
        deadline = time.time() + 3
        while time.time() < deadline:
            if _find_order(self.client.get("/api/state").get_json(), oid)[
                    "status"] == "paused":
                break
            time.sleep(0.02)
        bad = self.client.post("/api/capacity", json={
            "machine": "M1", "capacity": 99,
        })
        self.assertEqual(bad.status_code, 400)
        self.client.post(f"/api/orders/{oid}/resume")
        deadline = time.time() + 3
        while time.time() < deadline:
            if _find_order(self.client.get("/api/state").get_json(), oid)[
                    "status"] == "succeeded":
                break
            time.sleep(0.02)
        ok = self.client.post("/api/capacity", json={
            "machine": "M1", "capacity": 99,
        })
        self.assertEqual(ok.status_code, 200)
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["machines"]["M1"]["capacity"], 99)

    def test_pause_resume_with_failure_arm(self):
        resp = self.client.post("/api/orders", json={
            "quota": 9,
            "pause_after_load": True,
            "alarms": {"M1": "A1,D1", "M2": "A2,D2"},
            "ledger": {"M1": "D1,甲班", "M2": "D2,乙班"},
        })
        oid = resp.get_json()["order_id"]
        deadline = time.time() + 3
        while time.time() < deadline:
            if _find_order(self.client.get("/api/state").get_json(), oid)[
                    "status"] == "paused":
                break
            time.sleep(0.02)
        armed = self.client.post(f"/api/orders/{oid}/fail", json={"machine": "M2"})
        self.assertEqual(armed.status_code, 200)
        self.client.post(f"/api/orders/{oid}/resume")
        deadline = time.time() + 3
        while time.time() < deadline:
            order = _find_order(self.client.get("/api/state").get_json(), oid)
            if order["status"] in ("succeeded", "failed"):
                break
            time.sleep(0.02)
        self.assertEqual(order["status"], "failed")
        self.assertEqual(order["assignments"], [])
        state = self.client.get("/api/state").get_json()
        self.assertEqual(state["machines"]["M1"]["reserved"], 0)
        self.assertEqual(state["machines"]["M2"]["reserved"], 0)


if __name__ == "__main__":
    unittest.main()
