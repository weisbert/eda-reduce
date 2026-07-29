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

import wave_core as core
import wave_emit as emit

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
        self.cand = None
        self.red = None
        self.metrics = None
        self.fixed_bytes = 0
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
        self.nrep_v = tk.IntVar(value=getattr(args, "demod_cycles", None) or 6)
        self.fspan_v = tk.IntVar(value=getattr(args, "demod_fspan", 0) or 0)
        self.view_full = tk.BooleanVar(value=False)
        self._build()
        # 没给文件也要能开窗。开窗和选文件是两件事，绑死了不合理 ——
        # 想先看看界面、想换一个文件重来，都不该被逼着先满足一个文件对话框。
        if path:
            self._load_async(path)
        else:
            self.status.set("还没打开文件 —— 点左下角「打开 CSV…」，"
                            "或者命令行给一个：wave my.csv --gui")

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
        r.minsize(900, 600)

        top = tk.Frame(r, bg=BG)
        top.pack(fill="x", padx=8, pady=(8, 2))
        # 状态栏必须**换行**而不是被窗口右边切掉。这一行里有 WARN、点数、
        # 字节数、误差四样东西，切掉右半条等于把误差藏了 ——
        # 而误差恰恰是判断这次压缩能不能用的那个数。
        # 进度条先占位再放标签：pack 是按调用顺序分配的，标签先拿了
        # expand 的空间就会把进度条挤没。
        #
        # **一个控件不能有两个含义。** 原来这条既当载入进度、又当预算占用率：
        # 载入那半从来没动过（`work()` 从没往队列 push 过进度值，是死代码），
        # 载完又被 `_status` 拿去表示 nbytes/budget —— 于是满格既可能是
        # 「载完了」也可能是「超预算 63 倍」。现在它**只表示载入**，
        # 而且改成 indeterminate：解析 1e7 点没有可信的百分比，
        # 假装有一个精确进度比转圈更糟。预算占用率归 `_status` 的文字。
        self.pb = ttk.Progressbar(top, mode="indeterminate", length=160)
        self.stat_lab = tk.Label(top, textvariable=self.status, bg=BG, fg=FG,
                                 font=("Consolas", 10), justify="left",
                                 anchor="w")
        self.stat_lab.pack(side="left", fill="x", expand=True)
        top.bind("<Configure>", lambda e: self.stat_lab.configure(
            wraplength=max(200, e.width - 210)))

        self.c_wave = tk.Canvas(r, bg=BG, height=330, highlightthickness=1,
                                highlightbackground=GRID)
        self.c_wave.pack(fill="both", expand=True, padx=8, pady=2)
        self.c_err = tk.Canvas(r, bg=BG, height=180, highlightthickness=1,
                               highlightbackground=GRID)
        self.c_err.pack(fill="x", padx=8, pady=2)

        ctl = tk.Frame(r, bg=BG)
        ctl.pack(fill="x", padx=8, pady=4)
        tk.Label(ctl, text="max-points", bg=BG, fg=FG).grid(row=0, column=0)
        self.s_mp = tk.Scale(ctl, from_=2, to=4000, orient="horizontal",
                             variable=self.mp_v, length=330, bg=BG, fg=FG,
                             highlightthickness=0, command=self._on_slider)
        self.s_mp.grid(row=0, column=1, padx=(4, 18))
        self.tol_lab = tk.StringVar(value="tol (‰)")
        tk.Label(ctl, textvariable=self.tol_lab, bg=BG,
                 fg=FG).grid(row=0, column=2)
        self.s_tol = tk.Scale(ctl, from_=0.1, to=100.0, resolution=0.1,
                              orient="horizontal", variable=self.tol_v,
                              length=250, bg=BG, fg=FG, highlightthickness=0,
                              command=self._on_tol)
        self.s_tol.grid(row=0, column=3, padx=4)
        # 预算是整个工具在优化的那个数，必须能在这里改 ——
        # 20 KB 是「聊天框这条通道」的宽度，换条通道就该跟着变。
        bb = tk.Frame(ctl, bg=BG)
        bb.grid(row=1, column=3, sticky="w", pady=(4, 0))
        tk.Label(bb, text="预算", bg=BG, fg=FG).pack(side="left")
        e = tk.Entry(bb, textvariable=self.budget_v, width=6, bg="#fff", fg=FG)
        e.pack(side="left", padx=(4, 2))
        e.bind("<Return>", lambda _: self._on_budget())
        e.bind("<FocusOut>", lambda _: self._on_budget())
        tk.Label(bb, text="KB (0=不限)", bg=BG, fg=FG).pack(side="left")
        tk.Button(bb, text="自动压到预算", command=self._fit).pack(side="left",
                                                                  padx=6)

        self.colbox = tk.Frame(ctl, bg=BG)
        self.colbox.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))
        fb = tk.Frame(ctl, bg=BG)
        fb.grid(row=0, column=4, rowspan=2, padx=12)
        tk.Checkbutton(fb, text="强制保留极值", variable=self.force_extrema,
                       bg=BG, fg=FG, selectcolor=BG,
                       command=self._defer).pack(anchor="w")
        tk.Checkbutton(fb, text="强制保留 spur/事件", variable=self.force_metrics,
                       bg=BG, fg=FG, selectcolor=BG,
                       command=self._defer).pack(anchor="w")
        tk.Checkbutton(fb, text="纵轴跟视窗", variable=self.y_local,
                       bg=BG, fg=FG, selectcolor=BG,
                       command=self._redraw).pack(anchor="w")
        mb = tk.Frame(ctl, bg=BG)
        mb.grid(row=0, column=5, rowspan=2, padx=12)
        tk.Checkbutton(mb, text="解调（包络+频率+代表周期）", variable=self.demod_v,
                       bg=BG, fg=FG, selectcolor=BG,
                       command=self._remode).pack(anchor="w")
        tk.Checkbutton(mb, text="只压当前视窗", variable=self.win_v,
                       bg=BG, fg=FG, selectcolor=BG,
                       command=self._remode).pack(anchor="w")
        tk.Checkbutton(mb, text="波形上标出 EVENTS", variable=self.ev_v,
                       bg=BG, fg=FG, selectcolor=BG,
                       command=self._redraw).pack(anchor="w")
        # 解调那一层的两个**取舍**旋钮。tol 管不着它们（tol 只管「算出来的曲线
        # 存得准不准」），而它们决定「测得准不准」—— 两层精度是叠加的
        kb = tk.Frame(mb, bg=BG)
        kb.pack(anchor="w", pady=(2, 0))
        tk.Label(kb, text="代表周期", bg=BG, fg=FG).pack(side="left")
        tk.Spinbox(kb, from_=0, to=12, width=3, textvariable=self.nrep_v,
                   command=self._remode).pack(side="left", padx=(2, 8))
        tk.Label(kb, text="测频跨周期", bg=BG, fg=FG).pack(side="left")
        tk.Spinbox(kb, from_=0, to=64, width=3, textvariable=self.fspan_v,
                   command=self._remode).pack(side="left", padx=2)
        tk.Label(kb, text="(0=自动)", bg=BG, fg="#888").pack(side="left")

        # 下窗格的工具条。**复制到剪贴板**是这里最重要的一个按钮：
        # .wv 的归宿就是粘进聊天框，「先另存成文件、再打开、再全选、再复制」
        # 中间三步纯属多余。
        tb = tk.Frame(r, bg=BG)
        tb.pack(fill="x", padx=8, pady=(4, 0))
        tk.Button(tb, text="打开 CSV…", command=self._open).pack(side="left")
        self.tracebar = tk.Frame(tb, bg=BG)
        tk.Button(tb, text="复制全文到剪贴板", command=self._copy).pack(
            side="left", padx=(8, 2))
        tk.Button(tb, text="另存 .wv…", command=self._save).pack(side="left")
        tk.Label(tb, text="  下格:", bg=BG, fg=FG).pack(side="left", padx=(12, 0))
        for val, lab in (("err", "误差"), ("cycles", "代表周期")):
            tk.Radiobutton(tb, text=lab, variable=self.low_v, value=val,
                           bg=BG, fg=FG, selectcolor=BG,
                           command=self._redraw).pack(side="left")
        tk.Radiobutton(tb, text="METRICS", variable=self.view_full, value=False,
                       bg=BG, fg=FG, selectcolor=BG,
                       command=self._fill_text).pack(side="left", padx=(16, 0))
        tk.Radiobutton(tb, text="完整 .wv（可全选手动复制）",
                       variable=self.view_full, value=True, bg=BG, fg=FG,
                       selectcolor=BG, command=self._fill_text).pack(side="left")

        tf = tk.Frame(r, bg=BG)
        tf.pack(fill="both", expand=True, padx=8, pady=(2, 8))
        sb = tk.Scrollbar(tf)
        sb.pack(side="right", fill="y")
        self.txt = tk.Text(tf, height=10, bg="#fafafa", fg=FG,
                           font=("Consolas", 9), wrap="none",
                           yscrollcommand=sb.set)
        self.txt.pack(side="left", fill="both", expand=True)
        sb.config(command=self.txt.yview)
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
        self.traces, self.red, self.metrics, self.cand = [], None, None, None
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
            self.traces = self._build_traces(raw)
            self.ti_v.set(0)
            self._use_trace(0)             # metrics / 候选集在这里面算
            return
        self.root.after(40, lambda: self._poll(q))

    # ------------------------------------------------------------ 计算

    def _sub_trace(self):
        """按列选做一个浅拷贝 trace（数组是共享的，不复制数据）。"""
        tr = self.traces[self.ti]
        keep = [s for s, v in zip(tr.signals, self.cols) if v.get()]
        if len(keep) == len(tr.signals) or not keep:
            return tr
        sub = core.Trace(tr.xname, tr.index)
        for a in ("source", "xunit", "xunit_src", "x", "kind", "kind_src",
                  "xscale", "notes", "dt_med", "dt_min", "dt_max"):
            setattr(sub, a, getattr(tr, a))
        sub.signals = keep
        return sub

    def _reduce(self, max_points, check=True):
        tr = self._sub_trace()
        tol = self.tol_v.get() / 1000.0
        core.set_eps(tr, tol)
        # 强制保留点是一组**x 下标**，对任何列子集都成立 —— 原来这里有个
        # 「所有列都还在才用」的守卫，后果是：六路里取消任何一列勾选，
        # spur/事件的强制保留**静默全关**，复选框照样打着勾，点数和字节数
        # 暴跌，人只会归因于「少了五列」。守卫本身是多余的，删掉。
        # （代价：保底点按整条记，取消某一列不会减少它们 —— 这条要在界面上写明。）
        forced = []
        if self.force_metrics.get() and self.metrics:
            forced = list(self.metrics.forced())
        return core.reduce_trace(tr, tol, max_points, self.cand, forced,
                                 keep_extrema=self.force_extrema.get(),
                                 check=check)

    def _recompute(self, precise=True):
        if not self.traces:
            return
        # 上一次算了 150 ms 以上就先把「重算中」刷出去。同步的活没法打断，
        # 但**不能让窗口看起来死了** —— 六路 20 万点一次两秒多，
        # 没有这行的话按下去到出结果之间界面完全没反应。
        if getattr(self, "ms", 0.0) >= SLOW_MS:
            self.status.set("重算中…")
            self.root.update_idletasks()
        t0 = time.time()
        self.red = self._reduce(self.mp_v.get(), check=precise)
        self.ms = (time.time() - t0) * 1000.0
        if precise:
            txt = emit.emit(self.red, self.metrics)
            self.nbytes = emit.nbytes(txt)
            self.fixed_bytes = self.nbytes - emit.shape_bytes(self.red)
            self._fill_text()
        else:
            self.nbytes = self.fixed_bytes + emit.shape_bytes(self.red)
        self._sync_eps_marks()
        self._status()
        self._redraw()

    def wv_text(self):
        """当前参数下的完整 .wv —— 就是要粘进聊天框的那份东西。

        **所有 trace 都得在里面。** 原来只发当前这一条：布局 B（两个 time 列）
        和 `--demod`（包络块 + 频率块）都会产生多条 trace，粘出去的东西是残的，
        而且残得看不出来 —— 头部长得一模一样，只是少了一块。
        这条按钮是整个工具的出口，不能是残的。
        """
        if not self.red:
            return ""
        parts = []
        for i, tr in enumerate(self.traces):
            if i == self.ti:
                parts.append(emit.emit(self.red, self.metrics))
            else:
                parts.append(self._emit_other(tr))
        return "\n".join(parts)

    def _emit_other(self, tr):
        """非当前 trace：按当前参数压一遍再出文本。

        每次复制都重算一遍（O(n)）。可以缓存，但复制是点一下的事，
        而缓存要跟 tol / max-points / 强制保留三个开关同步失效 —— 不值。
        """
        tol = self.tol_v.get() / 1000.0
        core.set_eps(tr, tol)
        m = None if self.args.no_metrics else emit.run_metrics(tr)
        cand = core.predecimate(tr, tol, max_cand=self._max_cand())
        forced = list(m.forced()) if (m and self.force_metrics.get()) else []
        red = core.reduce_trace(tr, tol, self.mp_v.get() or None, cand, forced,
                                keep_extrema=self.force_extrema.get(),
                                keep_offset=not self.args.no_offset)
        return emit.emit(red, m)

    def _use_trace(self, i):
        """切到第 i 条 trace：候选集、metrics、列选框、视窗全部重来。"""
        if not self.traces:
            return
        self.ti = max(0, min(i, len(self.traces) - 1))
        tr = self.traces[self.ti]
        tol = self.tol_v.get() / 1000.0
        core.set_eps(tr, tol)
        self.metrics = None if self.args.no_metrics else emit.run_metrics(tr)
        self.cand = core.predecimate(tr, tol, max_cand=self._max_cand())
        for w in self.colbox.winfo_children():
            w.destroy()
        self.colbtn = []
        self.cols = [tk.BooleanVar(value=True) for _ in tr.signals]
        for k, s in enumerate(tr.signals):
            cb = tk.Checkbutton(self.colbox, text="c%d %s" % (k + 1, s.name),
                                variable=self.cols[k], bg=BG, fg=COLORS[k % 6],
                                selectcolor=BG, command=self._defer)
            cb.pack(side="left")
            self.colbtn.append(cb)
        self.s_mp.configure(to=max(10, len(self.cand)))
        with self._mute():
            self.mp_v.set(min(len(self.cand), max(50, len(self.cand) // 4)))
        self.band = self.band_key = None
        self._sync_trace_bar()
        self._zoom_all()
        self._recompute()

    def _sync_trace_bar(self):
        """多条 trace 时才显示切换条 —— 单条时它是纯噪音。"""
        for w in self.tracebar.winfo_children():
            w.destroy()
        if len(self.traces) < 2:
            self.tracebar.pack_forget()
            return
        self.tracebar.pack(side="left", padx=(16, 0))
        tk.Label(self.tracebar, text="块", bg=BG, fg=FG).pack(side="left")
        for i, tr in enumerate(self.traces):
            name = tr.signals[0].name if tr.signals else "?"
            tk.Radiobutton(self.tracebar,
                           text="%d/%d %s" % (i + 1, len(self.traces),
                                              name.split("(")[0]),
                           variable=self.ti_v, value=i, bg=BG, fg=FG,
                           selectcolor=BG,
                           command=lambda: self._use_trace(self.ti_v.get())
                           ).pack(side="left")

    def _fill_text(self):
        self.txt.delete("1.0", "end")
        if not self.red:
            return
        if self.view_full.get():
            self.txt.insert("1.0", self.wv_text())
        else:
            blk = emit.metrics_block(self.red, self.metrics)
            if not blk:
                blk = ["[METRICS] （这个 kind 没有注册 metrics 模块）"]
            self.txt.insert("1.0", "\n".join(blk))

    def _copy(self):
        """整份 .wv 直接进剪贴板。它的归宿就是聊天框，中间那三步是多余的。"""
        txt = self.wv_text()
        if not txt:
            self.status.set("还没有可复制的内容 —— 先打开一个 CSV")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(txt)
        self.root.update()               # 让 Tk 真正拿到剪贴板所有权
        n = emit.nbytes(txt)
        b = self.budget()
        tail = ""
        if b and n > b:
            tail = "  ✗ 超预算 %.1f KB" % (b / 1024.0)
        # X11 的剪贴板是「谁复制谁持有」，进程退出内容就没了。
        # 没有剪贴板管理器的隔离机上这一条会咬人，所以每次都提醒。
        self.status.set("已复制 %d 字节（%d 行）到剪贴板%s"
                        "  │  X11 下先粘贴再关窗口，剪贴板归本进程持有"
                        % (n, txt.count("\n"), tail))

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
        self._status()

    def _fit(self):
        """二分 max_points 压到当前预算。和 CLI 里那套是同一个做法。

        固定开销（头部 + METRICS + EVENTS）先量一次，之后只对 SHAPE 行数二分，
        所以中间那十几次不跑 O(n) 的重建自检。
        """
        if not self.red or not self.cand:
            return
        b = self.budget()
        if not b:
            self.status.set("预算设成了不限，没什么可压的")
            return
        # 固定开销要从**不限点数**那一版量，和 CLI 完全一致。
        # 拿「当前这一版」去量的话，头里每列的 err X.XX% 宽度会随点数变，
        # 于是同一个预算从不同起点压会落在不同的点数上（实测差过 55 字节）。
        # 「同一个预算 -> 同一个结果」这条得成立，不然 GUI 里调好的参数
        # 拿到命令行就对不上了。
        r0 = self._reduce(None, check=True)
        fixed = emit.nbytes(emit.emit(r0, self.metrics)) - emit.shape_bytes(r0)
        if fixed >= b:
            self.status.set(
                "压不进去：头部+METRICS+EVENTS 本身就要 %.1f KB > 预算 %.1f KB。"
                "那些是全精度事实，不能为预算牺牲 —— 放宽预算或调大 tol。"
                % (fixed / 1024.0, b / 1024.0))
            return
        lo, hi, best = 2, len(r0.kept), 2
        while lo <= hi:
            mid = (lo + hi) // 2
            r = self._reduce(mid, check=False)
            if fixed + emit.shape_bytes(r) <= b:
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        with self._mute():          # 只是把界面同步到已经二分出来的结果
            self.mp_v.set(best)
        self._recompute(True)
        if self.nbytes > b:                       # 强制点撑住了，压不下去
            self.status.set(self.status.get() + "  ← 强制保留点撑住了下限")

    def _status(self):
        if not self.red:
            return
        w = self.red.worst if self.red.err else None
        tr = self.red.trace
        b = self.budget()
        if b:
            fit = "%.1f KB / %.1f KB %s" % (
                self.nbytes / 1024.0, b / 1024.0,
                "✓" if self.nbytes <= b else "✗ 超预算")
        else:
            fit = "%.1f KB（预算不限）" % (self.nbytes / 1024.0)
        # 输入本身不可信的话，别的读数说得再准也没意义 —— WARN 顶到最前面
        head = ("!! %s" % tr.warns[0].split("。")[0]) if tr.warns else ""
        if not head:
            head = self._ceiling_hint()
        # 留个上限兜底：状态栏会换行，但一条几百字的 WARN 会把它撑成四五行，
        # 底下两个画布跟着跳。截断处的全文就在下面的文本框里。
        if len(head) > 90:
            head = head[:89] + "…"
        self.status.set(
            "%s%d 点  │  输出 %s │  %s  │  RDP %.0f ms  │  %s"
            % (head and head + "  │  ", len(self.red.kept), fit,
               ("max|err| %s (%.2f%%) rms %s  [%s]"
                % (core.eng_str(w.maxerr, w.sig.unit, 3), w.pct,
                   core.eng_str(w.rms, w.sig.unit, 3), w.sig.name))
               if w else "误差计算中…",
               self.ms, os.path.basename(tr.source or "")))

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
        self.traces = self._build_traces(self.raw)
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
        self.tol_lab.set("tol (‰) → 包络" if self.demod_v.get() else "tol (‰)")
        tol = self.tol_v.get() / 1000.0
        for k, s in enumerate(tr.signals):
            if k >= len(self.colbtn):
                break
            pinned = core.NOISE_K * s.noise > tol * (s.rng or 1.0)
            self.colbtn[k].configure(
                text="c%d %s%s" % (k + 1, s.name, " ·噪声底钉住" if pinned else ""))

    def _ceiling_hint(self):
        """滑块拖到头之后**是什么在挡着**。不说的话工具只是「不再变好」。

        真实困惑：「max-points 拖到最大了，波形还是跟原数据差很多」。
        三个天花板叠着，从里到外：预算 -> 候选集上限 -> 波形本身的周期数。
        最外面那个是信息量不是参数，拖滑块永远拖不过去。
        """
        if not self.red or not self.cand:
            return ""
        tr = self.red.trace
        # 候选集塌到个位数：滑块上限会显示成 10（`max(10, len(cand))` 的兜底值），
        # 而 10 是个凭空冒出来的数字，不说的话没人知道它是怎么来的。
        if len(self.cand) <= 10 and len(self.cand) < len(tr.x):
            s = tr.signals[0] if tr.signals else None
            why = "整条几乎是常数" if (s and s.rng <= 0) else "量程被极端点定死"
            return ("!! 候选点只有 %d 个（原始 %d）—— %s，滑块上限的 10 是兜底值"
                    % (len(self.cand), len(tr.x), why))
        cw = emit.carrier_warn(self.red)
        if cw:                                # 信息量不够：最外面那层，先说它
            return "!! " + cw.split("。")[0] + "，缩小视窗或换 --xrange 导一段"
        b = self.budget()
        if b and self.nbytes > 0.98 * b and self.mp_v.get() < len(self.cand):
            return "预算封顶（%.0f KB）" % (b / 1024.0)
        if self.mp_v.get() >= len(self.cand) and len(self.cand) < len(tr.x):
            return ("候选集封顶（%d 点 / 原始 %d）—— 候选集是质量上限，"
                    "命令行可用 --max-cand 开大" % (len(self.cand), len(tr.x)))
        return ""

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
        if self._muted or not self.traces:
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
        if self._muted:
            return
        self._live()

    def _on_tol(self, _=None):
        if self._muted:
            return
        self.cand = None            # tol 变了候选集要重来
        tr = self.traces[self.ti] if self.traces else None
        if tr is not None:
            core.set_eps(tr, self.tol_v.get() / 1000.0)
            self.cand = core.predecimate(tr, self.tol_v.get() / 1000.0,
                                         max_cand=self._max_cand())
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

    def _draw_wave(self):
        c = self.c_wave
        c.delete("all")
        tr = self.red.trace
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50 or h < 50:
            return
        xf = self._xform(c)
        i0, i1 = fit_view(tr.x, self.view[0], self.view[1])
        # 列的基准是**视窗**，不是 fit_view 给的下标区间 —— 后者特意往两边
        # 各多取一个原始点，拿它当基准的话分箱坐标系比绘图坐标系宽两个点
        x0, x1 = self.view
        key = (i0, i1, w, id(tr), x0, x1)
        # `band is None` 也要重算：换视窗的地方都会把 band 置空，但**下标区间可能没变**
        # （视窗缩在相邻两个原始点之间时 fit_view 给的是同一对下标）。
        # 只比 key 的话那一下就会拿着 None 去画。
        if self.band is None or self.band_key != key:
            # 线段总数要恒定：每条信号的包络是 2*ncol 个顶点，
            # 四路信号按 1200 列画就是 9600 段，拖动会开始掉帧。
            ncol = max(120, min(BAND_COLS, w,
                                MAX_SEG // max(1, 2 * len(tr.signals))))
            self.band = bin_envelope(tr.x, [s.y for s in tr.signals], i0, i1,
                                     ncol, x0, x1)
            self.band_key = key
        self._grid(c, xf, w, h)
        ncol = len(self.band[0][0]) if self.band else 0
        for si, s in enumerate(tr.signals):
            lo, hi = self.band[si]
            base, rng, floored = self._yscale(s, lo, hi)
            pts_a, pts_b = [], []
            for cix in range(ncol):
                if lo[cix] is None:
                    continue
                px = xf.l + cix / float(max(1, ncol - 1)) * xf.pw
                pts_a.append((px, xf.sy((lo[cix] - base) / rng)))
                pts_b.append((px, xf.sy((hi[cix] - base) / rng)))
            if len(pts_a) > 1:
                poly = []
                for p in pts_a:
                    poly += [p[0], p[1]]
                for p in reversed(pts_b):
                    poly += [p[0], p[1]]
                c.create_polygon(poly, fill=BAND, outline="", stipple="gray50")
            # reduce 之后的曲线：跑出灰带就是丢了东西。
            # 保留点比像素列还多时**折线画不得** —— 2500 个周期铺在 1100 像素上，
            # 每列一个来回，连出来是一坨实心色块，既看不出形状，也看不出
            # 它比灰带窄了多少（而「窄了多少」正是这一格要回答的问题）。
            # 那就跟灰带用同一种画法：也压成 min/max 带，两条带子直接比宽窄。
            col = COLORS[si % 6]
            # 画的必须是**量化之后**的值，跟误差格同源（见 q_val）
            cs = self.red.specs[si]
            vis = [i for i in self.red.kept if i0 <= i < i1]
            if len(vis) > ncol:
                klo, khi = _kept_band(tr.x, s.y, vis, x0, x1, ncol, cs)
                top, bot = [], []
                for cix in range(ncol):
                    if klo[cix] is None:
                        continue
                    px = xf.l + cix / float(max(1, ncol - 1)) * xf.pw
                    top.append((px, xf.sy((khi[cix] - base) / rng)))
                    bot.append((px, xf.sy((klo[cix] - base) / rng)))
                if len(top) > 1:
                    # 只有一条信号时才填实心。多条信号各填一块的话，
                    # 画在最后那条会把前面全盖掉 —— 六路电流试过一次，
                    # 整个画布是一坨橙色，比原来的折线还糟。
                    # 多条时只画上下两条沿：看得见彼此，也看得见底下的灰带。
                    if len(tr.signals) == 1:
                        poly = []
                        for p in top:
                            poly += [p[0], p[1]]
                        for p in reversed(bot):
                            poly += [p[0], p[1]]
                        c.create_polygon(poly, fill=col, outline=col,
                                         stipple="gray50")
                    for edge in (top, bot):
                        c.create_line([v for p in edge for v in p],
                                      fill=col, width=1)
            else:
                line = []
                for i in vis:
                    line += [xf.sx(tr.x[i]),
                             xf.sy((q_val(cs, s.y[i]) - base) / rng)]
                if len(line) >= 4:
                    c.create_line(line, fill=col, width=1)
            # 图例打的是**纵轴当前的上下限**，不是全局极值 —— 放大之后这两个
            # 差着几个数量级，打错一个就会把局部幅度当成全局幅度读
            c.create_text(xf.l + 6, 12 + si * 13, anchor="w",
                          text="c%d %s  纵轴 [%s..%s] %s%s"
                               % (si + 1, s.name,
                                  core.eng_str(base, s.unit, 4),
                                  core.eng_str(base + rng, s.unit, 4),
                                  "视窗" if self.y_local.get() else "全局",
                                  "·eps 兜底" if floored else ""),
                          fill=COLORS[si % 6], font=("Consolas", 8))
        if self._sel:
            a, b = sorted(self._sel)
            c.create_rectangle(a, xf.t, b, h - xf.b, outline="#1c71d8",
                               dash=(3, 2))
        # +8 是给图例留的余量：字体真实行高比这里的 13 大（缩放屏上更明显），
        # 贴着排会压到最后一条 c<n> 上
        self._draw_events(c, xf, h, top=20 + len(tr.signals) * 13)
        # 提示挪到右上角。原来钉在右下角，正好压在 `_grid` 画的最后一个
        # x 刻度上，两行字叠着，而且长到被画布右边切掉。
        c.create_text(w - 10, 12, anchor="ne", fill="#999",
                      font=("Consolas", 8),
                      text="灰带=原始包络，色带=压缩后；框选/滚轮缩放，"
                           "右键拖平移，双击或 0 复位")

    def _draw_events(self, c, xf, h, top=14):
        """把 `[EVENTS]` 画到波形上。

        EVENTS 是输出里**全精度的时间轴**（glitch 在哪、什么时候 settle、
        极值落在哪一点），但在界面上一条都看不见 —— 人得对着下面的文本
        自己往图上换算。竖线一画，METRICS 和图就接上了。

        `top` 是从哪一行开始摞标签：上面还有每条信号的图例，
        不让开就直接印在图例上（实测四条事件叠在 c1 那行上，两边都读不了）。
        """
        if not (self.ev_v.get() and self.metrics):
            return
        try:
            evs = self.metrics.events()
        except Exception:                           # noqa: BLE001
            return
        lo, hi = self.view
        vis = [e for e in evs if lo <= e.x <= hi]
        # 竖线便宜，标签贵。视窗里事件一多（六路信号各有 EDGE/GLITCH/OUTLIER，
        # 一屏几十个），标签会糊成一片纯色，连底下的波形一起吃掉。
        # 超了就只画竖线，右上角报个数 —— 想看是哪个，放大。
        label = len(vis) <= EV_LABELS
        seen = {}
        rows = max(1, int((h - xf.b - top) // 11) - 1)
        for e in vis:
            px = xf.sx(e.x)
            col = COLORS[_col_index(e.col) % 6]
            c.create_line(px, xf.t, px, h - xf.b, fill=col, dash=(2, 3))
            if not label:
                continue
            # 同一个 x 上挤了好几个事件就往下错开，否则标签叠成一坨
            k = int(px / 60)
            row = seen.get(k, 0)
            seen[k] = row + 1
            if row > rows:                  # 摞到画布底就不摞了
                continue
            c.create_text(px + 3, top + row * 11, anchor="nw",
                          text="%s %s" % (e.col, e.tag), fill=col,
                          font=("Consolas", 7))
        if vis and not label:
            c.create_text(xf.l + 6, top, anchor="nw", fill="#888",
                          font=("Consolas", 8),
                          text="视窗里 %d 个 EVENTS，只画了竖线；"
                               "放大到 %d 个以内才标名字" % (len(vis), EV_LABELS))

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
        if self.low_v.get() == "cycles":
            self._draw_cycles()
            return
        c = self.c_err
        c.delete("all")
        tr = self.red.trace
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50 or h < 50:
            return
        i0, i1 = fit_view(tr.x, self.view[0], self.view[1])
        lim = 3.0
        # 顶上留一条 22 px 的横幅给说明文字。原来它压在数据上，
        # 而这一格恰恰是最密的一格 —— 字和曲线糊在一起，两个都读不了。
        xf = self._xform(c, -lim, lim, pad=(46, 22, 8, 20))
        self._grid(c, xf, w, h)
        y0 = xf.sy(0.0)
        c.create_line(xf.l, y0, w - xf.r, y0, fill="#999")
        ncol = max(60, min(BAND_COLS, int(xf.pw)))
        worst = []
        for si, s in enumerate(tr.signals):
            band = error_band(tr, self.red, si, i0, i1, ncol,
                              self.view[0], self.view[1])
            col = COLORS[si % 6]
            top, bot = [], []
            for cix, v in enumerate(band):
                if v is None:
                    continue
                px = xf.l + cix / float(max(1, ncol - 1)) * xf.pw
                top.append((px, xf.sy(max(-lim, min(lim, v[1])))))
                bot.append((px, xf.sy(max(-lim, min(lim, v[0])))))
            if len(top) > 1:
                if len(tr.signals) == 1:       # 多条就别填实心，理由同上面那格
                    poly = []
                    for p in top:
                        poly += [p[0], p[1]]
                    for p in reversed(bot):
                        poly += [p[0], p[1]]
                    c.create_polygon(poly, fill=col, outline="",
                                     stipple="gray50")
                for edge in (top, bot):
                    c.create_line([v for p in edge for v in p], fill=col,
                                  width=1)
            hits = [v for v in band if v is not None]
            if hits:
                worst.append((si, max(max(abs(v[0]), abs(v[1])) for v in hits)))
        # 容差线画在带子**上面**：它是判据，被数据盖住就等于没有
        for lv in (1.0, -1.0):
            y = xf.sy(lv)
            c.create_line(xf.l, y, w - xf.r, y, fill="#e01b24", dash=(4, 3))
            c.create_text(xf.l - 4, y, anchor="e", fill="#e01b24",
                          font=("Consolas", 8), text="%+g" % lv)
        c.create_text(xf.l, 11, anchor="w", fill=FG, font=("Consolas", 8),
                      text="误差 / 各信号自己的 eps —— 带子=这一列里误差覆盖的范围；"
                           "红虚线 ±1 是容差，冲出去就是被削平了")
        if worst:
            c.create_text(w - 8, 11, anchor="e", font=("Consolas", 8),
                          fill=COLORS[worst[0][0] % 6],
                          text="  ".join("c%d 峰 %.2f" % (i + 1, v)
                                         for i, v in worst[:4]))

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
            app.band = None
            app._redraw()
            root.update_idletasks()
            s0 = app.red.trace.signals[0]
            b_l, r_l, fl = app._yscale(s0, *app.band[0])
            app.y_local.set(False)
            b_g, r_g, _ = app._yscale(s0, *app.band[0])
            app.y_local.set(True)
            app._redraw()
            root.update_idletasks()
            log.append("纵轴@窄视窗 %s: 视窗 %s / 全局 %s (%.1fx%s)"
                       % (core.eng_str(2 * half, tr.xunit, 3),
                          core.eng_str(r_l, s0.unit, 3),
                          core.eng_str(r_g, s0.unit, 3),
                          (r_g / r_l) if r_l else 0.0,
                          "，eps 兜底" if fl else ""))
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
            # 下窗格两种视图
            app.view_full.set(False)
            app._fill_text()
            n_met = len(app.txt.get("1.0", "end").splitlines())
            app.view_full.set(True)
            app._fill_text()
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
