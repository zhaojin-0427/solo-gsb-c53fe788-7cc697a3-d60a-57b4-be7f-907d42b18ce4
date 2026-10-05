/* 多麦克风敲击定位台 —— 原生 JS 前端 */
"use strict";

const SVGNS = "http://www.w3.org/2000/svg";
const MAX_CAND = 12, MIN_CAND = 1;

const state = {
  mics: [],              // [{x,y}]
  calA: { pos: {x:0,y:0}, arrivals: [] },
  calB: { pos: {x:0,y:0}, arrivals: [] },
  candidates: [],        // [[{time,strength}]]
  result: null,
};

const $ = (id) => document.getElementById(id);

// OK/AMBIGUOUS 返回主结果；NO_SOLUTION 时后端附带容限外最优尝试，用于红色展示
function activeResult() {
  const r = state.result;
  if (!r) return null;
  if (r.position) return { ...r, _isAttempt: false };
  if (r.best_attempt) return { ...r.best_attempt, status: r.status,
    message: r.message, elapsed_ms: r.elapsed_ms, warnings: r.warnings,
    clock_calibration: r.clock_calibration, anchor_mics: r.anchor_mics,
    _isAttempt: true };
  return { status: r.status, message: r.message, elapsed_ms: r.elapsed_ms,
    warnings: r.warnings, _isAttempt: false };
}

// ---------------------------------------------------------------------------
// 数值输入
// ---------------------------------------------------------------------------

function num(id, fallback) {
  const v = parseFloat($(id).value);
  return Number.isFinite(v) ? v : fallback;
}

// ---------------------------------------------------------------------------
// 表单：麦克风坐标 / 校准到达时刻 / 候选峰编辑
// ---------------------------------------------------------------------------

function rebuildMicTables() {
  const n = parseInt($("micCount").value, 10);
  const old = state.mics;
  state.mics = Array.from({ length: n }, (_, i) => old[i] || { x: "", y: "" });

  const tb = $("micTable").querySelector("tbody");
  tb.innerHTML = "";
  for (let i = 0; i < n; i++) {
    const tr = document.createElement("tr");
    tr.innerHTML =
      `<td>M${i + 1}${i === 0 ? "（基准）" : ""}</td>` +
      `<td><input type="number" step="0.01" data-mic-x="${i}"></td>` +
      `<td><input type="number" step="0.01" data-mic-y="${i}"></td>`;
    tr.querySelector(`[data-mic-x="${i}"]`).value = state.mics[i].x;
    tr.querySelector(`[data-mic-y="${i}"]`).value = state.mics[i].y;
    tb.appendChild(tr);
  }
  tb.addEventListener("input", syncMicInputs, { once: true });

  for (const key of ["calA", "calB"]) {
    const body = $(key === "calA" ? "calATable" : "calBTable").querySelector("tbody");
    if (state[key].arrivals.length !== n) {
      state[key].arrivals = Array.from({ length: n },
        (_, i) => state[key].arrivals[i] ?? "");
    }
    body.innerHTML = "";
    for (let i = 0; i < n; i++) {
      const tr = document.createElement("tr");
      tr.innerHTML =
        `<td>M${i + 1}</td>` +
        `<td><input type="number" step="0.0001" data-arr="${key}:${i}"></td>`;
      tr.querySelector(`[data-arr="${key}:${i}"]`).value = state[key].arrivals[i];
      body.appendChild(tr);
    }
  }
  document.querySelectorAll('[data-arr]').forEach((el) => {
    el.addEventListener("change", () => {
      const [key, i] = el.dataset.arr.split(":");
      state[key].arrivals[+i] = parseFloat(el.value);
    });
  });

  if (state.candidates.length !== n) {
    state.candidates = Array.from({ length: n },
      (_, i) => state.candidates[i] || [{ time: "", strength: 1 }]);
  } else {
    state.candidates.length = n;
    for (let i = 0; i < n; i++) if (!state.candidates[i])
      state.candidates[i] = [{ time: "", strength: 1 }];
  }
  renderCandEditor();
}

function syncMicInputs() {
  document.querySelectorAll("[data-mic-x]").forEach((el) => {
    state.mics[+el.dataset.micX].x = parseFloat(el.value);
  });
  document.querySelectorAll("[data-mic-y]").forEach((el) => {
    state.mics[+el.dataset.micY].y = parseFloat(el.value);
  });
  $("micTable").querySelector("tbody")
    .addEventListener("input", syncMicInputs, { once: true });
}

function renderCandEditor() {
  const n = state.mics.length;
  const anchors = state.result ? state.result.anchor_mics : [1];
  const wrap = $("candEditor");
  wrap.innerHTML = "";
  for (let i = 0; i < n; i++) {
    const row = document.createElement("div");
    row.className = "cand-row" + (anchors.includes(i + 1) ? " anchor" : "");
    const tag = document.createElement("div");
    tag.className = "mic-tag";
    tag.textContent = `M${i + 1}`;
    const peaks = document.createElement("div");
    peaks.className = "cand-peaks";
    state.candidates[i].forEach((pk, j) => peaks.appendChild(peakBox(i, j, pk)));
    if (state.candidates[i].length < MAX_CAND) {
      const add = document.createElement("button");
      add.type = "button"; add.className = "add"; add.textContent = "+ 峰";
      add.addEventListener("click", () => {
        state.candidates[i].push({ time: "", strength: 1 });
        renderCandEditor();
      });
      peaks.appendChild(add);
    }
    row.append(tag, peaks);
    wrap.appendChild(row);
  }
}

function peakBox(i, j, pk) {
  const box = document.createElement("div");
  box.className = "peak-input";
  const head = document.createElement("div");
  head.className = "pk-head";
  head.innerHTML = `<span>#${j + 1}</span>`;
  if (state.candidates[i].length > MIN_CAND) {
    const del = document.createElement("button");
    del.type = "button"; del.textContent = "✕";
    del.title = "删除候选";
    del.addEventListener("click", () => {
      state.candidates[i].splice(j, 1);
      renderCandEditor();
    });
    head.appendChild(del);
  }
  const t = document.createElement("input");
  t.type = "number"; t.step = "0.0001"; t.placeholder = "时刻 s";
  t.value = pk.time === "" ? "" : pk.time;
  t.addEventListener("change", () => { pk.time = parseFloat(t.value); });
  const s = document.createElement("input");
  s.type = "number"; s.step = "0.1"; s.placeholder = "强度";
  s.value = pk.strength;
  s.addEventListener("change", () => { pk.strength = parseFloat(s.value); });
  box.append(head, t, s);
  return box;
}

// ---------------------------------------------------------------------------
// 请求组装
// ---------------------------------------------------------------------------

function buildRequest() {
  syncMicInputs();
  const room = { width: num("roomW", NaN), height: num("roomH", NaN) };
  const microphones = state.mics.map((m) => ({ x: m.x, y: m.y }));
  const cals = [
    { position: { x: num("calAx", NaN), y: num("calAy", NaN) },
      arrivals: state.calA.arrivals.map((v) => parseFloat(v)) },
    { position: { x: num("calBx", NaN), y: num("calBy", NaN) },
      arrivals: state.calB.arrivals.map((v) => parseFloat(v)) },
  ];
  const candidates = state.candidates.map((arr) =>
    arr.map((p) => ({ time: p.time, strength: p.strength })));
  return {
    room,
    microphones,
    sound_speed: num("soundSpeed", NaN),
    drift_ppm: num("driftPpm", NaN),
    tdoa_tolerance_ms: num("tolMs", NaN),
    resolution: num("resolution", NaN),
    calibrations: cals,
    target: { candidates },
  };
}

// ---------------------------------------------------------------------------
// 状态条
// ---------------------------------------------------------------------------

function setStatus(kind, msg, sub) {
  const bar = $("statusBar");
  bar.className = "status " + kind;
  bar.innerHTML = msg + (sub ? `<div class="sub">${sub}</div>` : "");
}

// ---------------------------------------------------------------------------
// SVG 房间定位图
// ---------------------------------------------------------------------------

const PAD = 40, VW = 800, VH = 600;
let viewScale = 1, viewOX = 0, viewOY = 0;

function setTransform(w, h) {
  viewScale = Math.min((VW - 2 * PAD) / w, (VH - 2 * PAD) / h);
  viewOX = (VW - viewScale * w) / 2;
  viewOY = (VH - viewScale * h) / 2;
}
const X = (x) => viewOX + x * viewScale;
const Y = (y) => viewOY + y * viewScale;

function el(name, attrs) {
  const node = document.createElementNS(SVGNS, name);
  for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
  return node;
}

function drawMap() {
  const svg = $("map");
  svg.innerHTML = "";
  const w = num("roomW", 8), h = num("roomH", 6);
  if (!(w > 0 && h > 0)) return;
  setTransform(w, h);

  // 网格（1m）
  for (let gx = 0; gx <= w + 1e-9; gx++) {
    svg.appendChild(el("line", {
      class: "grid-line", x1: X(gx), y1: Y(0), x2: X(gx), y2: Y(h),
    }));
  }
  for (let gy = 0; gy <= h + 1e-9; gy++) {
    svg.appendChild(el("line", {
      class: "grid-line", x1: X(0), y1: Y(gy), x2: X(w), y2: Y(gy),
    }));
  }
  svg.appendChild(el("rect", {
    class: "room-fill", x: X(0), y: Y(0), width: w * viewScale, height: h * viewScale,
  }));
  svg.appendChild(el("rect", {
    class: "room-border", x: X(0), y: Y(0), width: w * viewScale, height: h * viewScale,
  }));

  drawCalMarker(state.calA.pos, "A");
  drawCalMarker(state.calB.pos, "B");

  const r = activeResult();
  const anchors = r ? r.anchor_mics : [];
  state.mics.forEach((m, i) => {
    if (!Number.isFinite(m.x) || !Number.isFinite(m.y)) return;
    const c = el("circle", {
      class: "mic-circle" + (anchors.includes(i + 1) ? " mic-anchor" : ""),
      cx: X(m.x), cy: Y(m.y), r: 8,
    });
    svg.appendChild(c);
    const t = el("text", { class: "mic-label", x: X(m.x) + 10, y: Y(m.y) - 8 });
    t.textContent = `${i + 1}`;
    svg.appendChild(t);
  });

  // 候选峰：沿麦克风右侧画小色点，颜色按强度，入选加白圈
  state.candidates.forEach((arr, i) => {
    const m = state.mics[i];
    if (!m || !Number.isFinite(m.x)) return;
    const pickIdx = r && r.candidate_indices ? r.candidate_indices[i] : null;
    const times = arr.map((p) => p.time).filter(Number.isFinite);
    const strs = arr.map((p) => p.strength).filter(Number.isFinite);
    const tMin = Math.min(...times), tMax = Math.max(...times);
    const sMax = Math.max(1, ...strs);
    const span = Math.max(tMax - tMin, 1e-6);
    arr.forEach((p, j) => {
      if (!Number.isFinite(p.time)) return;
      const u = (p.time - tMin) / span;          // 0..1 沿时间
      const dx = 14 + u * 26, dy = 14 - (j % 3) * 9;
      const intensity = Math.max(0, Math.min(1, (p.strength || 0) / sMax));
      const chosen = pickIdx === j + 1;
      const dot = el("circle", {
        class: chosen ? "pick-dot" : "echo-dot",
        cx: X(m.x) + dx, cy: Y(m.y) + dy, r: chosen ? 5 : 3.5,
        fill: chosen
          ? "#37d67a"
          : `rgb(${107 + intensity * 120|0},${118 - intensity * 60|0},${137 - intensity * 80|0})`,
      });
      svg.appendChild(dot);
    });
  });

  if (r && r.position) {
    const attempt = !!r._isAttempt;
    // 残差线：入选峰残差以颜色编码画在定位点→麦克风连线上
    r.selected_peaks.forEach((sp) => {
      const m = state.mics[sp.mic - 1];
      const good = !attempt &&
        Math.abs(sp.residual_ms || 0) <= num("tolMs", 0) + 1e-9;
      svg.appendChild(el("line", {
        class: "res-line",
        x1: X(r.position.x), y1: Y(r.position.y),
        x2: X(m.x), y2: Y(m.y),
        stroke: good ? "#37d67a" : "#ff5d6c",
      }));
    });

    // 分辨率圆（仅一致解）
    if (!attempt) {
      svg.appendChild(el("circle", {
        class: "res-circle",
        cx: X(r.position.x), cy: Y(r.position.y),
        r: num("resolution", 0.05) * viewScale,
      }));
    }

    if (!attempt) {
      (r.ambiguous_positions || []).forEach((a, k) => {
        svg.appendChild(el("circle", {
          class: "loc-amb", cx: X(a.position.x), cy: Y(a.position.y), r: 7,
        }));
        const t = el("text", {
          class: "loc-label-amb", x: X(a.position.x) + 9,
          y: Y(a.position.y) - 8,
        });
        t.textContent = `歧义${k + 2}`;
        svg.appendChild(t);
      });
    }

    svg.appendChild(el("circle", {
      class: attempt ? "loc-point-attempt" : "loc-point",
      cx: X(r.position.x), cy: Y(r.position.y), r: attempt ? 8 : 9,
    }));
    const lt = el("text", {
      class: attempt ? "loc-label-attempt" : "loc-label",
      x: X(r.position.x) + 11, y: Y(r.position.y) + 4,
    });
    lt.textContent = attempt
      ? `最优尝试（超容限）(${r.position.x}, ${r.position.y})`
      : `定位点 (${r.position.x}, ${r.position.y})`;
    svg.appendChild(lt);
  }

  renderLegend();
}

function drawCalMarker(pos, label) {
  if (!pos || !Number.isFinite(pos.x) || !Number.isFinite(pos.y)) return;
  const svg = $("map");
  const s = 7;
  svg.appendChild(el("rect", {
    class: "cal-marker",
    x: X(pos.x) - s, y: Y(pos.y) - s, width: 2 * s, height: 2 * s,
    transform: `rotate(45 ${X(pos.x)} ${Y(pos.y)})`,
  }));
  const t = el("text", { class: "cal-label", x: X(pos.x) + 9, y: Y(pos.y) - 8 });
  t.textContent = `校准${label}`;
  svg.appendChild(t);
}

function renderLegend() {
  $("mapLegend").innerHTML = [
    ["#4f9dff", "麦克风（亮蓝=锚点麦）"],
    ["#f5a524", "校准敲击 A/B"],
    ["#37d67a", "定位点 / 入选直达峰"],
    ["#c084fc", "歧义定位点"],
    ["#6b7689", "未入选候选（含强回声）"],
  ].map(([c, t]) =>
    `<div class="legend-item"><span class="legend-swatch" style="background:${c}"></span>${t}</div>`
  ).join("");
}

// ---------------------------------------------------------------------------
// 各麦残差卡 + 时钟校准表
// ---------------------------------------------------------------------------

function renderMicResults() {
  const r = activeResult();
  const wrap = $("micResults");
  wrap.innerHTML = "";
  if (!r || !r.selected_peaks) return;
  const tol = num("tolMs", 0);
  const attempt = !!r._isAttempt;
  r.selected_peaks.forEach((sp) => {
    const card = document.createElement("div");
    const anchor = r.anchor_mics.includes(sp.mic);
    card.className = "mic-card" + (anchor ? " anchor" : "");
    const good = !attempt && sp.residual_ms !== null &&
      Math.abs(sp.residual_ms) <= tol + 1e-9;
    const strTxt = sp.strength === null ? "—" : sp.strength;
    // 迷你峰序列条：按该麦全部候选的时间分布画强度色带，入选处加竖线
    const peaks = state.candidates[sp.mic - 1] || [];
    const times = peaks.map((p) => p.time).filter(Number.isFinite);
    const tMin = Math.min(...times), tMax = Math.max(...times);
    const span = Math.max(tMax - tMin, 1e-6);
    const sMax = Math.max(1, ...peaks.map((p) => p.strength || 0));
    let bars = "";
    peaks.forEach((p, j) => {
      if (!Number.isFinite(p.time)) return;
      const left = (p.time - tMin) / span * 100;
      const inten = Math.max(0, Math.min(1, (p.strength || 0) / sMax));
      const chosen = sp.candidate_index === j + 1;
      bars += `<div class="mini-fill" style="left:${left}%;width:7%;
        background:${chosen ? "#37d67a" : `rgba(245,165,36,${0.25 + inten * 0.6})`}"></div>`;
      if (chosen) bars += `<div class="mini-mark" style="left:calc(${left}% + 3%);background:#fff"></div>`;
    });
    card.innerHTML =
      `<div class="mc-head"><span>M${sp.mic}${anchor ? " ⚓" : ""} · 峰 #${sp.candidate_index ?? "—"}</span>` +
      `<span class="${good ? "res-good" : "res-bad"}">${sp.residual_ms === null ? "—" : sp.residual_ms.toFixed(4)} ms</span></div>` +
      `<div class="mini-bar">${bars}</div>` +
      `<div>实测 Δt ${sp.measured_tdoa_ms.toFixed(3)} ms ／ 预测 ${sp.predicted_tdoa_ms.toFixed(3)} ms</div>` +
      `<div style="color:var(--muted)">峰强度 ${strTxt}（不参与裁决）</div>`;
    wrap.appendChild(card);
  });
}

function renderClock() {
  const r = activeResult();
  const panel = $("clockPanel");
  if (!r || !r.clock_calibration) { panel.classList.add("hidden"); return; }
  panel.classList.remove("hidden");
  const tb = $("clockTable").querySelector("tbody");
  tb.innerHTML = `<tr><td>M1（基准）</td><td>0.0000</td><td>0.0000</td></tr>` +
    r.clock_calibration.map((c) =>
      `<tr><td>M${c.mic}</td><td>${c.drift_ppm.toFixed(4)}</td>` +
      `<td>${c.offset_ms.toFixed(4)}</td></tr>`).join("");
}

// ---------------------------------------------------------------------------
// 求解
// ---------------------------------------------------------------------------

async function runLocate() {
  syncMicInputs();
  setStatus("idle", "求解中……");
  const body = buildRequest();
  try {
    const resp = await fetch("/api/locate", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    state.result = data;
    renderCandEditor();
    drawMap();
    renderMicResults();
    renderClock();
    const rmax = data.max_residual_ms ??
      (data.best_attempt && data.best_attempt.max_residual_ms) ?? "—";
    const sub = `最大绝对时差残差 ${rmax} ms · 耗时 ${data.elapsed_ms ?? "—"} ms`;
    if (data.status === "OK") {
      setStatus("ok", `✔ ${data.message}　定位点 (${data.position.x}, ${data.position.y})`, sub);
    } else if (data.status === "AMBIGUOUS") {
      setStatus("ambiguous",
        `⚠ ${data.message}（图中紫色点）`, sub);
    } else if (data.status === "NO_SOLUTION") {
      setStatus("error",
        `✘ ${data.message}` +
        (data.best_attempt ? "（图中红点为容限外最优尝试，红线为残差）" : ""),
        sub);
    } else {
      setStatus("error", `✘ ${data.message || "求解失败"}`);
    }
    (data.warnings || []).forEach((wmsg) => console.warn(wmsg));
    if (data.warnings && data.warnings.length) {
      const bar = $("statusBar");
      const wdiv = document.createElement("div");
      wdiv.className = "sub";
      wdiv.style.color = "var(--warn)";
      wdiv.textContent = "⚠ " + data.warnings.join("；");
      bar.appendChild(wdiv);
    }
  } catch (err) {
    setStatus("error", `✘ 请求失败：${err.message}`);
  }
}

// ---------------------------------------------------------------------------
// 示例数据（与后端合成场景一致的独立时钟模型）
// ---------------------------------------------------------------------------

function mulberry32(a) {
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function loadExample(n, seed) {
  const rng = mulberry32(seed);
  const C = 343;
  const W = 8, H = 6;
  $("roomW").value = W; $("roomH").value = H;
  $("soundSpeed").value = C; $("driftPpm").value = 50;
  $("tolMs").value = n >= 8 ? 0.1 : 0.01;
  $("resolution").value = 0.05;
  $("micCount").value = n;

  const corners = [[0.5, 0.5], [7.5, 0.5], [7.5, 5.5], [0.5, 5.5]];
  const mics = corners.slice();
  for (let i = 0; i < n - 4; i++) {
    mics.push([1.5 + rng() * 5, 1 + rng() * 4]);
  }
  state.mics = mics.map(([x, y]) => ({ x: +x.toFixed(3), y: +y.toFixed(3) }));

  const drifts = [0], offsets = [0];
  for (let i = 1; i < n; i++) drifts.push((rng() * 2 - 1) * 50e-6);
  for (let i = 1; i < n; i++) offsets.push((rng() * 2 - 1) * 0.02);

  const calA = [1.0, 3.0], calB = [6.5, 2.5];
  const eA = 0.1, eB = 0.9, eT = 0.35;
  const target = n >= 8 ? [4.0, 3.0] : [3.2, 2.1];

  const arrivals = (px, py, e) => mics.map((m, i) => {
    const g = Math.hypot(px - m[0], py - m[1]) / C;
    return offsets[i] + (1 + drifts[i]) * (e + g);
  });

  state.calA = { pos: { x: calA[0], y: calA[1] }, arrivals: arrivals(...calA, eA) };
  state.calB = { pos: { x: calB[0], y: calB[1] }, arrivals: arrivals(...calB, eB) };
  $("calAx").value = calA[0]; $("calAy").value = calA[1];
  $("calBx").value = calB[0]; $("calBy").value = calB[1];

  const nCand = n >= 8 ? 12 : 6;
  const tArr = arrivals(...target, eT);
  state.candidates = mics.map((m, i) => {
    const direct = { time: +tArr[i].toFixed(6), strength: +(0.3 + rng() * 0.1).toFixed(2) };
    const echoes = [];
    for (let j = 1; j < nCand; j++) {
      echoes.push({
        time: +(tArr[i] + 0.0012 + rng() * 0.016).toFixed(6),
        strength: +(5 + rng() * 5).toFixed(2),
      });
    }
    return [direct, ...echoes].sort((a, b) => a.time - b.time);
  });

  rebuildMicTables();
  state.result = null;
  renderCandEditor();
  drawMap();
  renderMicResults();
  renderClock();
  setStatus("idle", `已载入 ${n} 麦示例：直达峰弱、回声强，点击「定位求解」。`);
}

// ---------------------------------------------------------------------------
// 初始化
// ---------------------------------------------------------------------------

$("micCount").addEventListener("change", () => {
  rebuildMicTables(); drawMap();
});
["roomW", "roomH"].forEach((id) =>
  $(id).addEventListener("change", drawMap));
$("btnLocate").addEventListener("click", runLocate);
$("btnExample8").addEventListener("click", () => loadExample(8, 21));
$("btnExample4").addEventListener("click", () => loadExample(4, 1));

loadExample(8, 21);
rebuildMicTables();
drawMap();
