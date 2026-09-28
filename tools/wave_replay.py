#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wave_replay.py — 把上一次仿真录下的节点电压，变成下一次仿真能直接 include 的激励。

    上一次（完整电路）             下一次（产生这些信号的模块已拿掉）
    psf.trn (SST2) ──wave_replay──▶ replay.scs + pwl/*.pwl ──include──▶ 同一个 DUT

replay.scs 里每个信号一行 vsource，**用层次名直接连到原来的 net 上**
（`I_top.blk.net\\<3\\>`），所以原理图一根线都不用改 —— ADE 里把它加进
Simulation Files → Definition Files 就行。

实测过的前提（Spectre 18.1）：
  * include 文件里的 vsource 能用层次名连到子电路内部的 net，转义的总线位也行
  * 理想源压得过原来的驱动 —— 所以没拆干净也不会报错，但那个驱动的电流是假的
  * simvisdbutil 能把 SST2 按原始时间点导成 CSV，时间是整数（库的时间单位），
    不丢精度；`-output /dev/stdout` 可以直接进管道，几 GB 的中间文件不落盘

抽点：每个信号独立做**流式斜率锥**（见 `Cone`）。保证每一个原始点离输出的
PWL 都不超过 `--tol`，不是「大致」。整段没动过的信号直接给 `dc=`。

依赖：**纯标准库**。要进隔离区，单文件传进去就能用。

    python3 wave_replay.py --list signal_list.csv --db psf/psf.trn -o replay
    python3 wave_replay.py --list signal_list.csv --csv exported.csv --timescale 1 -o replay
"""

import argparse
import csv
import datetime
import fnmatch
import io
import math
import os
import re
import subprocess
import sys

BUILD = "wave_replay 1"

DEFAULT_TOL = 1e-3          # 1 mV：比 LSB 级的控制电平、LDO 纹波都小一个量级
INLINE_MAX = 16             # 顶点不多于这个数就直接写进 wave=[...]，省一个文件
SIMVISDBUTIL = "simvisdbutil"

_ENG = {"a": 1e-18, "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6,
        "m": 1e-3, "k": 1e3, "meg": 1e6, "g": 1e9, "": 1.0}


def parse_eng(s):
    """'10u' / '1.5ns' / '2e-6' → 秒（或无量纲）。单位字母 s 可带可不带。"""
    m = re.match(r"^\s*([-+]?[0-9.]+(?:e[-+]?\d+)?)\s*(meg|[afpnumkg])?s?\s*$",
                 str(s), re.I)
    if not m:
        raise ValueError("看不懂的数: %r" % (s,))
    return float(m.group(1)) * _ENG[(m.group(2) or "").lower()]


# --------------------------------------------------------------------------
# 名字
# --------------------------------------------------------------------------

def norm_key(name):
    """跨三种写法对齐名字的键。

    同一个 net 在三处长得不一样：ADE 里 `/I_top/d<3>`，VCD 头里
    `d<3>`（scope 分开写），simvisdbutil 的 CSV 表头里 `I_top.\\d<3> `
    （转义 + 尾空格）。去掉反斜杠和空白、`<n>` 统一成 `[n]`、层级统一成 `/`。
    """
    k = name.replace("\\", "").replace(" ", "").replace("\t", "")
    k = k.replace("<", "[").replace(">", "]")
    return k.strip("/")


def spectre_node(comps, prefix=""):
    """ADE 路径分量 → Spectre 网表里的层次名。非 [A-Za-z0-9_] 字符逐个转义。"""
    esc = [re.sub(r"([^A-Za-z0-9_])", r"\\\1", c) for c in comps]
    if prefix:
        esc = [p for p in prefix.split(".") if p] + esc
    return ".".join(esc)


class Target(object):
    __slots__ = ("path", "kind", "comps", "key", "db", "dbtype", "status",
                 "col", "cone", "node")

    def __init__(self, path, kind="net"):
        self.path = path
        self.kind = kind
        self.comps = [c for c in path.strip().split("/") if c]
        self.key = norm_key("/".join(self.comps))
        self.db = None          # simvisdbutil 认的全名（点分）
        self.dbtype = None
        self.status = "pending"
        self.col = None
        self.cone = None
        self.node = None


def read_targets(path):
    """ADE Assembler 导出的 outputs CSV（有 Output 列），或者一行一个路径。"""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        text = fh.read()
    rows = [r for r in csv.reader(io.StringIO(text)) if any(c.strip() for c in r)]
    if not rows:
        return []
    head = [c.strip().lower() for c in rows[0]]
    out = []
    if "output" in head:
        io_ = head.index("output")
        it = head.index("type") if "type" in head else None
        for r in rows[1:]:
            if len(r) <= io_ or not r[io_].strip():
                continue
            kind = r[it].strip().lower() if it is not None and len(r) > it else "net"
            out.append(Target(r[io_].strip(), kind or "net"))
    else:
        out = [Target(r[0].strip()) for r in rows if r[0].strip()]
    seen, uniq = set(), []
    for t in out:
        if t.key not in seen:
            seen.add(t.key)
            uniq.append(t)
    return uniq


# --------------------------------------------------------------------------
# VCD 头：时间单位 + 库里有哪些信号
# --------------------------------------------------------------------------

_TS_UNIT = {"s": 1.0, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12,
            "fs": 1e-15, "as": 1e-18, "zs": 1e-21}


def parse_vcd_header(text):
    """→ (timescale 秒, [(scopes, name, type), ...])。只读到 $enddefinitions。

    simvisdbutil 写的是 `$timescale\\n    1e-06fs\\n$end`，也见过 `1 ns`
    分开写的，按 token 拼起来再认。
    """
    toks = text.split()
    ts, scopes, vars_ = None, [], []
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t == "$timescale":
            j = i + 1
            buf = ""
            while j < n and toks[j] != "$end":
                buf += toks[j]
                j += 1
            m = re.match(r"^([0-9.eE+-]+)([a-z]+)$", buf)
            if not m or m.group(2) not in _TS_UNIT:
                raise ValueError("认不出 $timescale: %r" % buf)
            ts = float(m.group(1)) * _TS_UNIT[m.group(2)]
            i = j
        elif t == "$scope":
            scopes.append(toks[i + 2])
            i += 3
        elif t == "$upscope":
            scopes.pop()
        elif t == "$var":
            # $var <type> <width> <id> <name> [<range>] $end
            typ, name = toks[i + 1], toks[i + 4]
            j = i + 5
            if j < n and toks[j].startswith("[") and toks[j] != "$end":
                name += toks[j]
            vars_.append((tuple(scopes), name.lstrip("\\"), typ))
            i = j
        elif t == "$enddefinitions":
            break
        i += 1
    return ts, vars_


def resolve(targets, vars_):
    """ADE 路径 → 库里的全名。库里多一层 testbench 顶层 scope 也认。

    精确匹配优先；只有「去掉顶层 scope 之后」才对得上的，要求唯一。
    """
    exact, loose = {}, {}
    for scopes, name, typ in vars_:
        full = list(scopes) + [name]
        db = ".".join(full)
        exact.setdefault(norm_key("/".join(full)), []).append((db, typ))
        if len(full) > 1:
            loose.setdefault(norm_key("/".join(full[1:])), []).append((db, typ))
    for t in targets:
        if t.kind != "net":
            t.status = "unsupported"
            continue
        hits = exact.get(t.key) or loose.get(t.key) or []
        reals = [h for h in hits if h[1] == "real"]
        if len(reals) == 1:
            t.db, t.dbtype = reals[0]
            t.status = "ok"
        elif len(reals) > 1:
            t.status = "ambiguous"
            t.db = " | ".join(h[0] for h in reals)
        elif hits:
            t.status = "logic"         # 只存了 0/1，没有电压
            t.db, t.dbtype = hits[0]
        else:
            t.status = "missing"


# --------------------------------------------------------------------------
# 流式斜率锥
# --------------------------------------------------------------------------

class Cone(object):
    """误差有上界的流式 PWL 抽点。

    从锚点 (t0, y0) 出发，每来一个点 i，能让它落在 ±tol 内的斜率是一个区间
    [(yi-tol-y0)/dti, (yi+tol-y0)/dti]。把所有区间求交：交集非空，就存在一条
    过锚点的直线同时照顾到这一段里**每一个**点。交集一空，就在上一个点处
    落一个顶点（斜率取交集里离弦最近的那个），以它为新锚点重开。

    和经典 swinging-door 的区别：SDT 把线段拉到快照点上，中间点并不保证
    在带内；这里斜率从交集里取，所以保证是逐点的。代价是顶点可能偏离原始点
    至多 tol —— 对激励无所谓。

    平台加速：值没变、且离锚点在 tol 内的一串点，它们的区间以 0 为中心逐个
    收缩（互相嵌套），只有最后一个起约束作用 —— 所以中间的直接跳过，
    只记时间。260 列里大部分是常数，这条决定了速度。
    """

    __slots__ = ("tol", "t0", "y0", "kmin", "kmax", "tp", "yp", "tq",
                 "out", "n", "ymin", "ymax", "dup")

    def __init__(self, tol):
        # 留一点余量给写文件时的舍入：顶点正好压在带边上，%.12g 一舍就可能
        # 越界 1e-10 量级 —— 测试逐点查的就是这个
        self.tol = tol * (1 - 1e-6)
        self.t0 = None
        self.out = []
        self.n = 0
        self.dup = 0
        self.tq = None          # 平台上被推迟的最后一个点的时间（值 = yp）

    def add(self, t, y):
        self.n += 1
        if self.t0 is None:
            self.t0, self.y0 = t, y
            self.tp, self.yp = t, y
            self.kmin, self.kmax = -math.inf, math.inf
            self.ymin = self.ymax = y
            self.out.append((t, y))
            return
        last_t = self.tq if self.tq is not None else self.tp
        if t <= last_t + max(1e-21, abs(t) * 1e-13):
            self.dup += 1       # 同一时刻两个值：保留先到的，PWL 不许时间回退
            return
        if y < self.ymin:
            self.ymin = y
        elif y > self.ymax:
            self.ymax = y
        if y == self.yp and self.tp != self.t0 and abs(y - self.y0) <= self.tol:
            self.tq = t
            return
        if self.tq is not None:
            tq, self.tq = self.tq, None
            self._step(tq, self.yp)
        self._step(t, y)

    def _step(self, t, y):
        dt = t - self.t0
        tol = self.tol
        lo = (y - tol - self.y0) / dt
        hi = (y + tol - self.y0) / dt
        nlo = lo if lo > self.kmin else self.kmin
        nhi = hi if hi < self.kmax else self.kmax
        if nlo <= nhi:
            self.kmin, self.kmax = nlo, nhi
            self.tp, self.yp = t, y
            return
        self._vertex()
        dt = t - self.t0
        self.kmin = (y - tol - self.y0) / dt
        self.kmax = (y + tol - self.y0) / dt
        self.tp, self.yp = t, y

    def _vertex(self):
        dtp = self.tp - self.t0
        k = (self.yp - self.y0) / dtp
        k = min(max(k, self.kmin), self.kmax)
        yv = self.y0 + k * dtp
        self.out.append((self.tp, yv))
        self.t0, self.y0 = self.tp, yv

    def finish(self):
        if self.t0 is None:
            return self.out
        if self.tq is not None:
            tq, self.tq = self.tq, None
            self._step(tq, self.yp)
        if self.tp != self.t0:
            self._vertex()
        return self.out

    def is_flat(self):
        return self.n > 0 and self.ymax - self.ymin <= 2 * self.tol


# --------------------------------------------------------------------------
# 数据源
# --------------------------------------------------------------------------



def db_header(tool, db, workdir):
    """只导 [0, 1fs] 的 VCD —— 头里有全部信号名和时间单位，几十 KB。

    落在输出目录而不是 /tmp：公司机的 /tmp 是公共的。
    """
    path = os.path.join(workdir, "_header.vcd")
    cmd = [tool, "-nocopyright", "-nolog", "-overwrite", "-quiet", db,
           "-vcd", "-range", "0:1fs", "-output", path]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       universal_newlines=True)
    if p.returncode != 0 or not os.path.exists(path):
        raise RuntimeError("simvisdbutil 取头失败 (rc=%d):\n%s%s"
                           % (p.returncode, p.stdout[-2000:], p.stderr[-2000:]))
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def db_rows(tool, db, signals, workdir, t_from=None, t_to=None):
    """simvisdbutil 的 CSV 直接走管道，不落盘（260 列 × 百万行是几 GB）。

    stderr 写文件而不是 PIPE：我们只读 stdout，stderr 那边要是写满了
    管道缓冲，双方会互相等死。
    """
    cmd = [tool, "-nocopyright", "-nolog", "-overwrite", "-quiet", db,
           "-csv", "-output", "/dev/stdout"]
    if t_from is not None and t_to is not None:
        cmd += ["-range", "%dfs:%dfs" % (math.floor(t_from * 1e15),
                                         math.ceil(t_to * 1e15))]
    for s in signals:
        cmd += ["-signal", s]
    err = open(os.path.join(workdir, "_simvisdbutil.err"), "w")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err,
                         universal_newlines=True, bufsize=1 << 20)
    p.errfile = err
    return p, p.stdout


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def _tail_match(col, full):
    """表头名是不是完整路径的「按层级对齐的后缀」。

    表头里写到哪一层并不固定：实测见过全路径，也见过只剩叶子名的。
    只要是按 `/` 边界对齐的后缀，就算同一个信号。
    """
    return col == full or full.endswith("/" + col)


def bind_columns(header, targets, requested=None):
    """CSV 表头 → 目标。返回是否按位置对齐（写进 REPORT，诊断用）。

    `--db` 模式下我们知道自己按什么顺序要的信号（`requested`），simvisdbutil
    按同样顺序出列 —— 所以优先**按位置**对齐，再逐列用 `_tail_match` 校验。
    校验不过才退回按名字找。

    只按名字找是会漏的：公司机上见过只有顶层 4 个信号对上、层级里的 256 个
    全部 nocolumn —— 表头的写法跟这里以为的不一样。
    """
    cols = [c.strip() for c in header]
    keys = [norm_key(c.replace(".", "/")) for c in cols]
    if requested is not None and len(cols) - 1 == len(requested):
        want = [norm_key(r.replace(".", "/")) for r in requested]
        if all(_tail_match(k, w) for k, w in zip(keys[1:], want)):
            pos = {r: i for i, r in enumerate(requested, 1)}
            for t in targets:
                if t.status == "ok":
                    t.col = pos[t.db]
            return True
    # 不能用 dict 去重：两个实例下都有叶子名 `en` 时，去重会把两个目标
    # 悄悄绑到同一列上 —— 数据看着齐全，其实是错的
    indexed = list(enumerate(keys[1:], 1))
    for t in targets:
        if t.status != "ok":
            continue
        full = norm_key(t.db.replace(".", "/")) if t.db else t.key
        hits = [ii for ii, kk in indexed if kk == full]
        if not hits:                            # 表头只写到某一层：找唯一的后缀
            hits = [ii for ii, kk in indexed
                    if _tail_match(kk, full) or kk.endswith("/" + t.key)]
        i = hits[0] if len(hits) == 1 else None
        if i is None:
            t.status = "nocolumn"
        else:
            t.col = i
    return False


def stream(lines, targets, timescale, tol, t_from=None, t_to=None):
    """逐行喂给各自的 Cone。返回 (读了多少行, 是否因 --to 提前停下)。"""
    live = [t for t in targets if t.col is not None]
    for t in live:
        t.cone = Cone(tol)
    cols = [t.col for t in live]
    cones = [t.cone for t in live]
    prev = [None] * len(live)
    rows = 0
    ts = timescale
    for line in lines:
        f = line.rstrip("\r\n").split(",")
        if len(f) < 2:
            continue
        try:
            t = float(f[0]) * ts
        except ValueError:
            continue
        if t_from is not None and t < t_from:
            continue
        if t_to is not None and t > t_to:
            return rows, True
        rows += 1
        for j, c in enumerate(cols):
            s = f[c]
            cone = cones[j]
            # 字符串都没变、且处在平台上：连 float() 都省了
            if s == prev[j] and cone.tp != cone.t0 and cone.tq is not None:
                cone.tq = t
                cone.n += 1
                continue
            prev[j] = s
            if not s or s in ("x", "z", "X", "Z"):
                continue
            cone.add(t, float(s))
    return rows, False


def fmt_t(t):
    return "%.15g" % t


def fmt_v(v):
    return "%.12g" % v


def emit(targets, outdir, opts, meta):
    """写 replay.scs + pwl/ + REPORT.txt。返回各类计数。"""
    pwl_dir = os.path.join(outdir, "pwl")
    os.makedirs(pwl_dir, exist_ok=True)
    shift = opts.shift
    lines, rep = [], []
    cnt = {"dc": 0, "pwl": 0, "skip": 0, "bad": 0}
    for i, t in enumerate(targets, 1):
        inst = "V_rp_%04d" % i
        t.node = spectre_node(t.comps, opts.prefix)
        skipped = _skipped(t.path, opts)
        tag = "%-45s" % t.path
        if t.status != "ok" or t.cone is None or t.cone.n == 0:
            why = t.status if t.status != "ok" else "nodata"
            lines.append("// %s  %s  -- %s" % (inst, t.path, why))
            rep.append((i, why, 0, 0, None, None, t.path, t.db or ""))
            cnt["bad"] += 1
            continue
        cone = t.cone
        pts = cone.finish()
        pts = [(tt + shift, v) for tt, v in pts]
        if cone.is_flat():
            kind = "dc"
            src = "dc=%s" % fmt_v((cone.ymin + cone.ymax) / 2)
            nout = 1
        else:
            kind = "pwl"
            nout = len(pts)
            if nout <= INLINE_MAX:
                src = "type=pwl wave=[%s]" % " ".join(
                    "%s %s" % (fmt_t(a), fmt_v(b)) for a, b in pts)
            else:
                fn = os.path.join(pwl_dir, "%04d_%s.pwl"
                                  % (i, re.sub(r"[^A-Za-z0-9_]+", "_", t.comps[-1])))
                with open(fn, "w", encoding="ascii", newline="\n") as fh:
                    for a, b in pts:
                        fh.write("%s %s\n" % (fmt_t(a), fmt_v(b)))
                src = 'type=pwl file="%s"' % os.path.abspath(fn).replace("\\", "/")
        if opts.rs:
            mid = "rp_%04d" % i
            body = ("%s (%s %s) vsource %s\nR_rp_%04d (%s %s) resistor r=%s"
                    % (inst, mid, opts.gnd, src, i, mid, t.node, opts.rs))
        else:
            body = "%s (%s %s) vsource %s" % (inst, t.node, opts.gnd, src)
        note = "// %s  %s  %d->%d pts  [%s, %s]" % (
            tag, kind, cone.n, nout, fmt_v(cone.ymin), fmt_v(cone.ymax))
        if skipped:
            kind = "skip"
            body = "\n".join("// " + b for b in body.split("\n"))
            note += "  (--skip: 取消注释即可启用)"
        lines.append(note)
        lines.append(body)
        rep.append((i, kind, cone.n, nout, cone.ymin, cone.ymax, t.path,
                    t.db or "(csv column %d)" % t.col))
        cnt[kind] += 1

    head = [
        "// %s  generated %s" % (BUILD, meta["when"]),
        "// source   : %s" % meta["source"],
        "// window   : %s .. %s  (recorded time)   shift: %+g s"
        % (_fs(meta["t_first"]), _fs(meta["t_last"]), shift),
        "//            new sim t=0 <=> recorded t=%s; after t=%s every source holds its last value"
        % (_fs(0.0 - shift), _fs(meta["t_last"] + shift if meta["t_last"] is not None else None)),
        "// tol      : %g V (every recorded sample is within tol of the PWL)" % opts.tol,
        "// signals  : %d dc, %d pwl, %d skipped, %d unresolved"
        % (cnt["dc"], cnt["pwl"], cnt["skip"], cnt["bad"]),
        "//",
        "// Ideal sources win against any driver still attached to these nets:",
        "// remove (or switch to an empty view) the blocks that used to drive them.",
        "// After the run, grep spectre.out for SFE-411: a mistyped hierarchical name is",
        "// only a WARNING ('treated as a regular node') and that source drives nothing.",
        "simulator lang=spectre",
        "",
    ]
    with open(os.path.join(outdir, "replay.scs"), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write("\n".join(head + lines) + "\n")

    with open(os.path.join(outdir, "REPORT.txt"), "w", encoding="utf-8",
              newline="\n") as fh:
        fh.write("\n".join(head[:6]) + "\n")
        fh.write("// rows read: %d\n" % meta["rows"])
        h = meta["header"]
        fh.write("// csv      : %d columns, bound %s; first: %s\n"
                 % (len(h), "by position" if meta["positional"] else "by name",
                    " | ".join(h[:3])))
        if any(t.status == "nocolumn" for t in targets):
            # 名字在库里解析到了，CSV 里却对不上 —— 把整张表头摊开，一眼看出差在哪
            fh.write("// NOCOLUMN: resolved in the database, but no CSV column matched.\n"
                     "//   Full CSV header, one per line:\n")
            for c in h:
                fh.write("//   %s\n" % c)
        fh.write("\n")
        fh.write("%5s  %-11s %9s %6s %12s %12s  %s\n"
                 % ("idx", "kind", "raw", "out", "min", "max", "ADE path  ->  database name"))
        for i, kind, n, no, lo, hi, path, db in rep:
            fh.write("%5d  %-11s %9d %6d %12s %12s  %s  ->  %s\n"
                     % (i, kind, n, no, "" if lo is None else fmt_v(lo),
                        "" if hi is None else fmt_v(hi), path, db))
    return cnt


def _fs(t):
    return "?" if t is None else "%.9g s" % (t + 0.0)      # + 0.0：别打出 -0


def _skipped(path, opts):
    if opts.only and not any(fnmatch.fnmatchcase(path, g) for g in opts.only):
        return True
    return any(fnmatch.fnmatchcase(path, g) for g in (opts.skip or []))


def build_parser():
    ap = argparse.ArgumentParser(
        prog="wave_replay",
        description="上一次仿真的节点电压 → 下一次仿真可 include 的 vsource 激励。")
    ap.add_argument("--list", required=True,
                    help="要回放的信号：ADE outputs 导出的 CSV（取 Output 列），或一行一个路径")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--db", help="SST2 波形库（AMS 的 psf/psf.trn）；经 simvisdbutil 读取")
    src.add_argument("--csv", help="已经导好的 CSV（第一列时间）；用 --timescale 说明时间单位")
    ap.add_argument("-o", "--out", default="replay", help="输出目录（默认 ./replay）")
    ap.add_argument("--tol", type=parse_eng, default=DEFAULT_TOL,
                    help="允许的最大偏差，伏（默认 1m）")
    ap.add_argument("--from", dest="t_from", type=parse_eng,
                    help="只取录下来的这一段：起点（如 200u）")
    ap.add_argument("--to", dest="t_to", type=parse_eng, help="终点")
    ap.add_argument("--keep-time", action="store_true",
                    help="不平移时间。默认把 --from 挪到新仿真的 t=0")
    ap.add_argument("--skip", action="append", default=[], metavar="GLOB",
                    help="这些路径写成注释（可重复）。如 '/I_top/I_PLL/*'")
    ap.add_argument("--only", action="append", default=[], metavar="GLOB",
                    help="只启用匹配的路径，其余写成注释（可重复）")
    ap.add_argument("--prefix", default="",
                    help="新仿真里 DUT 上面若多了一层实例，写在这里（点分）")
    ap.add_argument("--gnd", default="0", help="源的负端（默认 0）")
    ap.add_argument("--rs", help="给每个源串一个电阻（如 10）；默认理想源")
    ap.add_argument("--timescale", type=parse_eng,
                    help="--csv 模式下第一列的时间单位（秒）。simvisdbutil 默认整数，"
                         "通常是 1e-21")
    ap.add_argument("--header", help="--csv 模式下可选：对应的 VCD 头，用来认名字和时间单位")
    ap.add_argument("--simvisdbutil", default=SIMVISDBUTIL, help="可执行文件路径")
    return ap


def utf8_streams():
    """stdout/stderr 钉成 UTF-8：中文 Windows 的 locale 是 GBK，不钉就是乱码。

    Python 3.6 没有 reconfigure —— 那时候照原样输出，隔离区的 Linux 本来就是 UTF-8。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


def main(argv=None):
    opts = build_parser().parse_args(argv)
    os.makedirs(opts.out, exist_ok=True)
    targets = read_targets(opts.list)
    if not targets:
        print("清单里一个信号都没读到: %s" % opts.list, file=sys.stderr)
        return 2
    opts.shift = 0.0 if (opts.keep_time or opts.t_from is None) else -opts.t_from

    proc = None
    if opts.db:
        header = db_header(opts.simvisdbutil, opts.db, opts.out)
        timescale, vars_ = parse_vcd_header(header)
        resolve(targets, vars_)
        sigs = [t.db for t in targets if t.status == "ok"]
        if not sigs:
            _print_unresolved(targets)
            return 3
        proc, lines = db_rows(opts.simvisdbutil, opts.db, sigs, opts.out,
                              opts.t_from, opts.t_to)
        source = os.path.abspath(opts.db)
    else:
        fh = open(opts.csv, encoding="utf-8", errors="replace")
        lines = fh
        timescale = opts.timescale
        if opts.header:
            with open(opts.header, encoding="utf-8", errors="replace") as hf:
                ts, vars_ = parse_vcd_header(hf.read())
            timescale = timescale or ts
            resolve(targets, vars_)
        else:
            for t in targets:
                t.status = "ok" if t.kind == "net" else "unsupported"
        if timescale is None:
            print("--csv 模式要给 --timescale（或 --header）", file=sys.stderr)
            return 2
        source = os.path.abspath(opts.csv)

    first = next(iter(lines), "")
    header = first.rstrip("\r\n").split(",")
    positional = bind_columns(header, targets, sigs if opts.db else None)
    rows, cut = stream(lines, targets, timescale, opts.tol, opts.t_from, opts.t_to)

    if proc is not None:
        proc.stdout.close()
        rc = proc.wait()
        proc.errfile.close()
        # 读到 --to 就关了管道，对面吃 SIGPIPE 退出 —— 那不是失败
        if rc != 0 and not cut:
            with open(proc.errfile.name, encoding="utf-8", errors="replace") as fh:
                err = fh.read()
            print("simvisdbutil 退出码 %d:\n%s" % (rc, err[-2000:]), file=sys.stderr)
            return 4
    elif hasattr(lines, "close"):
        lines.close()

    t_first = min((t.cone.out[0][0] for t in targets if t.cone and t.cone.out),
                  default=None)
    t_last = max((max(t.cone.tp, t.cone.tq or 0) for t in targets
                  if t.cone and t.cone.n), default=None)
    meta = {"when": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "source": source, "rows": rows, "t_first": t_first, "t_last": t_last,
            "header": header, "positional": positional}
    cnt = emit(targets, opts.out, opts, meta)

    print("%d 行 → %s/replay.scs   dc %d · pwl %d · 注释掉 %d · 未解析 %d"
          % (rows, opts.out, cnt["dc"], cnt["pwl"], cnt["skip"], cnt["bad"]))
    _print_unresolved(targets)
    return 0 if cnt["bad"] == 0 else 1


def _print_unresolved(targets):
    bad = [t for t in targets if t.status not in ("ok",)]
    for t in bad[:30]:
        print("  %-11s %s   %s" % (t.status, t.path, t.db or ""), file=sys.stderr)
    if len(bad) > 30:
        print("  ... 另有 %d 个，见 REPORT.txt" % (len(bad) - 30), file=sys.stderr)


if __name__ == "__main__":
    utf8_streams()
    sys.exit(main())
