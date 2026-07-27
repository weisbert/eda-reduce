#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
plot_digitize.py — 把波形**截图**变回可计算的数。方案二，兜底。

不是 `wave_reduce` 的替代，是**导不出数据时的兜底**，以及把已有的图
（同事给的、报告里的、datasheet 上的）变回能算的东西。

## 物理上限就是像素

1920×1080 截图、绘图区高约 800 px：

| Y 轴量程 | 1 px | 亚像素质心后 |
|---|---|---|
| 1.0 V | 1.25 mV | ~0.4 mV |
| 100 mV（放大后） | 125 µV | ~40 µV |

有效 8–10 bit，相对满量程 0.1–0.3%。推论就是唯一重要的操作建议：

    **截图的缩放层级就是量化器。**
    要看 5 mV 纹波，先在 ViVA 里把 Y 轴缩到 20 mV 再截图。

## 设计上绕开最脆的一环

- **不做 OCR**。坐标范围手打（`--xaxis 0,300n --yaxis 0.7,0.81`），
  OCR 认错一个小数点，整张图的数就全错了，而且看不出来。
- 绘图框自动找（最长的水平/垂直边框线），或者 `--plotbox` 手给。
- 曲线按**颜色**提取：ViVA 每条 trace 一个颜色，这是最稳的一维。
- 每个 x 像素列取该颜色像素的**上下包络而不是中位数** ——
  密集振铃这样会保留成一条带，取中位数会被抽成锯齿（假信号）。

## 已知会挂的场景（提前说清楚，别指望）

两条同色曲线交叉、对数轴上密集的噪声底、半透明叠加、
深色主题下网格线和曲线亮度接近。这些场景下工具**报「该列有 N 个候选，
无法判定」而不是猜一个** —— 和这个项目其它地方一样：声明不确定度，不猜。

## 一句实话

通道能贴图的话，定性问题（"这看着像不像振铃"）直接贴 PNG 更好。
数字化的价值在别处 —— 把曲线变成**可计算**的东西：量振铃频率、
两次跑的曲线相减、拟合衰减包络、喂进 wave_reduce 走全套 metrics。
以及在纯文本通道里，它是唯一的路。

依赖：纯标准库（zlib 手解 PNG）。有 Pillow 就用 Pillow，两条路径结果一致。

    python tools/plot_digitize.py shot.png --xaxis 0,300n --yaxis 0.7,0.87 \\
        --trace '#e01b24=vdd_pll' -o out.csv
"""

import argparse
import os
import re
import struct
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

VERSION = "PLOTDIG1"
SUFFIX = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3,
          "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12}


def eng(s):
    """吃 '300n' / '1e-9' / '1.2G' / '-5m'。工程记数是手打坐标时的常态。"""
    s = s.strip()
    m = re.match(r"^([-+]?[0-9.]+(?:[eE][-+]?\d+)?)\s*([fpnuµmkKMGT]?)$", s)
    if not m:
        return float(s)
    return float(m.group(1)) * SUFFIX.get(m.group(2), 1.0)


# --------------------------------------------------------------- PNG 解码


class Png(object):
    """8-bit 非隔行 PNG。颜色类型 0/2/3/4/6，filter 0-4。

    自带解码器的存在理由是逃生舱：隔离区可能连 Pillow 都没有。
    有 Pillow 就用 Pillow（快得多），两条路径出一样的 RGB。
    """

    def __init__(self, w, h, rgb):
        self.w, self.h, self.rgb = w, h, rgb        # rgb: bytearray w*h*3

    def at(self, x, y):
        i = (y * self.w + x) * 3
        return self.rgb[i], self.rgb[i + 1], self.rgb[i + 2]


def load_png(path):
    try:
        from PIL import Image                       # noqa: F401
    except ImportError:
        return _load_png_pure(path), "自带解码器"
    from PIL import Image
    im = Image.open(path).convert("RGB")
    w, h = im.size
    return Png(w, h, bytearray(im.tobytes())), "Pillow"


def _load_png_pure(path):
    with open(path, "rb") as fh:
        data = fh.read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("不是 PNG：%s" % path)
    pos = 8
    idat = []
    plte = None
    trns = None
    w = h = depth = ctype = interlace = None
    while pos < len(data):
        ln = struct.unpack(">I", data[pos:pos + 4])[0]
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        pos += 12 + ln
        if tag == b"IHDR":
            w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", body)
        elif tag == b"PLTE":
            plte = body
        elif tag == b"tRNS":
            trns = body
        elif tag == b"IDAT":
            idat.append(body)
        elif tag == b"IEND":
            break
    if depth != 8:
        raise ValueError("只支持 8-bit（这张是 %d-bit）—— "
                         "用 Pillow，或者先转成 8-bit" % depth)
    if interlace:
        raise ValueError("不支持隔行（Adam7）PNG —— 用 Pillow，或另存为非隔行")
    ch = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(ctype)
    if ch is None:
        raise ValueError("不认识的颜色类型 %d" % ctype)
    raw = zlib.decompress(b"".join(idat))
    flat = _unfilter(raw, w, h, ch)
    rgb = bytearray(w * h * 3)
    for i in range(w * h):
        s = i * ch
        if ctype == 2 or ctype == 6:
            rgb[i * 3:i * 3 + 3] = flat[s:s + 3]
        elif ctype == 0 or ctype == 4:
            g = flat[s]
            rgb[i * 3:i * 3 + 3] = bytes((g, g, g))
        else:                                       # 调色板
            p = flat[s] * 3
            rgb[i * 3:i * 3 + 3] = plte[p:p + 3] if plte else b"\0\0\0"
    _ = trns
    return Png(w, h, rgb)


def _unfilter(raw, w, h, bpp):
    stride = w * bpp
    out = bytearray(h * stride)
    prev = bytearray(stride)
    pos = 0
    for y in range(h):
        f = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if f == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 255
        elif f == 2:
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 255
        elif f == 3:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((a + prev[i]) >> 1)) & 255
        elif f == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                c = prev[i - bpp] if i >= bpp else 0
                b = prev[i]
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pr) & 255
        elif f != 0:
            raise ValueError("第 %d 行 filter 类型 %d 不认识" % (y, f))
        out[y * stride:(y + 1) * stride] = line
        prev = line
    return out


# --------------------------------------------------------------- 绘图框


def find_plotbox(img, frac=0.6):
    """最长的水平/垂直深色线 = 边框。找不到就返回 None，让人手给。

    不猜：找不到就说找不到，而不是随便框一块然后把整张图的坐标都算错。
    """
    w, h = img.w, img.h
    dark_rows, dark_cols = [], []
    for y in range(h):
        n = sum(1 for x in range(0, w, 2) if _is_dark(img.at(x, y)))
        if n > frac * (w / 2):
            dark_rows.append(y)
    for x in range(w):
        n = sum(1 for y in range(0, h, 2) if _is_dark(img.at(x, y)))
        if n > frac * (h / 2):
            dark_cols.append(x)
    if len(dark_rows) < 2 or len(dark_cols) < 2:
        return None
    return (dark_cols[0], dark_rows[0], dark_cols[-1], dark_rows[-1])


def _is_dark(c):
    return (c[0] + c[1] + c[2]) < 300


# --------------------------------------------------------------- 提取


def extract(img, box, color, tol):
    """每个 x 像素列取该颜色的**上下包络**，顺带数出「有几段候选」。

    包络而不是中位数：密集振铃在一列里占几十个像素，取中位数会得到一条
    上下乱跳的锯齿（假信号），取包络得到一条带 —— 带是真的，锯齿不是。
    """
    x0, y0, x1, y1 = box
    r0, g0, b0 = color
    lo, hi, runs = [], [], []
    for x in range(x0 + 2, x1 - 1):
        ys = []
        for y in range(y0 + 2, y1 - 1):
            r, g, b = img.at(x, y)
            if abs(r - r0) <= tol and abs(g - g0) <= tol and abs(b - b0) <= tol:
                ys.append(y)
        if not ys:
            lo.append(None)
            hi.append(None)
            runs.append(0)
            continue
        nrun = 1
        for k in range(1, len(ys)):
            if ys[k] - ys[k - 1] > 2:
                nrun += 1
        lo.append(min(ys))
        hi.append(max(ys))
        runs.append(nrun)
    return lo, hi, runs


def fill_gaps(v):
    """无匹配像素的列线性补，并把补了几列**报出来**。"""
    idx = [i for i, t in enumerate(v) if t is None]
    good = [i for i, t in enumerate(v) if t is not None]
    if not good:
        return v, len(idx)
    for i in idx:
        import bisect
        p = bisect.bisect_left(good, i)
        if p == 0:
            v[i] = v[good[0]]
        elif p >= len(good):
            v[i] = v[good[-1]]
        else:
            a, b = good[p - 1], good[p]
            v[i] = v[a] + (i - a) / float(b - a) * (v[b] - v[a])
    return v, len(idx)


# --------------------------------------------------------------- 主流程


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="波形截图 -> 可计算的数（导不出数据时的兜底）",
        epilog="截图的缩放层级就是量化器：要看 5 mV 纹波，"
               "先在 ViVA 里把 Y 轴缩到 20 mV 再截图。")
    ap.add_argument("png")
    ap.add_argument("-o", "--out", help="输出 CSV，默认 stdout")
    ap.add_argument("--xaxis", required=True, metavar="X0,X1",
                    help="x 轴范围，吃工程记数：0,300n")
    ap.add_argument("--yaxis", required=True, metavar="Y0,Y1")
    ap.add_argument("--xlog", action="store_true", help="x 轴是对数轴")
    ap.add_argument("--ylog", action="store_true")
    ap.add_argument("--plotbox", metavar="X0,Y0,X1,Y1",
                    help="绘图框像素坐标；不给就自动找最长的边框线")
    ap.add_argument("--trace", action="append", default=[], required=True,
                    metavar="#RRGGBB=NAME", help="按颜色提取一条曲线，可重复")
    ap.add_argument("--tol", type=int, default=60, help="颜色容差（默认 60）")
    ap.add_argument("--mid", action="store_true",
                    help="只输出包络中线（默认输出上下包络两列）")
    a = ap.parse_args(argv)

    img, backend = load_png(a.png)
    box = tuple(int(v) for v in a.plotbox.split(",")) if a.plotbox \
        else find_plotbox(img)
    if not box:
        ap.error("自动找不到绘图框（图太花或者没有边框线）——"
                 " 用 --plotbox X0,Y0,X1,Y1 手给。不猜。")
    bx0, by0, bx1, by1 = box
    x0, x1 = (eng(v) for v in a.xaxis.split(","))
    y0, y1 = (eng(v) for v in a.yaxis.split(","))
    npx, npy = bx1 - bx0, by1 - by0
    if npx < 10 or npy < 10:
        ap.error("绘图框太小：%s" % (box,))

    import math

    def to_x(px):
        f = (px - bx0) / float(npx)
        if a.xlog:
            return 10.0 ** (math.log10(x0) + f * (math.log10(x1) - math.log10(x0)))
        return x0 + f * (x1 - x0)

    def to_y(py):
        f = (by1 - py) / float(npy)
        if a.ylog:
            return 10.0 ** (math.log10(y0) + f * (math.log10(y1) - math.log10(y0)))
        return y0 + f * (y1 - y0)

    ppx = abs(y1 - y0) / float(npy)
    head = ["# %s  src=%s  plotbox %d,%d..%d,%d  解码=%s"
            % (VERSION, os.path.basename(a.png), bx0, by0, bx1, by1, backend)]
    head.append("# 精度上限: 1 px = %s  (y %d px / %s);  小于 %s 的特征不可信"
                % (_fmt(ppx), npy, _fmt(abs(y1 - y0)), _fmt(3 * ppx)))
    head.append("# 提示: 截图的缩放层级就是量化器 —— 要看更小的特征，"
                "先在 ViVA 里把 Y 轴缩紧再截图")

    cols, names = [], []
    for spec in a.trace:
        if "=" not in spec:
            ap.error("--trace 要写成 '#RRGGBB=名字'")
        chex, nm = spec.split("=", 1)
        c = (int(chex.lstrip("#")[0:2], 16), int(chex.lstrip("#")[2:4], 16),
             int(chex.lstrip("#")[4:6], 16))
        lo, hi, runs = extract(img, box, c, a.tol)
        nmiss = sum(1 for v in lo if v is None)
        namb = sum(1 for r in runs if r > 1)
        lo, _ = fill_gaps(lo)
        hi, _ = fill_gaps(hi)
        head.append("# trace %s -> %s   %d 列, %d 列无匹配像素(已线性补), "
                    "%d 列有多段候选" % (chex, nm, len(lo), nmiss, namb))
        if namb > 0.02 * len(lo):
            # 多段候选多到这个份上，多半是两条同色曲线交叉或者半透明叠加。
            # 报出来，别让人把一条编出来的曲线当真。
            head.append("#   ！%d/%d 列有多段候选（>2%%）：可能是同色曲线交叉、"
                        "半透明叠加或网格线同色。包络仍然是对的，"
                        "但「中线」没有意义" % (namb, len(lo)))
        if nmiss > 0.05 * len(lo):
            head.append("#   ！%d/%d 列一个匹配像素都没有（>5%%）："
                        "颜色给错了？或者曲线被别的曲线盖住了" % (nmiss, len(lo)))
        names.append(nm)
        cols.append((lo, hi))

    lines = list(head)
    if a.mid:
        lines.append(",".join(["x"] + names))
    else:
        lines.append(",".join(["x"] + sum([[n + "_lo", n + "_hi"]
                                           for n in names], [])))
    for i in range(len(cols[0][0])):
        px = bx0 + 2 + i
        row = ["%.10g" % to_x(px)]
        for lo, hi in cols:
            # 像素 y 越大值越小，所以 lo(像素) 是上包络
            vh, vl = to_y(lo[i]), to_y(hi[i])
            if a.mid:
                row.append("%.7g" % (0.5 * (vh + vl)))
            else:
                row.append("%.7g" % vl)
                row.append("%.7g" % vh)
        lines.append(",".join(row))

    text = "\n".join(lines) + "\n"
    if a.out:
        with open(a.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        sys.stderr.write("\n".join(head) + "\n-> %s (%d 行)\n"
                         % (a.out, len(cols[0][0])))
        sys.stderr.write("接着可以喂给 wave_reduce 走全套 metrics：\n"
                         "    python tools/wave_reduce.py %s -o out.wv\n" % a.out)
    else:
        sys.stdout.write(text)
    return 0


def _fmt(v):
    for e, p in ((1e-15, "f"), (1e-12, "p"), (1e-9, "n"), (1e-6, "u"),
                 (1e-3, "m"), (1.0, ""), (1e3, "k"), (1e6, "M"), (1e9, "G")):
        if abs(v) < e * 1000:
            return "%.4g %s" % (v / e, p)
    return "%.4g" % v


if __name__ == "__main__":
    sys.exit(main())
