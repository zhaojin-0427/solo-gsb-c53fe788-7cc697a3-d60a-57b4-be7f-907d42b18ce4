// DOM 冒烟测试：用 jsdom 加载页面，mock fetch，验证初始化、收集、渲染不抛错。
const fs = require("fs");
const { JSDOM } = require("/tmp/jsdomenv/node_modules/jsdom");

const html = fs.readFileSync("/workspace/app/static/index.html", "utf-8");
const dom = new JSDOM(html, { runScripts: "dangerously", pretendToBeVisual: true });
const { window } = dom;
const doc = window.document;

// Canvas 2D context 在 jsdom 中为空对象，打桩
window.HTMLCanvasElement.prototype.getContext = function () {
  return new Proxy({}, { get: () => (...a) => undefined,
                            set: () => true });
};

// mock fetch：把请求转给内嵌的“伪后端”——直接用 Node 无法跑 Python，
// 这里返回一份按演示数据构造的成功结果（验证渲染路径）。
let postedBody = null;
window.fetch = async (url, opts) => {
  postedBody = JSON.parse(opts.body);
  const mkPeaks = (n) => Array.from({ length: n }, (_, i) => ({
    t: 110 + i * 0.002, strength: i === 0 ? 0.15 : 0.8,
  }));
  const m = postedBody.microphones.length;
  const sol = {
    position: [3.3, 4.1], max_residual: 0.00001, peak_indices: Array(m).fill(0),
    emission_time_ref: 109.99,
    mics: postedBody.microphones.map((_, i) => ({
      mic: i, peak_index: 0,
      t_local: postedBody.candidates[i][0].t,
      t_ref: postedBody.candidates[i][0].t,
      strength: postedBody.candidates[i][0].strength,
      residual: (i === 0 ? 0 : 1e-5 * (i % 2 ? 1 : -1)),
    })),
  };
  const result = {
    status: "AMBIGUOUS", ambiguous: true, solve_ms: 287.3,
    c: postedBody.sound_speed, tol: postedBody.tdoa_tolerance,
    resolution: postedBody.position_resolution,
    room: [postedBody.room_width, postedBody.room_height],
    microphones: postedBody.microphones,
    calib_positions: [postedBody.calib_pos_a, postedBody.calib_pos_b],
    calibration: postedBody.microphones.map((_, i) =>
      ({ mic: i, rate: 1 + (i - 1) * 1e-5, offset: 0.001 * i, drift_ppm: (i - 1) * 10 })),
    candidates: postedBody.candidates.map((ps) =>
      ps.map((p) => ({ t: p.t, t_ref: p.t, strength: p.strength }))),
    solution: sol, cluster_count: 2,
    alternatives: [{ ...sol, position: [6.1, 2.2], max_residual: 0.00002,
                     peak_indices: Array(m).fill(1) }],
    message: "检测到 2 组相距超过位置分辨率的容限内位置，定位存在歧义",
  };
  return { json: async () => result };
};

(async () => {
  await new Promise((r) => setTimeout(r, 50));
  // 初始化后应已生成 8 麦表单
  const micRows = doc.querySelectorAll("#mic-table tbody tr").length;
  if (micRows !== 8) throw new Error("mic rows = " + micRows);
  // 候选峰编辑器
  if (!doc.querySelector("#cand-table-0")) throw new Error("cand editor missing");
  // 增加候选峰到 12 个
  for (let k = 0; k < 11; k++) doc.querySelector('[data-add="3"]').click();
  const rows3 = doc.querySelectorAll("#cand-table-3 tbody tr").length;
  if (rows3 !== 12) throw new Error("expected 12 rows, got " + rows3);
  // 超过 12 不再增加
  doc.querySelector('[data-add="3"]').click();
  if (doc.querySelectorAll("#cand-table-3 tbody tr").length !== 12)
    throw new Error("cap at 12 failed");
  // 删除一行
  doc.querySelector('[data-del="3:5"]').click();
  if (doc.querySelectorAll("#cand-table-3 tbody tr").length !== 11)
    throw new Error("delete failed");
  // 触发求解（使用初始演示数据）
  doc.getElementById("btn-solve").click();
  await new Promise((r) => setTimeout(r, 100));
  const bar = doc.getElementById("status");
  if (!bar.className.includes("status-amb"))
    throw new Error("ambiguous bar not shown: " + bar.className);
  if (!bar.textContent.includes("歧义")) throw new Error("message missing");
  // 残差表渲染了 8 行
  const resRows = doc.querySelectorAll("#mic-resid-table tbody tr").length;
  if (resRows !== 8) throw new Error("resid rows = " + resRows);
  // 歧义卡片与点击切换
  const alt = doc.querySelector("[data-alt='0']");
  if (!alt) throw new Error("alternative card missing");
  alt.click();
  await new Promise((r) => setTimeout(r, 20));
  // 请求体结构检查
  const b = postedBody;
  if (b.candidates.length !== 8) throw new Error("bad body candidates");
  if (typeof b.tdoa_tolerance !== "number") throw new Error("tol not converted to s");

  // 错误路径：mock 返回 NO_CONSISTENT_SOLUTION 并重解
  window.fetch = async () => ({ json: async () => ({
    status: "NO_CONSISTENT_SOLUTION",
    message: "所有候选组合的最大时差残差均超过时差容限",
    calibration: b.microphones.map(() => ({ drift_ppm: 0, offset: 0 })),
    nearest: { position: [5, 5], max_residual: 0.009, peak_indices: Array(8).fill(0),
               emission_time_ref: 110,
               mics: b.microphones.map((_, i) => ({ mic: i, peak_index: 0,
                 t_local: b.candidates[i][0].t, t_ref: 0, strength: 0.5, residual: 0.005 })) },
  }) });
  doc.getElementById("btn-solve").click();
  await new Promise((r) => setTimeout(r, 80));
  if (!doc.getElementById("status").className.includes("status-err"))
    throw new Error("error bar missing");

  console.log("DOM smoke test OK");
})().catch((e) => { console.error(e); process.exit(1); });
