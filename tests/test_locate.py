"""合成场景测试：校准、回声压制、歧义、共线/倒序/漂移、8x12 性能。"""

import sys
import os
import time
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))

import numpy as np  # noqa: E402

from app.locate import solve, LocateError  # noqa: E402

C = 343.0
W, H = 8.0, 6.0


def make_mics(n):
    if n == 4:
        return [(0.5, 0.5), (7.5, 0.5), (7.5, 5.5), (0.5, 5.5)]
    pts = [(0.5, 0.5), (7.5, 0.5), (7.5, 5.5), (0.5, 5.5)]
    rng = random.Random(42 + n)
    for i in range(n - 4):
        pts.append((1.5 + 5.0 * rng.random(), 1.0 + 4.0 * rng.random()))
    return pts


def build_case(n=4, target=(3.2, 2.1), ncand=6, tol_ms=0.1, drift_ppm=50.0,
               resolution=0.05, echo_strong=True, seed=1, extra_targets=(),
               gen_drift_ppm=None, jitter_ms=0.0):
    """ncand 个候选/麦：直达峰 + 若干回声干扰，回声统一晚于直达且强度更高。

    jitter_ms>0 时给直达时刻加每麦独立抖动，模拟测量噪声。
    """
    rng = random.Random(seed)
    if gen_drift_ppm is None:
        gen_drift_ppm = drift_ppm
    mics = make_mics(n)
    mxy = np.array(mics)
    # 两个校准点分列目标前后（按发射时刻）
    cal_a = (1.0, 3.0)
    cal_b = (6.5, 2.5)
    targets = [target] + list(extra_targets)

    # 各麦独立时钟：漂移（ppm）与偏移（秒）
    drifts = [0.0]
    for i in range(1, n):
        drifts.append(rng.uniform(-gen_drift_ppm, gen_drift_ppm) * 1e-6)
    offsets = [0.0]
    for i in range(1, n):
        offsets.append(rng.uniform(-0.02, 0.02))

    e_a, e_targets, e_b = 0.100, [], 0.900
    for k, t in enumerate(targets):
        e_targets.append(0.35 + 0.1 * k)

    # 每麦独立测量抖动（秒），两次校准与目标各自独立
    def jitter():
        return rng.uniform(-jitter_ms, jitter_ms) / 1000.0

    def arrivals(pt, e, noisy=False):
        out = []
        for i, m in enumerate(mics):
            g = np.linalg.norm(np.array(pt) - m) / C
            t0 = e + g + (jitter() if noisy else 0.0)
            out.append(offsets[i] + (1.0 + drifts[i]) * t0)
        return out

    # 每个目标生成候选列表：直达 + 回声（延迟 1.2～18ms），直达序号最小
    all_target_cands = []
    for (pt, e) in zip(targets, e_targets):
        per_mic = []
        for i, m in enumerate(mics):
            true_t = arrivals(pt, e, noisy=True)[i]
            times = [true_t]
            for _ in range(ncand - 1):
                times.append(true_t + rng.uniform(0.0012, 0.018))
            ts = sorted(times)
            cands = []
            for j, t in enumerate(ts):
                if j == 0:
                    s = 0.3 + 0.1 * rng.random()  # 直达峰较弱
                else:
                    s = 5.0 + 5.0 * rng.random() if echo_strong else 1.0
                cands.append({"time": t, "strength": round(s, 3)})
            per_mic.append(cands)
        all_target_cands.append(per_mic)

    req = {
        "room": {"width": W, "height": H},
        "microphones": [{"x": x, "y": y} for x, y in mics],
        "sound_speed": C,
        "drift_ppm": drift_ppm,
        "tdoa_tolerance_ms": tol_ms,
        "resolution": resolution,
        "calibrations": [
            {"position": {"x": cal_a[0], "y": cal_a[1]},
             "arrivals": arrivals(cal_a, e_a)},
            {"position": {"x": cal_b[0], "y": cal_b[1]},
             "arrivals": arrivals(cal_b, e_b)},
        ],
        "target": {"candidates": all_target_cands[0]},
    }
    return req, targets


def check(name, cond, detail=""):
    print(f"  [{'PASS' if cond else 'FAIL'}] {name} {detail}")
    if not cond:
        check.failed += 1
check.failed = 0


def main():
    print("== 1) 4 麦基础定位（弱直达 + 强回声）==")
    req, targets = build_case(n=4, target=(3.2, 2.1), seed=1, tol_ms=0.01)
    t0 = time.perf_counter()
    r = solve(req)
    dt = time.perf_counter() - t0
    p = r.get("position", r.get("best_attempt", {}).get("position"))
    print(f"     status={r['status']} pos={p} 用时 {dt * 1000:.0f} ms {r.get('message','')}")
    check("定位成功", r["status"] == "OK", r.get("message", ""))
    err = np.hypot(p["x"] - 3.2, p["y"] - 2.1)
    check("位置误差 < 分辨率", err < req["resolution"] * 1.5, f"err={err:.3f}m")
    check("入选的是直达峰而非强回声",
          all(abs(sp["strength"]) < 1.0 for sp in r.get("selected_peaks", [])),
          str([sp["strength"] for sp in r.get("selected_peaks", [])]))
    check("残差在容限内", r.get("max_residual_ms", 9) <= req["tdoa_tolerance_ms"] + 1e-6)
    check("有时钟校准摘要", len(r.get("clock_calibration", [])) == 3)

    print("== 2) 无回声干扰时同样定位 ==")
    req2, _ = build_case(n=4, target=(5.6, 4.3), echo_strong=False, seed=1,
                         tol_ms=0.01, ncand=3)
    r2 = solve(req2)
    p2 = r2["position"]
    err2 = np.hypot(p2["x"] - 5.6, p2["y"] - 4.3)
    check("定位成功", r2["status"] == "OK", r2.get("message", ""))
    check("位置误差小", err2 < 0.1, f"err={err2:.3f}m")

    print("== 3) 8 麦 × 12 候选性能（2 核 1GiB 预算 5 秒）==")
    req8, _ = build_case(n=8, target=(4.0, 3.0), ncand=12, seed=21)
    t0 = time.perf_counter()
    r8 = solve(req8)
    dt8 = time.perf_counter() - t0
    p8 = r8.get("position")
    print(f"     status={r8['status']} pos={p8} 用时 {dt8 * 1000:.0f} ms")
    check("8x12 定位成功", r8["status"] == "OK", r8.get("message", ""))
    check("用时 < 5 秒", dt8 < 5.0, f"{dt8:.2f}s")
    check("位置误差小", np.hypot(p8["x"] - 4.0, p8["y"] - 3.0) < 0.1)
    check("全部 8 麦入选直达峰",
          all(abs(sp["strength"]) < 1.0 for sp in r8["selected_peaks"]))

    print("== 4) 无一致解（各麦候选统一远离真实直达）==")
    req_bad, _ = build_case(n=4, target=(2.0, 2.0), seed=31, ncand=3,
                            tol_ms=0.05)
    rngb = random.Random(99)
    for i in range(4):
        # 每麦整体平移一个远大于预筛窗、且破坏几何一致性的量
        shift = rngb.choice([-1, 1]) * rngb.uniform(0.003, 0.006)
        req_bad["target"]["candidates"][i] = [
            {"time": c["time"] + shift, "strength": c["strength"]}
            for c in req_bad["target"]["candidates"][i]
        ]
    rb = solve(req_bad)
    check("报告 NO_SOLUTION", rb["status"] == "NO_SOLUTION", rb.get("message", ""))
    check("给出最优尝试", "best_attempt" in rb)

    print("== 5) 校准时刻倒序 ==")
    req_rev, _ = build_case()
    req_rev["calibrations"][0], req_rev["calibrations"][1] = (
        req_rev["calibrations"][1], req_rev["calibrations"][0])
    try:
        solve(req_rev)
        check("倒序被拒绝", False)
    except LocateError as e:
        check("倒序被拒绝", e.code == "CALIBRATION_REVERSED", e.message)

    print("== 6) 麦克风共线 ==")
    req_col, _ = build_case()
    req_col["microphones"] = [{"x": 1.0 + i * 2.0, "y": 3.0} for i in range(4)]
    try:
        solve(req_col)
        check("共线被拒绝", False)
    except LocateError as e:
        check("共线被拒绝", e.code == "MICROPHONES_COLLINEAR", e.message)

    print("== 7) 漂移越界 ==")
    req_dr, _ = build_case(drift_ppm=5.0, gen_drift_ppm=50.0)
    try:
        solve(req_dr)
        check("漂移越界被拒绝", False)
    except LocateError as e:
        check("漂移越界被拒绝", e.code == "DRIFT_EXCEEDED", e.message)

    print("== 8) 歧义场景（第 4 麦候选覆盖宽，两组组合均在容限内）==")
    # 3 个锚点麦给出精确双曲线交点，第 4 麦给一串密候选（弱信号麦）：
    # 与其几何兼容的两个不同交点都落在容限内 -> 必须报告歧义
    req_am, _ = build_case(n=4, target=(3.2, 2.1), seed=1, tol_ms=0.05,
                           ncand=12)
    mxy4 = np.array(make_mics(4))
    # 手工重造第 4 麦候选：在真实峰前后 ±1ms 以 0.1ms 步长铺满
    from app.locate import validate_and_calibrate
    (_c, _t, _r, _m, _ct0, _cl, _ea, _eb, _w, _ck) = validate_and_calibrate(req_am)
    true_t0 = _ct0[3][0]
    o3, d3 = _ck[2]["offset_ms"] / 1000.0, _ck[2]["drift_ppm"] * 1e-6
    dense = []
    for k, dt in enumerate(np.arange(-0.001, 0.0011, 0.0002)):
        t0v = true_t0 + dt
        raw = o3 + (1 + d3) * t0v
        dense.append({"time": float(raw), "strength": 0.5})
    req_am["target"]["candidates"][3] = dense
    ram = solve(req_am)
    check("报告歧义", ram["status"] == "AMBIGUOUS", ram.get("message", ""))
    check("至少两个相距超分辨率的解",
          len(ram.get("ambiguous_positions", [])) >= 1)
    if ram["status"] == "AMBIGUOUS":
        far = np.hypot(ram["position"]["x"] - ram["ambiguous_positions"][0]["position"]["x"],
                       ram["position"]["y"] - ram["ambiguous_positions"][0]["position"]["y"])
        check("歧义位置相距 > 分辨率", far > req_am["resolution"], f"{far:.3f}m")

    print("== 9) 噪声稳健性（8 麦，校准/目标 ±0.05ms 抖动）==")
    robust = 0
    for seed in range(10):
        req_n, _ = build_case(n=8, target=(4.0, 3.0), ncand=8, seed=100 + seed,
                              tol_ms=0.2, jitter_ms=0.05)
        rn = solve(req_n)
        pn = rn.get("position", {})
        err_n = np.hypot(pn.get("x", 0.0) - 4.0, pn.get("y", 0.0) - 3.0)
        if rn["status"] in ("OK", "AMBIGUOUS") and err_n < 0.15:
            robust += 1
    check("10 组噪声场景全部定位", robust == 10, f"{robust}/10")

    print("== 10) 闭式种子退化时网格兜底 ==")
    import app.locate as locmod
    req_g, _ = build_case(n=6, target=(3.7, 2.6), ncand=5, seed=55,
                          tol_ms=0.05)
    _orig = locmod.hyperbola_intersections
    locmod.hyperbola_intersections = lambda *a, **k: []
    try:
        rg = solve(req_g)
    finally:
        locmod.hyperbola_intersections = _orig
    pg = rg.get("position", {})
    check("网格兜底定位成功", rg["status"] == "OK", rg.get("message", ""))
    check("使用了网格兜底", rg.get("used_grid_fallback") is True)
    check("兜底位置准确",
          np.hypot(pg.get("x", 0) - 3.7, pg.get("y", 0) - 2.6) < 0.05)

    print("== 11) 8×12 最差种子计时（多位置）==")
    worst = 0.0
    for seed in range(5):
        req_w, _ = build_case(n=8, target=(2.0 + seed * 0.7, 1.5 + seed * 0.6),
                              ncand=12, seed=200 + seed, tol_ms=0.1,
                              drift_ppm=150.0, gen_drift_ppm=100.0)
        t0 = time.perf_counter()
        rw = solve(req_w)
        worst = max(worst, time.perf_counter() - t0)
        check(f"种子{seed}定位成功", rw["status"] == "OK", rw.get("message", ""))
    check("最差用时 < 5 秒", worst < 5.0, f"{worst:.2f}s")

    print(f"\n失败数: {check.failed}")
    sys.exit(1 if check.failed else 0)


if __name__ == "__main__":
    main()
