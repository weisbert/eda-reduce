# -*- coding: utf-8 -*-
"""抽点 / 量化 / 重建自检。

自检那一行是整个格式里第一个该看的东西，所以它**必须诚实**：
这里拿一个独立写的朴素实现去核 recon_error 报的数。
"""

import math
import unittest

import _common as C
from _common import core


def naive_recon_error(tr, red, si):
    """朴素实现：一个个点插值，跟 wave_core 的快路径无关。

    故意写得笨：不复用 core 的段游标，直接每个点二分。慢，但错不了。
    """
    import bisect
    s = tr.signals[si]
    cs = red.specs[si]
    logx = tr.xscale == "log"
    kx = [red.xspec.val(tr.x[i]) for i in red.kept]
    ky = [cs.from_out(float(cs.txt(s.y[i]))) for i in red.kept]
    if logx:
        kx = [math.log(v) for v in kx]
    worst, at, acc = 0.0, None, 0.0
    for i in range(len(tr.x)):
        xv = math.log(tr.x[i]) if (logx and tr.x[i] > 0) else tr.x[i]
        j = bisect.bisect_right(kx, xv) - 1
        j = max(0, min(j, len(kx) - 2))
        x0, x1 = kx[j], kx[j + 1]
        r = ky[j] if x1 == x0 else ky[j] + (xv - x0) / (x1 - x0) * (ky[j + 1] - ky[j])
        d = abs(s.y[i] - r)
        acc += d * d
        if d > worst:
            worst, at = d, tr.x[i]
    return worst, at, math.sqrt(acc / len(tr.x))


class TestHonestSelfCheck(unittest.TestCase):
    """报出来的 max|err| 必须是真的。报小了整个格式就不可信了。"""

    def _check(self, name, **kw):
        tr = C.load(name)
        red = core.reduce_trace(tr, **kw)
        for si in range(len(tr.signals)):
            w, at, rms = naive_recon_error(tr, red, si)
            e = red.err[si]
            self.assertAlmostEqual(
                e.maxerr, w, delta=max(1e-12, 1e-6 * abs(w)),
                msg="%s/%s: 自检报 %g，独立实现算出 %g"
                    % (name, tr.signals[si].name, e.maxerr, w))
            self.assertAlmostEqual(e.rms, rms, delta=max(1e-12, 1e-6 * rms))
            self.assertAlmostEqual(e.at, at, delta=1e-18)

    def test_tran(self):
        self._check("demo_tran.csv", max_points=400)

    def test_log_axis(self):
        # log 轴上 RDP 在 log 空间切，重建也必须在 log 空间 —— 两边不一致的话
        # 自检报的是另一条曲线的误差（AC 上实测差了 40 倍）
        self._check("demo_ac.csv")

    def test_spectrum(self):
        self._check("demo_spec.csv", max_points=300)

    def test_error_includes_quantization(self):
        """自检必须用**量化之后**的值重建，否则只算了抽点误差那一半好看的。

        保留点上抽点误差恒为 0，所以那里剩下的偏差只可能来自量化。
        全局 max|err| 必须 >= 这些点上的量化误差 —— 小于就说明自检拿原值重建了。
        """
        tr = C.load("demo_tran.csv")
        red = core.reduce_trace(tr, max_points=400)
        for si, (cs, e) in enumerate(zip(red.specs, red.err)):
            qmax = max(abs(cs.sig.y[i] - cs.from_out(float(cs.txt(cs.sig.y[i]))))
                       for i in red.kept)
            self.assertGreater(qmax, 0.0, "量化本来就该带来偏差")
            self.assertLessEqual(qmax, cs.q_nat, "量化误差不超过一个步长")
            self.assertGreaterEqual(
                e.maxerr, qmax - 1e-15,
                "%s: 自检 %g 小于保留点上的量化误差 %g —— 说明重建用了原值"
                % (cs.sig.name, e.maxerr, qmax))


class TestBudget(unittest.TestCase):

    def test_max_points_respected_modulo_forced(self):
        tr = C.load("demo_tran.csv")
        for mp in (10, 50, 200, 800):
            red = core.reduce_trace(tr, max_points=mp, keep_extrema=False)
            self.assertLessEqual(len(red.kept), mp + 2,
                                 "没有强制点时必须严格守住预算")

    def test_forced_points_win_over_budget(self):
        """强制点优先于预算 —— 牺牲它们就等于回到 spur 低报 54x 那个坑。"""
        tr = C.load("demo_tran.csv")
        forced = list(range(0, len(tr.x), 20))
        red = core.reduce_trace(tr, max_points=5, forced=forced)
        for i in forced:
            self.assertIn(i, red.kept, "强制点必须在结果里")
        self.assertGreater(len(red.kept), 5, "强制点多于预算时结果只能超")

    def test_extrema_always_kept(self):
        tr = C.load("demo_tran.csv")
        red = core.reduce_trace(tr, max_points=20)
        for s in tr.signals:
            self.assertIn(s.vmin_at, red.kept)
            self.assertIn(s.vmax_at, red.kept)


class TestQuantization(unittest.TestCase):

    def test_step_at_most_quarter_eps(self):
        """量化步长 <= eps/4：抽点误差已经接受了，量化再细是纯浪费字节。"""
        tr = C.load("demo_tran.csv")
        core.set_eps(tr, 0.005)
        for cs in core.make_colspec(tr, 0.005):
            self.assertLessEqual(cs.q_nat, 0.25 * cs.sig.eps + 1e-300)

    def test_step_is_power_of_ten(self):
        # 定点文本里只有 10 的整数次幂才真省字节：q=2e-4 和 1e-4 都要写 4 位小数
        tr = C.load("demo_tran.csv")
        for cs in core.make_colspec(tr, 0.005):
            lg = math.log10(cs.q_nat)
            self.assertAlmostEqual(lg, round(lg), delta=1e-9)

    def test_decimals_match_step(self):
        tr = C.load("demo_tran.csv")
        for cs in core.make_colspec(tr, 0.005):
            v = cs.txt(cs.sig.y[len(cs.sig.y) // 2])
            frac = v.split(".")[1] if "." in v else ""
            self.assertLessEqual(len(frac), cs.dec)


class TestPredecimate(unittest.TestCase):

    def test_shoulder_point_kept(self):
        """没有肩点，RDP 的弦会从平坦区直接切进凹坑，误差爆掉。"""
        y = [0.0] * 50 + [-1.0] + [0.0] * 50        # 一个孤立的坑
        txt = "time (s),V(a) (V)\n" + "".join(
            "%g,%g\n" % (i * 1e-9, v) for i, v in enumerate(y))
        tr = core.parse_csv("<t>", text=txt)[0]
        core.analyze(tr)
        core.set_eps(tr, 0.005)
        cand = core.predecimate(tr, 0.005)
        self.assertIn(50, cand, "坑底")
        self.assertIn(49, cand, "坑前的肩点 —— 少了它弦会切进坑里")

    def test_candidate_cap(self):
        """噪声主导的通道会让候选集 = 原始集，GUI 的性能保证就没了。"""
        tr = C.load("demo_tran.csv")
        core.set_eps(tr, 0.005)
        cand = core.predecimate(tr, 0.005, max_cand=200)
        self.assertLessEqual(len(cand), 260, "超上限要放大阈值重扫")
        self.assertTrue(any("预细化阈值放大" in n for n in tr.notes),
                        "放大了要说出来")

    def test_endpoints_always_in(self):
        tr = C.load("demo_ac.csv")
        core.set_eps(tr, 0.005)
        cand = core.predecimate(tr, 0.005)
        self.assertEqual(cand[0], 0)
        self.assertEqual(cand[-1], len(tr.x) - 1)


class TestNoiseFloor(unittest.TestCase):

    def test_eps_floored_by_noise(self):
        """一条几乎平的噪声通道，它的量程本身就是噪声。
        tol×量程 会比噪声低几个数量级，RDP 一个点都删不掉 —— 在编码噪声。"""
        tr = C.load("demo_tran.csv")
        core.set_eps(tr, 0.005)
        vref = [s for s in tr.signals if s.name == "V(vref)"][0]
        self.assertGreater(vref.noise, 0.0)
        self.assertAlmostEqual(vref.eps, core.NOISE_K * vref.noise, delta=1e-12)
        self.assertGreater(vref.eps, 0.005 * vref.rng, "该被噪声底抬起来")
        self.assertTrue(any("噪声底" in n for n in tr.notes), "抬了要说出来")

    def test_clean_channel_uses_tol(self):
        tr = C.load("demo_tran.csv")
        core.set_eps(tr, 0.005)
        v = [s for s in tr.signals if s.name == "V(ctrl)"][0]
        self.assertAlmostEqual(v.eps, 0.005 * v.rng, delta=1e-9,
                               msg="干净通道该由 tol 说了算")


def _osc(t0=0.0, t1=4e-8, dt=1e-11, f=5.03e9, amp=0.28, dc=0.3):
    """一段干净的正弦，每周期 20 个原始点。振荡通道是一等公民的场景。"""
    rows = ["time (s),V(o) (V)"]
    t = t0
    while t < t1:
        rows.append("%.12g,%.9g" % (t, dc + amp * math.sin(2 * math.pi * f * t)))
        t += dt
    tr = core.parse_csv("<t>", text="\n".join(rows) + "\n")[0]
    core.analyze(tr)
    return tr


class TestCarrierResolution(unittest.TestCase):
    """振荡波形上的三条天花板。真实困惑：「max-points 拖到最大还是差很多」。"""

    def test_cycle_count_is_close(self):
        tr = _osc()
        want = 4e-8 * 5.03e9
        self.assertAlmostEqual(tr.signals[0].cycles, want, delta=0.05 * want,
                               msg="周期数要数得准，报「几点/周期」全靠它")

    def test_noise_does_not_fake_cycles(self):
        """滞回没做对的话，平信号上的噪声能数出成千上万个假周期。"""
        import random
        random.seed(3)
        rows = ["time (s),V(o) (V)"]
        for i in range(4000):
            rows.append("%.12g,%.9g" % (i * 1e-11, 0.8 + 1e-4 * random.gauss(0, 1)))
        tr = core.parse_csv("<t>", text="\n".join(rows) + "\n")[0]
        core.analyze(tr)
        self.assertLess(tr.signals[0].cycles, 100, "噪声被数成了振荡")

    def test_candidate_cap_does_not_overshoot(self):
        """候选集是**质量上限**，冲过头就是白丢分辨率。

        原来的乘法放大在正弦上会一步冲过头（实测上限 20000 只拿到 9448）：
        阈值一旦越过正弦的单步变化量，候选数就断崖式塌到每周期两三个。
        """
        tr = _osc(t1=2e-7)                       # 1000 个周期，20000 个原始点
        core.set_eps(tr, 0.005)
        cap = 4000
        cand = core.predecimate(tr, 0.005, max_cand=cap)
        self.assertLessEqual(len(cand), cap, "不许超上限")
        self.assertGreater(len(cand), 0.6 * cap,
                           "冲过头了：只拿到 %d / %d" % (len(cand), cap))
        self.assertTrue(any("候选集是质量上限" in n for n in tr.notes),
                        "压了要说出来，而且要说清是质量上限不是性能参数")

    def test_warns_when_shape_cannot_draw_the_carrier(self):
        import wave_emit
        tr = _osc(t1=4e-7)                       # 2000 个周期
        core.set_eps(tr, 0.005)
        red = core.reduce_trace(tr, 0.005, 800, core.predecimate(tr, 0.005), [])
        w = wave_emit.carrier_warn(red)
        self.assertTrue(w, "800 点画 2000 个周期，必须报出来")
        self.assertIn("点/周期", w)
        self.assertIn("--xrange", w, "得给出做得到的路径")

    def test_no_carrier_warning_when_resolution_is_enough(self):
        tr = _osc(t1=4e-8)                       # 200 个周期
        core.set_eps(tr, 0.005)
        red = core.reduce_trace(tr, 0.005, None, core.predecimate(tr, 0.005), [])
        import wave_emit
        self.assertGreaterEqual(len(red.kept) / float(tr.signals[0].cycles),
                                wave_emit.MIN_PTS_PER_CYCLE)
        self.assertEqual(wave_emit.carrier_warn(red), "", "够用就别啰嗦")


class TestWindow(unittest.TestCase):
    """`--xrange`：把预算花在你要看的那一段上。"""

    def test_slice_keeps_only_the_window_and_declares_it(self):
        tr = _osc(t1=4e-7)
        n_full = len(tr.x)
        core.slice_trace(tr, 1e-7, 1.2e-7)
        core.analyze(tr)
        self.assertLess(len(tr.x), n_full / 5)
        self.assertGreaterEqual(tr.x[0], 1e-7)
        self.assertLessEqual(tr.x[-1], 1.2e-7)
        self.assertEqual(tr.window[2:], (0.0, tr.window[3]))
        self.assertTrue(any("只导出了窗口" in n for n in tr.notes))
        self.assertTrue(any("窗口末态" in n for n in tr.notes),
                        "末态语义变了必须说 —— 模型会把窗口末态当仿真末态读")

    def test_window_beats_full_range_on_the_same_budget(self):
        """同样的字节预算，窗口能把每周期点数抬上去 —— 这是这个功能的全部理由。"""
        import wave_emit
        full = _osc(t1=4e-7)
        core.set_eps(full, 0.005)
        r1 = core.reduce_trace(full, 0.005, 800, core.predecimate(full, 0.005), [])
        ppc1 = len(r1.kept) / float(full.signals[0].cycles)

        win = _osc(t1=4e-7)
        core.slice_trace(win, 1e-7, 1.2e-7)
        core.analyze(win)
        core.set_eps(win, 0.005)
        r2 = core.reduce_trace(win, 0.005, 800, core.predecimate(win, 0.005), [])
        ppc2 = len(r2.kept) / float(win.signals[0].cycles)
        self.assertGreater(ppc2, 4 * ppc1,
                           "窗口没换来分辨率（%.2f -> %.2f 点/周期）" % (ppc1, ppc2))
        self.assertLess(r2.worst.pct, r1.worst.pct / 2.0)

    def test_window_covering_everything_is_not_a_window(self):
        tr = _osc()
        core.slice_trace(tr, -1.0, 1.0)
        self.assertIsNone(tr.window, "盖住全长就不该声称自己是窗口")

    def test_empty_window_raises_with_the_numbers(self):
        tr = _osc()
        try:
            core.slice_trace(tr, 1.0, 2.0)
        except ValueError as exc:
            self.assertIn("窗口", str(exc))
            self.assertIn("整条", str(exc))
        else:
            self.fail("窗口里没有点必须报错")

    def test_parse_eng(self):
        self.assertAlmostEqual(core.parse_eng("1.6u"), 1.6e-6)
        self.assertAlmostEqual(core.parse_eng("300n"), 300e-9)
        self.assertAlmostEqual(core.parse_eng("-5m"), -5e-3)
        self.assertAlmostEqual(core.parse_eng("1.2G"), 1.2e9)
        self.assertAlmostEqual(core.parse_eng("1e-9"), 1e-9)


if __name__ == "__main__":
    unittest.main()
