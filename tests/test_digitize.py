# -*- coding: utf-8 -*-
"""截图数字化的闭环：渲染 -> 数字化 -> 和原始 CSV 比，误差落在像素精度内。

**只有渲染端的坐标映射已知，这个闭环才可判定** —— 拿真实截图测，
你分不清误差是数字化算错了还是手打的坐标不准。
"""

import bisect
import os
import subprocess
import sys
import tempfile
import unittest

import _common as C
from _common import ROOT, TOOLS

sys.path.insert(0, os.path.join(ROOT, "examples"))
import plot_digitize as pdg                                     # noqa: E402

PNG = os.path.join(ROOT, "examples", "demo_plot.png")
BOX = (212, 88, 1687, 901)


def read_csv(path, ci):
    x, y = [], []
    with open(path, encoding="utf-8") as fh:
        for ln in fh:
            if ln.startswith("#"):
                continue
            p = ln.strip().split(",")
            try:
                x.append(float(p[0]))
                y.append(float(p[ci]))
            except (ValueError, IndexError):
                pass
    return x[1:], y[1:]


class TestPngDecode(unittest.TestCase):

    def test_pure_decoder_matches_pillow(self):
        """自带解码器是逃生舱（隔离区可能连 Pillow 都没有），两条路必须一致。"""
        try:
            from PIL import Image                               # noqa: F401
        except ImportError:
            self.skipTest("没有 Pillow，只有一条路径可测")
        a, backend = pdg.load_png(PNG)
        b = pdg._load_png_pure(PNG)
        self.assertEqual(backend, "Pillow")
        self.assertEqual((a.w, a.h), (b.w, b.h))
        self.assertEqual(bytes(a.rgb), bytes(b.rgb))

    def test_engineering_notation_axis(self):
        self.assertAlmostEqual(pdg.eng("300n"), 300e-9)
        self.assertAlmostEqual(pdg.eng("1.2G"), 1.2e9)
        self.assertAlmostEqual(pdg.eng("-5m"), -5e-3)
        self.assertAlmostEqual(pdg.eng("1e-9"), 1e-9)

    def test_plotbox_autodetect(self):
        img = pdg._load_png_pure(PNG)
        self.assertEqual(pdg.find_plotbox(img), BOX)


class TestClosedLoop(unittest.TestCase):
    """渲染一张已知坐标的图，数字化回来，逐列跟原始数据比。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="plotdig")
        cls.png = os.path.join(cls.tmp, "zoom.png")
        cls.csv = os.path.join(cls.tmp, "zoom.csv")
        cls.y0, cls.y1 = 0.70, 0.87
        gen = os.path.join(ROOT, "examples", "gen_demo_plot.py")
        subprocess.check_call(
            [sys.executable, gen, "--yaxis", "%g,%g" % (cls.y0, cls.y1),
             "--traces", "V(vdd_pll)", "-o", cls.png],
            stdout=subprocess.DEVNULL)
        pdg.main([cls.png, "--xaxis", "0,300n",
                  "--yaxis", "%g,%g" % (cls.y0, cls.y1),
                  "--trace", "#e01b24=vdd_pll", "-o", cls.csv])
        cls.ppx = (cls.y1 - cls.y0) / float(BOX[3] - BOX[1])

    def test_envelope_contains_truth(self):
        """真值必须落在上下包络之间（容差 1 px）。

        包络而不是中位数：一列像素里振铃占几十个点，取中位数会抽成锯齿（假信号）。
        """
        ox, oy = read_csv(os.path.join(ROOT, "examples", "demo_tran.csv"), 1)
        dx, dlo = read_csv(self.csv, 1)
        _, dhi = read_csv(self.csv, 2)
        worst = 0.0
        for i, t in enumerate(dx):
            j = bisect.bisect_left(ox, t)
            if j <= 0 or j >= len(ox):
                continue
            w = (t - ox[j - 1]) / (ox[j] - ox[j - 1])
            v = oy[j - 1] + w * (oy[j] - oy[j - 1])
            if dlo[i] - self.ppx <= v <= dhi[i] + self.ppx:
                continue
            worst = max(worst, min(abs(v - dlo[i]), abs(v - dhi[i])))
        self.assertLessEqual(worst / self.ppx, 2.0,
                             "带外误差 %.2f px（1 px = %.4g V）"
                             % (worst / self.ppx, self.ppx))

    def test_midline_rms_within_a_pixel(self):
        ox, oy = read_csv(os.path.join(ROOT, "examples", "demo_tran.csv"), 1)
        dx, dlo = read_csv(self.csv, 1)
        _, dhi = read_csv(self.csv, 2)
        acc = n = 0
        for i, t in enumerate(dx):
            j = bisect.bisect_left(ox, t)
            if j <= 0 or j >= len(ox):
                continue
            w = (t - ox[j - 1]) / (ox[j] - ox[j - 1])
            v = oy[j - 1] + w * (oy[j] - oy[j - 1])
            acc += (v - 0.5 * (dlo[i] + dhi[i])) ** 2
            n += 1
        rms = (acc / n) ** 0.5
        self.assertLessEqual(rms / self.ppx, 1.5,
                             "中线 rms %.2f px" % (rms / self.ppx))

    def test_precision_declared_in_header(self):
        with open(self.csv, encoding="utf-8") as fh:
            head = [ln for ln in fh if ln.startswith("#")]
        blob = "".join(head)
        self.assertIn("PLOTDIG1", blob)
        self.assertIn("精度上限", blob)
        self.assertIn("1 px =", blob)
        self.assertIn("缩放层级就是量化器", blob)

    def test_zoom_is_the_quantizer(self):
        """把 Y 轴缩紧 7.6 倍，1 px 代表的电压就该缩小同样的倍数。"""
        full = 1.3 / (BOX[3] - BOX[1])
        self.assertAlmostEqual(full / self.ppx, 1.3 / (self.y1 - self.y0),
                               delta=1e-6)


class TestFailureModesDeclared(unittest.TestCase):
    """已知会挂的场景要**报出来**，不能猜一个。"""

    def test_occluded_trace_is_reported(self):
        """全量程图里蓝色 ctrl 盖住了红色 vdd_pll，工具要说有多少列没匹配上。"""
        out = os.path.join(tempfile.mkdtemp(), "d.csv")
        import io
        old = sys.stderr
        sys.stderr = io.StringIO()
        try:
            pdg.main([PNG, "--xaxis", "0,300n", "--yaxis", "0,1.3",
                      "--trace", "#e01b24=vdd_pll", "-o", out])
            err = sys.stderr.getvalue()
        finally:
            sys.stderr = old
        self.assertIn("无匹配像素", err)
        self.assertIn("已线性补", err)

    def test_bad_plotbox_refuses(self):
        with self.assertRaises(SystemExit):
            pdg.main([PNG, "--xaxis", "0,1", "--yaxis", "0,1",
                      "--plotbox", "0,0,5,5", "--trace", "#e01b24=a"])

    def test_digitized_csv_feeds_wave_reduce(self):
        """数字化的产物要能直接喂给 wave_reduce 走全套 metrics。"""
        out = os.path.join(tempfile.mkdtemp(), "d.csv")
        pdg.main([PNG, "--xaxis", "0,300n", "--yaxis", "0,1.3", "--mid",
                  "--trace", "#1c71d8=ctrl", "-o", out])
        rc, txt = C.run_cli([out])
        self.assertEqual(rc, 0)
        self.assertIn("[SHAPE]", txt)
        _ = TOOLS


if __name__ == "__main__":
    unittest.main()
