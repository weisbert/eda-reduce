# -*- coding: utf-8 -*-
"""GUI 的无人值守自检。

默认**跳过**：它会真的弹一个窗口出来，routine 跑测试时不该被打断。
要跑就设环境变量：

    EDA_REDUCE_GUI_TEST=1 python -m unittest discover -s tests

自检本身有硬超时，而且接管了 Tk 的异常钩子 —— Tk 默认把回调异常打到 stderr
然后继续跑 mainloop，撞上就永远不退出（第一版就是这么挂住的）。
"""

import os
import re
import subprocess
import sys
import time
import unittest

from _common import ROOT

RUN = os.environ.get("EDA_REDUCE_GUI_TEST") == "1"


@unittest.skipUnless(RUN, "会弹窗；设 EDA_REDUCE_GUI_TEST=1 才跑")
class TestGuiSelftest(unittest.TestCase):

    def test_selftest_runs_and_exits(self):
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "wave_reduce.py"),
             os.path.join(ROOT, "examples", "demo_tran.csv"),
             "--gui", "--selftest"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        out = p.stdout.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, out)
        self.assertNotIn("TIMEOUT", out)
        self.assertNotIn("EXC", out)
        self.assertIn("载入完成", out)
        # 滑块拖动要真的重算：四档 max_points 应当给出不同的点数/字节数
        rows = [ln for ln in out.splitlines() if ln.startswith("max_points=")]
        self.assertGreaterEqual(len(rows), 4)
        got = set(rows)
        self.assertEqual(len(got), len(rows), "不同的 max_points 给了一样的结果")
        self.assertIn("框选缩放", out)
        self.assertIn("状态栏", out)

    def test_budget_is_editable_and_matches_cli(self):
        """预算在 GUI 里必须能改，而且「一键压到预算」要和命令行给同一个结果。

        两边给不出同一个数的话，GUI 里调好的参数拿到命令行就不作数了 ——
        这个工具存在的理由（在数据所在的机器上把参数调好）就没了。
        """
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "wave_reduce.py"),
             os.path.join(ROOT, "examples", "demo_tran.csv"),
             "--gui", "--selftest"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        out = p.stdout.decode("utf-8", "replace")
        rows = {}
        for ln in out.splitlines():
            if ln.startswith("预算 ") and "->" in ln and "kept" in ln:
                kb = ln.split()[1]
                rows[kb] = (int(ln.split("kept")[1].split()[0]),
                            int(ln.split("bytes")[1].split()[0]))
        self.assertIn("20", rows, out)
        self.assertIn("40", rows, "预算能改大")
        self.assertIn("6", rows, "预算能改小")
        self.assertGreater(rows["40"][0], rows["20"][0], "预算大了点数该多")
        self.assertLess(rows["6"][0], rows["20"][0], "预算小了点数该少")

        import _common as CC
        _, txt = CC.run_cli([CC.ex("demo_tran.csv"), "--budget", "20480"])
        self.assertEqual(rows["20"][1], len(txt.encode("utf-8")),
                         "GUI 压到 20 KB 和命令行 --budget 20480 结果不一致")

        # 同一个预算从不同起点压必须落在同一个结果上。自检里 20 KB 压了两次，
        # 中间去 40 KB 和 6 KB 绕了一圈；第二次的结果就是剪贴板里那份。
        clip = [ln for ln in out.splitlines() if ln.startswith("剪贴板: ")]
        self.assertTrue(clip, out)
        n = int(clip[0].split("剪贴板: ")[1].split()[0])
        self.assertEqual(n, rows["20"][1],
                         "同一个预算从不同起点压出了不同结果（%d vs %d）"
                         % (n, rows["20"][1]))

    def test_clipboard_holds_the_whole_wv(self):
        """.wv 的归宿就是聊天框 —— 剪贴板里必须是完整可粘的那一份。"""
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "wave_reduce.py"),
             os.path.join(ROOT, "examples", "demo_tran.csv"),
             "--gui", "--selftest"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        out = p.stdout.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("剪贴板内容 == 当前 .wv: True", out,
                      "剪贴板里的不是当前这份 .wv\n" + out)
        head = [ln for ln in out.splitlines() if ln.startswith("剪贴板: ")]
        self.assertIn("# WV1", head[0], "第一行得是 .wv 头，不是 METRICS 片段")
        # 下窗格两种视图都要有：完整视图是剪贴板挂了时的手动兜底
        v = [ln for ln in out.splitlines() if ln.startswith("下窗格 ")]
        self.assertTrue(v, out)
        met, full = (int(x) for x in re.findall(r"(\d+) 行", v[0]))
        self.assertGreater(full, met * 5, "完整 .wv 视图应当远长于 METRICS")

    def test_opens_without_a_file(self):
        """开窗和选文件是两件事，不给文件不该被一个对话框卡死。"""
        import tkinter as tk
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import wave_cli
        import wave_gui
        args = wave_cli.build_parser().parse_args([])
        args.budget = 20480
        root = tk.Tk()
        try:
            app = wave_gui.WaveGui(root, None, args)
            root.update_idletasks()
            self.assertIn("还没打开文件", app.status.get())
            self.assertEqual(app.wv_text(), "", "没数据时不该有 .wv")
            # 空状态下点这些按钮都不许炸
            app._copy()
            app._fill_text()
            app._fit()
            app._redraw()
            app._zoom_all()
            app._on_budget()
            root.update_idletasks()
            # 然后再打开文件，一切正常
            app._load_async(os.path.join(ROOT, "examples", "demo_ac.csv"))
            for _ in range(300):
                root.update()
                if app.red:
                    break
                time.sleep(0.02)
            self.assertIsNotNone(app.red, "打开文件后应当算出结果")
            self.assertIn("# WV1", app.wv_text())
            self.assertEqual(len(app.colbox.winfo_children()),
                             len(app.traces[0].signals))
            # 再换一个文件：列选框不许越积越多
            app._load_async(os.path.join(ROOT, "examples", "demo_tran.csv"))
            for _ in range(300):
                root.update()
                if app.red:
                    break
                time.sleep(0.02)
            self.assertEqual(len(app.colbox.winfo_children()), 4,
                             "换文件要把上一份的列选框清掉")
        finally:
            root.destroy()

    def test_canvas_segment_budget(self):
        """Canvas 上的图元数要恒定 —— 拖动时才不掉帧。"""
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "wave_reduce.py"),
             os.path.join(ROOT, "examples", "demo_tran.csv"),
             "--gui", "--selftest"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        out = p.stdout.decode("utf-8", "replace")
        counts = []
        for ln in out.splitlines():
            if "canvas items" in ln:
                counts.append(ln.split("canvas items")[1].strip())
        self.assertTrue(counts)
        self.assertEqual(len(set(counts)), 1, "图元数应当恒定: %s" % counts)


class TestGuiPureCompute(unittest.TestCase):
    """不碰 Tk 的那部分可以随时测：像素分箱、重建取值、坐标变换。"""

    def setUp(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))

    def test_envelope_binning(self):
        import wave_gui
        x = [i * 1e-9 for i in range(1000)]
        y = [(-1.0 if i == 500 else 0.0) for i in range(1000)]
        band = wave_gui.bin_envelope(x, [y], 0, 1000, 100)
        lo, hi = band[0]
        self.assertEqual(len(lo), 100)
        # 那个孤立的坑必须出现在某一列的下包络里 —— 抽样会漏掉它，包络不会
        self.assertAlmostEqual(min(v for v in lo if v is not None), -1.0)
        self.assertAlmostEqual(max(v for v in hi if v is not None), 0.0)

    def test_recon_lookup(self):
        import wave_gui
        kx = [0.0, 1.0, 2.0]
        ky = [0.0, 10.0, 20.0]
        self.assertAlmostEqual(wave_gui.recon_at(kx, ky, 0.5), 5.0)
        self.assertAlmostEqual(wave_gui.recon_at(kx, ky, 1.5), 15.0)
        self.assertAlmostEqual(wave_gui.recon_at(kx, ky, -1.0), 0.0)
        self.assertAlmostEqual(wave_gui.recon_at(kx, ky, 9.0), 20.0)

    def test_xform_log(self):
        import wave_gui
        f = wave_gui.Xform(10.0, 1e10, 0.0, 1.0, 1000, 200, logx=True)
        a, b = f.sx(10.0), f.sx(1e10)
        self.assertLess(a, b)
        mid = f.sx(1e5.__float__() ** 1)          # 10^5.5 的一半位置附近
        self.assertGreater(mid, a)
        self.assertLess(mid, b)
        self.assertAlmostEqual(f.wx(a), 10.0, delta=1e-6)


if __name__ == "__main__":
    unittest.main()
