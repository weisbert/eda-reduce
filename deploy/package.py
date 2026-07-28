#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
package.py — 黄区（Windows，有网）打包，产出隔离区能离线安装的包。

两种包，一条都不能省（wave-spec 第 7 节）：

    full         轮子 + requirements.lock + app + bootstrap.sh + update.sh
                 首次安装、**依赖变了**的时候用
    incremental  只有 app 源码 + update.sh，几十 KB
                 日常改代码用（绝大多数时候）

四个闸门：

1. **glibc 审计闸**（audit_wheels.py）：黄区就拒掉需要高于目标 glibc 的轮子。
   不然错误要到隔离区才暴露，来回一趟成本极高。
2. **依赖哈希闸**：incremental 不带轮子，所以它要求 requirements 自上次 full
   以来没变过。变了就中止，要求走 full。防「改了依赖却只推增量包」这类静默损坏。
3. **git 卫生闸**：打包读 **committed blob**（`git archive HEAD`），
   不读 Windows 工作树。理由有两个，都踩过：
   - 黄区的 core.autocrlf 会把 .py/.sh 变成 CRLF，到 Linux 上 shebang 带 \\r，
     报 "bad interpreter: /usr/bin/env python3^M"。`git archive` 走
     .gitattributes 的 `* text=auto eol=lf`，出来就是 LF。
   - 工作树里可能有没提交的改动，打进包里就无从追溯。
   工作树脏的时候会大声警告 —— 你以为打进去的改动其实没打进去。
4. **VERSION export-subst**：隔离区没有 git，用 `cat VERSION` 就能看到
   commit 和日期。

    python deploy/package.py full
    python deploy/package.py incremental
    python deploy/package.py full --out dist --target-glibc 2.17

只依赖标准库。
"""

import argparse
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import audit_wheels                                             # noqa: E402

# 进包的东西。examples/ 要带 —— 隔离区上的冒烟测试就靠它
EXPORT = ["tools", "examples", "docs", "deploy", "README.md", "LICENSE",
          "VERSION"]
PY_TAG, ABI, ARCH = "311", "cp311", "x86_64"
PLATFORMS = ["manylinux2014_x86_64", "manylinux_2_17_x86_64"]
REQ = "requirements.txt"


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for blk in iter(lambda: fh.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def sha256_text(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


def git(*args):
    return subprocess.check_output(["git"] + list(args), cwd=ROOT).decode(
        "utf-8", "replace").strip()


def git_sha():
    try:
        return git("rev-parse", "HEAD")
    except Exception:
        return "unknown"


def dirty():
    try:
        return bool(git("status", "--porcelain"))
    except Exception:
        return False


# 增量包里的载荷目录**故意不叫 app/**。
# 叫 app/ 的话，人把增量包解进安装目录（这是很自然的做法）就会在
# update.sh 跑起来之前把已装好的 app/ 盖掉，于是「备份」备的是新的那份，
# 回滚点静默丢失。换个名字，解哪儿都安全。
INCOMING = "app_incoming"
# 无后缀的启动器，打包时也要给 exec 位
EXEC_NAMES = {"update", "wave", "run_gui"}


def stage_app(stage, name="app"):
    """从 **committed blob** 导出 app，不碰工作树。"""
    app = os.path.join(stage, name)
    os.makedirs(app, exist_ok=True)
    have = []
    for p in EXPORT:
        try:
            git("cat-file", "-e", "HEAD:%s" % p)
            have.append(p)
        except Exception:
            pass
    blob = subprocess.check_output(
        ["git", "archive", "--format=tar", "HEAD"] + have, cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(blob)) as t:
        t.extractall(app)
    info = {"git_sha": git_sha(),
            "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "exported": have,
            "worktree_dirty": dirty()}
    _write(os.path.join(app, "BUILD_INFO.json"),
           json.dumps(info, indent=2, ensure_ascii=False) + "\n")
    return app, info


def _write(p, text):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


def _req_path():
    return os.path.join(HERE, REQ)


def _req_lines():
    if not os.path.exists(_req_path()):
        return []
    out = []
    with open(_req_path(), encoding="utf-8") as fh:
        for ln in fh:
            ln = ln.split("#")[0].strip()
            if ln:
                out.append(ln)
    return out


def download_wheels(stage):
    wheels = os.path.join(stage, "wheels")
    os.makedirs(wheels, exist_ok=True)
    reqs = _req_lines()
    if not reqs:
        print("      requirements 为空（wave_reduce 目前纯标准库）—— 不下轮子。")
        print("      管道照样按双包建：哪天加了 numpy，只是往 %s 加一行、"
              "重出一次 full 包，不用回头重做部署链。" % REQ)
        return wheels
    cmd = [sys.executable, "-m", "pip", "download", "-r", _req_path(),
           "--dest", wheels, "--only-binary=:all:", "--python-version", PY_TAG,
           "--implementation", "cp", "--abi", ABI]
    for p in PLATFORMS:
        cmd += ["--platform", p]
    print("      $ " + " ".join(cmd[2:]))
    subprocess.check_call(cmd)
    return wheels


def freeze_lock(wheels):
    pins = {}
    for w in sorted(os.listdir(wheels)):
        if not w.endswith(".whl"):
            continue
        parts = w[:-4].split("-")
        if len(parts) >= 2:
            pins[parts[0].replace("_", "-").lower()] = parts[1]
    if not pins:
        return "# 无第三方依赖（wave_core/wave_emit/wave_cli 纯标准库，硬性）\n"
    return "\n".join("%s==%s" % kv for kv in sorted(pins.items())) + "\n"


def _checksums(stage):
    out = {}
    for base, _, files in os.walk(stage):
        for f in files:
            p = os.path.join(base, f)
            rel = os.path.relpath(p, stage).replace(os.sep, "/")
            out[rel] = sha256_file(p)
    return dict(sorted(out.items()))


def _make_tar(stage, tar):
    names = []
    for base, _, files in os.walk(stage):
        for f in files:
            names.append(os.path.join(base, f))
    with tarfile.open(tar, "w:gz") as t:
        for p in sorted(names):
            arc = os.path.relpath(p, stage).replace(os.sep, "/")
            ti = t.gettarinfo(p, arcname=arc)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = "root"
            ti.mtime = 0                     # 可复现：同一个 commit 出同一个包
            if arc.endswith(".sh") or os.path.basename(arc) in EXEC_NAMES:
                ti.mode = 0o755
            with open(p, "rb") as fh:
                t.addfile(ti, fh)
    return tar


def build_full(out, target_glibc):
    stage = os.path.join(out, "_stage_full")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    print("[1/6] 从 committed blob 导出 app …")
    _, info = stage_app(stage)
    if info["worktree_dirty"]:
        print("      !! 工作树有未提交改动 —— 包里是 HEAD 的内容，那些改动"
              "**没有**进包。先 commit。")
    print("[2/6] 交叉下载隔离区目标的轮子 …")
    wheels = download_wheels(stage)
    print("[3/6] 审计 glibc <= %s …" % target_glibc)
    mj, mn = (int(v) for v in target_glibc.split("."))
    rows, viol = audit_wheels.audit_dir(wheels, (mj, mn), ARCH)
    for r in rows:
        print("      %s -> %s" % (r["name"], r["verdict"]))
    if viol:
        print("\n*** 审计不通过：%d 个轮子要 glibc > %s。"
              "在 %s 里降版本后重跑。***" % (len(viol), target_glibc, REQ))
        return 1
    print("      审计通过（%d 个轮子）" % len(rows))
    print("[4/6] 冻结 requirements.lock …")
    lock = freeze_lock(wheels)
    _write(os.path.join(stage, "requirements.lock"), lock)
    req_hash = sha256_text(lock)
    print("[5/6] 拷安装脚本 + 写 MANIFEST …")
    for f in ("bootstrap.sh", "update.sh"):
        shutil.copy(os.path.join(HERE, f), os.path.join(stage, f))
    man = {
        "mode": "full", "git_sha": info["git_sha"],
        "built_utc": info["built_utc"], "worktree_dirty": info["worktree_dirty"],
        "python": "cp" + PY_TAG, "arch": ARCH, "target_glibc": target_glibc,
        "requirements_hash": req_hash,
        "input_req_hash": (sha256_file(_req_path())
                           if os.path.exists(_req_path()) else "none"),
        "wheels": [w for w in sorted(os.listdir(wheels)) if w.endswith(".whl")],
    }
    _write(os.path.join(stage, "MANIFEST.json"),
           json.dumps(dict(man, checksums=_checksums(stage)), indent=2,
                      ensure_ascii=False) + "\n")
    man["checksums"] = _checksums(stage)
    print("[6/6] 打 tar …")
    tar = os.path.join(out, "eda_reduce_full.tar.gz")
    _make_tar(stage, tar)
    _write(os.path.join(out, "MANIFEST.full.json"),
           json.dumps(man, indent=2, ensure_ascii=False) + "\n")
    _write(tar + ".sha256",
           "%s  %s\n" % (sha256_file(tar), os.path.basename(tar)))
    print("\n完成 -> %s  (%.1f KB)\n      req-hash %s  |  %d 个轮子  |  "
          "sha256 旁文件已写"
          % (tar, os.path.getsize(tar) / 1024.0, req_hash[:12],
             len(man["wheels"])))
    print("      隔离区：mkdir ~/eda_reduce && cd ~/eda_reduce && "
          "tar xzf 这个包 && bash bootstrap.sh")
    print("            就地装，不需要 root。要装别处就 bash bootstrap.sh <路径>")
    return 0


def build_incremental(out, last_path):
    if not os.path.exists(last_path):
        print("增量包需要一次 full 的 MANIFEST（%s）；先跑 `package.py full`。"
              % last_path)
        return 2
    stage = os.path.join(out, "_stage_incr")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    print("[1/4] 从 committed blob 导出 app …")
    _, info = stage_app(stage, INCOMING)
    if info["worktree_dirty"]:
        print("      !! 工作树有未提交改动 —— 包里是 HEAD 的内容。先 commit。")
    print("[2/4] 核对 requirements 自上次 full 以来没变 …")
    with open(last_path, encoding="utf-8") as fh:
        last = json.load(fh)
    cur = sha256_file(_req_path()) if os.path.exists(_req_path()) else "none"
    if last.get("input_req_hash") is None:
        print("*** 中止：上次 full 的 MANIFEST 没有 input_req_hash，"
              "增量闸没有基准。重跑一次 `package.py full`。***")
        return 1
    if cur != last["input_req_hash"]:
        print("*** 中止：%s 自上次 full 之后变了（%s -> %s）。"
              "增量包不带轮子，必须走 `package.py full` 重下重审。***"
              % (REQ, str(last["input_req_hash"])[:12], cur[:12]))
        return 1
    print("      没变（%s）—— 只推代码是安全的" % cur[:12])
    print("[3/4] 拷 update.sh + 写 MANIFEST …")
    shutil.copy(os.path.join(HERE, "update.sh"), os.path.join(stage, "update.sh"))
    man = {
        "mode": "incremental", "git_sha": info["git_sha"],
        "built_utc": info["built_utc"], "worktree_dirty": info["worktree_dirty"],
        "requirements_hash": last["requirements_hash"],
        "based_on_full": last.get("built_utc"),
    }
    _write(os.path.join(stage, "MANIFEST.json"),
           json.dumps(dict(man, checksums=_checksums(stage)), indent=2,
                      ensure_ascii=False) + "\n")
    print("[4/4] 打 tar …")
    tar = os.path.join(out, "eda_reduce_incremental.tar.gz")
    _make_tar(stage, tar)
    _write(tar + ".sha256",
           "%s  %s\n" % (sha256_file(tar), os.path.basename(tar)))
    print("\n完成 -> %s  (%.1f KB，不带轮子，隔离区复用现有 venv)\n"
          "      带的 req-hash %s —— 跟已部署的对不上 update.sh 会中止"
          % (tar, os.path.getsize(tar) / 1024.0,
             man["requirements_hash"][:12]))
    print("      隔离区：解到哪儿都行，包括直接解进安装目录 ——")
    print("            载荷目录叫 %s/，不会盖掉已装好的 app/。" % INCOMING)
    print("            cd ~/eda_reduce && tar xzf 这个包 && bash update.sh")
    return 0


def main():
    ap = argparse.ArgumentParser(description="打隔离区离线包（双包模型）")
    ap.add_argument("mode", choices=["full", "incremental"])
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--last", default=None, help="增量包：上次 full 的 MANIFEST")
    ap.add_argument("--target-glibc", default="2.17")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    if a.mode == "full":
        return build_full(a.out, a.target_glibc)
    return build_incremental(
        a.out, a.last or os.path.join(a.out, "MANIFEST.full.json"))


if __name__ == "__main__":
    sys.exit(main())
