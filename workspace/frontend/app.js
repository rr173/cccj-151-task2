/* 山火应急撤离编排系统 —— 前端（原生 JS，无构建依赖） */
"use strict";

let S = null;
// 当前上下文：{mode:'live'} 实战，或 {mode:'branch', branch:{...}} 隔离推演
let CTX = { mode: "live", branch: null };
const inBranch = () => CTX.mode === "branch" && CTX.branch;
const WAVE_COLORS = ["#5aa9ff", "#b388ff", "#ffd166", "#4cc38a", "#ff7ab0", "#7be3d4"];
const waveColor = (w) => WAVE_COLORS[(w - 1) % WAVE_COLORS.length];

const api = async (path, body) => {
  // —— 分支上下文拦截：任何实战写操作都不允许，统一转到分支或阻止 ——
  if (inBranch() && body !== undefined) {
    if (path === "/api/events") {
      // 地图/侧栏的态势变更 → 入推演队列（不触碰实战）
      path = `/api/branches/${CTX.branch.id}/events`;
      body = { ...body };
    } else if (!path.startsWith("/api/branches/")) {
      throw new Error("当前处于隔离推演上下文，实战写操作已锁定；如需改动实战请先「返回实战上下文」");
    }
  }
  const opt = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `HTTP ${r.status}`);
  return data;
};

const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

const toast = (msg, kind = "") => {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "show " + kind;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = ""; }, 3600);
};

const fmtTime = (ts) => new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });

// ---------------------------------------------------------------- 数据加载

async function refresh() {
  try {
    const prevRev = S ? S.revision : null;
    const stateUrl = inBranch()
      ? `/api/branches/${CTX.branch.id}/state` : "/api/state";
    S = await api(stateUrl);
    document.getElementById("revision").textContent = S.revision;
    const warn = document.getElementById("revWarn");
    warn.hidden = !(prevRev !== null && prevRev !== S.revision);
    renderContext();
    renderAll();
    if (document.getElementById("tab-branch").classList.contains("active") && inBranch()) {
      renderBranchConsole();  // 轻量刷新队列/时钟
    }
  } catch (e) {
    toast("无法连接服务：" + e.message, "err");
  }
}

function renderAll() {
  renderMap();
  renderCapacity();
  renderVehicles();
  renderSelectors();
  renderBatches();
  renderHouseholds();
  renderLog();
  renderPlansList(); // 轻量摘要
  renderBranchList();
}

// ---------------------------------------------------------------- 上下文标识

function renderContext() {
  const pill = document.getElementById("ctxPill");
  const bar = document.getElementById("ctxBar");
  const text = document.getElementById("ctxBarText");
  if (inBranch()) {
    const b = CTX.branch;
    document.body.classList.add("ctx-branch");
    pill.textContent = `🧪 推演 ${b.name}`;
    pill.className = "ctx-pill branch";
    bar.hidden = false;
    bar.className = "ctx-bar branch";
    text.innerHTML = `🧪 <b>隔离推演上下文：${esc(b.name)}</b>（${esc(b.id)}）` +
      ` 基线修订号 <b>${b.base_revision}</b> ｜ 当前模拟时钟 T<b>${b.tick ?? S?.revision ?? 0}</b>` +
      ` ｜ 分支世界修订号 <b>${S?.revision ?? "–"}</b><br>` +
      `<span class="muted">此处所有写入仅作用于分支副本，不会污染实战；态势变更将进入推演队列。` +
      `人员去向/资源余量/受阻原因可在「推演沙盘」并排对比。</span>`;
  } else {
    document.body.classList.remove("ctx-branch");
    pill.textContent = "实战 LIVE";
    pill.className = "ctx-pill live";
    bar.hidden = true;
    bar.className = "ctx-bar live";
    text.textContent = "";
  }
}

function enterBranch(branch) {
  CTX = { mode: "branch", branch };
  document.querySelectorAll('[data-tab="branch"]').forEach(t => t.classList.add("has-branch"));
  switchTab("branch");
  document.getElementById("branchConsole").hidden = false;
  toast(`已进入隔离推演：${branch.name}`, "ok");
  refresh();
  loadBranchDiff();
}

function exitBranch() {
  CTX = { mode: "live", branch: null };
  document.getElementById("branchConsole").hidden = true;
  selectedBranchId = null;
  toast("已返回实战上下文", "ok");
  refresh();
}

document.getElementById("exitBranchBtn").addEventListener("click", exitBranch);

// ---------------------------------------------------------------- 地图

function nodeMap() {
  const m = {};
  S.grids.forEach(g => m[g.code] = { ...g, kind: "grid" });
  S.shelters.forEach(s => m[s.code] = { ...s, kind: "shelter" });
  return m;
}

function hhCountByGrid() {
  const cnt = {};
  S.households.forEach(h => {
    const st = S.household_state[h.ref];
    if (st && st.status !== "departed") cnt[h.grid] = (cnt[h.grid] || 0) + 1;
  });
  return cnt;
}

function renderMap() {
  const svg = document.getElementById("map");
  const nodes = nodeMap();
  const waiting = hhCountByGrid();
  let html = "";

  // 道路
  S.roads.forEach(r => {
    const a = nodes[r.a], b = nodes[r.b];
    if (!a || !b) return;
    html += `<line class="road ${r.status}" data-road="${esc(r.code)}"
      x1="${a.x}" y1="${a.y}" x2="${b.x}" y2="${b.y}">
      <title>${esc(r.code)} ${esc(r.a)}↔${esc(r.b)} 容量${r.capacity}/批次 — ${r.status === "open" ? "开放（点击封闭）" : "已封闭（点击恢复）"}</title></line>`;
  });

  // 批次路线（已承诺实线，待出发虚线）
  const wavesSeen = new Set();
  S.batches.forEach(b => {
    if (b.status === "cancelled") return;
    wavesSeen.add(b.wave);
    const pts = b.route.nodes.map(n => nodes[n]).filter(Boolean);
    if (pts.length < 2) return;
    const d = pts.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join("");
    const dash = b.status === "departed" ? "" : `stroke-dasharray=".18 .12"`;
    html += `<path class="route-line" d="${d}" stroke="${waveColor(b.wave)}"
      stroke-width="${b.status === "departed" ? .09 : .06}" ${dash}/>`;
  });
  document.getElementById("waveLegend").innerHTML = [...wavesSeen].sort().map(w =>
    `<span><i class="line-leg" style="border-top-color:${waveColor(w)}"></i>第${w}波</span>`
  ).join("");

  // 网格节点
  S.grids.forEach(g => {
    const n = waiting[g.code] || 0;
    const fill = g.threatened ? "#ff7a3d" : "#4c6b8f";
    html += `<circle class="node-circle" cx="${g.x}" cy="${g.y}" r=".28" fill="${fill}"/>`;
    if (g.threatened)
      html += `<text class="fire-icon" x="${g.x}" y="${g.y - .42}">🔥</text>`;
    html += `<text class="node-label" x="${g.x}" y="${g.y + .52}">${esc(g.code)} ${esc(g.name)}</text>`;
    if (n) html += `<text class="node-sub" x="${g.x}" y="${g.y + .78}">待撤离 ${n} 户</text>`;
  });

  // 避难点
  S.shelters.forEach(s => {
    const cap = S.capacity[s.code];
    const suspended = s.status !== "active";
    const fill = suspended ? "#6b7280" : "#4cc38a";
    html += `<rect class="shelter-rect" x="${s.x - .26}" y="${s.y - .26}"
      width=".52" height=".52" rx=".08" fill="${fill}"/>`;
    html += `<text class="node-label" x="${s.x}" y="${s.y - .42}">${esc(s.code)} ${esc(s.name)}</text>`;
    const sub = suspended ? "已暂停接收"
      : `床 ${cap.committed}用/${cap.reserved}留/${cap.remaining}余`;
    html += `<text class="node-sub" x="${s.x}" y="${s.y + .52}">${esc(sub)}</text>`;
  });

  svg.innerHTML = html;
  svg.querySelectorAll(".road").forEach(el => {
    el.addEventListener("click", async () => {
      const code = el.dataset.road;
      const road = S.roads.find(r => r.code === code);
      const type = road.status === "open" ? "road_close" : "road_open";
      try {
        await api("/api/events", { type, code });
        toast(inBranch()
          ? `道路 ${code} 变更已进入推演队列（下个时钟生效，实战不受影响）`
          : `道路 ${code} 已${type === "road_close" ? "封闭，仅撤回未出发批次" : "恢复，可继续编排"}`,
          "ok");
        refresh();
      } catch (e) { toast(e.message, "err"); }
    });
  });
}

// ---------------------------------------------------------------- 侧栏

function renderCapacity() {
  const box = document.getElementById("capList");
  box.innerHTML = Object.values(S.capacity).map(c => {
    const used = c.committed + c.reserved;
    const statusTag = c.status === "active"
      ? '<span class="tag ok">接收中</span>'
      : '<span class="tag bad">已暂停</span>';
    const flags = [c.accessible ? "♿" : "", c.pets_allowed ? "🐾" : ""]
      .filter(Boolean).join(" ");
    return `<div class="cap-row">
      <div>${esc(c.name)} ${statusTag} <span class="muted">${flags}</span></div>
      <div class="cap-bar">
        <div class="cap-committed" style="width:${100 * c.committed / c.beds}%"></div>
        <div class="cap-reserved" style="width:${100 * c.reserved / c.beds}%"></div>
        <div class="cap-free"></div>
      </div>
      <div class="muted">总 ${c.beds} ｜ 已承诺 ${c.committed} ｜ 已预留 ${c.reserved} ｜ 剩余 ${c.remaining}</div>
    </div>`;
  }).join("");
}

function renderVehicles() {
  const box = document.getElementById("vehList");
  const label = { available: ["ok", "可用"], down: ["bad", "故障"], departed: ["warn", "已离场"] };
  box.innerHTML = S.vehicles.map(v => {
    const [cls, name] = label[v.status] || ["", v.status];
    return `<div class="cap-row">🚐 ${esc(v.name)} <span class="muted">(${esc(v.code)})</span>
      ${v.accessible ? '♿' : ""} ${v.seats}座
      <span class="tag ${cls}">${name}</span></div>`;
  }).join("");
}

function renderSelectors() {
  const fill = (id, opts, current) => {
    const el = document.getElementById(id);
    if (el.dataset.built === "1") {
      el.value = current || el.value; return;
    }
    el.innerHTML = opts;
    el.dataset.built = "1";
  };
  fill("roadSel", S.roads.map(r =>
    `<option value="${esc(r.code)}">${esc(r.code)} ${esc(r.a)}↔${esc(r.b)}（${r.status === "open" ? "开放" : "封闭"}）</option>`).join(""));
  fill("vehSel", S.vehicles.map(v =>
    `<option value="${esc(v.code)}">${esc(v.code)} ${esc(v.name)}（${v.status}）</option>`).join(""));
  fill("shelSel", S.shelters.map(s =>
    `<option value="${esc(s.code)}">${esc(s.code)} ${esc(s.name)}（${s.status === "active" ? "接收" : "暂停"}）</option>`).join(""));
  fill("gridSel", S.grids.map(g =>
    `<option value="${esc(g.code)}">${esc(g.code)} ${esc(g.name)}（${g.threatened ? "受威胁" : "安全"}）</option>`).join(""));
}

// ---------------------------------------------------------------- 批次页

function batchChip(b) {
  const v = S.vehicles.find(x => x.code === b.vehicle) || {};
  const sh = S.shelters.find(x => x.code === b.shelter) || {};
  const hhs = b.households.map(ref => {
    const h = S.households.find(x => x.ref === ref) || {};
    return `${esc(h.name || ref)}(${h.members}人${h.mobility ? "♿" : ""}${h.pets ? "🐾" + h.pets : ""})`;
  }).join("、");
  const btn = b.status === "scheduled"
    ? `<button class="mini publish depart-live" onclick="depart('${esc(b.id)}')">确认车辆离场</button>`
    : b.status === "departed"
      ? `<span class="pill departed">✓ 已离场·已承诺</span>`
      : `<span class="pill cancelled">已撤回</span>`;
  return `<div class="batch-chip ${b.status}">
    <div><b>${esc(v.name || b.vehicle)}</b> <span class="muted">${esc(b.vehicle)}${v.accessible ? " ♿" : ""}</span></div>
    <div>${esc(b.grid)} → ${esc(sh.name || b.shelter)}</div>
    <div class="route">经 ${b.route.edges.map(esc).join(" → ")}</div>
    <div class="muted">${hhs || "（无家庭，已撤回待重排）"}</div>
    <div style="margin-top:6px">${btn}
      <span class="muted">${b.departed_at ? fmtTime(b.departed_at) : ""}</span></div>
  </div>`;
}

function renderBatches() {
  const area = document.getElementById("batchArea");
  const active = S.batches.filter(b => b.status !== "cancelled");
  const departed = active.filter(b => b.status === "departed");
  const scheduled = active.filter(b => b.status === "scheduled");

  let html = "";
  if (departed.length) {
    html += `<div class="commit-note">已离场 ${departed.length} 个批次为<b>不可变承诺</b>：
      家庭不会被改派，目的地床位持续占用，车辆不再参与编排。</div>`;
    html += `<div class="wave-block"><div class="wave-title">🚍 已承诺（已离场）</div>
      ${departed.map(batchChip).join("")}</div>`;
  }
  if (scheduled.length) {
    const waves = {};
    scheduled.forEach(b => (waves[b.wave] = waves[b.wave] || []).push(b));
    Object.keys(waves).map(Number).sort((a, c) => a - c).forEach(w => {
      html += `<div class="wave-block">
        <div class="wave-title" style="color:${waveColor(w)}">第 ${w} 波 · ${waves[w].length} 车待发</div>
        ${waves[w].map(batchChip).join("")}</div>`;
    });
  }
  if (!active.length) {
    html = `<p class="muted">当前没有生效中的撤离令。请到「方案比较与发布」生成并发布方案。
      受威胁待安排家庭：<b>${S.pending_households.length}</b> 户。</p>`;
  }
  area.innerHTML = html;
}

window.depart = async (id) => {
  if (!confirm("确认该批次车辆已离场？\n离场后批次与床位占用立即成为承诺，不可再改派或释放。")) return;
  try {
    const r = await api(`/api/batches/${id}/depart`, {});
    toast(`批次已离场并锁定，撤回同车后续批次 ${r.released_for_replan.length} 户待重排`, "ok");
    refresh();
  } catch (e) { toast(e.message, "err"); }
};

// ---------------------------------------------------------------- 方案比较

let currentPlanId = null;

async function generatePlan() {
  try {
    const commander = document.getElementById("commander").value.trim();
    const p = await api("/api/plan", { name: `方案 ${new Date().toLocaleTimeString("zh-CN")}`, commander });
    showPlanDetail(p, true);
    toast(p.feasible ? `已生成可行方案：${p.batches.length} 个批次 / ${p.max_wave} 个波次`
      : "已生成方案，但存在无法安置的家庭", p.feasible ? "ok" : "err");
    switchTab("plans");
    refresh();
  } catch (e) { toast(e.message, "err"); }
}

async function renderPlansList() {
  const box = document.getElementById("planCompare");
  let plans;
  try { plans = await api("/api/plans"); } catch { return; }
  if (inBranch()) {
    box.innerHTML = `<div class="commit-note">🧪 当前为隔离推演上下文：此处展示的仍是<b>实战</b>候选方案，且发布已锁定。
      推演的调度由「推演沙盘」每个模拟时钟自动生成与执行，请在沙盘中查看逐时钟结果。</div>`;
    return;
  }
  if (!plans.length) {
    box.innerHTML = `<p class="muted">尚无候选方案。点击右上角「生成候选方案」，系统将只对
      <b>受威胁网格中尚未出发</b>的家庭求解；已出发承诺保持不变。可多次生成后比较再发布。</p>`;
    return;
  }
  box.innerHTML = `<div class="plan-cards">${plans.map(p => {
    const s = p.summary;
    const stale = p.base_revision !== S.revision && p.status === "candidate";
    return `<div class="plan-card ${p.status === "published" ? "published" : ""} ${!s.feasible ? "infeasible" : ""}"
      onclick="openPlan('${esc(p.id)}')">
      <div class="row-between"><b>${esc(p.name)}</b>
        ${p.status === "published" ? '<span class="tag bad">已生效</span>'
          : stale ? '<span class="tag warn">已过期</span>'
          : '<span class="tag ok">可发布</span>'}</div>
      <div class="muted">${esc(p.commander || "未署名")} · ${fmtTime(p.created_at)}</div>
      <div class="metric"><span>状态</span><b>${s.feasible ? "可行" : "含不可安置户"}</b></div>
      <div class="metric"><span>批次 / 波次</span><b>${s.batch_count} / ${s.max_wave ?? "–"}</b></div>
      <div class="metric"><span>覆盖家庭</span><b>${s.totals.households_assigned} / ${s.totals.households_planned}</b></div>
      <div class="metric"><span>使用车辆</span><b>${s.totals.vehicles_used}</b></div>
      <div class="metric"><span>依据修订号</span><b>${p.base_revision}${stale ? "（旧）" : ""}</b></div>
    </div>`;
  }).join("")}</div>`;
}

window.openPlan = async (id) => {
  try {
    const p = await api(`/api/plans/${id}`);
    showPlanDetail({ ...p.payload, id: p.id, name: p.name, commander: p.commander,
      status: p.status, base_revision: p.base_revision }, false);
  } catch (e) { toast(e.message, "err"); }
};

function showPlanDetail(p, justCreated) {
  currentPlanId = p.id;
  document.getElementById("planDetailCard").hidden = false;
  document.getElementById("planDetailTitle").textContent =
    `方案详情 — ${p.name || p.id}（修订号 ${p.base_revision ?? "?"}）`;
  document.getElementById("publishBtn").disabled =
    p.status === "published" || !p.feasible;
  document.getElementById("publishMsg").innerHTML = p.status === "published"
    ? '<div class="commit-note">该方案为当前<b>已生效</b>撤离令。</div>' : "";

  let html = `<div class="metric"><span>可行性</span><b>${p.feasible ? "✅ 全部家庭可撤离" : "❌ 存在无法安置家庭"}</b></div>
    <div class="metric"><span>批次数 / 出发波次</span><b>${p.batches.length} / ${p.max_wave ?? "—"}</b></div>`;

  if (p.batches.length) {
    const waves = {};
    p.batches.forEach(b => (waves[b.wave] = waves[b.wave] || []).push(b));
    html += Object.keys(waves).map(Number).sort((a, c) => a - c).map(w =>
      `<div class="wave-block"><div class="wave-title" style="color:${waveColor(w)}">第 ${w} 波</div>
      ${waves[w].map(b => `<div class="batch-chip scheduled">
        <div><b>${esc(b.vehicle_name)}</b> <span class="muted">${esc(b.vehicle)}</span></div>
        <div>${esc(b.grid)} → ${esc(b.shelter_name)}</div>
        <div class="route">经 ${b.route_edges.map(esc).join(" → ")}（${b.distance}）</div>
        <div class="muted">座位 ${b.seats_used}/${b.seats_total} ｜
          ${b.households.map(h => esc(h.name || h.ref)).join("、")}</div>
      </div>`).join("")}</div>`).join("");
  }

  if (p.unassigned.length) {
    html += `<div class="unassigned-box"><h4>⚠ 无法安置的 ${p.unassigned.length} 户及具体约束</h4>` +
      p.unassigned.map(u => `<div><b>${esc(u.name || u.ref)}</b>（${esc(u.grid)}，${u.members}人${u.mobility ? "，行动辅助" : ""}${u.pets ? "，宠物" + u.pets : ""}）
        ${u.reasons.map(r => `<div class="reason">• ${esc(r)}</div>`).join("")}</div>`).join("") +
      `<div class="muted" style="margin-top:6px">资源恢复（道路重开、车辆修复、避难点恢复、床位释放）后可再次生成方案继续编排。</div></div>`;
  }

  html += `<h3>发布后床位预测</h3><div>` + Object.values(p.capacity).map(c =>
    `<div class="cap-row">${esc(c.name)}：
      <span class="muted">总${c.beds} · 已承诺${c.committed} · 本方案预留${c.reserved} · 余${c.remaining}</span></div>`
  ).join("") + `</div>`;

  document.getElementById("planDetail").innerHTML = html;
}

document.getElementById("publishBtn").addEventListener("click", async () => {
  if (!currentPlanId) return;
  const commander = document.getElementById("commander").value.trim();
  try {
    const r = await api(`/api/plans/${currentPlanId}/publish`, { commander });
    document.getElementById("publishMsg").innerHTML =
      `<div class="commit-note">✅ 分区撤离令已发布（${r.batches} 个批次）。
        旧的未出发批次已被替换；已出发批次与床位占用不变。修订号 ${r.revision}。</div>`;
    toast("撤离令已生效", "ok");
    refresh();
    renderPlansList();
  } catch (e) {
    document.getElementById("publishMsg").innerHTML =
      `<div class="unassigned-box"><h4>发布被拒绝</h4><div class="reason">${esc(e.message)}</div>
      <div class="muted">这是乐观并发保护：已有其他指挥员发布，或路况/资源在此期间变化。
      请重新生成方案、比较后再发布。</div></div>`;
    toast(e.message, "err");
  }
});
document.getElementById("closeDetail").addEventListener("click",
  () => { document.getElementById("planDetailCard").hidden = true; });

// ---------------------------------------------------------------- 住户

function renderHouseholds() {
  const gridSel = document.getElementById("hGrid");
  if (gridSel.dataset.built !== "1") {
    gridSel.innerHTML = S.grids.map(g =>
      `<option value="${esc(g.code)}">${esc(g.code)} ${esc(g.name)}</option>`).join("");
    gridSel.dataset.built = "1";
  }
  const stLabel = {
    unplanned: ['<span class="tag warn">待安排</span>', ""],
    scheduled: ['<span class="tag ok">已排批次</span>', ""],
    departed: ['<span class="tag bad">已撤离·承诺</span>', ""],
  };
  const rowsHtml = S.households.map(h => {
    const st = S.household_state[h.ref] || { status: "unplanned" };
    const g = S.grids.find(x => x.code === h.grid) || {};
    return `<tr>
      <td>${esc(h.ref)}</td><td>${esc(h.name)}</td>
      <td>${esc(h.grid)} ${esc(g.name || "")} ${g.threatened ? "🔥" : ""}</td>
      <td>${h.members}</td>
      <td>${h.mobility ? "♿ 需要" : "—"}</td>
      <td>${h.pets || "—"}</td>
      <td>${esc(h.contact)}</td>
      <td>${(stLabel[st.status] || [""])[0]} ${st.shelter ? `<span class="muted">→${esc(st.shelter)} 波次${st.wave}</span>` : ""}</td>
    </tr>`;
  }).join("");
  document.getElementById("hhTable").innerHTML =
    `<tr><th>户号</th><th>称呼</th><th>网格</th><th>人数</th><th>行动辅助</th><th>宠物</th><th>联络</th><th>状态</th></tr>${rowsHtml}`;
}

document.getElementById("hAdd").addEventListener("click", async () => {
  const body = {
    ref: document.getElementById("hRef").value.trim(),
    name: document.getElementById("hName").value.trim(),
    grid: document.getElementById("hGrid").value,
    members: Number(document.getElementById("hMembers").value),
    mobility: document.getElementById("hMobility").checked ? 1 : 0,
    pets: Number(document.getElementById("hPets").value),
    contact: document.getElementById("hContact").value.trim(),
  };
  if (!body.ref) return toast("户号必填", "err");
  try {
    const r = await api("/api/households", body);
    toast(r.skipped_duplicates ? `户号已存在，跳过重复登记（新增 ${r.inserted}）`
      : `已登记（修订号 ${r.revision}）`, r.skipped_duplicates ? "" : "ok");
    ["hRef", "hName", "hContact"].forEach(i => document.getElementById(i).value = "");
    refresh();
  } catch (e) { toast(e.message, "err"); }
});

// ---------------------------------------------------------------- 资源导入

const TEMPLATES = {
  grids: `[\n  {"code":"G7","name":"东岭","x":2.0,"y":4.6}\n]`,
  roads: `[\n  {"code":"r12","a":"G3","b":"G5","length":2.6,"capacity":1}\n]`,
  shelters: `[\n  {"code":"S5","name":"南山中心","x":6.0,"y":4.8,"beds":50,"accessible":1,"pets_allowed":1}\n]`,
  vehicles: `[\n  {"code":"V6","name":"中巴-06","seats":10,"accessible":0}\n]`,
};
document.getElementById("importKind").addEventListener("change", (e) => {
  document.getElementById("importJson").value = TEMPLATES[e.target.value];
});
document.getElementById("importJson").value = TEMPLATES.grids;
document.getElementById("importHelp").innerHTML =
  "字段说明：<br>• 网格：code,name,x,y<br>• 道路：code,a,b,length,capacity(每批次可过车辆数)<br>" +
  "• 避难点：code,name,x,y,beds,accessible,pets_allowed<br>• 车辆：code,name,seats,accessible<br>" +
  "同 code 重复导入执行覆盖更新，不会产生重复网格/道路/床位/车辆。";

document.getElementById("importBtn").addEventListener("click", async () => {
  const kind = document.getElementById("importKind").value;
  let items;
  try { items = JSON.parse(document.getElementById("importJson").value); }
  catch { return toast("JSON 格式错误", "err"); }
  try {
    const r = await api(`/api/import/${kind}`, items);
    toast(`已导入 ${r.imported} 条 ${kind}（修订号 ${r.revision}）`, "ok");
    document.querySelectorAll("#roadSel,#vehSel,#shelSel,#gridSel").forEach(el => el.dataset.built = "");
    refresh();
  } catch (e) { toast(e.message, "err"); }
});

// ---------------------------------------------------------------- 事件

const bindEvent = (btnId, selId, type, okMsg) => {
  document.getElementById(btnId).addEventListener("click", async () => {
    const code = document.getElementById(selId).value;
    try {
      const r = await api("/api/events", { type, code });
      toast(inBranch()
        ? `推演事件已入队（${okMsg(code)}，下个时钟生效；实战不受影响）`
        : okMsg(code) + `（撤回未出发 ${r.released_for_replan.length} 户待重排；已出发不受影响）`,
        "ok");
      document.querySelectorAll("#roadSel,#vehSel,#shelSel,#gridSel").forEach(el => el.dataset.built = "");
      refresh();
    } catch (e) { toast(e.message, "err"); }
  });
};
bindEvent("roadCloseBtn", "roadSel", "road_close", c => `道路 ${c} 已封闭`);
bindEvent("roadOpenBtn", "roadSel", "road_open", c => `道路 ${c} 已恢复，可继续编排`);
bindEvent("vehDownBtn", "vehSel", "vehicle_down", c => `车辆 ${c} 已故障`);
bindEvent("vehRecoverBtn", "vehSel", "vehicle_recovered", c => `车辆 ${c} 已恢复`);
bindEvent("shelSuspendBtn", "shelSel", "shelter_suspend", c => `避难点 ${c} 暂停接收`);
bindEvent("shelResumeBtn", "shelSel", "shelter_resume", c => `避难点 ${c} 恢复接收`);
bindEvent("fireClearBtn", "gridSel", "fire_clear", c => `网格 ${c} 解除威胁`);
document.getElementById("fireBtn").addEventListener("click", async () => {
  const code = document.getElementById("gridSel").value;
  const closeRoads = prompt("火线扩大，可同时封闭道路（输入道路编号，逗号分隔；留空不封路）", "");
  if (closeRoads === null) return;
  const body = { type: "fire_expand", code,
    close_roads: closeRoads.split(",").map(s => s.trim()).filter(Boolean) };
  try {
    const r = await api("/api/events", body);
    toast(inBranch()
      ? `推演事件已入队：火线扩大至 ${code}（下个时钟生效）`
      : `火线扩大至 ${code}；新增受威胁家庭将进入待编排，撤回未出发 ${r.released_for_replan.length} 户`, "ok");
    document.querySelectorAll("#roadSel,#gridSel").forEach(el => el.dataset.built = "");
    refresh();
  } catch (e) { toast(e.message, "err"); }
});

// ---------------------------------------------------------------- 日志

function renderLog() {
  const note = inBranch()
    ? `<div class="commit-note">🧪 隔离推演上下文：此处仍为<b>实战</b>审计流；
        分支自身的逐时钟与提升审计见「推演沙盘 → 分支审计」。</div>` : "";
  document.getElementById("logList").innerHTML = note + S.events.slice().reverse().map(e => {
    let detail = "";
    try { detail = JSON.stringify(JSON.parse(e.detail), null, 0); } catch { detail = e.detail; }
    return `<div class="logline"><span class="ts">${fmtTime(e.ts)}</span>
      <span class="kind">${esc(e.kind)}</span><span class="muted">r${e.revision}</span> ${esc(detail)}</div>`;
  }).join("");
}

document.getElementById("resetBtn").addEventListener("click", async () => {
  if (!confirm("重置将清空全部命令、批次与登记并恢复演示数据，确定？")) return;
  await api("/api/reset", {});
  toast("已重置为演示数据", "ok");
  refresh();
});

// ---------------------------------------------------------------- 推演沙盘

let selectedBranchId = null;
let BR_DIFF = null;

async function renderBranchList() {
  const box = document.getElementById("branchList");
  if (!box) return;
  let branches;
  try { branches = await api("/api/branches"); } catch { return; }
  if (!branches.length) {
    box.innerHTML = `<p class="muted">尚无推演分支。填写名称后从当前实战创建一个隔离副本。</p>`;
    return;
  }
  box.innerHTML = branches.map(b => {
    const stCls = { active: "ok", promoted: "bad", discarded: "warn" }[b.status] || "";
    const stName = { active: "推演中", promoted: "已提升", discarded: "已废弃" }[b.status];
    const stale = b.status === "active" && b.stale
      ? '<span class="tag warn">基线已过期</span>' : "";
    return `<div class="plan-card ${b.status === selectedBranchId ? "selected" : ""}"
      onclick="openBranch('${esc(b.id)}')">
      <div class="row-between"><b>🧪 ${esc(b.name)}</b>
        <span class="tag ${stCls}">${stName}</span>${stale}</div>
      <div class="muted">${esc(b.commander || "值班员")} · ${fmtTime(b.created_at)}</div>
      <div class="metric"><span>创建基线 / 当前实战修订号</span>
        <b>${b.base_revision} / ${b.live_revision}</b></div>
      <div class="metric"><span>模拟时钟</span><b>T${b.tick}</b></div>
    </div>`;
  }).join("");
}

document.getElementById("brCreateBtn").addEventListener("click", async () => {
  const name = document.getElementById("brName").value.trim() || `推演 ${new Date().toLocaleTimeString("zh-CN")}`;
  const commander = document.getElementById("brCommander").value.trim();
  try {
    const b = await api("/api/branches", {
      name, commander, idempotency_key: "ui-" + Date.now() });
    document.getElementById("brName").value = "";
    toast(`已创建隔离分支：${b.name}（基线修订号 ${b.base_revision}）`, "ok");
    selectedBranchId = b.id;
    enterBranch(b);
  } catch (e) { toast(e.message, "err"); }
});

window.openBranch = async (id) => {
  try {
    const b = await api(`/api/branches/${id}`);
    selectedBranchId = id;
    if (b.status === "discarded") {
      // 废弃分支：留在实战上下文，仅展示只读信息（态势/差异已不可用）
      CTX = { mode: "live", branch: null };
      document.getElementById("branchConsole").hidden = false;
      renderContext();
      renderBranchConsole();
      document.getElementById("brDiff").innerHTML =
        '<p class="muted">分支已废弃，其推演世界已清除；审计记录仍可查看。</p>';
      return;
    }
    enterBranch(b);
  } catch (e) { toast(e.message, "err"); }
};

function fillBranchEventCodes() {
  const type = document.getElementById("brEvType").value;
  const sel = document.getElementById("brEvCode");
  const val = document.getElementById("brEvVal");
  const opts = type.startsWith("road")
    ? S.roads.map(r => `<option value="${esc(r.code)}">${esc(r.code)} ${esc(r.a)}↔${esc(r.b)}</option>`)
    : type.startsWith("vehicle")
      ? S.vehicles.map(v => `<option value="${esc(v.code)}">${esc(v.code)} ${esc(v.name)}</option>`)
      : type.startsWith("shelter")
        ? S.shelters.map(s => `<option value="${esc(s.code)}">${esc(s.code)} ${esc(s.name)}</option>`)
        : S.grids.map(g => `<option value="${esc(g.code)}">${esc(g.code)} ${esc(g.name)}</option>`);
  sel.innerHTML = opts.join("");
  val.hidden = !(type === "road_set_capacity" || type === "shelter_set_beds");
  if (type === "road_set_capacity") val.value = "2";
  if (type === "shelter_set_beds") val.value = "100";
}
document.getElementById("brEvType").addEventListener("change", fillBranchEventCodes);

document.getElementById("brEvAddBtn").addEventListener("click", async () => {
  if (!inBranch()) return;
  const type = document.getElementById("brEvType").value;
  const code = document.getElementById("brEvCode").value;
  const ev = { type, code, idempotency_key: "ev-" + Date.now() };
  if (type === "road_set_capacity") ev.capacity = Number(document.getElementById("brEvVal").value);
  if (type === "shelter_set_beds") ev.beds = Number(document.getElementById("brEvVal").value);
  try {
    await api(`/api/branches/${CTX.branch.id}/events`, ev);
    toast(`已入队：${type} ${code}（下个时钟生效，仅作用于分支）`, "ok");
    renderBranchConsole();
  } catch (e) { toast(e.message, "err"); }
});

document.getElementById("brTickBtn").addEventListener("click", async () => {
  if (!inBranch()) return;
  const n = Math.max(1, Number(document.getElementById("brTickN").value || 1));
  try {
    const r = await api(`/api/branches/${CTX.branch.id}/tick`, {
      target_tick: (CTX.branch.tick || 0) + n });
    CTX.branch.tick = r.tick;
    toast(`已推进到 T${r.tick}`, "ok");
    await refresh();
    renderBranchConsole();
    loadBranchDiff();
  } catch (e) { toast(e.message, "err"); }
});

document.getElementById("brDiffBtn").addEventListener("click", loadBranchDiff);
document.getElementById("brCheckBtn").addEventListener("click", () => promotionCheck(true));
document.getElementById("brPromoteBtn").addEventListener("click", promoteBranch);
document.getElementById("brDiscardBtn").addEventListener("click", discardBranch);

function renderBranchConsole() {
  if (!selectedBranchId) return;
  (async () => {
    let b;
    try { b = await api(`/api/branches/${selectedBranchId}`); }
    catch { return; }
    if (CTX.mode === "branch" && CTX.branch.id === b.id) {
      CTX.branch = b; renderContext();
    }
    document.getElementById("brTitle").textContent = `分支控制台 — ${b.name}`;
    document.getElementById("brMeta").innerHTML =
      `状态 <b class="ctx-text-${b.status}">${({active:"推演中",promoted:"已提升",discarded:"已废弃"})[b.status]}</b>` +
      ` ｜ 创建基线修订号 <b>${b.base_revision}</b> ｜ 实战当前 <b>${b.live_revision}</b>` +
      ` ｜ 时钟 <b>T${b.tick}</b>` + (b.live_revision !== b.base_revision
        ? ' ｜ <span class="tag warn">基线已过期：实战已推进，提升将被拒绝</span>' : "");
    const readonly = b.status !== "active";
    ["brTickBtn", "brEvAddBtn", "brPromoteBtn"].forEach(id =>
      document.getElementById(id).disabled = readonly);
    fillBranchEventCodes();
    const pending = b.pending_events || [];
    document.getElementById("brPending").innerHTML = pending.length
      ? "待执行事件：" + pending.map(e =>
          `<span class="tag warn">T${e.at_tick} ${esc(e.type)} ${esc(e.code || "")}</span>`).join(" ")
      : "待执行事件：无（可在左侧注入，或直接推进模拟）";
    const steps = (b.last_tick_result && b.last_tick_result.steps) || [];
    document.getElementById("brSteps").innerHTML = steps.length
      ? steps.slice().reverse().map(s => `<div class="wave-block">
        <div class="wave-title">T${s.tick} ${s.feasible ? "✅ 可调度" : "❌ 受阻"}
          <span class="muted">｜事件 ${s.events.map(e => esc(e.type)).join("、") || "无"}
          ｜批次 ${s.batch_count}｜离场 ${s.departed_batch_count}｜未安置 ${s.unassigned.length}</span></div>
        ${s.unassigned.length ? s.unassigned.map(u =>
          `<div class="reason"><b>${esc(u.ref)}</b>：${esc((u.reasons || [""])[0])}</div>`).join("") : ""}
      </div>`).join("")
      : '<p class="muted">尚未推进模拟。</p>';
    loadBranchAudit();
  })();
}

async function loadBranchDiff() {
  if (!selectedBranchId) return;
  const box = document.getElementById("brDiff");
  try {
    const d = await api(`/api/branches/${selectedBranchId}/diff`);
    BR_DIFF = d;
    const hhName = (ref) => {
      const h = (S && S.households.find(x => x.ref === ref)) || {};
      return h.name || ref;
    };
    const dest = (x) => !x ? "–" :
      ({ unplanned: "待安排", scheduled: `已排→${x.shelter || ""}（波${x.wave ?? "–"}）`,
         departed: `已离场→${x.shelter || ""}（波${x.wave ?? "–"}）` })[x.status] || x.status;
    let html = `<div class="metric"><span>差异计数</span><b>人员 ${d.counts.people_diff} ·
      资源 ${d.counts.resource_diff} · 受阻 ${d.counts.blocked_diff} · 可提升 ${d.counts.promotable}</b></div>`;

    html += `<h3>可提升的资源改动（三方预检）</h3>` + (d.promotable_changes.length
      ? `<table class="grid-table"><tr><th>对象</th><th>字段</th><th>基线</th>
          <th>推演</th><th>实战当前</th><th>结论</th></tr>` +
        d.promotable_changes.map(c => `<tr>
          <td>${esc(c.kind)}/${esc(c.code)}</td><td>${esc(c.field)}</td>
          <td>${esc(c.baseline)}</td><td><b>${esc(c.branch)}</b></td>
          <td>${esc(c.live)}</td>
          <td>${c.conflicts_with_live ? '<span class="tag bad">冲突</span>'
            : '<span class="tag ok">可应用</span>'}</td></tr>`).join("") + "</table>"
      : '<p class="muted">无相对基线的净资源改动（中断均已恢复或尚未设置）。</p>');

    html += `<h3>人员去向差异（分支 vs 实战）</h3>` + (d.people_destinations.length
      ? `<table class="grid-table"><tr><th>户</th><th>实战</th><th>推演</th></tr>` +
        d.people_destinations.map(p => `<tr><td>${esc(p.ref)} ${esc(p.name || "")}</td>
          <td>${esc(dest(p.live))}</td><td><b>${esc(dest(p.branch))}</b></td></tr>`).join("")
        + "</table>"
      : '<p class="muted">人员去向一致。</p>');

    html += `<h3>资源余量差异</h3>` + (d.resource_remaining.length
      ? `<table class="grid-table"><tr><th>资源</th><th>实战</th><th>推演</th></tr>` +
        d.resource_remaining.map(r => `<tr><td>${esc(r.kind)}/${esc(r.code)} ${esc(r.name || "")}</td>
          <td>${esc(JSON.stringify(r.live))}</td><td><b>${esc(JSON.stringify(r.branch))}</b></td></tr>`
        ).join("") + "</table>"
      : '<p class="muted">资源余量一致。</p>');

    html += `<h3>受阻原因差异</h3>` + (d.blocked_reasons.length
      ? d.blocked_reasons.map(x => `<div class="reason"><b>${esc(x.ref)}</b><br>
          <span class="muted">实战：</span>${esc((x.live || []).join("；") || "无/已安置")}<br>
          <span class="muted">推演：</span>${esc((x.branch || []).join("；") || "无/已安置")}</div>`
        ).join("")
      : '<p class="muted">当前态势下受阻原因一致（或均已撤离）。</p>');
    box.innerHTML = html;
  } catch (e) { box.innerHTML = `<p class="reason">${esc(e.message)}</p>`; }
}

async function promotionCheck(verbose) {
  try {
    const r = await fetch(`/api/branches/${selectedBranchId}/promotion-check`);
    const info = await r.json();
    const box = document.getElementById("brPromote");
    if (r.ok && info.ok) {
      box.innerHTML = `<div class="commit-note">✅ 预检通过：可提升 ${info.changes.length} 项资源改动，
        基线修订号 ${info.base_revision} 与实战一致。<br><span class="muted">
        ${esc(info.excluded)}</span></div>`;
      if (verbose) toast("预检通过，可以提升", "ok");
    } else {
      box.innerHTML = renderReject(info);
      if (verbose) toast("预检未通过（存在冲突或基线过期）", "err");
    }
    return info;
  } catch (e) { toast(e.message, "err"); }
}

function renderReject(info) {
  const conf = (info.conflicts || []).map(c =>
    `<tr><td>${esc(c.kind)}/${esc(c.code)}</td><td>${esc(c.field)}</td>
      <td>${esc(c.baseline)}</td><td>${esc(c.live)}</td><td>${esc(c.branch)}</td>
      <td><span class="tag bad">${esc(c.reason || "冲突")}</span></td></tr>`).join("");
  return `<div class="unassigned-box"><h4>⛔ 提升被整体拒绝${info.stale ? "（基线已过期）" : "（存在三方冲突）"}</h4>
    <div class="reason">创建基线修订号 ${info.base_revision ?? "–"}，实战当前 ${info.live_revision ?? "–"}。
      任一冲突或过期都会拒绝全部改动，未写入实战。</div>
    ${conf ? `<table class="grid-table"><tr><th>对象</th><th>字段</th><th>基线</th>
      <th>实战</th><th>推演</th><th>原因</th></tr>${conf}</table>` : ""}
    <div class="muted">${esc(info.excluded || "")}</div></div>`;
}

async function promoteBranch() {
  if (!inBranch()) return;
  const info = await promotionCheck(false);
  if (!info) return;
  if (!info.ok) { toast("预检未通过，已列出冲突/过期对象与字段", "err"); return; }
  if (!confirm(`确认提升 ${info.changes.length} 项资源改动到实战？\n` +
      "仅应用资源配置；推演的命令、离场记录、已承诺占用一律不带入。")) return;
  try {
    const r = await api(`/api/branches/${selectedBranchId}/promote`, {
      idempotency_key: "prom-" + Date.now() });
    document.getElementById("brPromote").innerHTML =
      `<div class="commit-note">✅ 已提升 ${r.applied_count} 项，实战修订号 ${r.revision}。<br>
        <span class="muted">${esc(r.excluded)}</span></div>`;
    toast("提升成功", "ok");
    await renderBranchList();
    exitBranch();
  } catch (e) {
    document.getElementById("brPromote").innerHTML =
      `<div class="unassigned-box"><h4>提升被拒绝</h4><div class="reason">${esc(e.message)}</div></div>`;
    toast(e.message, "err");
  }
}

async function discardBranch() {
  if (!selectedBranchId) return;
  if (!confirm("确认废弃该推演分支？分支世界将被清除（审计与元数据保留）。")) return;
  try {
    await api(`/api/branches/${selectedBranchId}/discard`,
      { idempotency_key: "disc-" + Date.now() });
    toast("分支已废弃", "ok");
    if (inBranch()) exitBranch();
    await renderBranchList();
    document.getElementById("branchConsole").hidden = true;
  } catch (e) { toast(e.message, "err"); }
}

async function loadBranchAudit() {
  try {
    const au = await api(`/api/branches/${selectedBranchId}/audit`);
    document.getElementById("brAudit").innerHTML = au.slice(0, 30).map(e =>
      `<div class="logline"><span class="ts">${fmtTime(e.ts)}</span>
        <span class="kind">${esc(e.kind)}</span>
        <span class="muted">[${esc(e.world === "live" ? "实战" : "分支")} r${e.revision}]</span>
        ${esc(JSON.stringify(e.detail))}</div>`).join("");
  } catch { /* ignore */ }
}

// ---------------------------------------------------------------- 框架

function switchTab(name) {
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tabpanel").forEach(p =>
    p.classList.toggle("active", p.id === `tab-${name}`));
  if (name === "plans") renderPlansList();
  if (name === "branch") { renderBranchList(); if (selectedBranchId) renderBranchConsole(); }
}
document.querySelectorAll(".tab").forEach(t =>
  t.addEventListener("click", () => switchTab(t.dataset.tab)));
document.getElementById("refreshBtn").addEventListener("click", refresh);
document.getElementById("planBtn").addEventListener("click", generatePlan);
document.getElementById("replanBtn").addEventListener("click", generatePlan);

refresh();
setInterval(refresh, 6000);
