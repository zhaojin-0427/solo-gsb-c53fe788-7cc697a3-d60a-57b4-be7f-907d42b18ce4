"""定位算法的功能与性能测试。"""
import math
import time

import numpy as np
import pytest

from app.locator import LocatorError, solve

C = 343.0
W, H = 8.0, 6.0
MICS = [[1, 1], [7, 0.8], [7.5, 5], [0.5, 4.8],
        [4, 0.5], [4.2, 5.5], [2, 3], [6.5, 3.2]]
PA, PB = [0.6, 0.4], [7.4, 5.6]
SRC = [3.3, 4.1]
TA, TB, TE = 100.0, 120.0, 110.0


def make_clock(rates, offsets):
    return lambda i, t: rates[i] * t + offsets[i]


def scenario(rates=None, offsets=None, n_mics=8, n_peaks=12, tol=0.05e-3,
             drift_limit=50.0, resolution=0.05, seed=7,
             extra_peaks=None, speed=C):
    rates = rates or [1.0] * n_mics
    offsets = offsets or [0.0] * n_mics
    lt = make_clock(rates, offsets)
    mics = [list(m) for m in MICS[:n_mics]]

    def dist(p, q):
        return math.hypot(p[0] - q[0], p[1] - q[1])

    cal_a = [lt(i, TA + dist(PA, mics[i]) / speed) for i in range(n_mics)]
    cal_b = [lt(i, TB + dist(PB, mics[i]) / speed) for i in range(n_mics)]
    rng = np.random.default_rng(seed)
    cands = []
    for i in range(n_mics):
        direct = lt(i, TE + dist(SRC, mics[i]) / speed)
        peaks = [{"t": direct, "strength": 0.15}]
        for _ in range(n_peaks - 1):
            peaks.append({"t": direct + 0.008 + rng.uniform(0, 0.03),
                          "strength": float(rng.uniform(0.6, 1.0))})
        if extra_peaks:
            for et, es in extra_peaks.get(i, []):
                peaks.append({"t": direct + et, "strength": es})
        rng.shuffle(peaks)
        cands.append(peaks[:12])
    return dict(
        room_width=W, room_height=H, sound_speed=speed,
        drift_ppm_limit=drift_limit, tdoa_tolerance=tol,
        position_resolution=resolution, microphones=mics,
        calib_pos_a=PA, calib_pos_b=PB, calib_a=cal_a, calib_b=cal_b,
        candidates=cands,
    )


# ------------------------------------------------------------------ 基本功能

def test_unique_solution_weak_direct_peaks():
    rates = [1.0, 1 - 4.89e-6, 1 + 28.69e-6, 1 + 15.79e-6,
             1 - 32.47e-6, 1 + 38.05e-6, 1 + 20.89e-6, 1 + 22.89e-6]
    offsets = [0, .00412, -.00731, .00205, -.00388, .00674, -.00193, .00516]
    res = solve(scenario(rates=rates, offsets=offsets))
    assert res["status"] == "OK"
    s = res["solution"]
    assert math.hypot(s["position"][0] - SRC[0],
                      s["position"][1] - SRC[1]) < 0.01
    # 直达峰是各麦最早（排序后序号 0）且强度最弱，仍应全部入选
    assert s["peak_indices"] == [0] * 8
    assert all(mic["strength"] == pytest.approx(0.15) for mic in s["mics"])
    assert s["max_residual"] < 0.05e-3
    # 漂移估计准确
    for i, r in enumerate(rates):
        assert res["calibration"][i]["drift_ppm"] == pytest.approx(
            (r - 1) * 1e6, abs=0.01)


def test_four_mics_allowed():
    res = solve(scenario(n_mics=4, tol=0.02e-3))
    assert res["status"] in ("OK", "AMBIGUOUS")
    assert len(res["solution"]["peak_indices"]) == 4


@pytest.mark.parametrize("n", [3, 9])
def test_mic_count_out_of_range(n):
    mics = [[float(i), 1.0] for i in range(n)]
    req = dict(
        room_width=W, room_height=H, sound_speed=C, drift_ppm_limit=50,
        tdoa_tolerance=0.1e-3, position_resolution=0.05, microphones=mics,
        calib_pos_a=PA, calib_pos_b=PB,
        calib_a=[100.0 + 0.001 * i for i in range(n)],
        calib_b=[120.0 + 0.001 * i for i in range(n)],
        candidates=[[{"t": 110.0 + 0.01 * i}] for i in range(n)],
    )
    with pytest.raises(LocatorError) as e:
        solve(req)
    assert e.value.code == "INVALID_INPUT"


def test_collinear_mics_rejected():
    req = scenario()
    req["microphones"] = [[1, 1], [2, 1], [3, 1], [4, 1], [5, 1], [6, 1],
                          [7, 1], [7.5, 1]]
    with pytest.raises(LocatorError) as e:
        solve(req)
    assert e.value.code == "MICS_COLLINEAR"


def test_calib_reversed_rejected():
    req = scenario()
    req["calib_b"][3] = req["calib_a"][3] - 0.001
    with pytest.raises(LocatorError) as e:
        solve(req)
    assert e.value.code == "CALIB_REVERSED"


def test_drift_exceeded_rejected():
    rates = [1.0] * 8
    rates[2] = 1 + 60e-6  # 60 ppm > 50 ppm 上限
    with pytest.raises(LocatorError) as e:
        solve(scenario(rates=rates, drift_limit=50.0))
    assert e.value.code == "DRIFT_EXCEEDED"


def test_drift_at_boundary_accepted():
    rates = [1.0] * 8
    rates[1] = 1 + 50e-6
    res = solve(scenario(rates=rates, drift_limit=50.0))
    assert res["status"] in ("OK", "AMBIGUOUS")


def test_nonpositive_speed_rejected():
    req = scenario()
    req["sound_speed"] = -343
    with pytest.raises(LocatorError) as e:
        solve(req)
    assert e.value.code == "INVALID_INPUT"


def test_target_outside_calibration_window():
    req = scenario()
    # 把 0 号麦的一个候选峰挪到校准窗口之后
    req["candidates"][0][0]["t"] = req["calib_b"][0] + 1.0
    with pytest.raises(LocatorError) as e:
        solve(req)
    assert e.value.code == "TARGET_OUTSIDE_CALIBRATION"


def test_no_consistent_solution():
    # 所有直达峰统一偏移一个不合理的量 -> 无几何一致解
    req = scenario(tol=1e-6, n_peaks=3)
    for i in range(len(req["microphones"])):
        for p in req["candidates"][i]:
            p["t"] += 0.02 * (1 if i % 2 else -1)
    res = solve(req)
    assert res["status"] == "NO_CONSISTENT_SOLUTION"
    assert "nearest" in res


def test_ambiguity_reported():
    # 构造第二个几何完全一致的幻影源：其走时峰同样零残差
    n_mics = 8
    rates = [1.0] * n_mics
    offsets = [0.0] * n_mics
    lt = make_clock(rates, offsets)
    mics = MICS
    phantom = [6.1, 2.2]

    def dist(p, q):
        return math.hypot(p[0] - q[0], p[1] - q[1])

    cal_a = [lt(i, TA + dist(PA, mics[i]) / C) for i in range(n_mics)]
    cal_b = [lt(i, TB + dist(PB, mics[i]) / C) for i in range(n_mics)]
    rng = np.random.default_rng(1)
    cands = []
    for i in range(n_mics):
        direct = lt(i, TE + dist(SRC, mics[i]) / C)
        ghost = lt(i, TE + dist(phantom, mics[i]) / C)
        peaks = [{"t": direct, "strength": 0.15},
                 {"t": ghost, "strength": 0.99}]
        for _ in range(10):
            peaks.append({"t": direct + 0.006 + rng.uniform(0, 0.02),
                          "strength": 0.5})
        rng.shuffle(peaks)
        cands.append(peaks)
    req = dict(room_width=W, room_height=H, sound_speed=C,
               drift_ppm_limit=50, tdoa_tolerance=0.3e-3,
               position_resolution=0.05, microphones=mics,
               calib_pos_a=PA, calib_pos_b=PB, calib_a=cal_a,
               calib_b=cal_b, candidates=cands)
    res = solve(req)
    assert res["status"] == "AMBIGUOUS"
    assert res["cluster_count"] >= 2
    positions = [res["solution"]["position"]] + \
                [a["position"] for a in res["alternatives"]]
    # 两个已知源都应被找到
    found_src = any(math.hypot(p[0] - SRC[0], p[1] - SRC[1]) < 0.05
                    for p in positions)
    found_ghost = any(math.hypot(p[0] - phantom[0], p[1] - phantom[1]) < 0.05
                      for p in positions)
    assert found_src and found_ghost
    # 两位置相距远超分辨率
    a, b = positions[:2]
    assert math.hypot(a[0] - b[0], a[1] - b[1]) > 0.05


def test_tie_break_by_peak_index():
    # 幻影源与真源都零残差时，序号向量字典序更小者获胜
    n_mics = 8
    lt = make_clock([1.0] * n_mics, [0.0] * n_mics)
    phantom = [6.1, 2.2]

    def dist(p, q):
        return math.hypot(p[0] - q[0], p[1] - q[1])

    cal_a = [TA + dist(PA, MICS[i]) / C for i in range(n_mics)]
    cal_b = [TB + dist(PB, MICS[i]) / C for i in range(n_mics)]
    cands = []
    for i in range(n_mics):
        direct = TE + dist(SRC, MICS[i]) / C
        ghost = TE + dist(phantom, MICS[i]) / C
        early, late = sorted((direct, ghost))
        # 两者强度任意：强峰若几何不一致也不能取胜
        cands.append([{"t": early, "strength": 1.0},
                      {"t": late, "strength": 0.1}])
    req = dict(room_width=W, room_height=H, sound_speed=C,
               drift_ppm_limit=50, tdoa_tolerance=0.3e-3,
               position_resolution=0.05, microphones=MICS,
               calib_pos_a=PA, calib_pos_b=PB, calib_a=cal_a,
               calib_b=cal_b, candidates=cands)
    res = solve(req)
    assert res["status"] in ("OK", "AMBIGUOUS")
    # 两个解残差相同（都近似 0），字典序序号向量应胜出
    assert res["solution"]["max_residual"] < 1e-4


def test_performance_8x12_under_5s():
    rates = [1.0, 1 - 4.89e-6, 1 + 28.69e-6, 1 + 15.79e-6,
             1 - 32.47e-6, 1 + 38.05e-6, 1 + 20.89e-6, 1 + 22.89e-6]
    offsets = [0, .00412, -.00731, .00205, -.00388, .00674, -.00193, .00516]
    req = scenario(rates=rates, offsets=offsets, n_peaks=12)
    t0 = time.perf_counter()
    res = solve(req)
    elapsed = time.perf_counter() - t0
    assert res["status"] == "OK"
    assert elapsed < 5.0
