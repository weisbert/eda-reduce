# -*- coding: utf-8 -*-
"""部署管道。

这是出错成本最高的一环：包坏了要到隔离区才发现，一次来回极贵。
所以这里是**真的打包、真的解开、真的跑 bootstrap.sh**，不是读代码猜。

需要 bash（Windows 上 Git Bash 就行）和 git 仓库，缺了会跳过并说明。
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

from _common import ROOT

BASH = shutil.which("bash")
HAS_GIT = shutil.which("git") and os.path.isdir(os.path.join(ROOT, ".git"))


def sh(args, cwd=None, env=None):
    e = dict(os.environ, PYTHONIOENCODING="utf-8")
    e.update(env or {})
    return subprocess.run(args, cwd=cwd or ROOT, env=e, timeout=600,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)


class TestNoHardcodedInstallPath(unittest.TestCase):
    """安装位置不该由工具替用户决定。

    原来默认 /opt/eda_reduce，这一条同时假定了 root 权限、FHS 约定、
    和用户的目录布局 —— 三条都不成立。
    """

    def test_no_absolute_prefix_in_deploy(self):
        bad = []
        d = os.path.join(ROOT, "deploy")
        for f in sorted(os.listdir(d)):
            if not f.endswith((".sh", ".py", ".ps1", ".md")):
                continue
            with open(os.path.join(d, f), encoding="utf-8-sig") as fh:
                for i, ln in enumerate(fh, 1):
                    if re.search(r"/opt/eda[_-]?reduce", ln):
                        bad.append("%s:%d" % (f, i))
        self.assertEqual(bad, [], "deploy/ 里还有写死的绝对安装路径: %s" % bad)

    def test_prefix_resolution_is_explicit(self):
        """安装位置的来源必须是这三个，而且顺序固定：参数 > 环境变量 > 当前目录。"""
        for f in ("bootstrap.sh", "update.sh"):
            with open(os.path.join(ROOT, "deploy", f), encoding="utf-8") as fh:
                src = fh.read()
            for token in ("EDA_REDUCE_PREFIX", "$PWD/eda_reduce"):
                self.assertIn(token, src, "%s 少了 %s" % (f, token))


@unittest.skipUnless(BASH and HAS_GIT, "需要 bash + git 仓库")
class TestPackageAndInstall(unittest.TestCase):
    """打包 -> 解开 -> 安装 -> 增量更新，整条链真跑一遍。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="edadeploy")
        cls.dist = os.path.join(cls.tmp, "dist")
        r = sh([sys.executable, os.path.join(ROOT, "deploy", "package.py"),
                "full", "--out", cls.dist])
        cls.full_log = r.stdout.decode("utf-8", "replace")
        assert r.returncode == 0, cls.full_log
        cls.tar = os.path.join(cls.dist, "eda_reduce_full.tar.gz")
        cls.pkg = os.path.join(cls.tmp, "pkg")
        os.makedirs(cls.pkg)
        subprocess.check_call(["tar", "xzf", cls.tar, "-C", cls.pkg])

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_package_layout(self):
        for f in ("MANIFEST.json", "requirements.lock", "bootstrap.sh",
                  "update.sh", "app/tools/wave_reduce.py",
                  "app/examples/demo_tran.csv", "app/VERSION"):
            self.assertTrue(os.path.exists(os.path.join(self.pkg, f)), f)

    def test_no_crlf_anywhere(self):
        """.sh 带 \\r 到 Linux 上就是 bad interpreter —— 整条链最容易踩的坑。"""
        bad = []
        for base, _, files in os.walk(self.pkg):
            for f in files:
                p = os.path.join(base, f)
                if f.endswith((".png", ".gz")):
                    continue
                with open(p, "rb") as fh:
                    if b"\r" in fh.read():
                        bad.append(os.path.relpath(p, self.pkg))
        self.assertEqual(bad, [], "包里有 CRLF: %s" % bad)

    def test_version_export_subst_resolved(self):
        """隔离区没有 git，cat VERSION 就得能看到 commit —— 占位符必须被替换掉。"""
        with open(os.path.join(self.pkg, "app", "VERSION"),
                  encoding="utf-8") as fh:
            v = fh.read()
        self.assertNotIn("$Format:", v, "export-subst 没生效")
        self.assertRegex(v, r"commit\s+[0-9a-f]{40}")

    def test_shebangs_intact(self):
        for f in ("bootstrap.sh", "update.sh"):
            with open(os.path.join(self.pkg, f), "rb") as fh:
                self.assertTrue(fh.read(2) == b"#!", f + " 少了 shebang")

    def test_install_defaults_to_current_directory(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        r = sh([BASH, os.path.join(self.pkg, "bootstrap.sh")], cwd=home)
        out = r.stdout.decode("utf-8", "replace")
        self.assertEqual(r.returncode, 0, out)
        p = os.path.join(home, "eda_reduce")
        self.assertTrue(os.path.isdir(p), "默认该装到 ./eda_reduce\n" + out)
        for sub in ("app", "results", "INSTALL.json", "wave"):
            self.assertTrue(os.path.exists(os.path.join(p, sub)), sub)
        self.assertIn("装好了", out)
        # 冒烟测试跑的是真实压缩，结果要和仓库基线一致
        self.assertIn("2545 -> 585", out, "端到端结果和基线对不上\n" + out)

    def test_in_place_install(self):
        """包是 tarbomb，「解进自己建的目录然后就地装」才是最自然的用法。

        这时 app/ 已经在位，不用拷 —— 拷反而会自己吃自己
        （rm -rf $PREFIX/app 把源删了）。
        """
        home = os.path.join(self.tmp, "inplace", "eda_reduce")
        os.makedirs(home)
        subprocess.check_call(["tar", "xzf", self.tar, "-C", home])
        r = sh([BASH, "bootstrap.sh"], cwd=home)
        out = r.stdout.decode("utf-8", "replace")
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("就地", out)
        self.assertFalse(os.path.exists(os.path.join(home, "eda_reduce")),
                         "不该在里面再套一层")
        for sub in ("app", "results", "INSTALL.json", "wave", ".backups"):
            self.assertTrue(os.path.exists(os.path.join(home, sub)), sub)
        self.assertIn("2545 -> 585", out, "就地装出来的结果也得对")
        r = sh([BASH, os.path.join(home, "wave"), "--list-kinds"], cwd=home)
        self.assertEqual(r.returncode, 0, r.stdout.decode("utf-8", "replace"))

    def test_refuses_nested_install_inside_package_dir(self):
        """装到包目录**下面**（不等于它）才是自己吃自己，要拦。"""
        r = sh([BASH, "bootstrap.sh", os.path.join(self.pkg, "sub")],
               cwd=self.pkg)
        out = r.stdout.decode("utf-8", "replace")
        self.assertNotEqual(r.returncode, 0, "应当拒绝: " + out)
        self.assertIn("在包目录", out)

    def test_incremental_extracted_into_install_dir(self):
        """增量包直接解进安装目录 —— 这是最自然的用法，必须支持。

        能支持的前提是载荷目录叫 app_incoming/ 而不是 app/：不撞名，
        解包就不会在 update.sh 跑起来之前把已装好的 app/ 盖掉，
        备份才备得到**旧**版，回滚点才保得住。
        这里用一个 marker 文件把「备份里到底是新是旧」钉死。
        """
        home = os.path.join(self.tmp, "inplace_upd", "eda_reduce")
        os.makedirs(home)
        subprocess.check_call(["tar", "xzf", self.tar, "-C", home])
        self.assertEqual(sh([BASH, "bootstrap.sh"], cwd=home).returncode, 0)
        marker = os.path.join(home, "app", "tools", "_marker.txt")
        with open(marker, "w") as fh:
            fh.write("OLD")

        r = sh([sys.executable, os.path.join(ROOT, "deploy", "package.py"),
                "incremental", "--out", self.dist])
        self.assertEqual(r.returncode, 0, r.stdout.decode("utf-8", "replace"))
        itar = os.path.join(self.dist, "eda_reduce_incremental.tar.gz")
        names = subprocess.check_output(["tar", "tzf", itar]).decode()
        self.assertNotIn("\napp/", "\n" + names,
                         "增量包里不许有顶层 app/，会盖掉安装好的那份")
        self.assertIn("app_incoming/", names)

        subprocess.check_call(["tar", "xzf", itar, "-C", home])
        self.assertTrue(os.path.exists(marker), "解包不该碰到已装的 app/")
        r = sh([BASH, "update.sh"], cwd=home)
        out = r.stdout.decode("utf-8", "replace")
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("更新完成", out)

        bks = [d for d in os.listdir(os.path.join(home, ".backups"))]
        self.assertTrue(bks, "应当留下备份")
        got = [b for b in bks
               if os.path.exists(os.path.join(home, ".backups", b, "tools",
                                              "_marker.txt"))]
        self.assertTrue(got, "备份里必须是**旧**版（带 marker）——"
                             "备成新版就等于回滚点丢了")
        self.assertFalse(os.path.exists(marker), "新 app/ 不该有旧 marker")
        self.assertFalse(os.path.isdir(os.path.join(home, "app_incoming")),
                         "成功之后载荷目录该收掉")
        r = sh([BASH, os.path.join(home, "wave"), "--list-kinds"], cwd=home)
        self.assertEqual(r.returncode, 0, r.stdout.decode("utf-8", "replace"))

    def test_rollback_when_extracted_in_place(self):
        """就地更新失败也要能滚回去。"""
        home = os.path.join(self.tmp, "inplace_rb", "eda_reduce")
        os.makedirs(home)
        subprocess.check_call(["tar", "xzf", self.tar, "-C", home])
        self.assertEqual(sh([BASH, "bootstrap.sh"], cwd=home).returncode, 0)
        with open(os.path.join(home, "app", "tools", "_marker.txt"), "w") as fh:
            fh.write("OLD")
        itar = os.path.join(self.dist, "eda_reduce_incremental.tar.gz")
        if not os.path.exists(itar):
            r = sh([sys.executable, os.path.join(ROOT, "deploy", "package.py"),
                    "incremental", "--out", self.dist])
            self.assertEqual(r.returncode, 0)
        subprocess.check_call(["tar", "xzf", itar, "-C", home])
        with open(os.path.join(home, "app_incoming", "tools",
                               "wave_reduce.py"), "w") as fh:
            fh.write("import nonexistent_module_xyz\n")
        r = sh([BASH, "update.sh"], cwd=home)
        out = r.stdout.decode("utf-8", "replace")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("正在回滚", out)
        self.assertTrue(
            os.path.exists(os.path.join(home, "app", "tools", "_marker.txt")),
            "滚回去的应当是带 marker 的旧版")
        r = sh([BASH, os.path.join(home, "wave"), "--list-kinds"], cwd=home)
        self.assertEqual(r.returncode, 0, r.stdout.decode("utf-8", "replace"))

    def test_update_finds_install_when_run_from_inside_it(self):
        """人站在装好的目录里跑 update，不该还要求他打路径。"""
        home = os.path.join(self.tmp, "fromin", "eda_reduce")
        os.makedirs(home)
        subprocess.check_call(["tar", "xzf", self.tar, "-C", home])
        self.assertEqual(sh([BASH, "bootstrap.sh"], cwd=home).returncode, 0)
        ipkg = os.path.join(self.tmp, "fromin", "upd")
        os.makedirs(ipkg, exist_ok=True)
        r = sh([sys.executable, os.path.join(ROOT, "deploy", "package.py"),
                "incremental", "--out", self.dist])
        self.assertEqual(r.returncode, 0, r.stdout.decode("utf-8", "replace"))
        subprocess.check_call(
            ["tar", "xzf", os.path.join(self.dist,
                                        "eda_reduce_incremental.tar.gz"),
             "-C", ipkg])
        r = sh([BASH, os.path.join(ipkg, "update.sh")], cwd=home)
        out = r.stdout.decode("utf-8", "replace")
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("更新完成", out)

    def test_explicit_prefix_and_env_var(self):
        home = os.path.join(self.tmp, "h2")
        os.makedirs(home)
        want = os.path.join(home, "my tools")     # 顺便验带空格的路径
        r = sh([BASH, os.path.join(self.pkg, "bootstrap.sh"), want], cwd=home)
        self.assertEqual(r.returncode, 0, r.stdout.decode("utf-8", "replace"))
        self.assertTrue(os.path.isdir(os.path.join(want, "app")))

        home3 = os.path.join(self.tmp, "h3")
        os.makedirs(home3)
        r = sh([BASH, os.path.join(self.pkg, "bootstrap.sh")], cwd=home3,
               env={"EDA_REDUCE_PREFIX": os.path.join(home3, "viaenv")})
        self.assertEqual(r.returncode, 0, r.stdout.decode("utf-8", "replace"))
        self.assertTrue(os.path.isdir(os.path.join(home3, "viaenv", "app")))

    def test_incremental_update_and_rollback(self):
        home = os.path.join(self.tmp, "h4")
        os.makedirs(home)
        sh([BASH, os.path.join(self.pkg, "bootstrap.sh")], cwd=home)
        prefix = os.path.join(home, "eda_reduce")

        r = sh([sys.executable, os.path.join(ROOT, "deploy", "package.py"),
                "incremental", "--out", self.dist])
        self.assertEqual(r.returncode, 0, r.stdout.decode("utf-8", "replace"))
        ipkg = os.path.join(self.tmp, "ipkg")
        os.makedirs(ipkg, exist_ok=True)
        subprocess.check_call(
            ["tar", "xzf", os.path.join(self.dist,
                                        "eda_reduce_incremental.tar.gz"),
             "-C", ipkg])

        r = sh([BASH, os.path.join(ipkg, "update.sh")], cwd=home)
        out = r.stdout.decode("utf-8", "replace")
        self.assertEqual(r.returncode, 0, out)
        self.assertIn("更新完成", out)

        # 把包里的工具改坏 -> 冒烟测试失败 -> 必须自动滚回去
        with open(os.path.join(ipkg, "app_incoming", "tools",
                               "wave_reduce.py"), "w",
                  encoding="utf-8", newline="\n") as fh:
            fh.write("import nonexistent_module_xyz\n")
        r = sh([BASH, os.path.join(ipkg, "update.sh")], cwd=home)
        out = r.stdout.decode("utf-8", "replace")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("正在回滚", out)
        # 滚完还能用，results/ 还在
        r = sh([BASH, os.path.join(prefix, "wave"), "--list-kinds"], cwd=home)
        self.assertEqual(r.returncode, 0, r.stdout.decode("utf-8", "replace"))
        self.assertTrue(os.path.isdir(os.path.join(prefix, "results")))

    def test_update_without_install_reports_where_it_looked(self):
        empty = os.path.join(self.tmp, "empty")
        os.makedirs(empty, exist_ok=True)
        ipkg = os.path.join(self.tmp, "ipkg")
        if not os.path.exists(os.path.join(ipkg, "update.sh")):
            self.skipTest("增量包还没建（依赖上一个测试）")
        r = sh([BASH, os.path.join(ipkg, "update.sh")], cwd=empty)
        out = r.stdout.decode("utf-8", "replace")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("找过", out, "找不到时要把找过哪些地方列出来，不猜")


@unittest.skipUnless(HAS_GIT, "需要 git 仓库")
class TestDependencyHashGate(unittest.TestCase):
    """改了依赖却只推增量包 —— 这种损坏是静默的，必须在打包时就挡住。"""

    def test_incremental_aborts_when_requirements_changed(self):
        tmp = tempfile.mkdtemp(prefix="edahash")
        try:
            dist = os.path.join(tmp, "dist")
            self.assertEqual(sh([sys.executable,
                                 os.path.join(ROOT, "deploy", "package.py"),
                                 "full", "--out", dist]).returncode, 0)
            req = os.path.join(ROOT, "deploy", "requirements.txt")
            with open(req, "rb") as fh:
                orig = fh.read()
            try:
                with open(req, "ab") as fh:
                    fh.write(b"\nnumpy==1.24.4\n")
                r = sh([sys.executable,
                        os.path.join(ROOT, "deploy", "package.py"),
                        "incremental", "--out", dist])
                out = r.stdout.decode("utf-8", "replace")
                self.assertNotEqual(r.returncode, 0, "依赖变了必须中止")
                self.assertIn("中止", out)
                self.assertIn("full", out, "要指明得走 full 包")
            finally:
                with open(req, "wb") as fh:
                    fh.write(orig)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestGlibcAuditGate(unittest.TestCase):
    """轮子太新的错误必须在黄区暴露，等到隔离区来回一趟成本极高。"""

    def test_rejects_too_new_and_musl(self):
        sys.path.insert(0, os.path.join(ROOT, "deploy"))
        import audit_wheels
        tmp = tempfile.mkdtemp(prefix="edawheel")
        try:
            for n in ("numpy-1.26.0-cp311-cp311-manylinux_2_28_x86_64.whl",
                      "bad-1.0-cp311-cp311-musllinux_1_1_x86_64.whl",
                      "ok-1.0-py3-none-any.whl",
                      "fine-1.0-cp311-cp311-manylinux2014_x86_64.whl"):
                open(os.path.join(tmp, n), "wb").close()
            rows, viol = audit_wheels.audit_dir(tmp, (2, 17), "x86_64")
            self.assertEqual(len(rows), 4)
            names = sorted(r["name"].split("-")[0] for r in viol)
            self.assertEqual(names, ["bad", "numpy"])
            ok = {r["name"].split("-")[0]: r["verdict"] for r in rows}
            self.assertIn("2.28", ok["numpy"], "要说清要的是哪个 glibc")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
