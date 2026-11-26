/* Single-page controller for the two-executor alarm dispatch drill. */

const STATUS_LABEL = {
  loading: "台账加载中",
  paused: "暂停中",
  running: "关联执行中",
  succeeded: "成功",
  failed: "模拟失败",
  rejected: "未接单",
};

const MACHINES = ["M1", "M2"];

async function api(url, options = {}) {
  const resp = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const payload = await resp.json();
  return { ok: resp.ok && payload.ok !== false, payload };
}

function setFormMsg(text, isError) {
  const el = document.getElementById("form-msg");
  el.textContent = text;
  el.className = "form-msg " + (isError ? "err" : "ok");
  if (text) {
    setTimeout(() => {
      if (el.textContent === text) {
        el.textContent = "";
        el.className = "form-msg";
      }
    }, 6000);
  }
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

// ------------------------------------------------------------- capacity bar
function renderCapacity(state) {
  for (const m of MACHINES) {
    const info = state.machines[m];
    const input = document.getElementById(`cap-${m}`);
    if (document.activeElement !== input) {
      input.value = info.capacity;
    }
    document.getElementById(`cap-info-${m}`).textContent =
      `已预留 ${info.reserved} 行 / 剩余 ${info.remaining} 行`;
  }
}

// --------------------------------------------------------------- order card
function renderOrder(order) {
  const headActions = [];
  if (order.status === "paused") {
    headActions.push(
      `<button class="btn small" data-act="resume" data-oid="${order.order_id}">继续执行</button>`
    );
    headActions.push(
      `<select data-role="fail-machine">
         <option value="">设置失败模拟…</option>
         ${MACHINES.map((m) => `<option value="${m}">${m} 稍后失败</option>`).join("")}
       </select>
       <button class="btn small danger" data-act="arm-fail" data-oid="${order.order_id}">应用</button>`
    );
  }

  let body = "";

  if (order.status === "rejected") {
    body += `<div class="reject">未接单原因：${esc(order.reject_reason || "")}</div>`;
  }
  if (order.status === "failed") {
    body += `<div class="failure-note">${esc(order.fail_reason || "处理失败")}，整单无结果；预留容量已释放。</div>`;
  }

  // metrics: per-machine loaded rows + order cross-machine actual rows
  const metricHtml = MACHINES.map((m) =>
    `<span class="metric">${m} 加载行数 <b>${order.loaded_rows[m] ?? 0}</b>` +
    `（预留 ${order.reserved[m] ?? 0}）</span>`
  ).join("");
  const quotaText = order.status === "succeeded" || order.status === "running" ||
    order.status === "loading" || order.status === "paused" || order.status === "failed"
    ? `跨机实读 <b>${order.actual_cross}</b> / 额度 ${order.quota}`
    : `预计跨机 ${order.planned_cross} / 额度 ${order.quota}`;
  body += `<div class="metrics">${metricHtml}
    <span class="metric">${quotaText} 行</span>
    <span class="metric">明细：跨机告警 ${order.actual_detail.alarm_cross ?? 0} 行，
      跨机台账 ${order.actual_detail.ledger_cross ?? 0} 行</span>
  </div>`;

  if (order.assignments && order.assignments.length) {
    body += `<table>
      <thead><tr>
        <th>告警号</th><th>设备号</th><th>处理机</th><th>责任班组</th><th>读取情况</th>
      </tr></thead><tbody>`;
    for (const a of order.assignments) {
      const tags = [];
      tags.push(a.alarm_cross
        ? `<span class="tag-cross">跨机读告警</span>`
        : `<span class="tag-local">本机告警</span>`);
      if (a.team) {
        tags.push(a.ledger_cross
          ? `<span class="tag-cross">跨机读台账</span>`
          : `<span class="tag-local">本机台账</span>`);
      } else {
        tags.push(`<span class="tag-cross">无台账，班组留空</span>`);
      }
      body += `<tr>
        <td>${esc(a.alarm_id)}</td>
        <td>${esc(a.device_id)}</td>
        <td>${esc(a.machine)}</td>
        <td class="${a.team ? "" : "blank"}">${a.team ? esc(a.team) : "（留空）"}</td>
        <td>${tags.join(" ")}</td>
      </tr>`;
    }
    body += `</tbody></table>`;
  } else if (order.status !== "rejected" && order.status !== "failed") {
    body += `<div class="empty">台账加载/暂停阶段，关联结果尚未产生…</div>`;
  }

  body += `<div class="events">` +
    order.events.map((e) =>
      `<div class="ev"><span class="t">${esc(e.time)}</span>${esc(e.message)}</div>`
    ).join("") + `</div>`;

  return `<div class="order">
    <div class="order-head">
      <span class="oid">${esc(order.order_id)}</span>
      <span class="badge ${esc(order.status)}">${STATUS_LABEL[order.status] || order.status}</span>
      <span class="cap-info">创建 ${esc(order.created_at)}${order.finished_at ? " · 结束 " + esc(order.finished_at) : ""}</span>
      <span class="actions">${headActions.join("")}</span>
    </div>
    <div class="order-body">${body}</div>
  </div>`;
}

function render(state) {
  renderCapacity(state);
  const wrap = document.getElementById("orders");
  if (!state.orders.length) {
    wrap.innerHTML = `<div class="empty">暂无工单，在上方录入后接单。</div>`;
    return;
  }
  // newest first
  wrap.innerHTML = state.orders.slice().reverse().map(renderOrder).join("");
}

// ------------------------------------------------------------------ polling
async function refresh() {
  try {
    const { payload } = await api("/api/state");
    render(payload);
  } catch (err) {
    console.error("state refresh failed", err);
  }
}
setInterval(refresh, 1000);
refresh();

// ------------------------------------------------------------------- events
document.addEventListener("click", async (ev) => {
  const btn = ev.target.closest("button[data-act]");
  if (!btn) return;
  const act = btn.dataset.act;

  if (act === "set-cap") {
    const machine = btn.dataset.machine;
    const value = Number(document.getElementById(`cap-${machine}`).value);
    const { ok, payload } = await api("/api/capacity", {
      method: "POST",
      body: JSON.stringify({ machine, capacity: value }),
    });
    if (!ok) {
      setFormMsg(payload.error || "容量保存失败", true);
    } else {
      setFormMsg(`${machine} 台账容量已更新为 ${value}`, false);
      render(payload.state);
    }
    return;
  }

  if (act === "resume") {
    const { ok, payload } = await api(`/api/orders/${btn.dataset.oid}/resume`, {
      method: "POST",
      body: "{}",
    });
    if (!ok) setFormMsg(payload.error || "继续失败", true);
    else refresh();
    return;
  }

  if (act === "arm-fail") {
    const card = btn.closest(".order");
    const machine = card.querySelector('[data-role="fail-machine"]').value;
    if (!machine) {
      setFormMsg("请先选择要模拟失败的执行器", true);
      return;
    }
    const { ok, payload } = await api(`/api/orders/${btn.dataset.oid}/fail`, {
      method: "POST",
      body: JSON.stringify({ machine }),
    });
    if (!ok) setFormMsg(payload.error || "设置失败模拟失败", true);
    else {
      setFormMsg("已设置失败模拟，点击「继续执行」后另一台先完成、该机失败", false);
      refresh();
    }
  }
});

document.getElementById("submit-order").addEventListener("click", async () => {
  const quota = Number(document.getElementById("quota").value);
  const payload = {
    quota,
    pause_after_load: document.getElementById("pause-after-load").checked,
    alarms: {},
    ledger: {},
  };
  for (const m of MACHINES) {
    payload.alarms[m] = document.getElementById(`alarms-${m}`).value;
    payload.ledger[m] = document.getElementById(`ledger-${m}`).value;
  }
  const resp = await api("/api/orders", {
    method: "POST",
    body: JSON.stringify(payload),
  });
  if (resp.ok) {
    setFormMsg(`工单 ${resp.payload.order_id} 已接单并预留容量`, false);
  } else {
    setFormMsg(
      `工单 ${resp.payload.order_id || ""}：${resp.payload.reject_reason || resp.payload.error || "接单失败"}`,
      true
    );
  }
  if (resp.payload.state) render(resp.payload.state);
});
