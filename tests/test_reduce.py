# -*- coding: utf-8 -*-
"""抽点 / 量化 / 重建自检。

自检那一行是整个格式里第一个该看的东西，所以它**必须诚实**：
这里拿一个独立写的朴素实现去核 recon_error 报的数。
"""

import math
import re
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


def _osc_csv(path, t_end=4e-7, f=5.03e9, dc=4e-4, amp=2e-3, tau=2e-8):
    """起振电流的样子：直流 + 起振包络 × 载波。写成 ViVA X/Y 列对。"""
    rows = ["i /VCO/VDD; tran (I) X,i /VCO/VDD; tran (I) Y"]
    t = 0.0
    while t < t_end:
        a = amp * (1 - math.exp(-(t - 1e-7) / tau)) if t > 1e-7 else 0.0
        rows.append("%.12g,%.9g" % (t, dc + a * math.sin(2 * math.pi * f * t)))
        t += 1e-11
    with open(path, "w", newline="\n") as fh:
        fh.write("\n".join(rows) + "\n")
    return path


class TestShares(unittest.TestCase):
    """预算按内容类型分份额。「50 KB 装什么都行，够还原 debug 信息」。"""

    def _run(self, extra, budget="51200"):
        import os
        import tempfile
        fd, p = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            _osc_csv(p)
            rc, txt = C.run_cli([p, "--demod", "--budget", budget] + extra)
            self.assertEqual(rc, 0, txt[:400])
            return txt
        finally:
            os.unlink(p)

    def _pts(self, txt, tag):
        """某个派生块保留了多少点。拿 `# WV1 ... N -> M pts` 那一行数。"""
        blocks = ("\n" + txt).split("\n# WV1")
        for b in blocks:
            if tag in b:
                m = re.search(r"-> (\d+) pts", b)
                if m:
                    return int(m.group(1))
        return None

    def test_share_moves_points_between_content_types(self):
        """把份额给谁，点数就长在谁身上 —— 否则这个旋钮是假的。

        预算得选在**卡得住**的区间：50 KB 下包络早就到了它的自然上限
        （1697 个包络点 RDP 到 353 就不再增加，再多字节也买不到点），
        两种份额都是 353，测不出差别。16 KB 下是 13 vs 353。
        """
        few = self._run(["--share", "raw=90,env=2,freq=8"], budget="16384")
        many = self._run(["--share", "raw=10,env=80,freq=10"], budget="16384")
        self.assertLess(self._pts(few, "env_hi("), self._pts(many, "env_hi("),
                        "给包络加份额，包络的点数没变多")

    def test_share_map_is_proportional(self):
        """摊法本身：权重之比就是字节之比（只在出现的类型之间归一化）。"""
        import os
        import tempfile
        import wave_cli
        fd, p = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            _osc_csv(p)
            args = wave_cli.build_parser().parse_args(
                [p, "--demod", "--budget", "51200",
                 "--share", "raw=60,env=30,freq=10"])
            args.shares = wave_cli.parse_shares(args.share)
            args.windows = 0                     # 不切窗，好数 raw 只有一块
            ship = wave_cli.plan(wave_cli.prepare_traces(p, args), args)
            by = {}
            for b, n in ship.share_map().items():
                by[b.trace.role] = by.get(b.trace.role, 0) + n
            self.assertAlmostEqual(by["raw"] / float(by["env"]), 2.0, delta=0.1)
            self.assertAlmostEqual(by["env"] / float(by["freq"]), 3.0, delta=0.2)
        finally:
            os.unlink(p)

    def test_total_still_inside_budget_whatever_the_shares(self):
        """份额怎么分都不许超预算 —— 超了就粘不进聊天框，功能就没了。"""
        for sh in ("raw=90,env=5,freq=5", "raw=10,env=80,freq=10",
                   "raw=34,env=33,freq=33"):
            txt = self._run(["--share", sh])
            self.assertLessEqual(len(txt.encode("utf-8")), 51200,
                                 "份额 %s 撑爆了预算" % sh)

    def test_unused_share_is_handed_back_not_stranded(self):
        """用不完的份额要收回来摊给吃紧的块。

        包络 15 个点只要 0.3 KB，却按份额分到 8 KB。不收回来，「50 KB」
        里就永远空着一截，而那一截本可以变成原波形的分辨率。
        """
        txt = self._run(["--share", "raw=20,env=75,freq=5"])
        # 包络吃不下 75%，剩下的必须流到别处 —— 总量得明显超过它自己那一份
        self.assertGreater(len(txt.encode("utf-8")), int(51200 * 0.35),
                           "用不完的份额被浪费了，总量远小于预算")

    def test_over_budget_is_always_declared_never_silent(self):
        """份额可以把货撑到超预算，但**绝不许悄悄超**。

        预算紧 + 块多的时候确实压不进去（5 块光固定开销就 11 KB）。那没关系，
        契约从来是「压进去**或者**说清楚压不进去」。

        这条特别值得钉，因为它被自己的分配器抹掉过一次：重分份额时把
        「压不动的块」的预算改成它的实际用量，等于告诉 fit_budget「你没超」，
        那行 `**超预算**` 就再也不打印了 —— 输出看着干干净净，其实是假的。
        """
        for bud in ("8192", "16384", "51200"):
            for sh in ("raw=90,env=5,freq=5", "raw=10,env=80,freq=10",
                       "raw=50,env=30,freq=20"):
                txt = self._run(["--share", sh], budget=bud)
                if len(txt.encode("utf-8")) > int(bud):
                    self.assertIn("超预算", txt,
                                  "预算 %s 份额 %s：出了 %d 字节却没声明"
                                  % (bud, sh, len(txt.encode("utf-8"))))

    def test_bad_share_is_refused_not_guessed(self):
        import os
        import tempfile
        import wave_cli
        fd, p = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            _osc_csv(p, t_end=5e-8)
            for bad in (["--share", "raw=abc"], ["--share", "nosuch=50"],
                        ["--share", "raw=-3"]):
                with self.assertRaises(SystemExit):
                    a = wave_cli.build_parser().parse_args([p] + bad)
                    wave_cli.parse_shares(a.share)
        finally:
            os.unlink(p)


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
        # 这条警告一度只说 --xrange 和加预算，从没提过 --demod ——
        # 结果用户拿着它问「我看不见你说的包络」。最该用的那条要排第一。
        self.assertIn("--demod", w, "没告诉人还有解调这条路")
        self.assertLess(w.index("--demod"), w.index("--xrange"),
                        "要看包络时 --demod 才是首选，得排在前面")

    def test_no_carrier_warning_when_resolution_is_enough(self):
        tr = _osc(t1=4e-8)                       # 200 个周期
        core.set_eps(tr, 0.005)
        red = core.reduce_trace(tr, 0.005, None, core.predecimate(tr, 0.005), [])
        import wave_emit
        self.assertGreaterEqual(len(red.kept) / float(tr.signals[0].cycles),
                                wave_emit.MIN_PTS_PER_CYCLE)
        self.assertEqual(wave_emit.carrier_warn(red), "", "够用就别啰嗦")


def _startup_i(spike=0.0, t1=2e-6, n=40000, f=1.08e9, amp=1.5e-3, dc=1.2e-3):
    """起振电流：慢慢长大的振荡，可选在 t=0 加一发浪涌。

    真实场景就是这个形状 —— VDD 上的电流探针，开机瞬间充退耦电容的浪涌
    比稳态振荡大两三个数量级。
    """
    rows = ["time (s),I(VDD) (A)"]
    for i in range(n):
        t = t1 * i / (n - 1.0)
        env = amp * (1.0 - math.exp(-max(0.0, t - 0.25e-6) / 0.35e-6))
        v = dc + env * math.sin(2 * math.pi * f * t)
        if i == 0:
            v += spike
        rows.append("%.12g,%.9g" % (t, v))
    tr = core.parse_csv("<t>", text="\n".join(rows) + "\n")[0]
    core.analyze(tr)
    return tr


class TestOutlierRange(unittest.TestCase):
    """量程被离群点定死 —— 起振电流上最容易踩的一脚。

    真实症状：打开 GUI，max-points 滑块的上限只剩 10。那个 10 是
    `max(10, len(cand))` 的兜底值，候选点其实只剩 3 个。
    """

    def test_body_range_ignores_the_inrush_spike(self):
        tr = _startup_i(spike=0.9)
        s = tr.signals[0]
        self.assertGreater(s.rng, 0.5, "全量程该被浪涌顶上去")
        self.assertLess(s.rng_body, 0.01, "主体量程不该被一个点带跑")
        self.assertEqual(s.n_out, 1, "只有那一个点算离群")

    def test_spike_no_longer_collapses_the_candidate_set(self):
        clean = _startup_i(spike=0.0)
        spiky = _startup_i(spike=0.9)
        core.set_eps(clean, 0.005)
        core.set_eps(spiky, 0.005)
        c0 = len(core.predecimate(clean, 0.005))
        c1 = len(core.predecimate(spiky, 0.005))
        self.assertGreater(c1, 0.5 * c0,
                           "一个离群点就把候选集打掉一半以上（%d -> %d）" % (c0, c1))

    def test_it_says_so(self):
        tr = _startup_i(spike=0.9)
        core.set_eps(tr, 0.005)
        self.assertTrue(any("离群点" in n and "主体量程" in n for n in tr.notes),
                        "换了量程口径就必须说出来")

    def test_clean_trace_keeps_the_full_range(self):
        """没有离群点时**什么都不许变** —— 这条改的是异常路径，不是默认行为。"""
        tr = _startup_i(spike=0.0)
        s = tr.signals[0]
        rng, robust = core.eps_range(tr, s)
        self.assertFalse(robust)
        self.assertEqual(rng, s.rng)
        core.set_eps(tr, 0.005)
        self.assertFalse(any("离群点" in n for n in tr.notes))

    def test_spike_itself_survives(self):
        """换量程口径不是把浪涌扔了 —— 它还在数据里，也还在 METRICS 里。"""
        tr = _startup_i(spike=0.9)
        core.set_eps(tr, 0.005)
        red = core.reduce_trace(tr, 0.005, None, core.predecimate(tr, 0.005), [])
        self.assertIn(tr.signals[0].vmax_at, red.kept, "极值点必须还在")
        self.assertGreater(tr.signals[0].vmax, 0.5, "METRICS 量的还是全精度真值")

    def test_spike_no_longer_wrecks_the_rest_of_the_wave(self):
        """判据是「跟没有浪涌那份比」，不是一个绝对值。

        绝对值会把另一件事混进来：每周期点数不够本来就有误差（候选集上限那条），
        那跟浪涌无关。这里要问的只有一句 —— 多了一个离群点，其余的点有没有变差。
        原来是变差到 99.8%（整条被压成直线）。
        """
        def worst_over_body(spike):
            tr = _startup_i(spike=spike)
            core.set_eps(tr, 0.005)
            red = core.reduce_trace(tr, 0.005, None,
                                    core.predecimate(tr, 0.005), [])
            return red.worst.maxerr / tr.signals[0].rng_body

        clean, spiky = worst_over_body(0.0), worst_over_body(0.9)
        self.assertLess(spiky, 2.0 * clean + 0.02,
                        "浪涌把其余点的误差抬上去了（%.3f -> %.3f，按主体量程算）"
                        % (clean, spiky))


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


class TestVerdictAndBlockers(unittest.TestCase):
    """出口台那两条判据和出路卡那张决策表 —— 全是纯函数，不碰 Tk。

    这一层单独测得起来，是把 `_status` 从「一路 % 拼字符串」拆出来的理由：
    原来「有 WARN 时下一步提示一定消失」「预算封顶这句话在装得下时也会出现」
    这类毛病，全是渲染时拼出来的，没法测。
    """

    def _ship(self, name="demo_tran.csv", budget=20480, **kw):
        import wave_cli
        args = wave_cli.build_parser().parse_args([])
        args.budget = budget
        for k, v in kw.items():
            setattr(args, k, v)
        return wave_cli.plan(wave_cli.prepare_traces(C.ex(name), args), args)

    def test_two_independent_criteria(self):
        """装得下和够得准是两件事，不许混成一个符号。"""
        ship = self._ship()
        v = ship.verdict()
        self.assertTrue(v.bytes_ok, "demo_tran 压到预算之后该装得下")
        self.assertIn(v.err_ok, ("ok", "warn", "bad"))
        self.assertEqual(v.level, "bad" if (not v.bytes_ok or v.err_ok == "bad")
                         else ("warn" if v.err_ok == "warn" else "ok"))

    def test_over_budget_is_bad_even_if_accurate(self):
        ship = self._ship(budget=2048)          # 故意给不够
        v = ship.verdict()
        self.assertFalse(v.bytes_ok)
        self.assertEqual(v.level, "bad")
        self.assertGreater(v.over(), 1.0)

    def test_blockers_give_actions_not_prose(self):
        """出路必须是能点的东西，不是一句话里的三个片段。"""
        ship = self._ship(budget=2048)
        bl = ship.blockers()
        self.assertTrue(bl, "超预算了却一条出路都没给")
        self.assertTrue(bl[0].actions, "出路卡没有可执行的动作")
        for code, label in bl[0].actions:
            self.assertTrue(code and label)

    def test_all_green_gives_no_blocker(self):
        """没事就别占那 44 px。"""
        ship = self._ship(budget=0)             # 不限预算
        ship.compute(force=True)
        v = ship.verdict()
        if v.level == "ok":
            self.assertEqual(ship.blockers(), [])

    def test_carrier_beats_budget_in_the_ordering(self):
        """信息量那条排在预算前面 —— 里面几层拖参数还有救，最外层拖什么都没用。"""
        import wave_emit
        ship = self._ship("demo_tran.csv", budget=512)
        hit = any(wave_emit.carrier_exits(b.red) for b in ship.included()
                  if b.red is not None)
        bl = ship.blockers()
        if hit:
            self.assertEqual(bl[0].code, "carrier",
                             "撞上周期数那堵墙时应该先说它，而不是先说超预算")


if __name__ == "__main__":
    unittest.main()
