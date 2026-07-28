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
import os
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


def bin_envelope(x, ys, i0, i1, ncol):
    """把 [i0,i1) 的原始点按列压成 min/max 包络。O(区间长度)。

    1e7 个点画不了，也没必要画：一列像素里那几千个点，眼睛能看见的只有
    上下沿。压成包络之后画的是「原始数据真实覆盖的区域」，
    红线跑出灰带就是丢了东西 —— 这个判据只有包络能给，抽样给不了。
    """
    n = i1 - i0
    if n <= 0:
        return []
    x0, x1 = x[i0], x[i1 - 1]
    span = x1 - x0
    out = []
    for y in ys:
        lo = [None] * ncol
        hi = [None] * ncol
        for i in range(i0, i1):
            c = 0 if span <= 0 else int((x[i] - x0) / span * (ncol - 1))
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
        # 纵轴默认跟视窗：放大就是为了看细节，按全局量程画的话横轴放大了、
        # 纵向还是一条压平的直线，等于没放大
        self.y_local = tk.BooleanVar(value=True)
        self.status = tk.StringVar(value="载入中…")
        self.prog = tk.DoubleVar(value=0.0)
        self.tol_v = tk.DoubleVar(value=(args.tol or core.DEFAULT_TOL) * 1000.0)
        self.mp_v = tk.IntVar(value=0)
        b0 = args.budget if args.budget else 0
        self.budget_v = tk.StringVar(value=("%g" % (b0 / 1024.0)) if b0 else "0")
        self.force_extrema = tk.BooleanVar(value=True)
        self.force_metrics = tk.BooleanVar(value=True)
        self.cols = []
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
        r.title("wave_reduce — 预览")
        r.configure(bg=BG)
        r.geometry("1180x820")

        top = tk.Frame(r, bg=BG)
        top.pack(fill="x", padx=8, pady=(8, 2))
        tk.Label(top, textvariable=self.status, bg=BG, fg=FG,
                 font=("Consolas", 10)).pack(side="left")
        self.pb = ttk.Progressbar(top, variable=self.prog, maximum=1.0,
                                  length=200)
        self.pb.pack(side="right")

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
        tk.Label(ctl, text="tol (‰)", bg=BG, fg=FG).grid(row=0, column=2)
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
                       command=self._recompute).pack(anchor="w")
        tk.Checkbutton(fb, text="强制保留 spur/事件", variable=self.force_metrics,
                       bg=BG, fg=FG, selectcolor=BG,
                       command=self._recompute).pack(anchor="w")
        tk.Checkbutton(fb, text="纵轴跟视窗", variable=self.y_local,
                       bg=BG, fg=FG, selectcolor=BG,
                       command=self._redraw).pack(anchor="w")

        # 下窗格的工具条。**复制到剪贴板**是这里最重要的一个按钮：
        # .wv 的归宿就是粘进聊天框，「先另存成文件、再打开、再全选、再复制」
        # 中间三步纯属多余。
        tb = tk.Frame(r, bg=BG)
        tb.pack(fill="x", padx=8, pady=(4, 0))
        tk.Button(tb, text="打开 CSV…", command=self._open).pack(side="left")
        tk.Button(tb, text="复制全文到剪贴板", command=self._copy).pack(
            side="left", padx=(8, 2))
        tk.Button(tb, text="另存 .wv…", command=self._save).pack(side="left")
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
        r.bind("<Control-c>", lambda _: self._copy())
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
        self.prog.set(0.0)
        q = []

        def work():
            try:
                trs = core.parse_csv(path, layout=self.args.layout,
                                     xcols=self.args.xcols)
                for tr in trs:
                    if getattr(self.args, "xrange", None):
                        core.slice_trace(tr, *self.args.xrange)
                    core.analyze(tr, kind=self.args.kind, xscale=self.args.xscale)
                tr = trs[0]
                m = None
                if not self.args.no_metrics:
                    m = emit.run_metrics(tr)
                tol = self.args.tol or (m.suggest_tol() if m else None) \
                    or core.DEFAULT_TOL
                core.set_eps(tr, tol)
                cand = core.predecimate(tr, tol, max_cand=self._max_cand(),
                                       progress=lambda f: q.append(f))
                q.append(("done", trs, m, cand, tol))
            except Exception as exc:                    # noqa: BLE001
                q.append(("err", exc))

        threading.Thread(target=work, daemon=True).start()
        self._poll(q)

    def _poll(self, q):
        while q:
            it = q.pop(0)
            if isinstance(it, float):
                self.prog.set(it)
                continue
            if it[0] == "err":
                # 状态栏只有一行，解析失败的诊断是多行的 —— 全文进文本框，
                # 否则「切错列」这种一眼能定位的问题只剩一句 IndexError
                msg = "%s: %s" % (type(it[1]).__name__, it[1])
                self.status.set("载入失败: " + msg.splitlines()[0])
                self.txt.delete("1.0", "end")
                self.txt.insert("1.0", "载入失败\n\n" + msg)
                self.prog.set(0.0)
                return
            _, trs, m, cand, tol = it
            self.traces, self.metrics, self.cand = trs, m, cand
            self.tol_v.set(tol * 1000.0)
            tr = trs[0]
            self.s_mp.configure(to=max(10, len(cand)))
            self.mp_v.set(min(len(cand), max(50, len(cand) // 4)))
            self.cols = [tk.BooleanVar(value=True) for _ in tr.signals]
            for i, s in enumerate(tr.signals):
                tk.Checkbutton(self.colbox, text="c%d %s" % (i + 1, s.name),
                               variable=self.cols[i], bg=BG, fg=COLORS[i % 6],
                               selectcolor=BG,
                               command=self._recompute).pack(side="left")
            self._zoom_all()
            self._recompute()
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
        forced = []
        if self.force_metrics.get() and self.metrics:
            names = set(id(s) for s in tr.signals)
            if all(id(s) in names for s in self.traces[self.ti].signals):
                forced = list(self.metrics.forced())
        return core.reduce_trace(tr, tol, max_points, self.cand, forced,
                                 keep_extrema=self.force_extrema.get(),
                                 check=check)

    def _recompute(self, precise=True):
        if not self.traces:
            return
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
        self._status()
        self._redraw()

    def wv_text(self):
        """当前参数下的完整 .wv —— 就是要粘进聊天框的那份东西。"""
        if not self.red:
            return ""
        return emit.emit(self.red, self.metrics)

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
        self.status.set(
            "%s%d 点  │  输出 %s │  %s  │  RDP %.0f ms  │  %s"
            % (head and head + "  │  ", len(self.red.kept), fit,
               ("max|err| %s (%.2f%%) rms %s  [%s]"
                % (core.eng_str(w.maxerr, w.sig.unit, 3), w.pct,
                   core.eng_str(w.rms, w.sig.unit, 3), w.sig.name))
               if w else "误差计算中…",
               self.ms, os.path.basename(tr.source or "")))
        self.prog.set(min(1.0, self.nbytes / float(b)) if b else 0.0)

    def _max_cand(self):
        return getattr(self.args, "max_cand", None) or core.MAX_CAND

    def _ceiling_hint(self):
        """滑块拖到头之后**是什么在挡着**。不说的话工具只是「不再变好」。

        真实困惑：「max-points 拖到最大了，波形还是跟原数据差很多」。
        三个天花板叠着，从里到外：预算 -> 候选集上限 -> 波形本身的周期数。
        最外面那个是信息量不是参数，拖滑块永远拖不过去。
        """
        if not self.red or not self.cand:
            return ""
        tr = self.red.trace
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

    def _on_slider(self, _=None):
        self._live()

    def _on_tol(self, _=None):
        self.cand = None            # tol 变了候选集要重来
        tr = self.traces[self.ti] if self.traces else None
        if tr is not None:
            core.set_eps(tr, self.tol_v.get() / 1000.0)
            self.cand = core.predecimate(tr, self.tol_v.get() / 1000.0,
                                         max_cand=self._max_cand())
        self._live()

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
        if x0 < lo:                          # 平移撞边就整体推回来，不把视窗压扁
            x1, x0 = x1 + (lo - x0), lo
        if x1 > hi:
            x0, x1 = x0 - (x1 - hi), hi
        x0, x1 = max(lo, x0), min(hi, x1)
        if x1 <= x0:
            return False
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

    def _xform(self, canvas, y0=0.0, y1=1.0):
        tr = self.traces[self.ti]
        return Xform(self.view[0], self.view[1], y0, y1,
                     canvas.winfo_width(), canvas.winfo_height(),
                     logx=(tr.xscale == "log"))

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
        key = (i0, i1, w, id(tr))
        # `band is None` 也要重算：换视窗的地方都会把 band 置空，但**下标区间可能没变**
        # （视窗缩在相邻两个原始点之间时 fit_view 给的是同一对下标）。
        # 只比 key 的话那一下就会拿着 None 去画。
        if self.band is None or self.band_key != key:
            # 线段总数要恒定：每条信号的包络是 2*ncol 个顶点，
            # 四路信号按 1200 列画就是 9600 段，拖动会开始掉帧。
            ncol = max(120, min(BAND_COLS, w,
                                MAX_SEG // max(1, 2 * len(tr.signals))))
            self.band = bin_envelope(tr.x, [s.y for s in tr.signals], i0, i1,
                                     ncol)
            self.band_key = key
        self._grid(c, xf, w, h)
        ncol = len(self.band[0][0]) if self.band else 0
        x0, x1 = tr.x[i0], tr.x[i1 - 1]
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
            # reduce 之后的曲线：跑出灰带就是丢了东西
            line = []
            for i in self.red.kept:
                if i < i0 or i >= i1:
                    continue
                line += [xf.sx(tr.x[i]), xf.sy((s.y[i] - base) / rng)]
            if len(line) >= 4:
                c.create_line(line, fill=COLORS[si % 6], width=1)
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
        c.create_text(w - 8, h - 8, anchor="se", fill="#888",
                      font=("Consolas", 8),
                      text="灰带=原始 min/max 包络；框选或滚轮缩放，"
                           "右键拖平移，+/- ←/→，双击或 0 复位")

    def _draw_err(self):
        c = self.c_err
        c.delete("all")
        tr = self.red.trace
        w, h = c.winfo_width(), c.winfo_height()
        if w < 50 or h < 50:
            return
        i0, i1 = fit_view(tr.x, self.view[0], self.view[1])
        lim = 3.0
        xf = self._xform(c, -lim, lim)
        self._grid(c, xf, w, h)
        for lv, col in ((1.0, "#e01b24"), (-1.0, "#e01b24")):
            y = xf.sy(lv)
            c.create_line(xf.l, y, w - xf.r, y, fill=col, dash=(4, 3))
        y0 = xf.sy(0.0)
        c.create_line(xf.l, y0, w - xf.r, y0, fill="#999")
        for si, s in enumerate(tr.signals):
            pts = error_curve(tr, self.red, si, i0, i1, ERR_PTS)
            line = []
            for xv, e in pts:
                line += [xf.sx(xv), xf.sy(max(-lim, min(lim, e)))]
            if len(line) >= 4:
                c.create_line(line, fill=COLORS[si % 6], width=1)
        c.create_text(xf.l + 6, 12, anchor="w", fill=FG, font=("Consolas", 8),
                      text="误差 / 各信号自己的 eps —— 红虚线 ±1 就是容差；"
                           "曲线贴住 ±1 说明那一段刚好用满容差，冲出去就是被削平了")

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
