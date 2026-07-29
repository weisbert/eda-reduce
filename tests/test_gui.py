# -*- coding: utf-8 -*-
"""GUI 的无人值守自检。

默认**跳过**：它会真的弹一个窗口出来，routine 跑测试时不该被打断。
要跑就设环境变量：

    EDA_REDUCE_GUI_TEST=1 python -m unittest discover -s tests

自检本身有硬超时，而且接管了 Tk 的异常钩子 —— Tk 默认把回调异常打到 stderr
然后继续跑 mainloop，撞上就永远不退出（第一版就是这么挂住的）。
"""

import math
import os
import re
import subprocess
import sys
import time
import unittest

import _common as C
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

    def test_zoom_reported_in_selftest(self):
        """缩放/平移/纵轴自适应都要在无人值守自检里留下痕迹。"""
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "wave_reduce.py"),
             os.path.join(ROOT, "examples", "demo_tran.csv"),
             "--gui", "--selftest"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        out = p.stdout.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, out)
        self.assertIn("滚轮缩放", out)
        self.assertIn("平移", out)
        self.assertIn("按 0 复位: 视窗 == 全长 True", out, "键盘复位没生效")
        # 深缩放必须自己停住，而不是缩到视窗里一个原始点都不剩
        deep = [ln for ln in out.splitlines() if ln.startswith("深缩放到底")]
        self.assertTrue(deep, out)
        n_pts = int(re.search(r"原始点 (\d+)", deep[0]).group(1))
        self.assertGreaterEqual(n_pts, 4, "缩到没有原始点了")
        # 窄视窗上纵轴必须真的放大，否则「放大局部」只放大了横轴
        y = [ln for ln in out.splitlines() if ln.startswith("纵轴@窄视窗")]
        self.assertTrue(y, out)
        ratio = float(re.search(r"\(([\d.]+)x", y[0]).group(1))
        self.assertGreater(ratio, 5.0,
                           "窄视窗里纵轴还是按全局量程画的（%s）" % y[0])

    def _app(self, csv_name, w=900):
        """开一个真窗口把文件读进去。-> (root, app)，调用方负责 destroy。"""
        import tkinter as tk
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        import wave_cli
        import wave_gui
        args = wave_cli.build_parser().parse_args([])
        args.budget = 20480
        root = tk.Tk()
        app = wave_gui.WaveGui(root, os.path.join(ROOT, "examples", csv_name),
                               args)
        app.c_wave.configure(width=w, height=330)
        for _ in range(400):
            root.update()
            if app.red:
                break
            time.sleep(0.02)
        self.assertIsNotNone(app.red, "文件没读进来")
        # 载入后会自动压一次到预算，那一步在后台线程里。等它落定再交给
        # 测试 —— 否则测的是一个中间态（实测「屏幕字节数 != 剪贴板字节数」
        # 就是这么来的）。
        for _ in range(600):
            root.update()
            if not app._fitting:
                break
            time.sleep(0.02)
        self.assertFalse(app._fitting, "自动压到预算没落定")
        root.update()
        return root, app

    def test_zoom_is_anchored_and_bounded(self):
        """指针底下那个点缩放前后必须还在指针底下 —— 否则每滚一格目标就跑掉。"""
        import wave_gui
        root, app = self._app("demo_tran.csv")
        try:
            w = app.c_wave.winfo_width()
            px = int(w * 0.7)
            anchor = app._xform(app.c_wave).wx(px)
            span0 = app.view[1] - app.view[0]
            app._zoom_at(app.c_wave, px, wave_gui.ZOOM_STEP)
            span1 = app.view[1] - app.view[0]
            self.assertAlmostEqual(span1 / span0, wave_gui.ZOOM_STEP, places=6)
            self.assertAlmostEqual(app._xform(app.c_wave).wx(px), anchor,
                                   delta=span1 * 1e-6, msg="锚点跑了")
            # 一直放大：必须自己停在「视窗里还剩几个原始点」上，而不是缩成一条缝
            for _ in range(200):
                app._zoom_at(app.c_wave, w // 2, wave_gui.ZOOM_STEP)
            i0, i1 = wave_gui.fit_view(app.traces[0].x, app.view[0],
                                       app.view[1])
            self.assertGreaterEqual(i1 - i0, wave_gui.MIN_VIEW_PTS,
                                    "缩到视窗里没有原始点了")
        finally:
            root.destroy()

    def test_gui_text_equals_cli_text_byte_for_byte(self):
        """同一组参数下，GUI 复制出去的东西必须和命令行输出**逐字节相同**。

        这是整个模型层存在的理由。原来两边各写了一条预算二分（对同一份
        数据落在不同点数上，实测差过 55 字节），而 GUI 的非当前块是复制
        那一刻拿**当前块**的参数临时压的 —— 同一份数据同一组参数，
        两边给不同的答案，用户没法知道该信哪个。

        比的是「压到预算」那个状态：命令行永远压到预算，而 GUI 落地时
        停在探索状态（点数由滑块给，超预算就超着，这是刻意的）。
        点一下「自动压到预算」两边就该一个字节都不差。
        """
        import wave_cli
        for name in ("demo_tran.csv", "demo_ac.csv"):
            root, app = self._app(name)
            try:
                app._fit()
                gui = app.wv_text()
            finally:
                root.destroy()
            args = wave_cli.build_parser().parse_args([])
            args.budget = 20480
            cli_txt, _ = wave_cli.process(os.path.join(ROOT, "examples", name),
                                          args)
            self.assertEqual(gui, cli_txt, "%s：GUI 和命令行给了不同的答案" % name)

    def test_rail_never_gets_clipped(self):
        """内容轨里的东西**永远装得下** —— 这是竖排固定宽的全部理由。

        上一版横着排，六路信号时「解调 / 只压视窗 / EVENTS」整列被窗口
        右边切掉，而且看不出被切了，人只会以为这版没有解调。
        上次的修法是把窗口调宽，没修住：信号一多、名字一长又会溢出。
        """
        import wave_gui
        root, app = self._app("demo_tran.csv")
        try:
            root.update_idletasks()
            self.assertEqual(app.rail.winfo_width(), wave_gui.RAIL_W,
                             "轨的宽度被内容撑开了")
            for g in app._rail_groups:
                self.assertLessEqual(
                    g["holder"].winfo_reqwidth(), wave_gui.RAIL_W,
                    "轨内 %s 需要的宽度超过了轨宽" % g["title"])
        finally:
            root.destroy()

    def test_view_bar_has_nothing_that_changes_the_output(self):
        """视图条里**一个改输出的控件都不许有**。

        判据就是这个：动完之后 .wv 一个字节都不许变。
        """
        root, app = self._app("demo_tran.csv")
        try:
            before = app.wv_text()
            app.y_local.set(not app.y_local.get())
            app._redraw()
            app.ev_v.set(not app.ev_v.get())
            app._redraw()
            app.low_v.set("cycles")
            app._redraw()
            app.low_v.set("err")
            app.nb.select(2)
            app._fill_text()
            root.update()
            self.assertEqual(app.wv_text(), before,
                             "视图条里混进了会改输出的控件")
        finally:
            root.destroy()

    def test_displayed_bytes_are_the_copied_bytes(self):
        """屏幕上那个字节数必须**就是**剪贴板里的字节数。

        原来屏幕报的是当前块、剪贴板里是全部块；后来还有一层：
        便宜那条计算路径 emit 出来的文本少一行 `# recon:`，
        于是合计比真正复制出去的少几十字节。
        """
        for name in ("demo_tran.csv", "demo_tran_layoutb.csv"):
            root, app = self._app(name)
            try:
                app._recompute(True)
                shown = app.nbytes
                copied = C.emit.nbytes(app.wv_text())
                self.assertEqual(shown, copied,
                                 "%s：屏幕 %d 字节，剪贴板 %d 字节"
                                 % (name, shown, copied))
            finally:
                root.destroy()

    def test_switching_block_does_not_touch_any_parameter(self):
        """「看一眼别的块」不许改变要粘出去的东西。

        原来切块会把候选集、metrics、列选、点数全推倒重来，而且回不去 ——
        看一眼的代价是把调好的参数丢掉。
        """
        root, app = self._app("demo_tran_layoutb.csv")
        try:
            if len(app.ship.blocks) < 2:
                self.skipTest("这个夹具只有一块")
            before = app.wv_text()
            snap = [(b.max_points, list(b.cols or []), b.included)
                    for b in app.ship.blocks]
            app._use_trace(1)
            app._use_trace(0)
            self.assertEqual([(b.max_points, list(b.cols or []), b.included)
                              for b in app.ship.blocks], snap, "切块改了参数")
            self.assertEqual(app.wv_text(), before, "切块改了输出")
        finally:
            root.destroy()

    def test_column_clicks_coalesce_into_one_recompute(self):
        """连点几个列选框只许算最后一次。

        真实困惑：「快速勾选几个、或者取消勾选，都在重算，速度特别慢」。
        一次精确 recompute 在多路长波形上是秒级，而且同步跑在 Tk 回调里 ——
        点四下就是四次全量重算，前三次的结果一眼都没人看见。
        """
        import wave_gui
        root, app = self._app("demo_tran.csv")
        try:
            self.assertGreaterEqual(len(app.cols), 3, "夹具得是多列的")
            calls = []
            real = app._recompute
            app._recompute = lambda precise=True: (calls.append(precise),
                                                   real(precise))[1]
            for k in range(3):                  # 「快速勾选几个」
                app.cols[k].set(not app.cols[k].get())
                app._defer()
            root.update()
            self.assertEqual(calls, [], "还在攒的时候就不该算")
            self.assertIn("重算中", app.status.get(), "攒着也得让人知道在攒")
            deadline = time.time() + 5.0
            while not calls and time.time() < deadline:
                root.update()
                time.sleep(0.02)
            self.assertEqual(calls, [True],
                             "三次点击算了 %d 次（该只算最后那一次的精确解）"
                             % len(calls))
        finally:
            root.destroy()

    def test_unchecking_a_column_keeps_the_forced_points(self):
        """取消一列勾选，不许把 spur/事件的强制保留**静默**关掉。

        原来有个「所有列都还在才用 forced」的守卫，后果是六路里取消任何
        一列，保底点全没了，而复选框还打着勾 —— 点数和字节数暴跌，
        人只会归因于「少了五列」。
        """
        root, app = self._app("demo_tran.csv")
        try:
            self.assertGreaterEqual(len(app.cols), 3)
            self.assertTrue(app.force_metrics.get())
            self.assertTrue(app.red.forced, "夹具本身就该有强制保留点")
            app.cols[1].set(False)
            app._recompute(True)
            self.assertTrue(app.red.forced,
                            "取消一列之后强制保留点没了（守卫又回来了）")
        finally:
            root.destroy()

    def test_tol_change_resyncs_the_points_slider(self):
        """tol 改了候选集就变，滑块量程必须跟着变。"""
        root, app = self._app("demo_tran.csv")
        try:
            app.tol_v.set(0.5)
            app._on_tol()
            fine = int(app.s_mp.cget("to"))
            self.assertEqual(fine, max(10, len(app.cand)))
            app.tol_v.set(50.0)
            app._on_tol()
            coarse = int(app.s_mp.cget("to"))
            self.assertEqual(coarse, max(10, len(app.cand)))
            self.assertLess(coarse, fine, "tol 调粗候选点该变少，量程该跟着缩")
            self.assertLessEqual(app.mp_v.get(), coarse, "当前值该被夹回量程内")
        finally:
            root.destroy()

    def test_ctrl_c_is_left_to_the_text_widget(self):
        """整份复制不许占用 Ctrl+C —— 那是文本框选区复制的键。"""
        root, app = self._app("demo_tran.csv")
        try:
            self.assertEqual(root.bind("<Control-c>"), "",
                             "顶层绑了 Ctrl+C，会盖掉文本框的选区复制")
            self.assertTrue(root.bind("<Control-C>"), "Ctrl+Shift+C 没绑上")
        finally:
            root.destroy()

    def test_programmatic_set_does_not_recompute(self):
        """_mute() 里的 set() 只同步界面，不许回头触发重算。"""
        root, app = self._app("demo_tran.csv")
        try:
            calls = []
            real = app._recompute
            app._recompute = lambda precise=True: (calls.append(precise),
                                                   real(precise))[1]
            with app._mute():
                app.mp_v.set(max(2, app.mp_v.get() - 7))
                app.tol_v.set(app.tol_v.get() + 1.0)
            root.update()
            self.assertEqual(calls, [], "静音区里的 set 还是触发了重算")
        finally:
            root.destroy()

    def test_zoom_on_log_axis_is_geometric(self):
        """log 轴要在 log 空间里缩放。按线性缩，滚一格锚点就跑到几个 decade 外。"""
        import wave_gui
        root, app = self._app("demo_ac.csv")
        try:
            self.assertEqual(app.traces[0].xscale, "log", "夹具应当是 log 轴")
            w = app.c_wave.winfo_width()
            px = int(w * 0.35)
            anchor = app._xform(app.c_wave).wx(px)
            dec0 = math.log10(app.view[1] / app.view[0])
            app._zoom_at(app.c_wave, px, wave_gui.ZOOM_STEP)
            dec1 = math.log10(app.view[1] / app.view[0])
            self.assertAlmostEqual(dec1 / dec0, wave_gui.ZOOM_STEP, places=6,
                                   msg="log 轴上跨的 decade 数才是那个「跨度」")
            self.assertAlmostEqual(app._xform(app.c_wave).wx(px) / anchor, 1.0,
                                   delta=1e-6, msg="锚点跑了")
        finally:
            root.destroy()

    def test_pan_clamps_and_keeps_span(self):
        """平移只许挪，不许改跨度；撞到两端就停，不许把视窗压扁。"""
        root, app = self._app("demo_tran.csv")
        try:
            tr = app.traces[0]
            full = (tr.x[0], tr.x[-1])
            app._zoom_all()
            app._pan_px(app.c_wave, 10 ** 6)
            self.assertEqual(app.view, full, "全视窗时平移应当是空操作")
            app.view = (tr.x[0], tr.x[0] + (tr.x[-1] - tr.x[0]) * 0.2)
            app._redraw()
            span = app.view[1] - app.view[0]
            app._pan_px(app.c_wave, 10 ** 6)                 # 一把推到最右
            self.assertAlmostEqual(app.view[1], tr.x[-1], delta=span * 1e-6,
                                   msg="推到底该贴住右端")
            self.assertAlmostEqual(app.view[1] - app.view[0], span,
                                   delta=span * 1e-6, msg="平移改了跨度")
        finally:
            root.destroy()

    def test_y_axis_follows_the_view(self):
        """窄视窗里纵轴必须跟着收 —— 这是「放大局部」有没有用的分水岭。"""
        import wave_gui
        import wave_gui_draw as draw
        root, app = self._app("demo_tran.csv")
        try:
            tr = app.red.trace
            span = tr.x[-1] - tr.x[0]
            app.view = (tr.x[0] + span * 0.3975, tr.x[0] + span * 0.4025)
            app.lane_mode.set("each")       # 一条信号一道才比得出这件事
            app._redraw()
            root.update()
            blocks = app._lane_blocks()
            lane = draw.lanes_of(blocks, "each")[0]
            i0i1 = {id(b): wave_gui.fit_view(b.red.trace.x, app.view[0],
                                             app.view[1]) for b in blocks}
            lo_l, hi_l = draw.lane_span(lane[1], i0i1, True)
            lo_g, hi_g = draw.lane_span(lane[1], i0i1, False)
            self.assertLess((hi_l - lo_l) * 5, hi_g - lo_g,
                            "窄视窗里纵轴没跟着放大")
            # 视窗内的原始点必须完整落在纵轴范围里，否则画出来是被裁过的
            b, si = lane[1][0]
            i0, i1 = i0i1[id(b)]
            seg = b.red.trace.signals[si].y[i0:i1]
            self.assertLessEqual(lo_l, min(seg))
            self.assertGreaterEqual(hi_l, max(seg))
        finally:
            root.destroy()

    def test_same_unit_signals_share_one_axis(self):
        """同单位共轴 —— 各自拉满会让曲线之间的高低和交叉变成假的。"""
        import wave_gui_draw as draw
        root, app = self._app("demo_tran.csv")
        try:
            blocks = app._lane_blocks()
            by_unit = draw.lanes_of(blocks, "unit")
            each = draw.lanes_of(blocks, "each")
            self.assertLess(len(by_unit), len(each), "按单位分道没合并任何东西")
            units = [u for u, _ in by_unit]
            self.assertEqual(len(units), len(set(units)), "同一个单位分了两道")
        finally:
            root.destroy()

    def test_y_scale_floors_at_eps(self):
        """视窗里一片平的时候，纵轴尺度要被 eps 兜住。

        不兜的话尺度会被容差以内的抖动撑开，红线看起来到处跑出灰带，
        像是压缩把这段毁了 —— 其实全在容差里。判丢没丢东西看误差窗格的 ±1。
        """
        root, app = self._app("demo_tran.csv")
        try:
            import wave_gui
            import wave_gui_draw as draw
            tr = app.red.trace
            # 找一段全平的视窗：demo_tran 起点附近 V(ctrl) 基本不动
            app.view = (tr.x[0], tr.x[0] + (tr.x[-1] - tr.x[0]) * 0.002)
            app.lane_mode.set("each")
            app.y_local.set(True)
            app._redraw()
            root.update()
            blocks = app._lane_blocks()
            i0i1 = {id(b): wave_gui.fit_view(b.red.trace.x, app.view[0],
                                             app.view[1]) for b in blocks}
            for _u, items in draw.lanes_of(blocks, "each"):
                b, si = items[0]
                s = b.red.trace.signals[si]
                lo, hi = draw.lane_span(items, i0i1, True)
                i0, i1 = i0i1[id(b)]
                seg = s.y[i0:i1]
                if max(seg) - min(seg) < s.eps:      # 这一段确实是平的
                    self.assertLessEqual(lo, min(seg))
                    self.assertGreaterEqual(hi, max(seg))
        finally:
            root.destroy()

    def test_copy_contains_every_block(self):
        """「复制全文」必须是**全文**。

        原来只发当前那一条 trace：布局 B（两个 time 列）和 --demod（包络块+频率块）
        都会产生多条，粘出去的东西是残的，而且残得看不出来 —— 头部长得一模一样，
        只是少了一块。这个按钮是整个工具的出口。
        """
        root, app = self._app("demo_tran_layoutb.csv")
        try:
            self.assertEqual(len(app.traces), 2)
            txt = app.wv_text()
            _, cli = C.run_cli([C.ex("demo_tran_layoutb.csv")])
            self.assertEqual(txt.count("# WV1"), cli.count("# WV1"),
                             "GUI 复制出来的块数跟命令行对不上")
            for tr in app.traces:
                self.assertIn(tr.signals[0].name, txt)
        finally:
            root.destroy()

    def test_gui_honors_demod(self):
        """--demod 在 GUI 里也要生效，并且跟命令行给出同样的块。

        它曾经只接了命令行：`--gui --demod` 开出来的还是原始波形，没有任何提示。
        """
        import math
        import os
        import tempfile
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
            import tkinter as tk
            import wave_cli
            import wave_gui
            args = wave_cli.build_parser().parse_args(["--demod"])
            args.budget = 51200
            args.demod_cycles, args.demod_min = 6, 20
            root = tk.Tk()
            try:
                app = wave_gui.WaveGui(root, p, args)
                for _ in range(500):
                    root.update()
                    if app.red:
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(app.red)
                self.assertTrue(app.red.trace.signals[0].name.startswith("env_hi"),
                                "GUI 没有解调：%s"
                                % app.red.trace.signals[0].name)
                self.assertEqual(len(app.traces), 2, "包络块 + 频率块")
                txt = app.wv_text()
                self.assertIn("[CYCLES]", txt, "代表性周期没跟着复制出来")
                _, cli = C.run_cli([p, "--demod", "--budget", "51200"])
                self.assertEqual(txt.count("# WV1"), cli.count("# WV1"))
            finally:
                root.destroy()
        finally:
            os.unlink(p)

    def test_modes_are_toggles_in_the_window(self):
        """解调 / 只压当前视窗是**窗口里的开关**，不是命令行标志。

        用户的入口是 GUI：「为了换个模式回命令行重开窗口」这件事本身就是设计错了。
        而且勾了要能取消 —— 所以原始 trace 必须留着。
        """
        import math
        import os
        import tempfile
        rows = ["i /VCO/VDD; tran (I) X,i /VCO/VDD; tran (I) Y"]
        t = 0.0
        while t < 4e-7:
            a = 2e-3 * (1 - math.exp(-(t - 1e-7) / 2e-8)) if t > 1e-7 else 0.0
            rows.append("%.12g,%.9g"
                        % (t, 4e-4 + a * math.sin(2 * math.pi * 5.03e9 * t)))
            t += 1e-11
        fd, p = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            with open(p, "w", newline="\n") as fh:
                fh.write("\n".join(rows) + "\n")
            import tkinter as tk
            import wave_cli
            import wave_gui
            args = wave_cli.build_parser().parse_args([])
            args.budget = 51200
            args.demod_cycles, args.demod_min = 6, 20
            root = tk.Tk()
            try:
                app = wave_gui.WaveGui(root, p, args)
                for _ in range(500):
                    root.update()
                    if app.red:
                        break
                    time.sleep(0.02)
                self.assertIsNotNone(app.red)
                full = (app.red.trace.x[0], app.red.trace.x[-1])
                self.assertEqual(len(app.traces), 1)

                app.demod_v.set(True)
                app._remode()
                self.assertEqual(len(app.traces), 2, "包络块 + 频率块")
                self.assertTrue(app.red.trace.signals[0].name.startswith("env_hi"))
                self.assertIn("[CYCLES]", app.wv_text())

                app.view = (1.5e-7, 1.7e-7)
                app.win_v.set(True)
                app._remode()
                self.assertGreater(app.red.trace.x[0], 1.4e-7, "窗口没生效")
                self.assertLess(app.red.trace.x[-1], 1.8e-7)

                app.win_v.set(False)
                app.demod_v.set(False)
                app._remode()
                self.assertEqual(len(app.traces), 1, "取消不掉就是单向门")
                self.assertAlmostEqual(app.red.trace.x[0], full[0], delta=1e-12)
                self.assertAlmostEqual(app.red.trace.x[-1], full[1], delta=1e-12)
            finally:
                root.destroy()
        finally:
            os.unlink(p)

    def test_every_exported_section_has_a_view(self):
        """导出的每一段都得看得见。

        原来只有 SHAPE 和误差有画面：`[EVENTS]`（全精度时间轴）一条看不见，
        `[CYCLES]`（代表性周期）更是完全没有视图 —— 而后者恰恰是判断
        「波形失真没失真」该看的那块。
        """
        import math
        import os
        import tempfile
        rows = ["i /VCO/VDD; tran (I) X,i /VCO/VDD; tran (I) Y"]
        t = 0.0
        while t < 4e-7:
            a = 2e-3 * (1 - math.exp(-(t - 1e-7) / 2e-8)) if t > 1e-7 else 0.0
            rows.append("%.12g,%.9g"
                        % (t, 4e-4 + a * math.sin(2 * math.pi * 5.03e9 * t)))
            t += 1e-11
        fd, p = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            with open(p, "w", newline="\n") as fh:
                fh.write("\n".join(rows) + "\n")
            import tkinter as tk
            import wave_cli
            import wave_gui
            args = wave_cli.build_parser().parse_args([])
            args.budget = 51200
            args.demod_cycles, args.demod_min = 6, 20
            root = tk.Tk()
            try:
                app = wave_gui.WaveGui(root, p, args)
                app.c_wave.configure(width=900, height=330)
                app.c_err.configure(width=900, height=180)
                for _ in range(500):
                    root.update()
                    if app.red:
                        break
                    time.sleep(0.02)
                # EVENTS 叠加：开关一关，图元必须变少（证明真画了）
                app.ev_v.set(True)
                app._redraw()
                root.update()
                with_ev = len(app.c_wave.find_all())
                app.ev_v.set(False)
                app._redraw()
                root.update()
                self.assertLess(len(app.c_wave.find_all()), with_ev,
                                "EVENTS 开关没起作用")
                # 代表周期视图：没解调时给一句提示，解调后真画出来
                app.low_v.set("cycles")
                app._redraw()
                root.update()
                bare = len(app.c_err.find_all())
                app.demod_v.set(True)
                app._remode()
                root.update()
                self.assertTrue(app.red.trace.picks, "解调后应当有代表性周期")
                self.assertGreater(len(app.c_err.find_all()), bare,
                                   "代表周期没画出来")
                for pk in app.red.trace.picks:
                    self.assertEqual(len(pk["t"]), len(pk["y"]))
                    self.assertTrue(pk["why"], "每个代表周期都要说清为什么被选中")
            finally:
                root.destroy()
        finally:
            os.unlink(p)

    def test_window_title_shows_the_build(self):
        """「我跑的是哪一版」在窗口里就该看得见，不用回命令行。"""
        root, app = self._app("demo_tran.csv")
        try:
            from wave_core import build_id
            self.assertIn(build_id(), root.title())
        finally:
            root.destroy()

    def test_canvas_segment_budget(self):
        """拖 max-points 时 Canvas 图元数要**有界**，而且不跟点数一起长。

        只比**拖滑块**那几行。缩放会改变图元数是对的：EVENTS 标注只画视窗内的，
        窗口小了事件自然少几条。把缩放也算进「恒定」里，等于禁止任何
        跟视窗有关的绘制 —— 那不是这条测试想守的东西。

        原来卡的是「完全相等」，现在放成「差几个常数以内」：保留点比像素列
        少时画折线（1 个图元），多时画上下两条沿（2 个），换画法会差两三个。
        要守的从来是「图元数不随 max_points 增长」—— 掉帧是那么来的，
        不是差四个图元来的。
        """
        p = subprocess.run(
            [sys.executable, os.path.join(ROOT, "tools", "wave_reduce.py"),
             os.path.join(ROOT, "examples", "demo_tran.csv"),
             "--gui", "--selftest"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        out = p.stdout.decode("utf-8", "replace")
        counts = [ln.split("canvas items")[1].strip()
                  for ln in out.splitlines()
                  if ln.startswith("max_points=") and "canvas items" in ln]
        self.assertGreaterEqual(len(counts), 4, out)
        wave = [int(c.split("/")[0]) for c in counts]
        err = [int(c.split("/")[1]) for c in counts]
        # 判据是**有界**，不是恒定：三层带里那层「影」按连续段画 polygon，
        # 段数随「削掉了多少」变，而那本来就该随参数变。要守的是
        # 「图元数不随 max_points 一起长」—— 掉帧是那么来的。
        for name, vals in (("波形格", wave), ("误差格", err)):
            self.assertLess(max(vals), 600,
                            "%s图元数已经不是常数量级了: %s" % (name, counts))
            self.assertLessEqual(vals[-1], vals[0],
                                 "%s图元数跟着 max_points 一起长了: %s"
                                 % (name, counts))


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

    def test_kept_band_is_minmax_not_sampling(self):
        """保留点压成带子时，一列里的极值必须两头都在。"""
        import wave_gui
        x = [i * 1e-9 for i in range(1000)]
        y = [(3.0 if i == 42 else (-2.0 if i == 43 else 0.0))
             for i in range(1000)]
        lo, hi = wave_gui._kept_band(x, y, list(range(1000)), x[0], x[-1], 50)
        self.assertAlmostEqual(max(v for v in hi if v is not None), 3.0)
        self.assertAlmostEqual(min(v for v in lo if v is not None), -2.0)
        self.assertEqual(len(lo), 50)

    def test_error_band_catches_the_peak_that_sampling_misses(self):
        """误差分箱要**保证**含最大误差 —— 抽样版会漏，这正是换掉它的理由。"""
        import wave_gui
        from _common import core
        rows = ["time (s),V(o) (V)"]
        n = 4000
        for i in range(n):                      # 每周期约 4 点的振荡：抽样必漏
            t = i * 1e-11
            rows.append("%.12g,%.9g" % (t, math.sin(2 * math.pi * 2.5e10 * t)))
        tr = core.parse_csv("<t>", text="\n".join(rows) + "\n")[0]
        core.analyze(tr)
        core.set_eps(tr, 0.005)
        red = core.reduce_trace(tr, 0.005, 40, core.predecimate(tr, 0.005), [])
        band = wave_gui.error_band(tr, red, 0, 0, n, 60)
        peak = max(max(abs(v[0]), abs(v[1])) for v in band if v is not None)
        eps = tr.signals[0].eps
        self.assertGreaterEqual(peak, 0.9 * red.worst.maxerr / eps,
                                "带子没抓住 recon 报的那个最大误差")

    def test_event_colour_is_deterministic(self):
        """事件颜色必须由列号决定，不能由 str hash 决定。

        `hash("c1")` 每个进程都不一样（PYTHONHASHSEED 随机化），
        于是标着 c1 的竖线这次蓝下次橙，跟 c1 自己的颜色也对不上；
        截图发给别人，两边看到的颜色不同 —— 而颜色是图上唯一的归属线索。
        """
        import wave_gui
        self.assertEqual(wave_gui._col_index("c1"), 0)
        self.assertEqual(wave_gui._col_index("c3"), 2)
        self.assertEqual(wave_gui._col_index("c12"), 11)
        self.assertEqual(wave_gui._col_index(""), 0)
        self.assertEqual(wave_gui._col_index(None), 0)
        self.assertEqual(wave_gui._col_index("总计"), 0, "认不出就给 0，别抛")

    def test_kept_band_uses_quantized_values(self):
        """上格画的必须是 .wv 里**真的会存的**值，跟下格同源。"""
        import wave_gui
        from _common import core
        rows = ["time (s),V(o) (V)"]
        for i in range(400):
            rows.append("%.12g,%.9g" % (i * 1e-9, math.sin(i * 0.05)))
        tr = core.parse_csv("<t>", text="\n".join(rows) + "\n")[0]
        core.analyze(tr)
        core.set_eps(tr, 0.005)
        red = core.reduce_trace(tr, 0.005, 60, core.predecimate(tr, 0.005), [])
        cs = red.specs[0]
        y = tr.signals[0].y
        raw = wave_gui._kept_band(tr.x, y, red.kept, tr.x[0], tr.x[-1], 40)
        q = wave_gui._kept_band(tr.x, y, red.kept, tr.x[0], tr.x[-1], 40, cs)
        # 量化后的每个值都必须落在量化栅格上
        step = cs.q_nat
        if step > 0:
            for v in [v for v in q[0] if v is not None]:
                k = (v - cs.offset) * cs.scale / (step * cs.scale)
                self.assertAlmostEqual(k, round(k), places=6,
                                       msg="上格的值没落在量化栅格上")
        self.assertNotEqual(raw, q, "量化前后一模一样，说明 cs 根本没起作用")

    def test_bin_column_basis_is_the_view(self):
        """分箱的列基准由调用方给，且给的是视窗两端。

        `fit_view` 特意往两边各多取一个原始点。拿它当基准的话，
        分箱坐标系比绘图坐标系宽两个点 —— 放大到几十个点时偏得肉眼可见。
        """
        import wave_gui
        x = [i * 1.0 for i in range(100)]
        y = [0.0] * 100
        y[50] = 1.0
        # 视窗 [40, 60]，但下标区间被 fit_view 撑到 [39, 62)
        band = wave_gui.bin_envelope(x, [y], 39, 62, 21, 40.0, 60.0)
        hi = band[0][1]
        peak = [i for i, v in enumerate(hi) if v == 1.0]
        self.assertEqual(peak, [10], "x=50 在 [40,60] 上应当落在正中那一列")

    def test_nice_ticks_are_round(self):
        """刻度必须是 1/2/5×10^k 的整数值。

        原来是先把绘图区四等分再反算那个位置的 x，于是刻度落在
        717.6 ps、500.4 ns 这种数上 —— 一张图上没有一个整数，读数得靠估。
        """
        import wave_gui_draw as draw
        for lo, hi in ((0.0, 2e-6), (1.6e-6, 1.615e-6), (-3.0, 7.0),
                       (0.0, 1.0), (1e9, 5e9)):
            ts = draw.nice_ticks(lo, hi, 5)
            self.assertTrue(ts, "%g..%g 一个刻度都没给" % (lo, hi))
            self.assertTrue(all(lo - 1e-12 <= t <= hi + 1e-12 for t in ts))
            step = ts[1] - ts[0] if len(ts) > 1 else (hi - lo)
            m = step / 10.0 ** math.floor(math.log10(step))
            self.assertAlmostEqual(min((1.0, 2.0, 5.0, 10.0),
                                       key=lambda v: abs(v - m)), m, places=6,
                                   msg="步长 %g 不是 1/2/5 的整数倍" % step)
        self.assertEqual(draw.nice_ticks(1.0, 1.0), [], "退化区间该给空")

    def test_symlog_is_monotonic_and_keeps_the_knee(self):
        """symlog：±1 附近还是线性，之外按数量级压，而且不许饱和。

        原来纵轴硬钳在 ±3，3 倍和 300 倍长得一模一样 —— 而那正是
        「调 tol 还是换模式」的决定量。
        """
        import wave_gui_draw as draw
        self.assertAlmostEqual(draw.symlog(0.0), 0.0)
        self.assertAlmostEqual(draw.symlog(1.0), 1.0)
        self.assertAlmostEqual(draw.symlog(-1.0), -1.0)
        self.assertAlmostEqual(draw.symlog(10.0), 2.0)
        self.assertAlmostEqual(draw.symlog(100.0), 3.0)
        prev = None
        for e in [x / 10.0 for x in range(-2000, 2001)]:
            v = draw.symlog(e)
            if prev is not None:
                self.assertGreaterEqual(v, prev - 1e-12, "symlog 不单调")
            prev = v
        self.assertGreater(draw.symlog(300.0), draw.symlog(3.0),
                           "300× 和 3× 必须画在不同高度")

    def test_recon_band_has_no_holes(self):
        """重建带不许有空列 —— 保留点稀疏时整段列全 None，带子会断成虚线。"""
        import wave_gui_draw as draw
        from _common import core
        rows = ["time (s),V(o) (V)"]
        for i in range(2000):
            rows.append("%.12g,%.9g" % (i * 1e-9, math.sin(i * 0.01)))
        tr = core.parse_csv("<t>", text="\n".join(rows) + "\n")[0]
        core.analyze(tr)
        core.set_eps(tr, 0.005)
        red = core.reduce_trace(tr, 0.005, 12, core.predecimate(tr, 0.005), [])
        lo, hi = draw.recon_band(tr, red, 0, 0, 2000, 120, tr.x[0], tr.x[-1])
        self.assertEqual(sum(1 for v in lo if v is None), 0, "重建带有空列")
        for a, b in zip(lo, hi):
            self.assertLessEqual(a, b)

    def test_clipped_runs_only_reports_real_loss(self):
        """「影」只画超过容差的那些段 —— 否则整张图都是影。"""
        import wave_gui_draw as draw
        n = 20
        olo = [0.0] * n
        ohi = [1.0] * n
        rlo = [0.0] * n
        rhi = [1.0] * n
        self.assertEqual(draw.clipped_runs(olo, ohi, rlo, rhi, 0.01), [],
                         "没丢东西却报了影")
        for i in range(5, 9):
            rhi[i] = 0.5                       # 上沿被削掉 0.5
        runs = draw.clipped_runs(olo, ohi, rlo, rhi, 0.01)
        self.assertEqual(runs, [(5, 8, "hi")])
        self.assertEqual(draw.clipped_runs(olo, ohi, rlo, rhi, 1.0), [],
                         "容差比削掉的还大，不该报")

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
