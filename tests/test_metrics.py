# -*- coding: utf-8 -*-
"""测量对真值。

`examples/demo_truth.json` 里的数是解析式或 40 万点精密栅格算出来的，
不是量出来的。所以「测量对不对」在这里是**可判定**的，不用靠肉眼看曲线像不像。

容差都写了理由。凡是写不出理由的容差，说明测量本身没想清楚。
"""

import math
import re
import unittest

import _common as C
from _common import core, emit


def get(ms, col, name):
    for m in ms:
        if m.col == col and m.name == name:
            return m
    return None


def val(ms, col, name):
    m = get(ms, col, name)
    if m is None:
        return None
    return float(m.value) if isinstance(m.value, str) else m.value


class TestTranMetrics(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tr = C.load("demo_tran.csv")
        cls.m = C.metrics_of(cls.tr)
        cls.ms = cls.m.metrics()
        cls.ev = cls.m.events()

    def t(self, sig):
        return C.tran_truth(sig)

    # ---- c1 V(vdd_pll)：阶跃 droop + 振铃 + glitch

    def test_extrema(self):
        t = self.t("V(vdd_pll)")
        # 容差 1 个噪声 sigma：极值点上噪声直接进结果，本来就不可能更准
        sig = 150e-6
        self.assertAlmostEqual(val(self.ms, "c1", "min"), t["min"], delta=sig)
        self.assertAlmostEqual(val(self.ms, "c1", "max"), t["max"], delta=sig)
        # 位置：谷底附近栅格 10 ps，抛物线精修后应当到 ps 量级
        self.assertAlmostEqual(get(self.ms, "c1", "min").at, t["min_at"],
                               delta=50e-12)

    def test_overshoot(self):
        t = self.t("V(vdd_pll)")
        # 容差 3%（相对）：过冲峰是 25 MHz 振铃的平顶，噪声会把 argmax 挪一点
        self.assertTrue(C.close(val(self.ms, "c1", "overshoot"),
                                t["overshoot_pct"], rel=0.03))

    def test_settling(self):
        t = self.t("V(vdd_pll)")
        # 容差 2%：settle 定义为最后一次越出容差带，包络穿越点附近很陡，本该很准
        self.assertTrue(C.close(val(self.ms, "c1", "settle(+-1%)"),
                                t["settle_1pct"], rel=0.02))

    def test_settling_too_tight_band_declared_not_guessed(self):
        """±0.1% 带宽是 800 uV，噪声 155 uV —— 测不了就要说测不了。"""
        m = get(self.ms, "c1", "settle(+-0.1%)")
        self.assertIsNotNone(m)
        self.assertEqual(m.value, "n/a")
        self.assertIn("噪声底", m.note)

    def test_slew_reports_window(self):
        t = self.t("V(vdd_pll)")
        m = get(self.ms, "c1", "slew_max")
        self.assertIn("窗口", m.note, "斜率不报测量窗口就没意义")
        # 容差 8%：窗口按「噪声贡献 <5%」反推，再加有限窗口对峰值斜率的低估
        self.assertTrue(C.close(m.value * 1e6, t["slew_max"], rel=0.08),
                        "%g vs %g" % (m.value * 1e6, t["slew_max"]))

    def test_glitch(self):
        t = self.t("V(vdd_pll)")
        m = get(self.ms, "c1", "glitch1")
        self.assertIsNotNone(m, "埋了一个 210 ps 的窄尖峰，必须检出来")
        self.assertAlmostEqual(m.at, t["glitch_at"], delta=1e-9)
        # 深度容差 3 sigma：尖峰顶上的噪声直接叠进来
        self.assertAlmostEqual(m.value, t["glitch_depth"], delta=3 * 150e-6)
        self.assertIn("width", m.note)
        w = float(m.note.split("width ")[1].split()[0])
        u = m.note.split("width ")[1].split()[1].strip(",")
        self.assertIn(u, ("fs", "ps", "ns", "us"), "宽度得带单位")
        w *= {"fs": 1e-15, "ps": 1e-12, "ns": 1e-9, "us": 1e-6}[u]
        self.assertTrue(C.close(w, t["glitch_fwhm"], rel=0.25),
                        "FWHM %g vs 真值 %g" % (w, t["glitch_fwhm"]))

    def test_no_false_glitch_on_ringing(self):
        """阶跃后的振铃曲率不能被报成 glitch（只埋了一个）。"""
        gl = [m for m in self.ms if m.col == "c1" and m.name.startswith("glitch")]
        self.assertEqual(len(gl), 1, "多报的是振铃曲率，不是尖峰")

    # ---- c2 V(ctrl)：时钟

    def test_period_and_jitter(self):
        t = self.t("V(ctrl)")
        self.assertTrue(C.close(val(self.ms, "c2", "period"), t["period"],
                                rel=1e-4))
        # 抖动容差 5%：注入 3 ps/沿，dt 是 15 ps，全靠阈值穿越插值。
        # 不插值（取采样点）的话这一项会差 10% 以上 —— 这个测试守的就是那条
        self.assertTrue(C.close(val(self.ms, "c2", "jitter_rms"),
                                t["jitter_rms"], rel=0.05),
                        "%g vs %g" % (val(self.ms, "c2", "jitter_rms"),
                                      t["jitter_rms"]))
        self.assertTrue(C.close(val(self.ms, "c2", "jitter_pp"), t["jitter_pp"],
                                rel=0.05))
        self.assertIn("N=%d cycles" % t["n_cycles"],
                      get(self.ms, "c2", "jitter_pp").note)

    def test_edges_and_duty(self):
        t = self.t("V(ctrl)")
        # 上升沿容差 2%：升余弦沿上 6~7 个采样点，10/90 两端各插一次值
        self.assertTrue(C.close(val(self.ms, "c2", "rise(10-90)"),
                                t["rise_1090"], rel=0.02))
        self.assertTrue(C.close(val(self.ms, "c2", "fall(10-90)"),
                                t["fall_1090"], rel=0.02))
        self.assertTrue(C.close(val(self.ms, "c2", "duty"), t["duty"] * 100.0,
                                rel=0.005))

    def test_clock_has_no_settling_metric(self):
        """适用性判断：时钟报 settling time 是没意义的，不该出现。"""
        for nm in ("settle(+-1%)", "overshoot", "final"):
            self.assertIsNone(get(self.ms, "c2", nm), "时钟不该有 " + nm)

    # ---- c3 I(mp0)：阶跃电流 + 纹波

    def test_time_weighted_mean(self):
        t = self.t("I(mp0)")
        # 容差 1e-3：栅格非均匀，必须梯形积分。直接平均会差好几个百分点
        self.assertTrue(C.close(val(self.ms, "c3", "mean"), t["mean"], rel=1e-3))
        self.assertTrue(C.close(val(self.ms, "c3", "rms_ripple"),
                                t["rms_ripple"], rel=1e-3))

    def test_no_bogus_undershoot_from_prestep_level(self):
        """阶跃前的初始电平不是回冲。0.87 -> 1.5 mA 曾被报成 -42% 回冲。"""
        u = val(self.ms, "c3", "undershoot")
        if u is not None:
            self.assertLess(abs(u), 10.0, "回冲只该是阶跃**之后**的纹波")

    def test_not_settled_is_declared(self):
        """纹波 ±60 uA 进不去 ±1% 的 15 uA 带 —— 说未 settle，别报个假数。"""
        m = get(self.ms, "c3", "settle(+-1%)")
        self.assertEqual(m.value, "n/a")
        self.assertIn("未 settle", m.note)

    # ---- c4 V(vref)：几乎平的慢漂移

    def test_flat_channel_gets_no_step_metrics(self):
        t = self.t("V(vref)")
        self.assertTrue(C.close(val(self.ms, "c4", "mean"), t["mean"], rel=1e-3))
        self.assertIsNone(get(self.ms, "c4", "overshoot"),
                          "单调漂移不是过冲")
        self.assertIsNotNone(get(self.ms, "c4", "drift"))

    def test_events_sorted_and_tagged(self):
        xs = [e.x for e in sorted(self.ev, key=lambda e: e.x)]
        self.assertEqual(xs, sorted(xs))
        tags = set(e.tag for e in self.ev)
        for want in ("GLOB_MIN", "GLOB_MAX", "GLITCH", "SLEW_MAX", "SETTLED"):
            self.assertIn(want, tags)

    def test_edge_truncation_is_declared(self):
        """94 个沿不能全塞进 EVENTS，但截断了必须说出来。"""
        tot = [e for e in self.ev if e.tag == "EDGES_TOTAL"]
        self.assertTrue(tot, "总数要报")
        self.assertIn("共", tot[0].detail)


class TestFreqSpectrum(unittest.TestCase):
    """spur 保护是 LOCKED 规则（wave-spec 第 4 节）。

    实测漏采会把 353 nV 的 spur 报成 6.5 nV，**低报 54 倍**。
    这一组是防回归的核心闸门：动了抽点逻辑就得让它过。
    """

    @classmethod
    def setUpClass(cls):
        cls.tr = C.load("demo_spec.csv")
        cls.m = C.metrics_of(cls.tr)
        cls.ms = cls.m.metrics()
        cls.truth = C.truth()["demo_spec.csv"]["signals"]["V(vco_out)"]

    def test_linear_spectrum_converted_to_db(self):
        """线性域里 -52 dBc 的 spur 只有 2.5 mV，tol×量程 是 5 mV ——
        不转 dB 的话 RDP 和量化会联手把它抹平。"""
        self.assertEqual(self.tr.signals[0].unit, "dBc")
        self.assertTrue(any("转 dBc" in n for n in self.tr.notes), "换了要说")

    def test_carrier(self):
        self.assertTrue(C.close(val(self.ms, "c1", "carrier"),
                                self.truth["carrier_f"], rel=1e-4))

    def test_all_spurs_found(self):
        got = [m for m in self.ms if re.match(r"^spur\d+$", m.name)]
        self.assertEqual(len(got), len(self.truth["spurs"]),
                         "三根都要检出来")

    def test_spur_dbc_and_ratio(self):
        for s in self.truth["spurs"]:
            hit = [m for m in self.ms
                   if re.match(r"^spur\d+$", m.name) and abs(m.at - s["f"]) < 5e6]
            self.assertTrue(hit, "没找到 %g Hz 的 spur" % s["f"])
            m = hit[0]
            # 0.15 dB：谱线只有一两个 bin 宽，抛物线精修的残差就在这个量级。
            # 对的是 dbc_observed（洛伦兹尾巴会互相叠加），不是注入值
            self.assertAlmostEqual(float(m.value), s["dbc_observed"], delta=0.15,
                                   msg="%g Hz" % s["f"])
            self.assertIn("f/f0", m.note)
            r = float(m.note.split("f/f0 ")[1].split(",")[0])
            self.assertTrue(C.close(r, s["f_over_f0"], rel=1e-3))

    def test_threshold_is_declared(self):
        m = get(self.ms, "c1", "spur_thr")
        self.assertIsNotNone(m, "判据要写出来 —— 低于阈值的没报，得让人知道")
        self.assertIn("低于它的没报", m.note)

    def test_forced_indices_cover_hwhm_envelope(self):
        """峰本身 + HWHM 包络都要钉住。只钉峰的话抽点会把它削成孤立毛刺。"""
        forced = set(self.m.forced())
        x = self.tr.x
        for s in self.truth["spurs"]:
            near = [i for i in forced if abs(x[i] - s["f"]) < 10 * self.truth["hwhm"]]
            self.assertGreaterEqual(len(near), 5,
                                    "%g Hz 附近钉住的点太少" % s["f"])


class TestSpurSurvivesPipeline(unittest.TestCase):
    """端到端：压完之后 spur 还在不在。这是 LOCKED 规则真正要守的东西。"""

    @classmethod
    def setUpClass(cls):
        import wave_cli
        rc, txt = C.run_cli([C.ex("demo_spec.csv")])
        cls.rc, cls.txt = rc, txt
        cls.truth = C.truth()["demo_spec.csv"]["signals"]["V(vco_out)"]
        _ = wave_cli

    def shape(self):
        lines = self.txt.splitlines()
        i = [k for k, ln in enumerate(lines) if ln.startswith("[SHAPE]")][0]
        out = []
        for ln in lines[i + 1:]:
            if not ln.strip() or ln.startswith("#") or ln.startswith("["):
                continue
            p = ln.split()
            out.append((float(p[0]), float(p[1])))
        return out

    def test_spurs_present_in_shape(self):
        pts = self.shape()
        self.assertGreater(len(pts), 50)
        for s in self.truth["spurs"]:
            f = s["f"] / 1e9                       # SHAPE 的 x 单位是 GHz
            near = [(a, b) for a, b in pts if abs(a - f) < 0.02]
            self.assertGreaterEqual(
                len(near), 5, "%g GHz 附近保留的点太少 —— spur 被抽没了" % f)
            top = max(b for _, b in near)
            # 0.3 dB：SHAPE 是量化过的（0.1 dB 步长）+ 抛物线顶点没进 SHAPE
            self.assertAlmostEqual(
                top, s["dbc_observed"], delta=0.3,
                msg="%g GHz 的峰在 SHAPE 里是 %.2f dBc，真值 %.2f dBc"
                    % (f, top, s["dbc_observed"]))

    def test_without_protection_spurs_are_lost(self):
        """反证：关掉 metrics（也就关掉强制保留和 dB 转换）就丢。

        这个测试存在的意义是让「保护规则值多少」有个数，
        以后有人想简化抽点逻辑时能看见代价。
        """
        rc, txt = C.run_cli([C.ex("demo_spec.csv"), "--no-metrics"])
        lines = txt.splitlines()
        i = [k for k, ln in enumerate(lines) if ln.startswith("[SHAPE]")][0]
        pts = []
        for ln in lines[i + 1:]:
            if not ln.strip() or ln.startswith("#") or ln.startswith("["):
                continue
            p = ln.split()
            pts.append((float(p[0]), float(p[1])))
        lost = 0
        for s in self.truth["spurs"]:
            f = s["f"] / 1e9
            if not [1 for a, _ in pts if abs(a - f) < 0.02]:
                lost += 1
        self.assertGreaterEqual(lost, 2, "没有保护规则时本来就该丢")
        _ = rc


class TestFreqAC(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tr = C.load("demo_ac.csv")
        cls.m = C.metrics_of(cls.tr)
        cls.ms = cls.m.metrics()
        cls.truth = C.truth()["demo_ac.csv"]["signals"]["dB20(V(out))"]

    def test_not_treated_as_spectrum(self):
        """AC 曲线是光滑的，不该被当成谱去转 dB（它本来就是 dB）。"""
        self.assertEqual(self.tr.signals[0].unit, "dB")
        self.assertIsNone(get(self.ms, "c1", "carrier"))

    def test_dc_peak_bandwidth(self):
        self.assertAlmostEqual(val(self.ms, "c1", "dc"), self.truth["dc_db"],
                               delta=0.01)
        # 峰值容差 0.02 dB / 峰频 0.5%：120 点/decade 的栅格 + 抛物线精修
        self.assertAlmostEqual(val(self.ms, "c1", "peak"),
                               self.truth["peak_db"], delta=0.02)
        self.assertTrue(C.close(get(self.ms, "c1", "peak").at,
                                self.truth["peak_f"], rel=0.005))

    def test_phase_is_raw_no_pm(self):
        """相位只给裸值 —— 这条曲线是不是环路增益，脚本判断不了。"""
        self.assertIsNotNone(get(self.ms, "c2", "phase@dc"))
        self.assertIsNone(get(self.ms, "c2", "phase_margin"))

    def test_no_fft_of_transient(self):
        """明确不做：脚本不对变步长 transient 做 FFT（wave-spec 第 8 节）。"""
        tr = C.load("demo_tran.csv")
        m = C.metrics_of(tr)
        self.assertEqual(m.kind, "tran")
        self.assertFalse(any("spur" in x.name for x in m.metrics()))
        _ = math, emit, core


if __name__ == "__main__":
    unittest.main()
