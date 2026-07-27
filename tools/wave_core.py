#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wave_core.py — 波形压缩的通用核心：解析 / 预细化 / RDP / 量化 / 重建自检。

**这一层对分析类型一无所知**。它只认「一列 x + 若干列 y」，不知道 x 是时间还是频率，
也不知道 y 是电压还是谱密度。分析类型相关的东西全在 `wave_metrics_*` 里。

第一性原理（和 drawio_reduce 不一样，别搞混）：

    脚本看得见全分辨率数据，模型永远看不见。
    凡是需要全分辨率才能算出的量，脚本不算就永远丢了。

所以这一层的铁律是**自检**：输出必须声明自己的不确定度。`recon_error()` 拿
量化之后的保留点重建出整条曲线，跟**原始每一个点**比，报出 max|err| 和它的位置。
那一行是 `.wv` 里第一个该看的东西。

依赖：**纯标准库，硬性**。这是逃生舱——哪天部署链坏了，
`wave_core.py` + `wave_cli.py` 两个文件 scp 过去就能用。

Python 3.6+。
"""

import bisect
import csv
import heapq
import io
import math
import os
import re
from array import array

# --------------------------------------------------------------- 常量

# 归一化后被认作 x 轴的列名。只用来在布局 B 里认出「第二个 time 列」，
# 认错了会在 notes 里说出来，不会静默。
X_NAMES = frozenset("""
    time t freq frequency f hz x sweep index n temp temperature
    vdc idc dc vsweep isweep param variable
""".split())

# 单位可以从列名**读出来**（不是猜）的模式
_DECLARED_FN = re.compile(r"^\s*(db20|db10|dbm|db)\s*\(", re.I)
# 单位只能从列名**推**出来的模式 —— 输出里会带 ? 标记
_INFERRED_FN = [
    (re.compile(r"^\s*(v|vm|vr|vi|vp|vf|vdb|vn)\s*\(", re.I), "V"),
    (re.compile(r"^\s*(i|im|ir|ii|ip|if|idb|in)\s*\(", re.I), "A"),
    (re.compile(r"^\s*(p|pwr|power)\s*\(", re.I), "W"),
]
_X_UNIT_HINT = {"time": "s", "t": "s", "freq": "Hz", "frequency": "Hz",
                "f": "Hz", "hz": "Hz", "temp": "degC", "temperature": "degC"}

_KIND_HINT = [
    (("time", "t"), "tran"),
    (("freq", "frequency", "f", "hz"), "freq"),
    (("temp", "temperature"), "temp"),
    (("vdc", "idc", "dc", "vsweep", "isweep"), "dc"),
]

PREFIX = {-18: "a", -15: "f", -12: "p", -9: "n", -6: "u", -3: "m", 0: "",
          3: "k", 6: "M", 9: "G", 12: "T", 15: "P"}

DEFAULT_TOL = 0.005          # RDP 相对容差，占该信号量程的比例
PREDEC_FRAC = 0.3            # 预细化阈值 = PREDEC_FRAC * eps（来自实测注释）
QUANT_FRAC = 0.25            # 量化步长 <= eps/4：抽点误差已经接受了，再细是纯浪费
NOISE_K = 3.0                # eps 不低于 NOISE_K 倍噪声底：追噪声是在编码噪声
MAX_CAND = 20000             # 预细化的候选点上限，GUI 的交互全在这个集合上做
DT_COLLAPSE = 50.0           # dt 相对中位数塌掉这么多倍就报出来


# --------------------------------------------------------------- 数值小工具


def pow10_floor(v):
    """<= v 的最大 10 的整数次幂。q 取 10 的幂而不是 1/2/5，
    因为定点文本里只有 10 的幂才真的省字节。"""
    if not (v > 0.0) or not math.isfinite(v):
        return 1.0
    return 10.0 ** math.floor(math.log10(v))


def eng_exp(v):
    """把 |v| 落进 [1,1000) 的 3 的倍数指数。"""
    if v == 0.0 or not math.isfinite(v):
        return 0
    e = int(math.floor(math.log10(abs(v)) / 3.0)) * 3
    return max(min(e, 15), -18)


def eng_fmt(v, unit="", sig=6):
    """-> ('712.334', 'mV')。METRICS / EVENTS 用它，全精度但读得动。"""
    if v is None:
        return ("-", unit)
    if not math.isfinite(v):
        return ("nan" if math.isnan(v) else ("inf" if v > 0 else "-inf"), unit)
    if unit in ("", "%", "dB", "dBc", "deg", "x") or "/" in unit or unit is None:
        # 不能加词头的单位：原样打数
        return (_sigfmt(v, sig), unit or "")
    e = eng_exp(v)
    if e not in PREFIX:
        return (_sigfmt(v, sig), unit)
    return (_sigfmt(v / (10.0 ** e), sig), PREFIX[e] + unit)


def eng_str(v, unit="", sig=6):
    a, b = eng_fmt(v, unit, sig)
    return (a + " " + b).rstrip()


def _sigfmt(v, sig):
    """N 位有效数字。保留科学记数——log 轴跨 9 个 decade，定点写不下。"""
    if v == 0.0:
        return "0"
    # 整数部分有几位就至少要几位有效数字，否则 640 会被 %.2g 打成 6.4e2
    a = abs(v)
    if a >= 1.0:
        sig = max(sig, int(math.floor(math.log10(a))) + 1)
    s = "%.*g" % (sig, v)
    if "e" in s:                                   # 1e+10 -> 1e10，省两个字节
        m, e = s.split("e")
        s = m + "e" + ("-" if e[0] == "-" else "") + e.lstrip("+-").lstrip("0").rjust(1, "0")
    return s or "0"


def strip_num(s, dec):
    """定点格式化后掐掉无意义的尾零。方波/平信号上这一条省很多字节。"""
    t = "%.*f" % (dec, s)
    if dec > 0 and "." in t:
        t = t.rstrip("0").rstrip(".")
    if t in ("-0", "", "-"):
        t = "0"
    return t


# --------------------------------------------------------------- 数据模型


class Signal(object):
    __slots__ = ("name", "unit", "unit_src", "y", "vmin", "vmax", "vmin_at",
                 "vmax_at", "noise", "eps")

    def __init__(self, name, unit="", unit_src="unknown"):
        self.name = name
        self.unit = unit
        self.unit_src = unit_src          # declared | inferred | unknown
        self.y = array("d")
        self.vmin = self.vmax = 0.0
        self.vmin_at = self.vmax_at = 0
        self.noise = 0.0                  # 估出来的噪声底（自然单位）
        self.eps = 0.0                    # RDP 的绝对容差（自然单位）

    @property
    def rng(self):
        return self.vmax - self.vmin

    def __repr__(self):
        return "<Signal %s [%s] n=%d>" % (self.name, self.unit, len(self.y))


class Trace(object):
    """一列 x + 共享这列 x 的若干列 y。布局 B 会拆出多个 Trace。"""

    __slots__ = ("source", "xname", "xunit", "xunit_src", "x", "signals",
                 "kind", "kind_src", "xscale", "notes", "dt_med", "dt_min",
                 "dt_max", "index")

    def __init__(self, xname="x", index=0):
        self.source = ""
        self.xname = xname
        self.xunit = ""
        self.xunit_src = "unknown"
        self.x = array("d")
        self.signals = []
        self.kind = "unknown"
        self.kind_src = "unknown"
        self.xscale = "lin"
        self.notes = []
        self.dt_med = self.dt_min = self.dt_max = 0.0
        self.index = index

    def __len__(self):
        return len(self.x)

    def note(self, msg):
        if msg not in self.notes:          # 重跑 set_eps/predecimate 不该刷屏
            self.notes.append(msg)

    def __repr__(self):
        return "<Trace %s n=%d sig=%d kind=%s>" % (
            self.xname, len(self.x), len(self.signals), self.kind)


# --------------------------------------------------------------- CSV 解析


# 认得出的单位记号。白名单，认不出就**不当单位**——`V(vco_out)` 里的 vco_out
# 是网络名不是单位，猜错了整列的量纲就错了。
_UNIT_TOK = re.compile(
    r"^([yzafpnµumcdhkKMGTPE]?)"
    r"(V|A|s|S|Hz|W|F|H|C|J|N|T|K|Wb|ohm|Ohm|OHM|Ω|"
    r"dB|dBm|dBc|dBV|dBA|deg|degC|degF|rad|eV|bit|B|%)$")
_UNIT_EXTRA = frozenset("""
    V/s A/s V/us V/ns dBc/Hz V/sqrt(Hz) A/sqrt(Hz) V^2/Hz A^2/Hz
    none unitless dimensionless - 1 s^-1 1/s
""".split())


def _is_unit(tok):
    return bool(tok) and (tok in _UNIT_EXTRA or bool(_UNIT_TOK.match(tok)))


def _split_unit(name):
    """'V(out) (V)' -> ('V(out)', 'V')；没有可识别的单位后缀就返回 (name, None)。"""
    m = re.match(r"^(.*?)\s*\(\s*([A-Za-zΩµ°%/^0-9._-]{1,14})\s*\)\s*$",
                 name)
    if not m:
        return name.strip(), None
    base, unit = m.group(1).strip(), m.group(2).strip()
    if not base or base.endswith(("(", ",")):
        return name.strip(), None
    # 括号里是单位，还是 f(网络名) 的参数？只认白名单，或者 base 自己已经闭合了括号
    if _is_unit(unit) or base.endswith(")"):
        return base, unit
    return name.strip(), None


def _norm(name):
    base, _ = _split_unit(name)
    return base.strip().strip('"').strip().lower()


def _unit_of(name, declared):
    """-> (unit, src)。src=declared 才是可信的；inferred 会在输出里带 ?。"""
    if declared:
        return declared, "declared"
    m = _DECLARED_FN.match(name)
    if m:
        return m.group(1).lower().replace("db20", "dB20").replace("db10", "dB10"), \
            "declared"
    for rx, u in _INFERRED_FN:
        if rx.match(name):
            return u, "inferred"
    low = _norm(name)
    if low in _X_UNIT_HINT:
        return _X_UNIT_HINT[low], "inferred"
    return "", "unknown"


def _read_table(path_or_text, is_text=False):
    """-> (header:list[str], rows:iterator of list[str])。认逗号/分号/制表符。"""
    if is_text:
        fh = io.StringIO(path_or_text)
    else:
        fh = io.open(path_or_text, "r", encoding="utf-8-sig", errors="replace")
    with fh:
        lines = fh.read().splitlines()
    i = 0
    while i < len(lines) and (not lines[i].strip()
                              or lines[i].lstrip()[:1] in (";", "#")):
        i += 1
    if i >= len(lines):
        return [], []
    head = lines[i]
    delim = max(",;\t", key=head.count)
    if head.count(delim) == 0:
        delim = ","
    body = lines[i + 1:]
    quoted = '"' in head or any('"' in ln for ln in body[:5])
    if quoted:
        rdr = list(csv.reader([head] + body, delimiter=delim))
        return rdr[0], rdr[1:]
    return head.split(delim), [ln.split(delim) for ln in body if ln.strip()]


def _looks_numeric(cells):
    ok = bad = 0
    for c in cells:
        c = c.strip().strip('"').strip()
        if not c:
            continue
        try:
            float(c)
            ok += 1
        except ValueError:
            bad += 1
    return ok >= bad and ok > 0


def _find_x_columns(names, layout, xcols):
    """-> 每条 trace 的 x 列下标。布局 B 的判据见 wave-spec 第 6 节。"""
    if xcols:
        return sorted(set(xcols)), "declared"
    norm = [_norm(n) for n in names]
    first = norm[0]
    cand = [0]
    for j in range(1, len(norm)):
        if norm[j] == first or (first in X_NAMES and norm[j] in X_NAMES):
            cand.append(j)
    if layout == "b":
        return (cand if len(cand) > 1 else list(range(0, len(norm), 2))), "forced"
    if layout == "a":
        return [0], "forced"
    return (cand, "dup-name") if len(cand) > 1 else ([0], "single")


def parse_csv(path, layout="auto", xcols=None, text=None):
    """ViVA 导出的 CSV -> [Trace]。布局 A（共享 x）和布局 B（每 trace 自带 x）都吃。

    脏数据全部**报出来而不是静默处理**：重复 t / 非单调 / NaN / inf / 空单元 /
    dt 坍缩，每一样都进 trace.notes，最后印在 .wv 头里。
    """
    names, rows = _read_table(text if text is not None else path, is_text=text is not None)
    if not names:
        raise ValueError("空文件或读不出表头: %s" % path)
    names = [n.strip().strip('"').strip() for n in names]
    while names and not names[-1]:                       # ViVA 的尾逗号
        names.pop()
    ncol = len(names)

    unit_row = None
    if rows and not _looks_numeric(rows[0][:ncol]):
        unit_row = [c.strip().strip('"').strip() for c in rows[0][:ncol]]
        rows = rows[1:]
        unit_row += [""] * (ncol - len(unit_row))

    xidx, xsrc = _find_x_columns(names, layout, xcols)
    blocks = []
    for k, xi in enumerate(xidx):
        end = xidx[k + 1] if k + 1 < len(xidx) else ncol
        blocks.append((xi, [j for j in range(xi + 1, end)]))
    blocks = [b for b in blocks if b[1]]
    if not blocks:
        raise ValueError("没有找到任何 y 列: %s" % path)

    src = os.path.basename(path) if text is None else "<text>"
    traces = []
    for bi, (xi, yidx) in enumerate(blocks):
        base, declared = _split_unit(names[xi])
        if unit_row and unit_row[xi]:
            declared = unit_row[xi]
        tr = Trace(base or "x", index=bi)
        tr.source = src
        tr.xunit, tr.xunit_src = _unit_of(names[xi], declared)
        for j in yidx:
            nb, nd = _split_unit(names[j])
            if unit_row and unit_row[j]:
                nd = unit_row[j]
            u, us = _unit_of(names[j], nd)
            tr.signals.append(Signal(nb or ("c%d" % j), u, us))
        _fill_trace(tr, rows, xi, yidx)
        if len(tr.x) >= 2:
            traces.append(tr)
    if len(blocks) > 1:
        for tr in traces:
            tr.note("布局 B（%s）：这是第 %d/%d 条独立 trace"
                    % (xsrc, tr.index + 1, len(blocks)))
    return traces


def _fill_trace(tr, rows, xi, yidx):
    x = tr.x
    ys = [s.y for s in tr.signals]
    n_blank = [0] * len(yidx)
    n_nan = n_inf = n_dup = n_nonmono = n_badx = 0
    last = None
    dec_votes = 0
    prev_raw = None

    for r in rows:
        if xi >= len(r):
            continue
        c = r[xi].strip().strip('"').strip()
        if not c:
            continue
        try:
            xv = float(c)
        except ValueError:
            n_badx += 1
            continue
        if not math.isfinite(xv):
            n_badx += 1
            continue
        if prev_raw is not None and xv < prev_raw:
            dec_votes += 1
        prev_raw = xv
        vals = []
        ok = True
        for k, j in enumerate(yidx):
            cell = r[j].strip().strip('"').strip() if j < len(r) else ""
            if not cell:
                n_blank[k] += 1
                vals.append(None)
                continue
            try:
                v = float(cell)
            except ValueError:
                n_blank[k] += 1
                vals.append(None)
                continue
            if math.isnan(v):
                n_nan += 1
                vals.append(None)
            elif math.isinf(v):
                n_inf += 1
                vals.append(None)
            else:
                vals.append(v)
        if all(v is None for v in vals):
            ok = False
        if not ok:
            continue
        x.append(xv)
        for k, v in enumerate(vals):
            ys[k].append(v if v is not None else float("nan"))

    # 整体递减（反序扫描）-> 整条翻过来；局部递减 -> 当脏数据丢掉
    if len(x) > 2 and dec_votes > 0.5 * (len(x) - 1):
        x.reverse()
        for a in ys:
            a.reverse()
        tr.note("x 整体递减，已整条翻转为递增")

    # 严格递增过滤：重复点保留第一个，回头点直接丢
    keep = []
    last = None
    for i in range(len(x)):
        if last is None or x[i] > last:
            keep.append(i)
            last = x[i]
        elif x[i] == last:
            n_dup += 1
        else:
            n_nonmono += 1
    if len(keep) != len(x):
        tr.x = array("d", (x[i] for i in keep))
        for s, a in zip(tr.signals, ys):
            s.y = array("d", (a[i] for i in keep))
        ys = [s.y for s in tr.signals]
        x = tr.x

    # 空洞线性补，并且把补了多少个说出来
    for k, s in enumerate(tr.signals):
        n_fix = _interp_nan(x, s.y)
        if n_fix:
            tr.note("%s: %d 个空/NaN/inf 单元已线性补（首尾用最近值）"
                    % (s.name, n_fix))
    drop = [k for k, s in enumerate(tr.signals) if len(s.y) == 0 or _all_nan(s.y)]
    for k in reversed(drop):
        tr.note("%s: 整列无有效数据，已丢弃" % tr.signals[k].name)
        del tr.signals[k]

    if n_dup:
        tr.note("重复 x 点 %d 个，保留第一个" % n_dup)
    if n_nonmono:
        tr.note("非单调 x 点 %d 个，已丢弃（不排序——排序会把损坏的文件洗干净）"
                % n_nonmono)
    if n_badx:
        tr.note("x 列无法解析/非有限 %d 行，已丢弃" % n_badx)
    if n_nan:
        tr.note("NaN 单元 %d 个" % n_nan)
    if n_inf:
        tr.note("inf 单元 %d 个" % n_inf)


def _all_nan(a):
    for v in a:
        if not math.isnan(v):
            return False
    return True


def _interp_nan(x, y):
    n = len(y)
    idx = [i for i in range(n) if math.isnan(y[i])]
    if not idx:
        return 0
    good = [i for i in range(n) if not math.isnan(y[i])]
    if not good:
        return 0
    for i in idx:
        p = bisect.bisect_left(good, i)
        if p == 0:
            y[i] = y[good[0]]
        elif p >= len(good):
            y[i] = y[good[-1]]
        else:
            a, b = good[p - 1], good[p]
            w = (x[i] - x[a]) / (x[b] - x[a]) if x[b] != x[a] else 0.0
            y[i] = y[a] + w * (y[b] - y[a])
    return len(idx)


# --------------------------------------------------------------- 轴/类型判定


def analyze(tr, kind=None, xscale=None):
    """定 kind、定 x 轴 lin/log、算每个信号的极值、体检 dt。都是确定性的。"""
    n = len(tr.x)
    if n < 2:
        raise ValueError("trace 点数不足: %d" % n)

    if kind:
        tr.kind, tr.kind_src = kind, "declared"
    else:
        low = _norm(tr.xname)
        tr.kind, tr.kind_src = "unknown", "unknown"
        for keys, k in _KIND_HINT:
            if low in keys:
                tr.kind, tr.kind_src = k, "inferred"
                break

    tr.xscale = xscale or _detect_xscale(tr.x)

    dts = [tr.x[i + 1] - tr.x[i] for i in range(n - 1)]
    sd = sorted(dts)
    tr.dt_med = sd[len(sd) // 2]
    tr.dt_min, tr.dt_max = sd[0], sd[-1]
    if tr.xscale == "log":
        # log 轴上 dt 本来就跨几个数量级，坍缩判据不成立；改报每 decade 多少点
        span = math.log10(tr.x[-1] / tr.x[0]) if tr.x[0] > 0 else 0.0
        if span > 0:
            tr.note("x 轴判为 log（%.2f decade，%.0f 点/decade）" % (span, n / span))
    else:
        _flag_dt_collapse(tr, dts)

    for s in tr.signals:
        y = s.y
        lo = hi = y[0]
        li = hi_i = 0
        for i in range(1, n):
            v = y[i]
            if v < lo:
                lo, li = v, i
            elif v > hi:
                hi, hi_i = v, i
        s.vmin, s.vmax, s.vmin_at, s.vmax_at = lo, hi, li, hi_i
        s.noise = noise_floor(tr.x, y)
    return tr


def noise_floor(x, y, sample=200000):
    """稳健估一下噪声底：三点弦偏差的 MAD。

    量的正是 RDP 量的那个东西（垂直弦偏差），所以两者可比。用中位数而不是均值，
    所以少数真有曲率的点不会把它抬起来。上限卡在量程的 10%——比这还大就不叫噪声了。
    """
    n = len(y)
    if n < 16:
        return 0.0
    step = max(1, (n - 2) // sample)
    d = []
    for i in range(1, n - 1, step):
        dx = x[i + 1] - x[i - 1]
        if dx <= 0:
            continue
        w = (x[i] - x[i - 1]) / dx
        d.append(abs(y[i] - (y[i - 1] + w * (y[i + 1] - y[i - 1]))))
    if len(d) < 8:
        return 0.0
    d.sort()
    med = d[len(d) // 2]
    est = med * 1.4826 / math.sqrt(1.5)
    rng = max(y) - min(y)
    return min(est, 0.1 * rng) if rng > 0 else 0.0


def set_eps(tr, tol=DEFAULT_TOL):
    """每个信号的绝对容差 eps = max(tol*量程, 噪声底)。

    为什么要拿噪声兜底：一条几乎平的通道，它的「量程」本身可能就是噪声。
    tol*量程 会小到比噪声还低好几个数量级，于是 RDP 一个点都删不掉——
    在编码噪声，不是在编码信号。抬到噪声底之上是唯一讲得通的做法，
    而且**抬了要说出来**（进 notes，最后印在 .wv 头里）。
    """
    for s in tr.signals:
        base = tol * s.rng
        s.eps = max(base, NOISE_K * s.noise)
        if NOISE_K * s.noise > base and base > 0:
            tr.note("%s: 噪声底 %s（×%g）高过 tol×量程 %s，eps 抬到 %s"
                    " —— 再多的点只是在编码噪声"
                    % (s.name, eng_str(s.noise, s.unit), NOISE_K,
                       eng_str(base, s.unit), eng_str(s.eps, s.unit)))
        elif s.eps <= 0:
            s.eps = float("inf")            # 恒定信号：两个端点就够
    return tr


def _detect_xscale(x):
    """相邻点比值近似恒定 -> log。只看正的 x。"""
    pos = [v for v in x if v > 0.0]
    if len(pos) < 8 or pos[-1] / pos[0] < 10.0:
        return "lin"
    r = [math.log(pos[i + 1] / pos[i]) for i in range(len(pos) - 1)
         if pos[i + 1] > pos[i]]
    if len(r) < 8:
        return "lin"
    m = sum(r) / len(r)
    if m <= 0:
        return "lin"
    var = sum((v - m) ** 2 for v in r) / len(r)
    return "log" if math.sqrt(var) / m < 0.05 else "lin"


def _flag_dt_collapse(tr, dts):
    """dt 突然坍缩本身就是 debug 信号（Spectre 收敛挣扎），要报不要跳过。"""
    med = tr.dt_med
    if med <= 0:
        return
    runs = []
    i = 0
    n = len(dts)
    while i < n:
        if dts[i] * DT_COLLAPSE < med:
            j = i
            mn = dts[i]
            while j < n and dts[j] * DT_COLLAPSE < med:
                mn = min(mn, dts[j])
                j += 1
            runs.append((tr.x[i], tr.x[min(j, n - 1)], med / mn, j - i))
            i = j
        else:
            i += 1
    for t0, t1, ratio, cnt in runs[:6]:
        tr.note("dt 坍缩 %.0fx（中位 %s -> 最小 %s）@ %s..%s，%d 步"
                % (ratio, eng_str(med, tr.xunit), eng_str(med / ratio, tr.xunit),
                   eng_str(t0, tr.xunit), eng_str(t1, tr.xunit), cnt))
    if len(runs) > 6:
        tr.note("…另有 %d 段 dt 坍缩未列出" % (len(runs) - 6))


# --------------------------------------------------------------- 预细化


def _predec_pass(x_n, ys, thr, progress=None):
    m = len(ys)
    keep = [0]
    last = 0
    step = max(1, x_n // 100)
    for i in range(1, x_n - 1):
        if progress is not None and i % step == 0:
            progress(i / float(x_n))
        for k in range(m):
            if abs(ys[k][i] - ys[k][last]) > thr[k]:
                if i - 1 > last:
                    keep.append(i - 1)               # 肩点
                keep.append(i)
                last = i
                break
    keep.append(x_n - 1)
    return keep


def predecimate(tr, tol=DEFAULT_TOL, max_cand=MAX_CAND, progress=None):
    """O(n) 一遍扫，扛 1e5–1e7 点。产出几千个候选点，之后所有交互只在候选集上做。

    规则：任一信号相对**上次保留点**移动超过 PREDEC_FRAC*eps 就保留，
    **并保留它前一个点（肩点）**。没有肩点，RDP 的弦会从平坦区直接切进凹坑，
    误差爆掉（来自 decimate_trans.py 的实测注释）。

    候选点数超上限就把阈值整体放大重扫。噪声通道会撞上这一条：随机游走每一步
    都能越过一个小阈值，不放大的话候选集等于原始点集，GUI 那条
    「交互只在候选集上做」的性能保证就没了。放大了会说出来。
    """
    n = len(tr.x)
    if n <= 3:
        return list(range(n))
    if any(s.eps <= 0 for s in tr.signals):
        set_eps(tr, tol)
    ys = [s.y for s in tr.signals]
    base = [PREDEC_FRAC * s.eps for s in tr.signals]
    k = 1.0
    keep = _predec_pass(n, ys, base, progress)
    for _ in range(12):
        if len(keep) <= max_cand:
            break
        k *= max(1.6, len(keep) / float(max_cand))
        keep = _predec_pass(n, ys, [t * k for t in base])
    if k > 1.0:
        tr.note("预细化阈值放大 %.0fx 才把候选点压到 %d（%d 原始点）"
                "—— 噪声主导的通道会这样" % (k, len(keep), n))
    if progress is not None:
        progress(1.0)
    return keep


# --------------------------------------------------------------- RDP


def _u_params(x, xscale):
    if xscale == "log":
        return [math.log(v) if v > 0 else float("-inf") for v in x]
    return x


def rdp(tr, cand, tol=DEFAULT_TOL, max_points=None, forced=(), xscale=None):
    """按**垂直距离**（不是到弦的垂线距离）做多信号联合 RDP。

    - 垂线距离在陡沿上欠分辨：陡沿处弦几乎竖直，垂足很近，误差被低估。
    - 联合：所有信号共用一套 x 栅格（SHAPE 段是一行一个 x），
      所以误差取各信号**归一化后**的最大值。
    - eps 下限就是 tol：低于 tol 的噪声不该吃点数预算。
    - 用最大误差优先的堆来切，所以 max_points 一到就停在「当前最优的那 N 个点」上，
      而不是递归到一半被截断。

    误差是在候选集上量的；候选集之外的点由预细化保证偏离 < 0.3*tol*range。
    真正的把关是 recon_error()——它拿**原始每一个点**核对。
    """
    if any(s.eps <= 0 for s in tr.signals):
        set_eps(tr, tol)
    xs = _u_params(tr.x, xscale or tr.xscale)
    ys = [s.y for s in tr.signals]
    # 按各自的 eps 归一化，误差判据统一成「>1 就还得切」
    inv = [(1.0 / s.eps if 0.0 < s.eps < float("inf") else 0.0) for s in tr.signals]

    cand = sorted(set(cand) | set(forced) | {0, len(tr.x) - 1})
    m = len(cand)
    if m <= 2:
        return list(cand)

    pos = {v: i for i, v in enumerate(cand)}
    brk = sorted({0, m - 1} | {pos[f] for f in forced if f in pos})

    def seg_err(a, b):
        """-> (max_norm_err, argmax_position_in_cand)"""
        if b - a < 2:
            return 0.0, -1
        ia, ib = cand[a], cand[b]
        xa, xb = xs[ia], xs[ib]
        dx = xb - xa
        best, bp = 0.0, -1
        if dx == 0.0:
            return 0.0, -1
        slopes = [(y[ib] - y[ia]) / dx for y in ys]
        for p in range(a + 1, b):
            ip = cand[p]
            t = xs[ip] - xa
            e = 0.0
            for k in range(len(ys)):
                if inv[k] == 0.0:
                    continue
                d = abs(ys[k][ip] - (ys[k][ia] + slopes[k] * t)) * inv[k]
                if d > e:
                    e = d
            if e > best:
                best, bp = e, p
        return best, bp

    heap = []
    seq = 0
    for a, b in zip(brk, brk[1:]):
        e, p = seg_err(a, b)
        if p >= 0:
            heapq.heappush(heap, (-e, seq, a, b, p))
            seq += 1
    kept = set(brk)
    budget = max_points if max_points else m
    while heap and len(kept) < budget:
        ne, _, a, b, p = heapq.heappop(heap)
        if -ne <= 1.0:                    # 归一化后 1.0 就是各信号自己的 eps
            break
        kept.add(p)
        for s0, s1 in ((a, p), (p, b)):
            e, q = seg_err(s0, s1)
            if q >= 0:
                heapq.heappush(heap, (-e, seq, s0, s1, q))
                seq += 1
    return [cand[i] for i in sorted(kept)]


# --------------------------------------------------------------- 量化 / 单位


class ColSpec(object):
    """一列 y 的输出规格：换什么单位、扣多少基线、量化到几位。"""

    __slots__ = ("sig", "label", "offset", "off_txt", "scale", "unit_out",
                 "q_nat", "q_txt", "dec", "lo_out", "hi_out")

    def __init__(self, sig, label):
        self.sig = sig
        self.label = label
        self.offset = 0.0        # 自然单位，先减
        self.off_txt = ""
        self.scale = 1.0         # 再乘，得到输出单位
        self.unit_out = sig.unit
        self.q_nat = 0.0
        self.q_txt = ""
        self.dec = 3
        self.lo_out = self.hi_out = 0.0

    def to_out(self, v):
        return (v - self.offset) * self.scale

    def from_out(self, v):
        return v / self.scale + self.offset

    def txt(self, v):
        return strip_num((v - self.offset) * self.scale, self.dec)


def _pick_offset(lo, hi):
    """基线扣除：只有当 DC 基座比摆幅大得多时才扣，否则白扣。

    取值取「量程内有效数字最少的那个整数」——mid 按自己数量级的 10 的幂取整。
    0.8 V 基线 + 88 mV 摆幅 -> 扣 800 mV，正好是人看图时脑子里的那个数。
    """
    rng = hi - lo
    mid = 0.5 * (lo + hi)
    if rng <= 0 or mid == 0.0:
        return 0.0
    g = pow10_floor(abs(mid))
    off = round(mid / g) * g
    if off == 0.0 or abs(off) < 2.0 * rng:
        return 0.0
    return off


def _cheapest_prefix(mag, q, slack=0):
    """选让每个数**最短**的那个词头。20 KB 是硬约束，这里省的是真字节。

    1.2 V / 量化 1 mV 写成 [V] 是 '1.200'（5 字节），写成 [mV] 是 '1200'（4 字节）。
    一个 600 行 4 列的 SHAPE 段，一列省 1 字节就是 600 字节。

    slack>0 时允许多花几个字节去换「自然」词头——x 轴用得上：SHAPE 里的时刻
    要跟 METRICS / EVENTS 里的 `41.203 ns` 对着看，两边同一个数量级省事得多。
    """
    cands = []
    for e in sorted(PREFIX):
        sc = 10.0 ** (-e)
        dec = 0 if q <= 0 else int(round(-math.log10(q * sc)))
        if dec < 0 or dec > 12:
            continue
        v = mag * sc
        if v > 0 and (v >= 1e7 or v < 1e-4):
            continue
        idig = 1 if v < 1 else int(math.floor(math.log10(v))) + 1
        cands.append((idig + dec + (1 if dec > 0 else 0), e))
    if not cands:
        return 0
    best_w = min(c[0] for c in cands)
    nat = eng_exp(mag) if mag > 0 else 0
    ok = [c for c in cands if c[0] <= best_w + slack]
    return min(ok, key=lambda c: (abs(c[1] - nat), c[0], abs(c[1])))[1]


def make_colspec(tr, tol, labels=None, keep_offset=True):
    if any(s.eps <= 0 for s in tr.signals):
        set_eps(tr, tol)
    specs = []
    for i, s in enumerate(tr.signals):
        cs = ColSpec(s, (labels[i] if labels else "c%d" % (i + 1)))
        cs.offset = _pick_offset(s.vmin, s.vmax) if keep_offset else 0.0
        lo, hi = s.vmin - cs.offset, s.vmax - cs.offset
        rng = hi - lo
        # 量化步长 <= eps/4：抽点误差已经接受了，量化噪声压到它的 1/4 就够
        eps = s.eps if math.isfinite(s.eps) else (rng or 1.0)
        cs.q_nat = pow10_floor(QUANT_FRAC * eps) if eps > 0 else 0.0
        e = _cheapest_prefix(max(abs(lo), abs(hi), rng, cs.q_nat), cs.q_nat)
        cs.scale = 10.0 ** (-e)
        cs.unit_out = (PREFIX.get(e, "") + s.unit) if s.unit else (
            ("x1e%d" % (-e)) if e else "")
        if s.unit_src == "inferred":
            cs.unit_out += "?"
        elif s.unit_src == "unknown":
            cs.unit_out = (cs.unit_out + " unknown").strip()
        q_out = cs.q_nat * cs.scale
        cs.dec = 0 if q_out <= 0 else max(0, min(12, int(round(-math.log10(q_out)))))
        cs.q_txt = eng_str(cs.q_nat, s.unit) if cs.q_nat > 0 else "-"
        cs.off_txt = eng_str(cs.offset, s.unit) if cs.offset else "0"
        cs.lo_out, cs.hi_out = lo * cs.scale, hi * cs.scale
        specs.append(cs)
    return specs


class XSpec(object):
    """x 列的输出规格。log 轴用有效数字而不是固定步长——decade 跨得太宽。"""

    __slots__ = ("name", "unit_out", "scale", "mode", "dec", "sig", "q_nat",
                 "q_txt", "lo_out", "hi_out")

    def __init__(self, tr, kept=None):
        self.name = tr.xname
        x = tr.x
        lo, hi = x[0], x[-1]
        span = hi - lo
        if tr.xscale == "log":
            # log 轴横跨好几个 decade，任何固定词头都会把一头压成 0。
            # 用自然单位 + N 位有效数字（允许科学记数），这是唯一写得下的形式。
            self.mode, self.sig, self.dec = "sig", 6, 0
            self.scale = 1.0
            self.q_nat, self.q_txt = 0.0, "6 位有效数字"
            e = 0
        else:
            dmin = span
            if kept and len(kept) > 1:
                dmin = min(x[kept[i + 1]] - x[kept[i]] for i in range(len(kept) - 1))
            # 步长取最小保留间距的一半：够粗以省字节，又保证两个保留点不会撞进同一格
            q = pow10_floor(min(max(dmin, 1e-300) * 0.5, span * 1e-5))
            self.mode, self.sig = "step", 0
            self.q_nat = q
            e = _cheapest_prefix(max(abs(lo), abs(hi), span), q, slack=2)
            self.scale = 10.0 ** (-e)
            q_out = q * self.scale
            self.dec = max(0, min(12, int(round(-math.log10(q_out)))))
            self.q_txt = eng_str(q, tr.xunit)
        self.unit_out = (PREFIX.get(e, "") + tr.xunit) if tr.xunit else (
            ("x1e%d" % (-e)) if e else "")
        if tr.xunit_src == "inferred":
            self.unit_out += "?"
        elif tr.xunit_src == "unknown":
            self.unit_out = (self.unit_out + " unknown").strip()
        self.lo_out, self.hi_out = lo * self.scale, hi * self.scale

    def to_out(self, v):
        return v * self.scale

    def txt(self, v):
        o = v * self.scale
        if self.mode == "sig":
            return _sigfmt(o, self.sig)
        return strip_num(o, self.dec)

    def val(self, v):
        """量化之后回到自然单位——recon 自检必须用这个值，不能用原值。"""
        return float(self.txt(v)) / self.scale


# --------------------------------------------------------------- 重建自检


class ReconErr(object):
    __slots__ = ("sig", "maxerr", "at", "rms", "pct")

    def __init__(self, sig, maxerr, at, rms, pct):
        self.sig, self.maxerr, self.at, self.rms, self.pct = sig, maxerr, at, rms, pct


def recon_error(tr, kept, xspec, specs):
    """拿**量化之后**的保留点线性重建，跟原始每一个点比。

    这就是 `.wv` 头里第二行那个自检。用量化后的值，所以报出来的是端到端误差，
    抽点误差 + 量化误差都算进去了——不是只算抽点那一半好看的。

    log 轴上按 **log(x) 线性**重建——因为 RDP 也是在 log(x) 里切的，
    两边必须用同一个重建假设，否则自检报的是另一条曲线的误差。
    .wv 头里会把这一条写出来，读的人（或模型）照着连线才对得上。
    """
    n = len(tr.x)
    logx = tr.xscale == "log"
    kx = [xspec.val(tr.x[i]) for i in kept]
    if logx:
        kx = [math.log(v) if v > 0 else -745.0 for v in kx]
    out = []
    for si, s in enumerate(tr.signals):
        cs = specs[si]
        ky = [cs.from_out(float(cs.txt(s.y[i]))) for i in kept]
        y = s.y
        maxe, at, acc = 0.0, tr.x[0], 0.0
        seg = 0
        nk = len(kept)
        for i in range(n):
            xv = tr.x[i]
            if logx:
                xv = math.log(xv) if xv > 0 else -745.0
            while seg + 2 < nk and kx[seg + 1] < xv:
                seg += 1
            x0, x1 = kx[seg], kx[min(seg + 1, nk - 1)]
            if x1 == x0:
                r = ky[seg]
            else:
                w = (xv - x0) / (x1 - x0)
                r = ky[seg] + w * (ky[min(seg + 1, nk - 1)] - ky[seg])
            d = y[i] - r
            acc += d * d
            if abs(d) > maxe:
                maxe, at = abs(d), tr.x[i]
        rng = s.rng
        out.append(ReconErr(s, maxe, at, math.sqrt(acc / n),
                            100.0 * maxe / rng if rng > 0 else 0.0))
    return out


# --------------------------------------------------------------- 一步到位


class Reduction(object):
    __slots__ = ("trace", "kept", "cand", "xspec", "specs", "err", "tol",
                 "max_points", "forced")

    def __init__(self, tr, kept, cand, xspec, specs, err, tol, max_points, forced):
        self.trace = tr
        self.kept = kept
        self.cand = cand
        self.xspec = xspec
        self.specs = specs
        self.err = err
        self.tol = tol
        self.max_points = max_points
        self.forced = forced

    @property
    def worst(self):
        return max(self.err, key=lambda e: e.pct) if self.err else None

    @property
    def ratio(self):
        return len(self.trace.x) / float(len(self.kept)) if self.kept else 0.0


def reduce_trace(tr, tol=DEFAULT_TOL, max_points=None, cand=None, forced=(),
                 keep_extrema=True, keep_offset=True, check=True):
    """预细化 -> 强制点 -> RDP -> 量化 -> 自检。GUI 只重跑后三步。

    check=False 跳过重建自检（O(n)）。二分点数预算时用得上：中间那十几次
    只关心字节数，最后定下来再算一次真正的误差。
    """
    set_eps(tr, tol)
    if cand is None:
        cand = predecimate(tr, tol)
    f = set(forced)
    if keep_extrema:
        for s in tr.signals:
            f.add(s.vmin_at)
            f.add(s.vmax_at)
    f.discard(None)
    f = {i for i in f if 0 <= i < len(tr.x)}
    kept = rdp(tr, cand, tol, max_points, f)
    specs = make_colspec(tr, tol, keep_offset=keep_offset)
    xspec = XSpec(tr, kept)
    err = recon_error(tr, kept, xspec, specs) if check else []
    return Reduction(tr, kept, cand, xspec, specs, err, tol, max_points, sorted(f))
