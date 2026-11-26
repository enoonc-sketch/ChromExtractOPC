# -*- coding: utf-8 -*-
"""v3.0 桌面窗口新增功能的逻辑测试（desktop_features.py，不需要 tkinter / asyncua）：

谱图曲线坐标与刻度、快照来源（服务快照 / 直接读数据目录）、通道实时数据表、
常用配置的校验与保存（热加载 / 需重启分类）、新增配置键的校验、客户端会话防御性取值、
告警桌面提醒的判定、登录后自动打开窗口的快捷方式路径与创建 / 删除。
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config_tools as ct        # noqa: E402
import demo_data                 # noqa: E402
import desktop_features as df    # noqa: E402
import server_app as sa          # noqa: E402


def _rec(**over):
    r = dict(source="gc1_V3.db", analyzer="SN001", app=1, app_name="常规", stream=2, stream_name="流路B",
             det=1, det_name="TCD", cycle_id=7, cycle_time="2026-11-26 10:00:00", is_validation=0, cal_level=0,
             num_points=5, sample_period=0.5, duration=2.0, min_raw=10, max_raw=50, y_title="mV", x_title="s",
             time=[0.0, 0.5, 1.0, 1.5, 2.0], raw=[10.0, 20.0, 50.0, 20.0, 10.0],
             uv=[1.0, 2.0, 5.0, 2.0, 1.0], value=[0.1, 0.2, 0.5, 0.2, 0.1])
    r.update(over)
    return r


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ---------------------------------------------------------------------------
# 1. 谱图曲线
# ---------------------------------------------------------------------------

class TracePlot(unittest.TestCase):
    def test_channel_key_matches_alert_module(self):
        r = _rec()
        self.assertEqual(df.channel_key(r), "gc1_V3.db|1|2|1")     # 与 alerts.AlertMonitor 的通道键相同
        label = df.channel_label(r)
        for part in ("站点 SN001", "应用1 常规", "流路2 流路B", "检测器1 TCD"):
            self.assertIn(part, label)

    def test_channel_options_dedupe_and_sort(self):
        a, b = _rec(det=2, cycle_id=3), _rec(det=2, cycle_id=9)
        opts = df.channel_options([_rec(), a, b])
        self.assertEqual(len(opts), 2)
        self.assertIs(df.find_record([a, b], df.channel_key(a)), a)
        self.assertIsNone(df.find_record([a], "nope"))

    def test_series_selection(self):
        r = _rec()
        for kind, first in (("raw", 10.0), ("uv", 1.0), ("value", 0.1), ("bogus", 10.0)):
            xs, ys = df.series_of(r, kind)
            self.assertEqual(len(xs), len(ys))
            self.assertEqual(ys[0], first)
        self.assertIn("mV", df.series_title(r, "value"))

    def test_nice_ticks(self):
        self.assertEqual(df.nice_ticks(0, 10, 5), [0, 2, 4, 6, 8, 10])
        t = df.nice_ticks(0.13, 0.87, 4)
        self.assertTrue(all(0.13 <= v <= 0.87 for v in t))
        self.assertGreaterEqual(len(t), 3)
        self.assertEqual(df.nice_ticks(5, 5), [5])
        self.assertEqual(df.nice_ticks(10, 0, 5)[0], 0)
        self.assertEqual(df.fmt_tick(2.5, 0.5), "2.5")
        self.assertEqual(df.fmt_tick(200.0, 50), "200")

    def test_downsample_keeps_peaks(self):
        xs = list(range(10000))
        ys = [0.0] * 10000
        ys[5003] = 99.0
        ys[7001] = -50.0
        pts = df.downsample_minmax(xs, ys, 100)
        self.assertLessEqual(len(pts), 200)
        self.assertIn((5003, 99.0), pts)
        self.assertIn((7001, -50.0), pts)
        self.assertEqual([p[0] for p in pts], sorted(p[0] for p in pts))       # 顺序不乱
        self.assertEqual(len(df.downsample_minmax([1, 2], [3, 4], 100)), 2)    # 点少时原样返回

    def test_plot_layout_fits_canvas(self):
        r = _rec()
        lay = df.plot_layout(r, "raw", 600, 400)
        self.assertFalse(lay["empty"])
        x0, y0, x1, y1 = lay["box"]
        xs, ys = lay["line"][0::2], lay["line"][1::2]
        self.assertEqual(len(xs), 5)
        self.assertTrue(all(x0 - 0.5 <= x <= x1 + 0.5 for x in xs))
        self.assertTrue(all(y0 - 0.5 <= y <= y1 + 0.5 for y in ys))
        self.assertEqual(min(ys), min(ys[2:3]))          # 峰值（raw=50）在最上面
        self.assertTrue(lay["xticks"] and lay["yticks"])
        self.assertIn("周期时间 2026-11-26 10:00:00", lay["info"])
        self.assertIn("点数 5", lay["info"])
        self.assertIn("站点 SN001", lay["title"])
        # 缩放：画布变大，坐标跟着变
        big = df.plot_layout(r, "raw", 1200, 800)
        self.assertGreater(max(big["line"][0::2]), max(xs))

    def test_plot_layout_large_trace_is_downsampled(self):
        n = 20000
        r = _rec(time=[i * 0.1 for i in range(n)], raw=[float(i % 97) for i in range(n)], num_points=n)
        lay = df.plot_layout(r, "raw", 800, 400)
        self.assertEqual(lay["points"], n)
        self.assertLessEqual(lay["drawn"], 2 * 800)

    def test_plot_layout_empty_and_flat(self):
        self.assertTrue(df.plot_layout(None, "raw", 600, 400)["empty"])
        self.assertTrue(df.plot_layout(_rec(time=[], raw=[]), "raw", 600, 400)["empty"])
        self.assertTrue(df.plot_layout(_rec(), "raw", 50, 40)["empty"])                 # 窗口太小
        flat = df.plot_layout(_rec(raw=[3.0] * 5), "raw", 600, 400)                     # 一条平线也能画
        self.assertFalse(flat["empty"])
        one = df.plot_layout(_rec(time=[1.0], raw=[2.0]), "raw", 600, 400)
        self.assertEqual(len(one["line"]), 4)
        nan = df.plot_layout(_rec(raw=[float("nan"), 1.0, 2.0, float("inf"), 3.0]), "raw", 600, 400)
        self.assertEqual(nan["points"], 3)                                               # 非数值点被跳过

    def test_record_signature(self):
        self.assertNotEqual(df.record_signature(_rec()), df.record_signature(_rec(cycle_id=8)))
        self.assertIsNone(df.record_signature(None))


class _FakeApp:
    current = None


class _FakeSA:
    """只提供 RecordSource 用到的接口。"""

    def __init__(self):
        self.OpcUaApp = _FakeApp
        self.calls = 0

    def gather_snapshot(self, dirs, last_n, max_points, cache=None):
        self.calls += 1
        return [_rec()]


class Records(unittest.TestCase):
    def tearDown(self):
        _FakeApp.current = None

    def test_service_snapshot_preferred(self):
        fsa = _FakeSA()
        app = _Obj(latest_records=[_rec(), _rec(det=2)], revision=4)
        _FakeApp.current = app
        recs, origin, note = df.RecordSource(fsa).get(True, dirs=["/x"])
        self.assertEqual((len(recs), origin), (2, "service"))
        self.assertIn("4", note)
        self.assertEqual(fsa.calls, 0)               # 服务运行中不额外读文件

    def test_service_up_but_no_app_falls_back_to_dirs(self):
        fsa = _FakeSA()
        tmp = tempfile.mkdtemp()
        try:
            recs, origin, _n = df.RecordSource(fsa).get(True, dirs=[tmp])
            self.assertEqual(origin, "dir")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_dir_reads_are_throttled(self):
        fsa = _FakeSA()
        clock = FakeClock()
        tmp = tempfile.mkdtemp()
        try:
            src = df.RecordSource(fsa, dir_interval=30, clock=clock)
            src.get(False, dirs=[tmp])
            clock.t += 5
            src.get(False, dirs=[tmp])
            self.assertEqual(fsa.calls, 1)           # 30 秒内复用
            src.get(False, dirs=[tmp], force=True)
            self.assertEqual(fsa.calls, 2)           # “立即刷新”
            clock.t += 31
            src.get(False, dirs=[tmp])
            self.assertEqual(fsa.calls, 3)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_no_dirs_or_missing_dirs(self):
        self.assertEqual(df.RecordSource(_FakeSA()).get(False, dirs=[])[1], "none")
        recs, origin, note = df.RecordSource(_FakeSA()).get(False, dirs=["/没有这个目录/x"])
        self.assertEqual((recs, origin), ([], "none"))
        self.assertIn("找不到", note)

    def test_reads_demo_database_directly(self):
        """服务没运行：用与服务相同的 gather_snapshot 直接解析数据目录。"""
        tmp = tempfile.mkdtemp()
        try:
            demo_data.make_demo_db(os.path.join(tmp, "demo_V3.db"))
            recs, origin, note = df.RecordSource(sa).get(False, dirs=[tmp], max_points=500)
            self.assertEqual(origin, "dir")
            self.assertGreater(len(recs), 0)
            r = recs[0]
            self.assertLessEqual(len(r["raw"]), 500)
            lay = df.plot_layout(r, "uv", 900, 500)
            self.assertFalse(lay["empty"])
            self.assertIn("demo_V3.db", df.channel_key(r))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 2. 通道实时数据表
# ---------------------------------------------------------------------------

class ChannelTable(unittest.TestCase):
    def test_rows_with_alerts_and_mute(self):
        a, b, c = _rec(det=1), _rec(det=2), _rec(det=3)
        ka, kb, kc = df.channel_key(a), df.channel_key(b), df.channel_key(c)
        snap = {"active": [{"key": "stale:" + ka, "level": "警告", "text": "已 130 分钟没有新周期"}],
                "channel_list": [{"key": ka, "idle_min": 130, "present": True, "muted": False},
                                 {"key": kb, "idle_min": 0, "present": True, "muted": True},
                                 {"key": kc, "idle_min": 3, "present": False, "muted": False}]}
        rows = {r["key"]: r for r in df.channel_rows([a, b, c], snap, {}, "service", now=1_800_000_000)}
        self.assertEqual(rows[ka]["tag"], "alert")
        self.assertIn("130 分钟没有新周期", rows[ka]["values"][6])
        self.assertIn("130 分钟前", rows[ka]["values"][7])
        self.assertEqual(rows[kb]["values"][6], "已屏蔽告警")
        self.assertIn("刚刚", rows[kb]["values"][7])
        self.assertIn("消失", rows[kc]["values"][6])
        v = rows[ka]["values"]
        self.assertEqual(v[1:4], ("2026-11-26 10:00:00", 7, 5))
        self.assertEqual(v[4], "0.5 s")
        self.assertEqual(v[5], "2.0 s")
        self.assertEqual(len(df.CHANNEL_COLUMNS), len(v))

    def test_offline_rows_use_tracker_time(self):
        tr = df.ChannelTracker(clock=FakeClock(1_800_000_000))
        upd = tr.update([_rec()])
        rows = df.channel_rows([_rec()], None, upd, "dir")
        self.assertEqual(rows[0]["tag"], "offline")
        self.assertIn("服务未运行", rows[0]["values"][6])
        self.assertNotEqual(rows[0]["values"][7], "-")

    def test_tracker_marks_new_cycle(self):
        clock = FakeClock(100)
        tr = df.ChannelTracker(clock=clock)
        k = df.channel_key(_rec())
        self.assertEqual(tr.update([_rec()])[k], 100)
        clock.t = 200
        self.assertEqual(tr.update([_rec()])[k], 100)                 # 同一周期不变
        self.assertEqual(tr.update([_rec(cycle_id=8)])[k], 200)       # 新周期
        self.assertEqual(tr.update([]), {})

    def test_duration_format(self):
        self.assertEqual(df.fmt_duration(300), "300.0 s（5.0 分钟）")
        self.assertEqual(df.fmt_duration(None), "-")
        self.assertEqual(df.fmt_num("x"), "-")


# ---------------------------------------------------------------------------
# 3. 常用配置
# ---------------------------------------------------------------------------

class QuickSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "config.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"poll_seconds": 30, "endpoint": "opc.tcp://0.0.0.0:4840/ChromExtract"}, f)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _orig(self):
        view = dict(sa.DEFAULT_CONFIG)
        view.update(ct.read_raw(self.cfg))
        return view

    def test_endpoint_port(self):
        self.assertEqual(df.endpoint_port("opc.tcp://0.0.0.0:4840/ChromExtract"), 4840)
        self.assertEqual(df.endpoint_port("opc.tcp://host/ChromExtract"), 4840)
        self.assertEqual(df.endpoint_port("opc.tcp://[::1]:4999/x"), 4999)
        self.assertIsNone(df.endpoint_port("http://x"))
        self.assertEqual(df.endpoint_with_port("opc.tcp://0.0.0.0:4840/ChromExtract", 4841),
                         "opc.tcp://0.0.0.0:4841/ChromExtract")
        self.assertEqual(df.endpoint_with_port("opc.tcp://myhost/ChromExtract", 5000),
                         "opc.tcp://myhost:5000/ChromExtract")
        with self.assertRaises(df.QuickSettingError):
            df.endpoint_with_port("tcp://bad", 1)

    def test_form_values_roundtrip_no_changes(self):
        orig = self._orig()
        vals = df.quick_form_values(orig)
        self.assertEqual(vals["opc_port"], "4840")
        self.assertIs(vals["desktop_notify"], True)
        self.assertEqual(df.quick_updates(vals, orig), {})

    def test_changes_and_port_rewrites_endpoint(self):
        orig = self._orig()
        vals = df.quick_form_values(orig)
        vals.update(opc_port="4850", poll_seconds=" 60 ", desktop_notify=False, desktop_notify_level="critical")
        upd = df.quick_updates(vals, orig)
        self.assertEqual(upd, {"endpoint": "opc.tcp://0.0.0.0:4850/ChromExtract", "poll_seconds": 60,
                               "desktop_notify": False, "desktop_notify_level": "critical"})

    def test_range_errors_listed_together(self):
        orig = self._orig()
        vals = df.quick_form_values(orig)
        vals.update(poll_seconds="1", web_port="abc", csv_export_dir="", opc_port="70000")
        with self.assertRaises(df.QuickSettingError) as cm:
            df.quick_updates(vals, orig)
        msg = str(cm.exception)
        for part in ("扫描周期", "5～3600", "设置网页端口", "必须是整数", "CSV 导出目录", "OPC UA 端口"):
            self.assertIn(part, msg)

    def test_ranges_match_normalize_config(self):
        """表单的范围与 normalize_config 的夹取范围一致（边界值保存后不会被悄悄改掉）。"""
        for key, _label, kind, opt, _h in df.QUICK_FIELDS:
            if kind != "int" or key == "opc_port":
                continue
            lo, hi = opt
            for v in (lo, hi):
                cfg = dict(sa.DEFAULT_CONFIG, **{key: v})
                sa.normalize_config(cfg, self.tmp)
                self.assertEqual(cfg[key], v, key)
            cfg = dict(sa.DEFAULT_CONFIG, **{key: hi + 1})
            sa.normalize_config(cfg, self.tmp)
            self.assertEqual(cfg[key], hi, key)

    def test_save_classifies_hot_restart_desktop(self):
        upd = {"poll_seconds": 45, "endpoint": "opc.tcp://0.0.0.0:4850/ChromExtract", "web_port": 8850,
               "csv_export_dir": "导出2", "desktop_notify": False}
        res = df.save_quick_settings(self.cfg, self.tmp, sa, upd)
        self.assertEqual(sorted(res["changed"]), sorted(upd))
        self.assertEqual(res["hot"], ["poll_seconds"])
        self.assertEqual(sorted(res["restart"]), sorted(["endpoint", "web_port", "csv_export_dir"]))
        self.assertEqual(res["desktop"], ["desktop_notify"])
        self.assertIn("自动生效", res["message"])
        self.assertIn("需要重启服务", res["message"])
        self.assertIn("OPC UA 端口", res["message"])
        saved = ct.read_raw(self.cfg)
        self.assertEqual(saved["poll_seconds"], 45)
        self.assertIs(saved["desktop_notify"], False)

    def test_invalid_rejected_file_untouched(self):
        before = open(self.cfg, "rb").read()
        with self.assertRaises(ct.ConfigToolError):
            df.save_quick_settings(self.cfg, self.tmp, sa, {"endpoint": "http://wrong"})
        with self.assertRaises(ct.ConfigToolError):
            df.save_quick_settings(self.cfg, self.tmp, sa, {"desktop_notify_level": "warning"})
        self.assertEqual(open(self.cfg, "rb").read(), before)

    def test_no_change(self):
        res = df.save_quick_settings(self.cfg, self.tmp, sa, {})
        self.assertEqual((res["changed"], res["message"]), ([], "没有改动。"))

    def test_logic_save_quick(self):
        import desktop_ui as du
        logic = du.DesktopLogic(self.cfg, self.tmp, sa)
        try:
            vals = df.quick_form_values(logic.state()["config"])
            vals["last_n"] = "8"
            res = logic.save_quick(vals)
            self.assertEqual(res["changed"], ["last_n"])
            vals["last_n"] = "0"
            with self.assertRaises(df.QuickSettingError):
                logic.save_quick(vals)
        finally:
            logic.stop_stability()

    def test_startup_behavior(self):
        self.assertEqual(df.startup_behavior({}, True), (True, False))
        self.assertEqual(df.startup_behavior({"desktop_auto_start_service": False}, True), (False, False))
        self.assertEqual(df.startup_behavior({"desktop_start_minimized": True}, False), (False, True))


class NewConfigKeys(unittest.TestCase):
    KEYS = ("desktop_notify", "desktop_notify_level", "desktop_notify_quiet",
            "desktop_auto_start_service", "desktop_start_minimized")

    def test_defaults_editable_and_shipped(self):
        for k in self.KEYS:
            self.assertIn(k, sa.DEFAULT_CONFIG)
            self.assertIn(k, ct.EDITABLE)
            self.assertIn(k, sa.DESKTOP_KEYS)
            self.assertIn(k, df.DESKTOP_KEYS)
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8-sig") as f:
            shipped = json.load(f)
        for k in self.KEYS:
            self.assertEqual(shipped[k], sa.DEFAULT_CONFIG[k], k)

    def test_normalize(self):
        cfg = sa.normalize_config(dict(sa.DEFAULT_CONFIG, desktop_notify="false", desktop_notify_level="CRITICAL",
                                       desktop_start_minimized=1), ROOT)
        self.assertIs(cfg["desktop_notify"], False)
        self.assertIs(cfg["desktop_start_minimized"], True)
        self.assertEqual(cfg["desktop_notify_level"], "critical")
        for bad in ({"desktop_notify": "maybe"}, {"desktop_auto_start_service": [1]},
                    {"desktop_notify_level": "warning"}):
            with self.assertRaises(sa.ConfigError):
                sa.normalize_config(dict(sa.DEFAULT_CONFIG, **bad), ROOT)

    def test_desktop_keys_not_reported_as_service_settings(self):
        for k in self.KEYS:
            self.assertNotIn(k, sa.HOT_KEYS)
            self.assertNotIn(k, sa.RESTART_KEYS)


# ---------------------------------------------------------------------------
# 4. 客户端连接
# ---------------------------------------------------------------------------

class _Obj:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


class _Boom:
    """访问任何属性都抛异常：模拟 asyncua 版本差异 / 对象状态异常。"""

    def __getattr__(self, name):
        raise RuntimeError("boom")


class Sessions(unittest.TestCase):
    def _app(self, clients):
        role = _Obj(name="User")
        sess1 = _Obj(name="UaExpert@PC1", user=_Obj(name="dcs", role=role), state=_Obj(name="Activated"))
        sess2 = _Obj(name="Client2", user=_Obj(name=None, role=_Obj(name="User")), state=None)
        protos = [_Obj(peer_name=("192.168.1.20", 50123), processor=_Obj(session=sess1)),
                  _Obj(peer_name=("192.168.1.21", 50200), processor=_Obj(session=sess2)),
                  _Obj(peer_name=("::1", 6000, 0, 0), processor=_Obj(session=None))][:clients]
        server = _Obj(bserver=_Obj(clients=protos), iserver=_Obj(max_connections=20))
        return _Obj(server=server, cfg={"max_connections": 20})

    def test_collect(self):
        res = df.collect_sessions(self._app(3))
        self.assertTrue(res["ok"])
        self.assertEqual((res["count"], res["max"]), (3, 20))
        s = res["sessions"]
        self.assertEqual(s[0]["address"], "192.168.1.20:50123")
        self.assertEqual(s[0]["name"], "UaExpert@PC1")
        self.assertEqual(s[0]["user"], "dcs")
        self.assertEqual(s[0]["state"], "Activated")
        self.assertEqual(s[1]["user"], "匿名（只读）")         # ChromUserManager：匿名 = User 角色、没有用户名
        self.assertEqual(df._user_text(_Obj(name=None, role=_Obj(name="Anonymous"))), "匿名（只读）")
        self.assertEqual(df._user_text(_Obj(name="ops", role=_Obj(name="Admin"))), "ops（Admin）")
        self.assertEqual(df._user_text(None), "无法获取")
        self.assertEqual(s[2]["address"], "[::1]:6000")
        self.assertIn("尚未建立会话", s[2]["name"])
        self.assertIn("3 / 上限 20", df.sessions_summary(res))

    def test_not_running(self):
        res = df.collect_sessions(None)
        self.assertFalse(res["ok"])
        self.assertIn("服务未运行", df.sessions_summary(res))

    def test_unknown_asyncua_layout(self):
        res = df.collect_sessions(_Obj(server=_Obj(), cfg={"max_connections": 5}))
        self.assertFalse(res["ok"])
        self.assertIn("无法获取", df.sessions_summary(res))

    def test_transport_fallback(self):
        tr = _Obj(get_extra_info=lambda k: ("10.0.0.5", 1234))
        app = _Obj(server=_Obj(iserver=_Obj(asyncio_transports=[tr])), cfg={"max_connections": 5})
        res = df.collect_sessions(app)
        self.assertTrue(res["ok"])
        self.assertEqual(res["sessions"][0]["address"], "10.0.0.5:1234")
        self.assertEqual(res["sessions"][0]["name"], "无法获取")
        self.assertEqual(res["max"], 5)

    def test_hostile_objects_do_not_crash(self):
        for app in (_Boom(), _Obj(server=_Boom(), cfg={}),
                    _Obj(server=_Obj(bserver=_Obj(clients=[_Boom(), _Obj(processor=_Boom())])), cfg={})):
            res = df.collect_sessions(app)
            self.assertIn("ok", res)
            df.sessions_summary(res)

    def test_conn_tracker_since(self):
        clock = FakeClock(1_800_000_000)
        tr = df.ConnTracker(clock=clock)
        app = self._app(2)
        res = tr.apply(df.collect_sessions(app))
        first = res["sessions"][0]["since"]
        clock.t += 100
        res = tr.apply(df.collect_sessions(app))
        self.assertEqual(res["sessions"][0]["since"], first)       # 同一连接，时间不变
        res = tr.apply(df.collect_sessions(self._app(2)))           # 新连接（新对象）：重新计时
        self.assertNotEqual(res["sessions"][0]["since"], first)

    def test_service_app_lookup(self):
        self.assertIsNone(df.service_app(_Obj()))
        self.assertIsNone(df.service_app(_Obj(OpcUaApp=_Obj())))
        app = object()
        self.assertIs(df.service_app(_Obj(OpcUaApp=_Obj(current=app))), app)


# ---------------------------------------------------------------------------
# 5. 告警桌面提醒
# ---------------------------------------------------------------------------

def _snap(*alerts, quiet=None, offline=False):
    return {"active": [{"key": k, "level": lv, "text": t, "since": ""} for k, lv, t in alerts],
            "quiet": quiet, "offline": offline}


class Notify(unittest.TestCase):
    CFG = {"desktop_notify": True, "desktop_notify_level": "all", "desktop_notify_quiet": True}

    def test_only_new_alerts(self):
        tr = df.AlertNotifyTracker()
        self.assertEqual(len(tr.update(_snap(("a", "警告", "x")), self.CFG)), 1)
        self.assertEqual(tr.update(_snap(("a", "警告", "x")), self.CFG), [])            # 同一条不重复提醒
        new = tr.update(_snap(("a", "警告", "x"), ("b", "严重", "y")), self.CFG)
        self.assertEqual([a["key"] for a in new], ["b"])
        tr.update(_snap(("b", "严重", "y")), self.CFG)                                 # a 恢复
        self.assertEqual([a["key"] for a in tr.update(_snap(("a", "警告", "x"), ("b", "严重", "y")), self.CFG)],
                         ["a"])                                                         # a 再次出现：再提醒

    def test_disabled_level_and_quiet(self):
        tr = df.AlertNotifyTracker()
        self.assertEqual(tr.update(_snap(("a", "警告", "x")), dict(self.CFG, desktop_notify=False)), [])
        tr = df.AlertNotifyTracker()
        new = tr.update(_snap(("a", "警告", "x"), ("b", "严重", "y")), dict(self.CFG, desktop_notify_level="critical"))
        self.assertEqual([a["key"] for a in new], ["b"])
        tr = df.AlertNotifyTracker()
        self.assertEqual(tr.update(_snap(("a", "严重", "x"), quiet={"until": "18:00"}), self.CFG), [])
        self.assertEqual(tr.suppressed, 1)
        self.assertEqual(tr.update(_snap(("a", "严重", "x")), self.CFG), [])          # 静默结束不补弹
        tr = df.AlertNotifyTracker()
        self.assertEqual(len(tr.update(_snap(("a", "严重", "x"), quiet={"until": "18:00"}),
                                       dict(self.CFG, desktop_notify_quiet=False))), 1)

    def test_offline_resets(self):
        tr = df.AlertNotifyTracker()
        tr.update(_snap(("a", "警告", "x")), self.CFG)
        self.assertEqual(tr.update(_snap(offline=True), self.CFG), [])
        self.assertEqual(len(tr.update(_snap(("a", "警告", "x")), self.CFG)), 1)      # 服务重启后重新提醒

    def test_text_limits(self):
        alerts = [{"key": str(i), "level": "严重" if i == 0 else "警告", "text": "很长的告警" * 40} for i in range(5)]
        title, body, level = df.notification_text(alerts)
        self.assertLessEqual(len(title), 63)
        self.assertLessEqual(len(body), 250)
        self.assertEqual(level, "error")
        self.assertIn("5 项", title)
        _t, body2, level2 = df.notification_text([{"key": "a", "level": "警告", "text": "x"}])
        self.assertEqual((body2, level2), ("[警告] x", "warning"))

    def test_deliver_chain(self):
        calls = []

        def fail(t, x, lv):
            calls.append("fail")
            return False

        def boom(t, x, lv):
            raise RuntimeError("x")

        def ok(t, x, lv):
            calls.append("ok")
            return True
        self.assertEqual(df.deliver_notification("t", "x", "warning", [("a", fail), ("b", boom), ("c", ok)]), "c")
        self.assertEqual(calls, ["fail", "ok"])
        self.assertIsNone(df.deliver_notification("t", "x", "warning", [("a", fail)]))

    @unittest.skipIf(os.name == "nt", "非 Windows 上验证：Windows 专用调用安全返回 False")
    def test_windows_calls_are_safe_elsewhere(self):
        self.assertFalse(df.flash_window(1234))
        tb = df.TrayBalloon(1234)
        self.assertFalse(tb.show("t", "x"))
        tb.close()
        self.assertFalse(df.powershell_toast("t", "x"))

    def test_powershell_quoting(self):
        self.assertEqual(df.ps_quote("a'b"), "'a''b'")
        self.assertEqual(df.ps_quote("x\ny\x00"), "'x y '")
        self.assertNotIn("\u2019", df.ps_quote("it\u2019s"))
        script = df.toast_script("标题'; Remove-Item C:\\ -Recurse; '", "正文")
        self.assertIn("'标题''; Remove-Item C:\\ -Recurse; '''", script)
        import base64
        self.assertEqual(base64.b64decode(df.ps_encoded("中文")).decode("utf-16-le"), "中文")


# ---------------------------------------------------------------------------
# 6. 登录后自动打开窗口
# ---------------------------------------------------------------------------

class LoginShortcut(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.env = {"APPDATA": os.path.join(self.tmp, "AppData", "Roaming")}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_startup_dir(self):
        d = df.startup_dir(self.env)
        self.assertEqual(d, os.path.join(self.env["APPDATA"], "Microsoft", "Windows", "Start Menu", "Programs",
                                         "Startup"))
        self.assertEqual(os.path.basename(df.startup_shortcut_path(self.env)), "色谱提取 OPC UA 服务.lnk")
        self.assertIsNone(df.startup_dir({}))
        self.assertIsNone(df.startup_shortcut_path({}))

    def test_shortcut_name_matches_installer(self):
        iss = open(os.path.join(ROOT, "installer", "ChromExtractOPC.iss"), encoding="utf-8-sig").read()
        self.assertIn('#define MyAppName "%s"' % df.SHORTCUT_NAME[:-4], iss)

    def test_target_frozen_and_source(self):
        exe = os.path.join("C:\\", "App", "ChromExtractOPC.exe")
        self.assertEqual(df.shortcut_target(True, exe, "unused", exists=lambda p: True)[:3],
                         (exe, "", os.path.dirname(exe)))
        cli = os.path.join(os.path.dirname(exe), "ChromExtractOPC-cli.exe")
        self.assertEqual(df.shortcut_target(True, cli, "unused", exists=lambda p: True)[0], exe)   # 指向窗口版
        self.assertEqual(df.shortcut_target(True, cli, "unused", exists=lambda p: False)[0], cli)
        py = os.path.join("C:\\", "Py", "python.exe")
        t, args, wd, _icon = df.shortcut_target(False, py, os.path.join("D:\\", "src"), exists=lambda p: True)
        self.assertTrue(t.endswith("pythonw.exe"))                  # 源码运行不弹黑窗口
        self.assertIn("server_app.py", args)
        self.assertEqual(wd, os.path.join("D:\\", "src"))
        t2 = df.shortcut_target(False, py, "D:\\src", exists=lambda p: False)[0]
        self.assertEqual(t2, py)
        self.assertEqual(len(df.current_shortcut_target(sa)), 4)

    def test_enable_disable_with_fake_runner(self):
        lnk = df.startup_shortcut_path(self.env)
        scripts = []

        def runner(script):
            scripts.append(script)
            os.makedirs(os.path.dirname(lnk), exist_ok=True)
            open(lnk, "wb").close()
            return 0, ""
        info = ("C:\\App\\ChromExtractOPC.exe", "", "C:\\App", "C:\\App\\ChromExtractOPC.exe")
        self.assertFalse(df.login_open_enabled(lnk))
        msg = df.set_login_open(True, info, lnk, runner=runner, windows=True)
        self.assertIn("已开启", msg)
        self.assertIn("不是后台服务", msg)
        self.assertTrue(df.login_open_enabled(lnk))
        self.assertIn("WScript.Shell", scripts[0])
        self.assertIn("'C:\\App\\ChromExtractOPC.exe'", scripts[0])
        self.assertNotIn("schtasks", scripts[0].lower())
        self.assertNotIn("Register-ScheduledTask", scripts[0])
        msg = df.set_login_open(False, info, lnk, windows=True)
        self.assertIn("已删除", msg)
        self.assertFalse(df.login_open_enabled(lnk))
        self.assertIn("本来就没有", df.set_login_open(False, info, lnk, windows=True))

    def test_failures(self):
        lnk = df.startup_shortcut_path(self.env)
        info = ("a", "", "b", "a")
        with self.assertRaises(df.DesktopFeatureError):
            df.set_login_open(True, info, lnk, runner=lambda s: (1, "denied"), windows=True)
        with self.assertRaises(df.DesktopFeatureError):
            df.set_login_open(True, info, lnk, runner=lambda s: (0, ""), windows=True)    # 没生成文件
        with self.assertRaises(df.DesktopFeatureError):
            df.set_login_open(True, info, lnk, runner=lambda s: 1 / 0, windows=True)
        with self.assertRaises(df.DesktopFeatureError):
            df.set_login_open(True, info, lnk, windows=False)
        with self.assertRaises(df.DesktopFeatureError):
            df.set_login_open(True, info, "", windows=True)          # 找不到启动文件夹

    def test_shortcut_script_quoting(self):
        s = df.shortcut_script("C:\\U\\it's.lnk", "C:\\A B\\x.exe", '"y.py"', "C:\\A B", "C:\\A B\\x.exe")
        self.assertIn("'C:\\U\\it''s.lnk'", s)
        self.assertIn("$s.Arguments = '\"y.py\"'", s)
        self.assertIn("$s.Save()", s)


if __name__ == "__main__":
    unittest.main()
