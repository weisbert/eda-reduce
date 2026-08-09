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

# 预算按**内容类型**分的默认权重。归一化只在实际出现的类型之间做 ——
# 没解调时只有 raw，它就拿 100%。
#
# 为什么原波形拿大头：包络和瞬时频率是**每周期一个点**的派生量，几百个点
# 就到头了（实测 2166 个周期的包络 15 个点、频率 109 个点）；原波形是
# 唯一一个「给多少分辨率就吃多少」的。把预算摊平等于让原波形挨饿，
# 而那正是「1.8 点/周期、画出来不像正弦」的来源。
#
# 这些只是**默认值**：`--share raw=50,env=30,freq=20`，GUI 上是旋钮。
DEFAULT_SHARES = {"raw": 0.72, "env": 0.16, "freq": 0.12}

# 一块的**固定开销**：头部 + 列声明 + notes + METRICS + EVENTS，跟 SHAPE
# 有几个点无关。实测约 2.4 KB —— 一个只剩 5 个形状点的窗口照样要 2437 字节。
#
# 这个数决定「预算最多养得起几块」。不管它的后果实测过：预算 16 KB、
# 原波形份额 10% 时切出 3 段，每段分到 1622 字节却怎么压都要 2400，
# 三段全部超支，合计 18890 / 16384 —— 而 SHAPE 总共才 16 个点。
# 块数本身就是成本，切得越碎，能留给形状的越少。
BLOCK_OVERHEAD = 2400

# 每块的下限。份额再小也得装得下固定开销 + 两三行 SHAPE，
# 否则这块出来是个只有头没有身子的壳，比不搬还难读。
MIN_BLOCK_BYTES = 1200

# 「这块把份额用满了吗」的判定余量。fit 是二分出来的，落点本来就带
# 几十字节的颗粒度，卡死等号会把明明吃紧的块判成有余。
SHARE_SLACK = 64

# 按实测用量重分几轮。每轮一次「脏块重压」，不是整份货重压，所以便宜。
# 3 轮实测足够收敛；再多也只是在几十字节上抖。
SHARE_ROUNDS = 3


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


def parse_shares(decls):
    """--share raw=50,env=30,freq=20 -> {"raw":0.5, "env":0.3, "freq":0.2}。

    权重不要求加起来是 1：归一化在 `share_map` 里按**实际出现**的类型做。
    写死成百分比反而会骗人 —— 没解调时只有 raw，raw=50 也还是拿 100%。
    """
    if not decls:
        return None
    out = {}
    for item in decls:
        for part in item.split(","):
            if "=" not in part:
                continue
            k, v = part.split("=", 1)
            k = k.strip().lower()
            try:
                w = float(v.strip())
            except ValueError:
                raise SystemExit("--share 的权重得是数字：%r" % part.strip())
            if w < 0:
                raise SystemExit("--share 的权重不能是负数：%r" % part.strip())
            out[k] = w
    unknown = set(out) - set(DEFAULT_SHARES)
    if unknown:
        raise SystemExit("--share 不认识的内容类型 %s（认得的是 %s）"
                         % (", ".join(sorted(unknown)),
                            ", ".join(sorted(DEFAULT_SHARES))))
    return out or None


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


def demod_traces(tr, args, n_rep=None, fspan=None, min_cycles=None,
                 strict=True):
    """--demod 的**唯一**入口：解调 + 留住原波形 + 自动挑窗。

    解调必须在 analyze **之后**（要 vmin/vmax/噪声底才切得出周期），
    在 reduce **之前**（后面整条管道都跑在派生信号上）。

    命令行和 GUI 都走这里。上一版两边都调 `wave_demod.apply()`，看着是
    共用的，其实「解调之外还做什么」各写各的 —— 于是「原波形一起搬」
    只加进了命令行，GUI 里勾上解调原波形照样消失。共用的必须是**整件事**，
    不是中间那一步。`n_rep` / `fspan` / `min_cycles` 给了就覆盖 args，
    GUI 上那几个旋钮是实时的，不在 args 里。
    """
    try:
        import wave_demod
    except ImportError:                          # 逃生舱模式：只有三个文件时
        if strict:
            raise SystemExit("--demod 需要 tools/wave_demod.py，这份部署里没有")
        tr.note("--demod 需要 tools/wave_demod.py，这份部署里没有")
        return [tr]
    # 兜底默认值也放在**消费点**。`main()` 里有一段专门补 demod_cycles /
    # demod_min，但只有命令行走 main —— GUI 和测试都是
    # `build_parser().parse_args()` 直接来的，那边拿到的是 None，
    # 一路带到 `len(picks) < n` 就 TypeError。跟 --windows 是同一个坑。
    if n_rep is None:
        n_rep = getattr(args, "demod_cycles", None)
        if n_rep is None:
            n_rep = wave_demod.N_REPRESENT
    if min_cycles is None:
        min_cycles = getattr(args, "demod_min", None) or wave_demod.MIN_CYCLES
    if fspan is None:
        fspan = getattr(args, "demod_fspan", 0) or 0
    out = wave_demod.apply(tr, args.tol or core.DEFAULT_TOL, args.budget,
                           n_rep, min_cycles, args.kind, args.xscale, fspan)
    if out == [tr]:                              # 没生效，原样返回
        return out
    # **解调是加的，不是顶替的。** 原来这里把原波形整条换掉，于是「勾了包络
    # 就只剩包络」—— 用户原话「不能同时导出包络+原波形吗」。载波确实是最贵
    # 的那部分，但贵不等于该由工具替人决定不要：50 KB 装得下包络 + 频率 +
    # 几十个周期的原波形，那才是「够本地还原 debug 信息」。要不要原波形
    # 现在是勾选框（Block.included）和份额的事，不是模式的事。
    return _split_raw(tr, args, wave_demod) + out


# 一个原始样点在 SHAPE 里大约占这么多字节（x + 一列 y + 两个分隔）。
# 只用来把「份额」换算成「窗口能有多宽」，估偏一点无所谓：真正的点数
# 还是 fit_budget 二分出来的。
BYTES_PER_PT = 12
# 一个周期少于这么多点，画出来就不是正弦了。挑窗的目标就是把每个周期
# 的点数顶到这个数以上 —— 实测整条 2166 周期只有 1.8 点/周期。
TARGET_PTS_PER_CYCLE = 20


def win_count(args):
    """--windows 归一成整数。-1 = auto（装不下才切），0 = 永不切。

    归一化放在**消费点**而不是只放在 `main()`：GUI 的 args 是
    `build_parser().parse_args([])` 直接来的，从不经过 main 的后处理，
    于是那边拿到的还是字符串 "auto"，一比大小就 TypeError。
    「默认值只在一条路上被规整」是这个代码库里反复出现的坑。
    """
    v = getattr(args, "windows", -1)
    if isinstance(v, int):
        return v
    if v is None or str(v).strip().lower() in ("auto", ""):
        return -1
    try:
        return max(0, int(v))
    except (TypeError, ValueError):
        return -1


AUTO_WINDOWS = 3                 # auto 实际会切几段


def split_raw(tr, args):
    """原波形该不该切窗、切几段。-> [trace, ...]。**解调开不开都走这里。**

    原来这条只挂在 `--demod` 里面，于是不解调时一份 2166 周期的起振波形
    照样整条摊 —— 实测载入即 232 KB / 预算 50 KB，红着进不去，而
    `_autofit` 压不动（保底点撑住下限）。可「整条画不清楚」跟解调开没开
    毫无关系，它只跟「周期多不多、预算够不够」有关。
    """
    try:
        import wave_demod
    except ImportError:                      # 逃生舱模式：整条留着，别切
        return [tr]
    return _split_raw(tr, args, wave_demod)


def _split_raw(tr, args, wave_demod):
    """原波形整条装不下时，切成几段**有信息的**窗口。-> [trace, ...]。

    这是「50 KB 装什么都行，够本地还原 debug 信息」那条要求的落点。
    整条 2166 个周期摊 50 KB 是 1.8 点/周期——画出来不像正弦、数不了周期、
    量不了摆幅，等于这份 SHAPE 白搬。切成 3 段之后每段 25 点/周期。

    只在**确实装不下**时才切：周期数少、或者整条本来就画得清楚，就别动
    人家的数据。切了一定在 note 里说清挑了哪几段、按什么挑的。
    """
    n_win = win_count(args)
    if n_win == 0 or not args.budget:
        return [tr]
    cycles = wave_demod.find_cycles(tr)[0]
    if len(cycles) < 3 * wave_demod.MIN_CYCLES:
        return [tr]
    w = dict(DEFAULT_SHARES)
    w.update(getattr(args, "shares", None) or {})
    raw_bytes = args.budget * w.get("raw", 1.0) / (sum(w.values()) or 1.0)
    if raw_bytes / BYTES_PER_PT >= TARGET_PTS_PER_CYCLE * len(cycles):
        return [tr]                          # 整条就够清楚，不用切
    if n_win < 0:                            # auto
        n_win = AUTO_WINDOWS
    # **别切出养不起的块。** 每块有 ~2.4 KB 固定开销（头部/列声明/METRICS/
    # EVENTS/notes），跟形状点数无关。原波形那份份额养不起几块就只切几块 ——
    # 硬切的结果是每块都超支、合计爆预算，而形状点数反而更少。
    #
    # 注意方向：份额小的时候要切的是**更少的段**，不是「干脆不切」。
    # 不切 = 整条 1497 个周期的极值全是保底点 = 87 KB，比切 1 段还大 34 倍。
    # 切窗恰恰是让原波形变小的手段，只养得起一段就切一段（那不过是
    # 工具替你挑了个 --xrange）。
    n_win = max(1, min(n_win, int(raw_bytes // BLOCK_OVERHEAD)))
    per = raw_bytes / n_win
    cyc = max(4, int(per / BYTES_PER_PT / TARGET_PTS_PER_CYCLE))
    wins = wave_demod.pick_windows(cycles, n_win, cyc)
    if not wins:                             # 一段都挑不出来才整条留着
        return [tr]
    out = []
    for i, (t0, t1, why) in enumerate(wins):
        sub = tr.clone()
        try:
            core.slice_trace(sub, t0, t1)
        except ValueError:
            continue
        core.analyze(sub, kind=args.kind, xscale=args.xscale)
        sub.index = tr.index
        sub.role, sub.role_why = "raw", why
        sub.note("自动挑窗 %d/%d：%s。整条 %d 个振荡周期摊在 %s 预算上只有 "
                 "%.1f 点/周期（画出来不像正弦，也数不了周期）；切成 %d 段之后"
                 "这一段约 %d 点/周期。**挑段是工具替你做的判断** —— "
                 "不认同就用 --xrange 自己指定，或 --windows 0 关掉整条不切"
                 % (i + 1, len(wins), why, len(cycles),
                    core.eng_str(args.budget, "B", 3),
                    raw_bytes / BYTES_PER_PT / len(cycles), len(wins), cyc and
                    int(per / BYTES_PER_PT / max(1, cyc))))
        out.append(sub)
    return out or [tr]


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
        prepared.extend(demod_traces(tr, args) if args.demod
                        else split_raw(tr, args))
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
        """向后兼容的单值摊法。新代码用 `share_map()`。"""
        inc = self.included()
        return (self.budget // len(inc)) if (self.budget and inc) else None

    def share_map(self, spent=None):
        """-> {block: 这块能用多少字节}。**预算怎么摊只此一处。**

        原来是「按块均分」。均分的毛病在于块的**胃口差着两个数量级**：
        同一份 50 KB 里，包络 15 个点要 0.3 KB 就够了，而原波形 2166 个
        振荡周期给多少都不嫌多。均分等于把 1/3 的预算摊给一个只要 1%
        的块，剩下的 SHAPE 就只有 1.8 点/周期 —— 画出来不像正弦。

        所以按**内容类型**给权重（raw/env/freq），同类型的块再均分。
        权重是给的，不是猜的：`--share raw=70,env=20,freq=10`，GUI 上是旋钮。

        `spent` 给了就做**第二遍**：上一遍每块实际用了多少字节。用不完的
        份额收回来，按权重再摊给那些顶到上限的块 —— 否则「50 KB」里
        永远有一截是空的，而空的那一截本可以变成原波形的分辨率。
        """
        inc = self.included()
        if not self.budget or not inc:
            return dict((b, None) for b in inc)
        w = dict(DEFAULT_SHARES)
        w.update(getattr(self.args, "shares", None) or {})
        raw_w = [max(0.0, float(w.get(b.trace.role, w.get("raw", 1.0))))
                 for b in inc]
        if not sum(raw_w):                       # 全设成 0 就退回均分
            raw_w = [1.0] * len(inc)
        tot = sum(raw_w)
        out = {}
        for b, wt in zip(inc, raw_w):
            out[b] = max(MIN_BLOCK_BYTES, int(self.budget * wt / tot))
        if not spent:
            return out
        # 第二遍：按**第一遍实际用了多少**重新分。三种块要分开待：
        #
        #   压不动的  用量 > 份额。固定开销（头部/列声明/METRICS/EVENTS）就
        #             那么大，份额给少了它也下不来。实测 env 块的底是 6.3 KB，
        #             给它 5% × 50 KB = 2.5 KB 它照样吐 6.3 KB —— 于是合计
        #             54270 / 51200 超了，而超的原因跟原波形一点关系都没有。
        #             这种块按它**实际需要**的记账，别再假装它只占 2.5 KB。
        #   吃饱的    用量 < 份额。它到自己的自然上限了（包络就那么几个点），
        #             再给也不会变大。多出来的份额收回来。
        #   吃紧的    用量 ≈ 份额。剩下的预算全给这些块 —— 它们是唯一能把
        #             多出来的字节变成分辨率的。
        #
        # 不区分这三种的话，「50 KB」要么空着一截，要么被压不动的块顶穿。
        # 第一步：把**压不动**的块按实际用量记账，剩下的预算给其余块按权重分。
        # 顺序很关键：先扣掉压不动的，其余块才知道真正还剩多少 —— 哪怕这意味着
        # 它们要比上一遍**缩**。不这么做的话，压不动的那点超支没人埋单，
        # 合计就一直是超的（实测 env+freq 超 5 KB，原波形明明还有余量却不缩）。
        #
        # 关键：压不动的块**记账**按实际用量（别人才知道还剩多少），但**发给
        # 它的预算仍是它原来那一份**。发它「你实际用了多少」等于告诉
        # `fit_budget`「你没超」，那行 `**超预算**` 就再也不会打印 ——
        # 而「压不进去就说出来」是这个格式的立身之本，被自己的分配器
        # 悄悄抹掉是最坏的一种回归。两条测试正是钉这个的。
        fixed = dict((b, spent.get(b, 0)) for b in inc
                     if spent.get(b, 0) > out[b] + SHARE_SLACK)
        rest_blocks = [b for b in inc if b not in fixed]
        if not rest_blocks:
            return out                       # 全都压不动：原样发，让它们各自声明

        def split(blocks, pot):
            tw = sum(w.get(b.trace.role, 1.0) for b in blocks) or 1.0
            return dict((b, max(MIN_BLOCK_BYTES,
                                int(pot * w.get(b.trace.role, 1.0) / tw)))
                        for b in blocks)

        alloc = split(rest_blocks, self.budget - sum(fixed.values()))
        # 第二步：其余块里有**吃饱**的（到自然上限了，再给也不会变大），
        # 把它们吃不完的收回来给吃紧的。少了这步，「50 KB」里会空着一截。
        full = dict((b, spent[b]) for b in rest_blocks
                    if spent.get(b, 0) < alloc[b] - SHARE_SLACK)
        hungry = [b for b in rest_blocks if b not in full]
        if full and hungry:
            alloc.update(full)
            alloc.update(split(hungry, self.budget - sum(fixed.values())
                               - sum(full.values())))
        for b in inc:
            if b not in fixed:               # 压不动的保持原份额，好让它声明
                out[b] = max(MIN_BLOCK_BYTES, alloc[b])
        return out

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
        shares = self.share_map()
        self._compute_pass(shares, check, only, force, fit)
        if not (fit and self.budget):
            return
        # 再分几轮，每轮都拿**上一轮真实用了多少字节**去重分。
        #
        # 为什么必须闭环：一块要多少字节，事前只能估（窗口多宽 × 每周期几个
        # 点 × 每点几字节），而真实用量由保底点决定 —— 保底点由数据决定，
        # 估不准。开环估一次的结果实测是 24 组参数里 11 组超预算，而且越给
        # 原波形份额越超（份额大 -> 窗口宽 -> 保底点多 -> 比给的还多）。
        # 拿实测值重分就没有这个问题：每轮都在真实数字上收敛。
        for _ in range(SHARE_ROUNDS):
            spent = dict((b, emit.nbytes(b.text) if b.text else 0)
                         for b in self.included())
            again = self.share_map(spent)
            # 变大变小都要重算。只重算变大的那些是上一版的写法，结果是
            # 「压不动的块超支 -> 别人该缩却没缩 -> 合计一直超预算」。
            moved = [b for b in again
                     if abs(again[b] - shares[b]) > SHARE_SLACK]
            if not moved:
                break
            for b in moved:
                b.touch()
            # force=False：只有刚 touch 过的那几块是脏的，其余块的文本
            # 还在缓存里。重分不该把整份货重压一次。
            self._compute_pass(again, check, only, False, fit)
            shares = again
            if self.total_bytes() <= self.budget:
                break

    def _compute_pass(self, shares, check, only, force, fit):
        for b in self.included():
            per = shares.get(b)
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

    # -------------------------------------------------- 判据

    def verdict(self):
        v = Verdict()
        v.nbytes = self.total_bytes()
        v.budget = self.budget
        v.nblocks = len(self.included())
        v.warns = self.warns()
        v.worst = self.worst()
        v.peak_eps = self.worst_eps()
        v.bytes_ok = (not self.budget) or v.nbytes <= self.budget
        # 精度按**各信号自己的 eps 的几倍**判，不按 % of range ——
        # 量程本身可能是被一个离群点定的，而 eps 是这份数据自己声明的容差。
        if v.peak_eps <= 1.0:
            v.err_ok = "ok"
        elif v.peak_eps <= 3.0:
            v.err_ok = "warn"
        else:
            v.err_ok = "bad"
        if v.warns:                       # 输入本身就不足以支撑下面的数
            v.err_ok = "bad"
        if not v.bytes_ok or v.err_ok == "bad":
            v.level = "bad"
        elif v.err_ok == "warn":
            v.level = "warn"
        else:
            v.level = "ok"
        return v

    def blockers(self):
        """卡在哪儿。自上而下，**第一个命中的**那条就是要显示的那条。

        排序是「最外面那层先说」：信息量 > 保底点 > 固定开销 > 候选集 >
        预算 > 精度。里面那几层拖参数还有救，最外面那层拖什么都没用。
        """
        inc = self.included()
        if not inc:
            return [Blocker("empty", "一块都没勾 —— .wv 会是空的", [])]
        per = self.per_block_budget()

        # ① 信息量：点数撑不起周期数。拖任何参数都解决不了，所以排最前
        for b in inc:
            if b.red is None:
                continue
            ex = emit.carrier_exits(b.red)
            if ex:
                return [Blocker("carrier", emit.carrier_warn(b.red), [
                    ("demod", "① 开解调（包络+频率，实测 218 KB → 22 KB）"),
                    ("window", "② 只搬当前视窗"),
                    ("budget", "③ 预算改到 %.0f KB" % ex["need_kb"]),
                ])]

        # ② 保底点已经把点数顶住了 —— 此刻拖点数滑块完全无效
        for b in inc:
            if b.red is None or not b.max_points:
                continue
            if len(b.red.kept) > b.max_points:
                return [Blocker("forced", (
                    "保底点 %d 个已经超过点数上限 %d —— **此刻拖点数滑块无效**。"
                    "保底点是 spur/极值/事件，不为预算牺牲"
                    % (len(b.red.forced), b.max_points)), [
                    ("drop_forced", "关掉「保底 spur/事件」"),
                    ("tol_up", "调粗存储精度 tol"),
                    ("window", "只搬当前视窗"),
                ])]

        # ③ 固定开销就顶穿了每块的预算：SHAPE 一行都还没写
        if per:
            for b in inc:
                if b.red is None or not b.text:
                    continue
                fixed = emit.nbytes(b.text) - emit.shape_bytes(b.red)
                if fixed >= per:
                    need = sum(
                        emit.nbytes(x.text) - emit.shape_bytes(x.red)
                        for x in inc if x.red is not None and x.text)
                    return [Blocker("fixed", (
                        "头部+METRICS+EVENTS 本身要 %.1f KB，超过这块分到的 "
                        "%.1f KB。那些是全精度事实，不为预算牺牲"
                        % (fixed / 1024.0, per / 1024.0)), [
                        ("tol_up", "调粗存储精度 tol"),
                        ("budget", "预算改到 %.0f KB" % (need * 1.2 / 1024.0)),
                    ])]

        # ④ 候选集塌了：滑块上限那个 10 是兜底值，跟数据没关系
        for b in inc:
            if b.cand is not None and len(b.cand) <= 10 \
                    and len(b.cand) < len(b.trace.x):
                return [Blocker("cand", (
                    "候选点只有 %d 个（原始 %d）—— 量程八成被极端点定死了，"
                    "滑块上限那个 10 是兜底值"
                    % (len(b.cand), len(b.trace.x))), [
                    ("tol_down", "调细存储精度 tol"),
                    ("max_cand", "调大候选点上限"),
                ])]

        v = self.verdict()
        if not v.bytes_ok:
            return [Blocker("budget", "超预算 %.1f 倍" % v.over(), [
                ("fit", "自动压到预算"),
            ])]
        if v.err_ok != "ok":
            return [Blocker("precision", (
                "最差误差 %.1f× 容差 —— 这份搬出去会失真" % v.peak_eps), [
                ("more_points", "多给点数"),
                ("tol_up", "调粗存储精度（放宽判据）"),
                ("window", "只搬当前视窗"),
            ])]
        return []


class Verdict(object):
    """「这份能不能粘出去」的答案。**两条独立判据，取更差的那个。**

    原来只有字节数一条：19.9 KB 打勾、20.1 KB 打叉，而一份 49% 失真的
    19.9 KB 照样是绿的。装得下和够得准是两件事，混成一个符号就等于
    把其中一件藏了。
    """

    __slots__ = ("level", "bytes_ok", "err_ok", "nbytes", "budget",
                 "peak_eps", "worst", "nblocks", "warns")

    LABEL = {"ok": "✓ 可以粘走", "warn": "⚠ 能粘，但看清楚",
             "bad": "✗ 先别粘"}

    def label(self):
        return self.LABEL[self.level]

    def over(self):
        """超预算几倍。没超或不限预算给 0。"""
        if not self.budget or self.nbytes <= self.budget:
            return 0.0
        return self.nbytes / float(self.budget)


class Blocker(object):
    """卡在哪儿 + 能点的出路。`actions` 是一串 (code, 文案)。

    出路必须是**结构化**的。原来它们拼在一句话里（`carrier_warn`），
    而状态栏那句 `split("。")[0]` 正好把三条出路全切掉 —— 用户看见
    「拖 max-points 解决不了」却看不见该拖什么。
    """

    __slots__ = ("code", "text", "actions")

    def __init__(self, code, text, actions):
        self.code = code
        self.text = text
        self.actions = actions


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
    p.add_argument("--windows", metavar="N", default="auto",
                   help="原波形整条画不清楚时，自动切成 N 段有信息的窗口"
                        "（起振段 / 异常段 / 稳态段）。默认 auto = 装不下才切、"
                        "切 3 段；0 = 永远不切，整条摊。"
                        "挑了哪几段、按什么挑的都写进 note —— 不认同就用 "
                        "--xrange 自己指定")
    p.add_argument("--share", action="append", metavar="ROLE=W", default=None,
                   help="预算按内容类型分的权重，可重复或用逗号分隔："
                        "`--share raw=50,env=30,freq=20`。"
                        "只在**实际出现**的类型之间归一化，所以没解调时 raw 拿全部。"
                        "默认 %s。设成 0 = 这类只留一个最小块（要完全不搬用 GUI 的勾选框）。"
                        "用不完的份额会自动收回来摊给吃紧的块"
                        % ",".join("%s=%d" % (k, v * 100)
                                   for k, v in sorted(DEFAULT_SHARES.items())))
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
    args.shares = parse_shares(args.share)
    if str(args.windows).strip().lower() not in ("auto", ""):
        try:                                 # 早报错，别等到跑一半
            int(args.windows)
        except (TypeError, ValueError):
            build_parser().error("--windows 要写成整数或 auto，看不懂: %r"
                                 % args.windows)
    args.windows = win_count(args)
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
