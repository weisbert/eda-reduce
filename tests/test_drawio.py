# -*- coding: utf-8 -*-
"""`.drawio` ⇄ `.rd` 的来回。

这套测试的主心骨是**定点**：`reduce(expand(reduce(x))) == reduce(x)`。
它一句话覆盖住「reduce 丢了什么」和「expand 猜错了什么」两类错误 ——
只要有一位信息在路上掉了或者被 expand 补错了，第二遍 reduce 的输出就对不上。
它还不用装任何东西、不用渲染，所以能当日常回归跑。

定点管不住的只有一类：reduce 和 expand **同时**认错同一个 drawio 默认值
（两边一致地错，绕一圈还是自洽）。这一类只能靠真渲染来验，见
`TestRenderedPixels` —— 本机装了 draw.io 才跑，没装就 skip。
`rounded` / `jettySize` / `text;` 三个坑就是这么发现的，注释里记了实测结论。
"""

import io
import os
import re
import subprocess
import sys
import tempfile
import unittest

from _common import ROOT, TOOLS

sys.path.insert(0, TOOLS)

import drawio_reduce as R          # noqa: E402
import drawio_expand as X          # noqa: E402

DEMO = os.path.join(ROOT, "examples", "demo.drawio")
DEMO_RD = os.path.join(ROOT, "examples", "demo.rd")


def reduce_file(path, bbox=None):
    chunks = ["# %s" % os.path.basename(path)]
    for name, model in R.load_pages(path):
        chunks.append(R.reduce_page(name, model, bbox))
    return "\n\n".join(chunks) + "\n"


def reduce_xml(xml, bbox=None):
    fd, p = tempfile.mkstemp(suffix=".drawio")
    os.close(fd)
    try:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(xml)
        return reduce_file(p, bbox)
    finally:
        os.unlink(p)


def roundtrip(rd):
    """.rd -> .drawio -> .rd"""
    fd, p = tempfile.mkstemp(suffix=".drawio")
    os.close(fd)
    try:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(X.expand(rd))
        return reduce_file(p)
    finally:
        os.unlink(p)


def body(rd):
    """去掉第一行的文件名 —— 来回一趟文件名当然会变。"""
    return rd.split("\n", 1)[1]


def wrap(cells, model='pageWidth="850" pageHeight="1100"', name="p"):
    return ('<?xml version="1.0" encoding="UTF-8"?><mxfile host="t">'
            '<diagram name="%s" id="i"><mxGraphModel %s>'
            '<root><mxCell id="0"/><mxCell id="1" parent="0"/>%s'
            '</root></mxGraphModel></diagram></mxfile>' % (name, model, cells))


def vertex(cid, style, x=40, y=40, w=80, h=60, value=""):
    return ('<mxCell id="%s" value="%s" style="%s" vertex="1" parent="1">'
            '<mxGeometry x="%s" y="%s" width="%s" height="%s" as="geometry"/>'
            '</mxCell>' % (cid, value, style, x, y, w, h))


TWO_BOXES = (vertex("a", "whiteSpace=wrap;html=1;rounded=0;", 40, 40)
             + vertex("b", "whiteSpace=wrap;html=1;rounded=0;", 300, 200))


class TestFixedPoint(unittest.TestCase):
    """.rd 必须是 reduce∘expand 的定点。掉一位信息就对不上。"""

    def _fix(self, xml, msg=""):
        a = reduce_xml(xml)
        b = roundtrip(a)
        self.assertEqual(body(a), body(b), msg + "\n--- 第一遍 ---\n" + a
                         + "\n--- 绕一圈之后 ---\n" + b)
        return a

    def test_demo(self):
        a = reduce_file(DEMO)
        self.assertEqual(body(a), body(roundtrip(a)))

    def test_demo_bbox(self):
        """--bbox 切出来的 .rd 里有指向被切掉图元的边，也得是定点。"""
        a = reduce_file(DEMO, bbox=(300, 350, 600, 500))
        self.assertIn(":?", a + "x")     # 确认这个用例真的切出了悬空引用
        b = roundtrip(a)
        # 被切掉的那一端 expand 会退成悬空点（@x,y），所以只比图元和链的形状
        self.assertEqual(len(a.splitlines()), len(b.splitlines()))

    def test_multipage(self):
        xml = ('<?xml version="1.0" encoding="UTF-8"?><mxfile host="t">'
               '<diagram name="one" id="1"><mxGraphModel pageWidth="700" '
               'pageHeight="620" background="#fff9e0">'
               '<root><mxCell id="0"/><mxCell id="1" parent="0"/>'
               + TWO_BOXES + '</root></mxGraphModel></diagram>'
               '<diagram name="page two" id="2"><mxGraphModel pageWidth="400" '
               'pageHeight="300" grid="0" gridSize="5" math="1" shadow="1">'
               '<root><mxCell id="0"/><mxCell id="1" parent="0"/>'
               + vertex("c", "rounded=1;html=1;", value="second")
               + '</root></mxGraphModel></diagram></mxfile>')
        a = self._fix(xml, "多页")
        self.assertIn('P "one"  700x620 {background=#fff9e0}', a)
        self.assertIn('P "page two"  400x300 {grid=0} {gridSize=5} '
                      '{math=1} {shadow=1}', a)

    def test_rotation_flip_direction(self):
        """端点坐标是正着算出来的，expand 必须反着算回去 ——
        flipH 的图元上直接 (x-bx)/bw 会差一整个宽度。"""
        for extra in ("flipH=1;", "flipV=1;", "flipH=1;flipV=1;",
                      "direction=north;", "direction=south;", "direction=west;",
                      "rotation=37;", "rotation=-90;flipH=1;",
                      "direction=south;rotation=20;flipV=1;"):
            xml = wrap(
                vertex("v", "shape=mxgraph.electrical.transistors.nmos;html=1;"
                       + extra, 80, 60, 60, 40)
                + vertex("t", "rounded=0;html=1;", 300, 300)
                + '<mxCell id="e" style="html=1;rounded=0;exitX=1;exitY=0.25;'
                  'exitDx=0;exitDy=0;exitPerimeter=0;" edge="1" parent="1" '
                  'source="v" target="t"><mxGeometry relative="1" '
                  'as="geometry"/></mxCell>')
            self._fix(xml, "变换: " + extra)

    def test_label_escapes(self):
        """`*` `\\` `"` 在 .rd 里都有别的意思，来回一趟必须原样。"""
        for v in ("2*C", "a*b*c", "c&#92;d", "say &quot;hi&quot;",
                  "clk/2 div/4", "&lt;b&gt;bold&lt;/b&gt; x", "*", "**", "* *"):
            self._fix(wrap(vertex("v", "rounded=0;html=1;", value=v)),
                      "标签: " + v)

    def test_style_value_with_space(self):
        """样式串按空格分词，带空格的值得能分回去。"""
        a = self._fix(wrap(vertex(
            "v", "rounded=0;html=1;dashed=1;dashPattern=8 8;"
                 "fontFamily=Times New Roman;", value="x")), "带空格的样式值")
        self.assertIn('dashPattern="8 8"', a)
        self.assertIn('fontFamily="Times New Roman"', a)

    def test_edge_label_position(self):
        for geo in ('x="-0.6"', 'x="0.3" y="12"', 'y="-20"',
                    'x="0.5" y="4"'):
            for off in ("", '<mxPoint x="15" y="-8" as="offset"/>'):
                xml = wrap(TWO_BOXES + '<mxCell id="e" style="html=1;rounded=0;"'
                           ' edge="1" parent="1" source="a" target="b">'
                           '<mxGeometry relative="1" as="geometry"/></mxCell>'
                           '<mxCell id="el" value="lbl" style="edgeLabel;html=1;'
                           'align=center;verticalAlign=middle;resizable=0;'
                           'points=[];" vertex="1" connectable="0" parent="e">'
                           '<mxGeometry %s relative="1" as="geometry">%s'
                           '</mxGeometry></mxCell>' % (geo, off))
                self._fix(xml, "边标签几何: %s %s" % (geo, off))

    def test_styled_edge_label(self):
        a = self._fix(wrap(
            TWO_BOXES + '<mxCell id="e" style="html=1;rounded=0;" edge="1" '
            'parent="1" source="a" target="b"><mxGeometry relative="1" '
            'as="geometry"/></mxCell><mxCell id="el" value="lbl" '
            'style="edgeLabel;html=1;align=left;fontColor=#0000ff;" vertex="1" '
            'connectable="0" parent="e"><mxGeometry x="0.2" relative="1" '
            'as="geometry"/></mxCell>'), "带样式的边标签")
        self.assertRegex(a, r'\|"lbl"@0\.2 S\d')

    def test_object_custom_props(self):
        a = self._fix(wrap(
            '<object label="n1" W="10u" L="0.5u" note="input pair" id="v">'
            + vertex("", "rounded=0;html=1;").replace('id="" value="" ', "")
            + '</object>'), "Ctrl+M 自定义属性")
        self.assertIn("{note=input pair}", a)

    def test_waypoint_shapes(self):
        """默认尺寸+默认样式的 waypoint 缩成 J 行；改过的退回 V 行，两种都要定点。"""
        canon = R.WAYPOINT_STYLE
        a = self._fix(wrap(vertex("w", canon, 100, 100, 20, 20)), "标准 waypoint")
        self.assertRegex(a, r"(?m)^J w\s+110,110$")
        b = self._fix(wrap(vertex("w", canon.replace("size=6", "size=10"),
                                  100, 100, 30, 30)), "改过的 waypoint")
        self.assertNotIn("\nJ ", b)

    def test_self_loop(self):
        self._fix(wrap(vertex("v", "rounded=0;html=1;")
                       + '<mxCell id="e" style="edgeStyle=orthogonalEdgeStyle;'
                         'rounded=0;html=1;exitX=1;exitY=1;exitDx=0;exitDy=0;'
                         'exitPerimeter=0;entryX=0;entryY=0.5;entryDx=0;'
                         'entryDy=0;entryPerimeter=0;" edge="1" parent="1" '
                         'source="v" target="v"><mxGeometry relative="1" '
                         'as="geometry"/></mxCell>'), "自环边")

    def test_floating_and_waypoints(self):
        self._fix(wrap(
            '<mxCell id="e" style="html=1;rounded=0;endArrow=none;" edge="1" '
            'parent="1"><mxGeometry relative="1" as="geometry">'
            '<mxPoint x="120" y="120" as="sourcePoint"/>'
            '<mxPoint x="560" y="120" as="targetPoint"/>'
            '<Array as="points"><mxPoint x="300" y="200"/>'
            '<mxPoint x="400" y="90"/></Array>'
            '</mxGeometry></mxCell>'), "两端悬空 + 显式折点")



class TestPreservedInfo(unittest.TestCase):
    """A 的验收：这几类「丢了就回不来」的 key 必须出现在 .rd 里。

    每一条都配了实测出来的理由，别看着眼熟就往 DROP 里挪。
    """

    def _style(self, cellstyle, edge=False, value="x"):
        if edge:
            cells = (TWO_BOXES + '<mxCell id="e" value="%s" style="%s" edge="1" '
                     'parent="1" source="a" target="b"><mxGeometry '
                     'relative="1" as="geometry"/></mxCell>' % (value, cellstyle))
        else:
            cells = vertex("v", cellstyle, value=value)
        rd = reduce_xml(wrap(cells))
        pre = "E" if edge else "S"
        got = [l for l in rd.splitlines()
               if re.match(r"^%s\d+\s+x\d" % pre, l)]
        return " ".join(got)

    def test_edge_routing_survives(self):
        """正交边不存折点。丢了 edgeStyle 就只能连成斜线 —— 这是最贵的一条。"""
        self.assertIn("edgeStyle=orthogonalEdgeStyle",
                      self._style("edgeStyle=orthogonalEdgeStyle;html=1;", True))

    def test_edge_rounded_zero_survives(self):
        """实测：边的 style 里没有 rounded 时 drawio 画**圆角**，不是直角。
        所以 rounded=0 是非默认值，不能当零丢掉。"""
        self.assertIn("rounded=0", self._style(
            "edgeStyle=orthogonalEdgeStyle;rounded=0;html=1;", True))

    def test_jetty_size_survives(self):
        """实测：缺省 jettySize 是常数 10，`auto` 是按箭头算的，
        正交边第一段长度会差十几像素。auto 不等于缺省。"""
        self.assertIn("jettySize=auto", self._style(
            "edgeStyle=orthogonalEdgeStyle;jettySize=auto;html=1;", True))

    def test_label_alignment_survives(self):
        s = self._style("text;html=1;align=left;verticalAlign=top;")
        self.assertIn("align=left", s)
        self.assertIn("verticalAlign=top", s)

    def test_label_position_survives(self):
        """电路符号的名字挂在图形下面靠的就是这两个 key。"""
        s = self._style("shape=mxgraph.electrical.signal_sources.vss2;html=1;"
                        "verticalLabelPosition=bottom;verticalAlign=top;",
                        value="Vss")
        self.assertIn("verticalLabelPosition=bottom", s)

    def test_named_style_comes_first(self):
        """实测：`text;` 是 drawio 的具名样式，自带 align=left。mxGraph 从左往右
        合并，所以它必须排在所有 k=v 前面，否则 align=center 会被它顶掉。"""
        s = self._style("align=center;verticalAlign=middle;text;html=1;")
        self.assertLess(s.index("text"), s.index("align=center"))
        self.assertLess(s.index("text"), s.index("verticalAlign=middle"))

    def test_no_label_drops_layout_keys(self):
        """没标签的图元不带排版 key —— A 的代价就是靠这一条按住的。"""
        s = self._style("shape=mxgraph.electrical.transistors.nmos;html=1;"
                        "verticalLabelPosition=bottom;align=center;"
                        "verticalAlign=top;", value="")
        self.assertNotIn("align", s)
        self.assertNotIn("verticalLabelPosition", s)

    def test_subscript_is_flattened(self):
        """下标是**有意**压平的（下游要的是 `Vss` 这个名字），所以来回一趟回不来。
        改这条之前先想清楚 .ckt 里的网络名要不要跟着变。"""
        self.assertEqual(R.clean_label("V<sub>ss</sub>"), "Vss")
        self.assertEqual(X.label_to_html("Vss"), "Vss")

    def test_bold_round_trips(self):
        self.assertEqual(R.clean_label("<b>OTA</b>"), "*OTA*")
        self.assertEqual(X.label_to_html("*OTA*"), "<b>OTA</b>")

    def test_italic_is_one_way(self):
        """`/` 在 `clk/2` 里太常见，reduce 侧没给它转义，所以 expand 不还原斜体。"""
        self.assertEqual(R.clean_label("<i>x</i>"), "/x/")
        self.assertEqual(X.label_to_html("/x/"), "/x/")

    def test_interaction_keys_still_dropped(self):
        s = self._style("rounded=0;html=1;pointerEvents=1;resizable=0;"
                        "movable=1;editable=0;autosize=1;aspect=fixed;")
        for k in ("pointerEvents", "resizable", "movable", "editable",
                  "autosize", "aspect", "html"):
            self.assertNotIn(k, s, k + " 不该留在 .rd 里")


class TestDeterminism(unittest.TestCase):

    def test_reduce_twice(self):
        self.assertEqual(reduce_file(DEMO), reduce_file(DEMO))

    def test_expand_twice(self):
        rd = reduce_file(DEMO)
        self.assertEqual(X.expand(rd), X.expand(rd))

    def test_baseline(self):
        """examples/demo.rd 是提交进仓库的基线。变了不一定是错，但得看一眼 diff。

        确认之后更新：python tools/drawio_reduce.py examples/demo.drawio \\
                          -o examples/demo.rd
        """
        with open(DEMO_RD, encoding="utf-8") as fh:
            want = fh.read()
        import difflib
        got = reduce_file(DEMO)
        if got != want:
            d = "\n".join(list(difflib.unified_diff(
                want.splitlines(), got.splitlines(),
                "基线 demo.rd", "现在", lineterm=""))[:40])
            self.fail("和基线不一致，确认这个 diff 是你想要的变化之后按 "
                      "test_baseline 的注释更新：\n" + d)

    def test_expand_output_parses(self):
        import xml.etree.ElementTree as ET
        ET.fromstring(X.expand(reduce_file(DEMO)))


class TestLegacyRd(unittest.TestCase):
    """P 行是后加的。以前存下来的 .rd 还得能 expand。"""

    def test_no_p_line(self):
        rd = "\n".join(l for l in reduce_file(DEMO).splitlines()
                       if not l.startswith("P "))
        xml = X.expand(rd)
        self.assertIn('<diagram name="demo"', xml)
        self.assertIn('pageWidth="850"', xml)          # 退回默认页面尺寸


# ------------------------------------------------------- 压缩比 / 自己的尾行


def compressed_copy(src, dst):
    """把 .drawio 改存成 drawio 默认的 deflate+base64 形态。

    drawio 默认就是这么存的，所以用户手上的输入几乎总是这一种；
    examples/demo.drawio 存的是未压缩 XML，光靠它测不出「分母拿错了」。
    """
    import base64
    import urllib.parse
    import xml.etree.ElementTree as ET
    import zlib
    root = ET.parse(src).getroot()
    for dia in root.iter("diagram"):
        inner = dia.find("mxGraphModel")
        if inner is None:
            continue
        packed = urllib.parse.quote(ET.tostring(inner, encoding="unicode"), safe="")
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        dia.remove(inner)
        dia.text = base64.b64encode(
            co.compress(packed.encode("utf-8")) + co.flush()).decode("ascii")
    with open(dst, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(ET.tostring(root, encoding="unicode"))
    return dst


class TestReportedRatio(unittest.TestCase):
    """同一张图，压缩存的和未压缩存的，报出来的比例必须是同一个数。

    以前拿磁盘字节当分母：压缩存的报 0.5x、另存 XML 的报 5.2x，差一个数量级，
    而且 drawio 默认压缩存 —— 用户看到的几乎总是那个像「越压越大」的数。
    分子分母得是同一样东西，分母的定义是「下游不用这个脚本、直接读要吞多少字节」。
    """

    def _ratio(self, path):
        _, plain = R.source_sizes(path)
        return plain / float(len(reduce_file(path).encode("utf-8")))

    def test_plain_input_counts_itself(self):
        disk, plain = R.source_sizes(DEMO)
        self.assertEqual(disk, plain)

    def test_compressed_input_counts_the_xml(self):
        z = compressed_copy(DEMO, os.path.join(
            tempfile.mkdtemp(prefix="rdratio"), "z.drawio"))
        disk, plain = R.source_sizes(z)
        self.assertLess(disk, plain / 2)
        self.assertGreater(plain, os.path.getsize(DEMO) * 0.9,
                           "展开出来的 XML 该和未压缩存的那份差不多大")

    def test_same_drawing_same_ratio(self):
        z = compressed_copy(DEMO, os.path.join(
            tempfile.mkdtemp(prefix="rdratio"), "z.drawio"))
        a, b = self._ratio(DEMO), self._ratio(z)
        self.assertAlmostEqual(a, b, delta=0.15 * a,
                               msg="两种存法报出来的比例差太多: %.2f vs %.2f" % (a, b))
        self.assertGreater(a, 3.0, "README 说 1/5~1/8，报出来的数得对得上")


class TestOwnOutputRoundTrips(unittest.TestCase):
    """reduce 打的统计行，expand 必须认。

    它走 stderr，但 `2>&1` 和「整屏复制粘进 .rd」是这条路上的日常 ——
    自己的输出自己读不回去，是 bug 不是用法问题。
    """

    def _warnings(self, rd):
        err = io.StringIO()
        old, sys.stderr = sys.stderr, err
        try:
            X.expand(rd)
        finally:
            sys.stderr = old
        return err.getvalue()

    def test_stats_line_is_a_comment(self):
        out = subprocess.run(
            [sys.executable, os.path.join(TOOLS, "drawio_reduce.py"), DEMO,
             "-o", os.devnull], capture_output=True)
        tail = [l for l in out.stderr.decode("utf-8").splitlines() if l.strip()][-1]
        self.assertTrue(tail.startswith("#"), "统计行得是 .rd 的注释行: " + tail)
        self.assertEqual("", self._warnings(reduce_file(DEMO) + tail + "\n"))

    def test_legacy_stats_line_without_hash(self):
        """老版本打的是光秃秃的 `---- ...`，已经流出去的 .rd 里就有。"""
        self.assertEqual("", self._warnings(
            reduce_file(DEMO) + "\n---- 11830 -> 2273 bytes  (5.2x)\n"))

    def test_real_garbage_still_warns(self):
        self.assertIn("Z 99", self._warnings(reduce_file(DEMO) + "Z 99 谁也不认得\n"))


class TestStdoutIsUtf8(unittest.TestCase):
    """`reduce x.drawio > x.rd` 得和 `-o x.rd` 出同一种字节。

    以前 stdout 走 locale 编码：中文 Windows 上写出来的是 GBK 的 .rd，
    expand 一读就 UnicodeDecodeError —— 管道那条路整条是断的。
    """

    def _run(self, *argv):
        env = dict(os.environ, PYTHONIOENCODING="gbk")   # 模拟中文 Windows 的 locale
        return subprocess.run([sys.executable] + list(argv),
                              capture_output=True, env=env)

    def test_reduce_stdout_decodes_as_utf8(self):
        out = self._run(os.path.join(TOOLS, "drawio_reduce.py"), DEMO)
        self.assertIn("图元", out.stdout.decode("utf-8"))

    def test_piped_rd_expands(self):
        rd = os.path.join(tempfile.mkdtemp(prefix="rdpipe"), "p.rd")
        with open(rd, "wb") as fh:
            fh.write(self._run(os.path.join(TOOLS, "drawio_reduce.py"),
                               DEMO).stdout)
        out = self._run(os.path.join(TOOLS, "drawio_expand.py"), rd)
        self.assertEqual(0, out.returncode, out.stderr.decode("utf-8", "replace"))
        self.assertIn('<diagram name="demo"', out.stdout.decode("utf-8"))


# ------------------------------------------------------- --hints


def edge(eid, src, dst, ex, ey, nx, ny):
    return ('<mxCell id="%s" style="exitX=%s;exitY=%s;exitDx=0;exitDy=0;'
            'exitPerimeter=0;entryX=%s;entryY=%s;entryDx=0;entryDy=0;'
            'entryPerimeter=0;" edge="1" parent="1" source="%s" target="%s">'
            '<mxGeometry relative="1" as="geometry"/></mxCell>'
            % (eid, ex, ey, nx, ny, src, dst))


def hints_rd(path, kinds=None, tol=0.0):
    out = []
    for name, model in R.load_pages(path):
        out.append(R.reduce_page(name, model, None,
                                 list(kinds or R.HINT_KINDS), tol))
    return "\n".join(out)


def hints_xml(xml, kinds=None, tol=0.0):
    fd, p = tempfile.mkstemp(suffix=".drawio")
    os.close(fd)
    try:
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(xml)
        return hints_rd(p, kinds, tol)
    finally:
        os.unlink(p)


class TestHints(unittest.TestCase):
    """`--hints` 只量几何、不下判断，而且不许弄脏 .rd 的语法。"""

    def test_off_by_default(self):
        self.assertNotIn("## hints", reduce_file(DEMO))

    def test_every_hint_line_is_a_comment(self):
        rd = hints_rd(DEMO)
        for line in rd[rd.index("## hints"):].splitlines():
            self.assertTrue(not line or line.startswith("#"),
                            "hints 里混进了非注释行: " + line)

    def test_expand_ignores_hints(self):
        """加不加 hints，expand 出来的 XML 一个字节都不该变。"""
        self.assertIn("## hints", hints_rd(DEMO))
        self.assertEqual(X.expand(reduce_file(DEMO)),
                         X.expand("# demo.drawio\n\n" + hints_rd(DEMO) + "\n"))

    def test_fixed_point_survives(self):
        self.assertEqual(
            body(reduce_file(DEMO)),
            body(roundtrip("# demo.drawio\n\n" + hints_rd(DEMO) + "\n")))

    def test_hints_before_the_filename(self):
        """`--hints my.drawio` 是最自然的写法，不能让它把文件名吞掉。

        argparse 的 nargs="?" 只看下一个 token 是不是以 `-` 开头，
        原样用会报「缺 infile」—— 错在 --hints 上，错误信息却指向 infile。
        """
        n = R.normalize_hints_argv
        self.assertEqual(["--hints=" + ",".join(R.HINT_KINDS), "a.drawio"],
                         n(["--hints", "a.drawio"]))
        self.assertEqual(["--hints=same,near", "a.drawio"],
                         n(["--hints", "same,near", "a.drawio"]))
        self.assertEqual(["a.drawio", "--hints=" + ",".join(R.HINT_KINDS)],
                         n(["a.drawio", "--hints"]))
        self.assertEqual(["a.drawio"], n(["a.drawio"]))

    def test_cli_accepts_hints_before_the_filename(self):
        out = subprocess.run(
            [sys.executable, os.path.join(TOOLS, "drawio_reduce.py"),
             "--hints", DEMO], capture_output=True)
        self.assertEqual(0, out.returncode, out.stderr.decode("utf-8", "replace"))
        self.assertIn("#same", out.stdout.decode("utf-8"))

    def test_same_finds_coincident_endpoints(self):
        h = hints_xml(wrap(
            vertex("a", "rounded=0;", 40, 40, 80, 40)
            + vertex("b", "rounded=0;", 240, 40, 80, 40)
            + edge("e1", "a", "b", 1, 0.5, 0, 0.5)
            + edge("e2", "a", "b", 1, 0.5, 0, 0.5)))
        self.assertIn("#same 120,60", h)        # 两条边的起点落在同一个点上
        self.assertIn("e1.a", h)
        self.assertIn("e2.a", h)

    def test_same_tolerance_is_the_callers_call(self):
        """差 8 像素算不算同一个点是电路知识，脚本不替谁定：给了容差才合。"""
        xml = wrap(
            vertex("a", "rounded=0;", 40, 40, 80, 40)
            + vertex("b", "rounded=0;", 240, 140, 80, 40)
            + vertex("c", "rounded=0;", 40, 48, 80, 40)
            + edge("e1", "a", "b", 1, 0.5, 0, 0.5)
            + edge("e2", "c", "b", 1, 0.5, 1, 0.5))
        self.assertNotIn("#same", hints_xml(xml))
        self.assertIn("#same", hints_xml(xml, tol=10))

    def test_in_reports_geometric_containment(self):
        h = hints_xml(wrap(
            vertex("frame", "rounded=0;fillColor=#f8cecc;", 20, 20, 400, 300)
            + vertex("inner", "rounded=0;", 60, 60, 80, 40)
            + vertex("outside", "rounded=0;", 600, 60, 80, 40)))
        self.assertIn("#in   frame  inner", h)
        self.assertNotIn("outside", h[h.index("#in"):].splitlines()[0])

    def test_in_picks_the_smallest_container(self):
        h = hints_xml(wrap(
            vertex("big", "rounded=0;", 0, 0, 800, 600)
            + vertex("mid", "rounded=0;", 20, 20, 400, 300)
            + vertex("leaf", "rounded=0;", 40, 40, 40, 40)))
        self.assertIn("#in   mid  leaf", h)     # 挂最小的那个框，不是最大的
        self.assertIn("#in   big  mid", h)

    def test_near_skips_containers(self):
        """分区色块圈住半张图，跟框里每个文本框的边距都是 0。

        包含不是挨着 —— 不排掉的话，真正紧挨着的那个器件会被 `@0` 挤出榜单。
        包含关系 `#in` 已经单列了，这里排掉一位信息都不丢。
        """
        h = hints_xml(wrap(
            vertex("frame", "rounded=0;fillColor=#f8cecc;", 20, 20, 500, 400)
            + vertex("cap", "rounded=0;", 200, 200, 60, 20)
            + vertex("lab", "text;", 270, 200, 60, 20, "C=36pF")))
        line = [l for l in h.splitlines() if l.startswith("#near lab")][0]
        self.assertNotIn("frame", line)
        self.assertIn("cap@10", line)
        self.assertIn("#in   frame", h)      # 但包含关系还是要报

    def test_near_gives_distances_not_ownership(self):
        h = hints_xml(wrap(
            vertex("cap", "shape=mxgraph.electrical.capacitors.capacitor_1;",
                   200, 200, 60, 20)
            + vertex("far", "rounded=0;", 600, 500, 40, 40)
            + vertex("lab", "text;", 270, 200, 60, 20, "C=36pF")))
        line = [l for l in h.splitlines() if l.startswith("#near lab")][0]
        self.assertIn("cap@10", line)          # 边距 10 px：是个数，不是结论
        self.assertLess(line.index("cap@"), line.index("far@"))


# --------------------------------------------------------------- 真渲染

DRAWIO_EXE = os.environ.get("DRAWIO_EXE") or next(
    (p for p in (r"C:\Program Files\draw.io\draw.io.exe",
                 r"C:\Program Files (x86)\draw.io\draw.io.exe",
                 "/usr/bin/drawio", "/opt/drawio/drawio",
                 "/Applications/draw.io.app/Contents/MacOS/draw.io")
     if os.path.exists(p)), None)


@unittest.skipUnless(DRAWIO_EXE, "本机没装 draw.io，跳过真渲染（设 DRAWIO_EXE 指定）")
class TestRenderedPixels(unittest.TestCase):
    """定点测试管不住「两边一致地认错同一个默认值」，只有真渲染能。

    比的是**像素**不是 XML：原图和还原图各出一张 PNG，逐点比。
    允许的差异只有 reduce 明确声明单向的那些（下标/上标、斜体），
    它们只改字形不改位置，所以按「有差异的连通区域个数」卡，不按总像素数。
    """

    def _render(self, path, out):
        subprocess.run([DRAWIO_EXE, "--no-sandbox", "-x", "-f", "png",
                        "--scale", "2", "-o", out, path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=180)
        self.assertTrue(os.path.exists(out), "draw.io 没出图: " + path)

    def _compare(self, src_xml, allow_regions=0):
        import plot_digitize as PD
        d = tempfile.mkdtemp(prefix="rdrender")
        a_dio = os.path.join(d, "a.drawio")
        with open(a_dio, "w", encoding="utf-8") as fh:
            fh.write(src_xml)
        rd = reduce_file(a_dio)
        b_dio = os.path.join(d, "b.drawio")
        with open(b_dio, "w", encoding="utf-8") as fh:
            fh.write(X.expand(rd))
        pa, pb = os.path.join(d, "a.png"), os.path.join(d, "b.png")
        self._render(a_dio, pa)
        self._render(b_dio, pb)
        ia, _ = PD.load_png(pa)
        ib, _ = PD.load_png(pb)
        self.assertEqual((ia.w, ia.h), (ib.w, ib.h),
                         "两张图尺寸都不一样，先看 %s" % d)
        bad = set()
        for y in range(ia.h):
            for x in range(ia.w):
                if max(abs(p - q) for p, q in zip(ia.at(x, y), ib.at(x, y))) > 16:
                    bad.add((x // 80, y // 80))       # 粗粒度成簇，别数散点
        regions = len(_clusters(bad))
        self.assertLessEqual(regions, allow_regions,
                             "渲染出来有 %d 处不一样（只允许 %d 处）: %s"
                             % (regions, allow_regions, d))

    def test_demo_renders_the_same(self):
        """整张 demo 逐像素一致 —— 连线走法和标签位置全压在这一条里。

        比之前先把 `<sub>` / `<sup>` 从**原图**里去掉：reduce 明确声明下标是单向的
        （`V<sub>ss</sub>` -> `Vss`，见 rd-spec §7），留着它就只能卡个模糊的阈值，
        去掉之后这条断言是「零差异」，不依赖字体和 DPI。
        下标本身是否被去掉由 test_subscript_is_flattened 单独盯着。
        """
        with open(DEMO, encoding="utf-8") as fh:
            self._compare(re.sub(r"&lt;/?su[bp]&gt;", "", fh.read()),
                          allow_regions=0)

    def test_routing_and_alignment_pixel_exact(self):
        """不带下标/斜体的图必须零差异。"""
        self._compare(wrap(
            vertex("a", "rounded=0;whiteSpace=wrap;html=1;", 40, 40, 120, 60,
                   "left") .replace('style="', 'style="align=left;')
            + vertex("b", "rounded=0;whiteSpace=wrap;html=1;", 360, 260,
                     120, 60, "right").replace('style="', 'style="align=right;'
                                               'verticalAlign=bottom;')
            + vertex("c", "shape=mxgraph.electrical.signal_sources.vss2;html=1;"
                     "verticalLabelPosition=bottom;verticalAlign=top;"
                     "flipV=1;", 200, 400, 14, 21, "gnd")
            + '<mxCell id="e1" style="edgeStyle=orthogonalEdgeStyle;rounded=0;'
              'jettySize=auto;html=1;exitX=1;exitY=0.5;exitDx=0;exitDy=0;'
              'exitPerimeter=0;entryX=0;entryY=0.5;entryDx=0;entryDy=0;'
              'entryPerimeter=0;" edge="1" parent="1" source="a" target="b">'
              '<mxGeometry relative="1" as="geometry"/></mxCell>'
            + '<mxCell id="e2" style="edgeStyle=orthogonalEdgeStyle;rounded=1;'
              'html=1;" edge="1" parent="1" source="a" target="c">'
              '<mxGeometry relative="1" as="geometry"/></mxCell>'),
            allow_regions=0)


def _clusters(cells):
    """把差异块并成连通簇 —— 一个标签跨好几块，不该数成好几处。"""
    todo, out = set(cells), []
    while todo:
        seed = todo.pop()
        group, stack = {seed}, [seed]
        while stack:
            x, y = stack.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (x + dx, y + dy)
                    if n in todo:
                        todo.discard(n)
                        group.add(n)
                        stack.append(n)
        out.append(group)
    return out


if __name__ == "__main__":
    unittest.main()
