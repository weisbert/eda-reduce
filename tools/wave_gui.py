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
        self.status = tk.StringVar(value="载入中…")
        self.prog = tk.DoubleVar(value=0.0)
        self.tol_v = tk.DoubleVar(value=(args.tol or core.DEFAULT_TOL) * 1000.0)
        self.mp_v = tk.IntVar(value=0)
        self.force_extrema = tk.BooleanVar(value=True)
        self.force_metrics = tk.BooleanVar(value=True)
        self.cols = []
        self._build()
        self._load_async()

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
        self.colbox = tk.Frame(ctl, bg=BG)
        self.colbox.grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 0))
        fb = tk.Frame(ctl, bg=BG)
        fb.grid(row=0, column=4, rowspan=2, padx=12)
        tk.Checkbutton(fb, text="强制保留极值", variable=self.force_extrema,
                       bg=BG, fg=FG, selectcolor=BG,
                       command=self._recompute).pack(anchor="w")
        tk.Checkbutton(fb, text="强制保留 spur/事件", variable=self.force_metrics,
                       bg=BG, fg=FG, selectcolor=BG,
                       command=self._recompute).pack(anchor="w")
        tk.Button(fb, text="另存 .wv…", command=self._save).pack(anchor="w",
                                                                pady=(4, 0))

        self.txt = tk.Text(r, height=9, bg="#fafafa", fg=FG,
                           font=("Consolas", 9), wrap="none")
        self.txt.pack(fill="both", padx=8, pady=(2, 8))

        self.c_wave.bind("<Configure>", lambda e: self._redraw())
        self.c_err.bind("<Configure>", lambda e: self._redraw())
        self.c_wave.bind("<ButtonPress-1>", self._sel_start)
        self.c_wave.bind("<B1-Motion>", self._sel_move)
        self.c_wave.bind("<ButtonRelease-1>", self._sel_end)
        self.c_wave.bind("<Double-Button-1>", lambda e: self._zoom_all())

    # ------------------------------------------------------------ 载入

    def _load_async(self):
        """解析 + 预细化放后台线程。1e7 点解析要几十秒，不能冻界面。"""
        q = []

        def work():
            try:
                trs = core.parse_csv(self.path, layout=self.args.layout,
                                     xcols=self.args.xcols)
                for tr in trs:
                    core.analyze(tr, kind=self.args.kind, xscale=self.args.xscale)
                tr = trs[0]
                m = None
                if not self.args.no_metrics:
                    m = emit.run_metrics(tr)
                tol = self.args.tol or (m.suggest_tol() if m else None) \
                    or core.DEFAULT_TOL
                core.set_eps(tr, tol)
                cand = core.predecimate(tr, tol, progress=lambda f: q.append(f))
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
                self.status.set("载入失败: %s" % it[1])
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

    def _recompute(self, precise=True):
        if not self.traces:
            return
        tr = self._sub_trace()
        tol = self.tol_v.get() / 1000.0
        core.set_eps(tr, tol)
        forced = []
        if self.force_metrics.get() and self.metrics:
            names = set(id(s) for s in tr.signals)
            if all(id(s) in names for s in self.traces[self.ti].signals):
                forced = list(self.metrics.forced())
        t0 = time.time()
        self.red = core.reduce_trace(tr, tol, self.mp_v.get(), self.cand,
                                     forced,
                                     keep_extrema=self.force_extrema.get(),
                                     check=precise)
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

    def _fill_text(self):
        self.txt.delete("1.0", "end")
        blk = emit.metrics_block(self.red, self.metrics)
        if not blk:
            blk = ["[METRICS] （这个 kind 没有注册 metrics 模块）"]
        self.txt.insert("1.0", "\n".join(blk))

    def _status(self):
        w = self.red.worst if self.red.err else None
        tr = self.red.trace
        budget = self.args.budget or 20480
        self.status.set(
            "%d 点  │  输出 %.1f KB / %d KB %s │  %s  │  RDP %.0f ms  │  %s"
            % (len(self.red.kept), self.nbytes / 1024.0, budget // 1024,
               "✓" if self.nbytes <= budget else "✗ 超预算",
               ("max|err| %s (%.2f%%) rms %s  [%s]"
                % (core.eng_str(w.maxerr, w.sig.unit, 3), w.pct,
                   core.eng_str(w.rms, w.sig.unit, 3), w.sig.name))
               if w else "误差计算中…",
               self.ms, os.path.basename(tr.source or "")))
        self.prog.set(min(1.0, self.nbytes / float(budget)))

    # ------------------------------------------------------------ 交互

    def _on_slider(self, _=None):
        self._live()

    def _on_tol(self, _=None):
        self.cand = None            # tol 变了候选集要重来
        tr = self.traces[self.ti] if self.traces else None
        if tr is not None:
            core.set_eps(tr, self.tol_v.get() / 1000.0)
            self.cand = core.predecimate(tr, self.tol_v.get() / 1000.0)
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
        tr = self.traces[self.ti]
        self.view = (tr.x[0], tr.x[-1])
        self.band = None
        self._redraw()

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
        if self.band_key != key:
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
            rng = s.rng or 1.0
            base = s.vmin
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
            c.create_text(xf.l + 6, 12 + si * 13, anchor="w",
                          text="c%d %s  [%s..%s]"
                               % (si + 1, s.name,
                                  core.eng_str(s.vmin, s.unit, 4),
                                  core.eng_str(s.vmax, s.unit, 4)),
                          fill=COLORS[si % 6], font=("Consolas", 8))
        if self._sel:
            a, b = sorted(self._sel)
            c.create_rectangle(a, xf.t, b, h - xf.b, outline="#1c71d8",
                               dash=(3, 2))
        c.create_text(w - 8, h - 8, anchor="se", fill="#888",
                      font=("Consolas", 8),
                      text="灰带=原始 min/max 包络（各信号按自身量程归一化）；"
                           "框选缩放，双击复位")

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
        p = filedialog.asksaveasfilename(defaultextension=".wv",
                                         filetypes=[("wave reduce", "*.wv")])
        if not p:
            return
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(emit.emit(self.red, self.metrics))
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
            log.append("状态栏: " + app.status.get())
            log.append("METRICS 窗格 %d 行"
                       % len(app.txt.get("1.0", "end").splitlines()))
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
    if not path:
        root = tk.Tk()
        root.withdraw()
        path = filedialog.askopenfilename(
            filetypes=[("CSV", "*.csv"), ("all", "*.*")])
        root.destroy()
        if not path:
            return 1
    if getattr(args, "selftest", False):
        return selftest(path, args)
    root = tk.Tk()
    WaveGui(root, path, args)
    root.mainloop()
    return 0
