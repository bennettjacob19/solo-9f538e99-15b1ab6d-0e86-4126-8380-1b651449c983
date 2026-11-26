"""Tests for the pure dispatch engine."""

import unittest

from app.engine import (
    MACHINES,
    Alarm,
    LedgerRow,
    evaluate_plan,
    parse_alarms,
    parse_ledger,
)


def alarms(m1, m2):
    return {
        "M1": [Alarm(a, d) for a, d in m1],
        "M2": [Alarm(a, d) for a, d in m2],
    }


def ledger(m1, m2):
    return {
        "M1": [LedgerRow(d, t) for d, t in m1],
        "M2": [LedgerRow(d, t) for d, t in m2],
    }


class ParseTests(unittest.TestCase):
    def test_parse_alarms_accepts_full_width_comma(self):
        rows, errs = parse_alarms("A1，D1\nA2,D2\n\n")
        self.assertEqual(errs, [])
        self.assertEqual([(a.alarm_id, a.device_id) for a in rows],
                         [("A1", "D1"), ("A2", "D2")])

    def test_duplicate_alarm_and_bad_line(self):
        rows, errs = parse_alarms("A1,D1\nA1,D2\nbroken")
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(errs), 2)

    def test_duplicate_ledger_device(self):
        rows, errs = parse_ledger("D1,甲班\nD1,乙班")
        self.assertEqual(len(rows), 1)
        self.assertTrue(errs)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.cap = {m: 20 for m in MACHINES}
        self.rem = {m: 20 for m in MACHINES}

    def test_all_local_zero_cross(self):
        plan = evaluate_plan(
            alarms([("A1", "D1")], [("A2", "D2")]),
            ledger([("D1", "甲班")], [("D2", "乙班")]),
            quota=0, capacities=self.cap, remaining=self.rem,
        )
        self.assertTrue(plan.feasible, plan.reason)
        self.assertEqual(plan.cross_rows, 0)
        self.assertEqual({a.machine for a in plan.assignments}, {"M1", "M2"})
        self.assertEqual(plan.assignments[0].team, "甲班")
        self.assertEqual(plan.loads, {"M1": 1, "M2": 1})

    def test_cross_machine_ledger_read_counts_quota(self):
        # Alarm A1 lives on M1; its device D2 only has a ledger row on M2.
        plan = evaluate_plan(
            alarms([("A1", "D2")], []),
            ledger([], [("D2", "乙班")]),
            quota=1, capacities=self.cap, remaining=self.rem,
        )
        self.assertTrue(plan.feasible, plan.reason)
        self.assertEqual(plan.cross_rows, 1)
        self.assertEqual(plan.detail["ledger_cross"], 1)
        self.assertEqual(plan.assignments[0].machine, "M1")
        self.assertEqual(plan.assignments[0].team, "乙班")

    def test_quota_shortage_rejected(self):
        plan = evaluate_plan(
            alarms([("A1", "D2")], []),
            ledger([], [("D2", "乙班")]),
            quota=0, capacities=self.cap, remaining=self.rem,
        )
        self.assertFalse(plan.feasible)
        self.assertIn("额度不足", plan.reason)

    def test_unknown_device_team_blank(self):
        plan = evaluate_plan(
            alarms([("A1", "DX")], []),
            ledger([("D1", "甲班")], []),
            quota=5, capacities=self.cap, remaining=self.rem,
        )
        self.assertTrue(plan.feasible)
        row = plan.assignments[0]
        self.assertIsNone(row.team)
        self.assertFalse(row.ledger_cross)
        self.assertEqual(plan.cross_rows, 0)

    def test_capacity_shortage_rejected(self):
        plan = evaluate_plan(
            alarms([("A1", "D1")], []),
            ledger([("D1", "甲班"), ("D2", "乙班")], []),
            quota=9,
            capacities={"M1": 20, "M2": 20},
            remaining={"M1": 1, "M2": 20},
        )
        self.assertFalse(plan.feasible)
        self.assertIn("剩余台账容量", plan.reason)

    def test_stranger_device_chooses_cheapest_machine(self):
        # Alarm on M1; device has ledger only on M2 -> processing on M1 costs
        # one ledger cross read, on M2 costs one alarm cross read: tie, home wins.
        plan = evaluate_plan(
            alarms([("A1", "D9")], []),
            ledger([], [("D9", "丙班")]),
            quota=2, capacities=self.cap, remaining=self.rem,
        )
        self.assertEqual(plan.assignments[0].machine, "M1")

    def test_each_alarm_emits_one_row(self):
        plan = evaluate_plan(
            alarms([("A1", "D1"), ("A2", "D2")], [("A3", "D3")]),
            ledger([("D1", "甲班")], [("D2", "乙班"), ("D3", "丙班")]),
            quota=9, capacities=self.cap, remaining=self.rem,
        )
        self.assertTrue(plan.feasible)
        self.assertEqual(len(plan.assignments), 3)
        self.assertEqual([a.alarm_id for a in plan.assignments],
                         ["A1", "A2", "A3"])


if __name__ == "__main__":
    unittest.main()
