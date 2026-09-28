# -*- coding: utf-8 -*-
"""wave_replay 的测试。

主心骨是 **误差上界是逐点的**：每一个原始点离输出的 PWL 都不超过 tol。
检查用 `naive_err` —— 每点二分找段、线性插值，故意不复用 `Cone` 的任何东西，
和被测代码没有共同的错误。

其余守的是「接到仿真里不出错」的那几件事：名字转义、时间严格递增、
平台加速和逐点处理结果一致、跳过的信号是注释而不是消失。
"""

import bisect
import io
import math
import os
import random
import shutil
import sys
import tempfile
import unittest

import _common  # noqa: F401  （把 tools/ 放进 sys.path）
import wave_replay as R


def naive_err(raw, pts):
    ts = [p[0] for p in pts]
    worst = 0.0
    for t, y in raw:
        i = bisect.bisect_right(ts, t) - 1
        if i < 0:
            v = pts[0][1]
        elif i >= len(pts) - 1:
            v = pts[-1][1]
        else:
            (t0, y0), (t1, y1) = pts[i], pts[i + 1]
            v = y0 + (y1 - y0) * (t - t0) / (t1 - t0)
        worst = max(worst, abs(v - y))
    return worst


def run_cone(raw, tol):
    c = R.Cone(tol)
    for t, y in raw:
        c.add(t, y)
    return c, c.finish()


def waves():
    rnd = random.Random(7)
    n = 4000
    ts = [i * 1e-9 + rnd.random() * 1e-10 for i in range(n)]
    out = {}
    out["sine"] = [(t, 0.4 * math.sin(2 * math.pi * 3e6 * t)) for t in ts]
    out["noisy"] = [(t, 0.9 + rnd.gauss(0, 2e-3)) for t in ts]
    walk, y = [], 0.0
    for t in ts:
        y += rnd.gauss(0, 5e-3)
        walk.append((t, y))
    out["walk"] = walk
    # 数字位：平台 + 两个采样点宽的边沿，外加一个亚 tol 的毛刺
    dig = []
    for i, t in enumerate(ts):
        lvl = 0.8 if (i // 500) % 2 else 0.0
        if i % 500 == 0 and i:
            lvl = 0.4
        if i == 1234:
            lvl += 3e-4
        dig.append((t, lvl))
    out["digital"] = dig
    # 电源上电：斜坡 + 过冲 + 振铃 + 稳住
    sup = []
    for t in ts:
        u = t / ts[-1]
        v = 0.0 if u < 0.1 else min(1.0, (u - 0.1) * 20) * 0.8
        if u > 0.15:
            v += 0.05 * math.exp(-(u - 0.15) * 60) * math.sin((u - 0.15) * 300)
        sup.append((t, v))
    out["supply"] = sup
    return out


class TestConeBound(unittest.TestCase):

    def test_every_sample_within_tol(self):
        for tol in (1e-3, 1e-4, 1e-2):
            for name, raw in waves().items():
                c, pts = run_cone(raw, tol)
                e = naive_err(raw, pts)
                self.assertLessEqual(e, tol * (1 + 1e-9),
                                     "%s tol=%g err=%g (%d pts)" % (name, tol, e, len(pts)))

    def test_compresses(self):
        """误差限内应当压得动：平台信号只剩边沿附近几个点。"""
        w = waves()
        _, pts = run_cone(w["digital"], 1e-3)
        self.assertLess(len(pts), 60)           # 7 个边沿 × 每个至多 ~6 个顶点
        _, pts = run_cone(w["supply"], 1e-3)
        self.assertLess(len(pts), 400)
        _, pts = run_cone(w["noisy"], 1e-3)     # 噪声 2 mV > tol：压不动是对的
        self.assertGreater(len(pts), 500)

    def test_times_strictly_increase(self):
        for raw in waves().values():
            _, pts = run_cone(raw, 1e-3)
            ts = [R.fmt_t(p[0]) for p in pts]
            self.assertEqual(len(set(ts)), len(ts))
            self.assertEqual([p[0] for p in pts], sorted(p[0] for p in pts))
            self.assertEqual(pts[0][0], raw[0][0])
            self.assertEqual(pts[-1][0], raw[-1][0])

    def test_constant_is_flat(self):
        raw = [(i * 1e-9, 1.8) for i in range(1000)]
        c, pts = run_cone(raw, 1e-3)
        self.assertTrue(c.is_flat())
        self.assertEqual(pts, [(0.0, 1.8), (999e-9, 1.8)])

    def test_duplicate_time_dropped(self):
        raw = [(0.0, 0.0), (1e-9, 0.0), (1e-9, 0.5), (2e-9, 0.5), (3e-9, 0.5)]
        c, pts = run_cone(raw, 1e-3)
        self.assertEqual(c.dup, 1)
        ts = [p[0] for p in pts]
        self.assertEqual(ts, sorted(set(ts)))

    def test_plateau_shortcut_matches_plain_path(self):
        """`stream` 的字符串相同捷径 和 逐点 add 必须给出同一条 PWL。"""
        w = waves()
        for name in ("digital", "supply"):
            ints = [(round(t * 1e21), y) for t, y in w[name]]
            raw = [(k * 1e-21, y) for k, y in ints]     # 两条路吃同一组时间
            _, plain = run_cone(raw, 1e-3)
            text = ["SimTime,x"] + ["%d,%r" % (k, y) for k, y in ints]
            tg = R.Target("/x")
            tg.status, tg.col = "ok", 1
            R.stream(iter(text[1:]), [tg], 1e-21, 1e-3)
            fast = tg.cone.finish()
            self.assertEqual(len(fast), len(plain), name)
            for a, b in zip(fast, plain):
                self.assertAlmostEqual(a[0], b[0], delta=1e-18)
                self.assertAlmostEqual(a[1], b[1], delta=1e-12)
            self.assertLessEqual(naive_err(raw, fast), 1e-3 * (1 + 1e-9))


class TestNames(unittest.TestCase):

    def test_norm_key_aligns_three_spellings(self):
        ade = R.norm_key("/I_top/d_bus<13>")
        csvh = R.norm_key("sim_tb.I_top.\\d_bus<13> ".replace(".", "/"))
        self.assertEqual(ade, "I_top/d_bus[13]")
        self.assertTrue(csvh.endswith(ade))

    def test_spectre_node_escapes(self):
        self.assertEqual(R.spectre_node(["I_top", "d_n_ls<3>"]),
                         "I_top.d_n_ls\\<3\\>")
        self.assertEqual(R.spectre_node(["VDD"]), "VDD")
        self.assertEqual(R.spectre_node(["I_a", "x"], prefix="I_tb"), "I_tb.I_a.x")

    def test_read_ade_outputs_csv(self):
        text = ("Test,Name,Type,Output,Plot,Save,Spec\n"
                "sim_a,,net,/VDD_PG,,t,\n"
                "sim_a,,net,/I_top/d_x<1>,,t,\n"
                "sim_a,,terminal,/I_top/V0/PLUS,,t,\n"
                "sim_a,,net,/VDD_PG,,t,\n")
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "l.csv")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(text)
            tg = R.read_targets(p)
        finally:
            shutil.rmtree(d)
        self.assertEqual([t.path for t in tg], ["/VDD_PG", "/I_top/d_x<1>", "/I_top/V0/PLUS"])
        self.assertEqual(tg[2].kind, "terminal")


HEADER = """$timescale
    1e-06fs
$end
$scope module sim_TOP $end
$var real 64 ! VDD_PG $end
$scope module I_top $end
$var real 64 " d_x<1> $end
$var real 64 # d_x<0> $end
$var real 64 $ en $end
$scope module I_dig $end
$var wire 1 % en $end
$var wire 2 & d_x [1:0] $end
$upscope $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
"""


class TestHeader(unittest.TestCase):

    def test_parse_and_resolve(self):
        ts, vs = R.parse_vcd_header(HEADER)
        self.assertAlmostEqual(ts, 1e-21)
        tg = [R.Target(p) for p in ("/VDD_PG", "/I_top/d_x<1>", "/I_top/en",
                                    "/I_top/I_dig/en", "/I_top/nope")]
        tg.append(R.Target("/I_top/V0/PLUS", "terminal"))
        R.resolve(tg, vs)
        st = [(t.status, t.db) for t in tg]
        self.assertEqual(st[0], ("ok", "sim_TOP.VDD_PG"))
        self.assertEqual(st[1], ("ok", "sim_TOP.I_top.d_x<1>"))
        self.assertEqual(st[2], ("ok", "sim_TOP.I_top.en"))     # real 优先于同名 wire
        self.assertEqual(st[3][0], "logic")
        self.assertEqual(st[4][0], "missing")
        self.assertEqual(st[5][0], "unsupported")


class TestBindColumns(unittest.TestCase):
    """CSV 表头写到哪一层不固定。回归：公司机上见过只有顶层 4 个对上、
    层级里的 256 个全部 nocolumn —— 表头只剩叶子名，按全路径找不到。"""

    DB = ["sim_TOP.VDD_PG", "sim_TOP.I_top.en", "sim_TOP.I_top.d_x<1>"]

    def _targets(self):
        tg = [R.Target(p) for p in ("/VDD_PG", "/I_top/en", "/I_top/d_x<1>")]
        for t, d in zip(tg, self.DB):
            t.status, t.db = "ok", d
        return tg

    def test_leaf_only_header_binds_by_position(self):
        tg = self._targets()
        pos = R.bind_columns(["SimTime", "VDD_PG", "en", "\\d_x<1> "], tg, self.DB)
        self.assertTrue(pos)
        self.assertEqual([t.col for t in tg], [1, 2, 3])

    def test_full_header_binds_by_position(self):
        tg = self._targets()
        pos = R.bind_columns(["SimTime"] + self.DB, tg, self.DB)
        self.assertTrue(pos)
        self.assertEqual([t.col for t in tg], [1, 2, 3])

    def test_leaf_only_header_by_name_when_counts_differ(self):
        """列数对不上就不能信位置，退回按名字 —— 叶子名作后缀照样能认。"""
        tg = self._targets()
        pos = R.bind_columns(["SimTime", "en", "VDD_PG", "\\d_x<1> ", "extra"], tg, self.DB)
        self.assertFalse(pos)
        self.assertEqual([t.col for t in tg], [2, 1, 3])

    def test_position_rejected_when_names_disagree(self):
        tg = self._targets()
        pos = R.bind_columns(["SimTime", "en", "VDD_PG", "d_x<1>"], tg, self.DB)
        self.assertFalse(pos)                   # 顺序反了：位置不可信
        self.assertEqual([t.col for t in tg], [2, 1, 3])

    def test_ambiguous_leaf_is_nocolumn_not_a_guess(self):
        tg = [R.Target("/I_a/en"), R.Target("/I_b/en")]
        for t, d in zip(tg, ("top.I_a.en", "top.I_b.en")):
            t.status, t.db = "ok", d
        R.bind_columns(["SimTime", "en", "en", "x"], tg, ["top.I_a.en", "top.I_b.en"])
        self.assertEqual([t.status for t in tg], ["nocolumn", "nocolumn"])


class TestEndToEnd(unittest.TestCase):

    def setUp(self):
        self.d = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.d)

    def _w(self, name, text):
        p = os.path.join(self.d, name)
        with open(p, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        return p

    def _case(self):
        lst = self._w("list.csv", "Test,Name,Type,Output,Plot,Save,Spec\n"
                      "s,,net,/VDD_PG,,t,\n"
                      "s,,net,/I_top/d_x<1>,,t,\n"
                      "s,,net,/I_top/en,,t,\n"
                      "s,,net,/I_top/nope,,t,\n")
        hdr = self._w("h.vcd", HEADER)
        # simvisdbutil 的样子：整数时间、转义列名、尾空格
        rows = ["SimTime,sim_TOP.VDD_PG,sim_TOP.I_top.\\d_x<1> ,sim_TOP.I_top.en"]
        n = 3000
        for i in range(n):
            t = i * 10**12                      # 1 ns / 步，单位 1e-21 s
            pg = 0.0 if i < 1000 else 1.8
            dx = 0.8 * (0.5 + 0.5 * math.sin(i / 50.0))
            rows.append("%d,%r,%r,%r" % (t, pg, dx, 0.8))
        csvp = self._w("x.csv", "\n".join(rows) + "\n")
        return lst, hdr, csvp

    def _run(self, *extra):
        lst, hdr, csvp = self._case()
        out = os.path.join(self.d, "out")
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
        try:
            rc = R.main(["--list", lst, "--csv", csvp, "--header", hdr, "-o", out]
                        + list(extra))
            msg = sys.stdout.getvalue() + sys.stderr.getvalue()
        finally:
            sys.stdout, sys.stderr = old
        with open(os.path.join(out, "replay.scs"), encoding="utf-8") as fh:
            scs = fh.read()
        return rc, scs, out, msg

    def test_scs_shape(self):
        rc, scs, out, msg = self._run()
        self.assertEqual(rc, 1, msg)            # 有一个 missing，要让人看见
        self.assertIn("simulator lang=spectre", scs)
        self.assertIn("V_rp_0003 (I_top.en 0) vsource dc=0.8", scs)
        self.assertRegex(scs, r"V_rp_0001 \(VDD_PG 0\) vsource type=pwl wave=\[0 0 ")
        self.assertRegex(scs, r'V_rp_0002 \(I_top.d_x\\<1\\> 0\) vsource type=pwl file="[^"]+\.pwl"')
        self.assertIn("// V_rp_0004  /I_top/nope  -- missing", scs)
        self.assertTrue(os.path.exists(os.path.join(out, "REPORT.txt")))

    def test_pwl_file_within_tol(self):
        rc, scs, out, _ = self._run("--tol", "0.5m")
        pdir = os.path.join(out, "pwl")
        f = [x for x in os.listdir(pdir) if x.startswith("0002_")][0]
        with open(os.path.join(pdir, f)) as fh:
            pts = [tuple(map(float, ln.split())) for ln in fh]
        raw = [(i * 1e-9, 0.8 * (0.5 + 0.5 * math.sin(i / 50.0))) for i in range(3000)]
        self.assertLessEqual(naive_err(raw, pts), 0.5e-3 * (1 + 1e-9))
        self.assertLess(len(pts), 600)

    def test_window_and_shift(self):
        rc, scs, out, _ = self._run("--from", "900n", "--to", "1.2u")
        m = [ln for ln in scs.splitlines() if ln.startswith("V_rp_0001")][0]
        self.assertIn("wave=[0 0 ", m)          # 900 ns 挪到了 0
        self.assertIn("recorded t=9e-07 s", scs)

    def test_skip_is_comment_not_gone(self):
        rc, scs, out, _ = self._run("--skip", "/I_top/*")
        self.assertIn("// V_rp_0003 (I_top.en 0) vsource dc=0.8", scs)
        self.assertRegex(scs, r"\nV_rp_0001 ")

    def test_series_resistor(self):
        rc, scs, out, _ = self._run("--rs", "10")
        self.assertIn("V_rp_0003 (rp_0003 0) vsource dc=0.8", scs)
        self.assertIn("R_rp_0003 (rp_0003 I_top.en) resistor r=10", scs)


if __name__ == "__main__":
    unittest.main()
