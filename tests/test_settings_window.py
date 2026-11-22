#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
原生设置窗口测试：apply_settings（无界面）与 SettingsWindow（真 tkinter，没有显示环境自动跳过）。
运行：  python -m unittest discover -s tests -v
"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

import server_app as sa          # noqa: E402
import config_tools as ct        # noqa: E402


def _write_cfg(path, data):
    import json
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


class ApplySettingsTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "config.json")
        self.data_dir = os.path.join(self.d, "data")
        os.mkdir(self.data_dir)
        _write_cfg(self.p, {"watch_dirs": [self.data_dir], "poll_seconds": 30})

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_save_hot_keys_no_restart(self):
        changed, restart, msg = sa.apply_settings(self.p, {"poll_seconds": 45})
        self.assertEqual(changed, ["poll_seconds"])
        self.assertEqual(restart, [])
        self.assertIn("下一轮扫描内自动生效", msg)
        self.assertEqual(ct.read_raw(self.p)["poll_seconds"], 45)

    def test_save_restart_key_flags_restart(self):
        changed, restart, msg = sa.apply_settings(
            self.p, {"endpoint": "opc.tcp://127.0.0.1:4841/ChromExtract"})
        self.assertEqual(changed, ["endpoint"])
        self.assertEqual(restart, ["endpoint"])
        self.assertIn("需要重启服务才会生效", msg)

    def test_no_change_reports_nothing_changed(self):
        changed, restart, msg = sa.apply_settings(self.p, {"poll_seconds": 30})
        self.assertEqual(changed, [])
        self.assertIn("没有改动", msg)

    def test_bad_watch_dir_rejected_and_file_unchanged(self):
        before = ct.read_raw(self.p)
        with self.assertRaises(ct.ConfigToolError):
            sa.apply_settings(self.p, {"watch_dirs": [os.path.join(self.d, "no-such-dir")]})
        self.assertEqual(ct.read_raw(self.p), before)

    def test_unknown_key_rejected(self):
        with self.assertRaises(ct.ConfigToolError):
            sa.apply_settings(self.p, {"no_such_key": 1})

    def test_watch_dirs_saved(self):
        other = os.path.join(self.d, "data2")
        os.mkdir(other)
        changed, _, _ = sa.apply_settings(self.p, {"watch_dirs": [self.data_dir, other]})
        self.assertEqual(changed, ["watch_dirs"])
        self.assertEqual(ct.read_raw(self.p)["watch_dirs"], [self.data_dir, other])


class SettingsWindowTests(unittest.TestCase):
    """原生设置窗口：用假对话框驱动（需要 tkinter 与显示环境，没有则自动跳过）。"""

    class FakeDialogs:
        def __init__(self):
            self.answers, self.msgs, self.confirm_answer = [], [], True

        def ask(self, title, prompt, secret=False):
            return self.answers.pop(0) if self.answers else None

        def info(self, t):
            self.msgs.append(("info", t))

        def error(self, t):
            self.msgs.append(("error", t))

        def confirm(self, t):
            return self.confirm_answer

    def setUp(self):
        try:
            import tkinter as tk
            self.root = tk.Tk()
            self.root.withdraw()
        except Exception as e:  # noqa
            self.skipTest("没有可用的图形环境：%s" % e)
        self.d = tempfile.mkdtemp()
        self.data_dir = os.path.join(self.d, "data")
        os.mkdir(self.data_dir)
        self.p = os.path.join(self.d, "config.json")
        _write_cfg(self.p, {"watch_dirs": [self.data_dir], "poll_seconds": 30})
        self.cfg = sa.load_config(self.p)
        self.cfg.update(_base_dir=self.d, _cfg_path=self.p)
        sa.normalize_config(self.cfg, self.d)
        self.dlg = self.FakeDialogs()
        self.win = sa.SettingsWindow(self.root, self.cfg, self.dlg)

    def tearDown(self):
        try:
            self.root.destroy()
        except Exception:  # noqa
            pass
        shutil.rmtree(getattr(self, "d", ""), ignore_errors=True)

    def _entry(self, name):
        return self.win.entries[name].get()

    def _set_entry(self, name, value):
        e = self.win.entries[name]
        e.delete(0, "end")
        e.insert(0, str(value))

    def test_initial_values_mirror_config(self):
        self.assertEqual(self._entry("poll_seconds"), "30")
        self.assertEqual(self.win.dirs_lb.get(0), self.data_dir)
        self.assertEqual(self._entry("endpoint"), "opc.tcp://0.0.0.0:4840/ChromExtract")

    def test_save_through_window_writes_config(self):
        self._set_entry("poll_seconds", 45)
        self.win.save()
        self.assertEqual(ct.read_raw(self.p)["poll_seconds"], 45)
        self.assertTrue(any(k == "info" and "已保存" in t for k, t in self.dlg.msgs))

    def test_bad_int_rejected_in_window(self):
        self._set_entry("poll_seconds", "abc")
        self.win.save()
        self.assertEqual(ct.read_raw(self.p)["poll_seconds"], 30)
        self.assertTrue(any(k == "error" for k, _ in self.dlg.msgs))

    def test_remove_dir_through_window(self):
        self.win.dirs_lb.selection_set(0)
        self.win.remove_dir()
        self.win.save()
        self.assertEqual(ct.read_raw(self.p)["watch_dirs"], [])

    def test_webhook_test_requires_address(self):
        self.win.test_webhook()
        self.assertTrue(any(k == "error" and "推送地址" in t for k, t in self.dlg.msgs))

    def test_smtp_password_set_and_clear(self):
        self.dlg.answers = ["Good-Pass-2026!", "Good-Pass-2026!"]
        self.win.set_smtp_password()
        self.assertTrue(ct.read_raw(self.p).get("smtp_password"))
        self.assertIn("已设置", self.win.smtp_pw_state.cget("text"))
        self.dlg.answers = [""]
        self.dlg.confirm_answer = True
        self.win.set_smtp_password()
        self.assertFalse(ct.read_raw(self.p).get("smtp_password"))
        self.assertIn("未设置", self.win.smtp_pw_state.cget("text"))

    def test_smtp_password_mismatch_rejected(self):
        self.dlg.answers = ["Good-Pass-2026!", "Different-Pass-1!"]
        self.win.set_smtp_password()
        self.assertFalse(ct.read_raw(self.p).get("smtp_password"))
        self.assertTrue(any(k == "error" for k, _ in self.dlg.msgs))


if __name__ == "__main__":
    unittest.main()
