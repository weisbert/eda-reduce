#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wave_emit.py — `.wv` 三段格式 + 分析类型（kind）注册表。

三段的角色分工，这是整个格式的要害：

    [METRICS] / [EVENTS]   **全精度的事实**。脚本在全分辨率数据上量出来的，
                           丢了不可逆。别去 SHAPE 里量数。
    [SHAPE]                **降精度的形状**。量化过，只用来看走势。

头部第二行起是**自检**：输出声明自己的不确定度。等价于 `.ckt` 末尾那段推断标注——
先看那一行，再决定信不信下面的曲线。

注册表：`register("tran", TranMetrics)`。加一种分析类型 = 加一个文件 + 一行注册，
不动核心。`wave_emit` 自己不 import 任何 metrics 模块，它们缺席时照样出 .wv
（只是没有 METRICS/EVENTS 段）——`wave_core + wave_emit + wave_cli` 三个文件
是永远的逃生舱。

依赖：**纯标准库，硬性**。
"""

import os

from wave_core import build_id, eng_fmt, eng_str

WV_VERSION = "WV1"
MAX_EVENTS = 250
LINE_W = 108
MIN_PTS_PER_CYCLE = 4     # 低于这个数，SHAPE 画出来就不像正弦了
MIN_CYCLES = 20           # 周期数少于这个就不提 —— 几个周期的振铃本来就该逐点画
FLAT_REL = 1e-4           # 起伏不到自身量级的这个比例，就当它是平的


# --------------------------------------------------------------- 测量/事件


class Metric(object):
    """METRICS 段的一项。必须是「名字 值 单位」三元组——模型才读得动。

    value 给 float 就按工程记数格式化；给 str 就原样用（需要自己带符号时用它）。
    at 是 x 轴上的位置（自然单位），None 表示这个量不属于某个时刻。
    """

    __slots__ = ("col", "group", "name", "value", "unit", "at", "note")

    def __init__(self, col, name, value, unit="", at=None, group="", note=""):
        self.col = col
        self.group = group or name
        self.name = name
        self.value = value
        self.unit = unit
        self.at = at
        self.note = note

    def text(self, xunit, sig=6):
        if isinstance(self.value, str):
            # 预格式化的值也要带单位 —— spec 要求「名字 值 单位」三元组
            sep = "" if self.unit == "%" else " "
            v = (self.value + sep + self.unit) if self.unit else self.value
        else:
            v = eng_str(self.value, self.unit, sig)
        s = "%s %s" % (self.name, v)
        if self.at is not None:
            s += " @ " + eng_str(self.at, xunit, 6)
        if self.note:
            s += " (%s)" % self.note
        return s


class Event(object):
    """EVENTS 段的一行。全精度时间轴，跨信号对齐就看这一段。"""

    __slots__ = ("x", "col", "tag", "detail")

    def __init__(self, x, col, tag, detail=""):
        self.x = x
        self.col = col
        self.tag = tag
        self.detail = detail


# --------------------------------------------------------------- 注册表


class Metrics(object):
    """每种分析类型一个子类。核心只调这几个钩子，顺序如下：

        prepare()  -> bool   可以改写信号（比如线性谱换成 dB）。改了必须 tr.note()，
                             返回 True 让上层重新 analyze（极值/噪声底要重算）。
        analyze()            在**全分辨率**数据上做测量。这一步不做，信息就永远丢了。
        forced()   -> [int]  必须保留的原始点下标（spur 峰及其 HWHM 包络、事件时刻…）。
                             它们在 RDP 之前就被钉住。
        metrics()  -> [Metric]
        events()   -> [Event]
    """

    kind = "unknown"

    def __init__(self, trace):
        self.trace = trace

    def label(self, sig):
        for i, s in enumerate(self.trace.signals):
            if s is sig:
                return "c%d" % (i + 1)
        return "c?"

    def prepare(self):
        return False

    def analyze(self):
        pass

    def forced(self):
        return []

    def metrics(self):
        return []

    def events(self):
        return []

    def suggest_tol(self):
        """这种分析类型的合理默认容差；None = 用全局默认。

        只有 metrics 层知道「这一段是噪声底，1 dB 就够了」这种事。
        用户显式给了 --tol 就以用户为准，这里只在没给时兜底，且要 note 出来。
        """
        return None


_REGISTRY = {}


def register(kind, cls):
    _REGISTRY[kind] = cls


def registered():
    return sorted(_REGISTRY)


def make_metrics(trace):
    """按 trace.kind 取一个 Metrics。没注册就返回 None（照样能出 .wv）。"""
    cls = _REGISTRY.get(trace.kind)
    return cls(trace) if cls else None


def load_builtin(quiet=True):
    """把随包的 metrics 模块拉进注册表。缺了不报错——逃生舱模式下就是没有。"""
    got = []
    for name in ("wave_metrics_tran", "wave_metrics_freq"):
        try:
            __import__(name)
            got.append(name)
        except ImportError:
            if not quiet:
                raise
    return got


def run_metrics(trace):
    """prepare -> (需要时重新 analyze) -> analyze。返回 Metrics 或 None。"""
    import wave_core
    m = make_metrics(trace)
    if m is None:
        return None
    if m.prepare():
        wave_core.analyze(trace, kind=trace.kind, xscale=trace.xscale)
    m.analyze()
    return m


# --------------------------------------------------------------- 格式化


def _stem(tr):
    s = os.path.splitext(os.path.basename(tr.source or "wave"))[0]
    return s


def _wrap(prefix, items, width=LINE_W):
    """一组测量塞一行，超宽就换行重起 prefix。"""
    out, cur = [], prefix
    for it in items:
        add = ("   " if cur != prefix else "  ") + it
        if cur != prefix and len(cur) + len(add) > width:
            out.append(cur)
            cur = prefix + "  " + it
        else:
            cur += add
    if cur != prefix:
        out.append(cur)
    return out


# 正文里不写方括号形式的段名 —— 段标记的判据是「行首是 [」，
# 注释里出现一模一样的字样会让解析器（和模型）分不清哪一行才是真的段开始。
LEGEND = (
    "# 读法: METRICS/EVENTS 两段是全精度事实(脚本在全分辨率数据上量的,"
    " 丢了不可逆); SHAPE 段是量化过的形状, 别拿它量数",
    "# SHAPE 每行 = x 后跟各列; 真值 = 该列值/scale + offset,"
    " 两者都在上面的列声明里; 单位带 ? = 从列名推的, 不是数据里声明的",
    "# 段标记只认行首的 [ ；以 # 开头的都是注释",
)


def eng_range(lo, hi, unit):
    """'0 .. 300 ns' 比 '0 s .. 300 ns' 好读，所以两端共用一个词头。

    但跨了三个数量级以上（log 扫描）就各用各的 —— 共用词头会把一头压成 1e-8。
    """
    ref = hi if abs(hi) >= abs(lo) else lo
    if ref == 0.0:
        return "0 .. 0 %s" % unit if unit else "0 .. 0"
    if lo != 0.0 and abs(ref / lo) > 1000.0:
        return "%s .. %s" % (eng_str(lo, unit, 5), eng_str(hi, unit, 5))
    a, u = eng_fmt(ref, unit, 5)
    sc = float(a) / ref
    b, _ = eng_fmt(lo * sc, "", 5)
    c, _ = eng_fmt(hi * sc, "", 5)
    return "%s .. %s %s" % (b, c, u)


def carrier_warn(red):
    """SHAPE 的点数撑不撑得起这段波形里的**周期数**。撑不起就说出来。

    这条是拿一个真实的困惑换来的：「max-points 拖到最大了，波形还是跟原数据差很多」。
    答案是三个天花板叠在一起，而且最外面那个是**信息量**不是参数：
    2515 个振荡周期要画得像正弦至少要 6 点/周期 = 15000 点 ≈ 150 KB，
    20 KB 装不下，拖滑块永远拖不到。工具当时什么都没说，只是不再变好。

    所以这里报的是「每周期几个点」而不是误差百分比 —— 误差百分比对振荡信号
    没有直觉（差半个周期就是 100%），点/周期直接对得上「画出来像不像」。
    """
    tr = red.trace
    nk = len(red.kept)
    worst = None
    for s in tr.signals:
        if s.cycles < MIN_CYCLES:
            continue
        # 整条曲线的起伏还不到自身量级的万分之一 -> 它实质上是平的，
        # 「画不出正弦」这句话对它没有意义。派生量上会遇到：一条恒定载频的
        # f_inst，全部起伏就是 2e-6 的测量残差，照样能数出上百个「周期」。
        mid = abs(0.5 * (s.vmin + s.vmax))
        if mid > 0 and s.rng < FLAT_REL * mid:
            continue
        ppc = nk / float(s.cycles)
        if ppc < MIN_PTS_PER_CYCLE and (worst is None or ppc < worst[1]):
            worst = (s, ppc)
    if worst is None:
        return ""
    s, ppc = worst
    per_pt = shape_bytes(red) / float(max(1, nk))
    need_kb = MIN_PTS_PER_CYCLE * s.cycles * per_pt / 1024.0
    # 出路要**按用得上的顺序**给。这条警告写在 --demod 之前，一直只说
    # 「--xrange 或加预算」；结果用户拿着这条警告问「我看不见你说的包络」——
    # 工具从没告诉过他还有这个模式。最该用的那条排第一。
    return ("SHAPE 只有 %.1f 点/周期（%s 在这段里约 %d 个振荡周期，%d 个保留点）。"
            "画出来不会像正弦，也别拿它数周期或量摆幅。"
            "**这是预算和周期数的矛盾，拖大 max-points 解决不了。** 三条出路："
            "① 要看**起振/包络**就加 `--demod`（传包络+瞬时频率+几个代表性周期，"
            "同一份 txt，实测 218 KB 的东西 22 KB 装得下）；"
            "② 要看**某几十个周期的实际波形**就 `--xrange 1.6u:1.62u` 切一段；"
            "③ 硬要全画就把预算开到 %.0f KB 以上。"
            % (ppc, s.name, s.cycles, nk, need_kb))


def header(red, metrics=None, extra=None):
    tr = red.trace
    x = red.xspec
    out = []
    nin, nout = len(tr.x), len(red.kept)
    out.append("# %s  %s  %s  %d sig  %s %s  %d -> %d pts (%.1fx)"
               % (WV_VERSION, tr.kind, _stem(tr), len(tr.signals), tr.xname,
                  eng_range(tr.x[0], tr.x[-1], tr.xunit), nin, nout, red.ratio))
    if tr.window:
        out.append("# window: %s .. %s   ← **只是一段**，原文件 %s .. %s。"
                   "下面所有 METRICS 都是在这个窗口内量的"
                   % (eng_str(tr.window[0], tr.xunit, 5),
                      eng_str(tr.window[1], tr.xunit, 5),
                      eng_str(tr.window[2], tr.xunit, 5),
                      eng_str(tr.window[3], tr.xunit, 5)))
    # WARN 排在 recon 前面：recon 说的是「我压得准不准」，WARN 说的是
    # 「你这份输入根本撑不住下面的数」。后者更靠前一步，先看。
    for n in tr.warns:
        out.append("# WARN: " + n)
    cw = carrier_warn(red)
    if cw:
        out.append("# WARN: " + cw)
    w = red.worst
    if w is not None:
        tail = ""
        if w.sig.noise > 0 and w.maxerr <= 4.0 * w.sig.noise:
            tail = "  ← 已到该通道噪声底 %s，再多的点也降不下去" % eng_str(
                w.sig.noise, w.sig.unit, 3)
        out.append("# recon: max|err| %s (%.2f%% of range) @ %s=%s   rms %s"
                   "   [worst %s]%s"
                   % (eng_str(w.maxerr, w.sig.unit, 3), w.pct, tr.xname,
                      eng_str(w.at, tr.xunit, 5), eng_str(w.rms, w.sig.unit, 3),
                      w.sig.name, tail))
    out.append("# axis: x %s%s | 段间%s内插"
               % (tr.xscale,
                  "" if tr.kind_src == "declared" else
                  ("  kind=%s(%s)" % (tr.kind, tr.kind_src)),
                  "按 log(x) 线性" if tr.xscale == "log" else "线性"))
    out.append("# %-4s %-16s [%-7s] %s .. %s   (量化 %s)"
               % ("x", tr.xname, x.unit_out, x.txt(tr.x[0]), x.txt(tr.x[-1]),
                  x.q_txt))
    for cs, er in zip(red.specs, red.err):
        out.append("# %-4s %-16s [%-7s] offset %-11s range %s..%s   (量化 %s)"
                   "  err %.2f%% (%s)"
                   % (cs.label, cs.sig.name, cs.unit_out, cs.off_txt,
                      _n(cs.lo_out, cs.dec), _n(cs.hi_out, cs.dec), cs.q_txt,
                      er.pct, eng_str(er.maxerr, cs.sig.unit, 3)))
    for n in tr.notes:
        out.append("# note: " + n)
    for n in (extra or []):
        out.append("# note: " + n)
    # 每份 .wv 自报是哪一版代码出的。20 多个字节换掉「隔离区到底换上新版没有」
    # 这个反复出现的问题 —— 之前只能靠「输出里有没有那条新 note」去推断。
    out.append("# build: %s" % build_id())
    out.extend(LEGEND)
    return out


def _n(v, dec):
    s = "%+.*f" % (min(dec, 4), v)
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def metrics_block(red, m):
    tr = red.trace
    rows = m.metrics() if m else []
    if not rows:
        return []
    out = ["[METRICS]"]
    order, groups = [], {}
    for it in rows:
        key = (it.col, it.group)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(it)
    for key in order:
        out.extend(_wrap("%-4s" % key[0],
                         [it.text(tr.xunit) for it in groups[key]]))
    return out


def events_block(red, m):
    tr = red.trace
    evs = sorted(m.events(), key=lambda e: e.x) if m else []
    if not evs:
        return []
    out = ["[EVENTS]"]
    shown = evs[:MAX_EVENTS]
    for e in shown:
        out.append("%-13s %-4s %-11s %s"
                   % (eng_str(e.x, tr.xunit, 6), e.col, e.tag, e.detail))
    if len(evs) > len(shown):
        # 截断了要说出来 —— 不说的话，读的人会以为这就是全部
        out.append("# …另有 %d 条事件未列出（上限 %d）"
                   % (len(evs) - len(shown), MAX_EVENTS))
    return out


def shape_block(red):
    tr = red.trace
    x = red.xspec
    out = ["[SHAPE] %s %s" % (tr.xname, " ".join(c.label for c in red.specs))]
    ys = [(c, c.sig.y) for c in red.specs]
    for i in red.kept:
        row = [x.txt(tr.x[i])]
        for c, y in ys:
            row.append(c.txt(y[i]))
        out.append(" ".join(row))
    return out


def emit(red, metrics=None, extra_notes=None):
    """-> .wv 文本（一条 trace）。"""
    parts = header(red, metrics, extra_notes)
    # 附加段排在 SHAPE 之后：它们是「原始样点」，比量化过的形状更贵也更准，
    # 但读的顺序仍然是 先自检 -> 再测量 -> 再形状 -> 最后细节
    for blk in (metrics_block(red, metrics), events_block(red, metrics),
                shape_block(red)) + tuple(red.trace.extra):
        if blk:
            parts.append("")
            parts.extend(blk if isinstance(blk, list) else blk.splitlines())
    return "\n".join(parts) + "\n"


def emit_all(reds, metrics_list=None, extra_notes=None):
    """多条 trace（布局 B）拼一个文档，每条自带完整头部。"""
    ml = metrics_list or [None] * len(reds)
    chunks = [emit(r, m, extra_notes) for r, m in zip(reds, ml)]
    return "\n".join(chunks)


def nbytes(text):
    return len(text.encode("utf-8"))


def shape_bytes(red):
    """只数 SHAPE 段的字节。CLI / GUI 二分点数预算时用它，比整篇 emit 快。"""
    tr = red.trace
    x = red.xspec
    n = 0
    ys = [(c, c.sig.y) for c in red.specs]
    for i in red.kept:
        n += len(x.txt(tr.x[i])) + 1
        for c, y in ys:
            n += len(c.txt(y[i])) + 1
    return n
