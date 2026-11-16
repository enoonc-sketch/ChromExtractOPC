#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_demo_db.py —— 生成一个演示用的 *_V3.db 日志数据库，用于测试/验收。
实际逻辑在上级目录的 demo_data.py（打包进 EXE 后供 --selftest 使用）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from demo_data import build_trc, make_demo_db   # noqa: E402,F401


def _force_utf8_stdout():
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(path):
    _force_utf8_stdout()
    make_demo_db(path)
    print("演示数据库已生成：%s" % path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo_V3.db")
