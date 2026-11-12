#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make_demo_db.py —— 生成一个演示用的 *_V3.db 日志数据库，用于测试/验收。"""
import os
import sqlite3
import struct
import sys
import math


def _force_utf8_stdout():
    """Windows 控制台默认编码（如 cp1252）写不下中文会直接崩溃；
    程序输出中文，启动时就把 stdout/stderr 强制切到 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def build_trc(n=1200, sp=0.25, delay=2.0, det_name="FID1", y_title="uV"):
    """按 chrom_core.parse_trc 期望的 BinTrc 格式构造一段谱图。"""
    def cstr(s):
        return s.encode("utf-8") + b"\x00"

    data = []
    for i in range(n):
        t = delay + i * sp
        v = 1000 + 300 * math.sin(t / 3.0)
        for c, a, w in ((50, 8000, 0.4), (150, 12000, 0.6), (260, 5000, 0.35)):
            v += a * math.exp(-((t - c) ** 2) / (2 * w * w))
        data.append(int(v))
    head = cstr("BinTrc") + struct.pack("<I", 1)
    chan = (cstr("Chan") + struct.pack("<I", 1)
            + struct.pack("<fff", sp, 1.0, delay)
            + struct.pack("<i", n) + struct.pack("<%di" % n, *data)
            + struct.pack("<d", 1.0) + struct.pack("<f", 4.0))
    tail = (cstr("TraceInfo") + struct.pack("<I", 1)
            + struct.pack("<ii i", 3, 1, 0)
            + cstr(det_name) + struct.pack("<f", sp)
            + cstr(y_title) + struct.pack("<f", 1.0)
            + cstr("s") + struct.pack("<f", 1.0)
            + struct.pack("<f", n * sp + delay) + struct.pack("<f", delay)
            + struct.pack("<ii f", 1, 0, 4.0)
            + cstr(det_name))
    return head + chan + tail


def main(path):
    _force_utf8_stdout()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.execute("CREATE TABLE Analyzer (SerialNumber TEXT, Name TEXT, Version TEXT)")
    cur.execute("CREATE TABLE Application (ApplicationId INTEGER, Name TEXT)")
    cur.execute("CREATE TABLE Stream (ApplicationId INTEGER, StreamId INTEGER, Name TEXT)")
    cur.execute("CREATE TABLE Cycle (CycleRowId INTEGER, IsValidation INTEGER, CalibrationLevel INTEGER)")
    cur.execute("""CREATE TABLE Chromatogram (CycleRowId INTEGER, ApplicationId INTEGER,
                   StreamId INTEGER, DetectorId INTEGER, CycleTime TEXT, BinTrc BLOB)""")
    cur.execute("INSERT INTO Analyzer VALUES ('DEMO123456', '演示色谱仪', 'V9.2')")
    cur.execute("INSERT INTO Application VALUES (1, '常规分析')")
    cur.execute("INSERT INTO Stream VALUES (1, 1, '流路A')")
    # 3 个周期，模拟周期性采样
    for k, (hh, mm) in enumerate(((8, 0), (8, 30), (9, 0))):
        cid = k + 1
        cur.execute("INSERT INTO Cycle VALUES (?,?,?)", (cid, 0, 0))
        cur.execute("INSERT INTO Chromatogram VALUES (?,?,?,?,?,?)",
                    (cid, 1, 1, 1, "2026-11-01 %02d:%02d:00" % (hh, mm),
                     build_trc(det_name="FID1", n=1200 + k * 100)))
    # 第二个检测器，一条
    cur.execute("INSERT INTO Chromatogram VALUES (?,?,?,?,?,?)",
                (1, 1, 1, 2, "2026-11-01 08:00:00", build_trc(det_name="TCD1", n=900)))
    con.commit()
    con.close()
    print("演示数据库已生成：%s" % path)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "demo_V3.db")
