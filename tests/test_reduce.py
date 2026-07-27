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


if __name__ == "__main__":
    unittest.main()
