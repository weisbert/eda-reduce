#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wave_gui.py — Tkinter 预览：拖着滑块看丢了什么、看字节数。

**存在的理由**：在数据所在的隔离机器上就把参数调好，
避免「导出 → 传出 → 发现丢了特征 → 重来」的往返。一次往返的成本极高，
所以这个窗口要当场回答两个问题：**丢了什么** 和 **多少字节**。

三个窗格：

    上：原始 min/max 包络（灰带）+ reduce 后曲线（红）
        红线没跑出灰带 = 没丢东西
    中：误差曲线 = 重建值 - 原始值，横线标着容差        <- 关键窗格
        一眼看出哪一段被削平了，而不是猜
    下：METRICS 文本，全精度

误差按各信号**自己的 eps 归一化**，所以不管几路信号、什么单位，
容差线永远是 ±1 那两根 —— 这是唯一能把 V / A / dB 画进同一个窗格的画法。

性能架构（没有 numpy 也要流畅，这是硬要求）：

- 加载时跑一次 O(n) 预细化（后台线程 + 进度条），产出几千个候选点；
- 之后所有交互只在候选集上做 RDP，毫秒级；
- 灰带按画布**像素列**预分箱（~1200 段），1e7 点也是瞬间画完；
- Canvas 上线段总数恒定在 ~4000 以内。

**预览画布用 tk.Canvas 手画，不用 matplotlib** —— 理由是性能不是教条：
matplotlib 每次拖滑块要重画整个 figure，而这里要的是「拖动时字节数和误差实时变」。
Canvas 上就那几千条线段，改坐标即可。

依赖：标准库（tkinter）。
"""

import bisect
import contextlib
import os
import re
import sys
import threading
import time

try:
    import tkinter as tk
    from tkinter import ttk, filedialog
except ImportError:                                    # pragma: no cover
    tk = None

import wave_cli as cli
import wave_core as core
import wave_emit as emit
import wave_gui_draw as draw


_DERIVED = (
    ("env_hi(", "包络上沿：每个振荡周期的最大值"),
    ("env_lo(", "包络下沿：每个振荡周期的最小值"),
    ("f_inst(", "瞬时频率：每个周期量一次；0 = 这段没在振荡"),
)


def explain(name):
    """派生量的人话。-> 一句话，认不出给空串。

    勾一下「解调」，整份内容就从 `i /VCO_TOP/VDD` 换成 `env_hi(...)`、
    `env_lo(...)`、`f_inst(...)` —— 这些名字在 `.wv` 里是**契约**
    （模型靠它认这几列是什么），但界面上一个字都没解释过，
    于是用户看到的是三个凭空冒出来的缩写。名字不能改，那就注上。
    """
    for pre, txt in _DERIVED:
        if name.startswith(pre):
            return txt
    return ""


def _pale(hexcol, k=0.82):
    """往白里兑。原始包络用**每条信号自己的**淡色 —— 六路共用一个 #c8c8c8
    的话，六块灰糊成一个气球，谁都不知道哪块是谁的。"""
    r = int(hexcol[1:3], 16)
    g = int(hexcol[3:5], 16)
    b = int(hexcol[5:7], 16)
    f = lambda v: int(v + (255 - v) * k)        # noqa: E731
    return "#%02x%02x%02x" % (f(r), f(g), f(b))

BAND_COLS = 1200          # 灰带的像素列数上限
MAX_SEG = 4000            # Canvas 线段总数上限，多路信号时按它反推列数
ERR_PTS = 1500            # 误差曲线画多少点（拖动时用抽样，松手再算精确的）
PRECISE_MS = 250          # 松手多久之后算精确误差
DEFER_MS = 300            # 勾选框攒多久再算 —— 连点几个只算最后一次
SLOW_MS = 150             # 上一次算了这么久，就先把「重算中」画出来再动手
EV_LABELS = 14            # 视窗里事件超过这么多就只画竖线，不再逐个标名字
ZOOM_STEP = 0.8           # 滚一格视窗缩到 0.8 倍（反向就是 1/0.8）
MIN_VIEW_PTS = 4          # 视窗里至少还剩这么多原始点，再放大就没东西可看了
PAN_KEY_FRAC = 0.15       # 方向键一次平移视窗宽度的百分之几
Y_EPS_FLOOR = 2.0         # 纵轴半宽的下限 = 这么多倍 eps，见 _yscale
COLORS = ("#e01b24", "#1c71d8", "#2ec27e", "#e5a50a", "#9141ac", "#c64600")
BG = "#ffffff"
FG = "#1a1a1a"
GRID = "#dcdcdc"
BAND = "#c8c8c8"
GATE_BG = "#fdf3e3"       # 出口台：答案区
GATE_LINE = "#e8791a"
RAIL_BG = "#fff4e5"       # 内容轨：改这里 = 改要粘走的文本
RAIL_W = 258
VIEW_BG = "#eef0f2"       # 视图条：只改这张图
HINT_BG = "#fffbe6"       # 出路卡：下一步该点哪里
HINT_LINE = "#e5a50a"
VCOL = {"ok": "#1a7f37", "warn": "#9a6700", "bad": "#c01c28"}


# --------------------------------------------------------------- 纯计算部分
# 这一块刻意不碰 Tk，可以脱离窗口单独测。


def _col_index(name):
    """事件标签里的 `c3` -> 2。认不出给 0。

    原来是 `COLORS[hash(e.col) % 6]`。Python 3.3+ 的 str hash **每个进程
    随机**（PYTHONHASHSEED 不固定时），所以标着 `c1` 的竖线这次是蓝的、
    下次是橙的，跟 c1 自己的颜色也对不上。截图发给别人，两边看到的
    颜色不一样，而颜色是这张图上唯一的归属线索。
    """
    if not name:
        return 0
    m = re.match(r"^c(\d+)$", str(name).strip())
    return (int(m.group(1)) - 1) if m else 0


def q_val(cs, v):
    """这个保留点在 .wv 里的**实际**值 —— 也就是量化之后那个。

    形状格原来画的是原始 y，误差格算的是量化后的 y（error_band/error_curve
    里的 `cs.from_out(float(cs.txt(...)))`）。**两格在比不同的曲线**，
    而量化误差正是误差格在数的东西之一，于是上格系统性偏乐观：
    它画的是「如果不量化会长这样」，不是「.wv 里存的是这样」。
    """
    return cs.from_out(float(cs.txt(v)))


def bin_envelope(x, ys, i0, i1, ncol, x0=None, x1=None):
    """把 [i0,i1) 的原始点按列压成 min/max 包络。O(区间长度)。

    1e7 个点画不了，也没必要画：一列像素里那几千个点，眼睛能看见的只有
    上下沿。压成包络之后画的是「原始数据真实覆盖的区域」，
    红线跑出灰带就是丢了东西 —— 这个判据只有包络能给，抽样给不了。

    x0/x1 是**列的基准**，必须由调用方给成视窗的两端。原来是在这里拿
    `x[i0]..x[i1-1]` 自己算的，而画的时候列号是铺满整个绘图区（对应
    view[0]..view[1]）—— `fit_view` 特意往两边各多取一个原始点，
    于是分箱的坐标系比画图的坐标系宽出两个点。全长时看不出来，
    放大到几十个点时带子相对折线能偏出画布宽度的百分之十几。
    """
    n = i1 - i0
    if n <= 0:
        return []
    if x0 is None:
        x0 = x[i0]
    if x1 is None:
        x1 = x[i1 - 1]
    span = x1 - x0
    out = []
    for y in ys:
        lo = [None] * ncol
        hi = [None] * ncol
        for i in range(i0, i1):
            c = 0 if span <= 0 else int((x[i] - x0) / span * (ncol - 1))
            if c < 0:
                c = 0
            elif c >= ncol:
                c = ncol - 1
            v = y[i]
            if lo[c] is None:
                lo[c] = hi[c] = v
            elif v < lo[c]:
                lo[c] = v
            elif v > hi[c]:
                hi[c] = v
        out.append((lo, hi))
    return out


def recon_at(kx, ky, xv):
    """在重建折线上取值。kx 必须递增。"""
    j = bisect.bisect_right(kx, xv) - 1
    if j < 0:
        return ky[0]
    if j >= len(kx) - 1:
        return ky[-1]
    x0, x1 = kx[j], kx[j + 1]
    if x1 == x0:
        return ky[j]
    return ky[j] + (xv - x0) / (x1 - x0) * (ky[j + 1] - ky[j])


def error_curve(tr, red, si, i0, i1, npts):
    """误差曲线（抽样）。返回 [(x, err/eps)]，所以容差线恒为 ±1。"""
    s = tr.signals[si]
    cs = red.specs[si]
    logx = tr.xscale == "log"
    kx = [red.xspec.val(tr.x[i]) for i in red.kept]
    ky = [cs.from_out(float(cs.txt(s.y[i]))) for i in red.kept]
    if logx:
        kx = [core.math.log(v) if v > 0 else -745.0 for v in kx]
    eps = s.eps if 0 < s.eps < float("inf") else (s.rng or 1.0)
    step = max(1, (i1 - i0) // npts)
    out = []
    for i in range(i0, i1, step):
        xv = tr.x[i]
        u = core.math.log(xv) if (logx and xv > 0) else xv
        out.append((xv, (s.y[i] - recon_at(kx, ky, u)) / eps))
    return out


def _kept_band(x, y, vis, x0, x1, ncol, cs=None):
    """保留点按像素列压成 min/max 带。-> (lo[], hi[])，空列是 None。

    跟 `bin_envelope` 是同一件事，只是喂进来的不是全部原始点，而是
    reduce 之后**真正会写进 .wv 的那些点**。两条带子叠在一起看，
    红带比灰带窄多少，就是这次压缩丢了多少摆幅。

    给了 cs 就走量化值 —— 见 `q_val`。不给（比如画的不是保留点）保持原值。
    """
    lo = [None] * ncol
    hi = [None] * ncol
    span = (x1 - x0) or 1.0
    for i in vis:
        cix = int((x[i] - x0) / span * (ncol - 1))
        if cix < 0:
            cix = 0
        elif cix >= ncol:
            cix = ncol - 1
        v = q_val(cs, y[i]) if cs is not None else y[i]
        if lo[cix] is None:
            lo[cix] = hi[cix] = v
        elif v < lo[cix]:
            lo[cix] = v
        elif v > hi[cix]:
            hi[cix] = v
    return lo, hi


def error_band(tr, red, si, i0, i1, ncol, x0=None, x1=None):
    """误差按像素列压成 min/max 带。-> [(lo, hi) 或 None] * ncol，单位是 eps。

    原来是「隔 N 个点取一个再连折线」，两个毛病，在振荡波形上都是致命的：

    - **抽样会漏掉峰。** 误差在每个原始点上都在换号，隔 N 个取一个取到的是
      随机相位，最大误差经常一次都没被画到 —— 图上说「误差没超容差」，
      而 `recon:` 那行说超了，两个数对不上，人只能信文本、放弃这一格。
    - **画出来是一堵实心红墙。** 1500 个点铺在 1100 像素上，每列一根竖线，
      形状为零。

    分箱取 min/max 两个毛病一起治：画的是这一列里误差**真实覆盖的区间**，
    而且保证包含这一列的最大误差 —— 跟上面那格灰带是同一个道理。
    """
    s = tr.signals[si]
    cs = red.specs[si]
    logx = tr.xscale == "log"
    kx = [red.xspec.val(tr.x[i]) for i in red.kept]
    ky = [q_val(cs, s.y[i]) for i in red.kept]
    if logx:
        kx = [core.math.log(v) if v > 0 else -745.0 for v in kx]
    eps = s.eps if 0 < s.eps < float("inf") else (s.rng or 1.0)
    n = i1 - i0
    if n <= 0 or ncol <= 0:
        return []
    if x0 is None:
        x0 = tr.x[i0]
    if x1 is None:
        x1 = tr.x[i1 - 1]
    span = (x1 - x0) or 1.0
    out = [None] * ncol
    # 点数远多于列数时按固定步长跳着扫：每列仍能落到几十个点，
    # min/max 抓峰的能力保住了，而 1e7 点不至于扫到天亮。
    step = max(1, n // (ncol * 40))
    for i in range(i0, i1, step):
        xv = tr.x[i]
        cix = int((xv - x0) / span * (ncol - 1))
        if cix < 0:
            cix = 0
        elif cix >= ncol:
            cix = ncol - 1
        u = core.math.log(xv) if (logx and xv > 0) else xv
        e = (s.y[i] - recon_at(kx, ky, u)) / eps
        cur = out[cix]
        if cur is None:
            out[cix] = [e, e]
        elif e < cur[0]:
            cur[0] = e
        elif e > cur[1]:
            cur[1] = e
    return out


def fit_view(x, x0, x1):
    """视窗 -> 原始下标区间。缩放之后只对可见段重新分箱，所以深缩放是 O(可见点)。"""
    i0 = max(0, bisect.bisect_left(x, x0) - 1)
    i1 = min(len(x), bisect.bisect_right(x, x1) + 1)
    if i1 - i0 < 2:
        i0, i1 = 0, len(x)
    return i0, i1


def fitted_mp(b):
    """这块压完之后**实际落在**多少点。算不出来就返回 None。

    压预算的入口（`_fit` / `_autofit`）是把每块的 max_points 清成 None
    再交给 `fit_budget` 定的。清了之后必须**逐块**写回来：原来只写回当前
    聚焦的那块，其余块一直挂着 None，于是「切到另一块看一眼」就把 None
    塞进了绑在 Scale 上的 IntVar ——
        TclError: can't assign non-numeric value to scale variable
    多 trace + 超预算（布局 B、--demod、六路电流，全中）是常态不是边角。
    """
    if b.red is None or b.cand is None:
        return None
    return min(len(b.cand), len(b.red.kept))


class Xform(object):
    """世界坐标 <-> 画布像素。log 轴在 log 空间线性映射。"""

    def __init__(self, x0, x1, y0, y1, w, h, pad=(46, 8, 8, 20), logx=False):
        self.logx = logx
        if logx:
            x0 = core.math.log10(max(x0, 1e-300))
            x1 = core.math.log10(max(x1, 1e-300))
        self.x0, self.x1, self.y0, self.y1 = x0, x1, y0, y1
        self.l, self.t, self.r, self.b = pad
        self.w, self.h = w, h
        self.pw = max(1, w - self.l - self.r)
        self.ph = max(1, h - self.t - self.b)

    def sx(self, xv):
        if self.logx:
            xv = core.math.log10(max(xv, 1e-300))
        if self.x1 == self.x0:
            return self.l
        return self.l + (xv - self.x0) / (self.x1 - self.x0) * self.pw

    def sy(self, yv):
        if self.y1 == self.y0:
            return self.t + self.ph * 0.5
        return self.t + (self.y1 - yv) / (self.y1 - self.y0) * self.ph

    def wx(self, px):
        if self.x1 == self.x0:
            return self.x0
        v = self.x0 + (px - self.l) / float(self.pw) * (self.x1 - self.x0)
        return 10.0 ** v if self.logx else v


# --------------------------------------------------------------- GUI


class WaveGui(object):

    def __init__(self, root, path, args):
        self.root = root
        self.args = args
        self.path = path
        self.traces = []
        self.ti = 0
        # 整份货。`red` / `cand` / `metrics` 从今往后是**焦点块的**视图，
        # 不再是摊在 self 上、切一下块就被冲掉的状态。
        self.ship = None
        self.view = None
        self.band = None
        self.band_key = None
        self._precise_job = None
        self._sel = None
        self._pan = None
        # `tk.Scale.set()` / `BooleanVar.set()` 会触发控件自己的 command。
        # 程序性地改一个旋钮（自动压到预算、出路卡按钮、量程夹回）本来只是
        # 「把界面同步到已经算好的结果」，却会再触发一遍重算 —— 现在只是白算
        # 一次，链条一长就成环。所有程序性 set 包在 `with self._mute()` 里，
        # 所有 command 回调开头 `if self._muted: return`。
        self._muted = False
        # 纵轴默认跟视窗：放大就是为了看细节，按全局量程画的话横轴放大了、
        # 纵向还是一条压平的直线，等于没放大
        self.y_local = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="载入中…")
        self.verdict_v = tk.StringVar(value="载入中…")
        self.bytes_v = tk.StringVar(value="")
        self.over_v = tk.StringVar(value="")
        self.err_v = tk.StringVar(value="")
        self.selfcheck_v = tk.StringVar(value="")
        self.hint_v = tk.StringVar(value="")
        self.receipt_v = tk.StringVar(value="")
        self.actual_v = tk.StringVar(value="")
        self.mp_lab = tk.StringVar(value="形状点数（保底点另计）")
        self.demod_lab = tk.StringVar(value="解调：包络+频率（会改块数）")
        self.winlab_v = tk.StringVar(value="")
        self.fx_v = tk.StringVar(value="极值")
        self.fm_v = tk.StringVar(value="spur / 事件")
        self.maxcand_v = tk.StringVar(
            value="%d" % (getattr(args, "max_cand", None) or core.MAX_CAND))
        self._copied = None          # (字节数, 那一刻的参数指纹)
        self._autofit_pending = False
        # 后台压预算的那条线程和主线程改的是**同一批 Block**。两边同时算
        # 就是数据竞争：实测屏幕上 21332 字节、剪贴板里 20411 字节，
        # 因为一条线程正把 max_points 清成 None，另一条还按旧值在算。
        # 所以压预算期间主线程一律不算 —— 转圈已经说清楚在忙什么了。
        self._fitting = False
        self._cursor_x = None       # 游标：把「丢了什么」变成数字的那把尺
        self._lane_geom = []
        self.tol_v = tk.DoubleVar(value=(args.tol or core.DEFAULT_TOL) * 1000.0)
        self.mp_v = tk.IntVar(value=0)
        b0 = args.budget if args.budget else 0
        self.budget_v = tk.StringVar(value=("%g" % (b0 / 1024.0)) if b0 else "0")
        self.force_extrema = tk.BooleanVar(value=True)
        self.force_metrics = tk.BooleanVar(value=True)
        self.cols = []
        self.ti_v = tk.IntVar(value=0)
        self.raw = []
        self.colbtn = []
        # 这两个是**模式**，不是命令行开关：用户的入口是这个窗口，
        # 「用 --gui --demod 去开一个 GUI 模式」本身就是设计错了
        self.demod_v = tk.BooleanVar(value=bool(getattr(args, 'demod', False)))
        self.win_v = tk.BooleanVar(value=False)
        self.ev_v = tk.BooleanVar(value=True)      # 波形上标 EVENTS
        self.low_v = tk.StringVar(value="err")     # 下窗格画什么
        # 纵轴：同单位共轴（真的高低）/ 每路一道（小信号也看得见）。
        # **没有「同一道里各自拉满」这个选项** —— 那会让 env_hi 和 env_lo
        # 在图上交叉，是在生产假信息。
        self.lane_mode = tk.StringVar(value="unit")
        self.nrep_v = tk.IntVar(value=getattr(args, "demod_cycles", None) or 6)
        self.fspan_v = tk.IntVar(value=getattr(args, "demod_fspan", 0) or 0)
        self._build()
        # 没给文件也要能开窗。开窗和选文件是两件事，绑死了不合理 ——
        # 想先看看界面、想换一个文件重来，都不该被逼着先满足一个文件对话框。
        if path:
            self._load_async(path)
        else:
            self.status.set("还没打开文件 —— 点左下角「打开 CSV…」，"
                            "或者命令行给一个：wave my.csv --gui")

    # ------------------------------------------------------------ 焦点块的视图
    #
    # 这四个 property 是这一版最重要的一处改动。原来它们是普通属性，
    # 切块时 `_use_trace` 把它们整套冲掉重来 —— 于是「看一眼别的块」
    # 和「改要粘出去的东西」是同一个动作，回不去。现在状态归 Block，
    # 这里只是**望过去**。

    @property
    def blk(self):
        if not self.ship or not self.ship.blocks:
            return None
        return self.ship.blocks[min(self.ti, len(self.ship.blocks) - 1)]

    @property
    def red(self):
        b = self.blk
        return b.red if b else None

    @property
    def cand(self):
        b = self.blk
        return b.cand if b else None

    @property
    def metrics(self):
        b = self.blk
        return b.metrics if b else None

    # ------------------------------------------------------------ 出口台

    def _build_gate(self, r):
        """永远在最上面、**永远不被任何消息覆盖**的两行。

        这个窗口只回答一个问题：这份文本能不能粘出去。答案就该在
        第一眼的位置，而且是**两条独立判据** —— 装得下（字节）和
        够得准（误差）。原来只有字节一条，于是一份 49% 失真的 19.9 KB
        照样打勾；而复制成功那句提示会把整条状态栏盖掉，
        「粘贴前最后一眼」正好看不到判据。
        """
        g = self.gate = tk.Frame(r, bg=GATE_BG, highlightthickness=0)
        g.pack(fill="x", padx=0, pady=0)
        tk.Frame(g, bg=GATE_LINE, height=3).pack(fill="x")
        row1 = tk.Frame(g, bg=GATE_BG)
        row1.pack(fill="x", padx=10, pady=(5, 0))
        # 固定宽度是为了让右边那几个数不随判定文字长短左右跳；
        # 宽度按最长那句「⚠ 能粘，但看清楚」留，CJK 在 Tk 里按两格算不准，
        # 所以宁可宽一点也别切字 —— 判定被切成「能粘，但看清」是最糟的一种切法。
        self.lab_verdict = tk.Label(row1, textvariable=self.verdict_v,
                                    bg=GATE_BG, font=("Consolas", 12, "bold"),
                                    width=18, anchor="w")
        self.lab_verdict.pack(side="left")
        tk.Label(row1, text="│", bg=GATE_BG, fg="#c9b79a").pack(side="left")
        self.lab_bytes = tk.Label(row1, textvariable=self.bytes_v, bg=GATE_BG,
                                  fg=FG, font=("Consolas", 11), anchor="w")
        self.lab_bytes.pack(side="left", padx=(8, 2))
        # **预算必须能在这儿改。** 20 KB 是「聊天框这条通道」的宽度，
        # 换条通道就该跟着变。上一版搬控件时把这个输入框弄丢了，于是预算
        # 只剩「出路卡按钮改成推荐值」一条路 —— 改了还改不回来。
        e = tk.Entry(row1, textvariable=self.budget_v, width=5, bg="#fff",
                     fg=FG, font=("Consolas", 11), justify="right")
        e.pack(side="left")
        e.bind("<Return>", lambda _: self._on_budget())
        e.bind("<FocusOut>", lambda _: self._on_budget())
        tk.Label(row1, textvariable=self.over_v, bg=GATE_BG, fg=FG,
                 font=("Consolas", 11)).pack(side="left", padx=(2, 8))
        tk.Button(row1, text="自动压到预算", command=self._fit).pack(side="left")
        tk.Label(row1, text="│", bg=GATE_BG, fg="#c9b79a").pack(side="left",
                                                                padx=(8, 8))
        self.lab_err = tk.Label(row1, textvariable=self.err_v, bg=GATE_BG,
                                fg=FG, font=("Consolas", 11), anchor="w")
        self.lab_err.pack(side="left", fill="x", expand=True)
        self.pb = ttk.Progressbar(row1, mode="indeterminate", length=140)

        row2 = tk.Frame(g, bg=GATE_BG)
        row2.pack(fill="x", padx=10, pady=(2, 5))
        self.c_budget = tk.Canvas(row2, height=12, width=280, bg=GATE_BG,
                                  highlightthickness=0)
        self.c_budget.pack(side="left")
        # 「跳到最差点」放第二行：第一行那几个数是判据，塞不下再多东西了，
        # 而这个按钮的邻居本来就是自检行里那个 `@ 1.02 us`。
        self.btn_worst = tk.Button(row2, text="跳到最差点",
                                   command=self._goto_worst,
                                   font=("Consolas", 9))
        self.btn_worst.pack(side="left", padx=(8, 0))
        self.lab_check = tk.Label(row2, textvariable=self.selfcheck_v,
                                  bg=GATE_BG, fg="#6b5b45",
                                  font=("Consolas", 9), anchor="w",
                                  justify="left")
        self.lab_check.pack(side="left", padx=(10, 0), fill="x", expand=True)

    def _build_hint(self, r):
        """出路卡：**有事才出现**，没事把这 44 px 还给图。"""
        self.hint = tk.Frame(r, bg=HINT_BG)
        inner = tk.Frame(self.hint, bg=HINT_BG)
        inner.pack(fill="x", padx=(0, 8))
        tk.Frame(inner, bg=HINT_LINE, width=4).pack(side="left", fill="y")
        box = tk.Frame(inner, bg=HINT_BG)
        box.pack(side="left", fill="x", expand=True, padx=(6, 0), pady=3)
        self.lab_hint = tk.Label(box, textvariable=self.hint_v, bg=HINT_BG,
                                 fg="#7a5c00", font=("Consolas", 9),
                                 justify="left", anchor="w")
        self.lab_hint.pack(fill="x")
        self.hint_btns = tk.Frame(box, bg=HINT_BG)
        self.hint_btns.pack(anchor="w", pady=(2, 0))
        box.bind("<Configure>", lambda e: self.lab_hint.configure(
            wraplength=max(300, e.width - 10)))

    # ------------------------------------------------------------ 内容轨

    def _rail_group(self, parent, title, pin=False):
        """一组可折叠的轨内控件。-> 放东西的那个 body Frame。

        必须可折叠：轨里六组全展开约 620 px，而六路信号时光「搬哪几列」
        就是六行 —— 实测 880 高的窗口里 ④⑤⑥ 整个掉到屏幕外，
        和上一版「控件被右边切掉」是同一种病，只是换了个方向。
        规格里写的滚动 Frame 不做（内外两个 Configure 双向同步 + 滚轮
        按指针分派 + X11 上子控件未 map 时 bbox 返回 None，坑比收益多），
        改成折叠：装不下就从 ⑥ 往回收，而且**收了要看得见**。
        """
        holder = tk.Frame(parent, bg=RAIL_BG, highlightthickness=1,
                          highlightbackground="#e8d5b8")
        holder.pack(fill="x", padx=6, pady=(4, 0))
        hv = tk.StringVar(value="▾ " + title)
        # 标题要 wraplength：折起来之后它还得挂上这一组的状态
        # （「▸ ⑥ 保底点  极值 + spur/事件 7080 点」），不换行就把轨顶宽了
        head = tk.Label(holder, textvariable=hv, bg=RAIL_BG, fg="#7a4a00",
                        font=("Consolas", 9, "bold"), anchor="w",
                        cursor="hand2", wraplength=RAIL_W - 26,
                        justify="left")
        head.pack(fill="x", padx=4, pady=1)
        body = tk.Frame(holder, bg=RAIL_BG)
        body.pack(fill="x", padx=6, pady=(0, 3))
        grp = {"holder": holder, "body": body, "var": hv, "title": title,
               "open": True, "pin": pin, "state": ""}
        self._rail_groups.append(grp)
        head.bind("<Button-1>", lambda _e, g=grp: self._toggle_group(g))
        return body

    def _toggle_group(self, g, want=None):
        want = (not g["open"]) if want is None else want
        g["open"] = want
        # 折起来的组**必须把状态写在标题上**。否则折叠就是在藏信息：
        # 「⑥ 保底点」折了之后，spur/事件是开是关就没地方看了。
        g["var"].set(("▾ " if want else "▸ ") + g["title"]
                     + ("" if want else g["state"]))
        if want:
            g["body"].pack(fill="x", padx=6, pady=(0, 3))
        else:
            g["body"].pack_forget()

    def _set_group_state(self, i, txt):
        if i < len(self._rail_groups):
            g = self._rail_groups[i]
            g["state"] = txt
            if not g["open"]:
                g["var"].set("▸ " + g["title"] + txt)

    def _fit_rail(self):
        """装不下就折起来 —— 但**钉住的组一律不折**，而且折了要把状态写在标题上。

        折叠顺序是踩出来的：原来从最后一组往回折，而最后几组里正好有
        「解调」那个开关 —— 开了之后开关被折没了，回不去（用户报的
        「切到包络之后切不回原波形」就是这个）。现在开关钉在 ① 里，
        能折的只剩纯旋钮组。
        """
        if not self._rail_groups:
            return
        avail = self.rail.winfo_height()
        if avail < 50:                       # 还没布局好
            return
        for g in self._rail_groups:          # 先全展开再重新决定
            self._toggle_group(g, True)
        self.root.update_idletasks()
        for g in reversed(self._rail_groups):
            if g["pin"]:
                continue
            need = sum(x["holder"].winfo_reqheight() + 4
                       for x in self._rail_groups) + 30
            if need <= avail:
                break
            self._toggle_group(g, False)
            self.root.update_idletasks()

    def _build_rail(self, parent):
        """左边这一条**全是会改变要粘出去的文本的东西**。

        为什么竖排 + 固定宽度：横着排必然被窗口右边切掉，而且**看不出被切了**
        —— 上一版六路电流时「解调 / 只压视窗 / EVENTS」整列消失，人只会
        以为这个版本没有解调。上次的修法是把窗口调宽，没修住（信号一多、
        名字一长又会溢出）。竖排之后，控件的可见性和信号条数、名字长短
        彻底解耦，这才是结构性的解。
        """
        self._rail_groups = []
        rail = self.rail = tk.Frame(parent, bg=RAIL_BG, width=RAIL_W)
        rail.pack(side="left", fill="y")
        rail.pack_propagate(False)              # 宽度说了算，不让内容撑开
        rail.bind("<Configure>", lambda _e: self.root.after_idle(self._fit_rail))
        tk.Frame(rail, bg=GATE_LINE, width=3).place(x=0, y=0, relheight=1.0)
        tk.Label(rail, text="改这里 = 改变要粘走的文本", bg=RAIL_BG,
                 fg="#7a4a00", font=("Consolas", 9, "bold")).pack(
                     anchor="w", padx=10, pady=(6, 0))

        g1 = self._rail_group(rail, "① 搬哪几块", pin=True)
        self.tracebar = tk.Frame(g1, bg=RAIL_BG)
        self.tracebar.pack(fill="x")
        # **解调开关放在这儿，不放到下面的旋钮组里。** 它改的就是块数，
        # 属于「搬哪几块」；而且更实际的一条：轨装不下时下面的组会自动折起，
        # 折掉的那一下正好把这个开关藏了 —— 开了之后找不到地方关，
        # 用户报的就是这个（「切到包络之后切不回原波形」）。
        # 开关必须和它影响的东西在一起，而且必须钉住。
        tk.Checkbutton(g1, textvariable=self.demod_lab, variable=self.demod_v,
                       bg=RAIL_BG, fg=FG, selectcolor=RAIL_BG, anchor="w",
                       font=("Consolas", 9), wraplength=210, justify="left",
                       command=self._remode).pack(anchor="w", pady=(2, 0))

        g2 = self._rail_group(rail, "② 搬哪几列", pin=True)
        self.colbox = tk.Frame(g2, bg=RAIL_BG)
        self.colbox.pack(fill="x")

        g3 = self._rail_group(rail, "③ 搬哪一段 x")
        row = tk.Frame(g3, bg=RAIL_BG)
        row.pack(fill="x")
        tk.Radiobutton(row, text="全长", variable=self.win_v, value=False,
                       bg=RAIL_BG, fg=FG, selectcolor=RAIL_BG,
                       font=("Consolas", 9),
                       command=self._remode).pack(side="left")
        tk.Radiobutton(row, text="只搬一段", variable=self.win_v, value=True,
                       bg=RAIL_BG, fg=FG, selectcolor=RAIL_BG,
                       font=("Consolas", 9),
                       command=self._remode).pack(side="left")
        tk.Button(g3, text="取当前视窗", font=("Consolas", 9),
                  command=self._take_window).pack(anchor="w", pady=(2, 0))
        self.lab_win = tk.Label(g3, textvariable=self.winlab_v, bg=RAIL_BG,
                                fg="#7a5c00", font=("Consolas", 8),
                                justify="left", anchor="w", wraplength=230)
        self.lab_win.pack(fill="x")

        g4 = self._rail_group(rail, "④ 压多细")
        # 改名：它**从来就不是上限**。RDP 先把保底点整套放进 kept 再判预算
        # （wave_core 里 `kept = set(brk)` 那一段），所以滑块写 3803、
        # 实际写出 7082 是常态。叫「上限」是在骗人。
        tk.Label(g4, textvariable=self.mp_lab, bg=RAIL_BG, fg=FG,
                 font=("Consolas", 9), anchor="w").pack(fill="x")
        self.s_mp = tk.Scale(g4, from_=2, to=4000, orient="horizontal",
                             variable=self.mp_v, length=228, bg=RAIL_BG, fg=FG,
                             highlightthickness=0, showvalue=0, width=11,
                             command=self._on_slider)
        self.s_mp.pack(anchor="w")
        tk.Label(g4, textvariable=self.actual_v, bg=RAIL_BG, fg="#7a5c00",
                 font=("Consolas", 8), anchor="w",
                 justify="left", wraplength=230).pack(fill="x")
        self.tol_lab = tk.StringVar(value="存储精度 tol (‰)")
        tk.Label(g4, textvariable=self.tol_lab, bg=RAIL_BG, fg=FG,
                 font=("Consolas", 9), anchor="w").pack(fill="x", pady=(3, 0))
        self.s_tol = tk.Scale(g4, from_=0.1, to=100.0, resolution=0.1,
                              orient="horizontal", variable=self.tol_v,
                              length=228, bg=RAIL_BG, fg=FG, showvalue=0,
                              width=11, highlightthickness=0,
                              command=self._on_tol)
        self.s_tol.pack(anchor="w")
        cc = tk.Frame(g4, bg=RAIL_BG)
        cc.pack(fill="x", pady=(2, 0))
        tk.Label(cc, text="候选点上限", bg=RAIL_BG, fg=FG,
                 font=("Consolas", 9)).pack(side="left")
        e = tk.Entry(cc, textvariable=self.maxcand_v, width=7, bg="#fff", fg=FG)
        e.pack(side="left", padx=(4, 2))
        e.bind("<Return>", lambda _: self._on_maxcand())
        tk.Button(cc, text="重算", font=("Consolas", 8),
                  command=self._on_maxcand).pack(side="left")

        g5 = self._rail_group(rail, "⑤ 解调的两个旋钮")
        kb = tk.Frame(g5, bg=RAIL_BG)
        kb.pack(anchor="w")
        tk.Label(kb, text="代表周期存几个", bg=RAIL_BG, fg=FG,
                 font=("Consolas", 8)).pack(side="left")
        self.sp_rep = tk.Spinbox(kb, from_=0, to=12, width=3,
                                 textvariable=self.nrep_v, command=self._remode)
        self.sp_rep.pack(side="left", padx=(4, 0))
        kb2 = tk.Frame(g5, bg=RAIL_BG)
        kb2.pack(anchor="w")
        tk.Label(kb2, text="测频跨周期", bg=RAIL_BG, fg=FG,
                 font=("Consolas", 8)).pack(side="left")
        self.sp_fspan = tk.Spinbox(kb2, from_=0, to=64, width=3,
                                   textvariable=self.fspan_v,
                                   command=self._remode)
        self.sp_fspan.pack(side="left", padx=(4, 2))
        tk.Label(kb2, text="0=自动", bg=RAIL_BG, fg="#888",
                 font=("Consolas", 8)).pack(side="left")

        g6 = self._rail_group(rail, "⑥ 保底点（不为预算牺牲）")
        tk.Checkbutton(g6, textvariable=self.fx_v, variable=self.force_extrema,
                       bg=RAIL_BG, fg=FG, selectcolor=RAIL_BG, anchor="w",
                       font=("Consolas", 9), wraplength=210, justify="left",
                       command=self._defer).pack(anchor="w")
        tk.Checkbutton(g6, textvariable=self.fm_v, variable=self.force_metrics,
                       bg=RAIL_BG, fg=FG, selectcolor=RAIL_BG, anchor="w",
                       font=("Consolas", 9), wraplength=210, justify="left",
                       command=self._defer).pack(anchor="w")
        # 诚实说明：forced 是一组扁平的 x 下标，没有信号归属信息
        # （metrics 层的 `_forced` 就是个 set），所以取消某一列不会减少它们。
        tk.Label(g6, text="按整条记，取消某列不会少", bg=RAIL_BG, fg="#888",
                 font=("Consolas", 8), wraplength=225,
                 justify="left").pack(anchor="w")

    def _build_viewbar(self, parent):
        """这一条里**不许出现任何会改输出的东西，一个都没有**。

        原来「纵轴跟视窗」和「强制保留极值」并排、「波形上标出 EVENTS」
        和「解调」并排 —— 一个只改眼前这张图，一个改要粘出去的东西，
        排在同一个视觉分组里，用户没有任何线索区分。
        """
        vb = tk.Frame(parent, bg=VIEW_BG)
        vb.pack(fill="x", pady=(4, 0))
        tk.Frame(vb, bg="#9aa4ad", width=3).pack(side="left", fill="y")
        tk.Label(vb, text=" 只改这张图，不改要粘走的文本 ", bg=VIEW_BG,
                 fg="#4a5560", font=("Consolas", 9, "bold")).pack(side="left")
        tk.Label(vb, text="纵轴:", bg=VIEW_BG, fg="#4a5560",
                 font=("Consolas", 9)).pack(side="left", padx=(10, 0))
        for val, lab in (("unit", "同单位共轴"), ("each", "每路一道")):
            tk.Radiobutton(vb, text=lab, variable=self.lane_mode, value=val,
                           bg=VIEW_BG, fg=FG, selectcolor=VIEW_BG,
                           font=("Consolas", 9),
                           command=self._redraw).pack(side="left")
        tk.Checkbutton(vb, text="跟视窗", variable=self.y_local, bg=VIEW_BG,
                       fg=FG, selectcolor=VIEW_BG, font=("Consolas", 9),
                       command=self._redraw).pack(side="left", padx=(4, 0))
        tk.Checkbutton(vb, text="标出 EVENTS", variable=self.ev_v, bg=VIEW_BG,
                       fg=FG, selectcolor=VIEW_BG, font=("Consolas", 9),
                       command=self._redraw).pack(side="left", padx=(6, 0))
        tk.Label(vb, text="│ 下格:", bg=VIEW_BG, fg="#4a5560",
                 font=("Consolas", 9)).pack(side="left", padx=(8, 0))
        for val, lab in (("err", "误差"), ("cycles", "代表周期")):
            tk.Radiobutton(vb, text=lab, variable=self.low_v, value=val,
                           bg=VIEW_BG, fg=FG, selectcolor=VIEW_BG,
                           font=("Consolas", 9),
                           command=self._redraw).pack(side="left")


    def _text_page(self, nb, title):
        f = tk.Frame(nb, bg=BG)
        nb.add(f, text=title)
        sb = tk.Scrollbar(f)
        sb.pack(side="right", fill="y")
        t = tk.Text(f, height=6, bg="#fafafa", fg=FG, font=("Consolas", 9),
                    wrap="none", yscrollcommand=sb.set)
        t.pack(side="left", fill="both", expand=True)
        sb.config(command=t.yview)
        return t

    def _take_window(self):
        """把当前视窗**显式提交**成搬运范围。

        为什么不做成「持续跟随视窗」：每次缩放都会触发一次整条重建
        （切窗口 + analyze + 找周期，几十万点上一两秒），探索阶段没法用。
        显式提交 + 可反复点 + 不一致时给徽章，三样一起补住「快照语义
        没人猜得到」这个顾虑。
        """
        if not self.view:
            return
        self.win_v.set(True)
        self._remode()

    def _on_maxcand(self):
        try:
            n = int(float(self.maxcand_v.get()))
        except ValueError:
            self.maxcand_v.set("%d" % self._max_cand())
            return
        self.args.max_cand = max(100, n)
        if self.ship:
            for b in self.ship.blocks:
                b.cand = None
                b.touch()
        self._on_tol()

    # ------------------------------------------------------------ 界面

    def _build(self):
        r = self.root
        r.title("wave_reduce — 预览    [build %s]" % core.build_id())
        r.configure(bg=BG)
        # 1180 装不下那一排控件：解调/只压视窗/EVENTS 三个勾选框和两个旋钮
        # 全被右边切掉，而且**看不出被切了** —— 人只会以为这版没有解调。
        # 按屏幕夹一下，小屏上仍退回原来的尺寸。
        r.geometry("%dx%d" % (min(1360, r.winfo_screenwidth() - 40),
                              min(880, r.winfo_screenheight() - 80)))
        r.minsize(1000, 640)

        self._build_gate(r)
        self._build_hint(r)

        # 主体：左边一条固定宽的「内容轨」，右边视图条 + 两个画布。
        body = tk.Frame(r, bg=BG)
        body.pack(fill="both", expand=True, padx=0, pady=0)
        self._build_rail(body)
        right = tk.Frame(body, bg=BG)
        right.pack(side="left", fill="both", expand=True, padx=(6, 8))
        self._build_viewbar(right)

        self.c_wave = tk.Canvas(right, bg=BG, height=330, highlightthickness=1,
                                highlightbackground=GRID)
        self.c_wave.pack(fill="both", expand=True, pady=(2, 2))
        self.c_err = tk.Canvas(right, bg=BG, height=150, highlightthickness=1,
                               highlightbackground=GRID)
        self.c_err.pack(fill="x", pady=(0, 2))

        # 动作条：复制回执落在这里，**绝不碰出口台** —— 粘贴前最后一眼
        # 永远看得到判据。原来「已复制 N 字节…」会把整条状态栏盖掉。
        act = tk.Frame(r, bg=BG)
        act.pack(fill="x", padx=8, pady=(2, 0))
        tk.Button(act, text="打开 CSV…", command=self._open).pack(side="left")
        tk.Button(act, text="复制全文到剪贴板", command=self._copy).pack(
            side="left", padx=(8, 2))
        tk.Button(act, text="另存 .wv…", command=self._save).pack(side="left")
        tk.Label(act, textvariable=self.status, bg=BG, fg="#888",
                 font=("Consolas", 9), anchor="e").pack(side="right")
        self.lab_receipt = tk.Label(act, textvariable=self.receipt_v, bg=BG,
                                    fg="#666", font=("Consolas", 9),
                                    anchor="w", justify="left")
        self.lab_receipt.pack(side="left", fill="x", expand=True, padx=(12, 0))

        # 文本框**不再和画布抢 expand**。两个都 expand 的话，880 高的窗口里
        # 各分一半，而这个窗口的主诉是「图看不清」。
        #
        # 三个页签取代原来那对单选：「METRICS / 完整 .wv（可全选手动复制）」。
        # 那个标签在邀请一条路，而顶层的 Ctrl+C 绑定正好把它堵死（已在
        # Step 0 修掉）；而且**默认停在出口预览**——要粘走的东西该是
        # 打开就看得见的那一页，不是要先切一下的那一页。
        nb = ttk.Notebook(r, height=110)
        nb.pack(fill="x", padx=8, pady=(2, 8))
        self.txt = self._text_page(nb, "出口预览（要粘走的就是这个）")
        self.txt_metrics = self._text_page(nb, "METRICS")
        self.txt_warn = self._text_page(nb, "自检 & WARN")
        self.nb = nb
        # **不能绑 Ctrl+C。** Text 的 bindtags 是 (自己, 'Text', 顶层, 'all')，
        # 顶层排在 Text 类绑定**之后**，所以在文本框里选一段按 Ctrl+C，
        # 最后写进剪贴板的是这个回调放的整份 .wv，不是选区 ——
        # 而旁边那个单选按钮上就写着「可全选手动复制」，界面在邀请一条
        # 自己堵死的路。整份复制改挂 Ctrl+Shift+C。
        r.bind("<Control-C>", lambda _: self._copy())
        r.bind("<Control-o>", lambda _: self._open())

        self.c_wave.bind("<Configure>", lambda e: self._redraw())
        self.c_err.bind("<Configure>", lambda e: self._redraw())
        self.c_wave.bind("<ButtonPress-1>", self._sel_start)
        self.c_wave.bind("<B1-Motion>", self._sel_move)
        self.c_wave.bind("<ButtonRelease-1>", self._sel_end)
        self.c_wave.bind("<Double-Button-1>", lambda e: self._zoom_all())
        self.c_wave.bind("<Motion>", self._on_motion)
        # **松开滑块要把焦点还给画布。** 不还的话焦点一直留在 Scale 上，
        # 而 `_key` 见到 Scale 就不抢键 —— 于是「拖一次点数滑块之后，
        # 按 0 复位再也没反应」，而且没有任何迹象说明为什么。
        for s in (self.s_mp, self.s_tol):
            s.bind("<ButtonRelease-1>", lambda _e: self.c_wave.focus_set(),
                   add="+")
        self.c_wave.bind("<Button-1>", lambda _e: self.c_wave.focus_set(),
                         add="+")
        self.c_wave.configure(takefocus=1)
        self.c_wave.bind("<Leave>", lambda _e: (
            setattr(self, "_cursor_x", None), self.c_wave.delete("cursor")))

        # 滚轮绑在 root 上再按指针位置分派：Windows 的 <MouseWheel> 发给**有焦点的**
        # 控件而不是指针底下那个，绑在画布上会时灵时不灵。X11 没有 MouseWheel，
        # 滚轮是 Button-4/5 —— 隔离区是 Linux+X11，两套都得绑（本机只测得到前一套）。
        r.bind_all("<MouseWheel>", self._wheel)
        r.bind_all("<Button-4>", lambda e: self._wheel(e, 1))
        r.bind_all("<Button-5>", lambda e: self._wheel(e, -1))
        for c in (self.c_wave, self.c_err):
            for b in (2, 3):                       # 中键/右键拖 = 平移
                c.bind("<ButtonPress-%d>" % b, self._pan_start)
                c.bind("<B%d-Motion>" % b, self._pan_move)
                c.bind("<ButtonRelease-%d>" % b, self._pan_end)
        r.bind("<Key>", self._key)

    # ------------------------------------------------------------ 载入

    def _open(self):
        """随时换一个文件。开窗和选文件是两件事。"""
        p = filedialog.askopenfilename(
            title="选一个 ViVA 导出的 CSV",
            filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")])
        if p:
            self._load_async(p)

    def _load_async(self, path):
        """解析 + 预细化放后台线程。1e7 点解析要几十秒，不能冻界面。"""
        self.path = path
        # 换文件要把上一份状态清干净，否则列选框会越积越多、视窗还停在旧范围
        self.traces, self.ship = [], None
        self.view = self.band = self.band_key = None
        self.cols = []
        for w in self.colbox.winfo_children():
            w.destroy()
        self.txt.delete("1.0", "end")
        self.c_wave.delete("all")
        self.c_err.delete("all")
        self.status.set("载入中… " + os.path.basename(path))
        self._busy(True)
        q = []

        def work():
            try:
                trs = core.parse_csv(path, layout=self.args.layout,
                                     xcols=self.args.xcols)
                for tr in trs:
                    if getattr(self.args, "xrange", None):
                        core.slice_trace(tr, *self.args.xrange)
                    core.analyze(tr, kind=self.args.kind, xscale=self.args.xscale)
                # 到此为止：**解析和 analyze 是重活，放线程；模式切换不能放。**
                # `_build_traces` 要读 self.demod_v / self.win_v 这些 Tk 变量，
                # 跨线程碰 Tk 变量是未定义行为（实测：整个载入静默卡死，
                # app.red 永远是 None，也不报错）。模式的事回主线程做。
                m = None
                if not self.args.no_metrics:
                    m = emit.run_metrics(trs[0])
                tol = self.args.tol or (m.suggest_tol() if m else None) \
                    or core.DEFAULT_TOL
                q.append(("done", trs, tol))
            except Exception as exc:                    # noqa: BLE001
                q.append(("err", exc))

        threading.Thread(target=work, daemon=True).start()
        self._poll(q)

    def _busy(self, on):
        """载入转圈。只表示「后台线程在跑」这一件事。"""
        try:
            if on:
                self.pb.pack(side="right", padx=(8, 0))
                self.pb.start(12)
            else:
                self.pb.stop()
                self.pb.pack_forget()
        except tk.TclError:                             # pragma: no cover
            pass

    def _poll(self, q):
        while q:
            it = q.pop(0)
            if it[0] == "err":
                # 状态栏只有一行，解析失败的诊断是多行的 —— 全文进文本框，
                # 否则「切错列」这种一眼能定位的问题只剩一句 IndexError
                msg = "%s: %s" % (type(it[1]).__name__, it[1])
                self.status.set("载入失败: " + msg.splitlines()[0])
                self.txt.delete("1.0", "end")
                self.txt.insert("1.0", "载入失败\n\n" + msg)
                self._busy(False)
                return
            _, raw, tol = it
            self._busy(False)
            self.raw = raw
            with self._mute():
                self.tol_v.set(tol * 1000.0)
            self._rebuild_ship(raw)
            self.ti_v.set(0)
            self._autofit_pending = True   # 落地不该是失败态，见 _autofit
            self._use_trace(0)             # metrics / 候选集在这里面算
            return
        self.root.after(40, lambda: self._poll(q))

    # ------------------------------------------------------------ 计算

    def _push_ui_to_block(self):
        """把界面上那几个旋钮写回焦点块。块才是真相，控件只是它的显示。"""
        b = self.blk
        if b is None:
            return
        b.max_points = self.mp_v.get() or None
        b.cols = [v.get() for v in self.cols] if self.cols else None
        b.touch()

    def _recompute(self, precise=True):
        """重算焦点块，其余块用缓存 —— 合计字节因此还能实时。"""
        if not self.ship or self.blk is None or self._fitting:
            return
        # 上一次算了 150 ms 以上就先把「重算中」刷出去。同步的活没法打断，
        # 但**不能让窗口看起来死了** —— 六路 20 万点一次两秒多，
        # 没有这行的话按下去到出结果之间界面完全没反应。
        if getattr(self, "ms", 0.0) >= SLOW_MS:
            self.status.set("重算中…")
            self.root.update_idletasks()
        self._push_ui_to_block()
        self.ship.keep_extrema = self.force_extrema.get()
        for b in self.ship.blocks:
            b.use_forced = self.force_metrics.get()
        t0 = time.time()
        # fit=False：拖滑块时不许被预算顶回来，超预算是探索的一部分，
        # 由状态栏去说。「自动压到预算」按钮才走 fit=True。
        self.ship.compute(check=precise, only=self.blk, fit=False)
        self.ms = (time.time() - t0) * 1000.0
        self.nbytes = self.ship.total_bytes()
        if precise:
            self._fill_text()
        self._sync_eps_marks()
        self._status()
        self._redraw()

    def wv_text(self):
        """当前参数下的完整 .wv —— 就是要粘进聊天框的那份东西。

        **所有块都得在里面。** 原来只发当前这一条：布局 B（两个 time 列）
        和 `--demod`（包络块 + 频率块）都会产生多条 trace，粘出去的东西是残的，
        而且残得看不出来 —— 头部长得一模一样，只是少了一块。
        这条按钮是整个工具的出口，不能是残的。

        而且它现在**就是屏幕上那个字节数的来源**。原来非当前块是在这一刻
        拿当前块的参数临时压一遍出来的，屏幕读的是当前块、剪贴板里是全部块，
        两个数永远对不上。
        """
        if not self.ship:
            return ""
        # check=True：要粘出去的东西**必须**带 `# recon:` 那一行。
        # 复制是点一下的事，一次 O(n) 自检付得起。
        self.ship.compute(check=True, fit=False)       # 补齐还没算过的块
        return self.ship.text()

    def _use_trace(self, i):
        """把焦点挪到第 i 块。**只换看的，不动任何参数。**

        原来这里会把候选集、metrics、列选框、视窗全部推倒重来 ——
        于是「看一眼第二块」和「把第一块调过的东西丢掉」是同一个动作，
        而且回不去。现在每块的参数都在 Block 里存着，切回来原样还在。
        """
        if not self.ship or not self.ship.blocks:
            return
        self.ti = max(0, min(i, len(self.ship.blocks) - 1))
        b = self.blk
        b.ensure_cand(self.ship.tol_override)
        tr = b.trace
        for w in self.colbox.winfo_children():
            w.destroy()
        self.colbtn = []
        if b.cols is None:
            b.cols = [True] * len(tr.signals)
        self.cols = [tk.BooleanVar(value=on) for on in b.cols]
        for k, s in enumerate(tr.signals):
            row = tk.Frame(self.colbox, bg=RAIL_BG)
            row.pack(fill="x")
            cb = tk.Checkbutton(row, variable=self.cols[k], bg=RAIL_BG,
                                selectcolor=RAIL_BG, highlightthickness=0,
                                padx=0, command=self._defer)
            cb.pack(side="left")
            # 色块，不是彩色文字 —— 彩色文字会被当成图例（「这是给我看
            # 哪条线是哪个颜色的」），而它其实是**决定 .wv 里有没有这一列**。
            sw = tk.Canvas(row, width=9, height=9, bg=RAIL_BG,
                           highlightthickness=0)
            sw.create_rectangle(0, 0, 9, 9, fill=COLORS[k % 6], outline="")
            sw.pack(side="left", padx=(0, 4))
            lab = tk.Label(row, text="c%d %s" % (k + 1, s.name), bg=RAIL_BG,
                           fg=FG, font=("Consolas", 8), anchor="w",
                           justify="left", wraplength=195)
            lab.pack(side="left", fill="x", expand=True)
            self.colbtn.append(lab)
            why = explain(s.name)
            if why:                 # 派生量：名字是缩写，得注一句人话
                tk.Label(self.colbox, text="      " + why, bg=RAIL_BG,
                         fg="#8a7a63", font=("Consolas", 8), anchor="w",
                         justify="left", wraplength=225).pack(fill="x")
        # `b.cand` / `b.max_points` 在这里都可能还没定（块是懒算的，压预算
        # 又会把它们清掉）。**切一下块不许崩**，所以两个都取到具体的数再用。
        ncand = len(b.cand) if b.cand else 0
        self.s_mp.configure(to=max(10, ncand))
        mp = b.max_points if b.max_points is not None else fitted_mp(b)
        with self._mute():
            self.mp_v.set(mp if mp is not None else max(2, ncand // 4))
        self.band = self.band_key = None
        self._sync_trace_bar()
        self._zoom_all()
        self._recompute()
        if self._autofit_pending:
            self._autofit_pending = False
            self.root.after(60, self._autofit)

    def _autofit(self):
        """载入之后自动压一次到预算。

        不做这一步的话，每个人的落地画面必然是失败态：默认点数
        `len(cand)//4` 和预算**毫无关系**，实测起振电流一打开就是
        `94.7 KB ✗ / 49.53% / 一堵红墙`，而那跟数据好不好没关系，
        纯粹是个没人选过的初值。

        六路 20 万点的 fit 实测 30 s，所以放后台线程，界面照常能用；
        算完了再把结果搬回主线程 —— 跨线程碰 Tk 变量是未定义行为，
        这条上一轮已经踩过（整个载入静默卡死）。
        """
        if not self.ship or not self.budget():
            return
        v = self.ship.verdict()
        if v.bytes_ok:                       # 本来就装得下，别乱动人家的点数
            return
        self._fitting = True
        self._busy(True)
        self.status.set("压到预算中…（后台，界面照常能用）")
        ship, keep = self.ship, self.force_extrema.get()
        q = []

        def work():
            try:
                for b in ship.blocks:
                    b.max_points = None
                    b.touch()
                ship.compute(check=True, force=True, fit=True)
                q.append(("ok", None))
            except Exception as exc:                # noqa: BLE001
                q.append(("err", exc))

        threading.Thread(target=work, daemon=True).start()
        self._poll_autofit(q, keep)

    def _poll_autofit(self, q, keep):
        if not q:
            self.root.after(80, lambda: self._poll_autofit(q, keep))
            return
        kind, exc = q.pop(0)
        self._fitting = False
        self._busy(False)
        if kind == "err":
            self.status.set("自动压到预算失败：%s" % exc)
            return
        self._settle_fitted()                   # 每块都写回，不只当前这块
        cur = self.blk
        if cur is not None and cur.max_points is not None:
            with self._mute():
                self.mp_v.set(cur.max_points)
        self.nbytes = self.ship.total_bytes()
        self._fill_text()
        self._sync_eps_marks()
        self._status()
        self._redraw()

    def _sync_trace_bar(self):
        """轨①：每块两列 —— 「搬」是勾选框（改输出），「看」是单选（只改看）。

        分成两列是这一版最重要的一处语义拆分：原来切块这一个动作既换了
        显示、又把那块的参数冲掉，两件事绑死。现在「看」只挪焦点，
        「搬」才决定 .wv 里有没有这块。
        """
        for w in self.tracebar.winfo_children():
            w.destroy()
        if not self.ship:
            return
        blocks = self.ship.blocks
        hdr = tk.Frame(self.tracebar, bg=RAIL_BG)
        hdr.pack(fill="x")
        tk.Label(hdr, text="搬", bg=RAIL_BG, fg="#7a4a00",
                 font=("Consolas", 8), width=2).pack(side="left")
        tk.Label(hdr, text="看", bg=RAIL_BG, fg="#4a5560",
                 font=("Consolas", 8), width=2).pack(side="left")
        self.inc_v = []
        for i, b in enumerate(blocks):
            row = tk.Frame(self.tracebar, bg=RAIL_BG)
            row.pack(fill="x")
            var = tk.BooleanVar(value=b.included)
            self.inc_v.append(var)
            tk.Checkbutton(row, variable=var, bg=RAIL_BG, selectcolor=RAIL_BG,
                           highlightthickness=0, padx=0,
                           command=lambda k=i: self._toggle_block(k)
                           ).pack(side="left")
            tk.Radiobutton(row, variable=self.ti_v, value=i, bg=RAIL_BG,
                           selectcolor=RAIL_BG, highlightthickness=0, padx=0,
                           command=lambda: self._use_trace(self.ti_v.get())
                           ).pack(side="left")
            tk.Label(row, text="%d/%d %s" % (i + 1, len(blocks), b.label()),
                     bg=RAIL_BG, fg=FG, font=("Consolas", 8),
                     anchor="w").pack(side="left", fill="x", expand=True)
            tk.Label(row, textvariable=self._blk_size_v(i), bg=RAIL_BG,
                     fg="#7a5c00", font=("Consolas", 8)).pack(side="right")
            why = explain(b.label() + "(")
            if why:
                tk.Label(self.tracebar, text="      " + why, bg=RAIL_BG,
                         fg="#8a7a63", font=("Consolas", 8), anchor="w",
                         justify="left", wraplength=225).pack(fill="x")

    def _blk_size_v(self, i):
        if not hasattr(self, "_bsz"):
            self._bsz = {}
        if i not in self._bsz:
            self._bsz[i] = tk.StringVar(value="")
        return self._bsz[i]

    def _toggle_block(self, i):
        """勾掉「搬」= .wv 里真的没有这块。这是 A 类动作。"""
        if not self.ship:
            return
        self.ship.blocks[i].included = self.inc_v[i].get()
        for b in self.ship.blocks:        # 每块分到的预算变了
            b.touch()
        self._defer()

    def _sync_rail_readouts(self):
        """轨上那几个「实际是多少」的读数。"""
        if not self.ship:
            return
        for i, b in enumerate(self.ship.blocks):
            v = self._blk_size_v(i)
            v.set("%.1fKB" % (b.nbytes() / 1024.0) if b.text else "—")
        b = self.blk
        if b is not None and b.red is not None:
            n_forced = len(b.red.forced)
            n_kept = len(b.red.kept)
            over = n_kept > (b.max_points or 0)
            self.actual_v.set(
                "实际写出 %d 点（保底 %d）%s"
                % (n_kept, n_forced,
                   "\n!! 保底点已吃满，拖滑块无效" if over else ""))
            self.fx_v.set("极值")
            self.fm_v.set("spur / 事件  %d 点" % n_forced
                          if self.force_metrics.get() else "spur / 事件")
        # 搬运范围 vs 视窗：不一致时要说，否则「我看的这段」和「我搬的这段」
        # 会被当成同一件事
        tr = self.traces[self.ti] if self.traces else None
        if self.win_v.get() and tr is not None:
            self.winlab_v.set("已按视窗切：%s .. %s"
                              % (core.eng_str(tr.x[0], tr.xunit, 4),
                                 core.eng_str(tr.x[-1], tr.xunit, 4)))
        else:
            self.winlab_v.set("整条都搬")
        # 解调关着时那两个旋钮是**假的 A 类控件** —— 转了没反应
        on = self.demod_v.get()
        st = "normal" if on else "disabled"
        for w in (self.sp_rep, self.sp_fspan):
            try:
                w.configure(state=st)
            except tk.TclError:                     # pragma: no cover
                pass
        # 滑块的数值收进了标签（省一行高度），所以标签要自己报数
        self.mp_lab.set("形状点数 %d（保底另计）" % self.mp_v.get())
        self.tol_lab.set("存储精度 tol %.1f‰" % self.tol_v.get())
        n = len(self.ship.blocks)
        self.demod_lab.set("解调：包络+频率（%s）"
                           % ("现在 %d 块，取消回 1 块" % n if on else "会改块数"))
        # 折起来的组把状态写进标题，折叠因此不藏信息
        self._set_group_state(3, "  %d 点 / %.1f‰"
                              % (self.mp_v.get(), self.tol_v.get()))
        self._set_group_state(4, "  代表 %d / 跨 %d"
                              % (self.nrep_v.get(), self.fspan_v.get()))
        b0 = self.blk
        self._set_group_state(5, "  %s%s" % (
            "极值" if self.force_extrema.get() else "无极值",
            (" + spur/事件 %d 点" % len(b0.red.forced))
            if (self.force_metrics.get() and b0 and b0.red) else ""))
        self._set_group_state(2, "  %s" % ("只搬一段" if self.win_v.get()
                                           else "整条"))

    def _fill_text(self):
        for t in (self.txt, self.txt_metrics, self.txt_warn):
            t.delete("1.0", "end")
        if not self.red:
            return
        # 出口预览用**缓存**拼，不重算 —— `wv_text()` 会跑一遍 check=True，
        # 而这个函数每次精确重算之后都会被调一次
        self.txt.insert("1.0", self.ship.text() if self.ship else "")
        blk = emit.metrics_block(self.red, self.metrics)
        if not blk:
            blk = ["[METRICS] （这个 kind 没有注册 metrics 模块）"]
        self.txt_metrics.insert("1.0", "\n".join(blk))
        lines = []
        for b in self.ship.included():
            for ln in (b.text or "").splitlines():
                if ln.startswith("# recon:") or ln.startswith("# WARN:") \
                        or ln.startswith("# note:"):
                    lines.append(ln)
        self.txt_warn.insert("1.0", "\n".join(lines) or "（没有 WARN）")

    def _fingerprint(self):
        """当前这组参数的指纹。用来判断剪贴板里那份还算不算数。"""
        if not self.ship:
            return None
        return (self.ship.tol_override, self.ship.budget,
                self.ship.keep_extrema, self.demod_v.get(), self.win_v.get(),
                tuple((b.included, b.max_points, tuple(b.cols or ()),
                       b.use_forced) for b in self.ship.blocks))

    def _copy(self):
        """整份 .wv 直接进剪贴板。它的归宿就是聊天框，中间那三步是多余的。"""
        txt = self.wv_text()
        if not txt:
            self.receipt_v.set("还没有可复制的内容 —— 先打开一个 CSV")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(txt)
        self.root.update()               # 让 Tk 真正拿到剪贴板所有权
        n = emit.nbytes(txt)
        self._copied = (n, self._fingerprint())
        self._sync_receipt()

    def _sync_receipt(self):
        """复制回执 + **剪贴板还算不算数**。

        真实的坑：复制完再拖两下滑块，屏幕上的读数变了，剪贴板里还是旧的，
        而两者没有任何区别标记 —— 粘出去的是哪一份，只能靠记。
        """
        if not self._copied:
            self.receipt_v.set("")
            return
        n, fp = self._copied
        same = (fp == self._fingerprint())
        self.receipt_v.set(
            "已复制 %.1f KB 到剪贴板 · %s  │  X11 下先粘贴再关窗口，"
            "剪贴板归本进程持有"
            % (n / 1024.0,
               "与当前参数一致 ✓" if same else "**参数已改，剪贴板是旧的** ⚠"))
        self.lab_receipt.configure(fg="#666" if same else VCOL["warn"])

    def budget(self):
        """-> 字节数，0/空 = 不限。输入看不懂就退回启动时的值并说一声。"""
        s = (self.budget_v.get() or "").strip()
        if not s:
            return 0
        try:
            kb = float(s)
        except ValueError:
            self.budget_v.set("%g" % ((self.args.budget or 0) / 1024.0))
            return self.args.budget or 0
        return max(0, int(kb * 1024))

    def _on_budget(self):
        b = self.budget()
        self.args.budget = b or None
        if self.ship:
            self.ship.budget = b
        self._status()

    def _fit(self):
        """压到当前预算。

        这里原来有 GUI 自己写的一条二分 —— 和 `wave_cli.fit_budget` 里那条
        是两份实现，对同一份数据会落在不同的点数上（实测差过 55 字节）。
        「同一个预算 -> 同一个结果」这条不成立，GUI 里调好的参数拿到命令行
        就对不上。现在整条删掉，走 `Block.compute(fit=True)`，也就是
        命令行走的那一条。
        """
        if not self.ship or self.blk is None or self._fitting:
            return
        b = self.budget()
        if not b:
            self.status.set("预算设成了不限，没什么可压的")
            return
        self._push_ui_to_block()
        for blk in self.ship.blocks:
            blk.max_points = None               # 交给 fit_budget 去定
            blk.touch()
        self.status.set("压到预算中…")
        self.root.update_idletasks()
        self.ship.compute(check=True, force=True, fit=True)
        self.nbytes = self.ship.total_bytes()
        self._settle_fitted()                   # 每块都写回，不只当前这块
        cur = self.blk
        with self._mute():                      # 界面同步到算出来的结果
            if cur.max_points is not None:
                self.mp_v.set(cur.max_points)
        self._fill_text()
        self._sync_eps_marks()
        self._status()
        self._redraw()
        if self.nbytes > b:                     # 保底点撑住了，压不下去
            self.status.set(self.status.get() + "  ← 保底点撑住了下限")

    def _status(self):
        if not self.red:
            return
        # 判据一律按**整份货**报。原来这里报的是当前块：解调出两块时
        # 屏幕上写 9.1 KB / 20 KB ✓，而剪贴板里是 18.3 KB —— 两个数
        # 都对，只是回答的不是同一个问题，而用户没法知道该信哪个。
        ship = self.ship
        v = ship.verdict()
        self.verdict_v.set(v.label())
        self.lab_verdict.configure(fg=VCOL[v.level])

        nblk = "  %d 块" % v.nblocks if v.nblocks > 1 else ""
        self.bytes_v.set("总计 %.1f KB / 预算" % (v.nbytes / 1024.0))
        if v.budget:
            self.over_v.set("KB %s%s"
                            % (("超 %.1f×" % v.over()) if v.over()
                               else "装得下", nblk))
        else:
            self.over_v.set("KB（0=不限）%s" % nblk)
        self.lab_bytes.configure(fg=FG if v.bytes_ok else VCOL["bad"])

        # 三个刻度**同行并排**：×容差 / 绝对值 / % of range。
        # 状态栏原来只给 % of range，误差格只给 ±1（eps 归一），
        # 两个数第一次能互相印证 —— 而它们对不上正是信任崩掉的那一刻。
        w = v.worst
        if w:
            # 名字只在多列时才写 —— 单列时它是纯噪音，而这一行挤得下的
            # 只有三个刻度本身
            tail = ""
            if sum(len(b.trace.signals) for b in self.ship.included()) > 1:
                tail = "  [%s]" % w.sig.name
            self.err_v.set("最差误差 %.1f× 容差 ＝ %s ＝ 量程 %.1f%%%s"
                           % (v.peak_eps, core.eng_str(w.maxerr, w.sig.unit, 3),
                              w.pct, tail))
            self.lab_err.configure(fg=VCOL[v.err_ok])
        else:
            self.err_v.set("误差计算中…")
            self.lab_err.configure(fg=FG)
        self.btn_worst.configure(state="normal" if w else "disabled")

        self._draw_budget_bar(v)
        self._sync_hint()
        self._sync_receipt()
        self._sync_rail_readouts()
        # 自检行：`# recon:` 那一行的原文。粘贴前最后一眼要看到的就是它。
        self.selfcheck_v.set(self._selfcheck_line())
        self.status.set("%d 点  │  RDP %.0f ms  │  %s"
                        % (ship.n_kept(), self.ms,
                           os.path.basename(self.red.trace.source or "")))

    def _selfcheck_line(self):
        for b in self.ship.included():
            for ln in (b.text or "").splitlines():
                if ln.startswith("# recon:"):
                    return ln
        return ""

    def _draw_budget_bar(self, v):
        """预算尺：按块分段 + 均摊线 + 预算线。

        「哪一块吃掉了多少」在数字上是看不见的 —— 而按块均摊在解调上
        本来就不是个好分法（env 两列、f_inst 一列，固定开销差一截），
        顶穿的时候得一眼看得到是哪一块顶的。
        """
        c = self.c_budget
        c.delete("all")
        w = int(c.cget("width"))
        h = int(c.cget("height"))
        ref = float(v.budget or v.nbytes or 1)
        full = max(ref, float(v.nbytes or 1))
        c.create_rectangle(0, 0, w, h, fill="#efe4d2", outline="")
        x = 0.0
        for i, b in enumerate(self.ship.included()):
            seg = b.nbytes() / full * w
            c.create_rectangle(x, 0, x + seg, h,
                               fill=COLORS[i % 6], outline="", stipple="gray75")
            x += seg
        if v.budget:
            bx = v.budget / full * w
            c.create_line(bx, -1, bx, h + 1, fill=VCOL["bad"], width=2)
            n = len(self.ship.included())
            if n > 1:                       # 均摊线：每块分到多少
                for k in range(1, n):
                    px = bx * k / float(n)
                    c.create_line(px, 0, px, h, fill="#8a7a63", dash=(2, 2))

    def _sync_hint(self):
        """出路卡：有事才 pack，没事把那 44 px 还给图。"""
        for w in self.hint_btns.winfo_children():
            w.destroy()
        bl = self.ship.blockers() if self.ship else []
        if not bl:
            self.hint.pack_forget()
            return
        b = bl[0]
        txt = b.text
        if len(txt) > 200:
            txt = txt[:199] + "…"
        self.hint_v.set(txt)
        for code, label in b.actions:
            tk.Button(self.hint_btns, text=label, font=("Consolas", 9),
                      command=lambda c=code: self._do_action(c)).pack(
                          side="left", padx=(0, 6))
        n = len(self.ship.warns())
        if n:
            tk.Button(self.hint_btns, text="全部 WARN (%d) ▾" % n,
                      font=("Consolas", 9),
                      command=self._show_warns).pack(side="left", padx=(12, 0))
        self.hint.pack(fill="x", after=self.gate)

    def _show_warns(self):
        self._fill_text()
        self.txt_warn.insert("1.0", "".join(
            "!! %s\n\n" % w for w in self.ship.warns()))
        self.nb.select(2)

    def _do_action(self, code):
        """出路按钮**真去执行**，不是打印一行命令行给人抄。

        原来三条出路写在一句 WARN 里（而且被状态栏截断了），用户看见
        「用 --xrange」得自己关窗口、回命令行、重开。工具知道该做什么，
        就该自己做。
        """
        if code == "demod":
            self.demod_v.set(True)
            self._remode()
        elif code == "window":
            self.win_v.set(True)
            self._remode()
        elif code == "budget":
            need = self._need_kb()
            if need:
                with self._mute():
                    self.budget_v.set("%.0f" % need)
                self._on_budget()
                self._recompute(True)
        elif code == "fit":
            self._fit()
        elif code == "drop_forced":
            self.force_metrics.set(False)
            self._defer()
        elif code in ("tol_up", "tol_down"):
            f = 2.0 if code == "tol_up" else 0.5
            self.tol_v.set(max(0.1, min(100.0, self.tol_v.get() * f)))
            self._on_tol()
        elif code == "max_cand":
            self.args.max_cand = self._max_cand() * 2
            for b in self.ship.blocks:
                b.cand = None
                b.touch()
            self._on_tol()
        elif code == "more_points":
            b = self.blk
            if b and b.cand:
                self.mp_v.set(min(len(b.cand), max(4, self.mp_v.get() * 2)))
                self._live()

    def _need_kb(self):
        for b in self.ship.included():
            if b.red is None:
                continue
            ex = emit.carrier_exits(b.red)
            if ex:
                return ex["need_kb"]
        return self.nbytes * 1.2 / 1024.0

    def _goto_worst(self):
        """把视窗挪到全局最差点。

        顶上的判据是**整条**的（`recon` 全点自检），而误差格画的只有
        **视窗内**的 —— 用户放大到一段压得好的地方，误差格全绿而出口台
        仍写着 ✗ 24.3×，他没有任何办法找到那个 24.3× 在哪，
        因为唯一的定位信息 `@ 1.02 us` 是一行文字，而导航全是相对操作。
        """
        v = self.ship.verdict() if self.ship else None
        if not v or not v.worst or v.worst.at is None:
            return
        at = v.worst.at
        tr = self.traces[self.ti]
        span = (self.view[1] - self.view[0]) if self.view else (tr.x[-1] - tr.x[0])
        i0, i1 = fit_view(tr.x, at - span / 2.0, at + span / 2.0)
        if i1 - i0 < 20:                       # 缩得太狠就给 200 个原始点的宽度
            k = bisect.bisect_left(tr.x, at)
            lo = max(0, k - 100)
            hi = min(len(tr.x) - 1, k + 100)
            span = tr.x[hi] - tr.x[lo]
        self._set_view(at - span / 2.0, at + span / 2.0)

    def _max_cand(self):
        return getattr(self.args, "max_cand", None) or core.MAX_CAND

    def _build_traces(self, raw):
        """原始 trace -> 当前模式下要展示/输出的 trace 列表。

        顺序跟命令行**必须**一致：先切窗口再 analyze 再解调。
        切完窗口要重新 analyze —— 极值、噪声底、周期数都得是窗口内的，
        拿整条的极值去定窗口内的容差会差出量级。
        """
        win = self.view if (self.win_v.get() and self.view) else None
        out = []
        for tr in raw:
            t = tr
            if win:
                t = tr.clone()
                try:
                    core.slice_trace(t, win[0], win[1])
                except ValueError:
                    t = tr                     # 窗口里没点：当没切
                core.analyze(t, kind=self.args.kind, xscale=self.args.xscale)
            out.extend(self._demod(t) if self.demod_v.get() else [t])
        return out

    def _rebuild_ship(self, raw):
        """按当前模式重建整份货。模式一动块数就变，所以整份重来。"""
        self.traces = self._build_traces(raw)
        self.ship = cli.Shipment([cli.Block(t, self.args) for t in self.traces],
                                 self.args, self.force_extrema.get())
        # 命令行的 --tol 在 args 里；GUI 拖过滑块之后由 _on_tol 覆盖。
        # 没拖过就保持 None，让各块用自己的建议值 —— 和命令行一模一样。
        for b in self.ship.blocks:
            b.use_forced = self.force_metrics.get()
        self._settle_defaults()
        return self.traces

    def _settle_fitted(self):
        """压完预算之后，把每块**算出来的点数**写回该块。

        `fit_budget` 只认 max_points=None（「你来定」），所以压之前每块都被
        清成 None。不逐块写回，那些没被聚焦过的块就一直是 None ——
        参见 `fitted_mp` 的注释。
        """
        if not self.ship:
            return
        for b in self.ship.blocks:
            n = fitted_mp(b)
            if n is not None:
                b.max_points = n

    def _settle_defaults(self):
        """把每块的默认参数**一次性定下来**，别等到第一次聚焦才定。

        否则「切过去看一眼」这个动作会顺手把那块的 max_points 从 None
        变成一个具体值 —— 看一眼改变了要粘出去的东西，正是这次要消灭的事。
        """
        for b in self.ship.blocks:
            if b.cols is None:
                b.cols = [True] * len(b.trace.signals)
            if b.max_points is None:
                # 只备候选集，不做 RDP —— 这里要的只是「这块有多少候选点」，
                # 拿它定滑块量程和默认点数。真正的压交给随后的 _recompute，
                # 免得同一块在载入时被压两遍。
                cand = b.ensure_cand(self.ship.tol_override)
                b.max_points = min(len(cand), max(50, len(cand) // 4))
                b.touch()

    def _remode(self):
        """解调 / 只压当前视窗 —— 这两个开关一动就整条重来。

        重来一次是 O(n)（切窗口 + analyze + 找周期），几十万点上一两秒。
        点一下等一下可以接受；**不能接受的是让人为了换个模式回命令行重开窗口**。
        """
        if not self.raw:
            return
        self.status.set("重算中…（解调 %s，视窗 %s）"
                        % ("开" if self.demod_v.get() else "关",
                           "只压当前" if self.win_v.get() else "全长"))
        self.root.update_idletasks()
        self._rebuild_ship(self.raw)
        self.ti_v.set(0)
        self._use_trace(0)

    def _demod(self, tr):
        """`--demod` 在 GUI 里也要生效，而且走**命令行同一条路**。

        上一轮 `--xrange` / `--max-cand` 是分别接进两边的，结果 `--demod` 只接了
        命令行，GUI 静默忽略——用户拿 `--gui --demod` 开窗，看到的还是原始波形，
        而且没有任何提示。同一个功能有两个入口就迟早分叉，所以现在共用
        `wave_demod.apply()`。
        """
        # 要不要解调由**调用方**（窗口里那个勾）决定，不再看 args.demod ——
        # 留着那个早退会让命令行开关把界面开关按死，勾了也没反应
        try:
            import wave_demod
        except ImportError:
            tr.note("--demod 需要 tools/wave_demod.py，这份部署里没有")
            return [tr]
        tol = self.args.tol or core.DEFAULT_TOL
        return wave_demod.apply(tr, tol, self.args.budget,
                                max(0, self.nrep_v.get()),
                                getattr(self.args, "demod_min", None) or 20,
                                self.args.kind, self.args.xscale,
                                max(0, self.fspan_v.get()))

    def _sync_eps_marks(self):
        """在每个列选框上标出**这一列的 eps 是谁定的**。

        真实困惑：「包络和解调的精度跟原始波形是解耦的吗」。答案是
        `eps = max(tol×自己的量程, 3×自己的噪声底)` —— 一个滑块，每列各算各的，
        而且**经常有列被噪声底钉死，拖 tol 对它毫无作用**（实测频率块在
        0.5‰~50‰ 全程纹丝不动）。不标出来的话，人会以为自己在调所有曲线。
        """
        if not self.traces:
            return
        tr = self.traces[self.ti]
        # 名字**永远叫「存储精度」**。原来解调时它会改叫「tol → 包络」，
        # 而 tol 从头到尾只管一件事：算出来的曲线存得准不准。
        # 「包络提得准不准」是另一层（轨⑤那两个旋钮），改名会让人以为
        # 拖这个滑块能改提取精度。
        self.tol_lab.set("存储精度 tol (‰)")
        tol = self.tol_v.get() / 1000.0
        for k, s in enumerate(tr.signals):
            if k >= len(self.colbtn):
                break
            pinned = core.NOISE_K * s.noise > tol * (s.rng or 1.0)
            self.colbtn[k].configure(
                text="c%d %s%s" % (k + 1, s.name, "\n·噪声底钉住" if pinned else ""))

    # ------------------------------------------------------------ 交互

    def _defer(self, _=None):  # noqa: D401
        """勾选框：攒一下再算，连点几个只算最后那一次。

        一次精确 recompute 在 6 路 20 万点上要两秒多，而且是**同步**跑在 Tk
        回调里 —— 连勾四列就是十秒白屏，中间三次的结果一眼都没人看见。
        滑块那条早就 debounce 了（`_live`），勾选框当时直接接的 `_recompute`，
        是漏的一条。

        跟 `_live` 不一样的是：这里**不先跑一次粗的**。粗的那次省的只是
        `recon_error`（六路上 0.4 s / 2.2 s），剩下的 RDP 一分不少，
        先粗后精等于把两秒的活干两遍。
        """
        if self._muted or self._fitting or not self.traces:
            return
        on = sum(1 for v in self.cols if v.get())
        self.status.set("重算中…（%d/%d 列）" % (on, len(self.cols)))
        if self._precise_job:
            self.root.after_cancel(self._precise_job)
        self._precise_job = self.root.after(
            DEFER_MS, lambda: (setattr(self, "_precise_job", None),
                               self._recompute(True)))

    @contextlib.contextmanager
    def _mute(self):
        """这一段里的 set() 只同步界面，不许回头触发重算。"""
        old = self._muted
        self._muted = True
        try:
            yield
        finally:
            self._muted = old

    def _on_slider(self, _=None):
        if self._muted or self._fitting:
            return
        self._live()

    def _on_tol(self, _=None):
        if self._muted or self._fitting or not self.ship:
            return
        # tol 是**整份货**的判据，不是某一块的 —— 所以写进 Shipment，
        # 全部块的候选集一起失效。原来只重算了当前块，别的块还留着旧 tol，
        # 于是复制出去的两块用的是两个容差。
        self.ship.set_tol(self.tol_v.get() / 1000.0)
        b = self.blk
        if b is not None:
            b.ensure_cand(self.ship.tol_override)
            # 候选集变了，点数滑块的上限就得跟着变。原来只在 `_use_trace`
            # 里配一次：调大 tol -> 候选点变少 -> 滑块量程还停在老的大值上，
            # 拖到后半程完全没反应；调小 tol -> 候选点变多 -> 拖到头也够不着。
            self._sync_mp_range()
        self._live()

    def _sync_mp_range(self):
        """把点数滑块的量程对齐到当前候选集。上限的 10 是兜底值。"""
        n = len(self.cand) if self.cand else 0
        self.s_mp.configure(to=max(10, n))
        if n and self.mp_v.get() > n:
            with self._mute():
                self.mp_v.set(n)
            b = self.blk
            if b is not None:
                b.max_points = n

    def _live(self):
        """拖动时只算抽样误差（O(n) 的精确自检 145k 点要 0.5 s，会卡手）；
        松手 250 ms 后再算精确的那一次。"""
        if not self.traces:
            return
        self._recompute(precise=False)
        if self._precise_job:
            self.root.after_cancel(self._precise_job)
        self._precise_job = self.root.after(PRECISE_MS,
                                            lambda: self._recompute(True))

    def _zoom_all(self):
        if not self.traces:
            return
        tr = self.traces[self.ti]
        self.view = (tr.x[0], tr.x[-1])
        self.band = None
        self._redraw()

    def _set_view(self, x0, x1):
        """夹进整条曲线，并挡住「放大到没有原始点」。-> 有没有真的动。

        放大的下限用**还剩几个原始点**，不用一个绝对时间宽度：ps 级的 tran 和
        MHz 级的 ac 上没有同一个合理的绝对值，点数有。
        """
        if not self.traces or not self.view or x1 <= x0:
            return False
        tr = self.traces[self.ti]
        lo, hi = tr.x[0], tr.x[-1]
        # 先定跨度再定位置。原来是「哪头出界就把另一头推回去」，
        # 撞边时算的是 (lo+d) - ((hi+d) - hi)：d 大到一定程度就是
        # 灾难性抵消，本该回到 lo 的值差着 1e-20，于是「全视窗再平移」
        # 不再是空操作 —— 视窗每次都动一点点，band 缓存每次都失效。
        # 按跨度夹是精确的：撞边时 x0 直接取 hi-span，没有减法残渣。
        full = hi - lo
        span = x1 - x0
        if span <= 0:
            return False
        if span >= full * (1.0 - 1e-9):
            # 「跨度就是全长」要**吸附**到全长。平移 1e6 像素再换算回来，
            # 跨度会掉最后一两个 ulp，于是 x0 被夹到 lo 上面 1e-20 处：
            # 视窗跟全长只差一个浮点残渣，却每次都判为「动了」，
            # band 缓存跟着每次失效。请求全长就给全长。
            x0, x1 = lo, hi
        else:
            x0 = max(lo, min(x0, hi - span))
            x1 = x0 + span
        i0, i1 = fit_view(tr.x, x0, x1)
        if i1 - i0 < MIN_VIEW_PTS and (x1 - x0) < (self.view[1] - self.view[0]):
            return False
        if (x0, x1) == self.view:
            return False
        self.view = (x0, x1)
        self.band = None
        self._redraw()
        return True

    def _zoom_at(self, canvas, px, factor):
        """以指针处为锚点缩放。log 轴在 log 空间里做 —— 线性做的话滚一格就跑偏。"""
        if not self.view or not self.traces:
            return
        xf = self._xform(canvas)
        xa = xf.wx(px)
        x0, x1 = self.view
        if self.traces[self.ti].xscale == "log" and min(x0, x1, xa) > 0:
            a0, a1 = core.math.log10(x0), core.math.log10(x1)
            aa = core.math.log10(xa)
            self._set_view(10.0 ** (aa - (aa - a0) * factor),
                           10.0 ** (aa + (a1 - aa) * factor))
        else:
            self._set_view(xa - (xa - x0) * factor, xa + (x1 - xa) * factor)

    def _pan_px(self, canvas, dpx):
        """按**像素**平移。log 轴上这等价于按 decade 平移，正是想要的。"""
        if not self.view:
            return
        xf = self._xform(canvas)
        self._set_view(xf.wx(xf.l + dpx), xf.wx(xf.l + xf.pw + dpx))

    def _wheel(self, e, step=None):
        w = self.root.winfo_containing(e.x_root, e.y_root)
        if w not in (self.c_wave, self.c_err):
            return
        if step is None:
            step = 1 if getattr(e, "delta", 0) > 0 else -1
        self._zoom_at(w, e.x_root - w.winfo_rootx(),
                      ZOOM_STEP if step > 0 else 1.0 / ZOOM_STEP)

    def _pan_start(self, e):
        self._pan = e.x
        e.widget.configure(cursor="fleur")

    def _pan_move(self, e):
        if self._pan is None:
            return
        self._pan_px(e.widget, self._pan - e.x)
        self._pan = e.x

    def _pan_end(self, e):
        self._pan = None
        e.widget.configure(cursor="")

    def _key(self, e):
        """键盘缩放/平移。焦点在输入框或滑块上时**不抢键** ——
        那里 +/-/方向键是编辑操作，抢了就没法改预算了。"""
        w = self.root.focus_get()
        if isinstance(w, (tk.Entry, tk.Scale, tk.Text)):
            return
        k, c = e.keysym, self.c_wave
        if k in ("plus", "equal", "KP_Add"):
            self._zoom_at(c, c.winfo_width() // 2, ZOOM_STEP)
        elif k in ("minus", "underscore", "KP_Subtract"):
            self._zoom_at(c, c.winfo_width() // 2, 1.0 / ZOOM_STEP)
        elif k in ("Left", "Right"):
            d = self._xform(c).pw * PAN_KEY_FRAC
            self._pan_px(c, -d if k == "Left" else d)
        elif k in ("0", "Home"):
            self._zoom_all()

    def _sel_start(self, e):
        self._sel = (e.x, e.x)

    def _sel_move(self, e):
        if self._sel:
            self._sel = (self._sel[0], e.x)
            self._redraw()

    def _sel_end(self, e):
        if not self._sel:
            return
        a, b = sorted(self._sel)
        self._sel = None
        if b - a < 8 or not self.red:
            self._redraw()
            return
        xf = self._xform(self.c_wave)
        x0, x1 = xf.wx(a), xf.wx(b)
        if x1 > x0:
            self.view = (x0, x1)
            self.band = None
        self._redraw()

    # ------------------------------------------------------------ 绘制

    def _yscale(self, s, lo, hi):
        """-> (base, rng, 是否被 eps 兜底)。把这条信号映射进 0..1 的仿射变换。

        默认按**视窗内**的原始包络定尺度。全局量程在放大之后没意义：
        12 ps 的窗口里那段起伏可能只占全量程的 0.3%，横轴放大了、纵向还是直线。

        半宽下限卡在 `Y_EPS_FLOOR × eps`：视窗里什么都没发生时，尺度不该被
        容差以内的抖动撑开 —— 撑开了就会看见「红线到处跑出灰带」，
        像是压缩把这段毁了，其实全在容差里。判「丢没丢东西」看误差窗格的 ±1，
        那一格才是有刻度的；这一格是形状。
        """
        if not self.y_local.get():
            return s.vmin, (s.rng or 1.0), False
        vals = [v for v in lo if v is not None]
        vals += [v for v in hi if v is not None]
        if not vals:
            return s.vmin, (s.rng or 1.0), False
        v0, v1 = min(vals), max(vals)
        data_half = 0.5 * (v1 - v0)
        floor = Y_EPS_FLOOR * (s.eps if 0 < s.eps < float("inf") else 0.0)
        half = max(data_half, floor, 1e-300)
        return 0.5 * (v0 + v1) - half * 1.06, 2.12 * half, half > data_half

    def _xform(self, canvas, y0=0.0, y1=1.0, pad=(46, 8, 8, 20)):
        tr = self.traces[self.ti]
        return Xform(self.view[0], self.view[1], y0, y1,
                     canvas.winfo_width(), canvas.winfo_height(),
                     pad=pad, logx=(tr.xscale == "log"))

    def _redraw(self):
        if not self.red or not self.view:
            return
        self._draw_wave()
        self._draw_err()

    def _lane_blocks(self):
        """要画哪些块。全部**要搬走的**块都画，不只当前焦点那块。

        原来只画焦点块，于是解调出来的 f_inst 那一块从头到尾没在任何一张
        图上出现过 —— 它照样会被粘出去，只是没人看过它长什么样。
        """
        if not self.ship:
            return []
        return [b for b in self.ship.included() if b.red is not None]

    def _draw_wave(self):
        c = self.c_wave
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 60 or h < 60:
            return
        blocks = self._lane_blocks()
        if not blocks:
            return
        lanes = draw.lanes_of(blocks, self.lane_mode.get())
        if not lanes:
            return
        x0, x1 = self.view
        logx = self.traces[self.ti].xscale == "log"
        pl, pr = draw.GUTTER_L, draw.GUTTER_R
        pw = max(1, w - pl - pr)
        ev_h = draw.EV_H if (self.ev_v.get() and self.metrics) else 0
        axis_h = 18
        lane_h = max(70, (h - axis_h - ev_h) // len(lanes))

        i0i1 = {}
        for b in blocks:
            i0i1[id(b)] = fit_view(b.red.trace.x, x0, x1)

        def sx(xv):
            if logx:
                xv = core.math.log10(max(xv, 1e-300))
                a = core.math.log10(max(x0, 1e-300))
                z = core.math.log10(max(x1, 1e-300))
            else:
                a, z = x0, x1
            return pl + (0.0 if z == a else (xv - a) / (z - a)) * pw

        # x 刻度：整数值，且整张图只算一次
        for tv in draw.nice_ticks(x0, x1, 6):
            px = sx(tv)
            c.create_line(px, 0, px, h - axis_h, fill=GRID)

        top = 0
        self._lane_geom = []
        for li, (unit, items) in enumerate(lanes):
            self._paint_lane(c, li, unit, items, top, lane_h, pl, pw,
                             sx, i0i1, len(lanes))
            self._lane_geom.append({"top": top, "h": lane_h, "items": items,
                                    "x0": pl, "x1": pl + pw})
            top += lane_h
            if li < len(lanes) - 1:
                c.create_line(0, top, w, top, fill="#c9ced3")

        if ev_h:
            self._paint_events(c, sx, h - axis_h - ev_h, ev_h, w)
        self._paint_xaxis(c, sx, h - axis_h, w, x0, x1)
        self._paint_cursor_line(c, sx, h - axis_h)
        if self._sel:
            a, b = sorted(self._sel)
            c.create_rectangle(a, 0, b, h - axis_h, outline="#1c71d8",
                               dash=(3, 2))

    def _paint_lane(self, c, li, unit, items, top, lane_h, pl, pw, sx,
                    i0i1, nlane):
        """一道 = 一个单位。**同单位的信号共用一根纵轴。**

        各自拉满 0..1 是在生产假信息：env_hi 和 env_lo 会在图上交叉
        （物理上 env_hi >= env_lo 恒成立），六路电流峰值差近两倍的
        上下沿几乎重合。共轴之后高低是真的。
        """
        head_y = top + draw.HEAD_H
        body_y0, body_y1 = head_y + 2, top + lane_h - 4
        if body_y1 - body_y0 < 20:
            return
        c.create_rectangle(0, top, pl + pw + draw.GUTTER_R, head_y,
                           fill=draw.LANE_HEAD, outline="")
        lo, hi = draw.lane_span(items, i0i1, self.y_local.get())
        # eps 兜底：视窗里什么都没发生时，尺度不该被容差以内的抖动撑开
        floor = Y_EPS_FLOOR * max(
            (s.eps for s in (b.red.trace.signals[i] for b, i in items)
             if 0 < s.eps < float("inf")), default=0.0)
        if floor and (hi - lo) < 2 * floor:
            mid = 0.5 * (lo + hi)
            lo, hi = mid - floor, mid + floor

        def sy(v):
            return body_y1 - (v - lo) / float(hi - lo or 1.0) * (body_y1 - body_y0)

        # 纵轴真刻度。原来一根都没有 —— 图例写着 [-380.8 uA .. 2.781 mA]，
        # 中间任何一点是多少全靠估
        for tv in draw.nice_ticks(lo, hi, 4):
            py = sy(tv)
            c.create_line(pl, py, pl + pw, py, fill="#eef0f2")
            c.create_text(pl - 4, py, anchor="e", fill="#666",
                          font=("Consolas", 8),
                          text=core.eng_str(tv, unit, 3))
        if lo < 0 < hi:
            c.create_line(pl, sy(0.0), pl + pw, sy(0.0), fill="#b9bfc5")

        ncol = max(60, min(BAND_COLS, int(pw),
                           MAX_SEG // max(1, 4 * len(items))))
        names = []
        loss_txt = ""
        for k, (b, si) in enumerate(items):
            tr, red = b.red.trace, b.red
            s = tr.signals[si]
            col = COLORS[self._sig_color(b, si) % 6]
            i0, i1 = i0i1[id(b)]
            x0, x1 = self.view
            env = bin_envelope(tr.x, [s.y], i0, i1, ncol, x0, x1)
            if not env:
                continue
            olo, ohi = env[0]
            rlo, rhi = draw.recon_band(tr, red, si, i0, i1, ncol, x0, x1)
            px = [pl + i / float(max(1, ncol - 1)) * pw for i in range(ncol)]
            # ① 原始包络：每条信号**自己的**淡色。六块同一个灰会糊成一个气球
            self._band_poly(c, px, olo, ohi, sy, _pale(col), "")
            # ② 重建：会写进 .wv 的那条
            self._band_poly(c, px, rlo, rhi, sy, "", col)
            # ③ 影：被削掉的摆幅。判据方向因此是**正向**的 —— 丢多少是
            #    画面上主动出现的东西，不用人去比两条带子的宽窄
            eps = s.eps if 0 < s.eps < float("inf") else 0.0
            worst = 0.0
            for a, z, side in draw.clipped_runs(olo, ohi, rlo, rhi, eps):
                pts = []
                if side == "hi":
                    for i in range(a, z + 1):
                        pts += [px[i], sy(ohi[i])]
                    for i in range(z, a - 1, -1):
                        pts += [px[i], sy(rhi[i])]
                    worst = max(worst, max(ohi[i] - rhi[i]
                                           for i in range(a, z + 1)))
                else:
                    for i in range(a, z + 1):
                        pts += [px[i], sy(rlo[i])]
                    for i in range(z, a - 1, -1):
                        pts += [px[i], sy(olo[i])]
                    worst = max(worst, max(olo[i] - rlo[i]
                                           for i in range(a, z + 1)))
                if len(pts) >= 6:
                    # gray12 而不是 gray25：丢得多的时候「影」几乎盖住整个
                    # 包络（3.8 点/周期时它**就是**整个包络），太实的话
                    # 整格变成一块红，反而看不见中间那条重建曲线 ——
                    # 而「重建成了什么样」和「丢了多少」要同时看得见。
                    c.create_polygon(pts, fill=col, outline="",
                                     stipple="gray12")
            why = explain(s.name)
            names.append((col, "c%d %s%s"
                          % (si + 1, s.name, ("  ← " + why) if why else "")))
            if worst > 0:
                loss_txt = "  摆幅削掉最多 %s" % core.eng_str(worst, unit, 3)

        # 道头：图例、纵轴范围、点/周期。**绝不压数据**
        x = 6
        lim = pl + pw - 80
        for col, nm in names[:6]:
            if x > lim:                     # 放不下就别叠着画
                c.create_text(x, top + 8, anchor="w", text="…", fill="#888",
                              font=("Consolas", 8))
                break
            c.create_rectangle(x, top + 4, x + 8, top + 12, fill=col,
                               outline="")
            c.create_text(x + 11, top + 8, anchor="w", text=nm, fill=FG,
                          font=("Consolas", 8))
            x += 15 + draw.text_cols(nm) * 6
        tail = "纵轴 %s..%s %s%s" % (
            core.eng_str(lo, unit, 3), core.eng_str(hi, unit, 3),
            "视窗" if self.y_local.get() else "全长", loss_txt)
        c.create_text(pl + pw + draw.GUTTER_R - 6, top + 8, anchor="e",
                      text=tail + self._ppc_note(items), fill="#4a5560",
                      font=("Consolas", 8))

    def _ppc_note(self, items):
        """点/周期。用词和 `carrier_warn` 完全一致，放大之后会自己变回来 ——
        「照建议做了、界面一字不变」那个死循环就是这么断掉的。"""
        worst = None
        for b, si in items:
            s = b.red.trace.signals[si]
            if s.cycles < emit.MIN_CYCLES:
                continue
            i0, i1 = fit_view(b.red.trace.x, self.view[0], self.view[1])
            vis = sum(1 for i in b.red.kept if i0 <= i < i1)
            span = (self.view[1] - self.view[0])
            full = b.red.trace.x[-1] - b.red.trace.x[0]
            cyc = s.cycles * (span / full) if full > 0 else s.cycles
            if cyc < 1:
                continue
            ppc = vis / float(cyc)
            if worst is None or ppc < worst:
                worst = ppc
        if worst is None:
            return ""
        if worst < emit.MIN_PTS_PER_CYCLE:
            return "  %.1f 点/周期(!) 画不出正弦，别数周期别量摆幅" % worst
        return "  %.1f 点/周期·形状可信" % worst

    def _sig_color(self, b, si):
        """跨块也要唯一。两块各自从 c1 开始的话，图上两条不同的曲线同色。"""
        n = 0
        for x in self._lane_blocks():
            if x is b:
                return n + si
            n += len(x.red.trace.signals)
        return si

    def _band_poly(self, c, px, lo, hi, sy, fill, outline):
        top, bot = [], []
        for i in range(len(px)):
            if lo[i] is None or hi[i] is None:
                continue
            top.append((px[i], sy(hi[i])))
            bot.append((px[i], sy(lo[i])))
        if len(top) < 2:
            return
        if fill:
            poly = []
            for p in top:
                poly += [p[0], p[1]]
            for p in reversed(bot):
                poly += [p[0], p[1]]
            c.create_polygon(poly, fill=fill, outline="")
        if outline:
            for edge in (top, bot):
                c.create_line([v for p in edge for v in p], fill=outline,
                              width=1)

    def _paint_xaxis(self, c, sx, y, w, x0, x1):
        tr = self.traces[self.ti]
        c.create_line(0, y, w, y, fill="#c9ced3")
        for tv in draw.nice_ticks(x0, x1, 6):
            px = sx(tv)
            c.create_line(px, y, px, y + 4, fill="#888")
            c.create_text(px, y + 5, anchor="n", fill="#666",
                          font=("Consolas", 8),
                          text=core.eng_str(tv, tr.xunit, 4))
        c.create_text(w - 6, y + 5, anchor="ne", fill="#999",
                      font=("Consolas", 8),
                      text="滚轮缩放·右键拖平移·框选·双击复位")

    def _paint_events(self, c, sx, y, hh, w):
        """事件**单独一条轨**，绝不压数据。"""
        if not self.metrics:
            return
        try:
            evs = self.metrics.events()
        except Exception:                           # noqa: BLE001
            return
        lo, hi = self.view
        vis = [e for e in evs if lo <= e.x <= hi]
        c.create_rectangle(0, y, w, y + hh, fill="#fafbfc", outline="")
        label = len(vis) <= EV_LABELS
        for e in vis:
            px = sx(e.x)
            col = COLORS[_col_index(e.col) % 6]
            c.create_line(px, 0, px, y + hh, fill=col, dash=(2, 3))
            if label:
                c.create_text(px + 2, y + hh // 2, anchor="w",
                              text="%s %s" % (e.col, e.tag), fill=col,
                              font=("Consolas", 7))
        if vis and not label:
            c.create_text(6, y + hh // 2, anchor="w", fill="#888",
                          font=("Consolas", 8),
                          text="视窗里 %d 个 EVENTS，只画了竖线；"
                               "放大到 %d 个以内才标名字" % (len(vis), EV_LABELS))

    def _paint_cursor_line(self, c, sx, hmax):
        """游标：把「丢了什么」从形容词变成数字。

        上下两格都是**形状**，读不出「这一点丢了多少」。而这正是这个窗口
        要回答的问题，也是把 EVENTS 里那些时间点对回图上的唯一一把尺。
        只重画 tag="cursor"，所以鼠标移动不触发全图重绘。
        """
        c.delete("cursor")
        if self._cursor_x is None or not self._lane_geom:
            return
        px = sx(self._cursor_x)
        c.create_line(px, 0, px, hmax, fill="#1c71d8", tags="cursor")
        tr0 = self.traces[self.ti]
        c.create_text(px + 3, 2, anchor="nw", fill="#1c71d8", tags="cursor",
                      font=("Consolas", 8),
                      text=core.eng_str(self._cursor_x, tr0.xunit, 5))
        for g in self._lane_geom:
            y = g["top"] + draw.HEAD_H + 4
            for b, si in g["items"]:
                tr, red = b.red.trace, b.red
                s = tr.signals[si]
                j = bisect.bisect_left(tr.x, self._cursor_x)
                j = max(0, min(j, len(tr.x) - 1))
                raw = s.y[j]
                kx, ky = draw.recon_series(tr, red, si)
                u = self._cursor_x
                if tr.xscale == "log" and u > 0:
                    u = core.math.log(u)
                rec = draw.recon_at(kx, ky, u)
                eps = s.eps if 0 < s.eps < float("inf") else 0.0
                d = raw - rec
                col = COLORS[self._sig_color(b, si) % 6]
                txt = ("c%d 原 %s\n   存 %s\n   差 %s%s"
                       % (si + 1, core.eng_str(raw, s.unit, 4),
                          core.eng_str(rec, s.unit, 4),
                          core.eng_str(d, s.unit, 3),
                          (" (%.1f×)" % abs(d / eps)) if eps else ""))
                c.create_text(g["x1"] + 4, y, anchor="nw", fill=col,
                              tags="cursor", font=("Consolas", 8), text=txt)
                y += 34

    def _on_motion(self, e):
        if not self.red or not self.view or self._lane_geom is None:
            return
        g = self._lane_geom
        if not g:
            return
        pl, pw = g[0]["x0"], g[0]["x1"] - g[0]["x0"]
        if not (pl <= e.x <= pl + pw):
            if self._cursor_x is not None:
                self._cursor_x = None
                self.c_wave.delete("cursor")
            return
        x0, x1 = self.view
        self._cursor_x = x0 + (e.x - pl) / float(max(1, pw)) * (x1 - x0)
        h = self.c_wave.winfo_height()
        self._paint_cursor_line(self.c_wave, lambda v: pl + (v - x0) /
                                float((x1 - x0) or 1.0) * pw, h - 18)


    def _draw_cycles(self):
        """下窗格：代表性周期叠在一起（横轴对齐到各自周期起点）。

        `[CYCLES]` 是输出里唯一**一块画面都没有**的段，而它恰恰是判断
        「波形失真没失真」该看的那块 —— 解调把包络抹平了，畸变只在这里看得见。
        叠着画是刻意的：几个周期重合得好不好，一眼就知道形状稳不稳。
        """
        c = self.c_err
        c.delete("all")
        picks = getattr(self.red.trace, "picks", None) or []
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50 or h < 50:
            return
        if not picks:
            c.create_text(w // 2, h // 2, fill="#888", font=("Consolas", 9),
                          text="没有代表性周期 —— 勾上「解调」才有"
                               "（这一格画的是 CYCLES 段）")
            return
        tmax = max(max(p["t"]) for p in picks if p["t"]) or 1.0
        ylo = min(min(p["y"]) for p in picks if p["y"])
        yhi = max(max(p["y"]) for p in picks if p["y"])
        if yhi <= ylo:
            yhi = ylo + 1.0
        xf = Xform(0.0, tmax, ylo, yhi, w, h)
        self._grid(c, xf, w, h)
        u = self.red.trace.signals[0].unit if self.red.trace.signals else ""
        for i, p in enumerate(picks):
            col = COLORS[i % 6]
            line = []
            for t, y in zip(p["t"], p["y"]):
                line += [xf.sx(t), xf.sy(y)]
            if len(line) >= 4:
                c.create_line(line, fill=col, width=1)
            c.create_text(xf.l + 6, 10 + i * 11, anchor="w", fill=col,
                          font=("Consolas", 7),
                          text="@%s 幅度 %s 残差 %.1f%% (%s)"
                               % (core.eng_str(p["at"], self.red.trace.xunit, 4),
                                  core.eng_str(p["amp"], u, 3),
                                  100.0 * p["resid"], p["why"]))
        c.create_text(w - 8, h - 8, anchor="se", fill="#888",
                      font=("Consolas", 8),
                      text="代表性周期的原始样点，横轴对齐到各自周期起点；"
                           "重合得好=形状稳，散开=有畸变")

    def _draw_err(self):
        """误差格：symlog 纵轴 + 绿带 + **全部要搬走的块**。

        原来纵轴硬钳在 ±3，于是 3 倍和 300 倍长得一模一样 —— 而那正是
        「调 tol 还是换模式」的决定量。实测起振电流 500 ns 之后是一整块
        顶天立地的红，用户把误差从 49% 调到 10%，这一格毫无变化：
        整个调参循环里唯一的图形反馈是死的。
        """
        if self.low_v.get() == "cycles":
            self._draw_cycles()
            return
        c = self.c_err
        c.delete("all")
        w, h = c.winfo_width(), c.winfo_height()
        if w < 60 or h < 60:
            return
        blocks = self._lane_blocks()
        if not blocks:
            return
        pl, pr, ptop, pbot = draw.GUTTER_L, draw.GUTTER_R, 20, 18
        pw = max(1, w - pl - pr)
        ph = max(1, h - ptop - pbot)
        x0, x1 = self.view
        logx = self.traces[self.ti].xscale == "log"

        def sx(xv):
            if logx:
                xv = core.math.log10(max(xv, 1e-300))
                a = core.math.log10(max(x0, 1e-300))
                z = core.math.log10(max(x1, 1e-300))
            else:
                a, z = x0, x1
            return pl + (0.0 if z == a else (xv - a) / (z - a)) * pw

        ncol = max(60, min(BAND_COLS, int(pw)))
        series = []
        peak = 1.0
        for b in blocks:
            tr = b.red.trace
            i0, i1 = fit_view(tr.x, x0, x1)
            for si in range(len(tr.signals)):
                band = error_band(tr, b.red, si, i0, i1, ncol, x0, x1)
                hits = [v for v in band if v is not None]
                pk = max((max(abs(v[0]), abs(v[1])) for v in hits), default=0.0)
                peak = max(peak, pk)
                series.append((b, si, band, pk))
        lim = draw.symlog(peak * 1.15)

        def sy(e):
            return ptop + (lim - draw.symlog(e)) / (2.0 * lim) * ph

        # 绿带是**正向**判据：「在绿区里」比「没冲出红虚线」直接
        c.create_rectangle(pl, ptop, pl + pw, h - pbot, fill=draw.BAD_BAND,
                           outline="")
        c.create_rectangle(pl, sy(1.0), pl + pw, sy(-1.0), fill=draw.OK_BAND,
                           outline="")
        for tv, lab in draw.symlog_ticks(peak):
            if abs(draw.symlog(tv)) > lim:
                continue
            py = sy(tv)
            c.create_line(pl, py, pl + pw, py, fill="#e3e6e9")
            c.create_text(pl - 4, py, anchor="e", fill="#666",
                          font=("Consolas", 8), text=lab)
        for tv in draw.nice_ticks(x0, x1, 6):
            c.create_line(sx(tv), ptop, sx(tv), h - pbot, fill="#eef0f2")

        for b, si, band, pk in series:
            col = COLORS[self._sig_color(b, si) % 6]
            px = [pl + i / float(max(1, ncol - 1)) * pw for i in range(ncol)]
            lo = [v[0] if v else None for v in band]
            hi = [v[1] if v else None for v in band]
            self._band_poly(c, px, lo, hi, sy,
                            _pale(col) if len(series) == 1 else "", col)

        # 容差线画在**最上面**，而且换色：原来用 #e01b24，正是 c1 的颜色，
        # 于是单路（最常见的情形）下刻度和数据同色，等于没有刻度
        for lv in (1.0, -1.0):
            py = sy(lv)
            c.create_line(pl, py, pl + pw, py, fill=draw.TOL_LINE, dash=(4, 3))
        c.create_line(pl, sy(0.0), pl + pw, sy(0.0), fill="#9aa4ad")
        c.create_line(pl, h - pbot, w - pr, h - pbot, fill="#c9ced3")
        for tv in draw.nice_ticks(x0, x1, 6):
            c.create_text(sx(tv), h - pbot + 4, anchor="n", fill="#666",
                          font=("Consolas", 8),
                          text=core.eng_str(tv, self.traces[self.ti].xunit, 4))

        c.create_text(pl, 9, anchor="w", fill=FG, font=("Consolas", 8),
                      text="误差 / 各信号自己的容差（symlog）—— 绿区=没超差；"
                           "带子=这一列里误差覆盖的范围")
        # 峰值榜按大小降序、每条用自己的颜色。原来按列序取前四、
        # 颜色恒取第一名的 —— 六路时 c5/c6 永远看不到
        rank = sorted(series, key=lambda t: -t[3])[:5]
        x = w - pr
        for b, si, _band, pk in reversed(rank):
            txt = "c%d %.1f×" % (si + 1, pk)
            c.create_text(x, 9, anchor="e", font=("Consolas", 8),
                          fill=COLORS[self._sig_color(b, si) % 6], text=txt)
            x -= 8 + len(txt) * 7
        self._paint_global_worst(c, sx, ptop, h - pbot)

    def _paint_global_worst(self, c, sx, y0, y1):
        """顶上的判据是**整条**的，这一格画的只有**视窗内**的。

        两者可以永远互相矛盾且没有出口：放大到一段压得好的地方，
        这一格全绿而出口台仍写着 ✗ 24.3×，而唯一的定位信息
        `@ 1.02 us` 是一行文字。所以最差点要么画出来，要么说它不在画面里。
        """
        v = self.ship.verdict() if self.ship else None
        if not v or not v.worst or v.worst.at is None:
            return
        at = v.worst.at
        if self.view[0] <= at <= self.view[1]:
            px = sx(at)
            c.create_polygon(px - 5, y0, px + 5, y0, px, y0 + 8,
                             fill=VCOL["bad"], outline="")
            c.create_text(px + 7, y0 + 4, anchor="w", fill=VCOL["bad"],
                          font=("Consolas", 8), text="全局最差 %.1f×" % v.peak_eps)
        else:
            c.create_text(draw.GUTTER_L + 4, y1 - 6, anchor="sw",
                          fill=VCOL["bad"], font=("Consolas", 8),
                          text="全局最差 %.1f× @ %s（不在当前视窗）"
                               % (v.peak_eps,
                                  core.eng_str(at, self.traces[self.ti].xunit, 4)))


    def _grid(self, c, xf, w, h):
        tr = self.traces[self.ti]
        for k in range(5):
            px = xf.l + k / 4.0 * xf.pw
            c.create_line(px, xf.t, px, h - xf.b, fill=GRID)
            c.create_text(px, h - 6, anchor="s", fill="#666",
                          font=("Consolas", 8),
                          text=core.eng_str(xf.wx(px), tr.xunit, 4))

    # ------------------------------------------------------------ 保存

    def _save(self):
        if not self.red:
            self.status.set("还没有可保存的内容 —— 先打开一个 CSV")
            return
        p = filedialog.asksaveasfilename(defaultextension=".wv",
                                         filetypes=[("wave reduce", "*.wv")])
        if not p:
            return
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(self.wv_text())
        self.status.set("已写 %s（%d 字节）" % (p, os.path.getsize(p)))


# --------------------------------------------------------------- 自检


def selftest(path, args, timeout=60.0):
    """无人值守跑一遍：载入 -> 等预细化 -> 动滑块 -> 重画 -> 打状态 -> 退出。

    有硬超时，绝不会挂住。CI / 无头复核靠它。
    """
    root = tk.Tk()
    log = []
    t_start = time.time()

    # Tk 默认把回调里的异常打到 stderr 然后**继续跑 mainloop** ——
    # 自检要是撞上这条就永远不退出。这里改成记下来立刻收摊。
    def boom(exc, val, tb):
        import traceback
        log.append("EXC %s" % "".join(traceback.format_exception(exc, val, tb)))
        try:
            root.destroy()
        except tk.TclError:
            pass

    root.report_callback_exception = boom
    root.after(int(timeout * 1000), lambda: (log.append("TIMEOUT"),
                                             root.destroy()))
    app = WaveGui(root, path, args)

    def step(n):
        if time.time() - t_start > timeout:
            log.append("TIMEOUT")
            root.destroy()
            return
        if not app.red:
            root.after(60, lambda: step(n))
            return
        root.update_idletasks()
        if n == 0:
            log.append("载入完成: %d trace, %d 原始点, %d 候选点"
                       % (len(app.traces), len(app.traces[0].x), len(app.cand)))
        if n < 4:
            app.mp_v.set(max(2, int(len(app.cand) * (0.05 * (n + 1) ** 2))))
            app._recompute(True)
            root.update_idletasks()
            log.append("max_points=%-5d -> kept %-5d  bytes %-6d  "
                       "max|err| %.3f%%  RDP %.0f ms  canvas items %d/%d"
                       % (app.mp_v.get(), len(app.red.kept), app.nbytes,
                          app.red.worst.pct if app.red.err else -1, app.ms,
                          len(app.c_wave.find_all()), len(app.c_err.find_all())))
            root.after(20, lambda: step(n + 1))
            return
        if n == 4:
            tr = app.traces[0]
            a, b = tr.x[0], tr.x[0] + (tr.x[-1] - tr.x[0]) * 0.15
            app.view = (a, b)
            app.band = None
            app._redraw()
            root.update_idletasks()
            log.append("框选缩放到 %s..%s -> canvas items %d/%d"
                       % (core.eng_str(a, tr.xunit, 4),
                          core.eng_str(b, tr.xunit, 4),
                          len(app.c_wave.find_all()), len(app.c_err.find_all())))

            # --- 缩放/平移。滚轮那层要按指针位置分派，无人值守里模拟不可靠，
            # 所以直接打它调用的锚点缩放；键盘走真事件（那条不依赖指针位置）。
            w2 = app.c_wave.winfo_width() // 2
            sp0 = app.view[1] - app.view[0]
            app._zoom_at(app.c_wave, w2, ZOOM_STEP)
            root.update_idletasks()
            sp1 = app.view[1] - app.view[0]
            log.append("滚轮缩放: 跨度 %s -> %s (%.3fx)"
                       % (core.eng_str(sp0, tr.xunit, 3),
                          core.eng_str(sp1, tr.xunit, 3), sp1 / sp0))
            mid0 = 0.5 * (app.view[0] + app.view[1])
            app._pan_px(app.c_wave, app._xform(app.c_wave).pw * 0.25)
            root.update_idletasks()
            log.append("平移: 中心 %s -> %s"
                       % (core.eng_str(mid0, tr.xunit, 4),
                          core.eng_str(0.5 * (app.view[0] + app.view[1]),
                                       tr.xunit, 4)))
            # 纵轴两种尺度：在一个**窄**视窗上比才说明问题。宽视窗里两者差不多，
            # 差距正是随着放大长出来的 —— 这个功能存在的全部理由。
            # 取全长 40% 处的 0.5% 窗口（demo_tran 上正好是 120 ns 那根 glitch）。
            mid = tr.x[0] + (tr.x[-1] - tr.x[0]) * 0.40
            half = (tr.x[-1] - tr.x[0]) * 0.0025
            app.view = (mid - half, mid + half)
            app._redraw()
            root.update_idletasks()
            blocks = app._lane_blocks()
            # 用「每路一道」量：共轴模式下这一道装着同单位的全部信号，
            # 局部/全局之比会被最大那条压住，测不出「放大局部有没有用」
            lane = draw.lanes_of(blocks, "each")[0]
            i0i1 = {id(b): fit_view(b.red.trace.x, app.view[0], app.view[1])
                    for b in blocks}
            s0 = app.red.trace.signals[0]
            lo_l, hi_l = draw.lane_span(lane[1], i0i1, True)
            lo_g, hi_g = draw.lane_span(lane[1], i0i1, False)
            r_l, r_g = hi_l - lo_l, hi_g - lo_g
            log.append("纵轴@窄视窗 %s: 视窗 %s / 全局 %s (%.1fx)"
                       % (core.eng_str(2 * half, tr.xunit, 3),
                          core.eng_str(r_l, s0.unit, 3),
                          core.eng_str(r_g, s0.unit, 3),
                          (r_g / r_l) if r_l else 0.0))
            # 一直放大到底：必须自己停在「视窗里还剩几个原始点」上
            for _ in range(80):
                app._zoom_at(app.c_wave, w2, ZOOM_STEP)
            root.update_idletasks()
            i0, i1 = fit_view(app.red.trace.x, app.view[0], app.view[1])
            log.append("深缩放到底: 视窗内原始点 %d (下限 %d)"
                       % (i1 - i0, MIN_VIEW_PTS))
            root.focus_set()                    # 键盘事件别落在预算输入框里
            root.event_generate("<Key>", keysym="0")
            root.update_idletasks()
            log.append("按 0 复位: 视窗 == 全长 %s"
                       % (app.view == (tr.x[0], tr.x[-1])))
            app.view = (a, b)
            app.band = None
            app._redraw()
            # 预算可改 + 一键压到预算
            for kb in ("20", "40", "6"):
                app.budget_v.set(kb)
                app._on_budget()
                app._fit()
                root.update_idletasks()
                log.append("预算 %-3s KB -> kept %-5d bytes %-6d %s"
                           % (kb, len(app.red.kept), app.nbytes,
                              "OK" if app.nbytes <= app.budget() else "压不进(已声明)"))
            app.budget_v.set("20")
            app._on_budget()
            app._fit()
            root.update_idletasks()
            # 复制到剪贴板：.wv 的归宿就是聊天框
            app._copy()
            root.update()
            clip = root.clipboard_get()
            log.append("剪贴板: %d 字节，首行 %r"
                       % (len(clip.encode("utf-8")),
                          clip.splitlines()[0][:48]))
            log.append("剪贴板内容 == 当前 .wv: %s"
                       % (clip == app.wv_text()))
            # 三个页签各有各的内容
            app._fill_text()
            n_met = len(app.txt_metrics.get("1.0", "end").splitlines())
            n_full = len(app.txt.get("1.0", "end").splitlines())
            log.append("下窗格 METRICS %d 行 / 完整 .wv %d 行" % (n_met, n_full))
            log.append("状态栏: " + app.status.get())
        root.destroy()

    root.after(50, lambda: step(0))
    root.mainloop()
    for ln in log:
        print(ln)
    bad = [ln for ln in log if ln.startswith(("TIMEOUT", "EXC"))]
    return 1 if (bad or not log) else 0


def run(path, args):
    if tk is None:
        sys.stderr.write("这台机器没有 tkinter，GUI 用不了；"
                         "命令行照常可用。\n")
        return 2
    if getattr(args, "selftest", False):
        if not path:
            sys.stderr.write("--selftest 要给一个 CSV\n")
            return 1
        return selftest(path, args)
    # 没给文件也照样开窗 —— 窗口里有「打开 CSV…」，不该先被一个对话框卡住
    root = tk.Tk()
    WaveGui(root, path, args)
    root.mainloop()
    return 0
