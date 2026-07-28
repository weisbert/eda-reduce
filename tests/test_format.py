# -*- coding: utf-8 -*-
"""`.wv` 的格式契约 + 两条项目铁律。

铁律一：**脚本绝不写形容词**。输出里只有数，「这个过冲算不算问题」是模型的活。
铁律二：`wave_core` / `wave_emit` / `wave_cli` **纯标准库**。
        这三个文件 scp 到任何机器就能跑，是永远的逃生舱。
"""

import ast
import os
import sys
import unittest

import _common as C
from _common import ROOT, TOOLS, emit

# 脚本里出现这些就说明它在替模型下判断
ADJECTIVES = [
    "轻微", "严重", "看起来", "似乎", "大概", "基本稳定", "良好", "不错",
    "明显", "有点", "略微", "偏大", "偏小", "正常范围", "可以接受", "异常大",
    "slightly", "seems", "looks like", "probably", "reasonable", "acceptable",
    "excessive", "too large", "too small", "good", "bad", "healthy",
]

CORE_FILES = ["wave_core.py", "wave_emit.py", "wave_cli.py", "wave_reduce.py"]
STDLIB_OK = set("""
    argparse array ast bisect collections csv datetime functools gzip hashlib
    heapq io itertools json math os platform random re shutil struct subprocess
    sys tarfile tempfile threading time traceback unittest zlib
    wave_core wave_emit wave_cli wave_gui wave_metrics_tran wave_metrics_freq
""".split())


def imports_of(path):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), path)
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                out.add(a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
            out.add(n.module.split(".")[0])
    return out


class TestPureStdlib(unittest.TestCase):
    """依赖分层是硬性的：核心三件套不许碰第三方包。"""

    def test_core_layer_has_no_third_party(self):
        for f in CORE_FILES:
            bad = imports_of(os.path.join(TOOLS, f)) - STDLIB_OK
            self.assertEqual(bad, set(), "%s 引了非标准库: %s" % (f, bad))

    def test_metrics_layer_pure_python_default(self):
        """metrics 允许可选 numpy，但默认路径必须纯 python。"""
        for f in ("wave_metrics_tran.py", "wave_metrics_freq.py"):
            bad = imports_of(os.path.join(TOOLS, f)) - STDLIB_OK - {"numpy"}
            self.assertEqual(bad, set(), "%s 引了 %s" % (f, bad))

    def test_whole_repo_has_only_documented_optional_deps(self):
        """全仓扫一遍：非标准库的 import 只允许出现在文档里写明的可选依赖里。

        README 写着「不用 pip install」。这条测试就是那句话的守卫 ——
        谁哪天顺手 import 了个第三方包，这里会立刻炸。
        """
        allowed = {"PIL"}                       # deploy/requirements-optional.txt
        local = set(m[:-3] for m in os.listdir(TOOLS) if m.endswith(".py"))
        local |= {"audit_wheels", "_common", "_block"}
        try:
            std = set(sys.stdlib_module_names)
        except AttributeError:
            self.skipTest("Python < 3.10 没有 stdlib_module_names")
        bad = {}
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "dist", "__pycache__")]
            for f in files:
                if not f.endswith(".py"):
                    continue
                p = os.path.join(base, f)
                for m in imports_of(p) - std - local - allowed:
                    bad.setdefault(m, []).append(os.path.relpath(p, ROOT))
        self.assertEqual(bad, {}, "出现了没记录的第三方依赖: %s" % bad)

    def test_pillow_is_genuinely_optional(self):
        """把 PIL 从 meta_path 上彻底挡掉，plot_digitize 必须照常出结果。

        「有 Pillow 就用，没有就退回自带解码器」这句话得是真的 ——
        隔离区可能连 Pillow 都没有。
        """
        import subprocess
        import tempfile
        out = os.path.join(tempfile.mkdtemp(), "d.csv")
        code = (
            "import sys\n"
            "class B:\n"
            "    def find_module(self, n, p=None):\n"
            "        return self if n.split('.')[0] in ('PIL','numpy') else None\n"
            "    def load_module(self, n):\n"
            "        raise ImportError(n)\n"
            "sys.meta_path.insert(0, B())\n"
            "sys.path.insert(0, %r)\n"
            "import plot_digitize\n"
            "sys.exit(plot_digitize.main([%r,'--xaxis','0,300n','--yaxis','0,1.3',"
            "'--trace','#e01b24=v','-o',%r]))\n"
            % (TOOLS, os.path.join(ROOT, "examples", "demo_plot.png"), out))
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-c", code], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=180)
        err = p.stderr.decode("utf-8", "replace")
        self.assertEqual(p.returncode, 0, err)
        self.assertIn("自带解码器", err, "没有 PIL 时应当走自带解码器")
        self.assertTrue(os.path.exists(out))
        with open(out, encoding="utf-8") as fh:
            self.assertGreater(len(fh.readlines()), 1000)

    def test_escape_hatch_runs_without_metrics_modules(self):
        """metrics 模块缺席时也要能出 .wv —— 这是逃生舱的定义。"""
        rc, txt = C.run_cli([C.ex("demo_tran.csv"), "--no-metrics"])
        self.assertEqual(rc, 0)
        self.assertIn("[SHAPE]", txt)
        self.assertNotIn("[METRICS]", txt)


class TestScriptEncoding(unittest.TestCase):
    """BOM 的两个方向都会咬人，而且报错都指向别的地方。"""

    def _walk(self, ext):
        for base, dirs, files in os.walk(ROOT):
            dirs[:] = [d for d in dirs if d not in (".git", "dist", "__pycache__")]
            for f in files:
                if f.endswith(ext):
                    yield os.path.join(base, f)

    def test_ps1_with_chinese_has_utf8_bom(self):
        """PS 5.1 读**不带 BOM** 的 .ps1 会按 GBK 解码。

        中文变乱码之后引号配对全乱，报出来的是一堆「Unexpected token」，
        行号还是错的 —— 根本看不出真正原因是编码。实测踩过。
        """
        for p in self._walk(".ps1"):
            with open(p, "rb") as fh:
                raw = fh.read()
            try:
                raw.decode("ascii")
                continue                      # 纯 ASCII 的不需要 BOM
            except UnicodeDecodeError:
                pass
            self.assertTrue(
                raw.startswith(b"\xef\xbb\xbf"),
                "%s 含非 ASCII 字符但没有 UTF-8 BOM —— PS 5.1 会按 GBK 解码"
                % os.path.relpath(p, ROOT))

    def test_sh_has_no_bom(self):
        """反过来：.sh **绝不能**有 BOM。

        BOM 挡在 `#!/usr/bin/env bash` 前面，内核认不出 shebang，
        报的是 `bad interpreter` —— 和 CRLF 那个坑一模一样的症状。
        """
        for p in self._walk(".sh"):
            with open(p, "rb") as fh:
                head = fh.read(4)
            self.assertFalse(head.startswith(b"\xef\xbb\xbf"),
                             "%s 带了 BOM，shebang 会失效"
                             % os.path.relpath(p, ROOT))
            self.assertTrue(head.startswith(b"#!"),
                            "%s 第一行不是 shebang" % os.path.relpath(p, ROOT))

    def test_shell_scripts_are_lf(self):
        """.sh 带 \\r 到 Linux 上就是 bad interpreter。仓库 .gitattributes 管这个，
        这里再兜一层 —— 打包链上这是最容易踩的坑。"""
        for p in self._walk(".sh"):
            with open(p, "rb") as fh:
                self.assertNotIn(b"\r", fh.read(),
                                 "%s 有 CRLF" % os.path.relpath(p, ROOT))

    def test_ps1_parses_under_powershell(self):
        """有 powershell 就真的让它解析一遍 —— 上面那条 BOM 断言只是必要条件。"""
        import shutil
        import subprocess
        exe = shutil.which("powershell") or shutil.which("pwsh")
        if not exe:
            self.skipTest("这台机器没有 powershell")
        for p in self._walk(".ps1"):
            code = (
                "$e=$null;$t=$null;"
                "[System.Management.Automation.Language.Parser]::ParseFile("
                "'%s',[ref]$t,[ref]$e)|Out-Null;"
                "if($e.Count -gt 0){$e[0].Message;exit 1}else{exit 0}"
                % p.replace("'", "''"))
            r = subprocess.run([exe, "-NoProfile", "-NonInteractive",
                                "-Command", code],
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               timeout=120)
            self.assertEqual(r.returncode, 0,
                             "%s 解析失败: %s" % (os.path.relpath(p, ROOT),
                                                 r.stdout.decode("utf-8", "replace")))


class TestNoAdjectives(unittest.TestCase):

    def _scan(self, text, where):
        low = text.lower()
        for w in ADJECTIVES:
            self.assertNotIn(w.lower(), low,
                             "%s 里出现了形容词 %r —— 脚本只出数，判断是模型的活"
                             % (where, w))

    def test_wv_outputs(self):
        for f in ("demo_tran.csv", "demo_ac.csv", "demo_spec.csv"):
            rc, txt = C.run_cli([C.ex(f)])
            self.assertEqual(rc, 0)
            self._scan(txt, f)

    def test_baselines(self):
        for f in ("demo_tran.wv", "demo_ac.wv", "demo_spec.wv"):
            p = os.path.join(ROOT, "examples", f)
            with open(p, encoding="utf-8") as fh:
                self._scan(fh.read(), f)


class TestWvContract(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        _, cls.txt = C.run_cli([C.ex("demo_tran.csv")])
        cls.lines = cls.txt.splitlines()

    def test_first_line_is_version_header(self):
        self.assertTrue(self.lines[0].startswith("# WV1  tran  demo_tran"))

    def test_second_line_is_the_self_check(self):
        """头部第二行是自检 —— 这是读 .wv 时第一个该看的东西。"""
        self.assertTrue(self.lines[1].startswith("# recon:"))
        for k in ("max|err|", "% of range", "rms", "worst"):
            self.assertIn(k, self.lines[1])

    def test_sections_present_and_marked_at_line_start(self):
        starts = [ln for ln in self.lines if ln.startswith("[")]
        self.assertEqual([s.split("]")[0] + "]" for s in starts],
                         ["[METRICS]", "[EVENTS]", "[SHAPE]"])

    def test_legend_does_not_contain_bracketed_section_names(self):
        """注释里写 [SHAPE] 会让解析器分不清哪一行才是真的段开始。"""
        for ln in self.lines:
            if ln.startswith("#"):
                for s in ("[METRICS]", "[EVENTS]", "[SHAPE]"):
                    self.assertNotIn(s, ln)

    def test_column_declarations_cover_every_signal(self):
        decl = [ln for ln in self.lines if ln.startswith("# c")]
        self.assertEqual(len(decl), 4)
        for ln in decl:
            for k in ("offset", "range", "量化", "err"):
                self.assertIn(k, ln)

    def test_shape_rows_match_column_count(self):
        i = self.lines.index([ln for ln in self.lines
                              if ln.startswith("[SHAPE]")][0])
        ncol = len(self.lines[i].split()) - 1        # 去掉 [SHAPE] 自己
        rows = [ln for ln in self.lines[i + 1:] if ln.strip()]
        self.assertGreater(len(rows), 10)
        for ln in rows:
            self.assertEqual(len(ln.split()), ncol, ln)

    def test_metrics_are_name_value_unit_triples(self):
        """spec 要求 METRICS 每一项都是「名字 值 单位」三元组，模型才好读。"""
        tr = C.load("demo_tran.csv")
        m = C.metrics_of(tr)
        for it in m.metrics():
            self.assertTrue(it.name, "没名字")
            self.assertNotEqual(it.unit, None)
            if not isinstance(it.value, str):
                self.assertEqual(it.value, it.value, "值不能是 NaN")
            txt = it.text(tr.xunit)
            self.assertTrue(txt.startswith(it.name))
            if it.unit and it.value != "n/a":
                self.assertIn(it.unit.split("/")[0][:2], txt,
                              "单位没打出来: " + txt)


class TestBudget(unittest.TestCase):

    def test_default_budget_respected(self):
        for f in ("demo_tran.csv", "demo_ac.csv", "demo_spec.csv"):
            rc, txt = C.run_cli([C.ex(f)])
            n = len(txt.encode("utf-8"))
            self.assertLessEqual(n, 20 * 1024, "%s 出了 %d 字节" % (f, n))

    def test_small_budget_respected_or_declared(self):
        """契约是「压进预算，**或者**在输出里说清楚压不进去」。

        demo_tran 的 METRICS+EVENTS 本身就有 ~4 KB，3000 字节的预算物理上做不到。
        这时候正确行为是声明，不是偷偷截掉全精度测量 —— 那些是丢了不可逆的东西。
        """
        for b in (3000, 6000, 12000):
            rc, txt = C.run_cli([C.ex("demo_tran.csv"), "--budget", str(b)])
            n = len(txt.encode("utf-8"))
            if n > b:
                self.assertIn("超预算", txt,
                              "budget=%d 出了 %d 字节却没声明" % (b, n))
                self.assertIn("强制保留点", txt, "要说清楚为什么下不来")

    def test_impossible_budget_is_declared_not_silently_exceeded(self):
        """压不进去就说出来 —— 强制保留点不为预算牺牲。"""
        rc, txt = C.run_cli([C.ex("demo_spec.csv"), "--budget", "900"])
        self.assertEqual(rc, 0)
        if len(txt.encode("utf-8")) > 900:
            self.assertIn("超预算", txt, "超了必须在输出里声明")

    def test_budget_is_user_definable(self):
        """20 KB 是**这条通道**的宽度，不是普适真理 —— 必须能改。"""
        sizes = {}
        for b in (8192, 20480, 51200):
            _, txt = C.run_cli([C.ex("demo_tran.csv"), "--budget", str(b)])
            sizes[b] = len(txt.encode("utf-8"))
        self.assertLess(sizes[20480], sizes[51200], "预算放大要真的多带点")
        self.assertLessEqual(sizes[51200], 51200)
        self.assertLessEqual(sizes[20480], 20480)

    def test_budget_zero_means_unlimited(self):
        _, txt = C.run_cli([C.ex("demo_tran.csv"), "--budget", "0"])
        self.assertGreater(len(txt.encode("utf-8")), 20 * 1024)

    def test_budget_env_default(self):
        """换条通道就该换个常态值，不用每次敲 --budget。"""
        import importlib
        import wave_cli
        old = os.environ.get("EDA_REDUCE_BUDGET")
        try:
            for val, want in (("8k", 8192), ("51200", 51200), ("32kb", 32768)):
                os.environ["EDA_REDUCE_BUDGET"] = val
                importlib.reload(wave_cli)
                self.assertEqual(wave_cli.default_budget(), want, val)
            os.environ["EDA_REDUCE_BUDGET"] = "看不懂的东西"
            self.assertEqual(wave_cli.default_budget(), 20 * 1024,
                             "看不懂就退回默认，不该炸")
        finally:
            if old is None:
                os.environ.pop("EDA_REDUCE_BUDGET", None)
            else:
                os.environ["EDA_REDUCE_BUDGET"] = old
            importlib.reload(wave_cli)

    def test_gui_path_normalizes_xcols(self):
        """--gui --xcols 0,2 曾经把字符串直接传下去，set('0,2') 变成 [',','0','2']。"""
        import wave_cli
        seen = {}

        class FakeGui(object):
            @staticmethod
            def run(path, args):
                seen["xcols"] = args.xcols
                seen["budget"] = args.budget
                return 0

        sys.modules["wave_gui"] = FakeGui
        try:
            rc = wave_cli.main([C.ex("demo_tran_layoutb.csv"), "--gui",
                                "--xcols", "0,2", "--budget", "0"])
        finally:
            del sys.modules["wave_gui"]
        self.assertEqual(rc, 0)
        self.assertEqual(seen["xcols"], [0, 2], "GUI 也要拿到解析好的下标")
        self.assertIsNone(seen["budget"], "--budget 0 = 不限")

    def test_multi_trace_shares_budget(self):
        rc, txt = C.run_cli([C.ex("demo_tran_layoutb.csv")])
        self.assertLessEqual(len(txt.encode("utf-8")), 20 * 1024)
        self.assertEqual(txt.count("# WV1"), 2, "布局 B 出两段，各带完整头部")


class TestRegistry(unittest.TestCase):

    def test_both_kinds_registered(self):
        self.assertEqual(emit.registered(), ["freq", "tran"])

    def test_unknown_kind_still_emits(self):
        """没注册的 kind 也要出 .wv，只是没有 METRICS —— 加分析类型不该改核心。"""
        txt = "foo,bar\n" + "".join("%g,%g\n" % (i, i * i) for i in range(50))
        tr = C.core.parse_csv("<t>", text=txt)[0]
        C.core.analyze(tr)
        self.assertEqual(tr.kind, "unknown")
        self.assertIsNone(emit.make_metrics(tr))
        red = C.core.reduce_trace(tr)
        out = emit.emit(red, None)
        self.assertIn("[SHAPE]", out)
        self.assertNotIn("[METRICS]", out)


if __name__ == "__main__":
    unittest.main()
