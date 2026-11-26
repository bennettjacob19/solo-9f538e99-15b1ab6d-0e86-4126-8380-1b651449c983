"""In-memory order store and the background worker that runs each order.

The drill is single-process: the two "query executors" are simulated inside
worker threads.  Accepting an order immediately reserves ledger capacity on
both machines; the reservation is released when the order finishes, succeeds
or fails (and also if the worker crashes unexpectedly).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .engine import (
    MAX_DEVICES,
    MACHINES,
    Alarm,
    Assignment,
    LedgerRow,
    Plan,
    evaluate_plan,
    parse_alarms,
    parse_ledger,
)

# Timing of the simulated pipeline (seconds). Kept small so the drill is lively.
LOAD_STEP = 0.35          # per machine ledger loading
ASSOC_STEP = 0.12         # per alarm association
PAUSE_POLL = 0.2          # how often a paused order wakes up to re-check

# Order lifecycle.
STATUS_REJECTED = "rejected"
STATUS_LOADING = "loading"
STATUS_PAUSED = "paused"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
FINAL_STATUSES = (STATUS_REJECTED, STATUS_SUCCEEDED, STATUS_FAILED)


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


@dataclass
class Order:
    order_id: str
    quota: int
    per_machine_alarms: Dict[str, List[Alarm]]
    per_machine_ledger: Dict[str, List[LedgerRow]]
    pause_after_load: bool
    fail_machine: Optional[str] = None

    status: str = STATUS_LOADING
    created_at: str = field(default_factory=_now)
    finished_at: Optional[str] = None
    reject_reason: Optional[str] = None
    fail_reason: Optional[str] = None

    # reservation / accounting
    reserved: Dict[str, int] = field(default_factory=lambda: {m: 0 for m in MACHINES})
    loaded_rows: Dict[str, int] = field(default_factory=lambda: {m: 0 for m in MACHINES})
    planned_cross: int = 0
    cross_detail: Dict[str, int] = field(default_factory=dict)
    actual_cross: int = 0
    actual_detail: Dict[str, int] = field(default_factory=lambda: {
        "alarm_cross": 0, "ledger_cross": 0
    })
    assignments: List[Assignment] = field(default_factory=list)
    events: List[Dict[str, str]] = field(default_factory=list)

    # threading controls for the pause gate
    _go: threading.Event = field(default_factory=threading.Event)
    _fail_armed: threading.Event = field(default_factory=threading.Event)
    _worker: Optional[threading.Thread] = None

    def log(self, message: str) -> None:
        self.events.append({"time": _now(), "message": message})

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "quota": self.quota,
            "status": self.status,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
            "reject_reason": self.reject_reason,
            "fail_reason": self.fail_reason,
            "pause_after_load": self.pause_after_load,
            "fail_machine": self.fail_machine,
            "reserved": dict(self.reserved),
            "loaded_rows": dict(self.loaded_rows),
            "planned_cross": self.planned_cross,
            "cross_detail": dict(self.cross_detail),
            "actual_cross": self.actual_cross,
            "actual_detail": dict(self.actual_detail),
            "assignments": [vars(a) for a in self.assignments],
            "events": list(self.events),
            "inputs": {
                m: {
                    "alarms": [vars(a) for a in self.per_machine_alarms[m]],
                    "ledger": [vars(r) for r in self.per_machine_ledger[m]],
                }
                for m in MACHINES
            },
        }


class Store:
    """Thread-safe holder of machine capacities and orders."""

    def __init__(self, initial_capacities: Dict[str, int]):
        self._lock = threading.RLock()
        self.capacities: Dict[str, int] = dict(initial_capacities)
        self.reserved: Dict[str, int] = {m: 0 for m in MACHINES}
        self.orders: Dict[str, Order] = {}
        self._seq = 0

    # ------------------------------------------------------------------ state
    def remaining(self) -> Dict[str, int]:
        with self._lock:
            return {
                m: max(0, self.capacities[m] - self.reserved[m])
                for m in MACHINES
            }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            active = [
                o.order_id for o in self.orders.values()
                if o.status not in FINAL_STATUSES
            ]
            return {
                "machines": {
                    m: {
                        "capacity": self.capacities[m],
                        "reserved": self.reserved[m],
                        "remaining": max(0, self.capacities[m] - self.reserved[m]),
                    }
                    for m in MACHINES
                },
                "orders": [o.to_dict() for o in self.orders.values()],
                "active_order_ids": active,
            }

    # ------------------------------------------------------------- config cap
    def set_capacity(self, machine: str, value: int) -> None:
        if machine not in MACHINES:
            raise ValueError(f"未知执行器：{machine}")
        if value < 0:
            raise ValueError("容量不能为负")
        with self._lock:
            active = [
                o.order_id for o in self.orders.values()
                if o.status not in FINAL_STATUSES
            ]
            if active:
                raise ValueError(f"有进行中的工单（{', '.join(active)}），暂不能调整容量")
            if value < self.reserved[machine]:
                raise ValueError(
                    f"容量不能小于已预留行数 {self.reserved[machine]}"
                )
            self.capacities[machine] = value

    # ------------------------------------------------------------ submission
    def submit(
        self,
        quota: int,
        alarms_text: Dict[str, str],
        ledger_text: Dict[str, str],
        pause_after_load: bool,
    ) -> Order:
        """Parse, validate and accept (or reject) one order."""
        if quota < 0:
            raise ValueError("跨机行数额度必须为非负整数")

        per_machine_alarms: Dict[str, List[Alarm]] = {}
        per_machine_ledger: Dict[str, List[LedgerRow]] = {}
        errors: List[str] = []
        all_alarm_ids: set = set()
        all_ledger_devices: set = set()

        for machine in MACHINES:
            alarms, errs = parse_alarms(alarms_text.get(machine, ""))
            for e in errs:
                errors.append(f"{machine} 告警：{e}")
            rows, lerrs = parse_ledger(ledger_text.get(machine, ""))
            for e in lerrs:
                errors.append(f"{machine} 台账：{e}")

            for alarm in alarms:
                if alarm.alarm_id in all_alarm_ids:
                    errors.append(f"告警号在两台之间重复：{alarm.alarm_id}")
                all_alarm_ids.add(alarm.alarm_id)
            for row in rows:
                if row.device_id in all_ledger_devices:
                    errors.append(f"台账设备号在两台之间重复：{row.device_id}")
                all_ledger_devices.add(row.device_id)

            per_machine_alarms[machine] = alarms
            per_machine_ledger[machine] = rows

        if not all_alarm_ids:
            errors.append("至少需要录入一条告警")

        alarm_devices = {
            a.device_id for alarms in per_machine_alarms.values() for a in alarms
        }
        all_devices = alarm_devices | all_ledger_devices
        if len(all_devices) > MAX_DEVICES:
            errors.append(f"最多涉及 {MAX_DEVICES} 个设备，当前涉及 {len(all_devices)} 个")

        with self._lock:
            self._seq += 1
            order_id = f"O{self._seq:04d}"

            order = Order(
                order_id=order_id,
                quota=quota,
                per_machine_alarms=per_machine_alarms,
                per_machine_ledger=per_machine_ledger,
                pause_after_load=pause_after_load,
            )
            self.orders[order_id] = order

            if errors:
                order.status = STATUS_REJECTED
                order.reject_reason = "；".join(errors)
                order.finished_at = _now()
                order.log("录入校验失败：" + order.reject_reason)
                return order

            plan: Plan = evaluate_plan(
                per_machine_alarms,
                per_machine_ledger,
                quota,
                self.capacities,
                self.remaining(),
            )
            order.planned_cross = plan.cross_rows
            order.cross_detail = dict(plan.detail)
            order.reserved = dict(plan.loads)

            if not plan.feasible:
                order.status = STATUS_REJECTED
                order.reject_reason = plan.reason
                order.finished_at = _now()
                order.log("接单判定：" + plan.reason)
                return order

            # Accept: reserve ledger capacity on both machines immediately.
            for machine in MACHINES:
                self.reserved[machine] += plan.loads[machine]
            order.assignments = plan.assignments
            order.log(
                "接单成功并预留台账容量："
                + "，".join(f"{m} {plan.loads[m]} 行" for m in MACHINES)
                + f"；预计跨机 {plan.cross_rows} 行 / 额度 {quota} 行"
            )

            order._worker = threading.Thread(
                target=self._run_order, args=(order,), daemon=True,
                name=f"order-{order_id}",
            )
            order._worker.start()
            return order

    # --------------------------------------------------------- pause / fail
    def resume(self, order_id: str) -> None:
        with self._lock:
            order = self._require_order(order_id)
            if order.status != STATUS_PAUSED:
                raise ValueError("工单不在暂停状态")
            order.status = STATUS_RUNNING
            order.log("人工继续执行")
            order._go.set()

    def arm_failure(self, order_id: str, machine: str) -> None:
        """Arm a post-pause failure: ``machine`` fails after the other one."""
        with self._lock:
            order = self._require_order(order_id)
            if machine not in MACHINES:
                raise ValueError(f"未知执行器：{machine}")
            if order.status != STATUS_PAUSED:
                raise ValueError("仅暂停中的工单可以设置失败模拟")
            order.fail_machine = machine
            order._fail_armed.set()
            order.log(f"已设置失败模拟：{machine} 将在另一台完成后失败")

    def _require_order(self, order_id: str) -> Order:
        if order_id not in self.orders:
            raise ValueError(f"工单不存在：{order_id}")
        return self.orders[order_id]

    # ---------------------------------------------------------------- worker
    def _run_order(self, order: Order) -> None:
        try:
            self._phase_load(order)
            self._phase_gate(order)
            self._phase_associate(order)
            with self._lock:
                if order.status == STATUS_FAILED:
                    return
                order.status = STATUS_SUCCEEDED
                order.finished_at = _now()
                order.log(
                    f"完成：每个告警一行共 {len(order.assignments)} 行，"
                    f"跨机实读 {order.actual_cross} 行（额度 {order.quota} 行）"
                )
        except Exception as exc:  # pragma: no cover - defensive guard
            with self._lock:
                order.status = STATUS_FAILED
                order.finished_at = _now()
                order.fail_reason = f"内部异常：{exc}"
                order.log(order.fail_reason)
        finally:
            with self._lock:
                for machine in MACHINES:
                    self.reserved[machine] -= order.reserved[machine]
                    if self.reserved[machine] < 0:
                        self.reserved[machine] = 0
                order.reserved = {m: 0 for m in MACHINES}
                order.log("已释放两台预留的台账容量")

    def _phase_load(self, order: Order) -> None:
        """Both machines load their own ledger once (parallel-ish, in order)."""
        with self._lock:
            order.status = STATUS_LOADING
        for machine in MACHINES:
            rows = len(order.per_machine_ledger[machine])
            time.sleep(LOAD_STEP)
            with self._lock:
                order.loaded_rows[machine] = rows
                order.log(f"{machine} 台账加载完成，本机加载 {rows} 行")

    def _phase_gate(self, order: Order) -> None:
        """Pause after ledger loading when the order asked for it."""
        if not order.pause_after_load:
            with self._lock:
                order.status = STATUS_RUNNING
            return
        with self._lock:
            order.status = STATUS_PAUSED
            order.log("台账已加载，工单暂停等待；期间可继续提交其他工单")
        # Wake periodically so an armed failure is still observed promptly.
        while not order._go.wait(timeout=PAUSE_POLL):
            if order._fail_armed.is_set():
                # failure armed but not yet resumed: keep waiting, it runs on resume
                pass

    def _phase_associate(self, order: Order) -> None:
        """Associate alarm by alarm; optionally simulate one machine failing."""
        fail_machine = order.fail_machine
        if fail_machine is None and order._fail_armed.is_set():
            fail_machine = order.fail_machine

        # When a failure is simulated, the healthy machine finishes first and
        # the failing machine runs afterwards and raises.
        if fail_machine is not None:
            sequence = [m for m in MACHINES if m != fail_machine] + [fail_machine]
        else:
            sequence = list(MACHINES)

        done: set = set()
        for machine in sequence:
            if fail_machine == machine:
                time.sleep(LOAD_STEP / 2)
                with self._lock:
                    order.status = STATUS_FAILED
                    order.finished_at = _now()
                    order.fail_reason = (
                        f"模拟失败：{machine} 在另一台算完后处理失败，整单无结果"
                    )
                    order.log(order.fail_reason)
                    # cross reads already performed remain visible, no result rows
                    order.assignments = []
                return

            for assignment in order.assignments:
                if assignment.machine != machine or assignment.alarm_id in done:
                    continue
                time.sleep(ASSOC_STEP)
                with self._lock:
                    if assignment.alarm_cross:
                        order.actual_detail["alarm_cross"] += 1
                    if assignment.ledger_cross:
                        order.actual_detail["ledger_cross"] += 1
                    order.actual_cross = sum(order.actual_detail.values())
                    done.add(assignment.alarm_id)
                    cross_note = []
                    if assignment.alarm_cross:
                        cross_note.append("跨机读告警正文")
                    if assignment.ledger_cross:
                        cross_note.append("跨机读台账")
                    note = "、".join(cross_note) if cross_note else "本机读取"
                    team = assignment.team or "（留空）"
                    order.log(
                        f"{machine} 关联告警 {assignment.alarm_id}"
                        f"（设备 {assignment.device_id} → 班组 {team}）：{note}"
                    )
