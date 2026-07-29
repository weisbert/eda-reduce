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
DT_COLLAPSE = 50.0           # dt 相对附近粗尺度塌掉这么多倍才看一眼
DT_RUN_MIN = 4               # 连着这么多小步才算「挣扎」，两三步是栅格拼接痕迹
DROP_WARN_FRAC = 0.01        # 丢掉的行超过这个比例就报 WARN，不再只写 note
BODY_Q = 0.001               # 主体量程取 0.1% ~ 99.9% 分位
BODY_SAMPLE = 50000          # 算分位时最多抽这么多点（排序要钱）
OUTLIER_K = 20.0             # 全量程比主体量程大这么多倍，就认定是离群点定的量程


# --------------------------------------------------------------- 数值小工具


_SUFFIX = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
           "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


_BUILD = None


def build_id():
    """-> 'eeaa52e 2026-07-28' / 'dev(工作树)'。**这份代码到底是哪一版。**

    隔离区没有 git，版本只能从打包时 `export-subst` 写进 `VERSION` 的那行读。
    工作树里那行还是 `$Format:%H$` 没被替换 —— 那就老实说 `dev(工作树)`，
    不要假装有版本号：「我到底跑的是不是新版」这个问题必须能被**回答**，
    不是被猜。真实教训：更新过一轮之后，判断「换上没有」靠的是
    「输出里有没有那条新 note」，全凭推断。
    """
    global _BUILD
    if _BUILD is not None:
        return _BUILD
    _BUILD = "unknown"
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir,
                     "VERSION")
    try:
        with io.open(p, encoding="utf-8", errors="replace") as fh:
            txt = fh.read()
    except (IOError, OSError):
        return _BUILD
    if "$Format" in txt:
        _BUILD = "dev(工作树)"
        return _BUILD
    sha = date = ""
    for ln in txt.splitlines():
        f = ln.split(None, 1)
        if len(f) == 2 and f[0] == "commit":
            sha = f[1].strip()[:9]
        elif len(f) == 2 and f[0] == "date":
            date = f[1].strip()[:10]
    _BUILD = (sha + " " + date).strip() or "unknown"
    return _BUILD


def parse_eng(s):
    """吃 '300n' / '1e-9' / '1.2G' / '-5m'。手打时间范围时工程记数是常态。

    （`plot_digitize` 里有一份同样的实现。没有合并是刻意的：那个工具要能单独
    scp 出去跑，`wave_core` 也是——为省 6 行把两个逃生舱绑在一起不划算。）
    """
    s = s.strip()
    m = re.match(r"^([-+]?[0-9.]+(?:[eE][-+]?\d+)?)\s*([fpnuµmkKMGT]?)$", s)
    if not m:
        return float(s)
    return float(m.group(1)) * _SUFFIX.get(m.group(2), 1.0)


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
                 "vmax_at", "noise", "eps", "cycles", "body_lo", "body_hi",
                 "n_out")

    def __init__(self, name, unit="", unit_src="unknown"):
        self.name = name
        self.unit = unit
        self.unit_src = unit_src          # declared | inferred | unknown
        self.y = array("d")
        self.vmin = self.vmax = 0.0
        self.vmin_at = self.vmax_at = 0
        self.noise = 0.0                  # 估出来的噪声底（自然单位）
        self.eps = 0.0                    # RDP 的绝对容差（自然单位）
        self.cycles = 0                   # 绕中线的振荡周期数（analyze 里数的）
        self.body_lo = self.body_hi = 0.0  # 去掉离群点之后的量程（analyze 里量的）
        self.n_out = 0                    # 落在主体量程之外的点数

    @property
    def rng(self):
        return self.vmax - self.vmin

    @property
    def rng_body(self):
        """去掉两头 BODY_Q 之后的量程。**波形真正住在多宽的地方。**

        `rng` 是被最极端那一个点定的，一发浪涌电流就能把它抬高几百倍；
        而 tol、量化步长、预细化阈值全都是按量程的比例算的，于是整条波形
        被一个点挤成一条直线。判「量程是不是被离群点定死」用这个。
        """
        return self.body_hi - self.body_lo

    def __repr__(self):
        return "<Signal %s [%s] n=%d>" % (self.name, self.unit, len(self.y))


class Trace(object):
    """一列 x + 共享这列 x 的若干列 y。布局 B 会拆出多个 Trace。"""

    __slots__ = ("source", "xname", "xunit", "xunit_src", "x", "signals",
                 "kind", "kind_src", "xscale", "notes", "warns", "dt_med",
                 "dt_min", "dt_max", "index", "window", "extra", "picks")

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
        self.warns = []
        self.dt_med = self.dt_min = self.dt_max = 0.0
        self.index = index
        self.window = None                # (lo, hi, 原文件 lo, 原文件 hi)
        # 附加段（现在只有 --demod 的 [CYCLES]）。**跟着同一份文档走** ——
        # 一键复制是硬要求，不许把一次分析拆成几个文件
        self.extra = []
        # 代表性周期的原始样点（--demod 填）。GUI 要画得出来 ——
        # `[CYCLES]` 段是输出里唯一一块**没有任何画面**的东西，
        # 而它恰恰是判断「波形失真没失真」该看的那块
        self.picks = []

    def __len__(self):
        return len(self.x)

    def note(self, msg):
        if msg not in self.notes:          # 重跑 set_eps/predecimate 不该刷屏
            self.notes.append(msg)

    def clone(self):
        """深拷一份（x 和每列 y 都复制）。GUI 里切「只压当前视窗」要用：
        切窗口是就地改的，原始那份得留着，不然关掉开关就回不去了。"""
        t = Trace(self.xname, index=self.index)
        t.source, t.xunit, t.xunit_src = self.source, self.xunit, self.xunit_src
        t.kind, t.kind_src, t.xscale = self.kind, self.kind_src, self.xscale
        t.x = array("d", self.x)
        t.notes = list(self.notes)
        t.picks = list(self.picks)
        t.warns = list(self.warns)
        t.window = self.window
        t.dt_med, t.dt_min, t.dt_max = self.dt_med, self.dt_min, self.dt_max
        for s in self.signals:
            d = Signal(s.name, s.unit, s.unit_src)
            d.y = array("d", s.y)
            # analyze() 的结果也要带上：**克隆就该是克隆**。
            # 只复制 y 的话，拿到的是一个 rng=0 的壳子，
            # 而 rng=0 会让 find_cycles / set_eps 静默什么都不做 —— 不报错，只是没结果。
            d.vmin, d.vmax = s.vmin, s.vmax
            d.vmin_at, d.vmax_at = s.vmin_at, s.vmax_at
            d.noise, d.eps, d.cycles = s.noise, s.eps, s.cycles
            d.body_lo, d.body_hi, d.n_out = s.body_lo, s.body_hi, s.n_out
            t.signals.append(d)
        return t

    def warn(self, msg):
        """比 note 高一级：**输入本身不足以支撑下面的数**。

        note 是「我做了什么」，warn 是「你手上这份数据别照着往下推」。
        分开是因为 note 有十几条，重要的那条埋在里面等于没说 ——
        72% 的行被丢掉只写进 note，人就会拿着废数据往下分析。
        """
        if msg not in self.warns:
            self.warns.append(msg)

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


# ViVA 括号里写的是**量的符号**，不是单位：电压写 `(V)`、电流写 `(I)`。
# `(V)` 碰巧对（伏特也是 V），`(I)` 就不对了（电流的单位是 A）。
# 一个字母引发的连锁：单位不认识 -> `; tran` 不在名字末尾 -> 横轴认不出来 ->
# kind=unknown -> **一条 METRICS 都没有**。实测在真实电流波形上踩到。
# 只放**见过的**；没见过的别猜，让它落到 unknown 去（下面的分析名识别不受影响）。
_VIVA_QUANTITY = {"I": "A"}


def _is_unit(tok):
    return bool(tok) and (tok in _UNIT_EXTRA or tok in _VIVA_QUANTITY
                          or bool(_UNIT_TOK.match(tok)))


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
        u = _VIVA_QUANTITY.get(declared)
        if u:
            # 文件里写的是量的符号，单位是我们换算出来的 —— 算 inferred 不算 declared
            return u, "inferred"
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


def _sniff_delim(head, body):
    """认分隔符。**不能只数表头里出现几次**——ViVA 的列名自己就带分号
    （`v /gmp; tran (V) X`），只数表头会判成分号，然后整个数据区一行都切不出数，
    最后表现成「一条 trace 都没解析出来」。

    判据换成「哪个候选能把数据行切成数字格」：数据行是纯数字的，切错了就切不出数。
    表头只用来定列数、并排除根本没出现过的候选（欧洲那种 `;` 分隔 + `0,5` 小数的
    文件靠这一条不会被逗号骗走）。
    """
    probe = [ln for ln in body if ln.strip()][:20]
    best, best_score = None, 0.0
    for d in (",", ";", "\t"):
        if d not in head:
            continue
        ncol = head.count(d) + 1
        score = 0.0
        for ln in probe:
            cells = ln.split(d)
            n = sum(1 for c in cells if _isnum(c))
            score += n / float(max(ncol, len(cells)))
        if probe:
            score /= len(probe)
        if score > best_score:
            best, best_score = d, score
    if best is not None:
        return best
    # 数据区是空的、或整块都不是数（只有表头 / 纯文本表）—— 退回数表头
    d = max(",;\t", key=head.count)
    return d if head.count(d) else ","


def _read_table(path_or_text, is_text=False):
    """-> (header:list[str], rows:list[list[str]], delim)。认逗号/分号/制表符。"""
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
        return [], [], ","
    head = lines[i]
    body = lines[i + 1:]
    delim = _sniff_delim(head, body)
    quoted = '"' in head or any('"' in ln for ln in body[:5])
    if quoted:
        rdr = list(csv.reader([head] + body, delimiter=delim))
        return rdr[0], rdr[1:], delim
    return head.split(delim), [ln.split(delim) for ln in body if ln.strip()], delim


def _isnum(c):
    c = c.strip().strip('"').strip()
    if not c:
        return False
    try:
        float(c)
        return True
    except ValueError:
        return False


def _looks_numeric(cells):
    ok = bad = 0
    for c in cells:
        if not c.strip().strip('"').strip():
            continue
        if _isnum(c):
            ok += 1
        else:
            bad += 1
    return ok >= bad and ok > 0


# ViVA 的「Export CSV」按 trace 成对写列：`<表达式> X` 是横轴，`<表达式> Y` 是纵轴，
# 两列名字除了后缀一模一样。表达式里还带分析名和单位：`v /gmp; tran (V)`。
_VIVA_XY = re.compile(r"^(.*\S)[ \t_]([XY])$")
_VIVA_ANALYSIS = re.compile(r";\s*([A-Za-z][A-Za-z0-9_]*)\s*$")
# 分析名 -> 规范轴名。**只列横轴能确定是什么的**：pss 在 ViVA 里既可能出时域也可能
# 出频域，不猜——认不出就留 unknown，让人用 --kind / --unit x= 定。
_VIVA_AXIS = {"tran": "time", "ac": "freq", "sp": "freq", "noise": "freq",
              "stb": "freq", "xf": "freq", "pac": "freq", "pxf": "freq",
              "pnoise": "freq", "dc": "dc", "dcop": "dc"}


def _viva_xy(names):
    """ViVA 成对导出 -> ([去掉 X/Y 后缀的名字], [x 列下标])，不成对就返回 None。

    要求**每一列**都配得上对，否则不认——认错了整张图的横轴就读反了。
    """
    if len(names) < 2 or len(names) % 2:
        return None
    bases, tags = [], []
    for n in names:
        m = _VIVA_XY.match(n.strip())
        if not m:
            return None
        bases.append(m.group(1).strip())
        tags.append(m.group(2))
    for k in range(0, len(names), 2):
        if tags[k] != "X" or tags[k + 1] != "Y" or bases[k] != bases[k + 1]:
            return None
    return bases, list(range(0, len(names), 2))


def _viva_rewrite(names):
    """ViVA 的 X/Y 列对 -> (改写后的列名, x 列下标, [每对一条说明])；认不出返回 None。

    X 列的名字换成 time / freq 这种规范轴名，因为原名括号里的单位是 **Y 的**：
    `v /gmp; tran (V) X` 里那个 V 是电压，横轴却是秒，照搬过去整条 x 轴的量纲就错了。
    换成规范轴名之后，下游那套「x 名字 -> 单位 / kind」的现成规则直接接手。
    """
    pair = _viva_xy(names)
    if not pair:
        return None
    bases, xidx = pair
    out, notes = [], []
    for k in xidx:
        base, unit = _split_unit(bases[k])
        if unit is None:
            # 括号里那玩意儿不在单位白名单里（ViVA 会写量的符号，比如 `(I)`）。
            # **分析名的识别不该被单位白名单挡住** —— 先把尾部的括号组摘掉再找
            # `; tran`。这样即使单位认不出来，横轴和 kind 照样是对的，
            # 只有单位老实留 unknown。少认一个单位是小事，丢掉整份 METRICS 是大事。
            m2 = re.match(r"^(.*\S)\s*\([^()]*\)\s*$", bases[k])
            if m2:
                base = m2.group(1)
        m = _VIVA_ANALYSIS.search(base)
        ana = m.group(1) if m else ""
        axis = _VIVA_AXIS.get(ana.lower(), "")
        net = base[:m.start()].strip() if (m and axis) else base
        out.append(axis or "x")
        out.append(("%s (%s)" % (net, unit)) if unit else (net or base))
        if axis:
            notes.append("ViVA X/Y 列对：`%s` 按分析名 `%s` 把横轴认成 %s"
                         % (net or base, ana, axis))
        else:
            notes.append("ViVA X/Y 列对：`%s` 认不出分析类型，横轴单位未知"
                         "（用 --kind / --unit x=s 声明）" % base)
    return out, xidx, notes


def _find_x_columns(names, layout, xcols, viva_x=None):
    """-> 每条 trace 的 x 列下标。布局 B 的判据见 wave-spec 第 6 节。"""
    if xcols:
        return sorted(set(xcols)), "declared"
    if layout == "a":
        return [0], "forced"
    if viva_x:
        return viva_x, "ViVA X/Y"
    norm = [_norm(n) for n in names]
    first = norm[0]
    cand = [0]
    for j in range(1, len(norm)):
        if norm[j] == first or (first in X_NAMES and norm[j] in X_NAMES):
            cand.append(j)
    if layout == "b":
        return (cand if len(cand) > 1 else list(range(0, len(norm), 2))), "forced"
    return (cand, "dup-name") if len(cand) > 1 else ([0], "single")


def parse_csv(path, layout="auto", xcols=None, text=None):
    """ViVA 导出的 CSV -> [Trace]。布局 A（共享 x）和布局 B（每 trace 自带 x）都吃。

    脏数据全部**报出来而不是静默处理**：重复 t / 非单调 / NaN / inf / 空单元 /
    dt 坍缩，每一样都进 trace.notes，最后印在 .wv 头里。
    """
    names, rows, delim = _read_table(text if text is not None else path,
                                     is_text=text is not None)
    if not names:
        raise ValueError("空文件或读不出表头: %s" % path)
    names = [n.strip().strip('"').strip() for n in names]
    while names and not names[-1]:                       # ViVA 的尾逗号
        names.pop()
    ncol = len(names)

    # ViVA 的 X/Y 列对。人给了 --xcols / --layout a 就听人的，不自作主张
    viva_x, viva_notes = None, []
    if not xcols and layout != "a":
        rw = _viva_rewrite(names)
        if rw:
            names, viva_x, viva_notes = rw

    unit_row = None
    if rows and not _looks_numeric(rows[0][:ncol]):
        unit_row = [c.strip().strip('"').strip() for c in rows[0][:ncol]]
        rows = rows[1:]
        unit_row += [""] * (ncol - len(unit_row))

    xidx, xsrc = _find_x_columns(names, layout, xcols, viva_x)
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
        if viva_x and bi < len(viva_notes):
            tr.note(viva_notes[bi])
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
    if not traces:
        # 空手而归几乎总是**切错列**（列名里带分隔符是头号原因），
        # 静默返回 [] 会在上层变成一句没头没脑的 IndexError。把证据摆出来。
        raise ValueError(
            "一条 trace 都没解析出来（每条至少要 2 个有效点）: %s\n"
            "  分隔符判为 %r；表头 %d 列: %s\n"
            "  数据第一行切成 %d 格: %s\n"
            "  列名里带分隔符时，可以用 --xcols 指定 x 列，或把表头改干净再导。"
            % (path, delim, ncol, " | ".join(names[:6]),
               len(rows[0]) if rows else 0,
               " | ".join(c.strip() for c in rows[0][:6]) if rows else "(无数据行)"))
    return traces


def _sig_digits(s):
    """'1.6377e-06' -> 5。数的是**写下来的**有效数字位数。

    用来判「x 列是不是按有效数字导出的」。`%g` 会把尾零去掉，所以单看一行会低估，
    取一批行的**最大值**才是导出精度。
    """
    s = s.strip().strip('"').strip().lstrip("+-")
    for sep in ("e", "E"):
        if sep in s:
            s = s.split(sep, 1)[0]
            break
    s = s.replace(".", "").lstrip("0").rstrip("0")
    return len(s)


def _fill_trace(tr, rows, xi, yidx):
    x = tr.x
    ys = [s.y for s in tr.signals]
    n_blank = [0] * len(yidx)
    n_nan = n_inf = n_dup = n_nonmono = n_badx = n_noxcell = 0
    n_rows = 0
    x_digits = 0
    last = None
    dec_votes = 0
    prev_raw = None

    for ri, r in enumerate(rows):
        n_rows += 1
        if xi >= len(r):
            n_noxcell += 1
            continue
        c = r[xi].strip().strip('"').strip()
        if not c:
            n_noxcell += 1
            continue
        if not (ri & 63):                  # 抽 1/64 行估导出精度，全量算是白花钱
            d = _sig_digits(c)
            if d > x_digits:
                x_digits = d
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
    _warn_on_drops(tr, n_rows, n_dup, n_nonmono, n_badx, n_noxcell, x_digits)


def _warn_on_drops(tr, n_rows, n_dup, n_nonmono, n_badx, n_noxcell, x_digits):
    """丢掉的行占比过高就**大声说**，并且尽量说清成因和怎么修。

    这条闸门是拿真实文件换来的：一份 423153 行的 ViVA 导出里 72% 是重复时间戳
    （x 列只有 5 位有效数字，t≈1.6 µs 处分辨率只剩 100 ps，而求解器步长约 5 ps），
    工具照规则「重复点保留第一个」，等于把 5 GHz 的振荡按 10 GS/s 抽了样 ——
    混叠出来的拍频包络看着特别像真实的起振包络。当时这件事只写进了一条 note，
    人就拿着废数据往下分析了。北极星那条「丢一个模型需要的数就是失败」说的正是这个。
    """
    dropped = n_dup + n_nonmono + n_badx + n_noxcell
    if not n_rows or dropped <= DROP_WARN_FRAC * n_rows:
        return
    kept = len(tr.x)
    bits = []
    for n, what in ((n_dup, "重复时间戳"), (n_nonmono, "非单调"),
                    (n_badx, "x 无法解析"), (n_noxcell, "x 单元为空")):
        if n:
            bits.append("%s %d" % (what, n))
    tr.warn("原始 %d 行里丢了 %d 行（%.1f%%：%s），只有 %d 个点进了下面的分析。"
            % (n_rows, dropped, 100.0 * dropped / n_rows, "、".join(bits), kept))
    if n_dup <= DROP_WARN_FRAC * n_rows:
        return
    # 重复时间戳占大头 —— 这几乎总是导出精度的锅，而且是**可判定**的：
    # x 列写下来只有 N 位有效数字，末位就是那一带的时间分辨率。
    quant = step = 0.0
    top = max(abs(tr.x[0]), abs(tr.x[-1])) if len(tr.x) >= 2 else 0.0
    if 0 < x_digits <= 9 and top > 0:
        quant = 10.0 ** (math.floor(math.log10(top)) - x_digits + 1)
        step = (tr.x[-1] - tr.x[0]) / float(max(1, n_rows - 1))
    # 抽样比要报**最坏那一带**的，不是全文件平均：分辨率随 |x| 变粗，
    # 平均值会把末段 20:1 的伤害稀释成 4:1，读的人就低估了
    if quant > 0 and step > 0:
        ratio, where = quant / step, "%s 那一带" % eng_str(top, tr.xunit, 2)
    else:
        ratio, where = n_rows / float(max(1, kept)), "平均"
    tr.warn("重复的那部分等于把波形抽了样（%s约 %.0f:1），**高频内容会混叠**："
            "抽样后冒出来的拍频包络看着很像真实的起振/幅度包络，别照着它下结论。"
            % (where, ratio))
    if quant > 0:
        tr.warn("成因在导出侧：x 列最多只有 %d 位有效数字，%s 处的时间分辨率"
                "只有 %s，而平均步长约 %s。重导时把 CSV 的精度调到 12 位以上"
                "（OCEAN: ocnPrint ... ?precision 12），这几行消失就是修好了。"
                % (x_digits, eng_str(top, tr.xunit, 2),
                   eng_str(quant, tr.xunit, 3), eng_str(step, tr.xunit, 3)))


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


def slice_trace(tr, lo=None, hi=None):
    """截出 [lo, hi] 的子区间，**就地改** tr，并记下原文件跨度。

    存在的理由是预算：20 KB 摊在 2 µs / 2500 个振荡周期上是 1.6 点/周期，
    画出来必然不像正弦；同样 20 KB 摊在 20 ns / 100 个周期上是 6 点/周期，
    看得清清楚楚。**窗口不是省事，是把分辨率花在你要看的地方。**

    代价是 `.wv` 从此有两种语义（整条 / 一段），所以 `tr.window` 一定要传到头部
    声明出来 —— 模型把窗口末态当成仿真末态读，结论就全歪了。
    """
    if not tr.x:
        return tr
    n_full = len(tr.x)
    full = (tr.x[0], tr.x[-1])
    lo = full[0] if lo is None else lo
    hi = full[1] if hi is None else hi
    if lo > hi:
        lo, hi = hi, lo
    i0 = bisect.bisect_left(tr.x, lo)
    i1 = bisect.bisect_right(tr.x, hi)
    if i1 - i0 < 2:
        raise ValueError("窗口 %s..%s 里只有 %d 个点（整条 %s..%s，共 %d 点）"
                         % (eng_str(lo, tr.xunit, 4), eng_str(hi, tr.xunit, 4),
                            i1 - i0, eng_str(full[0], tr.xunit, 4),
                            eng_str(full[1], tr.xunit, 4), len(tr.x)))
    if i1 - i0 == len(tr.x):
        return tr                          # 窗口盖住了全长，不算窗口
    tr.x = array("d", tr.x[i0:i1])
    for s in tr.signals:
        s.y = array("d", s.y[i0:i1])
    tr.window = (tr.x[0], tr.x[-1], full[0], full[1])
    tr.note("只导出了窗口 %s..%s（原文件 %s..%s 的 %.2g%%，%d/%d 点）；"
            "METRICS 全部是**在这个窗口内**量的，settle/final/drift 这类末态量"
            "说的是窗口末态，不是仿真末态"
            % (eng_str(tr.x[0], tr.xunit, 4), eng_str(tr.x[-1], tr.xunit, 4),
               eng_str(full[0], tr.xunit, 4), eng_str(full[1], tr.xunit, 4),
               100.0 * (tr.x[-1] - tr.x[0]) / (full[1] - full[0] or 1.0),
               i1 - i0, n_full))
    return tr


def count_cycles(y, vmin, vmax, noise=0.0):
    """信号绕自己中线的过零次数 / 2 —— 大致有多少个振荡周期。

    纯计数，不做 FFT（变步长栅格上 FFT 的混叠风险不可见，见 wave-spec 第 8 节）。
    用中线而不是均值：起振过程里均值会被前半段的直流拖偏。

    滞回取 `max(5% 量程, 3×噪声底)`。只用 5% 量程不够：一路纯噪声的平信号，
    它的「量程」本身就是噪声的 ±4σ，5% 只有 0.4σ，实测 4000 个点能数出 703 个
    假周期（测试钉着这条）。拿噪声底当尺子才是对的——它量的正是同一个东西。
    """
    rng = vmax - vmin
    if rng <= 0 or len(y) < 4:
        return 0
    mid = 0.5 * (vmin + vmax)
    hyst = max(0.05 * rng, NOISE_K * noise)
    if hyst >= 0.5 * rng:                 # 滞回宽过半个量程：没有可数的振荡
        return 0
    state = 0
    n = 0
    for v in y:
        if state <= 0 and v > mid + hyst:
            state, n = 1, n + 1
        elif state >= 0 and v < mid - hyst:
            state, n = -1, n + 1
    return n // 2


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
        s.body_lo, s.body_hi = body_range(y)
        # 「离群」不能按分位数本身来数 —— 那样答案永远是 2*BODY_Q，是问法的
        # 同义反复。数的是**离主体一整个身位以上**的点，干净波形上它就是 0。
        w = s.body_hi - s.body_lo
        s.n_out = sum(1 for v in y if v < s.body_lo - w or v > s.body_hi + w)
        s.noise = noise_floor(tr.x, y)
        s.cycles = count_cycles(y, lo, hi, s.noise)

    if tr.xscale == "log":
        # log 轴上 dt 本来就跨几个数量级，坍缩判据不成立；改报每 decade 多少点
        span = math.log10(tr.x[-1] / tr.x[0]) if tr.x[0] > 0 else 0.0
        if span > 0:
            tr.note("x 轴判为 log（%.2f decade，%.0f 点/decade）" % (span, n / span))
    else:
        _flag_dt_collapse(tr, dts)
    return tr


def body_range(y, q=BODY_Q, sample=BODY_SAMPLE):
    """-> (lo, hi)：抽样后的 q ~ 1-q 分位。**波形主体住在哪一段。**

    抽样是因为这只是用来跟全量程比个数量级，不需要精确的分位数；
    1e7 点全排一遍要好几秒，抽 5 万点排一次是几十毫秒，结论一样。
    """
    n = len(y)
    if n < 32:
        return (min(y), max(y)) if n else (0.0, 0.0)
    step = max(1, n // sample)
    d = sorted(y[::step]) if step > 1 else sorted(y)
    m = len(d)
    k = int(m * q)
    return d[k], d[m - 1 - k]


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


def eps_range(tr, s):
    """-> (用来算 eps 的量程, 是不是改用了主体量程)。

    默认就是全量程。**除非全量程是被极少数离群点定的** —— 起振电流最典型：
    t=0 一发浪涌把 max 顶到 0.9 A，而整条波形真正住在 ±1.5 mA 里，
    量程差了 300 倍。tol、量化步长、预细化阈值全是量程的比例，于是：

      - 预细化阈值 0.3×tol×量程 比整条波形的摆幅还大 -> 候选点塌到 3 个，
        GUI 的 max-points 滑块**上限只剩 10**（`max(10, len(cand))` 的那个 10）；
      - 量化步长 0.25×tol×量程 = 1 mA，把 ±1.5 mA 的振荡压成三个台阶。

    出来的 .wv 是空的，而且看不出为什么空。改用主体量程之后浪涌那个点
    一点没丢（它照样在数据里、照样被强制保留、METRICS 还是全精度量的），
    只是不再由它一个人决定其余 20 万个点的分辨率。
    """
    if s.rng_body > 0 and s.rng > OUTLIER_K * s.rng_body:
        return s.rng_body, True
    return s.rng, False


def set_eps(tr, tol=DEFAULT_TOL):
    """每个信号的绝对容差 eps = max(tol*量程, 噪声底)。

    为什么要拿噪声兜底：一条几乎平的通道，它的「量程」本身可能就是噪声。
    tol*量程 会小到比噪声还低好几个数量级，于是 RDP 一个点都删不掉——
    在编码噪声，不是在编码信号。抬到噪声底之上是唯一讲得通的做法，
    而且**抬了要说出来**（进 notes，最后印在 .wv 头里）。

    量程本身也可能是假的（被离群点定的），见 `eps_range`。同样要说出来。
    """
    for s in tr.signals:
        rng, robust = eps_range(tr, s)
        if robust:
            tr.note("%s: 全量程 %s 是被 %d 个离群点定的（占 %.3g%%），"
                    "主体只有 %s —— 差 %.0f 倍。tol / 量化步长 / 预细化阈值"
                    "都改按**主体量程**算，否则那几个点会把其余 %d 个点压成直线。"
                    "离群点本身一点没丢：它还在数据里，METRICS 也还是全精度量的"
                    % (s.name, eng_str(s.rng, s.unit), s.n_out,
                       100.0 * s.n_out / max(1, len(s.y)),
                       eng_str(s.rng_body, s.unit), s.rng / s.rng_body,
                       len(s.y) - s.n_out))
        base = tol * rng
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


def _local_dt_ref(dts, block=64, pct=0.9):
    """每个位置的「附近本来能有多粗」。

    两个坑都踩过：
    - 不能用**全局**中位数。Spectre 输出里 dt 本来就跨几个量级（沿上 15 ps、
      平坦区 2 ns），全局中位数被点数最多的那一档带跑 —— demo_tran 里
      2 ps 的坍缩相对全局中位只有 7.5x，根本触发不了阈值。
    - 也不能用局部**中位数**。时钟每 6.4 ns 就加密一次，局部中位数同样是 15 ps。
    所以取邻近块的**高分位**：那才是「这一带本来的粗尺度」。
    真正的判据不是这个比值，是后面那道「区间内信号动没动」。
    """
    n = len(dts)

    def q(a):
        s = sorted(a)
        return s[min(len(s) - 1, int(pct * (len(s) - 1) + 0.5))]

    if n < 3 * block:
        return [q(dts)] * n
    hi = []
    for a in range(0, n, block):
        hi.append(q(dts[a:a + block]))
    out = [0.0] * n
    for i in range(n):
        k = i // block
        out[i] = max(hi[max(0, k - 1):min(len(hi), k + 2)])
    return out


def _flag_dt_collapse(tr, dts):
    """dt 突然坍缩**而信号又没怎么动** —— 那才是收敛挣扎，是 debug 信号。

    光看 dt 坍缩会把自适应细化也报进来：求解器在陡沿上加密是它该干的事，
    不是毛病。区别在于沿上信号在剧烈变化，而收敛挣扎时求解器在一个
    什么都没发生的地方原地踏步。所以判据是**坍缩 + 无活动**，
    另一类只报个数，不占篇幅。
    """
    n = len(dts)
    if n < 4:
        return
    ref = _local_dt_ref(dts)
    runs = []
    i = 0
    while i < n:
        if dts[i] * DT_COLLAPSE < ref[i]:
            j, mn = i, dts[i]
            while j < n and dts[j] * DT_COLLAPSE < ref[j]:
                mn = min(mn, dts[j])
                j += 1
            runs.append((i, min(j, n - 1), ref[i], mn))
            i = j
        else:
            i += 1
    quiet, busy, tiny = [], 0, 0
    for a, b, rf, mn in runs:
        if b - a + 1 < DT_RUN_MIN:
            # 两三个点凑得近是栅格拼接痕迹，不是挣扎。挣扎是连着走很多小步
            tiny += 1
            continue
        act = 0.0
        for s in tr.signals:
            # 「动没动」要跟**噪声底**比，不能只跟量程比：一条几乎平的通道
            # 光噪声就能占掉量程的 10%，那不叫动
            band = max(0.005 * s.rng, 6.0 * s.noise)
            if band <= 0:
                continue
            seg = s.y[a:b + 1]
            act = max(act, (max(seg) - min(seg)) / band)
        if act < 1.0:
            quiet.append((a, b, rf, mn, act))
        else:
            busy += 1
    for a, b, rf, mn, act in quiet[:6]:
        tr.note("dt 坍缩 %.0fx（附近 %s -> 最小 %s）@ %s..%s，%d 步，"
                "期间所有信号的变化都没超出噪声带 —— 像是求解器在原地挣扎"
                % (rf / mn, eng_str(rf, tr.xunit), eng_str(mn, tr.xunit),
                   eng_str(tr.x[a], tr.xunit), eng_str(tr.x[b + 1], tr.xunit),
                   b - a + 1))
    if len(quiet) > 6:
        tr.note("…另有 %d 段同类 dt 坍缩未列出" % (len(quiet) - 6))
    if busy or tiny:
        tr.note("另有 %d 段加密在信号快变处（自适应步长的正常行为）、"
                "%d 处 2~%d 步的孤立细化（栅格拼接痕迹），都未列出"
                % (busy, tiny, DT_RUN_MIN - 1))


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


def _fit_cand(n, ys, base, max_cand, n0):
    """把阈值放大到候选点刚好落进 [0.6*max_cand, max_cand]。-> (放大倍数, 候选)

    原来是 `k *= max(1.6, len(keep)/max_cand)` 反复乘。那条假设「候选数 ∝ 1/阈值」，
    在噪声通道上大致成立，在**振荡**通道上完全不成立：阈值一旦越过正弦的
    单步变化量，候选数就断崖式塌到每周期两三个。实测 200000 点的 5 GHz 正弦，
    上限 20000 时它一步冲到 9448（每周期 3.76 点，max|err| 75%），
    而 49945 个候选是够用的（17 点/周期，0.5%）。**冲过头的那一半是白丢的分辨率。**

    改成对 log(k) 二分：塌得再陡也能收在上限底下一点点。
    """
    lo_k, hi_k = 1.0, 2.0
    best = None
    for _ in range(24):                       # 先找一个能压到上限以下的 hi_k
        keep = _predec_pass(n, ys, [t * hi_k for t in base])
        if len(keep) <= max_cand:
            best = (hi_k, keep)
            break
        lo_k, hi_k = hi_k, hi_k * 2.0
    if best is None:
        return hi_k, keep
    for _ in range(18):                       # 再往回逼近上限
        mid = math.sqrt(lo_k * hi_k)
        keep = _predec_pass(n, ys, [t * mid for t in base])
        if len(keep) > max_cand:
            lo_k = mid
        else:
            best, hi_k = (mid, keep), mid
        if len(best[1]) >= 0.6 * max_cand:
            break
    return best


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
    keep = _predec_pass(n, ys, base, progress)
    k = 1.0
    if len(keep) > max_cand:
        k, keep = _fit_cand(n, ys, base, max_cand, len(keep))
        # 候选集是**质量上限**，不只是性能参数：RDP 只能从候选点里挑，
        # 挑不到的形状后面再怎么加预算也回不来。所以这里要说清楚被压到了多少，
        # 而不是像原来那样说一句「噪声主导的通道会这样」——振荡通道也会撞上，
        # 而且对它来说这些候选点是**信号**不是噪声。
        tr.note("预细化阈值放大 %.0fx 才把候选点压到 %d（%d 原始点，上限 %d）。"
                "候选集是质量上限：RDP 只能从这些点里挑。振荡波形撞上这条时"
                "要么开大 --max-cand，要么用 --xrange 缩小窗口"
                % (k, len(keep), n, max_cand))
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

    # 这个内层循环是整个工具最热的地方：一次 reduce 要跑几百万次。
    # 恒定信号（inv==0）先筛掉，别在循环里每点判一次；单信号是最常见的形状，
    # 单独走一条没有内层循环的路。实测 6 路 20 万点从 4.8 s 降到 2.0 s。
    act = [(ys[k], inv[k]) for k in range(len(ys)) if inv[k] != 0.0]
    one = act[0] if len(act) == 1 else None

    def seg_err(a, b):
        """-> (max_norm_err, argmax_position_in_cand)"""
        if b - a < 2 or not act:
            return 0.0, -1
        ia, ib = cand[a], cand[b]
        xa = xs[ia]
        dx = xs[ib] - xa
        if dx == 0.0:
            return 0.0, -1
        best, bp = 0.0, -1
        if one is not None:                      # 单信号：省掉内层循环和拆包
            y, iv = one
            ya = y[ia]
            sl = (y[ib] - ya) / dx
            for p in range(a + 1, b):
                ip = cand[p]
                d = abs(y[ip] - (ya + sl * (xs[ip] - xa))) * iv
                if d > best:
                    best, bp = d, p
            return best, bp
        terms = [(y, y[ia], (y[ib] - y[ia]) / dx, iv) for y, iv in act]
        for p in range(a + 1, b):
            ip = cand[p]
            t = xs[ip] - xa
            e = 0.0
            for y, ya, sl, iv in terms:
                d = abs(y[ip] - (ya + sl * t)) * iv
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
        n = len(tr.x)
        for s in tr.signals:
            for i in (s.vmin_at, s.vmax_at):
                if i is None:
                    continue
                f.add(i)
                # **孤立尖峰的左右邻居也得留。** 只保留峰顶的话，重建会从峰顶
                # 直接连到几百个点之外，紧挨着峰的那个样点就被插成了峰值本身
                # —— 误差正好是整个峰高。实测：0.9 A 的单点浪涌（起振电流上
                # 的开机充电），只留峰顶时 max|err| 99.8%，带上邻居之后 0.16%。
                #
                # 但只对**孤立**的峰这么做。光滑波形的峰顶导数近于零，邻居和
                # 峰顶差不到一个 eps，留了纯属占位 —— 而点数是有预算的，
                # 占掉的正是 RDP 本该拿去补别处的那几个点（demo_tran 上无脑
                # 留邻居，误差反而从 1.93% 涨到 3.08%）。
                for j in (i - 1, i + 1):
                    if 0 <= j < n and abs(s.y[i] - s.y[j]) > s.eps:
                        f.add(j)
    f.discard(None)
    f = {i for i in f if 0 <= i < len(tr.x)}
    kept = rdp(tr, cand, tol, max_points, f)
    specs = make_colspec(tr, tol, keep_offset=keep_offset)
    xspec = XSpec(tr, kept)
    err = recon_error(tr, kept, xspec, specs) if check else []
    return Reduction(tr, kept, cand, xspec, specs, err, tol, max_points, sorted(f))
