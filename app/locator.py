"""多麦克风敲击定位核心算法。

流程概述
--------
1. 校验房间、麦克风、声速、漂移上限、时差容限、分辨率等输入。
2. 以首麦（0 号麦）为时基，用两次分列目标事件前后的校准敲击，
   估计其余麦时钟的速率 r 与偏移 beta；|r-1| 换算为 ppm 并检查越界。
3. 每麦候选峰按时刻升序排序（强度不参与选择），统一映射到首麦时间。
4. 枚举首麦候选 k0；对每个 k0 枚举其余麦两两配对的双 TDOA 闭式解，
   辅以房间覆盖网格，生成候选声源位置种子。
5. 对每个种子按几何残差为各麦独立选择候选峰（直达波几何一致即可胜出，
   与峰强无关），记录最大绝对时差残差 E 与候选序号向量。
6. 在 E <= tolerance 的候选中，按 (E, 候选序号字典序) 裁决；对种子做
   有界 Gauss-Newton 最小最大残差抛光并迭代重选，得到最终位置。
7. 不同候选组合在容限内、位置相距超过 resolution 时报告歧义。
"""

from __future__ import annotations

import math
import time

import numpy as np
from scipy.linalg import lstsq as scipy_lstsq


class LocatorError(ValueError):
    """携带错误码的定位输入/求解错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# ---------------------------------------------------------------- 时钟校准

def calibrate(calib_a, calib_b, drift_ppm_limit, mics, mic_pos, calib_pos_a,
              calib_pos_b, speed):
    """以首麦为时基校准各麦时钟。

    两次校准敲击位置已知且不同，先由首麦观测时刻减去走时得到两次事件的
    发射时刻（首麦时标）：
        T_A = a_0 - |pA-m0|/c,  T_B = b_0 - |pB-m0|/c
    本地时刻 L 与首麦时标 t 满足 L = rate * t + offset，对其余麦：
        rate_i = (b_i-a_i) / [(T_B+|pB-mi|/c) - (T_A+|pA-mi|/c)]
    返回 list[dict]，0 号麦为恒等时钟。
    """
    a0_l, b0_l = float(calib_a[0]), float(calib_b[0])
    T_A = a0_l - float(np.linalg.norm(calib_pos_a - mic_pos[0])) / speed
    T_B = b0_l - float(np.linalg.norm(calib_pos_b - mic_pos[0])) / speed
    span0 = T_B - T_A
    if span0 <= 0:
        raise LocatorError(
            "CALIB_REVERSED",
            "校准时刻倒序：首麦两次校准敲击推得的事件发射时刻必须严格递增",
        )

    calibs = []
    for i in range(mics):
        ai_l, bi_l = float(calib_a[i]), float(calib_b[i])
        if bi_l <= ai_l:
            raise LocatorError(
                "CALIB_REVERSED",
                f"校准时刻倒序：{i} 号麦第二次校准敲击到达时刻必须严格晚于第一次",
            )
        if i == 0:
            rate, offset = 1.0, 0.0
        else:
            ta_i = T_A + float(np.linalg.norm(calib_pos_a - mic_pos[i])) / speed
            tb_i = T_B + float(np.linalg.norm(calib_pos_b - mic_pos[i])) / speed
            rate = (bi_l - ai_l) / (tb_i - ta_i)
            offset = ai_l - rate * ta_i
            if not math.isfinite(rate) or rate <= 0:
                raise LocatorError(
                    "CALIB_REVERSED",
                    f"由 {i} 号麦校准时刻估计出的时钟速率非正，校准数据无效",
                )
        ppm = (rate - 1.0) * 1.0e6
        if abs(ppm) > drift_ppm_limit + 1e-9:
            raise LocatorError(
                "DRIFT_EXCEEDED",
                f"{i} 号麦时钟漂移 {ppm:.3f} ppm 超过上限 {drift_ppm_limit:g} ppm",
            )
        calibs.append({"rate": rate, "offset": offset, "ppm": ppm,
                       "a_local": ai_l, "b_local": bi_l})
    return calibs


# ------------------------------------------------- 双 TDOA 闭式种子（矢量化）

def pair_seeds_vectorized(mic_pos, tau, K, speed, k0, W, H):
    """对固定 k0，枚举其余麦两两配对与各麦候选，返回交点数组 (P,2)。

    |s-p0|-|s-pj| = c*(tau_j-tau_0)，与 pk 联立，解 s0 二次方程。
    全部配对一次性矢量化计算。
    """
    m = mic_pos.shape[0]
    t0c = tau[0, k0]
    p0 = mic_pos[0]

    pairs_j, pairs_k, dj_all, dk_all = [], [], [], []
    for j in range(1, m):
        for k in range(j + 1, m):
            for kj in range(K[j]):
                dj_base = speed * (tau[j, kj] - t0c)
                for kk in range(K[k]):
                    pairs_j.append(j)
                    pairs_k.append(k)
                    dj_all.append(dj_base)
                    dk_all.append(speed * (tau[k, kk] - t0c))
    if not pairs_j:
        return np.empty((0, 2))
    pj = mic_pos[pairs_j]
    pk = mic_pos[pairs_k]
    dj = np.asarray(dj_all)
    dk = np.asarray(dk_all)

    # 平方差相减（ρ = |s-p0|，|s-pj| = ρ+dj）：
    # 2*(p0-pj).s = 2*ρ*dj + dj^2 - |pj|^2 + |p0|^2
    # 两行写成 M*s = 2*ρ*[dj,dk] + w，M=2*[aj; ak]
    # 故 s = n*ρ + q，其中 n = 2*M^{-1}*[dj,dk], q = M^{-1}*w
    aj = p0 - pj
    ak = p0 - pk
    M00 = 2.0 * aj[:, 0]
    M01 = 2.0 * aj[:, 1]
    M10 = 2.0 * ak[:, 0]
    M11 = 2.0 * ak[:, 1]
    det = M00 * M11 - M01 * M10
    valid = np.abs(det) > 1e-9
    if not np.any(valid):
        return np.empty((0, 2))

    def col(idx):
        return idx[valid]

    aj, ak = aj[valid], ak[valid]
    djv, dkv = dj[valid], dk[valid]
    M00v, M01v, M10v, M11v = (col(M00), col(M01), col(M10), col(M11))
    detv = det[valid]
    w0 = djv ** 2 - np.sum(pj[valid] ** 2, axis=1) + float(np.sum(p0 ** 2))
    w1 = dkv ** 2 - np.sum(pk[valid] ** 2, axis=1) + float(np.sum(p0 ** 2))
    # M^{-1}*[dj,dk] 与 M^{-1}*[w0,w1]
    i11 = (M11v * djv - M01v * dkv) / detv
    i12 = (-M10v * djv + M00v * dkv) / detv
    qx = (M11v * w0 - M01v * w1) / detv
    qy = (-M10v * w0 + M00v * w1) / detv
    vx, vy = 2.0 * i11, 2.0 * i12
    wx, wy = qx, qy

    # s = q + rho*n，代入 |s-p0|^2 = rho^2 解 rho 的二次方程
    A = vx * vx + vy * vy - 1.0
    B = 2.0 * (wx * vx + wy * vy - (vx * p0[0] + vy * p0[1]))
    C = (wx * wx + wy * wy
         - 2.0 * (wx * p0[0] + wy * p0[1]) + p0[0] ** 2 + p0[1] ** 2)

    # 记录 (S, rho, dj, dk) 以便过滤平方消元产生的伪根
    S_list, rho_list, dj_list, dk_list = [], [], [], []

    def accept(idx, rho):
        if idx.size == 0:
            return
        S_list.append(np.column_stack([wx[idx] + rho * vx[idx],
                                       wy[idx] + rho * vy[idx]]))
        rho_list.append(rho)
        dj_list.append(djv[idx])
        dk_list.append(dkv[idx])

    lin = np.abs(A) < 1e-14
    idx = lin & (np.abs(B) > 1e-14)
    accept(idx, -C[idx] / B[idx])
    quad = ~lin
    disc = B[quad] ** 2 - 4.0 * A[quad] * C[quad]
    real = disc >= -1e-12
    qi = np.where(quad)[0][real]
    if qi.size:
        sq = np.sqrt(np.maximum(disc[real], 0.0))
        accept(qi, (-B[qi] + sq) / (2.0 * A[qi]))
        accept(qi, (-B[qi] - sq) / (2.0 * A[qi]))
    if not S_list:
        return np.empty((0, 2))
    S = np.concatenate(S_list, axis=0)
    rho = np.concatenate(rho_list)
    djj = np.concatenate(dj_list)
    dkk = np.concatenate(dk_list)
    # 真实距离非负，排除平方消元伪根（带数值容差）
    geo_ok = (rho > -1e-7) & (rho + djj > -1e-7) & (rho + dkk > -1e-7)
    S = S[geo_ok]
    inside = ((S[:, 0] >= -1e-9) & (S[:, 0] <= W + 1e-9)
              & (S[:, 1] >= -1e-9) & (S[:, 1] <= H + 1e-9))
    return S[inside]


# ----------------------------------------------------------------- 覆盖网格

def build_grid(W, H, resolution):
    """覆盖房间的网格种子，间距不超过 resolution，含边界；规模设上限。"""
    nx = max(2, int(math.ceil(W / max(resolution, 1e-9))) + 1)
    ny = max(2, int(math.ceil(H / max(resolution, 1e-9))) + 1)
    while nx * ny > 3600 and (nx > 2 or ny > 2):
        if nx >= ny and nx > 2:
            nx -= 1
        elif ny > 2:
            ny -= 1
    xs = np.linspace(0.0, W, nx)
    ys = np.linspace(0.0, H, ny)
    gx, gy = np.meshgrid(xs, ys)
    return np.column_stack([gx.ravel(), gy.ravel()])


# --------------------------------------------------------------------- 求解

def solve(request: dict) -> dict:
    """执行一次完整定位。request 字段见 API 文档。"""
    t_start = time.perf_counter()

    def need_num(val, name):
        try:
            x = float(val)
        except (TypeError, ValueError):
            raise LocatorError("INVALID_INPUT", f"{name} 必须是数值")
        if not math.isfinite(x):
            raise LocatorError("INVALID_INPUT", f"{name} 必须为有限数")
        return x

    # 房间
    W = need_num(request.get("room_width"), "房间宽度")
    H = need_num(request.get("room_height"), "房间高度")
    if W <= 0 or H <= 0:
        raise LocatorError("INVALID_INPUT", "房间宽、高必须为正数")

    speed = need_num(request.get("sound_speed", 343.0), "声速")
    if speed <= 0:
        raise LocatorError("INVALID_INPUT", "声速必须为正数")
    tol = need_num(request.get("tdoa_tolerance", 0.0), "时差容限")
    if tol < 0:
        raise LocatorError("INVALID_INPUT", "时差容限必须为非负数")
    resolution = need_num(request.get("position_resolution", 0.0), "位置分辨率")
    if resolution <= 0:
        raise LocatorError("INVALID_INPUT", "位置分辨率必须为正数")
    drift_limit = need_num(request.get("drift_ppm_limit", 0.0), "时钟漂移上限")
    if drift_limit < 0:
        raise LocatorError("INVALID_INPUT", "时钟漂移上限必须为非负数 (ppm)")

    # 麦克风
    coords = request.get("microphones")
    if not isinstance(coords, list) or not (4 <= len(coords) <= 8):
        raise LocatorError("INVALID_INPUT", "麦克风数量必须在 4 ~ 8 之间")
    m = len(coords)
    mic_pos = np.zeros((m, 2))
    for i, xy in enumerate(coords):
        if not isinstance(xy, (list, tuple)) or len(xy) != 2:
            raise LocatorError("INVALID_INPUT", f"{i} 号麦坐标必须是 [x, y]")
        x, y = need_num(xy[0], f"{i} 号麦 x 坐标"), need_num(xy[1], f"{i} 号麦 y 坐标")
        if not (0.0 <= x <= W and 0.0 <= y <= H):
            raise LocatorError("INVALID_INPUT", f"{i} 号麦坐标 ({x:g}, {y:g}) 不在房间内")
        mic_pos[i] = (x, y)

    # 共线检查
    edge = mic_pos[1:] - mic_pos[0]
    norms = np.hypot(edge[:, 0], edge[:, 1])
    base = None
    for j in range(edge.shape[0]):
        if norms[j] > 1e-9:
            base = edge[j]
            break
    if base is None:
        raise LocatorError("MICS_COLLINEAR", "全部麦克风重合，不满足非共线要求")
    bx, by = base
    cross_all = bx * edge[:, 1] - by * edge[:, 0]
    scale = max(1.0, np.hypot(bx, by))
    if np.all(np.abs(cross_all) <= 1e-9 * np.maximum(norms, 1e-12) * scale):
        raise LocatorError("MICS_COLLINEAR", "麦克风全部共线（或重合），无法平面定位")

    # 校准
    def vec(name):
        v = request.get(name)
        if not isinstance(v, list) or len(v) != m:
            raise LocatorError("INVALID_INPUT", f"{name} 必须提供 {m} 个时刻")
        out = []
        for i, x in enumerate(v):
            out.append(need_num(x, f"{name}[{i}]"))
        return out

    calib_a = vec("calib_a")
    calib_b = vec("calib_b")
    for nm, key in (("校准点 A", "calib_pos_a"), ("校准点 B", "calib_pos_b")):
        val = request.get(key)
        if not isinstance(val, (list, tuple)) or len(val) != 2:
            raise LocatorError("INVALID_INPUT", f"{nm}坐标必须是 [x, y]")
        xa = need_num(val[0], f"{nm} x")
        ya = need_num(val[1], f"{nm} y")
        if not (0.0 <= xa <= W and 0.0 <= ya <= H):
            raise LocatorError("INVALID_INPUT", f"{nm}不在房间内")
        request.setdefault("_calib_pos", {})[key] = (xa, ya)
    pa = np.array(request["_calib_pos"]["calib_pos_a"])
    pb = np.array(request["_calib_pos"]["calib_pos_b"])
    if np.linalg.norm(pa - pb) < 1e-9:
        raise LocatorError("INVALID_INPUT", "两次校准敲击位置必须不同")

    calibs = calibrate(calib_a, calib_b, drift_limit, m, mic_pos, pa, pb, speed)

    # 候选峰
    cand_raw = request.get("candidates")
    if not isinstance(cand_raw, list) or len(cand_raw) != m:
        raise LocatorError("INVALID_INPUT", "候选峰数据数量必须与麦克风数量一致")
    cand_local, cand_power = [], []
    for i, peaks in enumerate(cand_raw):
        if not isinstance(peaks, list) or not (1 <= len(peaks) <= 12):
            raise LocatorError("INVALID_INPUT", f"{i} 号麦候选峰数量必须在 1 ~ 12 之间")
        rows = []
        for p in peaks:
            if not isinstance(p, dict) or "t" not in p:
                raise LocatorError("INVALID_INPUT",
                                   f"{i} 号麦候选峰必须包含时刻 t（可含强度 strength）")
            t = need_num(p["t"], f"{i} 号麦候选峰时刻")
            s = need_num(p.get("strength", 0.0), f"{i} 号麦候选峰强度")
            if s < 0:
                raise LocatorError("INVALID_INPUT", f"{i} 号麦候选峰强度必须非负")
            rows.append((t, s))
        rows.sort(key=lambda r: r[0])
        ts = np.array([r[0] for r in rows])
        ss = np.array([r[1] for r in rows])
        if np.any(ts < calibs[i]["a_local"] - 1e-9) or np.any(
            ts > calibs[i]["b_local"] + 1e-9
        ):
            raise LocatorError(
                "TARGET_OUTSIDE_CALIBRATION",
                f"{i} 号麦存在位于两次校准时刻之外的目标候选峰，目标须在两次校准敲击之间",
            )
        cand_local.append(ts)
        cand_power.append(ss)

    K = np.array([len(t) for t in cand_local], dtype=np.int64)
    Kmax = int(K.max())
    tau = np.full((m, Kmax), np.nan)
    for i in range(m):
        tau[i, : K[i]] = (cand_local[i] - calibs[i]["offset"]) / calibs[i]["rate"]

    # ------------------------------------------------ 种子评估（矢量化）
    # found: 候选序号向量 bytes -> 记录
    found: dict[bytes, dict] = {}

    def evaluate(S):
        """对一批位置点：枚举 k0，为其余麦选最优候选，记录不同赋值。"""
        S = np.asarray(S, dtype=float)
        if S.ndim == 1:
            S = S[None, :]
        inside = ((S[:, 0] >= -1e-9) & (S[:, 0] <= W + 1e-9)
                  & (S[:, 1] >= -1e-9) & (S[:, 1] <= H + 1e-9)
                  & np.isfinite(S[:, 0]) & np.isfinite(S[:, 1]))
        S = np.clip(S[inside], 0.0, None)
        S[:, 0] = np.minimum(S[:, 0], W)
        S[:, 1] = np.minimum(S[:, 1], H)
        if S.shape[0] == 0:
            return
        # 走时矩阵
        dx = S[:, 0, None] - mic_pos[None, :, 0]
        dy = S[:, 1, None] - mic_pos[None, :, 1]
        dist = np.sqrt(dx * dx + dy * dy)          # (P,m)
        CHUNK = 4096
        for lo in range(0, S.shape[0], CHUNK):
            _eval_chunk(S[lo:lo + CHUNK], dist[lo:lo + CHUNK])

    def _eval_chunk(S, dist):
        P = S.shape[0]
        base = (dist - dist[:, 0:1]) / speed       # (P,m) 走时差
        for k0 in range(int(K[0])):
            t0c = tau[0, k0]
            ksel = np.zeros((P, m), dtype=np.int64)
            resid = np.zeros((P, m))
            E = np.zeros(P)
            ksel[:, 0] = k0
            for i in range(1, m):
                ki = int(K[i])
                r = base[:, i:i + 1] - (tau[i, :ki][None, :] - t0c)   # (P,ki)
                absk = np.abs(r)
                kk = np.argmin(absk, axis=1)
                rr = r[np.arange(P), kk]
                ksel[:, i] = kk
                resid[:, i] = rr
                E = np.maximum(E, absk[np.arange(P), kk])
            t_emit = t0c - dist[:, 0]
            _record(S, E, ksel, resid, t_emit)

    def _record(S, E, ksel, resid, t_emit):
        order = np.argsort(E, kind="stable")
        for p in order:
            key = ksel[p].tobytes()
            e = float(E[p])
            rec = found.get(key)
            if rec is None or e < rec["E"]:
                found[key] = {"E": e, "S": S[p].copy(),
                              "ksel": ksel[p].copy(),
                              "resid": resid[p].copy(),
                              "t0": float(t_emit[p])}

    # ------------------------------------------------ 抛光与裁决
    def choose_at(x, k0):
        """在位置 x 处按残差为各麦（0 号固定 k0）选候选。"""
        diff = x[None, :] - mic_pos
        d = np.sqrt(np.einsum("ij,ij->i", diff, diff))
        t0c = tau[0, k0]
        ks = np.zeros(m, dtype=np.int64)
        rs = np.zeros(m)
        ks[0] = k0
        E = 0.0
        for i in range(1, m):
            r_all = (d[i] - d[0]) / speed - (tau[i, : int(K[i])] - t0c)
            kk = int(np.argmin(np.abs(r_all)))
            ks[i] = kk
            rs[i] = float(r_all[kk])
            E = max(E, abs(float(r_all[kk])))
        return E, ks, rs, t0c - float(d[0])

    def polish(rec):
        """固定/迭代重选候选组合下的有界最小化：Gauss-Newton 降最大绝对残差。

        残差 r_i(x) = (d_i-d_0)/c - (t_i-t_0)（i>=1）。每轮对绝对残差
        最大的活跃约束集做带符号加权最小二乘步，再为各麦按几何残差重选
        候选；直达组合即使峰强弱也能零残差胜出。
        """
        k0 = int(rec["ksel"][0])
        x = rec["S"].copy()
        ksel = rec["ksel"].copy()
        best = (rec["E"], x.copy(), ksel.copy())

        for _ in range(3):  # 外层：候选重选
            for _ in range(12):  # 内层：位置步
                diff = x[None, :] - mic_pos
                d = np.sqrt(np.einsum("ij,ij->i", diff, diff) + 1e-24)
                tcur = tau[np.arange(m), ksel]
                r = ((d - d[0]) / speed - (tcur - tcur[0]))[1:]
                ux = (x[0] - mic_pos[:, 0]) / d
                uy = (x[1] - mic_pos[:, 1]) / d
                G = np.column_stack([(ux - ux[0])[1:] / speed,
                                     (uy - uy[0])[1:] / speed])
                ar = np.abs(r)
                Emax = max(float(ar.max()), 1e-12)
                w = np.exp((ar / Emax - 1.0) * 25.0)  # soft minimax
                Wd = np.sqrt(w)
                A = G * Wd[:, None]
                b = -r * Wd
                try:
                    step, *_ = scipy_lstsq(A, b, lapack_driver="gelsy")
                except np.linalg.LinAlgError:
                    step = np.zeros(2)
                x_new = x
                for alpha in (1.0, 0.5, 0.25, 0.1, 0.03):
                    xt = np.clip(x + alpha * step, [0.0, 0.0], [W, H])
                    dd = np.hypot(xt[0] - mic_pos[:, 0], xt[1] - mic_pos[:, 1])
                    tt = tau[np.arange(m), ksel]
                    rr = (dd - dd[0]) / speed - (tt - tt[0])
                    if float(np.abs(rr[1:]).max()) <= Emax + 1e-13:
                        x_new = xt
                        break
                if np.hypot(*(x_new - x)) < 1e-9:
                    x = x_new
                    break
                x = x_new
            E, ks, rs, te = choose_at(x, k0)
            if E < best[0] - 1e-13:
                best = (E, x.copy(), ks.copy())
            if np.array_equal(ks, ksel):
                ksel = ks
                break
            ksel = ks
        E, ksel, rs, te = choose_at(best[1], k0)
        rec.update(E=E, S=best[1], ksel=ksel, resid=rs, t0=te)
        return rec

    # 网格 + 闭式种子
    evaluate(build_grid(W, H, resolution))
    for k0 in range(int(K[0])):
        evaluate(pair_seeds_vectorized(mic_pos, tau, K, speed, k0, W, H))

    # ------------------------------------------------ 裁决
    def lex_key(r):
        return tuple(int(v) for v in r["ksel"])

    feasible = sorted((r for r in found.values() if r["E"] <= tol + 1e-12),
                      key=lambda r: (r["E"], lex_key(r)))
    infeasible = sorted((r for r in found.values() if r["E"] > tol + 1e-12),
                        key=lambda r: (r["E"], lex_key(r)))

    def position_dedup(records, dist_limit, cap):
        kept = []
        for r in records:
            if all(np.hypot(r["S"][0] - q["S"][0], r["S"][1] - q["S"][1]) > dist_limit
                   for q in kept):
                kept.append(r)
                if len(kept) >= cap:
                    break
        return kept

    todo_feasible = position_dedup(feasible, max(resolution * 0.5, 1e-9), 200)
    # 始终抛光少量近可行种子：种子近似点的最优赋值可能本就落在容限边缘
    todo_near = infeasible[:40]
    todo = todo_feasible + todo_near
    for r in todo:
        polish(r)

    polished = [r for r in todo if r["E"] <= tol + 1e-12]
    polished.sort(key=lambda r: (r["E"], lex_key(r)))

    solve_ms = (time.perf_counter() - t_start) * 1000.0

    def serialize(rec):
        ks = [int(v) for v in rec["ksel"]]
        return {
            "position": [float(rec["S"][0]), float(rec["S"][1])],
            "max_residual": float(rec["E"]),
            "peak_indices": ks,
            "emission_time_ref": float(rec["t0"]),
            "mics": [
                {
                    "mic": i,
                    "peak_index": ks[i],
                    "t_local": float(cand_local[i][ks[i]]),
                    "t_ref": float(tau[i, ks[i]]),
                    "strength": float(cand_power[i][ks[i]]),
                    "residual": float(rec["resid"][i]),
                }
                for i in range(m)
            ],
        }

    out = {
        "solve_ms": solve_ms,
        "c": speed,
        "tol": tol,
        "resolution": resolution,
        "room": [W, H],
        "microphones": mic_pos.tolist(),
        "calibration": [
            {"mic": i, "rate": float(calibs[i]["rate"]),
             "offset": float(calibs[i]["offset"]),
             "drift_ppm": float(calibs[i]["ppm"])}
            for i in range(m)
        ],
        "calib_positions": [list(pa), list(pb)],
        "candidates": [
            [{"t": float(cand_local[i][k]), "t_ref": float(tau[i, k]),
              "strength": float(cand_power[i][k])}
             for k in range(int(K[i]))]
            for i in range(m)
        ],
    }

    if not polished:
        out.update(status="NO_CONSISTENT_SOLUTION",
                   message="无一致解：所有候选组合的最大时差残差均超过时差容限")
        near_polished = sorted(todo_near, key=lambda r: (r["E"], lex_key(r)))
        if near_polished:
            out["nearest"] = serialize(near_polished[0])
        return out

    # 歧义聚类
    clusters = []
    for r in polished:
        for cl in clusters:
            if np.hypot(r["S"][0] - cl[0]["S"][0],
                        r["S"][1] - cl[0]["S"][1]) <= resolution:
                cl.append(r)
                break
        else:
            clusters.append([r])
    clusters.sort(key=lambda cl: min(q["E"] for q in cl))
    for cl in clusters:
        cl.sort(key=lambda r: (r["E"], lex_key(r)))

    best = clusters[0][0]
    ambiguous = len(clusters) > 1
    out.update(
        status="AMBIGUOUS" if ambiguous else "OK",
        ambiguous=ambiguous,
        solution=serialize(best),
        cluster_count=len(clusters),
        alternatives=[serialize(cl[0]) for cl in clusters[1:]],
        message=(f"检测到 {len(clusters)} 组相距超过位置分辨率的容限内位置，定位存在歧义"
                 if ambiguous else "定位唯一（容限内未发现相距超过分辨率的其它位置）"),
    )
    return out
