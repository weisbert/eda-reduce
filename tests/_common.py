# -*- coding: utf-8 -*-
"""测试公用：路径、真值、跑 CLI 的小包装。"""

import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
EX = os.path.join(ROOT, "examples")
sys.path.insert(0, TOOLS)

import wave_core as core           # noqa: E402,F401
import wave_emit as emit           # noqa: E402,F401

emit.load_builtin()


def ex(name):
    return os.path.join(EX, name)


_TRUTH = None


def truth():
    global _TRUTH
    if _TRUTH is None:
        with open(ex("demo_truth.json"), encoding="utf-8") as fh:
            _TRUTH = json.load(fh)
    return _TRUTH


def tran_truth(sig):
    return truth()["demo_tran.csv"]["signals"][sig]


def load(name, **kw):
    """解析 + analyze，返回第一条 trace。"""
    trs = core.parse_csv(ex(name), **kw)
    for t in trs:
        core.analyze(t)
    return trs[0] if len(trs) == 1 else trs


def run_cli(args):
    """跑一遍 wave_cli.main，抓 stdout。"""
    import wave_cli
    old, err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
    try:
        rc = wave_cli.main(args)
        return rc, sys.stdout.getvalue()
    finally:
        sys.stdout, sys.stderr = old, err


def metrics_of(trace):
    return emit.run_metrics(trace)


def close(a, b, rel=0.02, abs_=0.0):
    if a is None or b is None:
        return False
    return abs(a - b) <= max(abs_, rel * abs(b))
