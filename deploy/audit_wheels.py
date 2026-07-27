#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_wheels.py — glibc 轮子标签审计。**离线安装的头号安全闸**。

隔离区是 CentOS7 那一档：glibc 2.17（manylinux2014 / manylinux_2_17 基线）。
黄区是 Windows，`pip download` 会兴高采烈地给你抓 manylinux_2_28/_2_31 的轮子，
到了隔离区就是 `GLIBC_2.28 not found`。

**这个错误必须在黄区暴露，不能等到隔离区**：一次来回的成本极高
（重新打包、重新走上传通道、重新等审批）。所以打包流程里这一步是硬闸，
不过就 exit 1，并且直接打印该把哪个包降到哪个版本。

规则：
- `py3-none-any`（纯 python）永远放行
- `manylinux_X_Y_<arch>` / `manylinux1|2010|2014_<arch>` 取它需要的最低 glibc
- `musllinux_*` 拒绝（libc 不对）
- 裸 `linux_*` 拒绝（本地编译产物，不可移植）

    python audit_wheels.py wheels/
    python audit_wheels.py wheels/ --max-glibc 2.17 --arch x86_64

搬自 LDO_modeling/deploy/audit_wheels.py（已在真实 airgap 上跑通）。
"""

import argparse
import os
import re
import sys

NAMED = {"manylinux1": (2, 5), "manylinux2010": (2, 12), "manylinux2014": (2, 17)}
_MX = re.compile(r"^manylinux_(\d+)_(\d+)_(.+)$")
_MX_NAMED = re.compile(r"^(manylinux1|manylinux2010|manylinux2014)_(.+)$")
_MUSL = re.compile(r"^musllinux_(\d+)_(\d+)_(.+)$")
INF = (999, 999)


def _platform_glibc(tag, arch):
    """一个 platform 子标签 -> (需要的 glibc, 架构对不对)。"""
    if tag == "any":
        return (0, 0), True                       # 纯 python
    m = _MX.match(tag)
    if m:
        return (int(m.group(1)), int(m.group(2))), (m.group(3) == arch)
    m = _MX_NAMED.match(tag)
    if m:
        return NAMED[m.group(1)], (m.group(2) == arch)
    if _MUSL.match(tag):
        return INF, False                         # musl，不是 glibc
    if tag.startswith("linux_"):
        return INF, False                         # 裸 linux：本地编译的，不可移植
    return INF, False                             # win_* / macosx_* / 不认识的


def audit_wheel(path, arch="x86_64"):
    name = os.path.basename(path)
    stem = name[:-4] if name.endswith(".whl") else name
    parts = stem.split("-")
    plat = parts[-1] if len(parts) >= 5 else ""
    best = None
    subtags = plat.split(".") if plat else []
    for t in subtags:
        g, ok_arch = _platform_glibc(t, arch)
        if g == (0, 0):
            best = (0, 0)
            break
        if ok_arch and g != INF:
            best = g if best is None else min(best, g)
    return {"name": name, "tags": subtags, "min_glibc": best}


def audit_dir(wheels_dir, max_glibc=(2, 17), arch="x86_64"):
    files = sorted(f for f in os.listdir(wheels_dir)) if os.path.isdir(wheels_dir) else []
    rows, viol = [], []
    for f in files:
        if not f.endswith(".whl"):
            continue
        r = audit_wheel(os.path.join(wheels_dir, f), arch=arch)
        g = r["min_glibc"]
        if g is None:
            r["verdict"] = "REJECT（没有 glibc/%s 可用的标签）" % arch
            viol.append(r)
        elif g > max_glibc:
            r["verdict"] = ("REJECT（要 glibc %d.%d > %d.%d）"
                            % (g[0], g[1], max_glibc[0], max_glibc[1]))
            viol.append(r)
        else:
            r["verdict"] = ("OK（纯 python）" if g == (0, 0)
                            else "OK（glibc %d.%d）" % (g[0], g[1]))
        rows.append(r)
    return rows, viol


def main():
    ap = argparse.ArgumentParser(description="审计轮子的 glibc 兼容性")
    ap.add_argument("wheels_dir")
    ap.add_argument("--max-glibc", default="2.17")
    ap.add_argument("--arch", default="x86_64")
    a = ap.parse_args()
    mj, mn = (int(v) for v in a.max_glibc.split("."))
    rows, viol = audit_dir(a.wheels_dir, (mj, mn), a.arch)
    if not rows:
        print("%s 里没有 .whl —— 依赖为空时这是正常的" % a.wheels_dir)
        return 0
    w = max(len(r["name"]) for r in rows)
    for r in rows:
        print("  %-*s  %s" % (w, r["name"], r["verdict"]))
    print("\n%d 个轮子，%d 个不合格；目标 glibc %s/%s"
          % (len(rows), len(viol), a.max_glibc, a.arch))
    if viol:
        print("审计不通过 -> 把这些降到最后一个有 manylinux_2_17/2014 轮子的版本：")
        for r in viol:
            print("   - %s  %s" % (r["name"], r["verdict"]))
        return 1
    print("审计通过 -> 全部能装进隔离区。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
