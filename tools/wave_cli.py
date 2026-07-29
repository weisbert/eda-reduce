#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wave_cli.py — `wave_reduce` 的命令行。CSV -> `.wv`。

核心动作是**精确压到预算**而不是猜：先不限点数压一遍，量出真实字节数，
不够就二分点数预算，直到贴着 20 KB 的天花板。上行通道是聊天框，
20 KB 是硬约束不是审美。

依赖：**纯标准库，硬性**。
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import wave_core as core                                        # noqa: E402
import wave_emit as emit                                        # noqa: E402

DEFAULT_BUDGET = 20 * 1024


def default_budget():
    """20 KB 是**这条通道**的宽度，不是普适真理。

    换个通道（能贴附件、字数上限不同、换个模型）这个数就该跟着变，
    所以既能用 --budget 临时改，也能用环境变量定成你自己的常态。
    """
    v = os.environ.get("EDA_REDUCE_BUDGET")
    if not v:
        return DEFAULT_BUDGET
    try:
        n = int(float(v.lower().rstrip("bk").strip()) *
                (1024 if v.strip().lower().endswith(("k", "kb")) else 1))
        return max(0, n)
    except ValueError:
        sys.stderr.write("EDA_REDUCE_BUDGET=%r 看不懂，用默认 %d\n"
                         % (v, DEFAULT_BUDGET))
        return DEFAULT_BUDGET


def _apply_units(tr, decls):
    """--unit c1=V,x=s：单位由人声明，脚本不猜。"""
    if not decls:
        return
    for item in decls:
        for part in item.split(","):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k, v = k.strip(), v.strip()
            if k in ("x", "t", tr.xname):
                tr.xunit, tr.xunit_src = v, "declared"
                continue
            for i, s in enumerate(tr.signals):
                if k in ("c%d" % (i + 1), s.name):
                    s.unit, s.unit_src = v, "declared"


def fit_budget(tr, tol, budget, m, forced, cand, max_points=None,
               keep_offset=True, notes=None, keep_extrema=True):
    """压到 <= budget 字节。返回 (reduction, text)。

    先量一次不限点数的结果，拿到「头部+METRICS+EVENTS」这块固定开销，
    再对 SHAPE 的行数二分——这样只有首尾两次跑完整的 O(n) 重建自检。

    `keep_extrema` 要**透传到每一次** reduce_trace：命令行没有对应的 flag、
    默认恒为 True，所以以前漏掉它看不出问题；但 GUI 上它是个勾选框，
    一旦 GUI 改走这条路，漏传就等于那个勾选框静默失效。
    """
    red = core.reduce_trace(tr, tol, max_points, cand, forced,
                            keep_offset=keep_offset, keep_extrema=keep_extrema)
    txt = emit.emit(red, m, notes)
    if budget is None or emit.nbytes(txt) <= budget:
        return red, txt
    fixed = emit.nbytes(txt) - emit.shape_bytes(red)
    lo, hi = 2, len(red.kept)
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        r = core.reduce_trace(tr, tol, mid, red.cand, forced,
                              keep_offset=keep_offset, check=False,
                              keep_extrema=keep_extrema)
        if fixed + emit.shape_bytes(r) <= budget:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if best is None:
        best = 2
    for _ in range(12):                      # 收尾核一次真字节数，超了再收一点
        red = core.reduce_trace(tr, tol, best, red.cand, forced,
                                keep_offset=keep_offset,
                                keep_extrema=keep_extrema)
        txt = emit.emit(red, m, notes)
        if emit.nbytes(txt) <= budget or best <= 2:
            break
        best = max(2, int(best * 0.9))
    n = emit.nbytes(txt)
    if n > budget:
        # 压不进去就**说出来**。强制保留点（spur 峰及其包络、极值、事件）
        # 是不许为了预算牺牲的 —— 牺牲了就等于回到 54x 低报那个坑里。
        red = core.reduce_trace(tr, tol, best, red.cand, forced,
                                keep_offset=keep_offset,
                                keep_extrema=keep_extrema)
        txt = emit.emit(red, m, (notes or []) + [
            "**超预算**：%d > %d 字节。强制保留点有 %d 个（spur/极值/事件，"
            "不许为预算牺牲），已经压到 %d 点还是下不来。"
            "要更小就调 --tol 或者分段导出。"
            % (n, budget, len(red.forced), len(red.kept))])
    return red, txt


def _why_tol(tr, tol):
    r = max((s.rng for s in tr.signals), default=0.0)
    u = tr.signals[0].unit if tr.signals else ""
    return "相当于 %s 的容差" % core.eng_str(tol * r, u, 3)


def _demod(tr, args):
    """--demod：把载波解调掉。-> [trace, ...]（不生效就返回 [原 trace]）。

    解调必须在 analyze **之后**（要 vmin/vmax/噪声底才切得出周期），
    在 reduce **之前**（后面整条管道都跑在派生信号上）。
    """
    try:
        import wave_demod
    except ImportError:                          # 逃生舱模式：只有三个文件时
        raise SystemExit("--demod 需要 tools/wave_demod.py，这份部署里没有")
    return wave_demod.apply(tr, args.tol or core.DEFAULT_TOL, args.budget,
                            args.demod_cycles, args.demod_min,
                            args.kind, args.xscale,
                            getattr(args, "demod_fspan", 0))


def prepare_traces(path, args):
    """解析 -> 单位 -> 切窗口 -> analyze -> 解调。-> [trace, ...]

    预处理（切窗口 / 解调）**会改变 trace 的条数**，所以预算要等条数
    定下来才摊得了 —— 这也是为什么它单独一步。
    """
    traces = core.parse_csv(path, layout=args.layout, xcols=args.xcols)
    if not traces:
        raise ValueError("没解析出任何 trace: %s" % path)
    prepared = []
    for tr in traces:
        _apply_units(tr, args.unit)
        if args.xrange:
            # 切窗口要在 analyze 之前：极值/噪声底/周期数都得是窗口内的，
            # 拿整条的极值去定窗口内的容差会差出量级
            core.slice_trace(tr, *args.xrange)
        core.analyze(tr, kind=args.kind, xscale=args.xscale)
        prepared.extend(_demod(tr, args) if args.demod else [tr])
    return prepared


class Block(object):
    """`.wv` 里的一「块」：一条 trace，加上**它自己那一整份**压缩状态。

    为什么要有这个类。GUI 原来把「当前这条 trace」的状态摊在 self 上
    （cand / metrics / red / cols / mp_v），切一下块全被冲掉；而非当前块
    是在复制的那一刻拿**当前块的参数**临时压一遍出去的（旧 `_emit_other`）。
    于是屏幕上那个字节数和真正会粘出去的东西对不上 —— 解调出两块时，
    状态栏读的是第一块，剪贴板里是两块。状态归块，这条就不成立了。

    每块自己的东西只有三样：`cols`（搬哪几列）、`max_points`、`included`
    （搬不搬这块）。tol 和预算是整份货的，不是块的 —— 拆开就需要
    「同步到所有块」这种二级概念。
    """

    def __init__(self, trace, args):
        self.trace = trace
        self.args = args
        self.metrics = None
        self.suggested_tol = None
        self.tol = None                  # 这一轮实际用的（compute 里定）
        self.cand = None
        self.cols = None                 # None = 整条都要；否则一组 bool
        self.max_points = getattr(args, "max_points", None)
        self.included = True
        # 命令行没有对应的 flag（保底点永远留），但 GUI 上是个勾选框。
        # 放在这里而不是让 GUI 自己旁路一条路 —— 旁路就是上一版
        # 「GUI 和命令行给不同答案」的来源。
        self.use_forced = True
        self.red = None
        self.text = ""
        self.notes = []
        self._prepared = False
        self._cand_tol = None
        self._dirty = True
        # 缓存里那份文本是不是**带 `# recon:`** 的。`check=False` 省掉 O(n)
        # 自检，代价是emit 出来的文本少一行 —— 少的正好是诚实闸门那一行。
        # 所以「缓存够不够用」不只看脏不脏，还要看要的精度够不够。
        self._checked = False

    # -------------------------------------------------- 只跟数据有关的一次性活

    def prepare(self):
        """metrics 和建议 tol。跟 tol / 预算都无关，所以只算一次。"""
        if self._prepared:
            return
        m = None
        if not self.args.no_metrics:
            m = emit.run_metrics(self.trace)
        self.metrics = m
        self.suggested_tol = m.suggest_tol() if m else None
        self._prepared = True

    def touch(self):
        """这块的参数变了，下次 compute 要真算。"""
        self._dirty = True

    def dirty(self):
        return self._dirty or self.red is None

    # -------------------------------------------------- 压

    def _effective_tol(self, override):
        """-> (tol, 要不要为此写一条 note)。

        口径和命令行完全一致：`--tol` 优先；没给才用 metrics 的建议值，
        **而且用了要说**；建议值也没有才退回 DEFAULT_TOL。
        """
        if override is not None:
            return override, None
        if self.suggested_tol:
            return self.suggested_tol, (
                "tol 用了 kind=%s 建议的 %g（没给 --tol）；%s"
                % (self.trace.kind, self.suggested_tol,
                   _why_tol(self.trace, self.suggested_tol)))
        return core.DEFAULT_TOL, None

    def ensure_cand(self, tol_override):
        """只把候选集备好（不做 RDP）。界面要拿它定滑块量程和默认点数。"""
        self.prepare()
        tol, _ = self._effective_tol(tol_override)
        core.set_eps(self.trace, tol)
        if self.cand is None or self._cand_tol != tol:
            self.cand = core.predecimate(self.trace, tol,
                                         max_cand=self.args.max_cand)
            self._cand_tol = tol
        return self.cand

    def sub_trace(self):
        """按 `cols` 做一个浅拷贝（数组共享，不复制数据）。"""
        tr = self.trace
        if not self.cols:
            return tr
        keep = [s for s, on in zip(tr.signals, self.cols) if on]
        if not keep or len(keep) == len(tr.signals):
            return tr
        sub = core.Trace(tr.xname, tr.index)
        for a in ("source", "xunit", "xunit_src", "x", "kind", "kind_src",
                  "xscale", "notes", "dt_med", "dt_min", "dt_max"):
            setattr(sub, a, getattr(tr, a))
        sub.signals = keep
        return sub

    def compute(self, per_budget, tol_override, keep_extrema=True, check=True,
                fit=True):
        """压一遍，填 self.red / self.text。

        `fit=True` 走 `fit_budget`，也就是**压到预算**（命令行永远这样）。
        `fit=False` 只按 `max_points` 压一次，超预算就超着 —— GUI 拖滑块时
        要的是这个：拖到超预算是**探索的一部分**（「多给点数能好多少」），
        顶回来的话滑块过了某一格就没反应，人只会以为坏了。
        超没超由出口台去说。
        """
        self.prepare()
        tol, tol_note = self._effective_tol(tol_override)
        self.tol = tol
        notes = [tol_note] if tol_note else []
        core.set_eps(self.trace, tol)
        if self.cand is None or self._cand_tol != tol:
            self.cand = core.predecimate(self.trace, tol,
                                         max_cand=self.args.max_cand)
            self._cand_tol = tol
        m = self.metrics
        forced = list(m.forced()) if (m and self.use_forced) else []
        if m is None and not self.args.no_metrics:
            notes.append("kind=%s 没有注册的 metrics 模块，本文件只有 SHAPE 段"
                         "（已注册: %s）"
                         % (self.trace.kind, ", ".join(emit.registered()) or "无"))
        self.notes = notes
        sub = self.sub_trace()
        if fit:
            self.red, self.text = fit_budget(
                sub, tol, per_budget, m, forced, self.cand,
                self.max_points, not self.args.no_offset, notes, keep_extrema)
        else:
            self.red = core.reduce_trace(
                sub, tol, self.max_points, self.cand, forced,
                keep_extrema=keep_extrema,
                keep_offset=not self.args.no_offset, check=check)
            self.text = emit.emit(self.red, m, notes)
        self._dirty = False
        self._checked = bool(check) or fit
        return self.red

    def nbytes(self):
        return emit.nbytes(self.text) if self.text else 0

    def label(self):
        s = self.trace.signals[0].name if self.trace.signals else "?"
        return s.split("(")[0].strip()


class Shipment(object):
    """要粘出去的**那一整份**：若干块 + 预算 + tol。

    「合计多少字节」「最差误差多少」这两个判据只有在这一层才问得出来。
    原来 GUI 是按当前块回答的，命令行是按全部块回答的 —— 同一份数据
    同一组参数，两边给不同的答案，而用户没法知道该信哪个。
    """

    def __init__(self, blocks, args, keep_extrema=True):
        self.blocks = blocks
        self.args = args
        self.tol_override = args.tol      # None = 各块用各自的建议值
        self.budget = args.budget or 0
        self.keep_extrema = keep_extrema

    # -------------------------------------------------- 选择

    def included(self):
        return [b for b in self.blocks if b.included]

    def per_block_budget(self):
        """预算怎么摊 —— **只此一处**。

        原来 GUI 自己写了一条二分、命令行写了另一条，两条对同一份数据
        给不同的点数。摊法是「按块均分」：不完美（各块的固定开销不一样，
        env 有两列、f_inst 只有一列），但必须和命令行是同一个不完美，
        不然屏幕和产物又要对不上。顶穿了由界面去说，不在这里偷偷改算法。
        """
        inc = self.included()
        return (self.budget // len(inc)) if (self.budget and inc) else None

    # -------------------------------------------------- 算

    def set_tol(self, tol):
        self.tol_override = tol
        for b in self.blocks:               # tol 是整份货的，全体失效
            b.cand = None
            b.touch()

    def set_budget(self, n):
        self.budget = n
        for b in self.blocks:
            b.touch()

    def compute(self, check=True, only=None, force=False, fit=True):
        """算脏块。

        `only` 给一块时其余块只在脏了才重算 —— 这是「拖滑块还能实时报
        合计字节」的全部秘诀：动的是焦点块，其余块的文本还在缓存里，
        合计只是把几段字符串加起来。
        """
        per = self.per_block_budget()
        for b in self.included():
            # 精度**不按块降级**。屏幕上那个合计字节数就是复制出去的字节数，
            # 而 `check=False` 出来的文本少一行 `# recon:` —— 只要有一块走了
            # 便宜那条，合计就比真正复制出去的少几十字节，Step 1 想消灭的
            # 正是这种「屏幕和产物对不上」。缓存够不够用因此有两个条件：
            # 脏没脏，以及**精度够不够**。
            stale = (b.dirty() or (check and not b._checked)
                     or (only is not None and b is only))
            if not force and not stale:
                continue
            b.compute(per, self.tol_override, self.keep_extrema, check, fit)

    # -------------------------------------------------- 结果

    def text(self):
        return "\n".join(b.text for b in self.included() if b.text)

    def total_bytes(self):
        t = self.text()
        return emit.nbytes(t) if t else 0

    def worst(self):
        """全部要发出去的块里最差的那个重建误差。-> Err 或 None。"""
        cands = [b.red.worst for b in self.included()
                 if b.red is not None and b.red.err]
        return max(cands, key=lambda e: e.pct) if cands else None

    def worst_eps(self):
        """最差误差是各自 eps 的几倍。字节和精度是两条独立的判据。"""
        peak = 0.0
        for b in self.included():
            if b.red is None:
                continue
            for e in b.red.err:
                eps = e.sig.eps
                if 0 < eps < float("inf"):
                    peak = max(peak, e.maxerr / eps)
        return peak

    def n_kept(self):
        return sum(len(b.red.kept) for b in self.included() if b.red)

    def warns(self):
        out = []
        for b in self.included():
            out.extend(getattr(b.trace, "warns", []) or [])
        return out


def plan(traces, args, keep_extrema=True):
    """[trace] -> 算好的 Shipment。命令行和 GUI 走的是同一条路。"""
    ship = Shipment([Block(tr, args) for tr in traces], args, keep_extrema)
    ship.compute()
    return ship


def process(path, args):
    """-> (text, [(trace, reduction), ...])"""
    ship = plan(prepare_traces(path, args), args)
    return ship.text(), [(b.trace, b.red) for b in ship.included()]


def build_parser():
    p = argparse.ArgumentParser(
        prog="wave_reduce",
        description="Cadence 波形 CSV -> .wv（能粘进聊天框的紧凑文本）")
    p.add_argument("infile", nargs="*", help="ViVA 导出的 CSV")
    p.add_argument("-o", "--out", help="输出文件，默认 stdout")
    p.add_argument("--tol", type=float, default=None,
                   help="RDP 相对容差，占量程比例（默认 %g；某些 kind 会自荐"
                        "更合适的值）" % core.DEFAULT_TOL)
    p.add_argument("--budget", type=int, default=default_budget(),
                   help="输出字节上限，0 = 不限（默认 %(default)s，"
                        "可用环境变量 EDA_REDUCE_BUDGET 改成你那条通道的宽度，"
                        "支持 '32k' 这种写法）")
    p.add_argument("--max-points", type=int, default=None,
                   help="SHAPE 段最多多少行（再叠加 --budget）")
    p.add_argument("--kind", help="强制分析类型: " + (", ".join(emit.registered())
                                                     or "tran/freq"))
    p.add_argument("--xscale", choices=("lin", "log"), help="强制 x 轴刻度")
    p.add_argument("--layout", choices=("auto", "a", "b"), default="auto",
                   help="CSV 布局；b = 每条 trace 自带 x 列")
    p.add_argument("--xcols", help="手指定 x 列下标，逗号分隔（从 0 数）")
    p.add_argument("--xrange", metavar="LO:HI",
                   help="只导出这一段，吃工程记数：--xrange 1.6u:1.62u。"
                        "预算摊在几十个周期上才看得清波形；一头留空表示到端点")
    p.add_argument("--max-cand", type=int, default=core.MAX_CAND,
                   dest="max_cand",
                   help="预细化候选点上限（默认 %(default)s）。候选集是**质量上限**："
                        "RDP 只能从候选点里挑。振荡波形想要更高分辨率就开大它，"
                        "代价是 RDP 变慢（GUI 交互会卡）")
    p.add_argument("--unit", action="append", default=[], metavar="C=U",
                   help="声明单位，如 --unit c1=V,x=s。脚本不猜单位")
    p.add_argument("--demod", action="store_true",
                   help="解调：SHAPE 改画上下包络 + 瞬时频率，另附几个代表性周期的"
                        "原始样点。振荡波形的正解——2515 个周期折线要 218 KB，"
                        "解调后约 26 KB，且回答的是「包络」和「频率牵引」")
    p.add_argument("--demod-cycles", type=int, default=None, dest="demod_cycles",
                   metavar="N", help="附几个代表性周期（默认 %d）。挑在极值处："
                                     "包络最大/最小、频率偏离最大、残差最大"
                                     % 6)
    p.add_argument("--demod-fspan", type=int, default=0, dest="demod_fspan",
                   metavar="M", help="频率跨几个周期测（0=自动）。这是取舍不是细节："
                                     "M 大了频率曲线平滑但迟钝，小了跟得快但噪 —— "
                                     "「频率被牵引了多少」看不看得见就取决于它")
    p.add_argument("--demod-min", type=int, default=None, dest="demod_min",
                   metavar="N", help="少于这么多周期就不解调（默认 %d）" % 20)
    p.add_argument("--no-metrics", action="store_true",
                   help="只出 SHAPE，不跑测量")
    p.add_argument("--no-offset", action="store_true", help="不扣基线")
    p.add_argument("--version", action="store_true",
                   help="打印这份代码的 build（commit + 日期）后退出。"
                        "隔离区没有 git，「我跑的是不是新版」只能这么问")
    p.add_argument("--list-kinds", action="store_true",
                   help="列出已注册的分析类型后退出")
    p.add_argument("--gui", action="store_true", help="开 Tkinter 预览窗口")
    p.add_argument("--selftest", action="store_true",
                   help="配 --gui 用：无人值守跑一遍 GUI 并打印状态后自动退出")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    emit.load_builtin()
    if args.version:
        print("wave_reduce  build %s" % core.build_id())
        print("             %s" % os.path.dirname(os.path.abspath(core.__file__)))
        return 0
    if args.list_kinds:
        print("已注册的 kind: " + (", ".join(emit.registered()) or "无"))
        return 0
    # 参数归一化必须在 --gui 分支**之前**：GUI 走的是同一个 args。
    # 放在后面的话 --gui --xcols 0,2 传进去的是字符串，
    # parse_csv 里 set("0,2") 会变成 [',','0','2']。
    if args.xcols:
        args.xcols = [int(v) for v in str(args.xcols).split(",")]
    if args.xrange:
        s = str(args.xrange).replace("..", ":")
        if ":" not in s:
            build_parser().error("--xrange 要写成 LO:HI，比如 1.6u:1.62u")
        a, b = s.split(":", 1)
        try:
            args.xrange = (core.parse_eng(a) if a.strip() else None,
                           core.parse_eng(b) if b.strip() else None)
        except ValueError:
            build_parser().error("--xrange 看不懂: %r" % args.xrange)
    if not args.budget:
        args.budget = None
    # 默认值放在这里而不是 add_argument 里：wave_demod 可能缺席（逃生舱模式），
    # 那时 build_parser() 也不该炸
    if args.demod_cycles is None or args.demod_min is None:
        try:
            import wave_demod
            dc, dm = wave_demod.N_REPRESENT, wave_demod.MIN_CYCLES
        except ImportError:
            dc, dm = 6, 20
        args.demod_cycles = args.demod_cycles or dc
        args.demod_min = args.demod_min or dm
    if args.gui:
        import wave_gui
        return wave_gui.run(args.infile[0] if args.infile else None, args)
    if not args.infile:
        build_parser().error("要么给一个 CSV，要么用 --gui")

    texts, allinfo = [], []
    for path in args.infile:
        txt, info = process(path, args)
        texts.append(txt)
        allinfo.extend(info)
    result = "\n".join(texts)

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(result)
    else:
        sys.stdout.write(result)

    # WARN 也吼到 stderr：人跑完命令看的是终端，不会先去翻 .wv 头
    for tr, _ in allinfo:
        for wmsg in tr.warns:
            sys.stderr.write("\n!! WARN [%s] %s\n" % (tr.source, wmsg))

    total = sum(os.path.getsize(p) for p in args.infile)
    outb = emit.nbytes(result)
    sys.stderr.write("\n---- %d -> %d bytes (%.0fx)  预算 %s\n"
                     % (total, outb, (total / outb) if outb else 0,
                        args.budget or "不限"))
    for tr, red in allinfo:
        w = red.worst
        sys.stderr.write(
            "     %-14s %-5s %6d -> %-5d pts (%5.1fx)   max|err| %s (%.2f%%) @ %s\n"
            % (tr.source, tr.kind, len(tr.x), len(red.kept), red.ratio,
               core.eng_str(w.maxerr, w.sig.unit, 3) if w else "-",
               w.pct if w else 0.0,
               core.eng_str(w.at, tr.xunit, 4) if w else "-"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
