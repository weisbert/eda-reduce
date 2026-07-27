#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wave_metrics_freq.py — 频域测量：spur / AC / PSS 谱。

**spur 保护是这个模块存在的首要理由**（wave-spec 第 4 节，规则 LOCKED）：

    频域数据在 RDP 之前，强制保留：所有比局部中位数底噪高 > X dB 的局部极大点，
    外加它们的 HWHM 包络（f0，f0 ± m*f0/(2Q)，m ∈ 0.05…4）。

出处是 `LDO_modeling/cadence/supply_noise/avdd_spectrum.py`，那边有
`test_spur_sampling_rule.py` 守着：**实测漏采会把 353 nV 的 spur 报成 6.5 nV，
低报 54 倍**。窄 spur 只有一两个 bin 宽，抽点会直接跨过去。

第二个理由是**线性谱必须先转 dB 再压**。载波 1 V、底噪 -120 dBc 的谱，
线性域里 tol×量程 = 5 mV，而 -52 dBc 的 spur 只有 2.5 mV ——
RDP 和量化会联手把它抹平。转成 dBc 之后 120 dB 的动态范围被均匀对待，
0.5% 的容差就是 0.6 dB，谁都活得下来。

脚本给频点、幅度、dBc、**以及 f/f0 比值**（都是确定性计算）。
「这是 DC-DC 二次谐波打进来的」是模型说的，不是这里。

明确不做：**不在脚本里对变步长 transient 做 FFT**（wave-spec 第 8 节）。
自适应步长要先插值到均匀栅格，混叠风险由插值引入且不可见。
要频谱就让 Cadence 出，这里只压缩结果。

依赖：纯标准库。
"""

import math

import wave_emit
from wave_core import eng_str

SPUR_DB = 15.0          # 高出局部底噪多少 dB 才算 spur
SPEC_DYN_DB = 40.0      # 峰值高出中位数这么多才可能是「谱」
SPEC_PEAK_FRAC = 0.02   # 且离峰值 20 dB 以内的 bin 不到这个比例
HWHM_M = (0.05, 0.1, 0.2, 0.4, 0.8, 1.2, 1.6, 2.4, 3.2, 4.0)
MAX_SPURS = 24
DB_FLOOR = -300.0


def _db20(v, ref):
    a = abs(v)
    if a <= 0.0 or ref <= 0.0:
        return DB_FLOOR
    return 20.0 * math.log10(a / ref)


def _median(v):
    s = sorted(v)
    return s[len(s) // 2] if s else 0.0


def _is_phase(sig):
    n = sig.name.lower()
    return sig.unit in ("deg", "rad") or "phase" in n or n.startswith("ph(")


def _is_db(sig):
    return "db" in (sig.unit or "").lower() or sig.name.lower().startswith("db")


class FreqMetrics(wave_emit.Metrics):
    kind = "freq"

    def __init__(self, trace):
        wave_emit.Metrics.__init__(self, trace)
        self._forced = set()
        self.spectra = []        # [(sig, info)]
        self.acs = []            # [(sig, phase_sig or None, info)]
        self._converted = {}

    # ------------------------------------------------------------ 分类

    def _classify(self, sig):
        """谱 还是 传递函数？靠数据判：谱是「一片底噪 + 几根针」。

        判据两条都要满足，两个数都写进 note —— 判错了读的人看得见依据。
        """
        if _is_phase(sig):
            return "phase"
        y = sig.y
        n = len(y)
        step = max(1, n // 20000)
        if _is_db(sig):
            samp = [y[i] for i in range(0, n, step)]
        else:
            mx = max(abs(v) for v in y) or 1.0
            samp = [_db20(y[i], mx) for i in range(0, n, step)]
        top = max(samp)
        med = _median(samp)
        dyn = top - med
        near = sum(1 for v in samp if v > top - 20.0) / float(len(samp))
        info = (dyn, near)
        if dyn > SPEC_DYN_DB and near < SPEC_PEAK_FRAC:
            return ("spectrum", info)
        return ("ac", info)

    # ------------------------------------------------------------ prepare

    def prepare(self):
        """线性谱 -> dB。**只在判定为谱、且 y 还是线性时**才换。

        y 已经是 dB20/dB10 的照搬不动（spec 第 6 节：头里带了就照搬，
        没带就标 unknown，不猜、不换算）。相位列永远不换。
        """
        tr = self.trace
        changed = False
        for sig in tr.signals:
            kind = self._classify(sig)
            if kind == "phase" or kind[0] != "spectrum":
                continue
            if _is_db(sig):
                tr.note("%s: 判为谱（峰高出中位 %.0f dB，近峰 bin 占 %.2f%%），"
                        "y 已经是 dB，照搬不换算"
                        % (sig.name, kind[1][0], 100 * kind[1][1]))
                continue
            y = sig.y
            ref = max(abs(v) for v in y)
            if ref <= 0:
                continue
            nclip = 0
            for i in range(len(y)):
                a = abs(y[i])
                if a <= 0.0:
                    y[i] = DB_FLOOR
                    nclip += 1
                else:
                    y[i] = 20.0 * math.log10(a / ref)
            self._converted[id(sig)] = ref
            sig.unit, sig.unit_src = "dBc", "declared"
            tr.note("%s: 判为谱（峰高出中位 %.0f dB，近峰 bin 占 %.2f%%）；"
                    "线性幅度已转 dBc，0 dBc = %s（原单位下的峰值）。"
                    "**不转的话 -52 dBc 的 spur 会被量化直接抹平**"
                    % (sig.name, kind[1][0], 100 * kind[1][1],
                       eng_str(ref, "", 8)))
            if nclip:
                tr.note("%s: %d 个 0 或负值无法取对数，钳到 %g dB"
                        % (sig.name, nclip, DB_FLOOR))
            changed = True
        return changed

    # ------------------------------------------------------------ analyze

    def analyze(self):
        tr = self.trace
        phases = [s for s in tr.signals if _is_phase(s)]
        for sig in tr.signals:
            if _is_phase(sig):
                continue
            kind = self._classify(sig)
            if kind[0] == "spectrum":
                self.spectra.append((sig, self._spectrum(sig)))
            else:
                self.acs.append((sig, self._pair_phase(sig, phases),
                                 self._ac(sig, kind[1])))

    def _pair_phase(self, sig, phases):
        """按名字配相位列：dB20(V(out)) <-> phase(V(out))。配不上就不配。"""
        arg = sig.name[sig.name.find("(") + 1:sig.name.rfind(")")] \
            if "(" in sig.name else sig.name
        for p in phases:
            if arg and arg in p.name:
                return p
        return phases[0] if len(phases) == 1 else None

    # ------------------------------------------------------------ 谱

    def _floor(self, y, n):
        """局部中位数底噪。分块取中位再插值 —— spur 只占块里一两个 bin，
        搬不动中位数，所以底噪估计不会被 spur 自己抬高。"""
        b = max(16, n // 64)
        cent, med = [], []
        for a in range(0, n, b):
            blk = y[a:min(n, a + b)]
            if not blk:
                continue
            cent.append(a + len(blk) // 2)
            med.append(_median(blk))
        out = [0.0] * n
        k = 0
        for i in range(n):
            while k + 2 < len(cent) and cent[k + 1] < i:
                k += 1
            if len(cent) == 1:
                out[i] = med[0]
                continue
            x0, x1 = cent[k], cent[min(k + 1, len(cent) - 1)]
            if x1 == x0:
                out[i] = med[k]
            else:
                w = (i - x0) / float(x1 - x0)
                out[i] = med[k] + w * (med[min(k + 1, len(med) - 1)] - med[k])
        return out

    def _spectrum(self, sig):
        tr = self.trace
        x, y = tr.x, sig.y
        n = len(y)
        floor = self._floor(y, n)
        # --- 载波 = 最高的那根
        ci = max(range(n), key=lambda i: y[i])
        cf, cv = _refine(x, y, ci)
        peaks = []
        for i in range(1, n - 1):
            if y[i] >= y[i - 1] and y[i] >= y[i + 1] and y[i] - floor[i] > SPUR_DB:
                peaks.append(i)
        # 相邻 bin 的同一根峰只留最高的一个
        merged = []
        for i in peaks:
            if merged and i - merged[-1][0] <= 2:
                if y[i] > y[merged[-1][0]]:
                    merged[-1] = (i, floor[i])
            else:
                merged.append((i, floor[i]))
        spurs = []
        for i, fl in merged:
            if i == ci:
                continue
            f, v = _refine(x, y, i)
            hw = self._hwhm(x, y, i)
            spurs.append({
                "i": i, "f": f, "db": v, "dbc": v - cv, "over": v - fl,
                "hwhm": hw, "q": (f / (2.0 * hw)) if hw > 0 else None,
                "ratio": (f / cf) if cf else None,
            })
            self._force_envelope(x, i, f, hw)
        spurs.sort(key=lambda s: -s["dbc"])
        info = {
            "ci": ci, "cf": cf, "cdb": cv, "floor": _median(floor),
            "spurs": spurs[:MAX_SPURS], "nspur": len(spurs),
            "ref": self._converted.get(id(sig)),
        }
        self._force_envelope(x, ci, cf, self._hwhm(x, y, ci))
        self._forced.add(ci)
        self._forced.add(0)
        self._forced.add(n - 1)
        return info

    def _hwhm(self, x, y, i):
        """半高半宽：从峰往两边找降 3 dB 的地方。y 已经是 dB，所以就是 -3。"""
        n = len(y)
        top = y[i]
        a = i
        while a > 0 and y[a] > top - 3.0:
            a -= 1
        b = i
        while b < n - 1 and y[b] > top - 3.0:
            b += 1
        w = 0.5 * (x[b] - x[a])
        return w if w > 0 else (x[min(i + 1, n - 1)] - x[max(i - 1, 0)]) * 0.5

    def _force_envelope(self, x, i, f0, hw):
        """LOCKED 规则：峰本身 + HWHM 包络上的采样点，全部钉死在 RDP 之前。

        f0 ± m*f0/(2Q)，而 f0/(2Q) 就是 HWHM，所以等价于沿着线型往外
        采到 4 个半宽。少了这一圈，抽点会把 spur 削成一根孤立毛刺，
        或者干脆跨过去 —— 实测低报 54 倍。
        """
        n = len(x)
        for k in (i - 1, i, i + 1):
            if 0 <= k < n:
                self._forced.add(k)
        if hw <= 0:
            return
        for m in HWHM_M:
            for f in (f0 - m * hw, f0 + m * hw):
                j = _nearest(x, f)
                if 0 <= j < n:
                    self._forced.add(j)

    # ------------------------------------------------------------ AC

    def _ac(self, sig, cls):
        tr = self.trace
        x, y = tr.x, sig.y
        n = len(y)
        db = _is_db(sig)
        yy = y if db else [_db20(v, 1.0) for v in y]
        pi = max(range(n), key=lambda i: yy[i])
        pf, pv = _refine(x, yy, pi)
        info = {"db": db, "dc": yy[0], "dc_f": x[0], "peak_f": pf, "peak_db": pv,
                "dyn": cls[0], "near": cls[1], "pi": pi}
        # -3 dB（相对峰值）两侧
        info["f3_hi"] = _cross_db(x, yy, pi, pv - 3.0, +1)
        info["f3_lo"] = _cross_db(x, yy, pi, pv - 3.0, -1)
        # 0 dB 穿越（从峰值往高频找第一次落到 0 dB）
        info["f0db"] = _cross_db(x, yy, pi, 0.0, +1) if pv > 0 else None
        self._forced.add(pi)
        self._forced.add(0)
        self._forced.add(n - 1)
        for f in (info["f3_hi"], info["f3_lo"], info["f0db"]):
            if f:
                self._forced.add(_nearest(x, f))
        return info

    # ------------------------------------------------------------ 输出

    def suggest_tol(self):
        """谱的默认容差按「底噪起伏多少 dB 才值得记」来定，不按 0.5% 量程。

        载波到底噪 120 dB，0.5% 就是 0.6 dB —— 那是在逐点编码 Rayleigh 起伏，
        对模型分析电路毫无用处（真正要的 spur 表在 METRICS 里，全精度）。
        放到 ~2 dB，形状还在，字节省一大截。AC 曲线是光滑的，不动它。
        """
        if not self.spectra:
            return None
        rng = max(s.rng for s, _ in self.spectra)
        return min(0.05, 2.0 / rng) if rng > 0 else None

    def forced(self):
        return sorted(i for i in self._forced if i is not None)

    def metrics(self):
        out = []
        tr = self.trace
        fu = tr.xunit
        for sig, inf in self.spectra:
            c, u = self.label(sig), sig.unit
            out.append(wave_emit.Metric(c, "carrier", inf["cf"], fu, None, "spec"))
            if inf["ref"] is not None:
                out.append(wave_emit.Metric(
                    c, "carrier_abs", inf["ref"], "", None, "spec",
                    note="原单位；0 dBc 就是它"))
            out.append(wave_emit.Metric(c, "noise_floor(median)",
                                        inf["floor"] - inf["cdb"], "dBc",
                                        None, "spec"))
            out.append(wave_emit.Metric(
                c, "spur_thr", SPUR_DB, "dB", None, "spec",
                note="判据=高出局部底噪；低于它的没报，共检出 %d 根" % inf["nspur"]))
            for k, s in enumerate(inf["spurs"]):
                note = "f/f0 %.6g" % s["ratio"] if s["ratio"] else ""
                if s["q"]:
                    note += ", HWHM %s, Q %.4g" % (eng_str(s["hwhm"], fu, 3), s["q"])
                out.append(wave_emit.Metric(
                    c, "spur%d" % (k + 1), "%+.3f" % s["dbc"], "dBc", s["f"],
                    "spur%d" % (k + 1), note=note))
        for sig, ph, inf in self.acs:
            c = self.label(sig)
            uu = "dB" if inf["db"] else "dB(rel 1)"
            out.append(wave_emit.Metric(c, "dc", inf["dc"], uu, inf["dc_f"], "ac"))
            out.append(wave_emit.Metric(c, "peak", inf["peak_db"], uu,
                                        inf["peak_f"], "ac"))
            if inf["f3_hi"]:
                out.append(wave_emit.Metric(c, "f_-3dB_hi", inf["f3_hi"], fu,
                                            None, "ac"))
            if inf["f3_lo"]:
                out.append(wave_emit.Metric(c, "f_-3dB_lo", inf["f3_lo"], fu,
                                            None, "ac"))
            if inf["f0db"]:
                out.append(wave_emit.Metric(c, "f_0dB", inf["f0db"], fu, None,
                                            "ac"))
            if ph is not None:
                pc, pu = self.label(ph), ph.unit or "deg"
                g = "phase"
                out.append(wave_emit.Metric(pc, "phase@dc", ph.y[0], pu,
                                            tr.x[0], g))
                out.append(wave_emit.Metric(
                    pc, "phase@peak", _at(tr.x, ph.y, inf["peak_f"]), pu,
                    inf["peak_f"], g))
                out.append(wave_emit.Metric(pc, "phase@end", ph.y[-1], pu,
                                            tr.x[-1], g))
                if inf["f0db"]:
                    out.append(wave_emit.Metric(
                        pc, "phase@0dB", _at(tr.x, ph.y, inf["f0db"]), pu,
                        inf["f0db"], g,
                        note="裸相位，不做 PM 换算 —— 这条曲线是不是环路增益，"
                             "脚本判断不了"))
        return out

    def events(self):
        out = []
        for sig, inf in self.spectra:
            c = self.label(sig)
            out.append(wave_emit.Event(inf["cf"], c, "CARRIER",
                                       "%.3f dB (0 dBc 参考)" % inf["cdb"]))
            for s in inf["spurs"]:
                out.append(wave_emit.Event(
                    s["f"], c, "SPUR",
                    "%+.3f dBc, 高出底噪 %.1f dB%s"
                    % (s["dbc"], s["over"],
                       (", f/f0 %.6g" % s["ratio"]) if s["ratio"] else "")))
        for sig, ph, inf in self.acs:
            c = self.label(sig)
            out.append(wave_emit.Event(inf["peak_f"], c, "PEAK",
                                       "%.4f dB" % inf["peak_db"]))
            if inf["f3_hi"]:
                out.append(wave_emit.Event(inf["f3_hi"], c, "F_3DB_HI", ""))
            if inf["f0db"]:
                out.append(wave_emit.Event(inf["f0db"], c, "F_0DB", ""))
        return out


# --------------------------------------------------------------- 小工具


def _refine(x, y, i):
    """抛物线顶点。谱线宽只有一两个 bin 时，不精修的频点误差就是半个 bin。"""
    n = len(y)
    if i <= 0 or i >= n - 1:
        return x[i], y[i]
    x0, x1, x2 = x[i - 1], x[i], x[i + 1]
    y0, y1, y2 = y[i - 1], y[i], y[i + 1]
    d0, d1, d2 = (x0 - x1) * (x0 - x2), (x1 - x0) * (x1 - x2), (x2 - x0) * (x2 - x1)
    if 0 in (d0, d1, d2):
        return x[i], y[i]
    a = y0 / d0 + y1 / d1 + y2 / d2
    b = -(y0 * (x1 + x2) / d0 + y1 * (x0 + x2) / d1 + y2 * (x0 + x1) / d2)
    if a == 0:
        return x[i], y[i]
    xv = -b / (2.0 * a)
    if not (x0 < xv < x2):
        return x[i], y[i]
    c = y1 - a * x1 * x1 - b * x1
    return xv, a * xv * xv + b * xv + c


def _nearest(x, f):
    lo, hi = 0, len(x) - 1
    while lo < hi:
        mid = (lo + hi) // 2
        if x[mid] < f:
            lo = mid + 1
        else:
            hi = mid
    if lo > 0 and abs(x[lo - 1] - f) < abs(x[lo] - f):
        return lo - 1
    return lo


def _cross_db(x, y, i0, level, step):
    """从 i0 往一个方向找第一次穿过 level，线性插值给频点。"""
    n = len(y)
    i = i0
    while 0 < i < n - 1:
        j = i + step
        if j < 0 or j >= n:
            return None
        if (y[i] - level) * (y[j] - level) <= 0 and y[j] != y[i]:
            w = (level - y[i]) / (y[j] - y[i])
            return x[i] + w * (x[j] - x[i])
        i = j
    return None


def _at(x, y, f):
    j = _nearest(x, f)
    if 0 <= j < len(y) - 1 and x[j + 1] != x[j]:
        w = (f - x[j]) / (x[j + 1] - x[j])
        return y[j] + w * (y[j + 1] - y[j])
    return y[min(j, len(y) - 1)]


wave_emit.register("freq", FreqMetrics)
