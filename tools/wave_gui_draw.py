# -*- coding: utf-8 -*-
"""画布那一层：分道、三层带、symlog 误差轴、真刻度、缩略条、游标。

从 `wave_gui.py` 拆出来的，因为它变成了整个窗口里最厚的一块，而且
**这一块的对错是可判定的** —— 一列里的极值有没有被画到、刻度是不是整数、
带子和折线对不对得齐，都能不开窗口直接测。

三条贯穿全文的判断：

1. **判据的方向要能主动看见。** 上一版画的是「红线跑没跑出灰带」，
   而保留点比像素列多时红带只会落在灰带**内侧** —— 丢幅度表现为
   「变窄」，图上永远不会「跑出去」。于是 49% 的误差配了一张
   「红在灰里」的图。改成画**被削掉的那块**（影），丢多少是主动出现的
   东西，不用人去比宽窄。
2. **假的归一化比不画更糟。** 每条信号各自拉满 0..1 之后，env_hi 和
   env_lo 会在图上交叉（物理上 env_hi >= env_lo 恒成立），六路电流
   峰值差近两倍的上下沿几乎重合。同单位的信号必须共用一根纵轴。
3. **饱和等于没有刻度。** 误差轴原来硬钳在 ±3，3 倍和 300 倍长得一模一样，
   而那正是「调 tol 还是换模式」的决定量 —— 用户从 49% 调到 10%，
   图上毫无变化。改 symlog。
"""

import bisect
import math

import wave_core as core

COLORS = ("#e01b24", "#1c71d8", "#2ec27e", "#e5a50a", "#9141ac", "#c64600")
GRID = "#dcdcdc"
FG = "#1a1a1a"
TOL_LINE = "#202020"      # 容差线**不能**用 COLORS[0]：单路是最常见的情形，
                          # 而那时刻度和数据同色，等于没有刻度
OK_BAND = "#eaf7ef"       # |e| <= 1
BAD_BAND = "#fdecec"      # |e| > 1
LANE_HEAD = "#f4f6f8"
GUTTER_L = 62            # 左边留给纵轴刻度
GUTTER_R = 96            # 右边留给游标读数
HEAD_H = 16              # 道头
EV_H = 18                # 事件轨


# --------------------------------------------------------------- 纯计算


def nice_ticks(lo, hi, want=5):
    """-> [刻度值]。步长取 1/2/5 × 10^k。

    **先定数值再算像素**。原来是先把绘图区四等分再反算那个位置的 x，
    于是刻度落在 717.6 ps、500.4 ns 这种数上 —— 一张图上没有一个整数，
    读数得靠估。
    """
    if not (hi > lo) or not all(map(math.isfinite, (lo, hi))):
        return []
    raw = (hi - lo) / float(max(1, want))
    e = math.floor(math.log10(raw)) if raw > 0 else 0
    base = 10.0 ** e
    for m in (1.0, 2.0, 5.0, 10.0):
        if raw <= m * base:
            step = m * base
            break
    else:                                       # pragma: no cover
        step = 10.0 * base
    out = []
    k = math.ceil(lo / step)
    while k * step <= hi + 1e-12 * abs(hi):
        out.append(k * step)
        k += 1
        if len(out) > 40:                       # pragma: no cover
            break
    return out


def symlog(e, knee=1.0):
    """|e| <= knee 线性，之外按 log10 压。-> 画在轴上的位置（同号）。

    误差轴要同时表达「刚好用满容差」（1 附近，要看得清）和「超了两个
    数量级」（100，不能跑出画外）。线性做不到，纯 log 在 0 附近炸。
    """
    a = abs(e)
    if a <= knee:
        return e
    v = knee + math.log10(a / knee)
    return v if e > 0 else -v


def symlog_ticks(peak, knee=1.0):
    """-> [(值, 标签)]，覆盖到 peak 为止。"""
    out = [(0.0, "0"), (knee, "+1"), (-knee, "-1")]
    d = 10.0
    while d <= max(peak, knee) * 1.05 and d <= 1e6:
        out.append((d, "%g×" % d))
        out.append((-d, "-%g×" % d))
        d *= 10.0
    return out


def lanes_of(blocks, mode="unit"):
    """分道。-> [(unit, [(block, si), ...]), ...]

    `mode="unit"`：按**单位**分，同单位的信号共用一根纵轴 —— 决策 2 的落点。
    跨块也一起分，所以解调出来的 env(A) 和 f_inst(Hz) 天然是两道、共用
    一条时间轴，而「起振包络往上跳的同一时刻频率跳没跳」正是判 squegging
    的看法。

    `mode="each"`：一条信号一道，各有各的纵轴。共轴的代价是小信号被大信号
    压平（demo_tran 里 V(vref) 只有 0.8 mV，和 V(ctrl) 的 1.2 V 同轴就是
    一条直线）。逃生舱是**分道**而不是「同一道里各自拉满」—— 后者会让
    env_hi 和 env_lo 在图上交叉，那是在生产假信息，比看不见更糟。
    """
    if mode == "each":
        out = []
        for b in blocks:
            if b.red is None:
                continue
            for si, s in enumerate(b.red.trace.signals):
                out.append((s.unit or "?", [(b, si)]))
        return out
    order, groups = [], {}
    for b in blocks:
        if b.red is None:
            continue
        for si, s in enumerate(b.red.trace.signals):
            u = s.unit or "?"
            if u not in groups:
                groups[u] = []
                order.append(u)
            groups[u].append((b, si))
    return [(u, groups[u]) for u in order]


def lane_span(items, i0i1, y_local):
    """一道里全部信号的公共 [lo, hi]。y_local 时只看视窗内的原始点。"""
    lo = hi = None
    for b, si in items:
        s = b.red.trace.signals[si]
        if y_local:
            i0, i1 = i0i1.get(id(b), (0, len(b.red.trace.x)))
            seg = s.y[i0:i1] or [s.vmin, s.vmax]
            a, z = min(seg), max(seg)
        else:
            a, z = s.vmin, s.vmax
        lo = a if lo is None else min(lo, a)
        hi = z if hi is None else max(hi, z)
    if lo is None:
        return 0.0, 1.0
    if hi <= lo:
        pad = abs(lo) * 0.05 or 1.0
        return lo - pad, hi + pad
    m = 0.06 * (hi - lo)
    return lo - m, hi + m


def text_cols(s):
    """字符串占几个等宽格。CJK 算两格。

    道头上那排图例是手工往右推的，按 `len()` 推的话中文注解一进来
    就推得不够，两条信号的图例叠在一起（实测 env_hi 和 env_lo 的
    注解糊成一片）。
    """
    n = 0
    for ch in s:
        o = ord(ch)
        # U+2000 往上一律按两格算。CJK 本来就是两格；箭头、破折号这些
        # 「宽度不确定」的字符在中文字体下通常也按两格画，而**宁可算宽也别算窄**
        # —— 算窄了图例会叠在一起，算宽了只是多留点空。
        n += 2 if (o >= 0x2000 or 0x1100 <= o <= 0x115F) else 1
    return n


def q_val(cs, v):
    """这个点在 .wv 里的实际值（量化之后）。"""
    return cs.from_out(float(cs.txt(v)))


def recon_at(kx, ky, xv):
    j = bisect.bisect_right(kx, xv) - 1
    if j < 0:
        return ky[0]
    if j >= len(kx) - 1:
        return ky[-1]
    x0, x1 = kx[j], kx[j + 1]
    if x1 == x0:
        return ky[j]
    return ky[j] + (xv - x0) / (x1 - x0) * (ky[j + 1] - ky[j])


def recon_series(tr, red, si):
    """-> (kx, ky)，ky 已量化。上下两格必须用同一条重建曲线。"""
    s = tr.signals[si]
    cs = red.specs[si]
    kx = [red.xspec.val(tr.x[i]) for i in red.kept]
    ky = [q_val(cs, s.y[i]) for i in red.kept]
    if tr.xscale == "log":
        kx = [math.log(v) if v > 0 else -745.0 for v in kx]
    return kx, ky


def recon_band(tr, red, si, i0, i1, ncol, x0, x1):
    """重建曲线按像素列压成 min/max。-> (lo[], hi[])

    列内极值 = min/max over {列左边界取值, 列右边界取值} ∪ {落在列内的保留点}。
    这个定义**没有空洞**：上一版只装保留点，保留点稀疏时一整段列全是 None，
    带子会断成虚线；而且缩放到「保留点比列少」时画法翻转，内容会横向跳一下。
    """
    lo = [None] * ncol
    hi = [None] * ncol
    if ncol <= 0 or i1 <= i0:
        return lo, hi
    kx, ky = recon_series(tr, red, si)
    logx = tr.xscale == "log"
    span = (x1 - x0) or 1.0

    def at(xv):
        u = math.log(xv) if (logx and xv > 0) else xv
        return recon_at(kx, ky, u)

    for c in range(ncol):
        a = x0 + span * c / float(max(1, ncol))
        z = x0 + span * (c + 1) / float(max(1, ncol))
        va, vz = at(a), at(z)
        lo[c], hi[c] = (va, vz) if va <= vz else (vz, va)
    for i in red.kept:                      # 落在列内的保留点也要算进去
        if not (i0 <= i < i1):
            continue
        c = int((tr.x[i] - x0) / span * ncol)
        if c < 0:
            c = 0
        elif c >= ncol:
            c = ncol - 1
        v = q_val(red.specs[si], tr.signals[si].y[i])
        if lo[c] is None or v < lo[c]:
            lo[c] = v
        if hi[c] is None or v > hi[c]:
            hi[c] = v
    return lo, hi


def clipped_runs(olo, ohi, rlo, rhi, eps):
    """被削掉的摆幅：连续成段地给出来。-> [(c0, c1, 'hi'|'lo')]

    只报 gap > eps 的段。按段画 polygon 而不是按列画线，
    是为了让 Canvas 图元数不随列数长。
    """
    out = []
    for key, get in (("hi", lambda c: (ohi[c], rhi[c])),
                     ("lo", lambda c: (rlo[c], olo[c]))):
        run = None
        for c in range(len(olo)):
            a, b = get(c)
            bad = (a is not None and b is not None and (a - b) > eps)
            if bad and run is None:
                run = c
            elif not bad and run is not None:
                if c - run >= 1:
                    out.append((run, c - 1, key))
                run = None
        if run is not None:
            out.append((run, len(olo) - 1, key))
    return out
