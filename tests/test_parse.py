# -*- coding: utf-8 -*-
"""解析层。每一条都对应 wave-spec 第 6 节里点名「会咬人」的东西。"""

import unittest

import _common as C
from _common import core


class TestLayoutA(unittest.TestCase):

    def test_shared_x_grid(self):
        tr = C.load("demo_tran.csv")
        self.assertEqual(tr.xname, "time")
        self.assertEqual([s.name for s in tr.signals],
                         ["V(vdd_pll)", "V(ctrl)", "I(mp0)", "V(vref)"])
        self.assertEqual(len(tr.x), C.truth()["demo_tran.csv"]["n"])
        for i in range(1, len(tr.x)):
            self.assertGreater(tr.x[i], tr.x[i - 1], "x 必须严格递增")


class TestLayoutB(unittest.TestCase):
    """布局 B：每条 trace 自带 x 列，长度还不一样。

    spec 原话「这条会咬人」——把第二个 time 列当成信号，整个分析就废了。
    """

    def test_split_into_traces(self):
        trs = core.parse_csv(C.ex("demo_tran_layoutb.csv"))
        self.assertEqual(len(trs), 2, "两个 time 列应当拆成两条独立 trace")
        want = C.truth()["demo_tran_layoutb.csv"]["n"]
        self.assertEqual([len(t.x) for t in trs], want, "两条长度不等，短的先空")
        self.assertEqual([s.name for t in trs for s in t.signals],
                         ["V(vdd_pll)", "I(mp0)"])
        for t in trs:
            self.assertEqual(len(t.signals), 1)
            self.assertTrue(any("布局 B" in n for n in t.notes),
                            "拆了要说出来")

    def test_forced_layout_a_keeps_one_trace(self):
        trs = core.parse_csv(C.ex("demo_tran_layoutb.csv"), layout="a")
        self.assertEqual(len(trs), 1, "--layout a 应当强制按共享栅格读")


class TestDirty(unittest.TestCase):
    """脏数据：每一样都要被**正确处理且报进 notes**，不许静默。"""

    def setUp(self):
        self.tr = C.load("demo_dirty.csv")
        self.notes = " ".join(self.tr.notes)

    def test_row_count(self):
        self.assertEqual(len(self.tr.x),
                         C.truth()["demo_dirty.csv"]["kept_rows"])

    def test_duplicate_x_dropped_and_reported(self):
        self.assertIn("重复 x", self.notes)

    def test_nonmonotonic_dropped_not_sorted(self):
        self.assertIn("非单调", self.notes)
        for i in range(1, len(self.tr.x)):
            self.assertGreater(self.tr.x[i], self.tr.x[i - 1])

    def test_nan_inf_blank_interpolated_and_reported(self):
        self.assertIn("NaN", self.notes)
        self.assertIn("inf", self.notes)
        self.assertIn("线性补", self.notes)
        for s in self.tr.signals:
            for v in s.y:
                self.assertEqual(v, v, "补完不许还有 NaN")
                self.assertNotEqual(abs(v), float("inf"))

    def test_dt_collapse_reported(self):
        # dt 突然坍缩本身就是 debug 信号（Spectre 收敛挣扎），要报不要跳过
        self.assertIn("dt 坍缩", self.notes)

    def test_quotes_unit_row_trailing_comma(self):
        self.assertEqual([s.name for s in self.tr.signals],
                         C.truth()["demo_dirty.csv"]["signals"])
        u = C.truth()["demo_dirty.csv"]["units"]
        self.assertEqual(self.tr.xunit, u["time"])
        self.assertEqual(self.tr.xunit_src, "declared")
        for s in self.tr.signals:
            self.assertEqual(s.unit, u[s.name])
            self.assertEqual(s.unit_src, "declared", "单位行给的就是 declared")


class TestUnits(unittest.TestCase):
    """不猜单位。推出来的要带标记 —— 猜错了整列的量纲就错了。"""

    def test_network_name_is_not_a_unit(self):
        # V(vco_out) 里的 vco_out 是网络名。当成单位的话这一列就废了
        tr = C.load("demo_spec.csv")
        self.assertEqual(tr.signals[0].name, "V(vco_out)")

    def test_declared_unit_suffix(self):
        tr = C.load("demo_tran.csv")
        self.assertEqual(tr.signals[0].unit, "V")
        self.assertEqual(tr.signals[0].unit_src, "declared")
        self.assertEqual(tr.signals[2].unit, "A")

    def test_inferred_marked(self):
        trs = core.parse_csv(C.ex("demo_tran_layoutb.csv"))
        s = trs[0].signals[0]
        self.assertEqual(s.unit, "V")
        self.assertEqual(s.unit_src, "inferred", "从列名推的必须标出来")
        spec = core.make_colspec(trs[0], 0.005)
        self.assertTrue(spec[0].unit_out.endswith("?"), "推出来的单位带 ?")

    def test_db_function_is_declared(self):
        tr = C.load("demo_ac.csv")
        self.assertEqual(tr.signals[0].unit, "dB")
        self.assertEqual(tr.signals[0].unit_src, "declared")

    def test_unknown_stays_unknown(self):
        txt = "foo,bar\n0,1\n1,2\n2,4\n3,9\n"
        tr = core.parse_csv("<t>", text=txt)[0]
        core.analyze(tr)
        self.assertEqual(tr.signals[0].unit_src, "unknown")
        self.assertIn("unknown", core.make_colspec(tr, 0.005)[0].unit_out)


class TestAxis(unittest.TestCase):

    def test_log_detected(self):
        self.assertEqual(C.load("demo_ac.csv").xscale, "log")

    def test_lin_detected(self):
        self.assertEqual(C.load("demo_spec.csv").xscale, "lin")
        self.assertEqual(C.load("demo_tran.csv").xscale, "lin")

    def test_no_dt_collapse_flag_on_log_axis(self):
        # log 轴上 dt 本来就跨几个数量级，坍缩判据不成立，报了就是噪音
        tr = C.load("demo_ac.csv")
        self.assertFalse(any("dt 坍缩" in n for n in tr.notes))

    def test_dt_collapse_flag_on_lin_axis(self):
        tr = C.load("demo_tran.csv")
        at = C.truth()["demo_tran.csv"]["dt_collapse_at"]
        hits = [n for n in tr.notes if "dt 坍缩" in n]
        self.assertTrue(hits, "250 ns 处刻意埋了一次 dt 坍缩")
        self.assertIn("250", " ".join(hits))
        _ = at

    def test_descending_x_is_flipped(self):
        txt = "freq,V(o)\n1e9,1\n1e8,2\n1e7,3\n1e6,4\n1e5,5\n1e4,6\n"
        tr = core.parse_csv("<t>", text=txt)[0]
        core.analyze(tr)
        self.assertLess(tr.x[0], tr.x[-1])
        self.assertTrue(any("递减" in n for n in tr.notes), "翻转了要说")


if __name__ == "__main__":
    unittest.main()
