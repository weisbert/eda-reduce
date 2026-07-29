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
        self.err_v = tk.StringVar(value="")
        self.selfcheck_v = tk.StringVar(value="")
        self.hint_v = tk.StringVar(value="")
        self.receipt_v = tk.StringVar(value="")
        self.actual_v = tk.StringVar(value="")
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
        self.lab_bytes.pack(side="left", padx=(8, 8))
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

    def _rail_group(self, parent, title):
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
        head = tk.Label(holder, textvariable=hv, bg=RAIL_BG, fg="#7a4a00",
                        font=("Consolas", 9, "bold"), anchor="w", cursor="hand2")
        head.pack(fill="x", padx=4, pady=1)
        body = tk.Frame(holder, bg=RAIL_BG)
        body.pack(fill="x", padx=6, pady=(0, 3))
        grp = {"holder": holder, "body": body, "var": hv, "title": title,
               "open": True}
        self._rail_groups.append(grp)
        head.bind("<Button-1>", lambda _e, g=grp: self._toggle_group(g))
        return body

    def _toggle_group(self, g, want=None):
        want = (not g["open"]) if want is None else want
        if want == g["open"]:
            return
        g["open"] = want
        g["var"].set(("▾ " if want else "▸ ") + g["title"])
        if want:
            g["body"].pack(fill="x", padx=6, pady=(0, 3))
        else:
            g["body"].pack_forget()

    def _fit_rail(self):
        """装不下就从 ⑥ 往回折。折叠状态由标题上的 ▸ 说明，不是静默隐藏。"""
        if not self._rail_groups:
            return
        avail = self.rail.winfo_height()
        if avail < 50:                       # 还没布局好
            return
        for g in self._rail_groups:          # 先全展开再重新决定
            self._toggle_group(g, True)
        self.root.update_idletasks()
        for g in reversed(self._rail_groups):
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

        g1 = self._rail_group(rail, "① 搬哪几块")
        self.tracebar = tk.Frame(g1, bg=RAIL_BG)
        self.tracebar.pack(fill="x")

        g2 = self._rail_group(rail, "② 搬哪几列")
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
        tk.Label(g4, text="形状点数（保底点另计）", bg=RAIL_BG, fg=FG,
                 font=("Consolas", 9)).pack(anchor="w")
        self.s_mp = tk.Scale(g4, from_=2, to=4000, orient="horizontal",
                             variable=self.mp_v, length=228, bg=RAIL_BG, fg=FG,
                             highlightthickness=0, font=("Consolas", 8),
                             command=self._on_slider)
        self.s_mp.pack(anchor="w")
        tk.Label(g4, textvariable=self.actual_v, bg=RAIL_BG, fg="#7a5c00",
                 font=("Consolas", 8), anchor="w",
                 justify="left", wraplength=230).pack(fill="x")
        self.tol_lab = tk.StringVar(value="存储精度 tol (‰)")
        tk.Label(g4, textvariable=self.tol_lab, bg=RAIL_BG, fg=FG,
                 font=("Consolas", 9)).pack(anchor="w", pady=(4, 0))
        self.s_tol = tk.Scale(g4, from_=0.1, to=100.0, resolution=0.1,
                              orient="horizontal", variable=self.tol_v,
                              length=228, bg=RAIL_BG, fg=FG, font=("Consolas", 8),
                              highlightthickness=0, command=self._on_tol)
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

        g5 = self._rail_group(rail, "⑤ 派生（会改块数）")
        tk.Checkbutton(g5, text="解调：包络+频率", variable=self.demod_v,
                       bg=RAIL_BG, fg=FG, selectcolor=RAIL_BG,
                       font=("Consolas", 9),
                       command=self._remode).pack(anchor="w")
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
                       bg=RAIL_BG, fg=FG, selectcolor=RAIL_BG,
                       font=("Consolas", 9),
                       command=self._defer).pack(anchor="w")
        tk.Checkbutton(g6, textvariable=self.fm_v, variable=self.force_metrics,
                       bg=RAIL_BG, fg=FG, selectcolor=RAIL_BG,
                       font=("Consolas", 9),
                       command=self._defer).pack(anchor="w")
        # 诚实说明：forced 是一组扁平的 x 下标，没有信号归属信息
        # （metrics 层的 `_forced` 就是个 set），所以取消某一列不会减少它们。
        tk.Label(g6, text="保底点按整条记 —— 取消某一列不会减少它们",
                 bg=RAIL_BG, fg="#888", font=("Consolas", 8),
                 wraplength=230, justify="left").pack(anchor="w")

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
        tk.Checkbutton(vb, text="纵轴跟视窗", variable=self.y_local, bg=VIEW_BG,
                       fg=FG, selectcolor=VIEW_BG, font=("Consolas", 9),
                       command=self._redraw).pack(side="left", padx=(10, 0))
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
        tk.Label(vb, text="│ 文本框:", bg=VIEW_BG, fg="#4a5560",
                 font=("Consolas", 9)).pack(side="left", padx=(8, 0))
        tk.Radiobutton(vb, text="METRICS", variable=self.view_full, value=False,
                       bg=VIEW_BG, fg=FG, selectcolor=VIEW_BG,
                       font=("Consolas", 9),
                       command=self._fill_text).pack(side="left")
        tk.Radiobutton(vb, text="完整 .wv", variable=self.view_full, value=True,
                       bg=VIEW_BG, fg=FG, selectcolor=VIEW_BG,
                       font=("Consolas", 9),
                       command=self._fill_text).pack(side="left")

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
        r.minsize(900, 600)

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
        self.c_err = tk.Canvas(right, bg=BG, height=180, highlightthickness=1,
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
        # 各分一半，而这个窗口的主诉是「图看不清」。文本要读全文可以拖大窗口，
        # 或者用「完整 .wv」那个单选 —— 它默认只占 6 行。
        tf = tk.Frame(r, bg=BG)
        tf.pack(fill="x", padx=8, pady=(2, 8))
        sb = tk.Scrollbar(tf)
        sb.pack(side="right", fill="y")
        self.txt = tk.Text(tf, height=6, bg="#fafafa", fg=FG,
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
        self.s_mp.configure(to=max(10, len(b.cand)))
        with self._mute():
            self.mp_v.set(b.max_points)
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
        cur = self.blk
        if cur is not None and cur.red is not None:
            with self._mute():
                self.mp_v.set(min(len(cur.cand), len(cur.red.kept)))
            cur.max_points = self.mp_v.get()
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
        st = "normal" if self.demod_v.get() else "disabled"
        for w in (self.sp_rep, self.sp_fspan):
            try:
                w.configure(state=st)
            except tk.TclError:                     # pragma: no cover
                pass

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
        cur = self.blk
        with self._mute():                      # 界面同步到算出来的结果
            self.mp_v.set(min(len(cur.cand), len(cur.red.kept)))
        cur.max_points = self.mp_v.get()
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
        if v.budget:
            self.bytes_v.set(
                "总计 %.1f KB / 预算 %.0f KB%s%s"
                % (v.nbytes / 1024.0, v.budget / 1024.0,
                   ("  超 %.1f×" % v.over()) if v.over() else "  装得下", nblk))
        else:
            self.bytes_v.set("总计 %.1f KB（预算不限）%s"
                             % (v.nbytes / 1024.0, nblk))
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
        self.view_full.set(True)
        self._fill_text()
        self.txt.insert("1.0", "".join(
            "!! %s\n\n" % w for w in self.ship.warns()))

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
