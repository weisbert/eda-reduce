# -*- coding: utf-8 -*-
"""回归基线：改了工具之后 diff `examples/demo_*.wv`，和 `examples/demo.rd` 一个路子。

基线**变了不一定是错**——改进了输出当然会变。这个测试的作用是让变化
无法悄悄发生：每次变都得看一眼 diff，确认那是你想要的变化。

更新基线（确认 diff 是你想要的之后）：

    python tools/wave_reduce.py examples/demo_tran.csv -o examples/demo_tran.wv
    python tools/wave_reduce.py examples/demo_ac.csv   -o examples/demo_ac.wv
    python tools/wave_reduce.py examples/demo_spec.csv -o examples/demo_spec.wv

样例 CSV 本身也是确定性生成的，改了生成器要一起重来：

    python examples/gen_demo_wave.py
    python examples/gen_demo_plot.py
"""

import difflib
import os
import unittest

import _common as C
from _common import ROOT

CASES = ["demo_tran", "demo_ac", "demo_spec"]


class TestBaselines(unittest.TestCase):

    def _one(self, name):
        base = os.path.join(ROOT, "examples", name + ".wv")
        self.assertTrue(os.path.exists(base), "基线不在: " + base)
        with open(base, encoding="utf-8") as fh:
            want = fh.read()
        _, got = C.run_cli([C.ex(name + ".csv")])
        if got != want:
            d = "\n".join(list(difflib.unified_diff(
                want.splitlines(), got.splitlines(),
                "基线 " + name + ".wv", "现在", lineterm=""))[:40])
            self.fail("%s 和基线不一致。确认这个 diff 是你想要的变化之后，"
                      "按 tests/test_regression.py 顶上的命令更新基线：\n%s"
                      % (name, d))

    def test_tran(self):
        self._one("demo_tran")

    def test_ac(self):
        self._one("demo_ac")

    def test_spec(self):
        self._one("demo_spec")

    def test_deterministic(self):
        """同一份输入跑两遍必须逐字节一致，否则基线机制本身就不成立。"""
        for n in CASES:
            _, a = C.run_cli([C.ex(n + ".csv")])
            _, b = C.run_cli([C.ex(n + ".csv")])
            self.assertEqual(a, b, n + " 两次跑出来不一样")

    def test_lf_line_endings(self):
        """仓库 .gitattributes 是 eol=lf，基线里不许有 CRLF。"""
        for n in CASES:
            p = os.path.join(ROOT, "examples", n + ".wv")
            with open(p, "rb") as fh:
                self.assertNotIn(b"\r", fh.read(), n + ".wv 有 CRLF")


class TestGeneratorDeterminism(unittest.TestCase):
    """样例 CSV 是确定性生成的 —— 不然基线在别的机器上就对不上。"""

    def test_regenerating_gives_identical_csv(self):
        import hashlib
        import subprocess
        import sys
        import tempfile
        d = tempfile.mkdtemp(prefix="gendemo")
        subprocess.check_call(
            [sys.executable, os.path.join(ROOT, "examples", "gen_demo_wave.py"),
             "-o", d], stdout=subprocess.DEVNULL)

        def h(p):
            with open(p, "rb") as fh:
                return hashlib.sha256(fh.read()).hexdigest()

        for f in ("demo_tran.csv", "demo_ac.csv", "demo_spec.csv",
                  "demo_dirty.csv", "demo_tran_layoutb.csv"):
            self.assertEqual(h(os.path.join(d, f)),
                             h(os.path.join(ROOT, "examples", f)),
                             f + " 重新生成的和仓库里的不一样")


if __name__ == "__main__":
    unittest.main()
