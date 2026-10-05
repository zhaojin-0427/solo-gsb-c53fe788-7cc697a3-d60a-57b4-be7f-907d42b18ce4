"""多麦克风敲击定位核心算法。

流程：
1. 校验输入（房间、麦克风非共线、声速、ppm、容限、分辨率、两次校准事件的顺序）。
2. 以 0 号麦为时基，用两次位置已知的校准敲击线性拟合各麦时钟的偏移与速率，
   检查漂移是否超过 ppm 上限。
3. 把目标敲击各麦候选峰时刻换算到 0 号麦时基，与候选强度一同进入组合搜索。
4. 枚举锚点三麦（0 号麦 + 一对非共线麦）全部候选组合，双曲线交点给出闭式种子；
   必要时用房间网格兜底，防止数值退化漏解。
5. 对种子做光滑化最大残差最小化，按「最大绝对时差残差、候选序号」裁决。
6. 残差容限内相距超过分辨率的多个解报告歧义；强回声仅作展示，不参与几何裁决。

单位约定：
    距离：米；声速：m/s；漂移：ppm；时差容限：毫秒；分辨率：米；
    时刻：秒（任意共同原点）。
"""

from __future__ import annotations

import math
import time
from typing import Any

import numpy as np
from scipy.ndimage import label
from scipy.optimize import minimize

# ---------------------------------------------------------------------------
# 常量与异常
# ---------------------------------------------------------------------------

MAX_MICS = 8
MIN_MICS = 4
MAX_CANDIDATES = 12
MIN_CANDIDATES = 1
# 网格兜底：闭式种子全部退化时启用。只需给出「分辨率/走廊宽度」级别的种子
# （之后有带梯度精修），网格点数按 5 秒预算封顶，实测 2 核下 < 2 秒
MAX_GRID_CELLS = 220_000
# 网格分块点数：限定 diffs（P×峰数 float64）等临时数组在 ~10MB 量级
GRID_CHUNK = 40_000
# 进入精修的种子数量上限
MAX_REFINE_SEEDS = 120
# 闭式种子预筛倍数（相对容限）：松到能接住优化可收敛的种子与「最优尝试」，
# 紧到不给精修灌入噪声；另设绝对下限保证小容限下也能返回容限外最优组合
SEED_PRESHIELD_TOL = 2.5
SEED_PRESHIELD_FLOOR_MS = 0.15
EPS_T = 1e-9


class LocateError(Exception):
    """输入不合法（400）。code 为稳定的英文标识，message 为中文原因。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _num(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LocateError("INVALID_PARAMETER", f"{name}必须是数字")
    value = float(value)
    if not math.isfinite(value):
        raise LocateError("INVALID_PARAMETER", f"{name}必须是有限数")
    return value


def _dist2(ax: float, ay: float, bx: float, by: float) -> float:
    return math.hypot(ax - bx, ay - by)


# ---------------------------------------------------------------------------
# 输入校验
# ---------------------------------------------------------------------------


def validate_and_calibrate(req: dict) -> tuple[float, float, float, np.ndarray, np.ndarray,
                                               list[list[dict]], float, float, list[str]]:
    """校验整份请求并完成时钟校准。

    返回 (c, tol_ms, resolution, mic_xy, cand_time0, cand_lists,
          e_a0, e_b0, warnings, clock_summary)
    其中 cand_time0[i] 为各麦候选峰换算到 0 号麦时基的时刻（秒），
    candidates 为原样保留（含 strength）的候选列表。
    """
    warnings: list[str] = []

    # --- 房间 ---
    room = req.get("room") or {}
    width = _num(room.get("width"), "房间宽度")
    height = _num(room.get("height"), "房间高度")
    if width <= 0 or height <= 0:
        raise LocateError("INVALID_ROOM", "房间宽度和高度必须为正数")

    # --- 声速 / ppm / 容限 / 分辨率 ---
    c = _num(req.get("sound_speed"), "声速")
    if c <= 0:
        raise LocateError("INVALID_SOUND_SPEED", "声速必须为正数")
    ppm = _num(req.get("drift_ppm"), "时钟漂移上限")
    if ppm < 0:
        raise LocateError("INVALID_DRIFT_PPM", "时钟漂移上限不能为负数")
    tol_ms = _num(req.get("tdoa_tolerance_ms"), "时差容限")
    if tol_ms < 0:
        raise LocateError("INVALID_TOLERANCE", "时差容限不能为负数")
    resolution = _num(req.get("resolution"), "位置分辨率")
    if resolution <= 0:
        raise LocateError("INVALID_RESOLUTION", "位置分辨率必须为正数")
    tol_s = tol_ms / 1000.0

    # --- 校准事件 ---
    cal = req.get("calibrations") or []
    if len(cal) != 2:
        raise LocateError("INVALID_CALIBRATION", "必须提供两次位置已知的校准敲击")
    cal_pts: list[tuple[float, float]] = []
    cal_rows: list[list[float]] = []
    for ev in cal:
        pos = ev.get("position") or {}
        px = _num(pos.get("x"), "校准点x")
        py = _num(pos.get("y"), "校准点y")
        if not (0.0 <= px <= width and 0.0 <= py <= height):
            raise LocateError("CALIB_POSITION_OUTSIDE", "校准敲击位置必须在房间内")
        rows = ev.get("arrivals")
        if not isinstance(rows, list):
            raise LocateError("INVALID_CALIBRATION", "校准敲击到达时刻格式不正确")
        try:
            rows = [float(v) for v in rows]
        except (TypeError, ValueError):
            raise LocateError("INVALID_CALIBRATION", "校准敲击到达时刻必须为数字")
        if any(not math.isfinite(v) for v in rows):
            raise LocateError("INVALID_CALIBRATION", "校准敲击到达时刻必须为有限数")
        cal_rows.append(rows)
        cal_pts.append((px, py))

    # --- 麦克风 ---
    mics = req.get("microphones") or []
    if not isinstance(mics, list) or not (MIN_MICS <= len(mics) <= MAX_MICS):
        raise LocateError("INVALID_MICROPHONES",
                          f"麦克风数量必须在 {MIN_MICS}～{MAX_MICS} 个之间")
    n = len(mics)
    mic_xy = np.zeros((n, 2))
    for i, m in enumerate(mics):
        x = _num(m.get("x"), f"麦克风{i + 1}的x坐标")
        y = _num(m.get("y"), f"麦克风{i + 1}的y坐标")
        if not (0.0 <= x <= width and 0.0 <= y <= height):
            raise LocateError("MIC_INSIDE_ROOM", f"麦克风 {i + 1} 必须位于房间内部")
        mic_xy[i] = (x, y)

    for row in cal_rows:
        if len(row) != n:
            raise LocateError("INVALID_CALIBRATION",
                              f"每次校准敲击都需要全部 {n} 个麦克风的到达时刻")

    # 共线检测：所有麦克风相对 0 号麦的方向张成面积（SVD 最小奇异值）
    rel = mic_xy[1:] - mic_xy[0]
    if rel.shape[0] >= 2:
        s = np.linalg.svd(rel - rel.mean(axis=0), compute_uv=False)
        scale = max(float(np.linalg.norm(mic_xy, axis=1).max()), width, height, 1.0)
        if s[-1] < 1e-9 * scale:
            raise LocateError("MICROPHONES_COLLINEAR", "麦克风位置共线，无法在平面内唯一定位")
    else:
        raise LocateError("MICROPHONES_COLLINEAR", "麦克风数量不足，无法判定非共线")

    # 事件顺序：以 0 号麦读数判断 A/B 先后（时基本身单调）
    t0_a, t0_b = cal_rows[0][0], cal_rows[1][0]
    if t0_b <= t0_a:
        raise LocateError("CALIBRATION_REVERSED",
                          "校准时刻倒序：第二次校准敲击的首麦到达时刻必须晚于第一次")
    # 几何也必须一致：真实事件先后不允许被传播时延颠倒
    pa = np.array(cal_pts[0])
    pb = np.array(cal_pts[1])
    geo_a = _dist2(pa[0], pa[1], mic_xy[0, 0], mic_xy[0, 1]) / c
    geo_b = _dist2(pb[0], pb[1], mic_xy[0, 0], mic_xy[0, 1]) / c
    if (t0_b - geo_b) <= (t0_a - geo_a):
        raise LocateError("CALIBRATION_REVERSED",
                          "校准时刻倒序：由已知位置反推的发射时刻第二次早于第一次")

    # 每个麦自身两次读数必须严格递增（独立时钟也必须单调）
    for i in range(n):
        if cal_rows[1][i] <= cal_rows[0][i]:
            raise LocateError("CALIBRATION_REVERSED",
                              f"麦克风 {i + 1} 的两次校准到达时刻不是严格递增")

    # --- 时钟线性校准：t0 = (t - o) / (1 + δ)，两点恰好确定 δ 与 o ---
    e_a0 = t0_a - geo_a
    e_b0 = t0_b - geo_b
    if e_b0 <= e_a0:  # 前置几何检查的冗余兜底
        raise LocateError("CALIBRATION_REVERSED", "两次校准事件的发射时刻间隔非正")
    drift = np.zeros(n)
    offset = np.zeros(n)
    clock_summary: list[dict] = []
    for i in range(1, n):
        ga = _dist2(cal_pts[0][0], cal_pts[0][1], mic_xy[i, 0], mic_xy[i, 1]) / c
        gb = _dist2(cal_pts[1][0], cal_pts[1][1], mic_xy[i, 0], mic_xy[i, 1]) / c
        # 各麦自身时钟反推的发射时刻（含未知偏置与速率）
        e_a_i = cal_rows[0][i] - ga
        e_b_i = cal_rows[1][i] - gb
        span = e_b_i - e_a_i
        if span <= 0:
            raise LocateError("CALIBRATION_REVERSED",
                              f"麦克风 {i + 1} 反推的两次发射时刻间隔非正，校准数据自相矛盾")
        # 到达模型 t = o + (1+δ)·t0，发射时刻间隔与几何无关，
        # 故速率由「反推发射间隔」之比给出
        d = span / (e_b0 - e_a0) - 1.0
        # 校准点 A：t_a^i = o + (1+δ)(e_a0 + g_a) = o + (1+δ)(t0_a - g_a^0) + g_a
        o = cal_rows[0][i] - (1.0 + d) * (t0_a - geo_a) - ga
        drift[i] = d
        offset[i] = o
        clock_summary.append({
            "mic": i + 1,
            "drift_ppm": round(d * 1e6, 4),
            "offset_ms": round(o * 1000.0, 4),
        })

    ppm_abs = np.abs(drift[1:]) * 1e6
    worst = int(np.argmax(ppm_abs)) + 2  # 跳过 0 号麦后的 1 基麦号
    if ppm_abs.max() > ppm + 1e-9:
        raise LocateError(
            "DRIFT_EXCEEDED",
            f"漂移越界：麦克风 {worst} 的时钟速率偏差 "
            f"{ppm_abs.max():.3f} ppm 超过上限 {ppm:.3f} ppm（以首麦为时基）",
        )

    # --- 目标候选峰 ---
    targets = req.get("target") or {}
    per_mic = targets.get("candidates")
    if not isinstance(per_mic, list) or len(per_mic) != n:
        raise LocateError("INVALID_TARGET", f"目标敲击需要全部 {n} 个麦克风的候选峰列表")

    cand_lists: list[list[dict]] = []
    cand_time0: list[np.ndarray] = []
    for i, raw in enumerate(per_mic):
        if not isinstance(raw, list) or not (MIN_CANDIDATES <= len(raw) <= MAX_CANDIDATES):
            raise LocateError("INVALID_TARGET",
                              f"麦克风 {i + 1} 需要 {MIN_CANDIDATES}～{MAX_CANDIDATES} 个候选峰")
        items: list[dict] = []
        times: list[float] = []
        for j, cand in enumerate(raw):
            t = _num(cand.get("time"), f"麦{i + 1}候选{j + 1}时刻")
            s = _num(cand.get("strength", 0.0), f"麦{i + 1}候选{j + 1}强度")
            items.append({"time": t, "strength": s, "index": j + 1})
            t0 = (t - offset[i]) / (1.0 + drift[i])
            times.append(t0)
        cand_lists.append(items)
        cand_time0.append(np.asarray(times))

    # 目标峰应落在两次校准之间（各麦独立时钟，按各自读数比较）
    for i in range(n):
        lo = cal_rows[0][i]
        hi = cal_rows[1][i]
        out = [c["index"] for c in cand_lists[i] if not (lo <= c["time"] <= hi)]
        if out:
            warnings.append(
                f"麦克风 {i + 1} 的候选峰 {', '.join(map(str, out))} "
                "位于两次校准时刻之外，时钟外推可能影响精度")

    return (c, tol_ms, resolution, mic_xy, cand_time0, cand_lists,
            e_a0, e_b0, warnings, clock_summary)


# ---------------------------------------------------------------------------
# 几何：三麦双曲线闭式交点
# ---------------------------------------------------------------------------


def hyperbola_intersections(mics: np.ndarray, tau1: float, tau2: float,
                            c: float, room: tuple[float, float],
                            tol_s: float) -> list[tuple[float, float]]:
    """以 0 号麦为基准，解 |x-p1|-|x-p0|=c·tau1、|x-p2|-|x-p0|=c·tau2。

    把 x' = p + q·R（R = |x-p0|）代入 R 二次方程，返回房间内且回代合格的根。
    """
    p0, p1, p2 = mics
    d1 = float(np.linalg.norm(p1 - p0))
    d2 = float(np.linalg.norm(p2 - p0))
    r1, r2 = c * tau1, c * tau2
    # 双曲差必须不超过麦间距
    if abs(r1) > d1 + 1e-9 or abs(r2) > d2 + 1e-9:
        return []

    # 平移到 p0 后，由 |x'-pi|^2 - R^2 = 2Rr_i + r_i^2 线性化：
    # 2 pi·x' = |pi|^2 - r_i^2 - 2R r_i
    A = np.array([2.0 * (p1 - p0), 2.0 * (p2 - p0)])
    rhs_const = np.array([d1 * d1 - r1 * r1, d2 * d2 - r2 * r2])
    rhs_lin = np.array([-2.0 * r1, -2.0 * r2])

    det = A[0, 0] * A[1, 1] - A[0, 1] * A[1, 0]
    if abs(det) < 1e-14 * max(d1 * d2, 1e-12):
        return []
    Ainv = np.array([[A[1, 1], -A[0, 1]], [-A[1, 0], A[0, 0]]]) / det
    p = Ainv @ rhs_const
    q = Ainv @ rhs_lin

    a2 = float(q @ q) - 1.0
    a1 = 2.0 * float(p @ q)
    a0 = float(p @ p)
    roots: list[tuple[float, float]] = []
    for R in _quadratic_roots(a2, a1, a0):
        if R < -1e-9:
            continue
        R = max(R, 0.0)
        x = p0 + p + q * R
        if not (-tol_s * c <= x[0] <= room[0] + tol_s * c and
                -tol_s * c <= x[1] <= room[1] + tol_s * c):
            continue
        rr0 = float(np.linalg.norm(x - p0))
        rr1 = float(np.linalg.norm(x - p1))
        rr2 = float(np.linalg.norm(x - p2))
        e1 = abs((rr1 - rr0) - r1) / c
        e2 = abs((rr2 - rr0) - r2) / c
        if max(e1, e2) <= max(SEED_PRESHIELD_TOL * tol_s, SEED_PRESHIELD_FLOOR_MS / 1000.0):
            x = np.array([min(max(x[0], 0.0), room[0]), min(max(x[1], 0.0), room[1])])
            roots.append((float(x[0]), float(x[1])))
    return roots


def _quadratic_roots(a: float, b: float, c0: float) -> list[float]:
    if abs(a) < 1e-18:
        if abs(b) < 1e-18:
            return []
        return [-c0 / b]
    disc = b * b - 4.0 * a * c0
    if disc < -1e-12 * max(1.0, b * b):
        return []
    disc = max(disc, 0.0)
    s = math.sqrt(disc)
    return [(-b - s) / (2.0 * a), (-b + s) / (2.0 * a)]


# ---------------------------------------------------------------------------
# 候选组合评价
# ---------------------------------------------------------------------------


def make_evaluator(mic_xy: np.ndarray, cand_time0: list[np.ndarray], c: float):
    """返回 eval_tuple(choice)：choice[i] 为候选下标或 -1（不使用该麦）。"""
    n = len(mic_xy)

    def tdoa_for(choice: tuple[int, ...], x: np.ndarray):
        pred = np.linalg.norm(mic_xy - x, axis=1) - np.linalg.norm(mic_xy[0] - x)
        pred /= c
        meas = np.zeros(n)
        used = np.zeros(n, dtype=bool)
        t0 = cand_time0[0][choice[0]]
        used[0] = True
        for i in range(1, n):
            if choice[i] >= 0:
                meas[i] = cand_time0[i][choice[i]] - t0
                used[i] = True
        return meas, pred, used

    def eval_tuple(choice: tuple[int, ...], x: np.ndarray):
        meas, pred, used = tdoa_for(choice, x)
        res = np.where(used, meas - pred, 0.0)
        return float(np.max(np.abs(res[used]))), res, meas, pred, used

    tdoa_for.mic_xy = mic_xy
    tdoa_for.c = c
    return eval_tuple, tdoa_for


def refine_seed(x0: tuple[float, float], choice, tdoa_for,
                width: float, height: float, tol_s: float) -> tuple[float, float]:
    """L-BFGS-B（光滑 max，带解析梯度）精修；闭式种子已是二次方程精确交点，
    梯度优化足以收敛到亚容限精度，无需二级单纯形搜索。"""
    soft = max(tol_s * 0.25, 1e-6)

    def f(z):
        x = np.array([min(max(z[0], 0.0), width), min(max(z[1], 0.0), height)])
        meas, pred, used = tdoa_for(choice, x)
        res = (meas - pred)[used]
        if res.size == 0:
            return 1e9, np.zeros(2)
        zabs = np.abs(res) / soft
        m = zabs.max()
        w = np.exp(zabs - m)
        # f = soft·logsumexp(|res|/soft)；res=meas-pred，
        # dpred/dx = 指向麦i单位向量 − 指向麦0单位向量（距离随 x 变化方向）
        mic_xy = tdoa_for.mic_xy
        c = tdoa_for.c
        u = mic_xy[used] - x
        # x 恰好在某麦上时距离为 0，单位向量方向任意（该点残差对位置不可微），
        # 用微小 epsilon 防止 NaN 卡住优化
        d0 = max(np.linalg.norm(mic_xy[0] - x), 1e-6)
        u0 = (mic_xy[0] - x) / d0
        dist = np.linalg.norm(u, axis=1)
        u = u / np.maximum(dist, 1e-6)[:, None]
        grad_pred = (u - u0) / c
        grad_res = -grad_pred  # res = meas - pred
        grad_z = np.sign(res)[:, None] * grad_res / soft
        grad = soft * (w[:, None] * grad_z).sum(axis=0) / w.sum()
        return soft * (m + math.log(w.sum())), grad

    best = np.array(x0)
    r = minimize(lambda z: f(z), best, jac=True, method="L-BFGS-B",
                 bounds=[(0.0, width), (0.0, height)],
                 options={"maxiter": 40, "ftol": 1e-13, "gtol": 1e-10})
    if r.fun < f(best)[0]:
        best = r.x
    return float(best[0]), float(best[1])


# ---------------------------------------------------------------------------
# 网格兜底（闭式种子数值退化时）
# ---------------------------------------------------------------------------


def grid_seeds(mic_xy: np.ndarray, cand_time0: list[np.ndarray], c: float,
               width: float, height: float, tol_s: float,
               resolution: float, seed_floor_s: float = 0.0) -> list:
    """在房间网格上对每个位置选各麦最近候选，标记 max|残差|<=tol 的连通块取局部最优。

    按行分块计算，峰值临时内存与网格总点数无关（约 10MB 量级）。
    """
    # 网格间距必须明显小于残差走廊半宽 c·tol（否则会漏掉宽度仅 2c·tol 的
    # 可行走廊），同时不必细于分辨率；取两者较小值，点数超预算时再统一降采样
    spacing = min(resolution, 0.25 * c * tol_s)
    spacing = max(spacing, 1e-4)
    nx = int(math.ceil(width / spacing)) + 1
    ny = int(math.ceil(height / spacing)) + 1
    cells = nx * ny
    if cells > MAX_GRID_CELLS:
        scale = math.sqrt(MAX_GRID_CELLS / cells)
        nx, ny = max(int(nx * scale), 2), max(int(ny * scale), 2)
    xs = np.linspace(0.0, width, nx)
    ys = np.linspace(0.0, height, ny)
    n = len(mic_xy)

    best_r = np.full((ny, nx), np.inf)
    best_choice = -np.ones((ny, nx, n), dtype=np.int8)
    # 连通扩展余量；无可行解时放宽到预筛下限，以保留容限外最优尝试
    slack = max(tol_s, seed_floor_s) + spacing / c
    rows_per_chunk = max(1, GRID_CHUNK // nx)

    # 逐 0 号麦候选（基准 TDOA=0）、逐行块选最近候选并记录残差
    for t0_idx in range(len(cand_time0[0])):
        t0 = cand_time0[0][t0_idx]
        for y0 in range(0, ny, rows_per_chunk):
            y1 = min(ny, y0 + rows_per_chunk)
            gx, gy = np.meshgrid(xs, ys[y0:y1], indexing="xy")
            pts = np.stack([gx.ravel(), gy.ravel()], axis=1)
            P = pts.shape[0]
            r0 = np.linalg.norm(pts - mic_xy[0], axis=1)
            res_abs = np.zeros((P, n))
            choice_grid = np.full((P, n), -1, dtype=np.int8)
            choice_grid[:, 0] = t0_idx
            for i in range(1, n):
                pred = (np.linalg.norm(pts - mic_xy[i], axis=1) - r0) / c
                diffs = np.abs((cand_time0[i][None, :] - t0) - pred[:, None])
                j = np.argmin(diffs, axis=1)
                res_abs[:, i] = diffs[np.arange(P), j]
                choice_grid[:, i] = j
            r = res_abs.max(axis=1).reshape(y1 - y0, nx)
            choice_grid = choice_grid.reshape(y1 - y0, nx, n)
            upd = r < best_r[y0:y1]
            best_r[y0:y1][upd] = r[upd]
            best_choice[y0:y1][upd] = choice_grid[upd]

    mask = best_r <= slack
    if not mask.any():
        return []
    labeled, num = label(mask, structure=np.ones((3, 3)))
    gxf, gyf = np.meshgrid(xs, ys, indexing="xy")
    flat_label = labeled.ravel()
    flat_r = best_r.ravel()
    flat_choice = best_choice.reshape(-1, n)
    flat_x, flat_y = gxf.ravel(), gyf.ravel()
    # 每个 8 连通可行块取残差最小的网格点作为种子
    result = []
    for lab in range(1, num + 1):
        idx = np.flatnonzero(flat_label == lab)
        k = idx[np.argmin(flat_r[idx])]
        result.append(((float(flat_x[k]), float(flat_y[k])),
                       tuple(int(v) for v in flat_choice[k])))
    return result


# ---------------------------------------------------------------------------
# 主求解
# ---------------------------------------------------------------------------


def _anchor_pair(mic_xy: np.ndarray) -> tuple[int, int]:
    """选与 0 号麦构成最大三角形面积的一对麦。"""
    best, best_area = (1, 2), -1.0
    n = len(mic_xy)
    for i in range(1, n):
        for j in range(i + 1, n):
            area = abs(np.cross(mic_xy[i] - mic_xy[0], mic_xy[j] - mic_xy[0]))
            if area > best_area:
                best, best_area = (i, j), float(area)
    return best


def solve(req: dict, deadline: float | None = None) -> dict:
    t_start = time.perf_counter()
    (c, tol_ms, resolution, mic_xy, cand_time0, cand_lists,
     e_a0, e_b0, warnings, clock_summary) = validate_and_calibrate(req)
    width = float(req["room"]["width"])
    height = float(req["room"]["height"])
    tol_s = tol_ms / 1000.0
    n = len(mic_xy)
    eval_tuple, tdoa_for = make_evaluator(mic_xy, cand_time0, c)

    i1, i2 = _anchor_pair(mic_xy)
    anchor_mics = np.stack([mic_xy[0], mic_xy[i1], mic_xy[i2]])

    # ---- 1) 闭式种子：枚举锚点三麦候选 ----
    raw_seeds: list[dict] = []
    counts = [len(cand_time0[i]) for i in range(n)]
    for k0 in range(counts[0]):
        t0 = cand_time0[0][k0]
        for k1 in range(counts[i1]):
            tau1 = cand_time0[i1][k1] - t0
            for k2 in range(counts[i2]):
                tau2 = cand_time0[i2][k2] - t0
                pts = hyperbola_intersections(
                    anchor_mics, tau1, tau2, c, (width, height), tol_s)
                for x in pts:
                    # 非锚点麦先各取最近候选（序号最小的等距候选），用于残差预筛
                    choice = [-1] * n
                    choice[0], choice[i1], choice[i2] = k0, k1, k2
                    pred = (np.linalg.norm(mic_xy - x, axis=1)
                            - np.linalg.norm(mic_xy[0] - x)) / c
                    for i in range(1, n):
                        if i in (i1, i2):
                            continue
                        diffs = np.abs(cand_time0[i] - (t0 + pred[i]))
                        choice[i] = int(np.argmin(diffs))
                    rmax, _, _, _, _ = eval_tuple(tuple(choice), np.array(x))
                    preshield = max(SEED_PRESHIELD_TOL * tol_s,
                                    SEED_PRESHIELD_FLOOR_MS / 1000.0)
                    if rmax <= preshield:
                        raw_seeds.append({"x": x, "choice": tuple(choice),
                                          "kind": "algebraic", "rmax": rmax})

    used_grid = False
    if not raw_seeds:
        # ---- 2a) 退化种子：声源恰好落在某麦上时双曲线/网格都难以精确命中，
        # 直接以各麦位置为种子（代价极低）----
        preshield_s = max(SEED_PRESHIELD_TOL * tol_s,
                          SEED_PRESHIELD_FLOOR_MS / 1000.0)
        for mi in range(n):
            x = (float(mic_xy[mi][0]), float(mic_xy[mi][1]))
            choice = [-1] * n
            for i in range(n):
                if i == 0:
                    choice[0] = 0
                    continue
                pred_i = (np.linalg.norm(mic_xy[i] - mic_xy[mi])
                          - np.linalg.norm(mic_xy[0] - mic_xy[mi])) / c
                choice[i] = int(np.argmin(np.abs(
                    cand_time0[i] - (cand_time0[0][0] + pred_i))))
            rmax, _, _, _, _ = eval_tuple(tuple(choice), np.array(x))
            if rmax <= preshield_s:
                raw_seeds.append({"x": x, "choice": tuple(choice),
                                  "kind": "mic_point", "rmax": rmax})

    if not raw_seeds:
        # ---- 2b) 网格兜底 ----
        used_grid = True
        preshield_s = max(SEED_PRESHIELD_TOL * tol_s,
                          SEED_PRESHIELD_FLOOR_MS / 1000.0)
        for x, choice in grid_seeds(mic_xy, cand_time0, c, width, height,
                                    tol_s, resolution,
                                    seed_floor_s=SEED_PRESHIELD_FLOOR_MS / 1000.0):
            rmax, _, _, _, _ = eval_tuple(choice, np.array(x))
            if rmax <= preshield_s:
                raw_seeds.append({"x": x, "choice": choice,
                                  "kind": "grid", "rmax": rmax})

    # ---- 3) 精修（按位置+组合去重后限量）----
    raw_seeds.sort(key=lambda s: s["rmax"])
    uniq: list[dict] = []
    for s in raw_seeds:
        if all(math.hypot(s["x"][0] - u["x"][0], s["x"][1] - u["x"][1])
               > resolution * 0.5 or s["choice"] != u["choice"] for u in uniq):
            uniq.append(s)
        if len(uniq) >= MAX_REFINE_SEEDS:
            break

    refined: list[dict] = []
    for s in uniq:
        x, y = refine_seed(s["x"], s["choice"], tdoa_for,
                           width, height, tol_s)
        rmax, res, meas, pred, used = eval_tuple(s["choice"], np.array([x, y]))
        # 精修后允许为非锚点麦重新选最近候选（几何可能微移）
        choice = list(s["choice"])
        t0 = cand_time0[0][choice[0]]
        for i in range(1, n):
            if i in (i1, i2):
                continue
            diffs = np.abs(cand_time0[i] - (t0 + pred[i]))
            choice[i] = int(np.argmin(diffs))
        choice_t = tuple(choice)
        if choice_t != s["choice"]:
            x, y = refine_seed((x, y), choice_t, tdoa_for,
                               width, height, tol_s)
            rmax, res, meas, pred, used = eval_tuple(choice_t, np.array([x, y]))
        refined.append({"x": (x, y), "choice": choice_t, "rmax": rmax,
                        "res": res, "meas": meas, "pred": pred, "used": used,
                        "kind": s["kind"]})
        if deadline is not None and time.perf_counter() > deadline:
            break

    # ---- 4) 容限内可行解 ----
    feasible = [s for s in refined if s["rmax"] <= tol_s + EPS_T]

    # 同一组合精修可能产生近重复；先按位置聚类，簇内取最优
    feasible.sort(key=lambda s: (round(s["rmax"], 12),
                                 _choice_key(s["choice"])))
    clusters: list[dict] = []
    for s in feasible:
        placed = False
        for cl in clusters:
            if math.hypot(s["x"][0] - cl["x"][0], s["x"][1] - cl["x"][1]) <= resolution:
                placed = True
                break
        if not placed:
            clusters.append(s)

    # 簇代表再统一比较（簇首已是最优，因为已按裁决键排序）
    def sort_key(s):
        return (s["rmax"], _choice_key(s["choice"]))

    clusters.sort(key=sort_key)

    elapsed_ms = (time.perf_counter() - t_start) * 1000.0

    if not clusters:
        # 即便容限内无解，也返回容限外最优组合，便于页面展示残差
        best = min(refined, key=lambda s: sort_key(s), default=None)
        result = {
            "status": "NO_SOLUTION",
            "message": f"无一致解：所有候选组合的最小最大时差残差均超过容限 "
                       f"{tol_ms:g} ms" + (f"（最优 {best['rmax'] * 1000:.3f} ms）"
                                          if best else ""),
            "warnings": warnings,
            "elapsed_ms": round(elapsed_ms, 1),
        }
        if best is not None:
            result["best_attempt"] = _solution_payload(
                best, cand_lists, mic_xy, c, resolution, used_grid)
            result["anchor_mics"] = [1, i1 + 1, i2 + 1]
        result["clock_calibration"] = clock_summary
        return result

    primary = clusters[0]
    others = clusters[1:]
    ambiguous = []
    for s in others:
        # 与主解及已报告歧义解两两相距超过分辨率才算歧义
        far = [primary] + ambiguous
        if all(math.hypot(s["x"][0] - q["x"][0], s["x"][1] - q["x"][1])
               > resolution for q in far):
            ambiguous.append(s)

    payload = _solution_payload(primary, cand_lists, mic_xy, c,
                                resolution, used_grid)
    payload["status"] = "AMBIGUOUS" if ambiguous else "OK"
    payload["message"] = (
        f"歧义：容限内存在 {len(ambiguous) + 1} 个相距超过分辨率 "
        f"{resolution:g} m 的定位结果，请结合更多信息裁决"
        if ambiguous else "定位成功")
    payload["warnings"] = warnings
    payload["elapsed_ms"] = round(elapsed_ms, 1)
    payload["ambiguous_positions"] = [
        _solution_payload(s, cand_lists, mic_xy, c, resolution, used_grid)
        for s in ambiguous
    ]
    payload["anchor_mics"] = [1, i1 + 1, i2 + 1]
    payload["used_grid_fallback"] = used_grid
    payload["clock_calibration"] = clock_summary
    payload["emission_time_window_s"] = [round(e_a0, 6), round(e_b0, 6)]
    return payload


def _choice_key(choice: tuple[int, ...]) -> tuple[int, ...]:
    """候选序号裁决键：按麦序逐位比较，1 基序号。-1（无峰）排在最后。"""
    return tuple(j if j >= 0 else 999 for j in choice)


def _solution_payload(s: dict, cand_lists, mic_xy, c, resolution, used_grid) -> dict:
    choice = s["choice"]
    n = len(mic_xy)
    selected = []
    residuals_ms: list[float | None] = []
    for i in range(n):
        j = choice[i]
        res_ms = round(float(s["res"][i]) * 1000.0, 4) if s["used"][i] else None
        residuals_ms.append(res_ms)
        selected.append({
            "mic": i + 1,
            "candidate_index": j + 1 if j >= 0 else None,
            "time": cand_lists[i][j]["time"] if j >= 0 else None,
            "strength": cand_lists[i][j]["strength"] if j >= 0 else None,
            "residual_ms": res_ms,
            "measured_tdoa_ms": round(float(s["meas"][i]) * 1000.0, 4),
            "predicted_tdoa_ms": round(float(s["pred"][i]) * 1000.0, 4),
        })
    return {
        "position": {"x": round(s["x"][0], 4), "y": round(s["x"][1], 4)},
        "max_residual_ms": round(s["rmax"] * 1000.0, 4),
        "candidate_indices": [j + 1 if j >= 0 else None for j in choice],
        "selected_peaks": selected,
        "residuals_ms": residuals_ms,
        "resolution": resolution,
    }
