#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wave_reduce.py — 入口。把 Cadence 波形 CSV 压成能粘进聊天框的 `.wv`。

    python tools/wave_reduce.py my.csv -o my.wv
    python tools/wave_reduce.py my.csv --tol 0.002 --budget 20480
    python tools/wave_reduce.py my.csv --gui

真正的东西在 wave_cli / wave_core / wave_emit / wave_metrics_*。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from wave_cli import main                                       # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
