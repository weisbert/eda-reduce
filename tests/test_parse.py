# -*- coding: utf-8 -*-
"""解析层。每一条都对应 wave-spec 第 6 节里点名「会咬人」的东西。"""

import math
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


class TestViVAXY(unittest.TestCase):
    """ViVA「Export CSV」的真实形状：每条 trace 两列 `<表达式> X` / `<表达式> Y`。

    真实文件里咬了一口的就是这个：列名 `v /gmp; tran (V) X` 自带分号，
    分隔符按「表头里出现次数」判会判成分号，数据区一行都切不出数，
    最后在 GUI 里表现成一句 `list index out of range`。
    """

    RAW = ("v /gmp; tran (V) X,v /gmp; tran (V) Y\n"
           "0,0.00076333\n3e-13,-0.00086388\n6e-13,-0.0017263\n"
           "9e-13,-0.0022337\n1.1677e-12,-0.0025453\n1.6449e-12,-0.0027668\n"
           "2.4999e-12,-0.0025178\n3.3549e-12,-0.0018986\n")

    def test_semicolon_in_name_does_not_win_delimiter(self):
        head = "v /gmp; tran (V) X,v /gmp; tran (V) Y"
        body = ["0,0.00076333", "3e-13,-0.00086388"]
        self.assertEqual(core._sniff_delim(head, body), ",")

    def test_real_export_parses(self):
        trs = core.parse_csv("<t>", text=self.RAW)
        self.assertEqual(len(trs), 1)
        tr = trs[0]
        core.analyze(tr)
        self.assertEqual(len(tr.x), 8)
        self.assertEqual([s.name for s in tr.signals], ["v /gmp"])
        self.assertEqual(tr.signals[0].unit, "V")
        self.assertEqual(tr.signals[0].unit_src, "declared")

    def test_x_unit_comes_from_analysis_not_from_the_y_unit(self):
        # `(V)` 是 Y 的单位，横轴是秒。照搬过去整条 x 轴的量纲就错了
        tr = core.parse_csv("<t>", text=self.RAW)[0]
        core.analyze(tr)
        self.assertEqual(tr.xunit, "s")
        self.assertEqual(tr.xunit_src, "inferred")
        self.assertEqual(tr.kind, "tran")
        self.assertTrue(any("ViVA X/Y" in n for n in tr.notes), "认了要说出来")

    def test_pairs_split_into_traces(self):
        trs = core.parse_csv(C.ex("demo_tran_viva.csv"))
        want = C.truth()["demo_tran_viva.csv"]
        self.assertEqual(len(trs), want["traces"])
        self.assertEqual([len(t.x) for t in trs], want["n"])
        self.assertEqual([s.name for t in trs for s in t.signals],
                         want["signals"])
        for t in trs:
            core.analyze(t)
            self.assertEqual(len(t.signals), 1)
            self.assertEqual(t.xunit, want["x_unit"])
            self.assertEqual(t.kind, want["kind"])
            self.assertEqual(t.signals[0].unit, want["units"][t.signals[0].name])

    def test_freq_analysis_names_map_to_hz(self):
        txt = ("dB20(v /out); ac (dB) X,dB20(v /out); ac (dB) Y\n"
               + "".join("%g,%g\n" % (10.0 * 10 ** (i / 8.0), 40 - i)
                         for i in range(24)))
        tr = core.parse_csv("<t>", text=txt)[0]
        core.analyze(tr)
        self.assertEqual((tr.xunit, tr.kind, tr.xscale), ("Hz", "freq", "log"))
        self.assertEqual(tr.signals[0].unit, "dB")

    def test_unknown_analysis_is_not_guessed(self):
        # pss 在 ViVA 里既可能出时域也可能出频域 —— 不猜，留 unknown
        txt = ("v /o; pss (V) X,v /o; pss (V) Y\n"
               + "".join("%g,%g\n" % (i * 1e-9, i) for i in range(12)))
        tr = core.parse_csv("<t>", text=txt)[0]
        core.analyze(tr)
        self.assertEqual(tr.xunit_src, "unknown")
        self.assertTrue(any("认不出分析类型" in n for n in tr.notes))

    def test_viva_writes_the_quantity_symbol_not_the_unit(self):
        """ViVA 括号里是**量的符号**：电压 `(V)`、电流 `(I)`。

        `(V)` 碰巧对（伏特也是 V），`(I)` 就不对了（电流单位是 A）。
        真实电流波形上踩到过，一个字母引发连锁：单位不认识 -> `; tran` 不在末尾
        -> 横轴认不出 -> kind=unknown -> **一条 METRICS 都没有**。
        """
        txt = ("i /VCO_TOP/VDD; tran (I) X,i /VCO_TOP/VDD; tran (I) Y\n"
               + "".join("%g,%g\n" % (i * 1e-11, 1e-3 * (i % 7 - 3))
                         for i in range(60)))
        tr = core.parse_csv("<t>", text=txt)[0]
        core.analyze(tr)
        self.assertEqual(tr.kind, "tran")
        self.assertEqual((tr.xname, tr.xunit), ("time", "s"))
        self.assertEqual(tr.signals[0].name, "i /VCO_TOP/VDD")
        self.assertEqual(tr.signals[0].unit, "A")
        self.assertEqual(tr.signals[0].unit_src, "inferred",
                         "文件写的是 I，A 是我们换算的 —— 不算 declared")

    def test_unknown_quantity_symbol_still_gets_kind_and_axis(self):
        """没见过的量符号只该丢**单位**，不该连横轴和 kind 一起丢。

        少认一个单位是小事，丢掉整份 METRICS 是大事 —— 所以分析名的识别
        不能被单位白名单挡住。
        """
        txt = ("p /x; tran (Wat) X,p /x; tran (Wat) Y\n"
               + "".join("%g,%g\n" % (i * 1e-11, i) for i in range(60)))
        tr = core.parse_csv("<t>", text=txt)[0]
        core.analyze(tr)
        self.assertEqual(tr.kind, "tran", "单位没认出来就把 kind 也丢了")
        self.assertEqual(tr.xunit, "s")
        self.assertEqual(tr.signals[0].unit_src, "unknown", "认不出就老实说")

    def test_declared_overrides_win(self):
        # 人给了 --xcols / --layout a 就听人的，不许自作主张按 X/Y 拆
        trs = core.parse_csv(C.ex("demo_tran_viva.csv"), layout="a")
        self.assertEqual(len(trs), 1)
        self.assertEqual(len(trs[0].signals), 3)
        trs = core.parse_csv(C.ex("demo_tran_viva.csv"), xcols=[0])
        self.assertEqual(len(trs), 1)

    def test_semicolon_delimited_file_still_works(self):
        # 真的用分号分隔的文件不能被上面那条修坏
        txt = "time;V(out)\n0;0.8\n1e-9;0.81\n2e-9;0.82\n3e-9;0.83\n"
        tr = core.parse_csv("<t>", text=txt)[0]
        core.analyze(tr)
        self.assertEqual([s.name for s in tr.signals], ["V(out)"])
        self.assertEqual(len(tr.x), 4)


class TestDropWarning(unittest.TestCase):
    """大面积丢行必须**大声说**，不能只写进 note。

    真实教训（2026-07-28）：一份 423153 行的 ViVA 导出里 72% 是重复时间戳
    （x 列只有 5 位有效数字），工具按规则「重复保留第一个」，等于把 5 GHz 的
    振荡按 10 GS/s 抽了样。混叠出来的拍频包络看着就像真实的起振包络，
    而这件事当时只在一条 note 里提了一句。
    """

    def _five_digit_export(self, n=4000, dt=4.7e-12):
        rows = ["v /x; tran (V) X,v /x; tran (V) Y"]
        for i in range(n):
            t = i * dt
            rows.append("%.5g,%.6g"                       # ViVA 默认 5 位有效数字
                        % (t + 1.5e-6, 0.3 + 0.25 * math.sin(2e10 * t)))
        return "\n".join(rows) + "\n"

    def test_duplicate_flood_warns(self):
        tr = core.parse_csv("<t>", text=self._five_digit_export())[0]
        core.analyze(tr)
        self.assertTrue(tr.warns, "72% 的行被丢掉，一条 WARN 都没有")
        all_w = " ".join(tr.warns)
        self.assertIn("混叠", all_w, "得说清后果，不能只报个数")
        self.assertIn("有效数字", all_w, "得说清成因")
        self.assertIn("12 位", all_w, "得说清怎么修")

    def test_clean_export_has_no_warning(self):
        """验收标准就是这条：精度够了 WARN 就该消失。"""
        rows = ["v /x; tran (V) X,v /x; tran (V) Y"]
        for i in range(4000):
            t = 1.5e-6 + i * 4.7e-12
            rows.append("%.12g,%.6g" % (t, 0.3 + 0.25 * math.sin(2e10 * i)))
        tr = core.parse_csv("<t>", text="\n".join(rows) + "\n")[0]
        core.analyze(tr)
        self.assertEqual(tr.warns, [], "干净的文件不许报警")
        self.assertEqual(len(tr.x), 4000)

    def test_a_few_duplicates_stay_a_note(self):
        """零星几个重复点是求解器的正常行为，报警会变成狼来了。"""
        rows = ["time,V(o)"]
        for i in range(500):
            rows.append("%.12g,%.6g" % (i * 1e-9, 0.5 + 0.001 * i))
        rows.insert(200, rows[200])                      # 就重复一个
        tr = core.parse_csv("<t>", text="\n".join(rows) + "\n")[0]
        core.analyze(tr)
        self.assertEqual(tr.warns, [], "0.2% 的重复不该报警")
        self.assertTrue(any("重复 x" in n for n in tr.notes), "但要留个 note")

    def test_sig_digits_counts_what_was_written(self):
        self.assertEqual(core._sig_digits("1.6377e-06"), 5)
        self.assertEqual(core._sig_digits("0.00076333"), 5)
        # 尾零不算：`%g` 本来就把它们去掉了，算进去会把 1.000000 判成 7 位。
        # 导出精度取一批行的最大值，个别行短一点不影响
        self.assertEqual(core._sig_digits("1.500000000000e-06"), 2)
        self.assertEqual(core._sig_digits("1.234567890123e-06"), 13)
        self.assertEqual(core._sig_digits("-3e-13"), 1)
        self.assertEqual(core._sig_digits("0"), 0)


class TestParseFailure(unittest.TestCase):
    """解析不出东西时要**说清楚为什么**，不能静默返回空列表。"""

    def test_no_trace_raises_with_diagnosis(self):
        txt = "a;b;c\nnot,a,number\nstill,not,one\n"
        try:
            core.parse_csv("<t>", text=txt)
        except ValueError as exc:
            msg = str(exc)
        else:
            self.fail("解析不出 trace 必须抛异常，不能返回 []")
        self.assertIn("分隔符判为", msg)
        self.assertIn("表头", msg)


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
