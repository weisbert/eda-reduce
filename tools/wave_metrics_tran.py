#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wave_metrics_tran.py — 瞬态波形的**测量**。

为什么这些必须脚本做（wave-spec 第 0.1 节）：

    脚本看得见全分辨率数据，模型永远看不见。
    峰值的精确位置、周期抖动、settling time、最大 slew —— 需要全分辨率才算得出，
    脚本不算就永远丢了。模型拿到 400 个点之后再怎么聪明也算不回来。

所以这一层只出**数**，不出形容词。「这个过冲算不算问题」是模型的活。
不确定的地方声明不确定度（比如 slew 的测量窗口、glitch 搜索覆盖了多少区间），
不要猜一个数糊过去。

每个测量都有**适用性判断**：时钟不报 settling time，平信号不报过冲。
不适用就不输出这一项，而不是输出一个没意义的数。

依赖：纯标准库。numpy 在这里也没什么可加速的（全是一遍扫）。
"""

import math

import wave_emit
from wave_core import eng_str

# glitch 搜索的窗口阶梯（乘以局部采样间隔）和物理上限
GLITCH_LAGS = (2, 4, 8, 16, 32)
GLITCH_K = 6.0                 # 超过噪声底这么多倍才算
GLITCH_WMAX_FRAC = 1.0 / 200   # 窗口宽于全长的 1/200 就不叫 glitch 了
EDGE_HYST = 0.15               # 阈值穿越的迟滞，占摆幅比例
MIN_CYCLES = 4                 # 少于这么多周期就不当周期信号
PERIOD_CV = 0.25               # 周期变异系数超过这个值就不当周期信号
MAX_EDGE_EVENTS = 8            # EVENTS 里最多列几个边沿，超了报统计


# --------------------------------------------------------------- 小工具


def _pct(sorted_y, p):
    if not sorted_y:
        return 0.0
    i = min(len(sorted_y) - 1, max(0, int(p * (len(sorted_y) - 1) + 0.5)))
    return sorted_y[i]


def _sample_sorted(y, cap=40000):
    step = max(1, len(y) // cap)
    s = list(y[::step])
    s.sort()
    return s


def _trapz(x, y):
    """时间加权积分。栅格是非均匀的，直接平均是错的。"""
    a = 0.0
    for i in range(1, len(x)):
        a += 0.5 * (y[i] + y[i - 1]) * (x[i] - x[i - 1])
    return a


def _refine_peak(x, y, i):
    """抛物线顶点做亚采样定位。栅格非均匀，用拉格朗日形式。

    极值落在采样点之间是常态：demo 里 10 ps 栅格上的谷底，不精修就有半个 dt 的
    系统偏差。端点和局部不是真峰的情况直接退回采样点。
    """
    n = len(y)
    if i <= 0 or i >= n - 1:
        return x[i], y[i]
    x0, x1, x2 = x[i - 1], x[i], x[i + 1]
    y0, y1, y2 = y[i - 1], y[i], y[i + 1]
    d0, d1, d2 = (x0 - x1) * (x0 - x2), (x1 - x0) * (x1 - x2), (x2 - x0) * (x2 - x1)
    if d0 == 0 or d1 == 0 or d2 == 0:
        return x[i], y[i]
    a = y0 / d0 + y1 / d1 + y2 / d2
    b = -(y0 * (x1 + x2) / d0 + y1 * (x0 + x2) / d1 + y2 * (x0 + x1) / d2)
    if a == 0:
        return x[i], y[i]
    xv = -b / (2.0 * a)
    if not (x0 < xv < x2):                     # 顶点跑出三点之外 = 不是真峰
        return x[i], y[i]
    c = y1 - a * x1 * x1 - b * x1
    return xv, a * xv * xv + b * xv + c


def _cross(x, y, i, thr):
    """在 [i, i+1] 上线性插值求阈值穿越时刻。

    取采样点会把抖动分辨率锁死在 dt 上 —— demo 里注入的抖动 3 ps、dt 15 ps，
    不插值根本量不出来。
    """
    y0, y1 = y[i], y[i + 1]
    if y1 == y0:
        return x[i]
    return x[i] + (thr - y0) * (x[i + 1] - x[i]) / (y1 - y0)


def _stdev(v):
    n = len(v)
    if n < 2:
        return 0.0
    m = sum(v) / n
    return math.sqrt(sum((t - m) ** 2 for t in v) / (n - 1))


# --------------------------------------------------------------- 每个信号


class SigStat(object):
    """一个信号的全部测量结果。什么该报什么不该报，由这里的标志位决定。"""

    def __init__(self, sig, label):
        self.sig = sig
        self.label = label
        self.periodic = False
        self.has_step = False
        self.glitches = []
        self.edges_r = []
        self.edges_f = []
        self.notes = []


class TranMetrics(wave_emit.Metrics):
    kind = "tran"

    def __init__(self, trace):
        wave_emit.Metrics.__init__(self, trace)
        self.stats = []
        self._forced = set()

    # ------------------------------------------------------------ 主流程

    def analyze(self):
        tr = self.trace
        x = tr.x
        span = x[-1] - x[0]
        for s in tr.signals:
            st = SigStat(s, self.label(s))
            y = s.y
            n = len(y)
            srt = _sample_sorted(y)
            st.p05, st.p95 = _pct(srt, 0.05), _pct(srt, 0.95)
            st.med = _pct(srt, 0.5)

            # --- 极值（亚采样精修）
            st.min_at, st.vmin = _refine_peak(x, y, s.vmin_at)
            st.max_at, st.vmax = _refine_peak(x, y, s.vmax_at)
            st.pp = st.vmax - st.vmin
            self._forced.add(s.vmin_at)
            self._forced.add(s.vmax_at)

            # --- 时间加权均值 / rms / 纹波
            if span > 0:
                st.mean = _trapz(x, y) / span
                st.rms = math.sqrt(max(0.0, _trapz(x, [v * v for v in y]) / span))
                st.ripple = math.sqrt(max(0.0, _trapz(
                    x, [(v - st.mean) ** 2 for v in y]) / span))
            else:
                st.mean = st.rms = st.ripple = y[0]

            self._classify(st, x, y)
            if st.periodic:
                self._clock(st, x, y)
            else:
                self._step(st, x, y, span)
                self._glitch(st, x, y, span)
            self.stats.append(st)

    # ------------------------------------------------------------ 分类

    def _classify(self, st, x, y):
        """周期信号 / 阶跃信号 / 慢漂移，靠数据判，不靠信号名猜。"""
        s = st.sig
        swing = st.p95 - st.p05
        if swing <= 4.0 * max(s.noise, 1e-300):
            st.notes.append("摆幅 %s 不到噪声底的 4 倍，只报统计量"
                            % eng_str(swing, s.unit, 3))
            return
        thr = 0.5 * (st.p05 + st.p95)
        hy = EDGE_HYST * swing
        # 迟滞只用来**确认**这是条真边沿，穿越时刻仍取 thr 的那一段。
        # 直接在 thr+hy 那一段插值是错的：88 ps 的沿上只有六七个采样点，
        # thr 和 thr+hy 常常不在同一段里，插出来的时刻会偏，抖动就量歪了。
        rise, fall = [], []
        hi = y[0] > thr
        pend = None
        for i in range(len(y) - 1):
            v = y[i + 1]
            if not hi:
                if pend is None and v > thr:
                    pend = _cross(x, y, i, thr)
                if pend is not None:
                    if v > thr + hy:
                        rise.append(pend)
                        pend, hi = None, True
                    elif v < thr - hy:
                        pend = None            # 没爬到高电平，是毛刺不是沿
            else:
                if pend is None and v < thr:
                    pend = _cross(x, y, i, thr)
                if pend is not None:
                    if v < thr - hy:
                        fall.append(pend)
                        pend, hi = None, False
                    elif v > thr + hy:
                        pend = None
        st.edges_r, st.edges_f = rise, fall
        if len(rise) >= MIN_CYCLES + 1:
            per = [rise[i + 1] - rise[i] for i in range(len(rise) - 1)]
            m = sum(per) / len(per)
            if m > 0 and _stdev(per) / m < PERIOD_CV:
                st.periodic = True
                return
        # 首尾电平差得比噪声多很多 -> 有阶跃；否则当作回到原位的暂态/漂移
        st.has_step = len(rise) + len(fall) >= 1

    # ------------------------------------------------------------ 时钟

    def _clock(self, st, x, y):
        s = st.sig
        rise, fall = st.edges_r, st.edges_f
        per = [rise[i + 1] - rise[i] for i in range(len(rise) - 1)]
        st.period = sum(per) / len(per)
        st.jit_rms = _stdev(per)
        st.jit_pp = max(per) - min(per)
        st.ncyc = len(per)
        st.vlo, st.vhi = st.p05, st.p95
        duty = []
        for i in range(len(per)):
            f = [t for t in fall if rise[i] < t < rise[i + 1]]
            if len(f) == 1:
                duty.append((f[0] - rise[i]) / per[i])
        st.duty = sum(duty) / len(duty) if duty else None
        st.rise1090 = self._edge_time(x, y, rise, st, up=True)
        st.fall1090 = self._edge_time(x, y, fall, st, up=False)
        # 边沿点必须留住：SHAPE 里塌了，方波就变成三角波
        self._force_near(x, rise + fall)

    def _edge_time(self, x, y, edges, st, up):
        """每条边沿量一次 10-90，取中位数（对个别被噪声打歪的边沿稳健）。"""
        lo, hi = st.vlo, st.vhi
        t10, t90 = lo + 0.1 * (hi - lo), lo + 0.9 * (hi - lo)
        out = []
        n = len(y)
        for tc in edges[:400]:
            i = _bisect_x(x, tc)
            a = b = None
            rng = range(max(0, i - 60), min(n - 1, i + 60))
            for j in rng:
                y0, y1 = y[j], y[j + 1]
                for thr, slot in ((t10, 0), (t90, 1)):
                    if (y0 - thr) * (y1 - thr) <= 0 and y0 != y1:
                        t = _cross(x, y, j, thr)
                        if abs(t - tc) > 0.5 * getattr(st, "period", 1e30):
                            continue
                        if slot == 0 and (a is None or abs(t - tc) < abs(a - tc)):
                            a = t
                        if slot == 1 and (b is None or abs(t - tc) < abs(b - tc)):
                            b = t
            if a is not None and b is not None:
                d = (b - a) if up else (a - b)
                if d > 0:
                    out.append(d)
        if not out:
            return None
        out.sort()
        return out[len(out) // 2]

    # ------------------------------------------------------------ 阶跃

    def _step(self, st, x, y, span):
        """过冲 / settling / 最大 slew。终值取尾部 5% 的中位数（抗噪）。"""
        s = st.sig
        n = len(y)
        sigma = max(s.noise, 1e-300)
        tail = [y[i] for i in range(int(n * 0.95), n)]
        tail.sort()
        st.final = tail[len(tail) // 2] if tail else y[-1]
        ref = abs(st.final) if abs(st.final) > 0.1 * st.pp else st.pp
        st.slew = self._slew(st, x, y)
        t_step = st.slew[1]

        st.settle = {}
        for frac in (0.01, 0.001):
            band = frac * ref
            # 容差带比噪声还窄就测不了 —— 说出来，别报一个被噪声决定的数
            if band < 6.0 * sigma:
                st.settle[frac] = (None, band, "带宽 %s < 6x 噪声底 %s，测不了"
                                   % (eng_str(band, s.unit, 3),
                                      eng_str(sigma, s.unit, 3)))
                continue
            last = None
            for i in range(n):
                if abs(y[i] - st.final) > band:
                    last = x[i]
            if last is None or last <= x[0]:
                continue                       # 从没出过带，这一项没意义
            if last >= x[-1] - 0.05 * span:
                st.settle[frac] = (None, band, "到窗口结束仍在带外，未 settle")
            else:
                st.settle[frac] = (last, band, "")

        # 过冲/回冲只在**阶跃之后**找。阶跃前的初始电平不是过冲：
        # I(mp0) 从 0.87 mA 跳到 1.5 mA，把 0.87 当"回冲 -42%"是胡说。
        head = sorted(y[i] for i in range(max(1, int(n * 0.05))))
        st.init = head[len(head) // 2]
        st.drift = st.final - st.init
        i0 = _bisect_x(x, t_step)
        # 有净电平变化（I(mp0) 从 0.87 跳到 1.5 mA）时，从初值走到终值的那段爬升
        # 不是回冲 —— 要跳到第一次够到终值之后。不这么做会报出 -21% 的假回冲。
        # 没有净电平变化（V(vdd_pll) 掉下去又回来）时不能跳，那个坑本身就是要报的量。
        if abs(st.drift) > 6.0 * sigma:
            s0 = 1.0 if st.init > st.final else -1.0
            for i in range(i0, n):
                if (y[i] - st.final) * s0 <= 0.0:
                    i0 = i
                    break
        over, over_i, under, under_i = 0.0, None, 0.0, None
        for i in range(i0, n):
            d = y[i] - st.final
            if d > over:
                over, over_i = d, i
            if -d > under:
                under, under_i = -d, i
        floor = max(4.0 * sigma, 0.005 * ref)   # 4σ 且 >0.5% —— 否则报的是噪声/漂移
        st.over = st.under = None
        if over > floor and over_i is not None:
            t, v = _refine_peak(x, y, over_i)
            st.over = (100.0 * over / ref, t, v)
            self._forced.add(over_i)
        if under > floor and under_i is not None:
            t, v = _refine_peak(x, y, under_i)
            st.under = (100.0 * under / ref, t, v)
            self._forced.add(under_i)

    def _slew(self, st, x, y):
        """最大 |dy/dx|，**并把测量窗口报出来**。

        窗口不报出来这个数就没意义：dt 坍缩的区间里两点相隔 2 ps，
        150 uV 的噪声除以 2 ps 就是 75 kV/us —— 纯噪声，比真实斜率大三个数量级。
        所以先用 3 倍中位 dt 量一遍，再按噪声占比把窗口放宽到噪声贡献 <5%。
        """
        s = st.sig
        n = len(y)
        med = self.trace.dt_med

        def scan(h):
            best, bi = 0.0, 0
            j = 0
            for i in range(n - 1):
                if j < i + 1:
                    j = i + 1
                while j < n - 1 and x[j] - x[i] < h:
                    j += 1
                dx = x[j] - x[i]
                if dx <= 0:
                    continue
                g = (y[j] - y[i]) / dx
                if abs(g) > abs(best):
                    best, bi = g, i
            return best, bi

        h0 = max(3.0 * med, 1e-300)
        g0, i0 = scan(h0)
        h = h0
        if g0 != 0.0 and s.noise > 0:
            need = 1.414 * s.noise / (0.05 * abs(g0))
            if need > h0:
                h = min(need, 0.02 * (x[-1] - x[0]))
                g0, i0 = scan(h)
        self._forced.add(i0)
        return (g0, x[i0], h)

    # ------------------------------------------------------------ glitch

    def _glitch(self, st, x, y, span):
        """窄尖峰检测。多个窗口尺度扫，但窗口有物理上限。

        为什么要上限：背景栅格 2 ns 的区间里，往两边各跨 16 个点就是 ±32 ns，
        25 MHz 振铃在这个跨度上的弦偏差有几十 mV —— 满屏假 glitch。
        栅格太粗的地方**根本测不了**窄尖峰，那就如实说「这段没搜」，
        而不是报一堆猜的。
        """
        s = st.sig
        n = len(y)
        sigma = s.noise
        if sigma <= 0 or n < 32:
            return
        wmax = span * GLITCH_WMAX_FRAC
        thr = GLITCH_K * sigma * 1.2247            # 弦偏差的噪声放大 sqrt(1.5)
        searched = [False] * n
        dev = {}                                   # (i, m) -> 弦偏差

        def chord(i, m):
            a, b = i - m, i + m
            if a < 0 or b >= n:
                return None, 0.0
            w = x[b] - x[a]
            if w <= 0 or w > wmax:
                return None, w
            r = (x[i] - x[a]) / w
            return y[i] - (y[a] + r * (y[b] - y[a])), w

        best = {}
        for m in GLITCH_LAGS:
            for i in range(m, n - m):
                d, w = chord(i, m)
                if d is None:
                    continue
                searched[i] = True
                dev[(i, m)] = d
                if abs(d) <= thr:
                    continue
                # 尖峰要回到基线：两侧电平差不多，否则那是一条边沿不是 glitch
                if abs(y[i + m] - y[i - m]) > 0.5 * abs(d):
                    continue
                # **窄不窄**：光滑曲率的弦偏差 ∝ w²，窗口缩到 1/4 就只剩 1/16；
                # 真的窄尖峰在小窗口里几乎不衰减。不加这一条，
                # 阶跃刚过去那段振铃的曲率会被报成一串假 glitch。
                ds, _ = chord(i, max(1, m // 4))
                if ds is None or abs(ds) < 0.35 * abs(d):
                    continue
                if i not in best or abs(d) > abs(best[i][0]):
                    best[i] = (d, m, w)
        cov = sum(1 for v in searched if v) / float(n)
        if cov < 0.999:
            st.notes.append("glitch 搜索覆盖 %.0f%% 的采样点；其余区间栅格比 %s 还粗，"
                            "窄尖峰在那里本来就测不出来"
                            % (100.0 * cov, eng_str(wmax, self.trace.xunit, 2)))
        if not best:
            return
        # 相邻的候选点归并成一个 glitch，取峰
        idx = sorted(best)
        groups, cur = [], [idx[0]]
        for k in idx[1:]:
            if k - cur[-1] <= 2 * GLITCH_LAGS[-1]:
                cur.append(k)
            else:
                groups.append(cur)
                cur = [k]
        groups.append(cur)
        for g in groups:
            pk = max(g, key=lambda k: abs(best[k][0]))
            d, m, w = best[pk]
            t, _ = _refine_peak(x, y, pk)
            # 本地采样间隔：宽度量不出来时报的是「< 这个数」而不是 0。
            # 宽度为 0 的跳变不是物理量，是「只落到了一个采样点上」。
            dtl = min([x[k] - x[k - 1] for k in (pk, pk + 1)
                       if 0 < k < n] or [0.0])
            st.glitches.append({
                "at": t, "depth": d, "width": self._fwhm(x, y, pk, d, w),
                "ratio": abs(d) / sigma, "win": w, "i": pk, "dtl": dtl,
            })
            for k in (pk - m, pk, pk + m):       # 峰和两侧肩点都得留住
                if 0 <= k < n:
                    self._forced.add(k)
        st.glitches.sort(key=lambda gg: -abs(gg["depth"]))
        del st.glitches[8:]

    def _fwhm(self, x, y, i, depth, w):
        """半高全宽：从峰往两边找偏离降到一半的地方。

        搜索范围按**物理宽度**卡在检测窗口的 ±1 倍，不能按下标算 ——
        栅格是非均匀的，往外走几十个点可能就跨进 2 ns 的背景区，
        基线取错了宽度能报大 70 倍。
        """
        n = len(y)
        a, b = i, i
        while a > 0 and x[i] - x[a] < w:
            a -= 1
        while b < n - 1 and x[b] - x[i] < w:
            b += 1
        if b <= a:
            return 0.0
        base = 0.5 * (y[a] + y[b])
        half = base + 0.5 * depth
        lo = hi = x[i]
        j = i
        while j > a and (y[j] - half) * depth > 0:
            j -= 1
        lo = x[j]
        j = i
        while j < b and (y[j] - half) * depth > 0:
            j += 1
        hi = x[j]
        return hi - lo

    def _force_near(self, x, times):
        for t in times:
            i = _bisect_x(x, t)
            for k in (i, i + 1):
                if 0 <= k < len(x):
                    self._forced.add(k)

    # ------------------------------------------------------------ 输出

    def forced(self):
        return sorted(self._forced)

    def metrics(self):
        out = []
        tr = self.trace
        for st in self.stats:
            s, c, u = st.sig, st.label, st.sig.unit
            g = "level"
            out.append(wave_emit.Metric(c, "min", st.vmin, u, st.min_at, g))
            out.append(wave_emit.Metric(c, "max", st.vmax, u, st.max_at, g))
            out.append(wave_emit.Metric(c, "pp", st.pp, u, None, g))
            out.append(wave_emit.Metric(c, "mean", st.mean, u, None, g))
            out.append(wave_emit.Metric(
                c, "rms_ac" if st.periodic else "rms_ripple", st.ripple, u,
                None, g))

            if st.periodic:
                g = "clock"
                out.append(wave_emit.Metric(c, "period", st.period, tr.xunit,
                                            None, g))
                out.append(wave_emit.Metric(c, "jitter_rms", st.jit_rms,
                                            tr.xunit, None, g))
                out.append(wave_emit.Metric(c, "jitter_pp", st.jit_pp, tr.xunit,
                                            None, g, note="N=%d cycles" % st.ncyc))
                g = "edge"
                if st.rise1090 is not None:
                    out.append(wave_emit.Metric(c, "rise(10-90)", st.rise1090,
                                                tr.xunit, None, g))
                if st.fall1090 is not None:
                    out.append(wave_emit.Metric(c, "fall(10-90)", st.fall1090,
                                                tr.xunit, None, g))
                if st.duty is not None:
                    out.append(wave_emit.Metric(c, "duty", 100.0 * st.duty, "%",
                                                None, g))
                out.append(wave_emit.Metric(c, "vlo", st.vlo, u, None, g))
                out.append(wave_emit.Metric(c, "vhi", st.vhi, u, None, g))
                continue

            if not hasattr(st, "final"):
                continue
            g = "step"
            out.append(wave_emit.Metric(c, "final", st.final, u, None, g))
            if st.over:
                out.append(wave_emit.Metric(
                    c, "overshoot", "%+.3f" % st.over[0], "%", st.over[1], g))
            if st.under:
                out.append(wave_emit.Metric(
                    c, "undershoot", "%+.3f" % -st.under[0], "%", st.under[1], g))
            for frac in (0.01, 0.001):
                if frac in st.settle:
                    t, band, why = st.settle[frac]
                    out.append(wave_emit.Metric(
                        c, "settle(+-%g%%)" % (frac * 100),
                        t if t is not None else "n/a",
                        tr.xunit if t is not None else "", None, g,
                        note=why or ("band %s" % eng_str(band, u, 3))))
            if st.slew and st.slew[0] != 0.0:
                v, at, h = st.slew
                sv, su = _rate(v, u, tr.xunit)
                out.append(wave_emit.Metric(
                    c, "slew_max", sv, su, at, g,
                    note="窗口 %s" % eng_str(h, tr.xunit, 2)))
            if abs(st.drift) > 3.0 * max(s.noise, 1e-300):
                out.append(wave_emit.Metric(c, "drift", st.drift, u, None, g))
            for k, gl in enumerate(st.glitches):
                out.append(wave_emit.Metric(
                    c, "glitch%d" % (k + 1), gl["depth"], u, gl["at"], "glitch",
                    note="width %s, %.1fx 噪声底, 窗口 %s"
                         % (_width_txt(gl, tr.xunit), gl["ratio"],
                            eng_str(gl["win"], tr.xunit, 2))))
        return out

    def events(self):
        out = []
        tr = self.trace
        for st in self.stats:
            c, u = st.label, st.sig.unit
            out.append(wave_emit.Event(st.min_at, c, "GLOB_MIN",
                                       eng_str(st.vmin, u, 6)))
            out.append(wave_emit.Event(st.max_at, c, "GLOB_MAX",
                                       eng_str(st.vmax, u, 6)))
            if st.periodic:
                # 列前两个沿定相位，再列**偏离均值最大的几个周期** ——
                # 按时间顺序列前 8 个沿对 debug 没用，抖动的离群点才有用。
                nr, nf = len(st.edges_r), len(st.edges_f)
                r = st.edges_r
                for t in r[:2]:
                    out.append(wave_emit.Event(t, c, "EDGE_RISE", "定相位"))
                per = [(abs(r[i + 1] - r[i] - st.period), i)
                       for i in range(len(r) - 1)]
                per.sort(reverse=True)
                for dv, i in per[:3]:
                    out.append(wave_emit.Event(
                        r[i + 1], c, "PERIOD_OUTLIER",
                        "T=%s (%s 偏离均值, 第 %d 个周期)"
                        % (eng_str(r[i + 1] - r[i], tr.xunit, 6),
                           eng_str(r[i + 1] - r[i] - st.period, tr.xunit, 3), i)))
                out.append(wave_emit.Event(
                    r[0], c, "EDGES_TOTAL",
                    "共 %d 上升 / %d 下降沿；全部沿的时刻不进 .wv（会撑爆预算），"
                    "周期/抖动/占空比见 [METRICS]" % (nr, nf)))
                continue
            if getattr(st, "over", None):
                out.append(wave_emit.Event(st.over[1], c, "OVERSHOOT",
                                           "%+.3f %%" % st.over[0]))
            if getattr(st, "under", None):
                out.append(wave_emit.Event(st.under[1], c, "UNDERSHOOT",
                                           "%+.3f %%" % -st.under[0]))
            for frac in (0.01, 0.001):
                ent = getattr(st, "settle", {}).get(frac)
                if ent and ent[0] is not None:     # 测不了的不进时间轴
                    out.append(wave_emit.Event(
                        ent[0], c, "SETTLED",
                        "+-%g%% band %s" % (frac * 100, eng_str(ent[1], u, 3))))
            if getattr(st, "slew", None) and st.slew[0] != 0.0:
                sv, su = _rate(st.slew[0], u, tr.xunit)
                out.append(wave_emit.Event(st.slew[1], c, "SLEW_MAX",
                                           eng_str(sv, su, 4)))
            for gl in st.glitches:
                out.append(wave_emit.Event(
                    gl["at"], c, "GLITCH",
                    "%s, %.1fx 噪声底, width %s"
                    % (eng_str(gl["depth"], u, 4), gl["ratio"],
                       _width_txt(gl, tr.xunit))))
        return out


def _width_txt(gl, xunit):
    """半高宽量不出来时报 `< 该处采样间隔`，**不报 `0 s`**。

    宽度为 0 的跳变不是物理量。读的人（尤其是模型）会把 `width 0 s` 当成
    「一个无限陡的真实跳变」，而它实际说的是「相邻采样点里没有一个落在半高以上，
    这个栅格解不出它的宽度」。后者是**该回去把数据重导一遍**的信号，
    前者会被当成电路特性写进结论。
    """
    if gl["width"] > 0:
        return eng_str(gl["width"], xunit, 3)
    if gl.get("dtl"):
        return "< %s（该处采样解不出宽度）" % eng_str(gl["dtl"], xunit, 3)
    return "该处采样解不出宽度"


def _rate(v, yu, xu):
    """斜率换成人看的单位。V/s 的数配 V/us 的标签是错的，值要一起换。"""
    if xu == "s" and yu in ("V", "A"):
        return v * 1e-6, yu + "/us"
    return v, "%s/%s" % (yu or "?", xu or "?")


def _bisect_x(x, t):
    lo, hi = 0, len(x) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if x[mid] < t:
            lo = mid + 1
        else:
            hi = mid
    return max(0, lo - 1)


wave_emit.register("tran", TranMetrics)
