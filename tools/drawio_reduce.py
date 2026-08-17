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

# 闸门只有一条：**这个 key 变了，画出来的图会不会变？**
# 不会变的（纯编辑器/交互元数据）才丢。会变的一律留 —— 丢了就还原不回来，
# 而留下来的代价是每个「去重后的样式」几十字符，不是每个 cell。
DROP = {
    "pointerEvents", "resizable", "rotatable", "movable", "deletable", "editable",
    "connectable", "autosize", "snapToPoint", "aspect", "outlineConnect",
    "backgroundOutline", "expand", "recursiveResize", "container",
    # 标签统一按转义后的纯文本带出，expand 一律按 html=1 写回
    "html",
}

# 纯开关型、缺省即关闭的 key：取默认值才丢。
#
# 谁**不在**这张表里，以及为什么（三条都是渲染实测出来的，别按字面猜；
# 每条在 tests/test_drawio.py::TestPreservedInfo 里有一个断言钉着）：
#   align / verticalAlign / *LabelPosition / whiteSpace
#       默认值不是常数。drawio 的内置具名样式（`text;` `label;` …）自带一套，
#       `text;` 的 align 默认是 left 不是 center。猜错 = 标签整体挪位置。
#   rounded
#       边上缺省画的是**圆角**，顶点上缺省是直角。所以边的 rounded=0 是非默认值。
#   jettySize
#       `auto` 不等于缺省。缺省是常数 10，auto 是按箭头大小现算的，
#       正交边第一段长度会差十几像素。
DROP_IF_DEFAULT = {
    "fillStyle": "solid", "points": "[]",
    "shadow": "0", "sketch": "0", "glass": "0", "comic": "0", "noLabel": "0",
}

# 只在「这个 cell 有标签」时才影响画面的 key：无标签的 cell 上一律不输出。
# 电路图里绝大多数图元（管子、电容、结点）都没标签，这一条把 A 的代价按住了。
LABEL_ONLY = {
    "align", "verticalAlign", "labelPosition", "verticalLabelPosition",
    "whiteSpace", "noLabel", "labelBackgroundColor", "labelBorderColor",
    "textOpacity", "spacing", "spacingTop", "spacingBottom",
    "spacingLeft", "spacingRight", "textShadow", "overflow",
}

# 取默认值时丢掉，非默认值保留
DROP_IF_ZERO = {
    "exitDx", "exitDy", "entryDx", "entryDy", "exitPerimeter", "entryPerimeter",
    "flipH", "flipV", "dashed",
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


def canon_style(st, consumed=(), labeled=True):
    """归一化残留样式，shape 放最前，其余按 key 排序以便去重。

    样式串以空格分词，所以带空格的值（`dashPattern=8 8`、`fontFamily=Times New
    Roman`）要加引号，否则 expand 分不回去。
    """
    shape = None
    items = []
    named = []      # 无值的词是 drawio 内置具名样式（text / ellipse / swimlane …）
    for k, v in st.items():
        if k in DROP or k in consumed:
            continue
        if k in DROP_IF_ZERO and v in ("0", "0.0", "none", ""):
            continue
        if DROP_IF_DEFAULT.get(k) == v:
            continue
        if not labeled and k in LABEL_ONLY:
            continue
        if k == "shape":
            shape = v
            continue
        if v == "":
            named.append(k)
            continue
        items.append((k, v))
    parts = []
    if shape:
        # `mxgraph.a.b` 缩成 `a.b`：留了个点，expand 才认得出这是 stencil 名而不是
        # `ellipse` / `triangle` 这类无值样式 key。缩不了的原样写 `shape=v`。
        if shape.startswith("mxgraph.") and "." in shape[8:]:
            parts.append(shape[8:])
        else:
            parts.append("shape=" + shape)
    # 具名样式必须排在所有 k=v 前面、且保持原有先后：mxGraph 是从左到右合并的，
    # `align=center;text;` 会被 text 自带的 align=left 顶掉。按字母排序会踩这个坑。
    parts.extend(named)
    for k, v in sorted(items):
        if " " in v:
            v = '"%s"' % v
        parts.append("%s=%s" % (k, v))
    return " ".join(parts) or "-"


# --------------------------------------------------------------- 文本清洗


def clean_label(v):
    """去 HTML / LaTeX 包装，但保留 <b>/<i> 的强调语义（转成 * / /）。

    正文里本来就有的 `*` 转义成 `\\*`（`\\` 转义成 `\\\\`）—— 不然 `2*C` 会被
    expand 当成加粗标记读回去。`/` 不转义：`clk/2` 这类名字太常见，
    转义的噪声比换回斜体的收益大，所以斜体是**单向**的（见 rd-spec.md §7）。
    """
    if not v:
        return ""
    s = v
    s = re.sub(r"</?b>|</?strong>", "\x01", s, flags=re.I)
    s = re.sub(r"</?i>|</?em>", "\x02", s, flags=re.I)
    s = re.sub(r"<(br|div|p|tr|li)\b[^>]*>|</(div|p|tr|li)>", " ", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)          # sub/sup/span/font 等直接去标签不留空格
    s = html.unescape(s)
    s = s.replace("$$", "")
    s = re.sub(r"[\x01]\s*[\x01]|[\x02]\s*[\x02]", "", s)  # 配对后变空的强调标记
    # 正文里的字面量：`\` `*` 会撞上强调标记，`"` 会撞上 .rd 的引号
    s = s.replace("\\", "\\\\").replace("*", "\\*").replace('"', '\\"')
    s = s.replace("\x01", "*").replace("\x02", "/")
    return " ".join(s.split())


def n(v):
    """坐标取整：够接近整数就输出整数，否则一位小数。"""
    r = round(float(v), 1)
    if abs(r - round(r)) < 0.05:
        return str(int(round(r)))
    return "%.1f" % r


def pt(p):
    return "%s,%s" % (n(p[0]), n(p[1]))


def trail(vals):
    """尾零截掉的坐标串，全零就整个省掉：[x,0,0,0] -> '@x'，[0,0,0,0] -> ''。"""
    v = list(vals)
    while v and float(v[-1]) == 0:
        v.pop()
    return "@" + ",".join(n(t) for t in v) if v else ""


# --------------------------------------------------------------- 页面属性

# mxGraphModel 上「缺省时 drawio 用什么」。等于默认值的不输出。
MODEL_DEFAULT = {
    "grid": "1", "gridSize": "10", "guides": "1", "tooltips": "1", "connect": "1",
    "arrows": "1", "fold": "1", "page": "1", "pageScale": "1",
    "pageWidth": "850", "pageHeight": "1100", "math": "0", "shadow": "0",
}
# dx/dy 是编辑器视口的滚动位置，跟画出来的东西无关
MODEL_SKIP = {"dx", "dy", "pageWidth", "pageHeight"}


def page_line(name, model):
    """P 行：页名 + 页面尺寸 + 其余非默认的 mxGraphModel 属性。

    导 PNG 时不带 --crop 就是按页面尺寸出图，丢了这行两张图连画布都不一样大。
    """
    a = model.attrib
    extra = "".join(
        " {%s=%s}" % (k, a[k]) for k in sorted(a)
        if k not in MODEL_SKIP and MODEL_DEFAULT.get(k) != a[k])
    return 'P "%s"  %sx%s%s' % (
        name.replace("\\", "\\\\").replace('"', '\\"'),
        n(a.get("pageWidth") or MODEL_DEFAULT["pageWidth"]),
        n(a.get("pageHeight") or MODEL_DEFAULT["pageHeight"]), extra)


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


# drawio 建 waypoint 结点 / 边标签时写死的样式串。归一化后等于这个值、且尺寸也是
# 默认的，才用紧凑的 J 行 / 光秃秃的 |"…" 写法；否则退回带样式引用的完整写法。
# expand 靠同样两个字面量写回去 —— 改这里要同步改 drawio_expand.py。
WAYPOINT_STYLE = ("shape=waypoint;sketch=0;fillStyle=solid;size=6;pointerEvents=1;"
                  "points=[];fillColor=none;resizable=0;rotatable=0;"
                  "perimeter=centerPerimeter;snapToPoint=1;")
WAYPOINT_SIZE = 20.0
EDGELABEL_STYLE = ("edgeLabel;html=1;align=center;verticalAlign=middle;"
                   "resizable=0;points=[];")


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
            # 边标签的位置 = 沿线相对位置 x(-1..1) + 垂直偏移 y + 像素 offset。
            # 只带 x 的话拖过的标签会弹回线上，所以四个都带。
            pos = [0.0, 0.0, 0.0, 0.0]
            mg = mx.find("mxGeometry")
            if mg is not None:
                for i, k in enumerate(("x", "y")):
                    try:
                        pos[i] = float(mg.get(k) or 0)
                    except ValueError:
                        pass
                for mp in mg.findall("mxPoint"):
                    if mp.get("as") == "offset":
                        try:
                            pos[2] = float(mp.get("x") or 0)
                            pos[3] = float(mp.get("y") or 0)
                        except ValueError:
                            pass
            elabels.setdefault(rec["parent"], []).append(
                (pos, rec["label"], st))
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

    wp_canon = canon_style(parse_style(WAYPOINT_STYLE), labeled=False)
    # 有没有标签会影响 LABEL_ONLY 那一闸，所以两种都要备着
    el_canon = {b: canon_style(parse_style(EDGELABEL_STYLE), labeled=b)
                for b in (False, True)}

    lines = []
    for cid, v in verts.items():
        st = v["style"]
        cs = canon_style(st, labeled=bool(v["label"]))
        if (st.get("shape") == "waypoint" and cs == wp_canon and not v["label"]
                and v["bounds"][2] == WAYPOINT_SIZE == v["bounds"][3]):
            lines.append(("J", cid, v))
            continue
        v["ref"] = ref(sdict, "S", cs)
        lines.append(("T" if ("text" in st and "shape" not in st) else "V", cid, v))
    for eid, e in edges.items():
        e["ref"] = ref(edict, "E", canon_style(e["style"], EDGE_CONSUMED,
                                               labeled=bool(e["label"])))
        # 边标签：非默认样式才给引用，默认的（drawio 自己写的那串）省掉
        out_lab = []
        for pos, txt, lst in elabels.get(eid, []):
            cs = canon_style(lst, labeled=bool(txt))
            out_lab.append((pos, txt, None if cs == el_canon[bool(txt)]
                            else ref(sdict, "S", cs)))
        e["labels"] = out_lab

    # ---- 输出
    out = []
    out.append("## page %s  vertices=%d edges=%d" % (page_name, len(verts), len(edges)))
    out.append("## P 页面 | V 图元 | T 文本 | J 结点 | W 连线")
    out.append("## 端点  id:x,y=接在图元上 | @x,y=悬空或折点 | id:?=无约束点 | ~=近似(未做perimeter投影)")
    out.append("")
    out.append(page_line(page_name, model))
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
        for pos, txt, sref in e["labels"]:
            lab += '  |"%s"%s%s' % (txt, trail(pos), " " + sref if sref else "")
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
