# -*- coding: utf-8 -*-
"""控制台 UTF-8 输出修正（Windows GBK 控制台下中文与符号会报错）。"""
from __future__ import annotations

import sys


def enable_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        except Exception:
            pass
