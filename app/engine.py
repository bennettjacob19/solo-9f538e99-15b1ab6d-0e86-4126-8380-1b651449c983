"""Pure domain logic for the two-executor alarm dispatch drill.

Two query executors (machines) exist: ``M1`` and ``M2``.  Every order enters
its own alarms and ledger rows on each machine.  Reading data that lives on the
*other* machine consumes one row of the order's cross-machine quota; reading
local data is free.  Every machine loads its own ledger for the order exactly
once, which reserves that many rows of the machine's remaining ledger capacity.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

MACHINES = ("M1", "M2")
MAX_DEVICES = 12

# A line is "key,value"; accept ASCII / full-width commas and surrounding spaces.
_SPLIT_RE = re.compile(r"[,，]")


@dataclass
class Alarm:
    alarm_id: str
    device_id: str


@dataclass
class LedgerRow:
    device_id: str
    team: str


@dataclass
class Assignment:
    """One alarm row after association."""

    alarm_id: str
    device_id: str
    machine: str            # the machine that processed this alarm
    team: Optional[str]     # None / blank when the device has no ledger row
    ledger_cross: bool      # True if the ledger row had to be read cross-machine
    alarm_cross: bool       # True if the alarm body was read cross-machine


@dataclass
class Plan:
    feasible: bool
    reason: str = ""
    assignments: List[Assignment] = field(default_factory=list)
    # minimum cross-machine rows actually needed (alarm bodies + ledger reads)
    cross_rows: int = 0
    # ledger rows each machine loads once for this order
    loads: Dict[str, int] = field(default_factory=dict)
    detail: Dict[str, int] = field(default_factory=dict)


def split_key_value(line: str) -> Tuple[str, str]:
    """Split one textarea line into a (key, value) pair."""
    parts = _SPLIT_RE.split(line.strip(), maxsplit=1)
    key = parts[0].strip()
    value = parts[1].strip() if len(parts) > 1 else ""
    return key, value


def parse_pairs(text: str) -> List[Tuple[str, str]]:
    """Parse multi-line ``key,value`` input, skipping blank lines."""
    out: List[Tuple[str, str]] = []
    for raw in (text or "").splitlines():
        if not raw.strip():
            continue
        out.append(split_key_value(raw))
    return out


def parse_alarms(text: str) -> Tuple[List[Alarm], List[str]]:
    """Parse alarm textarea content. Returns (alarms, errors)."""
    errors: List[str] = []
    alarms: List[Alarm] = []
    seen_ids: set = set()
    for line_no, pair in enumerate(parse_pairs(text), start=1):
        alarm_id, device_id = pair
        if not alarm_id or not device_id:
            errors.append(f"第 {line_no} 行告警格式应为「告警号,设备号」")
            continue
        if alarm_id in seen_ids:
            errors.append(f"告警号重复：{alarm_id}")
            continue
        seen_ids.add(alarm_id)
        alarms.append(Alarm(alarm_id, device_id))
    return alarms, errors


def parse_ledger(text: str) -> Tuple[List[LedgerRow], List[str]]:
    """Parse ledger textarea content. Returns (ledger rows, errors)."""
    errors: List[str] = []
    rows: List[LedgerRow] = []
    seen_devices: set = set()
    for line_no, pair in enumerate(parse_pairs(text), start=1):
        device_id, team = pair
        if not device_id or not team:
            errors.append(f"第 {line_no} 行台账格式应为「设备号,班组」")
            continue
        if device_id in seen_devices:
            errors.append(f"台账设备号重复：{device_id}")
            continue
        seen_devices.add(device_id)
        rows.append(LedgerRow(device_id, team))
    return rows, errors


def _find_home(alarm: Alarm, per_machine_alarms: Dict[str, List[Alarm]]) -> str:
    for machine in MACHINES:
        if any(a.alarm_id == alarm.alarm_id for a in per_machine_alarms[machine]):
            return machine
    return MACHINES[0]


def evaluate_plan(
    per_machine_alarms: Dict[str, List[Alarm]],
    per_machine_ledger: Dict[str, List[LedgerRow]],
    quota: int,
    capacities: Dict[str, int],
    remaining: Dict[str, int],
) -> Plan:
    """Check whether the order can be accepted and build the cheapest plan.

    Each alarm is independent, so the minimum cross-machine arrangement is the
    greedy one: process an alarm on its home machine whenever that does not cost
    more than the alternative.  The ledger of the device's home machine costs
    one cross-machine read whenever the processing machine is the other one.
    """
    loads = {m: len(per_machine_ledger[m]) for m in MACHINES}

    # 1. ledger capacity: each machine loads its own ledger exactly once.
    for machine in MACHINES:
        if loads[machine] > remaining[machine]:
            return Plan(
                feasible=False,
                reason=(
                    f"额度不足：{machine} 本单需加载台账 {loads[machine]} 行，"
                    f"剩余台账容量仅 {remaining[machine]} 行"
                ),
                loads=loads,
            )

    # device -> machine that owns the ledger row (None if no ledger row at all)
    ledger_home: Dict[str, Optional[str]] = {}
    for machine in MACHINES:
        for row in per_machine_ledger[machine]:
            ledger_home[row.device_id] = machine

    assignments: List[Assignment] = []
    detail = {"alarm_cross": 0, "ledger_cross": 0}

    # keep input order: M1 alarms first, then M2 alarms
    ordered = [a for m in MACHINES for a in per_machine_alarms[m]]
    for alarm in ordered:
        home = _find_home(alarm, per_machine_alarms)
        other = MACHINES[1] if home == MACHINES[0] else MACHINES[0]
        lh = ledger_home.get(alarm.device_id)

        def cost(machine: str) -> int:
            total = 0
            if machine != home:
                total += 1  # cross-machine read of the alarm body
            if lh is not None and machine != lh:
                total += 1  # cross-machine read of the ledger row
            return total

        # Prefer the home machine on a tie, M1 on an exact tie between strangers.
        candidates = sorted(
            MACHINES,
            key=lambda machine: (cost(machine), 0 if machine == home else 1,
                                 0 if machine == MACHINES[0] else 1),
        )
        chosen = candidates[0]
        alarm_cross = chosen != home
        ledger_cross = lh is not None and chosen != lh
        team = None
        if lh is not None:
            for row in per_machine_ledger[lh]:
                if row.device_id == alarm.device_id:
                    team = row.team
                    break
        if alarm_cross:
            detail["alarm_cross"] += 1
        if ledger_cross:
            detail["ledger_cross"] += 1
        assignments.append(
            Assignment(
                alarm_id=alarm.alarm_id,
                device_id=alarm.device_id,
                machine=chosen,
                team=team,
                ledger_cross=ledger_cross,
                alarm_cross=alarm_cross,
            )
        )

    cross_rows = detail["alarm_cross"] + detail["ledger_cross"]

    # 2. order-level cross-machine quota.
    if cross_rows > quota:
        return Plan(
            feasible=False,
            reason=(
                f"额度不足：最少需要跨机读取 {cross_rows} 行"
                f"（告警正文 {detail['alarm_cross']} 行 + 台账 {detail['ledger_cross']} 行），"
                f"本单跨机额度仅 {quota} 行"
            ),
            assignments=assignments,
            cross_rows=cross_rows,
            loads=loads,
            detail=detail,
        )

    return Plan(
        feasible=True,
        reason="",
        assignments=assignments,
        cross_rows=cross_rows,
        loads=loads,
        detail=detail,
    )
