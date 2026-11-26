"""Tests for the threaded order store (standard library only)."""

import time
import unittest

from app import store as store_mod
from app.store import Store

# Speed the simulated pipeline up for tests.
store_mod.LOAD_STEP = 0.01
store_mod.ASSOC_STEP = 0.01
store_mod.PAUSE_POLL = 0.02


def wait_for(order_id, store, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with store._lock:
            status = store.orders[order_id].status
        if status in store_mod.FINAL_STATUSES:
            return status
        time.sleep(0.02)
    raise AssertionError(f"order {order_id} did not finish in time")


def wait_status(order_id, store, wanted, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with store._lock:
            status = store.orders[order_id].status
        if status == wanted:
            return status
        time.sleep(0.02)
    raise AssertionError(f"order {order_id} never reached {wanted}")


class StoreFlowTests(unittest.TestCase):
    def setUp(self):
        self.store = Store({"M1": 5, "M2": 5})

    def test_reservation_released_on_success(self):
        order = self.store.submit(
            quota=5,
            alarms_text={"M1": "A1,D1", "M2": "A2,D2"},
            ledger_text={"M1": "D1,甲班", "M2": "D2,乙班"},
            pause_after_load=False,
        )
        self.assertEqual(order.status, store_mod.STATUS_LOADING)
        self.assertEqual(self.store.reserved, {"M1": 1, "M2": 1})
        status = wait_for(order.order_id, self.store)
        self.assertEqual(status, store_mod.STATUS_SUCCEEDED)
        self.assertEqual(self.store.reserved, {"M1": 0, "M2": 0})
        done = self.store.orders[order.order_id]
        self.assertEqual(len(done.assignments), 2)
        self.assertEqual(done.actual_cross, 0)
        self.assertEqual(done.loaded_rows, {"M1": 1, "M2": 1})

    def test_rejected_order_reserves_nothing(self):
        order = self.store.submit(
            quota=0,
            alarms_text={"M1": "A1,D2", "M2": ""},
            ledger_text={"M1": "", "M2": "D2,乙班"},
            pause_after_load=False,
        )
        self.assertEqual(order.status, store_mod.STATUS_REJECTED)
        self.assertIn("额度不足", order.reject_reason)
        self.assertEqual(self.store.reserved, {"M1": 0, "M2": 0})

    def test_capacity_reservation_blocks_second_order(self):
        # M1 ledger takes 4 of 5 rows; the second order needs 3 -> rejected.
        first = self.store.submit(
            quota=9,
            alarms_text={"M1": "A1,D1", "M2": ""},
            ledger_text={"M1": "D1,甲\nD2,乙\nD3,丙\nD4,丁", "M2": ""},
            pause_after_load=True,
        )
        self.assertNotEqual(first.status, store_mod.STATUS_REJECTED)
        wait_status(first.order_id, self.store, store_mod.STATUS_PAUSED)
        self.assertEqual(self.store.remaining()["M1"], 1)

        second = self.store.submit(
            quota=9,
            alarms_text={"M1": "A9,D9", "M2": ""},
            ledger_text={"M1": "D9,戊\nD10,己\nD11,庚", "M2": ""},
            pause_after_load=False,
        )
        self.assertEqual(second.status, store_mod.STATUS_REJECTED)
        self.assertIn("剩余台账容量", second.reject_reason)

        # another order can still be submitted while the first is paused
        other = self.store.submit(
            quota=9,
            alarms_text={"M1": "", "M2": "A8,D8"},
            ledger_text={"M1": "", "M2": "D8,辛班"},
            pause_after_load=False,
        )
        self.assertNotEqual(other.status, store_mod.STATUS_REJECTED)
        wait_for(other.order_id, self.store)

        self.store.resume(first.order_id)
        self.assertEqual(wait_for(first.order_id, self.store),
                         store_mod.STATUS_SUCCEEDED)
        self.assertEqual(self.store.reserved, {"M1": 0, "M2": 0})

    def test_failure_simulation_yields_no_result_and_releases(self):
        order = self.store.submit(
            quota=9,
            alarms_text={"M1": "A1,D1", "M2": "A2,D2"},
            ledger_text={"M1": "D1,甲班", "M2": "D2,乙班"},
            pause_after_load=True,
        )
        wait_status(order.order_id, self.store, store_mod.STATUS_PAUSED)
        self.store.arm_failure(order.order_id, "M2")
        self.store.resume(order.order_id)
        self.assertEqual(wait_for(order.order_id, self.store),
                         store_mod.STATUS_FAILED)
        done = self.store.orders[order.order_id]
        self.assertEqual(done.assignments, [])
        self.assertTrue(done.fail_reason)
        self.assertEqual(self.store.reserved, {"M1": 0, "M2": 0})

    def test_too_many_devices_rejected(self):
        alarms = "\n".join(f"A{i},D{i}" for i in range(13))
        order = self.store.submit(
            quota=99,
            alarms_text={"M1": alarms, "M2": ""},
            ledger_text={"M1": "", "M2": ""},
            pause_after_load=False,
        )
        self.assertEqual(order.status, store_mod.STATUS_REJECTED)
        self.assertIn("12 个设备", order.reject_reason)

    def test_cross_reads_are_counted_at_runtime(self):
        # Alarm on M1, its ledger row lives on M2 -> exactly one cross read.
        order = self.store.submit(
            quota=3,
            alarms_text={"M1": "A1,D2", "M2": ""},
            ledger_text={"M1": "", "M2": "D2,乙班"},
            pause_after_load=False,
        )
        self.assertNotEqual(order.status, store_mod.STATUS_REJECTED)
        wait_for(order.order_id, self.store)
        done = self.store.orders[order.order_id]
        self.assertEqual(done.status, store_mod.STATUS_SUCCEEDED)
        self.assertEqual(done.actual_cross, 1)
        self.assertEqual(done.actual_detail["ledger_cross"], 1)
        self.assertEqual(done.assignments[0].team, "乙班")

    def test_duplicate_alarm_across_machines_rejected(self):
        order = self.store.submit(
            quota=9,
            alarms_text={"M1": "A1,D1", "M2": "A1,D2"},
            ledger_text={"M1": "D1,甲", "M2": "D2,乙"},
            pause_after_load=False,
        )
        self.assertEqual(order.status, store_mod.STATUS_REJECTED)
        self.assertIn("告警号在两台之间重复", order.reject_reason)


if __name__ == "__main__":
    unittest.main()
