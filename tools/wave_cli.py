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
               keep_offset=True, notes=None):
    """压到 <= budget 字节。返回 (reduction, text)。

    先量一次不限点数的结果，拿到「头部+METRICS+EVENTS」这块固定开销，
    再对 SHAPE 的行数二分——这样只有首尾两次跑完整的 O(n) 重建自检。
    """
    red = core.reduce_trace(tr, tol, max_points, cand, forced,
                            keep_offset=keep_offset)
    txt = emit.emit(red, m, notes)
    if budget is None or emit.nbytes(txt) <= budget:
        return red, txt
    fixed = emit.nbytes(txt) - emit.shape_bytes(red)
    lo, hi = 2, len(red.kept)
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        r = core.reduce_trace(tr, tol, mid, red.cand, forced,
                              keep_offset=keep_offset, check=False)
        if fixed + emit.shape_bytes(r) <= budget:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if best is None:
        best = 2
    for _ in range(12):                      # 收尾核一次真字节数，超了再收一点
        red = core.reduce_trace(tr, tol, best, red.cand, forced,
                                keep_offset=keep_offset)
        txt = emit.emit(red, m, notes)
        if emit.nbytes(txt) <= budget or best <= 2:
            break
        best = max(2, int(best * 0.9))
    n = emit.nbytes(txt)
    if n > budget:
        # 压不进去就**说出来**。强制保留点（spur 峰及其包络、极值、事件）
        # 是不许为了预算牺牲的 —— 牺牲了就等于回到 54x 低报那个坑里。
        red = core.reduce_trace(tr, tol, best, red.cand, forced,
                                keep_offset=keep_offset)
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


def process(path, args):
    """-> (text, [(trace, reduction), ...])"""
    traces = core.parse_csv(path, layout=args.layout, xcols=args.xcols)
    if not traces:
        raise ValueError("没解析出任何 trace: %s" % path)
    per = args.budget // len(traces) if args.budget else None
    chunks, info = [], []
    for tr in traces:
        _apply_units(tr, args.unit)
        core.analyze(tr, kind=args.kind, xscale=args.xscale)
        m = None
        if not args.no_metrics:
            m = emit.run_metrics(tr)
        tol = args.tol
        notes = []
        if tol is None:
            tol = core.DEFAULT_TOL
            sug = m.suggest_tol() if m else None
            if sug:
                notes.append("tol 用了 kind=%s 建议的 %g（没给 --tol）；"
                             "%s" % (tr.kind, sug, _why_tol(tr, sug)))
                tol = sug
        core.set_eps(tr, tol)
        cand = core.predecimate(tr, tol)
        forced = list(m.forced()) if m else []
        if m is None and not args.no_metrics:
            notes.append("kind=%s 没有注册的 metrics 模块，本文件只有 SHAPE 段"
                         "（已注册: %s）" % (tr.kind, ", ".join(emit.registered())
                                            or "无"))
        red, txt = fit_budget(tr, tol, per, m, forced, cand,
                              args.max_points, not args.no_offset, notes)
        chunks.append(txt)
        info.append((tr, red))
    return "\n".join(chunks), info


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
    p.add_argument("--unit", action="append", default=[], metavar="C=U",
                   help="声明单位，如 --unit c1=V,x=s。脚本不猜单位")
    p.add_argument("--no-metrics", action="store_true",
                   help="只出 SHAPE，不跑测量")
    p.add_argument("--no-offset", action="store_true", help="不扣基线")
    p.add_argument("--list-kinds", action="store_true",
                   help="列出已注册的分析类型后退出")
    p.add_argument("--gui", action="store_true", help="开 Tkinter 预览窗口")
    p.add_argument("--selftest", action="store_true",
                   help="配 --gui 用：无人值守跑一遍 GUI 并打印状态后自动退出")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    emit.load_builtin()
    if args.list_kinds:
        print("已注册的 kind: " + (", ".join(emit.registered()) or "无"))
        return 0
    # 参数归一化必须在 --gui 分支**之前**：GUI 走的是同一个 args。
    # 放在后面的话 --gui --xcols 0,2 传进去的是字符串，
    # parse_csv 里 set("0,2") 会变成 [',','0','2']。
    if args.xcols:
        args.xcols = [int(v) for v in str(args.xcols).split(",")]
    if not args.budget:
        args.budget = None
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
