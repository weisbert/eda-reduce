# -*- coding: utf-8 -*-
"""img2b64 的测试。

分三类：

  * 编码/解码的性质 —— 往返一致、分块能拼回、量化真的降到指定色数
  * `advise()` 的三条判断 —— 灰度和 JPEG 对曲线图都是陷阱，压太狠要提示
  * **回归** —— 下面两条是开真窗走查时撞出来的，单元测试当初一条都没抓到，
    所以特意钉在这里

回归那两条值得说清楚，因为它们是同一类错误：

  1. `render_text(chunked=False)` 原本写成 `not chunked and n == 1`，
     于是块数 > 1 时静默退回分块 —— 用户点「复制 base64」（明确说了不要块头）
     拿到的却是带 `###` 的文本。
  2. `chunk` 原本是独立常数 40000，`limit` 是 50000。44572 字符会同时显示
     「够小，1 条消息搞定」和「块数 2」，自相矛盾。现在 chunk 从 limit 派生。

GUI 走查默认跳过（会弹窗），跟 test_gui.py 一个开关：

    EDA_REDUCE_GUI_TEST=1 python -m unittest discover -s tests
"""

import io
import os
import subprocess
import sys
import tempfile
import unittest

import _common as C
from _common import ROOT, TOOLS

try:
    from PIL import Image
    import img2b64 as M
    HAVE = True
except ImportError:
    HAVE = False

RUN_GUI = os.environ.get("EDA_REDUCE_GUI_TEST") == "1"


@unittest.skipUnless(HAVE, "img2b64 需要 Pillow")
class TestEncode(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.img = M._make_curve()

    def test_roundtrip(self):
        r = M.encode(self.img, M.Opts())
        self.assertEqual(M.decode_text(M.render_text(r)), r.data)

    def test_empty_opts_does_not_explode(self):
        """Opts() 空构造直接喂进 encode 不能炸 —— 参数归一化在消费点，不在 main()。"""
        r = M.encode(self.img, M.Opts())
        self.assertGreater(r.nbytes, 0)

    def test_out_of_range_params_are_clamped(self):
        o = M.Opts(colors=9999, scale=-5, quality=200, fmt="tiff")
        self.assertEqual(o.colors, 256)
        self.assertEqual(o.scale, 5)
        self.assertEqual(o.fmt, "png")

    def test_quantize_hits_target_color_count(self):
        for c in (64, 16, 4):
            r = M.encode(self.img, M.Opts(colors=c))
            got = len(r.preview.convert("RGB").getcolors(maxcolors=100000) or [])
            self.assertLessEqual(got, c, "colors=%d 出来 %d 色" % (c, got))

    def test_fewer_colors_is_actually_smaller(self):
        """曲线图的主力旋钮。合成图必须带抗锯齿，否则这条测了个寂寞。"""
        ncol = len(self.img.convert("RGB").getcolors(maxcolors=10 ** 7) or [])
        self.assertGreater(ncol, 500, "合成图只有 %d 色，不像真实截图" % ncol)
        big = M.encode(self.img, M.Opts(colors=256)).nb64
        small = M.encode(self.img, M.Opts(colors=16)).nb64
        self.assertGreater(100.0 * (big - small) / big, 10)

    def test_preview_is_decoded_back_not_the_intermediate(self):
        """预览必须是压完再解码回来的那张，否则看不出压过头了。"""
        r = M.encode(self.img, M.Opts(scale=50))
        self.assertEqual(r.preview.size, r.out_size)


@unittest.skipUnless(HAVE, "img2b64 需要 Pillow")
class TestAutoFit(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.img = M._make_curve()

    def test_lands_under_limit_with_headroom(self):
        for lim in (50_000, 20_000, 8_000):
            r, ok = M.auto_fit(self.img, M.Opts(), lim)
            self.assertTrue(ok, "limit=%d 没压进去" % lim)
            self.assertLessEqual(r.nb64, lim * M.SAFETY,
                                 "limit=%d 贴边了，没留余量" % lim)

    def test_keeps_colors_before_dropping_resolution(self):
        """多条彩色曲线量化到 2 色会混成同一色，图就废了。

        宁可先掉分辨率（刻度还能读），也别先掉颜色区分度。
        """
        r, ok = M.auto_fit(self.img, M.Opts(), 8_000)
        self.assertGreaterEqual(r.opts.colors, 8,
                                "压到 colors=%d，曲线要混色了" % r.opts.colors)

    def test_loose_limit_keeps_full_resolution(self):
        r, ok = M.auto_fit(self.img, M.Opts(), 50_000)
        self.assertEqual(r.out_size, self.img.size)


@unittest.skipUnless(HAVE, "img2b64 需要 Pillow")
class TestAdvise(unittest.TestCase):
    """实测：曲线图上灰度和 JPEG 都是陷阱，工具得主动拦。"""

    @classmethod
    def setUpClass(cls):
        cls.img = M._make_curve()

    def test_recognizes_a_plot(self):
        self.assertEqual(M.guess_kind(self.img), "plot")

    def test_jpeg_really_is_worse_for_line_art(self):
        j = M.encode(self.img, M.Opts(fmt="jpeg", quality=80)).nb64
        p = M.encode(self.img, M.Opts(colors=32)).nb64
        self.assertGreater(j, p, "JPEG %d vs PNG-32 %d" % (j, p))

    def test_warns_on_jpeg_and_gray(self):
        self.assertIsNotNone(M.advise(self.img, M.Opts(fmt="jpeg")))
        self.assertIsNotNone(M.advise(self.img, M.Opts(gray=True)))

    def test_quiet_when_params_are_fine(self):
        self.assertIsNone(M.advise(self.img, M.Opts()))

    def test_warns_when_resolution_got_crushed(self):
        """压得下去不等于传得有用 —— 刻度读不出来就白传了。"""
        r = M.encode(self.img, M.Opts(scale=40))
        self.assertIsNotNone(M.advise(self.img, r.opts, r))
        full = M.encode(self.img, M.Opts())
        self.assertIsNone(M.advise(self.img, full.opts, full))


@unittest.skipUnless(HAVE, "img2b64 需要 Pillow")
class TestRegressions(unittest.TestCase):
    """走查撞出来的两条，单元测试当初一条都没抓到。"""

    @classmethod
    def setUpClass(cls):
        cls.img = M._make_curve()

    def test_chunked_false_is_respected_even_when_multi_block(self):
        r = M.encode(self.img, M.Opts(chunk=1000))
        self.assertGreater(r.nchunks(), 1, "这条测试需要一张会分多块的图")
        txt = M.render_text(r, chunked=False)
        self.assertNotIn("###", txt, "说了不要块头，却给了块头")
        self.assertEqual(len(txt), r.nb64)

    def test_chunk_follows_limit_so_verdict_never_contradicts(self):
        """不许出现「够小，1 条消息」和「块数 2」同时成立。"""
        for lim in (8_000, 20_000, 50_000, 120_000):
            o = M.Opts(limit=lim)
            r = M.encode(self.img, o)
            if r.nb64 <= lim:
                self.assertEqual(r.nchunks(), 1,
                                 "limit=%d 下 %d 字符说够小，却要分 %d 块"
                                 % (lim, r.nb64, r.nchunks()))


@unittest.skipUnless(HAVE, "img2b64 需要 Pillow")
class TestDecode(unittest.TestCase):
    """接收侧。指纹和块数必须**先验再开图**。

    反过来的话粘漏一块只会抛 `OSError: broken data stream`，看不出哪儿坏了 ——
    而粘 4 块漏一块、顺序错、被截断都是很可能发生的。
    """

    @classmethod
    def setUpClass(cls):
        cls.img = M._make_curve()

    def _run(self, txt):
        d = tempfile.mkdtemp()
        src = os.path.join(d, "in.txt")
        with open(src, "w", encoding="utf-8") as fh:
            fh.write(txt)
        out = os.path.join(d, "out.png")
        p = subprocess.run([sys.executable, os.path.join(TOOLS, "img2b64.py"),
                            "--decode", src, "--out", out],
                           capture_output=True, text=True, encoding="utf-8")
        return p.returncode, (p.stderr or ""), out

    def test_good_chunked_text_round_trips(self):
        r = M.encode(self.img, M.Opts(chunk=4000, name="FIG1"))
        txt = M.render_text(r, chunked=True)
        rc, err, out = self._run(txt)
        self.assertEqual(rc, 0, err)
        with open(out, "rb") as fh:
            self.assertEqual(fh.read(), r.data)

    def test_missing_block_is_named(self):
        r = M.encode(self.img, M.Opts(chunk=4000, name="FIG1"))
        txt = M.render_text(r, chunked=True)
        blocks = txt.split("\n\n")
        self.assertGreaterEqual(len(blocks), 3)
        rc, err, _ = self._run("\n\n".join(blocks[:1] + blocks[2:]))
        self.assertEqual(rc, 2)
        self.assertIn("块不全", err)

    def test_tampered_payload_fails_on_fingerprint(self):
        r = M.encode(self.img, M.Opts(chunk=4000, name="FIG1"))
        txt = M.render_text(r, chunked=True)
        lines = txt.splitlines()
        for i, ln in enumerate(lines):
            if not ln.startswith("###") and len(ln) > 60:
                lines[i] = ln[:50] + ("A" if ln[50] != "A" else "B") + ln[51:]
                break
        rc, err, _ = self._run("\n".join(lines))
        self.assertEqual(rc, 2)
        self.assertIn("指纹对不上", err)

    def test_not_an_image_is_reported_as_such(self):
        import base64
        rc, err, _ = self._run(base64.b64encode(b"not a picture" * 40).decode())
        self.assertEqual(rc, 2)
        self.assertIn("不是一张能打开的图", err)


@unittest.skipUnless(HAVE, "img2b64 需要 Pillow")
class TestCli(unittest.TestCase):

    def _run(self, args):
        p = subprocess.run([sys.executable, os.path.join(TOOLS, "img2b64.py")] + args,
                           capture_output=True, text=True, encoding="utf-8")
        return p.returncode, p.stdout, (p.stderr or "")

    def test_selftest_passes(self):
        rc, out, err = self._run(["--selftest"])
        self.assertEqual(rc, 0, out + err)

    def test_stdout_is_clean_base64_for_piping(self):
        """省略 --out 时 stdout 必须是干净的 base64，统计走 stderr。"""
        d = tempfile.mkdtemp()
        png = os.path.join(d, "a.png")
        M._make_curve().save(png)
        rc, out, err = self._run([png, "--limit", "200000"])
        self.assertEqual(rc, 0, err)
        body = out.strip()
        self.assertTrue(body.startswith("iVBORw0KGgo"), body[:40])
        self.assertNotIn("###", body)
        self.assertIn("base64", err)      # 统计确实在 stderr

    def test_encode_then_decode_from_files(self):
        d = tempfile.mkdtemp()
        png, txt, back = (os.path.join(d, n) for n in ("a.png", "a.txt", "b.png"))
        M._make_curve().save(png)
        rc, _, err = self._run([png, "--out", txt, "--name", "FIG1"])
        self.assertEqual(rc, 0, err)
        rc, _, err = self._run(["--decode", txt, "--out", back])
        self.assertEqual(rc, 0, err)
        with open(png, "rb") as a, open(back, "rb") as b:
            self.assertEqual(Image.open(a).size, Image.open(b).size)


@unittest.skipUnless(HAVE and RUN_GUI, "会弹窗；设 EDA_REDUCE_GUI_TEST=1 才跑")
class TestGuiWalkthrough(unittest.TestCase):
    """开真窗走一遍整条路。

    截图只守「画得像不像」，守不住「这条路走不走得通」—— 上面那两条回归
    就是这么撞出来的。run_gui 的 _probe 回调是专门为这个留的口子。
    """

    def test_walk(self):
        img = M._make_curve()
        seen = {}

        def walk(ns):
            try:
                ns["state"]["src"] = img
                ns["recompute"]()
                seen["stat"] = ns["stat"].cget("text")
                seen["verdict"] = ns["verdict"].cget("text")
                seen["tip_png"] = ns["tip"].cget("text")

                ns["fmt_v"].set("jpeg"); ns["recompute"]()
                seen["tip_jpeg"] = ns["tip"].cget("text")
                ns["fmt_v"].set("png"); ns["recompute"]()

                ns["limit_v"].set(8000); ns["do_auto"]()
                seen["auto"] = ns["state"]["result"]
                seen["colors_widget"] = ns["colors_v"].get()

                ns["limit_v"].set(200000); ns["do_auto"]()
                ns["do_copy"](False)
                seen["clip"] = ns["root"].clipboard_get()
                seen["result"] = ns["state"]["result"]
            except Exception as e:      # noqa: BLE001
                seen["error"] = repr(e)
            finally:
                ns["root"].after(50, ns["root"].destroy)

        M.run_gui(_probe=walk)

        self.assertNotIn("error", seen, seen.get("error"))
        self.assertIn("base64", seen["stat"])
        self.assertIn("够小", seen["verdict"])
        self.assertEqual(seen["tip_png"], "", "PNG 彩色不该啰嗦")
        self.assertIn("JPEG", seen["tip_jpeg"], "选 JPEG 必须给建议")
        self.assertGreaterEqual(seen["colors_widget"], 8, "自动寻优的参数没写回控件")
        # 点「复制 base64」拿到的必须是纯 base64，这是那条回归的 GUI 侧
        self.assertNotIn("###", seen["clip"])
        self.assertEqual(len(seen["clip"]), seen["result"].nb64)
        self.assertEqual(M.decode_text(seen["clip"]), seen["result"].data)


if __name__ == "__main__":
    unittest.main()
