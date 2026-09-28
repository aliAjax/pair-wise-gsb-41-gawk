/* 页面层：事件登记/解除、队列时效展示、案件详情与时间线。 */
const PERIL_LABEL = { typhoon: "台风", earthquake: "地震", flood: "洪水", other: "其他" };

function headers_() {
  return {
    "Content-Type": "application/json",
    "X-User": document.querySelector("#user").value || "viewer",
    "X-Role": document.querySelector("#role").value,
  };
}

function flash(text, ok = false) {
  const el = document.querySelector("#msg");
  el.textContent = text || "";
  el.style.color = ok ? "#2f855a" : "#c05621";
  if (text) setTimeout(() => { if (el.textContent === text) el.textContent = ""; }, 4000);
}

async function api(path, method = "GET", body) {
  const resp = await fetch(path, {
    method,
    headers: headers_(),
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await json_(resp);
  if (!resp.ok) throw new Error(data.error || ("请求失败 " + resp.status));
  return data;
}

async function json_(resp) {
  try { return await resp.json(); } catch (_) { return {}; }
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]
  ));
}

function fmtClock(c) {
  if (!c) return "";
  const segments = (c.pause_segments || []).map((s) => `${s.start.slice(0, 16)} → ${s.end.slice(0, 16)}`).join("<br>");
  let badge;
  if (c.overdue) badge = '<span class="badge badge-overdue">已逾期</span>';
  else if (c.clock_stopped) badge = '<span class="badge badge-suspended">停表中</span>';
  else badge = '<span class="badge badge-ok">计时中</span>';
  return {
    original: c.original_deadline.slice(0, 16).replace("T", " "),
    segments: segments || "—",
    days: c.paused_days.toFixed(2),
    current: c.current_deadline.slice(0, 16).replace("T", " "),
    badge,
  };
}

function renderEvents(events) {
  const tbody = document.querySelector("#events tbody");
  tbody.innerHTML = events.map((e) => {
    const active = e.status === "active";
    const badge = active
      ? '<span class="badge badge-active">生效中</span>'
      : '<span class="badge badge-lifted">已解除</span>';
    const lift = active
      ? `<button class="danger" data-lift="${e.id}">解除并顺延</button>`
      : "—";
    return `<tr>
      <td>${esc(e.code)}</td><td>${esc(e.name)}</td><td>${esc(PERIL_LABEL[e.peril_type] || e.peril_type)}</td>
      <td>${esc(e.region)}</td><td>${esc(e.started_at)}</td><td>${esc(e.ended_at || "—")}</td>
      <td>${badge}</td><td>${lift}</td>
    </tr>`;
  }).join("");
  tbody.querySelectorAll("[data-lift]").forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const r = await api("/api/events/lift", "POST", { event_id: Number(btn.dataset.lift) });
        flash(`事件已解除，重开 ${r.reopened_claims.length} 件，期限已顺延`, true);
        load();
      } catch (err) { flash(err.message); btn.disabled = false; }
    };
  });
}

function renderClaims(claims) {
  const tbody = document.querySelector("#claims tbody");
  tbody.innerHTML = claims.map((r) => {
    const c = fmtClock(r.clock);
    const status = r.status === "suspended"
      ? '<span class="badge badge-suspended">已中止</span>'
      : esc(r.status);
    const reopen = r.status === "suspended"
      ? `<button class="ghost" data-reopen="${r.id}">重开</button>`
      : "";
    return `<tr>
      <td>${esc(r.claim_no)}</td><td>${esc(r.region)}</td><td>${status}</td>
      <td>${Number(r.priority_score).toFixed(1)}</td><td>${esc(r.fraud_score)}</td>
      <td>${esc(r.assignee || "")}</td>
      <td>${c.original}</td><td class="pauses">${c.segments}</td><td>${c.days}</td>
      <td>${c.current}</td><td>${c.badge}</td>
      <td><button class="ghost" data-detail="${r.id}">详情</button> ${reopen}</td>
    </tr>`;
  }).join("");
  tbody.querySelectorAll("[data-detail]").forEach((btn) => {
    btn.onclick = () => loadDetail(Number(btn.dataset.detail));
  });
  tbody.querySelectorAll("[data-reopen]").forEach((btn) => {
    btn.onclick = async () => {
      btn.disabled = true;
      try {
        const r = await api("/api/claims/reopen", "POST", { claim_id: Number(btn.dataset.reopen) });
        flash(`案件 ${r.claim_no} 已重开，当前期限 ${r.clock.current_deadline}`, true);
        load();
      } catch (err) { flash(err.message); btn.disabled = false; }
    };
  });
}

function renderTimeline(entries) {
  document.querySelector("#timeline").innerHTML = entries.slice(0, 25).map((x) => {
    let detail = x.details;
    try { detail = JSON.stringify(JSON.parse(x.details)); } catch (_) { /* 原样展示 */ }
    return `<li>${esc(x.created_at)} ${esc(x.actor)} <strong>${esc(x.action)}</strong> ${esc(detail)}</li>`;
  }).join("");
}

async function loadDetail(claimId) {
  const box = document.querySelector("#detail");
  try {
    const d = await api(`/api/claims/${claimId}/detail`);
    const c = fmtClock(d.clock);
    const segments = (d.clock.pause_segments || [])
      .map((s) => `<li>${esc(s.start)} ～ ${esc(s.end)}</li>`).join("") || "<li>无暂停</li>";
    const events = (d.events || [])
      .map((e) => `<li>${esc(e.code)} ${esc(e.name)}（${esc(e.region)}）${esc(e.started_at)} → ${esc(e.ended_at || "生效中")} [${esc(e.status)}]</li>`)
      .join("") || "<li>无关联灾害事件</li>";
    box.innerHTML = `
      <div class="detail-box">
        <h3 style="margin:4px 0">${esc(d.claim_no)} · ${esc(d.region)} · ${esc(d.status)}</h3>
        <dl>
          <dt>原期限（受理后15天）</dt><dd>${c.original}</dd>
          <dt>暂停区间（并集去重）</dt><dd><ul class="timeline-detail" style="margin:0;padding-left:18px">${segments}</ul></dd>
          <dt>累计暂停</dt><dd>${c.days} 天（${d.clock.paused_seconds} 秒）</dd>
          <dt>当前期限</dt><dd>${c.current} ${c.badge}</dd>
          <dt>命中灾害事件</dt><dd><ul class="timeline-detail" style="margin:0;padding-left:18px">${events}</ul></dd>
        </dl>
        <h4>案件时间线</h4>
        <ol class="timeline-detail">${(d.timeline || []).map((t) => {
          let detail = t.details;
          try { detail = JSON.stringify(JSON.parse(t.details)); } catch (_) {}
          return `<li>${esc(t.created_at)} ${esc(t.actor)} <strong>${esc(t.action)}</strong> ${esc(detail)}</li>`;
        }).join("")}</ol>
      </div>`;
  } catch (err) {
    box.innerHTML = `<span class="msg">${esc(err.message)}</span>`;
  }
}

async function load() {
  try {
    const s = await api("/api/state");
    if (s.access_limited) {
      flash("当前角色无权查看，请选择角色");
      return;
    }
    renderEvents(s.events || []);
    renderClaims(s.claims || []);
    renderTimeline(s.timeline || []);
  } catch (err) {
    flash(err.message);
  }
}

document.querySelector("#refresh").onclick = load;
document.querySelector("#role").onchange = load;

document.querySelector("#event-form").onsubmit = async (ev) => {
  ev.preventDefault();
  const form = new FormData(ev.target);
  const body = Object.fromEntries(form.entries());
  try {
    const r = await api("/api/events", "POST", body);
    flash(`事件 ${r.code} 已登记，${r.suspended_claims.length} 件在办案件停表`, true);
    ev.target.reset();
    load();
  } catch (err) { flash(err.message); }
};

load();
