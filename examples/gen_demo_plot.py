#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_demo_plot.py — 把合成波形画成一张仿 ViVA 截图，用来闭环验证 plot_digitize。

为什么要它：`plot_digitize` 的完成判据是「把第 0 步生成的波形渲染成 PNG
再数字化回来，误差落在像素精度内」。**只有渲染端的坐标映射是已知的，
这个闭环才是可判定的** —— 拿真实截图测，你分不清误差是数字化算错了
还是你手打的坐标范围不准。

PNG 是手写的（zlib + CRC32，几十行）：纯标准库，而且完全可控 ——
非隔行、8-bit 真彩、filter 全 0。数字化那边要吃的是**真实世界的** PNG，
所以它的解码器比这里全（filter 0-4、RGBA、调色板）。

用法：
    python examples/gen_demo_plot.py                       # examples/demo_plot.png
    python examples/gen_demo_plot.py --yaxis 0.70,0.87 --traces V(vdd_pll) \
        -o /tmp/zoom.png                                   # 放大版

只依赖标准库。
"""

import argparse
import os
import struct
import sys
import zlib

W, H = 1920, 1080
BOX = (212, 88, 1687, 901)          # 绘图框 x0,y0,x1,y1（含边框线）
BG = (0xFF, 0xFF, 0xFF)
FRAME = (0x30, 0x30, 0x30)
GRID = (0xDD, 0xDD, 0xDD)
# ViVA 每条 trace 一个颜色，这是数字化最稳的一维
TRACE_COLORS = ["#e01b24", "#1c71d8", "#2ec27e", "#e5a50a"]


def hex2rgb(s):
    s = s.lstrip("#")
    return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


class Img(object):
    def __init__(self, w, h, bg):
        self.w, self.h = w, h
        self.rows = [bytearray(bg * w) for _ in range(h)]

    def px(self, x, y, c):
        if 0 <= x < self.w and 0 <= y < self.h:
            i = x * 3
            self.rows[y][i:i + 3] = bytes(c)

    def hline(self, x0, x1, y, c):
        for x in range(min(x0, x1), max(x0, x1) + 1):
            self.px(x, y, c)

    def vline(self, x, y0, y1, c):
        for y in range(min(y0, y1), max(y0, y1) + 1):
            self.px(x, y, c)

    def line(self, x0, y0, x1, y1, c, wide=1):
        """Bresenham。wide>1 时纵向加粗 —— ViVA 的线也不是 1 px。"""
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx = 1 if x0 < x1 else -1
        sy = 1 if y0 < y1 else -1
        err = dx + dy
        while True:
            for k in range(wide):
                self.px(x0, y0 + k - wide // 2, c)
            if x0 == x1 and y0 == y1:
                break
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def write(self, path):
        raw = b"".join(b"\x00" + bytes(r) for r in self.rows)
        comp = zlib.compress(raw, 9)

        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        ihdr = struct.pack(">IIBBBBB", self.w, self.h, 8, 2, 0, 0, 0)
        with open(path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
                     + chunk(b"IDAT", comp) + chunk(b"IEND", b""))


def read_csv(path):
    with open(path, encoding="utf-8-sig") as fh:
        head = fh.readline().strip().split(",")
        cols = [[] for _ in head]
        for ln in fh:
            p = ln.strip().split(",")
            if len(p) < len(head):
                continue
            try:
                for i in range(len(head)):
                    cols[i].append(float(p[i]))
            except ValueError:
                continue
    names = [h.split("(")[0] + "(" + h.split("(")[1] if h.count("(") else h
             for h in head]
    names = [h.rsplit(" (", 1)[0].strip() for h in head]
    return names, cols


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="把合成波形渲染成仿 ViVA 截图")
    ap.add_argument("--csv", default=os.path.join(here, "demo_tran.csv"))
    ap.add_argument("-o", "--out", default=os.path.join(here, "demo_plot.png"))
    ap.add_argument("--xaxis", default=None, help="x 范围，默认取数据全程")
    ap.add_argument("--yaxis", default="0,1.3")
    ap.add_argument("--traces", default="V(vdd_pll),V(ctrl)")
    args = ap.parse_args()

    names, cols = read_csv(args.csv)
    x = cols[0]
    x0, x1 = (float(v) for v in args.xaxis.split(",")) if args.xaxis \
        else (x[0], x[-1])
    y0, y1 = (float(v) for v in args.yaxis.split(","))
    want = [t.strip() for t in args.traces.split(",")]

    im = Img(W, H, BG)
    bx0, by0, bx1, by1 = BOX
    for k in range(1, 10):                                   # 网格
        gx = bx0 + (bx1 - bx0) * k // 10
        im.vline(gx, by0 + 1, by1 - 1, GRID)
        gy = by0 + (by1 - by0) * k // 10
        im.hline(bx0 + 1, bx1 - 1, gy, GRID)
    for d in (0, 1):                                         # 边框（画两像素粗）
        im.hline(bx0, bx1, by0 + d, FRAME)
        im.hline(bx0, bx1, by1 - d, FRAME)
        im.vline(bx0 + d, by0, by1, FRAME)
        im.vline(bx1 - d, by0, by1, FRAME)

    def sx(v):
        return bx0 + int(round((v - x0) / (x1 - x0) * (bx1 - bx0)))

    def sy(v):
        return by1 - int(round((v - y0) / (y1 - y0) * (by1 - by0)))

    used = []
    for ti, tname in enumerate(want):
        if tname not in names:
            sys.stderr.write("没有这一列: %s（有 %s）\n" % (tname, names))
            continue
        col = cols[names.index(tname)]
        c = hex2rgb(TRACE_COLORS[ti % len(TRACE_COLORS)])
        used.append((TRACE_COLORS[ti % len(TRACE_COLORS)], tname))
        px = py = None
        for i in range(len(x)):
            if not (x0 <= x[i] <= x1):
                continue
            cx, cy = sx(x[i]), max(by0 + 2, min(by1 - 2, sy(col[i])))
            if px is not None:
                im.line(px, py, cx, cy, c, wide=2)
            px, py = cx, cy

    im.write(args.out)
    ppx = (y1 - y0) / float(by1 - by0)
    print("写了 %s  (%.0f KB)" % (args.out, os.path.getsize(args.out) / 1024.0))
    print("  plotbox %d,%d..%d,%d   x %g..%g   y %g..%g"
          % (bx0, by0, bx1, by1, x0, x1, y0, y1))
    print("  1 px = %.4g （y 轴 %d px / %g）—— 这就是数字化的物理上限"
          % (ppx, by1 - by0, y1 - y0))
    for c, n in used:
        print("  %s -> %s" % (c, n))
    print("\n数字化回来：")
    print("  python tools/plot_digitize.py %s --plotbox %d,%d,%d,%d \\"
          % (args.out, bx0, by0, bx1, by1))
    print("      --xaxis %g,%g --yaxis %g,%g %s"
          % (x0, x1, y0, y1,
             " ".join("--trace '%s=%s'" % (c, n) for c, n in used)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
