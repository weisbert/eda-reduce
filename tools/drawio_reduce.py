#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
drawio_reduce.py — 把 .drawio 压成「只剩语义信息」的紧凑文本，供大模型重建电路/架构图。

设计原则：
  1. 脚本只做确定性变换（坐标解算、样式去重、文本清洗），不做任何语义判断。
     不判 d/g/s、不合并节点、不猜走线路径、不关联浮动文本、不起网络名。
  2. 保留策略是黑名单：只丢已知纯渲染用途的样式 key，未知 key 一律保留。
  3. 宁可多输出、不解释。脚本猜错了下游看不出来，下游猜错了人对着图能看出来。

用法：
    python drawio_reduce.py my.drawio
    python drawio_reduce.py my.drawio -o my.rd
    python drawio_reduce.py my.drawio --bbox 400,300,800,700

只依赖 Python 标准库 (3.8+)。
"""

import argparse
import base64
import html
import math
import os
import re
import sys
import urllib.parse
import zlib
import xml.etree.ElementTree as ET
from collections import OrderedDict

# --------------------------------------------------------------- 样式黑名单

# 纯渲染用途，丢掉不影响语义
DROP = {
    "html", "shadow", "jettySize", "orthogonalLoop", "pointerEvents", "resizable",
    "rotatable", "snapToPoint", "perimeter", "sketch", "fillStyle", "aspect",
    "align", "verticalAlign", "verticalLabelPosition", "labelPosition",
    "whiteSpace", "arcSize", "glass", "comic", "edgeStyle", "startSize", "endSize",
    "points", "movable", "deletable", "editable", "connectable", "noLabel",
    "autosize", "fontFamily", "fontSource", "outlineConnect", "backgroundOutline",
}

# 取默认值时丢掉，非默认值保留
DROP_IF_ZERO = {
    "exitDx", "exitDy", "entryDx", "entryDy", "exitPerimeter", "entryPerimeter",
    "flipH", "flipV", "dashed", "rounded",
}

# 已被解算成绝对坐标，边上不再重复输出
EDGE_CONSUMED = {
    "exitX", "exitY", "entryX", "entryY", "exitDx", "exitDy",
    "entryDx", "entryDy", "exitPerimeter", "entryPerimeter",
}

# --------------------------------------------------------------- drawio 解析


def _inflate(payload):
    raw = base64.b64decode(payload)
    try:
        txt = zlib.decompress(raw, -15).decode("utf-8")
    except zlib.error:
        txt = zlib.decompress(raw).decode("utf-8")
    return urllib.parse.unquote(txt)


def load_pages(path):
    """-> [(page_name, mxGraphModel_element), ...]，兼容压缩/非压缩存储。"""
    root = ET.parse(path).getroot()
    if root.tag == "mxGraphModel":
        return [("page1", root)]
    pages = []
    for i, dia in enumerate(root.iter("diagram")):
        name = dia.get("name") or "page%d" % (i + 1)
        inner = dia.find("mxGraphModel")
        if inner is None:
            payload = (dia.text or "").strip()
            if not payload:
                continue
            inner = ET.fromstring(_inflate(payload))
        pages.append((name, inner))
    return pages


def iter_cells(model):
    """<root> 下的扁平遍历。-> (id, label, mxCell元素, 自定义属性dict)"""
    root = model.find("root")
    if root is None:
        return
    for el in root:
        if el.tag in ("object", "UserObject"):
            mx = el.find("mxCell")
            if mx is None:
                continue
            extra = {k: v for k, v in el.attrib.items()
                     if k not in ("id", "label", "placeholders")}
            yield el.get("id"), el.get("label", ""), mx, extra
        elif el.tag == "mxCell":
            yield el.get("id"), el.get("value", ""), el, {}


def parse_style(s):
    d = OrderedDict()
    for part in (s or "").split(";"):
        part = part.strip()
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            d[k.strip()] = v.strip()
        else:
            d[part] = ""
    return d


def canon_style(st, consumed=()):
    """归一化残留样式，shape 放最前，其余按 key 排序以便去重。"""
    shape = None
    items = []
    for k, v in st.items():
        if k in DROP or k in consumed:
            continue
        if k in DROP_IF_ZERO and v in ("0", "0.0", "none", ""):
            continue
        if k == "shape":
            shape = v
            continue
        items.append((k, v))
    parts = []
    if shape:
        parts.append(shape.replace("mxgraph.", ""))
    for k, v in sorted(items):
        parts.append(k if v == "" else "%s=%s" % (k, v))
    return " ".join(parts) or "-"


# --------------------------------------------------------------- 文本清洗


def clean_label(v):
    """去 HTML / LaTeX 包装，但保留 <b>/<i> 的强调语义（转成 * / /）。"""
    if not v:
        return ""
    s = v
    s = re.sub(r"</?b>|</?strong>", "*", s, flags=re.I)
    s = re.sub(r"</?i>|</?em>", "/", s, flags=re.I)
    s = re.sub(r"<(br|div|p|tr|li)\b[^>]*>|</(div|p|tr|li)>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)          # sub/sup/span/font 等直接去标签不留空格
    s = html.unescape(s)
    s = s.replace("$$", "")
    s = re.sub(r"\*\s*\*|/\s*/", "", s)    # 清掉配对后变空的强调标记
    return " ".join(s.split())


def n(v):
    """坐标取整：够接近整数就输出整数，否则一位小数。"""
    r = round(float(v), 1)
    if abs(r - round(r)) < 0.05:
        return str(int(round(r)))
    return "%.1f" % r


def pt(p):
    return "%s,%s" % (n(p[0]), n(p[1]))


# --------------------------------------------------------------- 几何解算


def rot(px, py, deg, cx, cy):
    rad = math.radians(deg)
    co, si = math.cos(rad), math.sin(rad)
    dx, dy = px - cx, py - cy
    return (dx * co - dy * si + cx, dy * co + dx * si + cy)


def connection_point(v, px, py, ddx=0.0, ddy=0.0):
    """复刻 mxGraph.getConnectionPoint 的非 perimeter 分支：
       direction 旋转 bounds -> 取归一化点 -> flipH/flipV 绕中心镜像 -> 按 r2 旋转。
       顺序与 mxGraph 源码一致（先 flip 后 rotate）。"""
    x, y, w, h = v["bounds"]
    cx, cy = x + w / 2.0, y + h / 2.0
    d = v["style"].get("direction")
    r1 = {"north": 270, "west": 180, "south": 90}.get(d, 0)
    if d in ("north", "south"):
        bw, bh = h, w
        bx, by = cx - bw / 2.0, cy - bh / 2.0
    else:
        bw, bh = w, h
        bx, by = x, y
    ptx = bx + px * bw + ddx
    pty = by + py * bh + ddy
    if v["flipH"]:
        ptx = 2 * cx - ptx
    if v["flipV"]:
        pty = 2 * cy - pty
    try:
        r2 = float(v["style"].get("rotation", 0) or 0)
    except ValueError:
        r2 = 0.0
    r2 += r1
    if r2:
        ptx, pty = rot(ptx, pty, r2, cx, cy)
    return (ptx, pty)


def geom_of(cell):
    g = cell.find("mxGeometry")
    if g is None:
        return None
    try:
        return (float(g.get("x", 0) or 0), float(g.get("y", 0) or 0),
                float(g.get("width", 0) or 0), float(g.get("height", 0) or 0))
    except ValueError:
        return None


def edge_points(cell):
    """<Array as="points"> 显式折点 + 悬空 sourcePoint/targetPoint。"""
    g = cell.find("mxGeometry")
    if g is None:
        return None, None, []
    sp = tp = None
    mids = []
    for mp in g.findall("mxPoint"):
        try:
            p = (float(mp.get("x", 0) or 0), float(mp.get("y", 0) or 0))
        except ValueError:
            continue
        if mp.get("as") == "sourcePoint":
            sp = p
        elif mp.get("as") == "targetPoint":
            tp = p
    arr = g.find("Array")
    if arr is not None and arr.get("as") == "points":
        for mp in arr.findall("mxPoint"):
            try:
                mids.append((float(mp.get("x", 0) or 0), float(mp.get("y", 0) or 0)))
            except ValueError:
                pass
    return sp, tp, mids


# --------------------------------------------------------------- 主流程


def reduce_page(page_name, model, bbox=None):
    verts = OrderedDict()   # id -> {style(dict), bounds, flipH, flipV, label, extra}
    edges = OrderedDict()
    elabels = {}            # edge id -> [(relx, text)]

    for cid, raw_label, mx, extra in iter_cells(model):
        if cid in ("0", "1"):
            continue
        st = parse_style(mx.get("style"))
        rec = {
            "style": st,
            "label": clean_label(raw_label),
            "extra": extra,
        }
        if mx.get("edge") == "1":
            sp, tp, mids = edge_points(mx)
            rec.update(source=mx.get("source"), target=mx.get("target"),
                       sp=sp, tp=tp, mids=mids)
            edges[cid] = rec
            continue
        if mx.get("vertex") != "1":
            continue
        g = geom_of(mx)
        if g is None:
            continue
        rec.update(bounds=g,
                   flipH=st.get("flipH") == "1",
                   flipV=st.get("flipV") == "1",
                   parent=mx.get("parent"))
        if "edgeLabel" in st:
            relx = None
            mg = mx.find("mxGeometry")
            if mg is not None and mg.get("x") is not None:
                try:
                    relx = float(mg.get("x"))
                except ValueError:
                    pass
            elabels.setdefault(rec["parent"], []).append((relx, rec["label"]))
            continue
        verts[cid] = rec

    # ---- 解算每条边的两个端点
    def terminal(e, end):
        cid = e["source"] if end == "source" else e["target"]
        px = e["style"].get("exitX" if end == "source" else "entryX")
        py = e["style"].get("exitY" if end == "source" else "entryY")
        dx = e["style"].get("exitDx" if end == "source" else "entryDx", 0)
        dy = e["style"].get("exitDy" if end == "source" else "entryDy", 0)
        per = e["style"].get("exitPerimeter" if end == "source" else "entryPerimeter")
        if cid is None:
            p = e["sp"] if end == "source" else e["tp"]
            return ("@" + pt(p)) if p else "?"
        if cid not in verts:
            return "%s:?" % cid
        if px is None or py is None:
            # centerPerimeter 图元（waypoint 圆点）的连接点恒为中心，可确定性给出
            tv = verts[cid]
            if (tv["style"].get("perimeter") == "centerPerimeter"
                    or tv["style"].get("shape") == "waypoint"):
                bx, by, bw, bh = tv["bounds"]
                return "%s:%s" % (cid, pt((bx + bw / 2.0, by + bh / 2.0)))
            return "%s:?" % cid
        try:
            p = connection_point(verts[cid], float(px), float(py),
                                 float(dx or 0), float(dy or 0))
        except ValueError:
            return "%s:?" % cid
        # perimeter 投影未实现，非 0 时标记为近似
        mark = "" if per == "0" else "~"
        return "%s:%s%s" % (cid, mark, pt(p))

    for e in edges.values():
        e["a"] = terminal(e, "source")
        e["b"] = terminal(e, "target")

    # ---- bbox 裁剪
    def vert_in(v):
        if not bbox:
            return True
        x, y, w, h = v["bounds"]
        return not (x > bbox[2] or x + w < bbox[0] or y > bbox[3] or y + h < bbox[1])

    keep_v = {cid for cid, v in verts.items() if vert_in(v)}
    if bbox:
        keep_e = set()
        for eid, e in edges.items():
            for side in ("source", "target"):
                if e[side] in keep_v:
                    keep_e.add(eid)
            for p in [e["sp"], e["tp"]] + e["mids"]:
                if p and bbox[0] <= p[0] <= bbox[2] and bbox[1] <= p[1] <= bbox[3]:
                    keep_e.add(eid)
        edges = OrderedDict((k, v) for k, v in edges.items() if k in keep_e)
        verts = OrderedDict((k, v) for k, v in verts.items() if k in keep_v)

    # ---- 样式字典
    sdict, edict = OrderedDict(), OrderedDict()

    def ref(table, prefix, key):
        if key not in table:
            table[key] = ["%s%d" % (prefix, len(table) + 1), 0]
        table[key][1] += 1
        return table[key][0]

    lines = []
    for cid, v in verts.items():
        st = v["style"]
        if st.get("shape") == "waypoint":
            lines.append(("J", cid, v))
            continue
        v["ref"] = ref(sdict, "S", canon_style(st))
        lines.append(("T" if ("text" in st and "shape" not in st) else "V", cid, v))
    for eid, e in edges.items():
        e["ref"] = ref(edict, "E", canon_style(e["style"], EDGE_CONSUMED))

    # ---- 输出
    out = []
    out.append("## page %s  vertices=%d edges=%d" % (page_name, len(verts), len(edges)))
    out.append("## V 图元 | T 文本 | J 结点 | W 连线")
    out.append("## 端点  id:x,y=接在图元上 | @x,y=悬空或折点 | id:?=无约束点 | ~=近似(未做perimeter投影)")
    out.append("")
    out.append("## styles")
    for key, (name, cnt) in sdict.items():
        out.append("%-4s x%-3d %s" % (name, cnt, key))
    for key, (name, cnt) in edict.items():
        out.append("%-4s x%-3d %s" % (name, cnt, key))
    out.append("")
    out.append("## cells")
    for kind, cid, v in lines:
        x, y, w, h = v["bounds"]
        extra = "".join(" {%s=%s}" % (k, vv) for k, vv in sorted(v["extra"].items()))
        lab = '  "%s"' % v["label"] if v["label"] else ""
        if kind == "J":
            out.append("J %-4s %s" % (cid, pt((x + w / 2.0, y + h / 2.0))))
        else:
            out.append("%s %-4s %-3s %s,%s,%s,%s%s%s"
                       % (kind, cid, v["ref"], n(x), n(y), n(w), n(h), lab, extra))
    for eid, e in edges.items():
        chain = [e["a"]] + ["@" + pt(p) for p in e["mids"]] + [e["b"]]
        lab = '  "%s"' % e["label"] if e["label"] else ""
        for relx, txt in elabels.get(eid, []):
            lab += '  |"%s"%s' % (txt, "" if relx is None else "@%s" % n(relx))
        extra = "".join(" {%s=%s}" % (k, vv) for k, vv in sorted(e["extra"].items()))
        out.append("W %-4s %-3s %s%s%s" % (eid, e["ref"], " > ".join(chain), lab, extra))
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description="把 .drawio 压成紧凑语义文本")
    ap.add_argument("infile")
    ap.add_argument("-o", "--out", help="输出文件，默认 stdout")
    ap.add_argument("--bbox", help="只导出与该区域相交的部分: x1,y1,x2,y2")
    args = ap.parse_args()

    bbox = None
    if args.bbox:
        try:
            v = [float(t) for t in args.bbox.split(",")]
            bbox = (min(v[0], v[2]), min(v[1], v[3]), max(v[0], v[2]), max(v[1], v[3]))
        except (ValueError, IndexError):
            ap.error("--bbox 格式应为 x1,y1,x2,y2")

    chunks = ["# %s" % os.path.basename(args.infile)]
    for name, model in load_pages(args.infile):
        chunks.append(reduce_page(name, model, bbox))
    result = "\n\n".join(chunks) + "\n"

    if args.out:
        with open(args.out, "w", encoding="utf-8", newline="\n") as f:
            f.write(result)
    else:
        sys.stdout.write(result)

    src = os.path.getsize(args.infile)
    dst = len(result.encode("utf-8"))
    print("\n---- %d -> %d bytes  (%.1fx)" % (src, dst, src / dst if dst else 0),
          file=sys.stderr)


if __name__ == "__main__":
    main()
