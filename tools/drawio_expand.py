#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drawio_expand.py — 把 `drawio_reduce.py` 产出的 `.rd` 还原回 `.drawio`。

跟 reduce 严格互为逆：reduce 做过的每一步确定性变换，这里反着做一遍。

    样式字典 S#/E#   -> 展开回每个 cell 的 style 串
    `a.b` 形式的 shape -> `shape=mxgraph.a.b`
    端点绝对坐标      -> 反解出 exitX/exitY/entryX/entryY
                        （要**反着**过一遍 direction / flip / rotation，不能直接
                          拿 (x-bx)/bw —— flipH 的图元上那样算会差一整个宽度）
    `~` 标记          -> 有 ~ = 原图 exitPerimeter 默认(1)；没有 = 原图显式写了 0
    `*…*`            -> `<b>…</b>`；`\\*` 还原成字面量 `*`
    J 行             -> drawio 那串 waypoint 样式 + 20x20 方框
    P 行             -> mxGraphModel 的页面属性

**不可逆的三处**（reduce 侧就是有意丢的，见 docs/rd-spec.md §7）：
下标/上标（`V<sub>ss</sub>` 已经变成 `Vss`）、斜体（`/…/` 会撞 `clk/2`，
所以只单向标记不还原）、cell 的 z 序（.rd 把图元和连线分成两段列，
还原出来连线一律在图元之上）。

用法：
    python drawio_expand.py my.rd -o my.drawio
    python drawio_expand.py my.rd            # 输出到 stdout

只依赖 Python 标准库 (3.8+)。
"""

import argparse
import math
import os
import re
import sys
import xml.etree.ElementTree as ET

# 跟 drawio_reduce.py 里的两个字面量必须一致 —— reduce 靠它判「能不能缩」，
# expand 靠它写回去。改一边要改两边。
WAYPOINT_STYLE = ("shape=waypoint;sketch=0;fillStyle=solid;size=6;pointerEvents=1;"
                  "points=[];fillColor=none;resizable=0;rotatable=0;"
                  "perimeter=centerPerimeter;snapToPoint=1;")
WAYPOINT_SIZE = 20.0
EDGELABEL_STYLE = ("edgeLabel;html=1;align=center;verticalAlign=middle;"
                   "resizable=0;points=[];")

# 缺省的 mxGraphModel 属性，P 行里带出来的覆盖它
MODEL_DEFAULT = [
    ("dx", "1422"), ("dy", "798"), ("grid", "1"), ("gridSize", "10"),
    ("guides", "1"), ("tooltips", "1"), ("connect", "1"), ("arrows", "1"),
    ("fold", "1"), ("page", "1"), ("pageScale", "1"), ("pageWidth", "850"),
    ("pageHeight", "1100"), ("math", "0"), ("shadow", "0"),
]


# --------------------------------------------------------------- 小工具


def fmt(v):
    """回写坐标：整数就不带小数点，跟 drawio 自己存的样子一致。"""
    f = float(v)
    return str(int(f)) if f == int(f) else repr(round(f, 4))


def unquote_style_value(v):
    return v[1:-1] if len(v) >= 2 and v[0] == '"' and v[-1] == '"' else v


def split_style_tokens(s):
    """样式串按空格分词，但带引号的值（`dashPattern="8 8"`）算一个词。"""
    out, cur, q = [], [], False
    for ch in s:
        if ch == '"':
            q = not q
            cur.append(ch)
        elif ch == " " and not q:
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def expand_style(s):
    """`electrical.transistors.pmos flipH=1` -> drawio 的 style 串。"""
    s = (s or "").strip()
    if s == "-":
        s = ""
    toks = split_style_tokens(s)
    parts = []
    for i, t in enumerate(toks):
        if "=" not in t:
            # 首词且带点 = 被缩过的 mxgraph stencil 名；其余无值词原样保留
            # （`ellipse` / `triangle` / `text` 这些本来就是无值的样式 key）
            parts.append("shape=mxgraph." + t if i == 0 and "." in t else t)
            continue
        k, v = t.split("=", 1)
        parts.append("%s=%s" % (k, unquote_style_value(v)))
    parts.append("html=1")          # reduce 把 html 丢了，标签统一按 HTML 渲染
    return ";".join(parts) + ";"


class _Bold(object):
    def __init__(self, open_):
        self.s = "<b>" if open_ else "</b>"


def label_to_html(s):
    """`.rd` 的标签文本 -> drawio 的 value。

    成对的 `*` 变 `<b>`，落单的当字面量；`\\x` 一律脱一层转义。
    斜体不还原 —— reduce 侧就没转义 `/`，`clk/2` 会被误配对（rd-spec §7）。
    """
    buf, marks, i = [], [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            buf.append(s[i + 1])
            i += 2
            continue
        if c == "*":
            marks.append(len(buf))
        buf.append(c)
        i += 1
    paired = len(marks) - len(marks) % 2
    for j, k in enumerate(marks[:paired]):
        buf[k] = _Bold(j % 2 == 0)
    return "".join(b.s if isinstance(b, _Bold) else b for b in buf)


def unescape_plain(s):
    """P 行页名之类只有 `\\` 转义、没有强调标记的字段。"""
    return re.sub(r"\\(.)", r"\1", s)


def read_quoted(s, i):
    """从 s[i] == '"' 开始读一个带 \\ 转义的引号串。-> (原始内容, 结束位置)"""
    assert s[i] == '"'
    out, i = [], i + 1
    while i < len(s):
        if s[i] == "\\" and i + 1 < len(s):
            out.append(s[i:i + 2])
            i += 2
            continue
        if s[i] == '"':
            return "".join(out), i + 1
        out.append(s[i])
        i += 1
    return "".join(out), i


EXTRA_RE = re.compile(r"\{([^={}]+)=(.*?)\}(?=\s*(?:\{|$))")


def parse_extras(s):
    return [(m.group(1), m.group(2)) for m in EXTRA_RE.finditer(s.strip())]


# --------------------------------------------------------------- 几何反解


def _rot(px, py, deg, cx, cy):
    rad = math.radians(deg)
    co, si = math.cos(rad), math.sin(rad)
    dx, dy = px - cx, py - cy
    return (dx * co - dy * si + cx, dy * co + dx * si + cy)


def snap(v, span):
    """反解出来的 exitX 贴到 0/¼/⅓/½ 这些常见值上。

    reduce 把坐标量化到 0.1，除回去就带零头（0.5 -> 0.4998）。零头本身看不出来，
    但写回文件里就再也认不出「这里原本是正中间」了。
    """
    tol = max(2e-3, 0.06 / max(abs(span), 1.0))
    best = None
    for d in (2, 3, 4, 8):
        for k in range(d + 1):
            c = k / float(d)
            e = abs(v - c)
            if e <= tol and (best is None or e < best[0]):
                best = (e, c)
    return best[1] if best else round(v, 4)


def inv_connection_point(v, px, py):
    """reduce 里 connection_point() 的逆：画布绝对坐标 -> 归一化 (exitX, exitY)。

    正向是 direction 换框 -> 取点 -> flip 镜像 -> 转 rotation+direction 角，
    所以逆向要倒着来：先转回去，再镜像回去，最后除框。
    """
    x, y, w, h = v["bounds"]
    cx, cy = x + w / 2.0, y + h / 2.0
    st = v["st"]
    d = st.get("direction")
    r1 = {"north": 270, "west": 180, "south": 90}.get(d, 0)
    if d in ("north", "south"):
        bw, bh = h, w
        bx, by = cx - bw / 2.0, cy - bh / 2.0
    else:
        bw, bh = w, h
        bx, by = x, y
    try:
        r2 = float(st.get("rotation", 0) or 0) + r1
    except ValueError:
        r2 = r1
    if r2:
        px, py = _rot(px, py, -r2, cx, cy)
    if st.get("flipH") == "1":
        px = 2 * cx - px
    if st.get("flipV") == "1":
        py = 2 * cy - py
    return (snap((px - bx) / bw if bw else 0.0, bw),
            snap((py - by) / bh if bh else 0.0, bh))


# --------------------------------------------------------------- .rd 解析

STYLE_RE = re.compile(r"^([SE]\d+)\s+x\d+\s*(.*)$")
P_RE = re.compile(r'^P\s+"((?:[^"\\]|\\.)*)"\s+([\d.]+)x([\d.]+)\s*(.*)$')
VT_RE = re.compile(r"^([VT])\s+(\S+)\s+(S\d+)\s+"
                   r"(-?[\d.]+),(-?[\d.]+),(-?[\d.]+),(-?[\d.]+)\s*(.*)$")
J_RE = re.compile(r"^J\s+(\S+)\s+(-?[\d.]+),(-?[\d.]+)\s*$")
W_RE = re.compile(r"^W\s+(\S+)\s+(E\d+)\s+(.*)$")
PAGE_RE = re.compile(r"^##\s*page\s+(\S+)")


class Page(object):
    def __init__(self, name):
        self.name = name
        self.model = {}
        self.styles = {}
        self.cells = []          # ("V"/"T"/"J", id, style_ref, bounds, label, extras)
        self.edges = []


def parse_rd(text):
    pages = []
    # 老 .rd（P 行是后加的）只能靠 `## page` 注释分页；有 P 行就以 P 行为准
    legacy = not any(P_RE.match(ln) for ln in text.splitlines())

    def page():
        if not pages:
            pages.append(Page("Page-1"))
        return pages[-1]

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        m = P_RE.match(line)
        if m:
            cur = Page(unescape_plain(m.group(1)))
            cur.model = dict(parse_extras(m.group(4)))
            cur.model["pageWidth"] = m.group(2)
            cur.model["pageHeight"] = m.group(3)
            pages.append(cur)
            continue

        if line.startswith("#"):
            m = PAGE_RE.match(line) if legacy else None
            if m:
                pages.append(Page(m.group(1)))
            continue

        m = STYLE_RE.match(line)
        if m:
            page().styles[m.group(1)] = m.group(2).strip()
            continue

        m = J_RE.match(line)
        if m:
            cx, cy = float(m.group(2)), float(m.group(3))
            page().cells.append(
                ("J", m.group(1), None,
                 (cx - WAYPOINT_SIZE / 2, cy - WAYPOINT_SIZE / 2,
                  WAYPOINT_SIZE, WAYPOINT_SIZE), "", []))
            continue

        m = VT_RE.match(line)
        if m:
            kind, cid, sref = m.group(1), m.group(2), m.group(3)
            bounds = tuple(float(m.group(i)) for i in (4, 5, 6, 7))
            tail = m.group(8)
            label, rest = "", tail
            if tail.startswith('"'):
                label, j = read_quoted(tail, 0)
                rest = tail[j:]
            page().cells.append((kind, cid, sref, bounds, label,
                                 parse_extras(rest)))
            continue

        m = W_RE.match(line)
        if m:
            page().edges.append(parse_edge(m.group(1), m.group(2), m.group(3)))
            continue

        sys.stderr.write("跳过不认识的行: %s\n" % line)
    return pages


def parse_edge(eid, sref, body):
    """`W` 行的尾巴：链 ["label"] |"边标签"@pos [S#] ... {k=v}"""
    cut = len(body)
    for ch in ('"', "|", "{"):
        i = body.find(ch)
        if i != -1:
            cut = min(cut, i)
    chain = [t.strip() for t in body[:cut].split(">") if t.strip()]
    tail = body[cut:].strip()

    label, labels = "", []
    if tail.startswith('"'):
        label, j = read_quoted(tail, 0)
        tail = tail[j:].strip()
    while tail.startswith("|"):
        tail = tail[1:]
        txt, j = read_quoted(tail, 0) if tail.startswith('"') else ("", 0)
        tail = tail[j:]
        pos, lref = [0.0, 0.0, 0.0, 0.0], None
        if tail.startswith("@"):
            k = 1
            while k < len(tail) and (tail[k].isdigit() or tail[k] in ".,-"):
                k += 1
            for i, t in enumerate(tail[1:k].split(",")):
                pos[i] = float(t)
            tail = tail[k:]
        tail = tail.lstrip()
        m = re.match(r"^(S\d+)\b", tail)
        if m:
            lref = m.group(1)
            tail = tail[m.end():].lstrip()
        labels.append((pos, txt, lref))
    return (eid, sref, chain, label, labels, parse_extras(tail))


def parse_endpoint(tok):
    """-> ('none',) | ('pt', x, y) | ('ref', id, x|None, y|None, approx)"""
    tok = tok.strip()
    if not tok or tok == "?":
        return ("none",)
    if tok.startswith("@"):
        x, y = tok[1:].split(",")
        return ("pt", float(x), float(y))
    if ":" not in tok:
        return ("ref", tok, None, None, True)
    cid, coord = tok.split(":", 1)
    if coord == "?":
        return ("ref", cid, None, None, True)
    approx = coord.startswith("~")
    x, y = coord.lstrip("~").split(",")
    return ("ref", cid, float(x), float(y), approx)


# --------------------------------------------------------------- 组装 XML


def sub(_p, _tag, **attrs):
    # 参数名带下划线：drawio 的属性里就有 parent / tag，不躲开会撞上
    el = ET.SubElement(_p, _tag)
    for k, v in attrs.items():
        el.set(k, v)
    return el


def geom(parent, x=None, y=None, w=None, h=None, relative=False):
    g = ET.SubElement(parent, "mxGeometry")
    for k, v in (("x", x), ("y", y), ("width", w), ("height", h)):
        if v is not None:
            g.set(k, fmt(v))
    if relative:
        g.set("relative", "1")
    g.set("as", "geometry")
    return g


def build_page(page, index):
    dia = ET.Element("diagram", {"name": page.name, "id": "expand-%d" % index})
    attrs = dict(MODEL_DEFAULT)
    attrs.update(page.model)
    model = ET.SubElement(dia, "mxGraphModel")
    for k, _ in MODEL_DEFAULT:
        model.set(k, attrs.pop(k))
    for k in sorted(attrs):
        model.set(k, attrs[k])
    root = ET.SubElement(model, "root")
    sub(root, "mxCell", id="0")
    sub(root, "mxCell", id="1", parent="0")

    verts = {}
    ids = set()
    dangling = []

    def style_of(ref):
        return expand_style(page.styles.get(ref, "")) if ref else ""

    for kind, cid, sref, bounds, label, extras in page.cells:
        ids.add(cid)
        st = WAYPOINT_STYLE if kind == "J" else style_of(sref)
        verts[cid] = {"bounds": bounds, "st": raw_style_dict(st)}
        holder = root
        mx_attrs = {"style": st, "vertex": "1", "parent": "1"}
        if extras:
            obj = ET.SubElement(root, "object")
            obj.set("label", label_to_html(label))
            for k, v in extras:
                obj.set(k, v)
            obj.set("id", cid)
            holder = obj
        else:
            mx_attrs["id"] = cid
            mx_attrs["value"] = label_to_html(label)
        cell = ET.SubElement(holder, "mxCell")
        for k in ("id", "value", "style", "vertex", "parent"):
            if k in mx_attrs:
                cell.set(k, mx_attrs[k])
        geom(cell, bounds[0], bounds[1], bounds[2], bounds[3])

    for eid, sref, chain, label, labels, extras in page.edges:
        ids.add(eid)
        st = style_of(sref)
        ends = [parse_endpoint(chain[0]), parse_endpoint(chain[-1])]
        mids = [parse_endpoint(t) for t in chain[1:-1]]
        eattrs = {}
        constraints = []
        for ep, kind, tag in ((ends[0], "exit", "source"),
                              (ends[1], "entry", "target")):
            if ep[0] != "ref":
                continue
            cid = ep[1]
            if cid not in verts:
                # --bbox 切出来的 .rd 会引用被切掉的图元。端点坐标 reduce 已经
                # 解算过了，退回成悬空端点，线还在原位；连坐标都没有才真丢。
                dangling.append((eid, tag, cid, ep[2] is not None))
                if ep[2] is not None:
                    ends[0 if tag == "source" else 1] = ("pt", ep[2], ep[3])
                continue
            eattrs[tag] = cid
            if ep[2] is None:
                continue
            cx, cy = inv_connection_point(verts[cid], ep[2], ep[3])
            constraints.append("%sX=%s;%sY=%s;%sDx=0;%sDy=0;"
                               % (kind, fmt(cx), kind, fmt(cy), kind, kind))
            # `~` = reduce 没做 perimeter 投影 = 原图用的默认值 1，别写死
            if not ep[4]:
                constraints.append("%sPerimeter=0;" % kind)
        st = "".join(constraints) + st

        holder = root
        mx_attrs = {"style": st, "edge": "1", "parent": "1"}
        mx_attrs.update(eattrs)
        if extras:
            obj = ET.SubElement(root, "object")
            obj.set("label", label_to_html(label))
            for k, v in extras:
                obj.set(k, v)
            obj.set("id", eid)
            holder = obj
        else:
            mx_attrs["id"] = eid
            mx_attrs["value"] = label_to_html(label)
        cell = ET.SubElement(holder, "mxCell")
        for k in ("id", "value", "style", "edge", "source", "target", "parent"):
            if k in mx_attrs:
                cell.set(k, mx_attrs[k])
        g = geom(cell, relative=True)
        for ep, tag in ((ends[0], "sourcePoint"), (ends[1], "targetPoint")):
            if ep[0] == "pt":
                sub(g, "mxPoint", x=fmt(ep[1]), y=fmt(ep[2]), **{"as": tag})
        pts = [p for p in mids if p[0] == "pt"]
        if pts:
            arr = sub(g, "Array", **{"as": "points"})
            for p in pts:
                sub(arr, "mxPoint", x=fmt(p[1]), y=fmt(p[2]))

        for i, (pos, txt, lref) in enumerate(labels):
            lid = "%s-label%d" % (eid, i + 1)
            while lid in ids:
                lid += "x"
            ids.add(lid)
            lst = EDGELABEL_STYLE if lref is None else style_of(lref)
            lc = sub(root, "mxCell", id=lid, value=label_to_html(txt),
                     style=lst, vertex="1", connectable="0", parent=eid)
            lg = geom(lc, x=pos[0], y=pos[1], relative=True)
            sub(lg, "mxPoint", x=fmt(pos[2]), y=fmt(pos[3]), **{"as": "offset"})

    for eid, tag, cid, kept in dangling:
        sys.stderr.write(
            "边 %s 的 %s 指向 .rd 里没有的 cell %s —— %s"
            "（--bbox 切出来的 .rd 正常会这样）\n"
            % (eid, tag, cid, "已退回成悬空端点，位置不变" if kept else "这一端丢了"))
    return dia


def raw_style_dict(s):
    d = {}
    for part in (s or "").split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip()] = v.strip()
        elif part.strip():
            d[part.strip()] = ""
    return d


def indent(el, level=0):
    pad = "\n" + "  " * level
    if len(el):
        if not (el.text or "").strip():
            el.text = pad + "  "
        for c in el:
            indent(c, level + 1)
        if not (el[-1].tail or "").strip():
            el[-1].tail = pad
    if level and not (el.tail or "").strip():
        el.tail = pad


def expand(text):
    pages = parse_rd(text)
    if not pages:
        raise SystemExit("这份 .rd 里一个 cell 都没有")
    mxfile = ET.Element("mxfile", {"host": "drawio_expand", "type": "device"})
    for i, p in enumerate(pages, 1):
        mxfile.append(build_page(p, i))
    indent(mxfile)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            + ET.tostring(mxfile, encoding="unicode") + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(description="把 .rd 还原回 .drawio")
    ap.add_argument("infile")
    ap.add_argument("-o", "--out", help="输出文件，默认 stdout")
    args = ap.parse_args(argv)

    with open(args.infile, encoding="utf-8") as fh:
        result = expand(fh.read())

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(result)
    else:
        sys.stdout.write(result)

    src = os.path.getsize(args.infile)
    print("\n---- %d -> %d bytes" % (src, len(result.encode("utf-8"))),
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
