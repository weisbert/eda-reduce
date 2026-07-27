#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_demo_wave.py — 合成波形生成器：造出**真值已知**的回归样例。

为什么要合成的：真实波形没有真值，metrics 算得对不对只能靠肉眼看曲线像不像。
合成信号的极值 / 过冲 / 抖动 / spur dBc 是解析式给出的，
`wave_metrics_*` 算出来的数对不对是**可判定**的。

生成物（都写进 examples/）：

  demo_tran.csv        瞬态，布局 A，表头带单位。4 个信号：
                         V(vdd_pll)  800 mV 基线 + 负载阶跃droop + 阻尼振铃 + 一个窄 glitch
                         V(ctrl)     6.4 ns 时钟，注入了已知的边沿抖动、45% 占空比
                         I(mp0)      负载阶跃电流 + 开关纹波
                         V(vref)     近乎平的参考（验证安静通道不吃点数预算）
                       时间栅格是**事件驱动的非均匀栅格**（模仿 Spectre 自适应步长），
                       并在 250 ns 处刻意做了一次 dt 坍缩。
  demo_ac.csv          AC 扫描，log 频率，y 已经是 dB20（工具不许再换算一次）
  demo_spec.csv        PSS/pnoise 谱，lin 频率，y 是**线性 V**（dBc 得工具自己算），
                       载波 + 底噪 + 三根窄 spur（放在精确的 bin 上，真值是精确的）
  demo_tran_layoutb.csv  布局 B 夹具：两个 time 列、长度不等
  demo_dirty.csv         脏数据夹具：重复/非单调 t、NaN/inf、空单元、引号、尾逗号、单位行
  demo_truth.json      上面所有已知真值，tests 拿它断言

确定性：不用 random 模块的 gauss（实现可能随版本变），自带 Box-Muller + MT19937 的
`random.random()`（这个有向后兼容承诺）。同一个 seed 在任何 CPython 上生成同一份文件。

用法：
    python examples/gen_demo_wave.py                 # 写进 examples/
    python examples/gen_demo_wave.py -o /tmp/big --scale 50   # 压力测试用的大文件

只依赖 Python 标准库 (3.6+)。
"""

import argparse
import json
import math
import os
import random
import sys

# --------------------------------------------------------------- 确定性随机


class Rng(object):
    """Box-Muller on top of MT19937。只用 random.random()，跨版本稳定。"""

    def __init__(self, seed):
        self._r = random.Random(seed)
        self._spare = None

    def uniform(self):
        return self._r.random()

    def gauss(self):
        if self._spare is not None:
            v, self._spare = self._spare, None
            return v
        while True:
            u1 = self._r.random()
            if u1 > 1e-12:
                break
        u2 = self._r.random()
        m = math.sqrt(-2.0 * math.log(u1))
        self._spare = m * math.sin(2.0 * math.pi * u2)
        return m * math.cos(2.0 * math.pi * u2)


# --------------------------------------------------------------- 波形基元


def raised_cos(u):
    """0 -> 1 的 C1 连续过渡，u 在 [0,1] 之外被钳位。"""
    if u <= 0.0:
        return 0.0
    if u >= 1.0:
        return 1.0
    return 0.5 * (1.0 - math.cos(math.pi * u))


# 升余弦过渡的 10%-90% 占总过渡时间的比例（解析值，用来把 tt 反推成给定的 t_rise）
RC_1090 = (math.acos(-0.8) - math.acos(0.8)) / math.pi     # 0.590334...


def gaussian_pulse(t, t0, fwhm):
    sigma = fwhm / 2.354820045
    z = (t - t0) / sigma
    if abs(z) > 8.0:
        return 0.0
    return math.exp(-0.5 * z * z)


# --------------------------------------------------------------- 时间栅格


def build_grid(spans, quantum=1e-15):
    """spans = [(t0, t1, dt), ...] -> 去重排序后的时间点列表。

    模仿 Spectre 自适应步长：在事件附近密、平坦区疏。quantum 用来做去重，
    避免两段 span 的端点因为浮点误差差 1e-19 而变成两个点。
    """
    seen = set()
    out = []
    for t0, t1, dt in spans:
        n = int(math.floor((t1 - t0) / dt + 0.5)) + 1
        for i in range(n):
            t = t0 + i * dt
            if t > t1 + 0.5 * dt:
                break
            k = int(round(t / quantum))
            if k in seen:
                continue
            seen.add(k)
            out.append(k * quantum)
    out.sort()
    return out


# --------------------------------------------------------------- 瞬态模型

TRAN = {
    "t_end": 300e-9,
    # --- V(vdd_pll) 供电 droop
    "v0": 0.800,             # 基线 [V]
    "t_step": 40e-9,         # 负载阶跃时刻
    "droop_a": 0.088,        # 振铃初始幅度 [V]
    "f_ring": 25e6,          # 振铃频率 [Hz]
    "tau_ring": 50e-9,       # 包络时间常数 [s]
    "t_edge": 1.0e-9,        # 阶跃上升沿（升余弦），避免斜率发散
    "glitch_t": 120e-9,
    "glitch_a": -3.2e-3,     # [V]
    "glitch_fwhm": 210e-12,
    "vdd_noise": 0.15e-3,    # [V] rms —— glitch 是它的 21x，必然被检出
    # --- V(ctrl) 时钟
    "clk_t0": 1.0e-9,        # 第一个上升沿中心
    "clk_period": 6.4e-9,
    "clk_duty": 0.45,
    "clk_hi": 1.200,
    "clk_lo": 0.000,
    "clk_rise": 88.0e-12,    # 10-90
    "clk_fall": 92.0e-12,    # 10-90
    "clk_jitter": 3.0e-12,   # 每个边沿独立的高斯抖动 rms
    "clk_noise": 0.02e-3,
    # --- I(mp0)
    "i_dc": 0.900e-3,
    "i_step": 0.600e-3,
    "i_sw": 0.060e-3,        # 开关纹波峰值
    "i_noise": 0.5e-6,
    # --- V(vref)
    "vref": 0.600,
    "vref_drift": 0.8e-3,    # 整个窗口内的线性漂移
    "vref_noise": 0.02e-3,
}


def clock_edges(p, rng):
    """按注入的抖动算出每个边沿的**真实**中心时刻。真值从这里来，不是估的。"""
    rise, fall = [], []
    k = 0
    while True:
        tr = p["clk_t0"] + k * p["clk_period"]
        if tr > p["t_end"]:
            break
        tf = tr + p["clk_duty"] * p["clk_period"]
        rise.append(tr + p["clk_jitter"] * rng.gauss())
        fall.append(tf + p["clk_jitter"] * rng.gauss())
        k += 1
    # 最后一个下降沿可能掉出窗口，砍掉，保证 rise/fall 成对
    while fall and fall[-1] > p["t_end"]:
        fall.pop()
    rise = rise[:len(fall) + 1]
    return rise, fall


def make_tran_model(p, rng):
    """-> (signals, ctx)。signals = [(name, unit, f(t) 无噪, noise_rms)]"""
    rise, fall = clock_edges(p, rng)
    tt_r = p["clk_rise"] / RC_1090
    tt_f = p["clk_fall"] / RC_1090
    swing = p["clk_hi"] - p["clk_lo"]

    def clk_norm(t):
        """0..1 的时钟波形。边沿不重叠，所以直接叠加升/降贡献。"""
        v = 0.0
        for tr in rise:
            d = t - tr
            if -tt_r <= d <= tt_r:
                v += raised_cos(d / tt_r + 0.5)
            elif d > tt_r:
                v += 1.0
        for tf in fall:
            d = t - tf
            if -tt_f <= d <= tt_f:
                v -= raised_cos(d / tt_f + 0.5)
            elif d > tt_f:
                v -= 1.0
        return v

    def v_ctrl(t):
        return p["clk_lo"] + swing * clk_norm(t)

    def v_vdd(t):
        v = p["v0"]
        d = t - p["t_step"]
        if d > 0.0:
            v -= (p["droop_a"] * raised_cos(d / p["t_edge"])
                  * math.exp(-d / p["tau_ring"])
                  * math.cos(2.0 * math.pi * p["f_ring"] * d))
        v += p["glitch_a"] * gaussian_pulse(t, p["glitch_t"], p["glitch_fwhm"])
        return v

    def i_mp0(t):
        d = t - p["t_step"]
        i = p["i_dc"]
        if d > 0.0:
            i += p["i_step"] * raised_cos(d / p["t_edge"])
        i += p["i_sw"] * (clk_norm(t) - p["clk_duty"])
        return i

    def v_vref(t):
        return p["vref"] + p["vref_drift"] * (t / p["t_end"])

    signals = [
        ("V(vdd_pll)", "V", v_vdd, p["vdd_noise"]),
        ("V(ctrl)", "V", v_ctrl, p["clk_noise"]),
        ("I(mp0)", "A", i_mp0, p["i_noise"]),
        ("V(vref)", "V", v_vref, p["vref_noise"]),
    ]
    return signals, {"rise": rise, "fall": fall, "tt_r": tt_r, "tt_f": tt_f}


def tran_grid(p, scale=1):
    """事件驱动栅格。scale 放大点密度（压力测试用）。"""
    s = float(scale)
    T, t0 = p["clk_period"], p["clk_t0"]
    spans = [
        (0.0, p["t_end"], 2e-9 / s),                              # 背景
        (39.8e-9, 42.0e-9, 10e-12 / s),                           # droop 谷底
        (42.0e-9, 48.0e-9, 50e-12 / s),                           # 第一个振铃周期
        (48.0e-9, 210e-9, 300e-12 / s),                           # 振铃衰减段
        (119.4e-9, 120.6e-9, 10e-12 / s),                         # glitch
        (250.0e-9, 250.06e-9, 2e-12 / s),                         # dt 坍缩（收敛挣扎）
    ]
    k = 0
    while True:                                                    # 每个时钟边沿加密
        tr = t0 + k * T
        if tr > p["t_end"]:
            break
        for c in (tr, tr + p["clk_duty"] * T):
            if c <= p["t_end"]:
                spans.append((max(0.0, c - 105e-12), min(p["t_end"], c + 105e-12),
                              15e-12 / s))
        k += 1
    return build_grid(spans)


# --------------------------------------------------------------- 瞬态真值


def _dense(f, t0, t1, n):
    dt = (t1 - t0) / (n - 1)
    return [(t0 + i * dt, f(t0 + i * dt)) for i in range(n)]


def tran_truth(p, signals, ctx):
    """在无噪解析模型上、用极密栅格算真值。测量值应当落在它附近。"""
    t_end = p["t_end"]
    f_vdd = signals[0][2]
    f_i = signals[2][2]

    # --- vdd_pll: 极值 / 过冲 / settle / slew
    # 谷底在阶跃后 ~1 ns，单独用 1 fs 级栅格扫；其余段用 20 ps
    fine = _dense(f_vdd, p["t_step"] - 0.2e-9, p["t_step"] + 3.0e-9, 64001)
    coarse = _dense(f_vdd, 0.0, t_end, 300001)
    allpts = fine + coarse
    vmin_t, vmin = min(allpts, key=lambda a: a[1])[0], min(a[1] for a in allpts)
    vmax_t, vmax = max(allpts, key=lambda a: a[1])[0], max(a[1] for a in allpts)

    v0 = p["v0"]
    band = 0.01 * abs(v0)
    settle = 0.0
    for t, v in sorted(coarse):
        if abs(v - v0) > band:
            settle = t
    # 过冲 = 阶跃后最大的正向偏离（相对终值 v0）
    over = 0.0
    over_t = 0.0
    for t, v in coarse:
        if t > p["t_step"] and v - v0 > over:
            over, over_t = v - v0, t
    # 最大斜率：解析模型在阶跃沿上最陡，用 1 ps 栅格差分
    slew, slew_t = 0.0, 0.0
    sd = _dense(f_vdd, p["t_step"] - 0.1e-9, p["t_step"] + 1.2e-9, 13001)
    for i in range(1, len(sd)):
        g = (sd[i][1] - sd[i - 1][1]) / (sd[i][0] - sd[i - 1][0])
        if abs(g) > abs(slew):
            slew, slew_t = g, 0.5 * (sd[i][0] + sd[i - 1][0])

    # --- ctrl: 周期 / 抖动 / 占空比（直接来自注入的边沿时刻，精确）
    rise, fall = ctx["rise"], ctx["fall"]
    periods = [rise[i + 1] - rise[i] for i in range(len(rise) - 1)]
    pm = sum(periods) / len(periods)
    pvar = sum((x - pm) ** 2 for x in periods) / (len(periods) - 1)
    duty = [(fall[i] - rise[i]) / periods[i] for i in range(len(periods))
            if i < len(fall)]

    # --- I(mp0): 时间加权均值 + 纹波 rms（梯形积分，栅格非均匀）
    dn = _dense(f_i, 0.0, t_end, 400001)
    area = 0.0
    for i in range(1, len(dn)):
        area += 0.5 * (dn[i][1] + dn[i - 1][1]) * (dn[i][0] - dn[i - 1][0])
    imean = area / t_end
    acc = 0.0
    for i in range(1, len(dn)):
        a = dn[i - 1][1] - imean
        b = dn[i][1] - imean
        acc += 0.5 * (a * a + b * b) * (dn[i][0] - dn[i - 1][0])
    irms = math.sqrt(acc / t_end)

    return {
        "V(vdd_pll)": {
            "baseline": v0,
            "min": vmin, "min_at": vmin_t,
            "max": vmax, "max_at": vmax_t,
            "pp": vmax - vmin,
            "settle_1pct": settle,
            "overshoot_pct": 100.0 * over / abs(v0), "overshoot_at": over_t,
            "slew_max": slew, "slew_max_at": slew_t,
            "f_ring": p["f_ring"], "tau_ring": p["tau_ring"],
            "glitch_at": p["glitch_t"], "glitch_depth": p["glitch_a"],
            "glitch_fwhm": p["glitch_fwhm"],
            "noise_rms": p["vdd_noise"],
        },
        "V(ctrl)": {
            "n_cycles": len(periods),
            "period": pm,
            "jitter_rms": math.sqrt(pvar),
            "jitter_pp": max(periods) - min(periods),
            "edge_jitter_rms": p["clk_jitter"],
            "duty": sum(duty) / len(duty),
            "rise_1090": p["clk_rise"], "fall_1090": p["clk_fall"],
            "vlo": p["clk_lo"], "vhi": p["clk_hi"],
            "first_rise": rise[0], "n_rise": len(rise),
        },
        "I(mp0)": {
            "mean": imean, "rms_ripple": irms,
            "dc": p["i_dc"], "step": p["i_step"], "sw_amp": p["i_sw"],
        },
        "V(vref)": {
            "min": p["vref"], "max": p["vref"] + p["vref_drift"],
            "mean": p["vref"] + 0.5 * p["vref_drift"],
        },
    }


# --------------------------------------------------------------- AC / 谱

AC = {
    "f0": 10.0, "f1": 10e9, "per_decade": 120,
    "a0_db": -60.0,        # DC PSRR
    "fz": 1e6,             # 实零点
    "fp": 100e6,           # 复极点对
    "q": 3.0,
}

SPEC = {
    "df": 1.25e6, "n": 4001,           # 0 .. 5 GHz，lin 栅格
    "carrier": 2.400e9, "carrier_v": 1.0,
    "floor_dbc": -120.0,
    "hwhm_bins": 1.5,
    # (频率, dBc)  —— 都落在精确的 bin 上，所以真值是精确的
    "spurs": [(2.500e9, -52.0), (4.800e9, -38.0), (0.100e9, -67.0)],
}


def ac_rows(p):
    n_dec = math.log10(p["f1"] / p["f0"])
    n = int(round(n_dec * p["per_decade"])) + 1
    a0 = 10.0 ** (p["a0_db"] / 20.0)
    wz = 2.0 * math.pi * p["fz"]
    w0 = 2.0 * math.pi * p["fp"]
    rows = []
    for i in range(n):
        f = p["f0"] * 10.0 ** (i * n_dec / (n - 1))
        w = 2.0 * math.pi * f
        # H = a0 (1 + jw/wz) / (1 - (w/w0)^2 + j w/(Q w0))
        nr, ni = 1.0, w / wz
        dr, di = 1.0 - (w / w0) ** 2, w / (p["q"] * w0)
        den = dr * dr + di * di
        hr = a0 * (nr * dr + ni * di) / den
        hi = a0 * (ni * dr - nr * di) / den
        mag = math.hypot(hr, hi)
        rows.append((f, 20.0 * math.log10(mag), math.degrees(math.atan2(hi, hr))))
    return rows


def ac_truth(p, rows):
    peak = max(rows[1:], key=lambda r: r[1])
    dc = rows[0][1]
    # -3 dB 相对峰值的高频回落点
    f3 = None
    for i in range(1, len(rows)):
        if rows[i][1] < peak[1] - 3.0 and rows[i][0] > peak[0]:
            f3 = rows[i][0]
            break
    return {
        "dc_db": dc, "peak_db": peak[1], "peak_f": peak[0],
        "f_3db_hi": f3, "fp": p["fp"], "fz": p["fz"], "q": p["q"],
        "n": len(rows), "xscale": "log",
    }


def spec_rows(p, rng):
    car_lin = p["carrier_v"]
    floor = car_lin * 10.0 ** (p["floor_dbc"] / 20.0)
    hw = p["hwhm_bins"] * p["df"]
    tones = [(p["carrier"], car_lin)]
    for f, dbc in p["spurs"]:
        tones.append((f, car_lin * 10.0 ** (dbc / 20.0)))
    rows = []
    for k in range(p["n"]):
        f = k * p["df"]
        # 底噪：瑞利分布的幅度，模仿真实谱的起伏（相位随机 -> 幅度瑞利）
        u = max(rng.uniform(), 1e-12)
        v = floor * math.sqrt(-math.log(u))
        for tf, ta in tones:                       # 洛伦兹线型，1~3 bin 宽
            d = (f - tf) / hw
            v += ta / (1.0 + d * d)
        rows.append((f, v))
    return rows


def spec_truth(p):
    out = {"carrier_f": p["carrier"], "carrier_v": p["carrier_v"],
           "floor_dbc": p["floor_dbc"], "df": p["df"], "n": p["n"],
           "xscale": "lin", "spurs": []}
    for f, dbc in sorted(p["spurs"]):
        out["spurs"].append({
            "f": f, "dbc": dbc,
            "v": p["carrier_v"] * 10.0 ** (dbc / 20.0),
            "f_over_f0": f / p["carrier"],
        })
    return out


# --------------------------------------------------------------- 写文件


def write_csv(path, header, rows, fmts):
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(",".join(header) + "\n")
        for r in rows:
            fh.write(",".join(f % v for f, v in zip(fmts, r)) + "\n")
    return os.path.getsize(path)


def write_layout_b(path, t, cols):
    """布局 B：每条 trace 自带 x 列，长度**不等**（短的那列先空）。

    这是 ViVA 把不同 sweep 的 trace 叠在一张图上导出时的样子。
    解析器必须认出第二个 time 列是 x 而不是信号。
    """
    a = list(range(0, len(t), 3))            # trace 1 —— 抽 1/3
    b = list(range(0, len(t), 7))            # trace 2 —— 抽 1/7，短得多
    n = max(len(a), len(b))
    lines = ["time,V(vdd_pll),time,I(mp0)"]
    for i in range(n):
        c = []
        c.append("%.10g" % t[a[i]] if i < len(a) else "")
        c.append("%.7g" % cols[0][a[i]] if i < len(a) else "")
        c.append("%.10g" % t[b[i]] if i < len(b) else "")
        c.append("%.7g" % cols[2][b[i]] if i < len(b) else "")
        lines.append(",".join(c))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")
    return len(a), len(b)


DIRTY = '''"time","V(out)","I(load)",
"s","V","A",
0,0.8000,1.0e-3,
1e-9,0.8001,1.01e-3,
1e-9,0.8001,1.01e-3,
2e-9,nan,1.02e-3,
3e-9,0.8003,,
4e-9,inf,1.04e-3,
5e-9,0.8005,1.05e-3,
4.5e-9,0.8004,1.045e-3,
6e-9,0.8006,1.06e-3,
6.000001e-9,0.8006,1.06e-3,
6.000002e-9,0.8006,1.06e-3,
6.000003e-9,0.8006,1.06e-3,
8e-9,0.8008,1.08e-3,
10e-9,0.8010,1.10e-3,
'''

DIRTY_TRUTH = {
    "raw_rows": 14,
    "kept_rows": 10,          # 去掉 1 个重复 t、1 个 NaN 行、1 个 inf 行、1 个非单调行
    "expect_notes": ["dup", "nonmono", "nan", "inf", "blank", "dt"],
    "signals": ["V(out)", "I(load)"],
    "units": {"time": "s", "V(out)": "V", "I(load)": "A"},
}


def main():
    ap = argparse.ArgumentParser(description="生成真值已知的合成波形样例")
    ap.add_argument("-o", "--outdir", default=os.path.dirname(os.path.abspath(__file__)))
    ap.add_argument("--scale", type=float, default=1.0,
                    help="点密度倍数；--scale 50 造 ~13 万点的压力测试文件")
    ap.add_argument("--seed", type=int, default=20260727)
    args = ap.parse_args()

    d = args.outdir
    if not os.path.isdir(d):
        os.makedirs(d)

    # ---------------- 瞬态
    rng = Rng(args.seed)
    p = dict(TRAN)
    signals, ctx = make_tran_model(p, rng)
    t = tran_grid(p, args.scale)
    nrng = Rng(args.seed + 1)
    cols = []
    for name, unit, f, sigma in signals:
        cols.append([f(tt) + sigma * nrng.gauss() for tt in t])

    rows = [[t[i]] + [c[i] for c in cols] for i in range(len(t))]
    header = ["time (s)"] + ["%s (%s)" % (n_, u) for n_, u, _, _ in signals]
    fmts = ["%.10g"] + ["%.7g"] * len(signals)
    sz = write_csv(os.path.join(d, "demo_tran.csv"), header, rows, fmts)
    print("demo_tran.csv        %6d pts  %5d cols  %7.1f KB"
          % (len(t), len(signals), sz / 1024.0))

    na, nb = write_layout_b(os.path.join(d, "demo_tran_layoutb.csv"), t, cols)
    print("demo_tran_layoutb.csv  trace1=%d pts  trace2=%d pts (ragged)" % (na, nb))

    with open(os.path.join(d, "demo_dirty.csv"), "w",
              encoding="utf-8", newline="\n") as fh:
        fh.write(DIRTY)
    print("demo_dirty.csv       脏数据夹具")

    # ---------------- AC
    arows = ac_rows(AC)
    sz = write_csv(os.path.join(d, "demo_ac.csv"),
                   ["freq (Hz)", "dB20(V(out)) (dB)", "phase(V(out)) (deg)"],
                   arows, ["%.8g", "%.6f", "%.4f"])
    print("demo_ac.csv          %6d pts  log f  y 已是 dB20  %7.1f KB"
          % (len(arows), sz / 1024.0))

    # ---------------- 谱
    srng = Rng(args.seed + 2)
    srows = spec_rows(SPEC, srng)
    sz = write_csv(os.path.join(d, "demo_spec.csv"),
                   ["freq", "V(vco_out)"], srows, ["%.9g", "%.6e"])
    print("demo_spec.csv        %6d pts  lin f  y 线性 V  无单位行  %7.1f KB"
          % (len(srows), sz / 1024.0))

    # ---------------- 真值
    truth = {
        "_note": "由 gen_demo_wave.py 生成；解析式/精密栅格给出，不是量出来的",
        "seed": args.seed,
        "scale": args.scale,
        "demo_tran.csv": {
            "kind": "tran", "n": len(t), "t_end": p["t_end"],
            "x_unit": "s", "xscale": "lin",
            "dt_collapse_at": 250.0e-9,
            "signals": tran_truth(p, signals, ctx),
        },
        "demo_ac.csv": {"kind": "freq", "signals": {"dB20(V(out))": ac_truth(AC, arows)}},
        "demo_spec.csv": {"kind": "freq", "signals": {"V(vco_out)": spec_truth(SPEC)}},
        "demo_tran_layoutb.csv": {"traces": 2, "n": [na, nb]},
        "demo_dirty.csv": DIRTY_TRUTH,
    }
    tp = os.path.join(d, "demo_truth.json")
    with open(tp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(truth, fh, indent=1, sort_keys=True)
        fh.write("\n")
    print("demo_truth.json      真值 %d 字节" % os.path.getsize(tp))

    tv = truth["demo_tran.csv"]["signals"]
    print("\n埋进去的东西（真值）：")
    print("  droop  min %.6f V @ %.4f ns   过冲 +%.3f%% @ %.2f ns   settle(1%%) %.2f ns"
          % (tv["V(vdd_pll)"]["min"], tv["V(vdd_pll)"]["min_at"] * 1e9,
             tv["V(vdd_pll)"]["overshoot_pct"], tv["V(vdd_pll)"]["overshoot_at"] * 1e9,
             tv["V(vdd_pll)"]["settle_1pct"] * 1e9))
    print("  glitch %.2f mV @ %.1f ns  FWHM %.0f ps"
          % (p["glitch_a"] * 1e3, p["glitch_t"] * 1e9, p["glitch_fwhm"] * 1e12))
    print("  clk    T %.6f ns  jitter rms %.3f ps pp %.3f ps  duty %.3f%%  N=%d"
          % (tv["V(ctrl)"]["period"] * 1e9, tv["V(ctrl)"]["jitter_rms"] * 1e12,
             tv["V(ctrl)"]["jitter_pp"] * 1e12, tv["V(ctrl)"]["duty"] * 100.0,
             tv["V(ctrl)"]["n_cycles"]))
    sp = truth["demo_spec.csv"]["signals"]["V(vco_out)"]
    print("  spur   " + " | ".join("%.3f GHz %.1f dBc (f/f0=%.5f)"
                                   % (s["f"] / 1e9, s["dbc"], s["f_over_f0"])
                                   for s in sp["spurs"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
