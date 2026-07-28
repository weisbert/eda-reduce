# -*- coding: utf-8 -*-
"""解调：把载波换成包络 + 瞬时频率 + 几个代表性周期。

存在的理由是信息率不等于采样率——2515 个周期里没有 2515 份独立信息。
这里钉的是三件事：**不发明数据**、**异常周期必须留原始样点**、**自检照旧**。
"""

import math
import unittest

import _common as C
from _common import core, emit

import wave_demod as dm


def osc(t_end=4e-7, dt=1e-11, f=5.03e9, t_start=1e-7, tau=2e-8,
        dc=0.008, dc2=0.30, t_step=5e-8, amp=0.28, squeg=0.0):
    """起振：直流台阶 -> 起振 -> 稳幅。可选 squegging（包络一跳一跳）。"""
    rows = ["time (s),V(o) (V)"]
    t = 0.0
    while t < t_end:
        base = dc if t < t_step else dc2
        a = 0.0
        if t >= t_start:
            u = (t - t_start) / tau
            a = amp * (1.0 - math.exp(-u))
            if squeg:
                a *= 1.0 + squeg * math.sin(2 * math.pi * u * 0.8)
        rows.append("%.12g,%.9g" % (t, base + a * math.sin(2 * math.pi * f * t)))
        t += dt
    tr = core.parse_csv("<t>", text="\n".join(rows) + "\n")[0]
    core.analyze(tr, kind="tran")
    return tr


class TestCycleFinding(unittest.TestCase):

    def test_period_is_not_quantized_by_the_grid(self):
        """过零必须插值。按栅格点取的话周期被步长量化，5 GHz 配 10 ps 栅格
        就是 5% 的假抖动，瞬时频率整条废掉。"""
        tr = osc()
        cyc, _ = dm.find_cycles(tr)
        self.assertGreater(len(cyc), 100)
        per = sorted(c.period for c in cyc)
        med = per[len(per) // 2]
        self.assertAlmostEqual(med, 1.0 / 5.03e9, delta=0.002 / 5.03e9)
        # 真值是常数频率：插值之后周期的离散程度必须远小于一个栅格步长
        spread = per[int(0.9 * len(per))] - per[int(0.1 * len(per))]
        self.assertLess(spread, 1e-11, "周期被栅格量化了（插值没生效）")

    def test_transition_is_not_counted_as_a_cycle(self):
        """直流台阶穿过中线一次，会跟下一个真周期凑出一个假『周期』。

        真实后果：残差 100%、中位载频被拉到 4.7 MHz、代表周期挑错。
        """
        tr = osc()
        cyc, _ = dm.find_cycles(tr)
        per = sorted(c.period for c in cyc)
        self.assertLess(per[-1] / per[0], dm.PERIOD_OUTLIER ** 2,
                        "混进了过渡段，不是载波周期")
        self.assertLess(max(c.resid for c in cyc), 0.2,
                        "残差爆表说明有假周期")

    def test_residual_flags_a_non_sine(self):
        """方波不是正弦——解调的前提不成立时必须报出来，不能假设。"""
        rows = ["time (s),V(o) (V)"]
        t = 0.0
        while t < 4e-7:
            rows.append("%.12g,%.9g"
                        % (t, 0.5 if math.sin(2 * math.pi * 5.03e9 * t) > 0
                           else -0.5))
            t += 1e-11
        tr = core.parse_csv("<t>", text="\n".join(rows) + "\n")[0]
        core.analyze(tr, kind="tran")
        cyc, _ = dm.find_cycles(tr)
        self.assertTrue(cyc)
        self.assertGreater(max(c.resid for c in cyc), 0.2,
                           "方波的残差应当很大 —— 它不像正弦")


class TestDemod(unittest.TestCase):

    def test_two_blocks_envelope_full_range_freq_only_where_oscillating(self):
        tr = osc()
        out, cyc = dm.demod(tr)
        self.assertEqual(len(out), 2)
        env, frq = out
        self.assertEqual([s.name.split("(")[0] for s in env.signals],
                         ["env_hi", "env_lo"])
        # 包络覆盖整条时间轴：起振前那段直流台阶不能丢
        self.assertLess(env.x[0], 5e-8)
        self.assertGreater(env.x[-1], 3.5e-7)
        # 频率只覆盖振荡区间，否则量程被 0 Hz 撑成 5 GHz，牵引就看不见了
        self.assertGreater(frq.x[0], 9e-8)
        for v in frq.signals[0].y:
            self.assertGreater(v, 1e9)

    def test_envelope_keeps_the_dc_step(self):
        """起振前的直流台阶是真实文件里的关键情节，不许被解调吃掉。"""
        tr = osc()
        env = dm.demod(tr)[0][0]
        core.analyze(env, kind="tran")
        hi = env.signals[0]
        before = [hi.y[i] for i in range(len(env.x)) if env.x[i] < 4e-8]
        after = [hi.y[i] for i in range(len(env.x))
                 if 6e-8 < env.x[i] < 9e-8]
        self.assertTrue(before and after)
        self.assertAlmostEqual(sum(before) / len(before), 0.008, delta=2e-3)
        self.assertAlmostEqual(sum(after) / len(after), 0.30, delta=2e-3)

    def test_dead_zone_fill_is_capped(self):
        """空档按载波周期补窗口的话，1.5 µs 死区能补出 7500 个没用的点。"""
        tr = osc(t_start=3e-7, t_end=4e-7)
        env = dm.demod(tr)[0][0]
        self.assertLess(len(env.x), 3 * dm.MAX_FILL + 2000)

    def test_frequency_is_averaged_and_says_so(self):
        tr = osc(t_end=1e-6)
        out, _ = dm.demod(tr)
        frq = out[1]
        self.assertLessEqual(len(frq.x), dm.FREQ_MAX_PTS)
        self.assertTrue(any("跨" in n and "周期" in n for n in frq.notes),
                        "降噪方式必须声明，不能偷偷平滑")

    def test_refuses_when_not_enough_cycles(self):
        tr = osc(t_end=4e-9)                       # 只有二十来个周期
        out, cyc = dm.demod(tr, min_cycles=200)
        self.assertEqual(out, [])


class TestRepresentativeCycles(unittest.TestCase):

    def test_picks_extremes_not_a_uniform_sample(self):
        """均匀撒会漏掉唯一有价值的那几个周期。同一条规则在频域叫 spur 强制保留。"""
        tr = osc(t_end=1e-6, squeg=0.35)
        cyc, _ = dm.find_cycles(tr)
        picks = dm.pick_representative(cyc, 6)
        why = " ".join(p.why for p in picks)
        self.assertIn("包络最大", why)
        self.assertIn("包络最小", why)   # 谷底
        self.assertIn("残差最大", why)
        big = max(cyc, key=lambda c: c.amp)
        self.assertTrue(any(p is big for p in picks), "包络最大的那个必须在")

    def test_cycles_block_is_self_contained(self):
        """这一段必须自带列声明：上面那些列是派生量，量程不同，
        词头和 offset 可能不一样，套错就是一个数量级。"""
        tr = osc(t_end=1e-6)
        cyc, _ = dm.find_cycles(tr)
        picks = dm.pick_representative(cyc, 3)
        core.set_eps(tr, 0.005)
        cs = core.make_colspec(tr, 0.005)[0]
        cs.label = "c_raw"
        blk = dm.cycles_block(tr, 0, picks, cs, "s")
        self.assertTrue(blk[0].startswith("[CYCLES]"))
        decl = [b for b in blk if b.startswith("# c_raw")]
        self.assertTrue(decl, "没有列声明:\n" + "\n".join(blk[:5]))
        self.assertIn("量化", decl[0])
        self.assertIn("offset", decl[0])
        # 每个代表周期都要说清为什么被选中
        heads = [b for b in blk if b.startswith("# @")]
        self.assertEqual(len(heads), len(picks))
        for h in heads:
            self.assertIn("残差", h)

    def test_cycle_rows_are_raw_samples(self):
        tr = osc(t_end=1e-6)
        cyc, _ = dm.find_cycles(tr)
        picks = dm.pick_representative(cyc, 1)
        core.set_eps(tr, 0.005)
        cs = core.make_colspec(tr, 0.005)[0]
        blk = dm.cycles_block(tr, 0, picks, cs, "s")
        rows = [b for b in blk if b and b[0].isdigit()]
        self.assertEqual(len(rows), picks[0].i1 - picks[0].i0,
                         "代表周期必须是原始样点，不许抽点")


class TestOneDocument(unittest.TestCase):
    """一键复制是硬要求：四件东西必须在**同一份** .wv 里。"""

    def test_everything_lands_in_one_text(self):
        import tempfile
        import os
        rows = ["time (s),V(o) (V)"]
        t = 0.0
        while t < 4e-7:
            a = 0.28 * (1 - math.exp(-(t - 1e-7) / 2e-8)) if t > 1e-7 else 0.0
            rows.append("%.12g,%.9g"
                        % (t, 0.3 + a * math.sin(2 * math.pi * 5.03e9 * t)))
            t += 1e-11
        fd, p = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            with open(p, "w", newline="\n") as fh:
                fh.write("\n".join(rows) + "\n")
            rc, txt = C.run_cli([p, "--demod", "--budget", "51200"])
            self.assertEqual(rc, 0)
            self.assertEqual(txt.count("[SHAPE]"), 2, "包络块 + 频率块")
            self.assertEqual(txt.count("[CYCLES]"), 1)
            self.assertIn("[METRICS]", txt)
            self.assertIn("解调：", txt)
            self.assertLessEqual(len(txt.encode("utf-8")), 51200)
        finally:
            os.unlink(p)

    def test_demod_beats_the_polyline_on_the_same_budget(self):
        """同样预算下，解调必须既更小又更准 —— 否则这个功能没有存在理由。"""
        import tempfile
        import os
        rows = ["time (s),V(o) (V)"]
        t = 0.0
        while t < 4e-7:
            a = 0.28 * (1 - math.exp(-(t - 1e-7) / 2e-8)) if t > 1e-7 else 0.0
            rows.append("%.12g,%.9g"
                        % (t, 0.3 + a * math.sin(2 * math.pi * 5.03e9 * t)))
            t += 1e-11
        fd, p = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            with open(p, "w", newline="\n") as fh:
                fh.write("\n".join(rows) + "\n")
            _, plain = C.run_cli([p, "--budget", "51200"])
            _, demod = C.run_cli([p, "--demod", "--budget", "51200"])
            self.assertLess(len(demod.encode("utf-8")),
                            len(plain.encode("utf-8")))
            # 折线在这个预算下必然报出「点/周期不够」，解调不该报
            self.assertIn("点/周期", plain)
            self.assertNotIn("点/周期", demod)
        finally:
            os.unlink(p)


if __name__ == "__main__":
    unittest.main()
