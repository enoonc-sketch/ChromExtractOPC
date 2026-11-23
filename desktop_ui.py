#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
desktop_ui.py —— 桌面设置窗口（tkinter），与浏览器设置页面一一对应（v2.6 新增）

设计：
  - 逻辑层 DesktopLogic 直接复用 web_settings.SettingsServer 的动作/状态方法
    （只构造对象、不启动 HTTP 服务）：保存仍走 config_tools.update_settings
    （校验、备份、原子替换，密码只存哈希），校验规则与服务启动时完全一致，
    告警/设备检测/连接测试/稳定性测试也走同一套后端，行为与网页版相同。
  - 窗口层用 tkinter + ttk.Notebook，四个标签页与网页版一致：
    运行状态 / 告警与通知 / 诊断工具 / 系统设置，外加原图形窗口的日志窗和
    数据传输检测 / 发送测试数据 / 智能诊断 / 485配型诊断 / 安全设置按钮。
  - 本模块 import 时不创建任何窗口；只有 run_desktop() 被调用才建窗口，
    无显示环境下 import 不报错（方便自动测试只测逻辑层）。
  - 网页服务（web_settings.py）保留：服务运行时仍可按 web_ui 配置开启，
    供远程/浏览器访问与自检使用；桌面窗口是本机默认入口。
"""
import os
import threading

import config_tools
import web_settings

# 与网页版 F 完全一致的字段分组（保存时只提交改动过的项）
STR_FIELDS = ["endpoint", "server_name", "security", "csv_export_dir", "web_host", "web_https",
              "web_cert_file", "web_key_file", "alert_webhook", "alert_email_to", "smtp_host",
              "smtp_security", "smtp_user", "smtp_from", "alert_webhook_level", "alert_email_level"]
NUM_FIELDS = ["poll_seconds", "last_n", "max_points", "max_connections", "export_min_interval_sec",
              "max_trace_points", "max_file_mb", "log_max_mb", "log_backups", "web_port",
              "alert_stale_minutes", "alert_scan_errors", "smtp_port", "env_check_hours"]
BOOL_FIELDS = ["allow_anonymous", "allow_insecure_fallback", "remote_export", "web_ui",
               "env_check_on_start"]
ALL_FIELDS = STR_FIELDS + NUM_FIELDS + BOOL_FIELDS + ["watch_dirs"]

# 连接测试的种类与参数（与网页版 NT/SER 一致）：(键, 标签, 默认值, 类型)
SERIAL_FIELDS = [("port", "串口", "COM3", "port"), ("baud", "波特率", "9600", "baud"),
                 ("bytesize", "数据位", "8", "bits"), ("parity", "校验", "N", "parity"),
                 ("stopbits", "停止位", "1", "stop")]
NETTEST_SPECS = {
    "ping": {"t": "Ping", "f": [("host", "目标地址", "192.168.1.10", ""),
                                ("count", "次数", "4", ""), ("timeout", "每次超时(秒)", "1", "")]},
    "tcp": {"t": "TCP 端口", "f": [("host", "目标地址", "192.168.1.10", ""), ("port", "端口", "4840", ""),
                                   ("timeout", "超时(秒)", "3", ""), ("send_text", "发送文本（可选）", "", "")]},
    "dns": {"t": "DNS 解析", "f": [("host", "主机名", "dcs-server", "")]},
    "interfaces": {"t": "本机网卡", "f": []},
    "opcua": {"t": "OPC UA", "f": [("endpoint", "服务器地址", "opc.tcp://127.0.0.1:4840/ChromExtract", ""),
                                   ("username", "用户名（可选）", "", ""),
                                   ("password", "密码（可选）", "", "password"),
                                   ("timeout", "超时(秒)", "5", "")]},
    "modbus_tcp": {"t": "Modbus TCP", "f": [("host", "设备地址", "192.168.1.20", ""), ("port", "端口", "502", ""),
                                            ("unit", "站号", "1", ""), ("func", "功能码", "3", "fc"),
                                            ("addr", "起始地址（从 0 开始）", "0", ""),
                                            ("count", "数量", "10", ""), ("timeout", "超时(秒)", "3", "")]},
    "serial_list": {"t": "串口列表", "f": []},
    "serial": {"t": "串口收发 232/485", "f": "S",
               "x": [("send", "发送内容（留空 = 只监听）", "01 03 00 00 00 0A C5 CD", ""),
                     ("mode", "格式", "hex", "mode"), ("timeout", "接收等待(秒)", "1", ""),
                     ("rs485_rts", "RTS 控制收发方向（无自动切换的 485 转换器）", "", "chk")]},
    "loopback": {"t": "串口回环自检", "f": "S", "x": [("timeout", "超时(秒)", "1", "")],
                 "tip": "先把串口 TX 和 RX 短接（DB9 的 2、3 脚），用于确认串口和转换器本身是好的。RS-485 半双工不适用。"},
    "modbus_rtu": {"t": "Modbus RTU", "f": "S",
                   "x": [("unit", "站号", "1", ""), ("func", "功能码", "3", "fc"),
                         ("addr", "起始地址（从 0 开始）", "0", ""), ("count", "数量", "10", ""),
                         ("timeout", "超时(秒)", "1", ""), ("rs485_rts", "RTS 控制收发方向", "", "chk")]},
}
STAB_KINDS = ["ping", "tcp", "dns", "opcua", "modbus_tcp", "modbus_rtu", "serial"]
PROBE_KINDS = STAB_KINDS
SELECT_OPTIONS = {
    "security": [("none", "none（不加密）"), ("sign", "sign（签名）"),
                 ("sign_encrypt", "sign_encrypt（签名+加密，推荐）")],
    "alert_webhook_level": [("all", "严重和警告都发"), ("critical", "只发严重告警"), ("off", "不发")],
    "alert_email_level": [("all", "严重和警告都发"), ("critical", "只发严重告警"), ("off", "不发")],
    "smtp_security": [("ssl", "SSL（一般 465）"), ("starttls", "STARTTLS（一般 587）"),
                      ("none", "不加密（仅内网免登录中继，25）")],
    "web_https": [("auto", "auto（对外开放时自动启用，推荐）"), ("on", "on（始终启用）"),
                  ("off", "off（不启用）")],
}
RESTART_TAG = "（需重启）"


def nettest_fields(kind):
    """某种连接测试的完整字段表（串口类自动拼上串口参数）。"""
    spec = NETTEST_SPECS[kind]
    return list(SERIAL_FIELDS) + list(spec["x"]) if spec["f"] == "S" else list(spec["f"])


def build_nettest_params(kind, raw):
    """从表单原始值拼连接测试参数：只取该种类声明过的键，勾选项转 bool。"""
    params = {}
    for key, _label, _default, typ in nettest_fields(kind):
        if key not in raw:
            continue
        val = raw[key]
        params[key] = bool(val) if typ == "chk" else val
    return params


def collect_settings(values, orig):
    """对比表单值与原配置，只返回改动过的项（与网页版 save() 相同的比较规则）。

    values: {字段: 字符串/bool，watch_dirs 可为列表或多行文本}
    orig:   state()["config"]（合并默认值后的视图）
    """
    changed = {}
    for k in STR_FIELDS + NUM_FIELDS:
        if k in values and str(values[k]) != str(orig.get(k, "")):
            changed[k] = values[k]
    for k in BOOL_FIELDS:
        if k in values and bool(values[k]) != bool(orig.get(k)):
            changed[k] = bool(values[k])
    if "watch_dirs" in values:
        wd = values["watch_dirs"]
        if isinstance(wd, str):
            wd = [x.strip() for x in wd.split("\n") if x.strip()]
        if list(wd) != list(orig.get("watch_dirs") or []):
            changed["watch_dirs"] = list(wd)
    return changed


def quiet_once_item(start, end, note=""):
    """一次性静默时段的条目（start/end 为 'YYYY-MM-DD HH:MM'）。"""
    return {"type": "once", "start": (start or "").strip(), "end": (end or "").strip(),
            "note": (note or "").strip()}


def quiet_daily_item(start, end, days, note=""):
    """每天重复静默时段的条目（days 为 1..7 的列表，空 = 每天）。"""
    return {"type": "daily", "start": (start or "").strip(), "end": (end or "").strip(),
            "days": [int(d) for d in (days or [])], "note": (note or "").strip()}


def channel_rule_body(channel, mute, stale_minutes, until, note):
    """通道告警规则的提交体（与网页版 channel_rule 相同形状）。"""
    return {"channel": channel, "mute": bool(mute),
            "stale_minutes": ("" if stale_minutes in (None, "") else stale_minutes),
            "until": (until or "").strip(), "note": (note or "").strip()}


def probe_body(edit_id, label, kind, params, interval, fail_threshold, level, enabled=True):
    """设备检测的提交体（与网页版 probe_save 相同形状）。"""
    return {"id": edit_id or "", "label": (label or "").strip(), "kind": kind,
            "params": dict(params or {}), "interval": interval,
            "fail_threshold": fail_threshold, "level": level, "enabled": bool(enabled)}


class DesktopLogic:
    """桌面窗口的逻辑层：复用 SettingsServer 的动作与状态方法（不启动 HTTP）。

    cfg_mod 是 server_app 模块（提供 DEFAULT_CONFIG / load_config / normalize_config /
    security_report / HOT_KEYS / RESTART_KEYS / ALERTS 等）。
    """

    def __init__(self, cfg_path, base_dir, cfg_mod, log=None):
        self.cfg_path, self.base_dir, self.m = cfg_path, base_dir, cfg_mod
        self.srv = web_settings.SettingsServer(cfg_path, base_dir, cfg_mod, log=log)

    # ---- 状态读取 ----
    def state(self):
        st = self.srv.state()
        st["alerts"] = self.srv.alerts()
        return st

    def alerts(self):
        return self.srv.alerts()

    def probes(self):
        return self.srv.probe_status()

    def env(self):
        return self.srv.env_status()

    def stability(self):
        return self.srv.stab_status()

    # ---- 动作（与网页版 handle_action 完全相同）----
    def action(self, name, body=None):
        return self.srv.handle_action(name, body or {})

    def save(self, values):
        """保存表单：只提交改动项，校验不过抛 ConfigToolError 且不写文件。返回提示文字。"""
        changed = collect_settings(values, self.srv.state()["config"])
        if not changed:
            return "没有改动。"
        return self.srv.handle_action("save", {"settings": changed})

    def nettest(self, kind, params):
        return self.srv.handle_action("nettest", {"kind": kind, "params": params})

    def stop_stability(self):
        self.srv.stop()     # 只停稳定性测试；HTTP 未启动，stop() 里 httpd 为 None

    def stab_csv_path(self, name):
        return os.path.join(self.srv._stab_dir(), name) if name else ""


# ---------------------------------------------------------------------------
# 窗口层（tkinter）。以下代码只在 run_desktop() 调用时执行，import 本模块不建窗口。
# ---------------------------------------------------------------------------

def _scrollable(parent, tk, ttk):
    """在 parent 里放一个可滚动区域，返回内层 Frame。"""
    canvas = tk.Canvas(parent, highlightthickness=0)
    bar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
    inner = tk.Frame(canvas)
    inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
    win = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
    canvas.configure(yscrollcommand=bar.set)
    canvas.pack(side="left", fill="both", expand=True)
    bar.pack(side="right", fill="y")

    def _wheel(e):
        canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")
    canvas.bind_all("<MouseWheel>", _wheel, add="+")
    return inner


class DesktopWindow:
    """桌面主窗口：四标签页 + 日志窗。start_service=True 时同时在后台线程启动 OPC UA 服务。"""

    def __init__(self, cfg, start_service=True, background_note=""):
        import queue
        import tkinter as tk
        from tkinter import messagebox, scrolledtext, simpledialog, ttk

        import server_app as sa

        self.tk, self.ttk, self.mb, self.sd = tk, ttk, messagebox, simpledialog
        self.sa = sa
        self.cfg = cfg
        self.start_service = start_service
        self.logic = DesktopLogic(cfg["_cfg_path"], cfg["_base_dir"], sa, log=sa.get_log())
        self._q = queue.Queue()
        self._orig = {}
        self._vars = {}          # 字段 -> tk Variable（系统/告警两页的配置表单）
        self._nt_vars = {}       # 连接测试当前表单
        self._nt_kind = "ping"
        self._nt_ports = []
        self._pr_list = []
        self._pr_editing = ""
        self._mt_list = []
        self._dc_shown_time = ""
        self._closed = False

        sa._hide_own_console()
        root = tk.Tk()
        self.root = root
        root.title("色谱提取 OPC UA 服务 v%s" % sa.__version__)
        sa._set_window_icon(root)
        root.geometry("980x720")
        root.minsize(860, 600)

        # 顶部状态栏 + 快捷按钮（沿用原图形窗口）
        self.status = tk.StringVar(value="启动中…" if start_service else (background_note or "服务未运行（只打开了桌面设置窗口）"))
        tk.Label(root, textvariable=self.status, anchor="w", fg="blue",
                 font=("Microsoft YaHei", 11)).pack(fill="x", padx=10, pady=(8, 2))
        self.sec_var = tk.StringVar(value=sa.security_summary(cfg))
        bar = tk.Frame(root)
        bar.pack(fill="x", padx=10)
        tk.Label(bar, textvariable=self.sec_var, anchor="w").pack(side="left")
        for text, cmd in (("安全设置…", self._open_security), ("485配型诊断", self._gui_rtu485),
                          ("发送测试数据", lambda: self._gui_transmission(True)),
                          ("数据传输检测", lambda: self._gui_transmission(False)),
                          ("智能诊断", self._gui_diagnose)):
            tk.Button(bar, text=text, command=cmd).pack(side="right", padx=4)

        nb = ttk.Notebook(root)
        nb.pack(fill="both", expand=True, padx=10, pady=6)
        self.nb = nb
        self._build_status_tab(nb)
        self._build_alerts_tab(nb)
        self._build_tools_tab(nb)
        self._build_system_tab(nb)

        # 底部保存栏（与网页版 savebar 对应）
        savebar = tk.Frame(root)
        savebar.pack(fill="x", padx=10, pady=(0, 4))
        self.dirty_txt = tk.StringVar(value="没有未保存的修改")
        tk.Label(savebar, textvariable=self.dirty_txt, anchor="w").pack(side="left")
        tk.Button(savebar, text="放弃修改", command=self._reload).pack(side="right", padx=4)
        tk.Button(savebar, text="保存设置", command=self._save).pack(side="right", padx=4)

        # 日志窗（沿用原图形窗口）
        self.box = scrolledtext.ScrolledText(root, height=8, state="disabled")
        self.box.pack(fill="both", padx=10, pady=(2, 8))

        self._setup_service()
        self._reload()
        self._pump_log()
        self._poll_queue()
        self._refresh_sec()
        self._poll_alerts()
        self._poll_probes()
        self._poll_stability()
        self._poll_env()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- 服务与日志 ----
    def _setup_service(self):
        import logging
        import threading

        sa = self.sa
        cfg = self.cfg

        class QHandler(logging.Handler):
            def emit(_self, record):
                sa.log_q.put(_self.format(record))

        sa.setup_logging(cfg, cfg["_base_dir"])
        logging.getLogger().addHandler(QHandler())
        self._holder = {}
        self._worker_done = threading.Event()
        self._web_srv = None
        if not self.start_service:
            return

        def server_worker():
            import asyncio
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def _main():
                self._holder["loop"] = loop
                self._holder["stop"] = asyncio.Event()
                await sa.run_server(cfg, self._holder["stop"])

            try:
                loop.run_until_complete(_main())
            except Exception as e:  # noqa
                self.status.set("服务出错：" + str(e))
                sa.get_log().error("服务线程退出：%s", e)
            finally:
                self._worker_done.set()

        threading.Thread(target=server_worker, daemon=True).start()
        # 网页服务保留（web_ui 配置决定是否开启），但不再自动打开浏览器
        self._web_srv = sa.start_web_settings(cfg)
        self.status.set("运行中：%s" % cfg["endpoint"])

    def _on_close(self):
        self._closed = True
        try:
            self.logic.stop_stability()
        except Exception:  # noqa
            pass
        self.root.destroy()
        if self.start_service:
            lp, ev = self._holder.get("loop"), self._holder.get("stop")
            if lp is not None and ev is not None:
                try:
                    lp.call_soon_threadsafe(ev.set)
                except RuntimeError:
                    pass
            self._worker_done.wait(timeout=5)
            if self._web_srv is not None:
                try:
                    self._web_srv.stop()
                except Exception:  # noqa
                    pass

    def _pump_log(self):
        import queue as _queue
        try:
            while True:
                rec = self.sa.log_q.get_nowait()
                self.box.configure(state="normal")
                self.box.insert("end", rec + "\n")
                self.box.see("end")
                self.box.configure(state="disabled")
        except _queue.Empty:
            pass
        if not self._closed:
            self.root.after(300, self._pump_log)

    # ---- 通用小工具 ----
    def _bg(self, fn, done=None):
        """后台线程跑 fn()，结果经队列回主线程执行 done(result, error)。"""
        import threading

        def work():
            try:
                self._q.put((done, fn(), None))
            except Exception as e:  # noqa
                self._q.put((done, None, e))
        threading.Thread(target=work, daemon=True).start()

    def _poll_queue(self):
        import queue as _queue
        try:
            while True:
                done, result, err = self._q.get_nowait()
                if done is not None:
                    done(result, err)
        except _queue.Empty:
            pass
        if not self._closed:
            self.root.after(200, self._poll_queue)

    def _show(self, ok, text):
        if ok:
            self.mb.showinfo("提示", text, parent=self.root)
        else:
            self.mb.showerror("无法完成", text, parent=self.root)

    def _do_action(self, name, body=None, refresh=True):
        """跑一个后端动作（后台线程），完成后提示并刷新。"""
        def done(result, err):
            if err is not None:
                self._show(False, str(err))
                return
            if isinstance(result, dict):       # 连接测试类：调用方自己处理
                return
            self._show(True, str(result))
            if refresh:
                self._reload()
        self._bg(lambda: self.logic.action(name, body or {}), done)

    def _section(self, parent, title):
        tk = self.tk
        frame = tk.LabelFrame(parent, text=title, font=("Microsoft YaHei", 11, "bold"),
                              fg="#1f4e79", padx=10, pady=8)
        frame.pack(fill="x", padx=6, pady=6)
        return frame

    def _row(self, parent, label, widget_maker, hint=""):
        tk = self.tk
        row = tk.Frame(parent)
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, width=26, anchor="w").pack(side="left")
        widget = widget_maker(row)
        widget.pack(side="left", fill="x", expand=True)
        if hint:
            tk.Label(parent, text=hint, fg="#777", anchor="w", justify="left",
                     wraplength=820).pack(fill="x", padx=(190, 0))
        return widget

    def _field(self, parent, key, label, hint="", kind="str", options=None, restart=False):
        """配置表单字段：登记到 self._vars，返回控件。"""
        tk, ttk = self.tk, self.ttk
        if restart:
            label = label + RESTART_TAG
        if kind == "bool":
            var = tk.BooleanVar(value=False)
            w = self._row(parent, label, lambda r: tk.Checkbutton(r, variable=var), hint)
        elif kind == "select":
            var = tk.StringVar(value="")
            labels = [t for _v, t in options]
            values = [v for v, _t in options]
            w = self._row(parent, label, lambda r: ttk.Combobox(r, values=labels, state="readonly"), hint)
            w._values = values
            w._labels = labels
            w.bind("<<ComboboxSelected>>", lambda _e: self._mark_dirty())
        elif kind == "text":
            var = None
            w = self._row(parent, label, lambda r: tk.Text(r, height=4, width=60), hint)
        else:
            var = tk.StringVar(value="")
            w = self._row(parent, label, lambda r: tk.Entry(r, textvariable=var), hint)
        self._vars[key] = (var, w, kind)
        if var is not None:
            var.trace_add("write", lambda *_a: self._mark_dirty())
        else:
            w.bind("<KeyRelease>", lambda _e: self._mark_dirty())
        return w

    def _get_values(self):
        values = {}
        for key, (var, w, kind) in self._vars.items():
            if kind == "bool":
                values[key] = bool(var.get())
            elif kind == "select":
                idx = w.current()
                values[key] = w._values[idx] if 0 <= idx < len(w._values) else var.get()
            elif kind == "text":
                values[key] = w.get("1.0", "end").strip("\n")
            else:
                values[key] = var.get()
        return values

    def _set_values(self, cfg):
        for key, (var, w, kind) in self._vars.items():
            if kind == "bool":
                var.set(bool(cfg.get(key)))
            elif kind == "select":
                val = str(cfg.get(key, ""))
                var.set(val)
                if val in w._values:
                    w.current(w._values.index(val))
            elif kind == "text":
                w.delete("1.0", "end")
                w.insert("1.0", "\n".join(cfg.get(key) or []))
            else:
                var.set("" if cfg.get(key) is None else str(cfg.get(key)))

    def _mark_dirty(self):
        try:
            changed = collect_settings(self._get_values(), self._orig)
        except Exception:  # noqa
            return
        self.dirty_txt.set("有 %d 项修改未保存" % len(changed) if changed else "没有未保存的修改")

    def _save(self):
        values = self._get_values()

        def done(msg, err):
            if err is not None:
                self._show(False, str(err))
                return
            self._show(True, str(msg))
            self._reload()
        self._bg(lambda: self.logic.save(values), done)

    def _reload(self):
        def done(st, err):
            if err is not None:
                self._show(False, "读取配置失败：%s" % err)
                return
            self._render_state(st)
        self._bg(self.logic.state, done)

    def _render_state(self, st):
        self._orig = st["config"]
        self._set_values(st["config"])
        self._mark_dirty()
        # 安全体检
        lines = []
        if st.get("error"):
            lines.append("[错误] %s" % st["error"])
        lines += ["[%s] %s" % (r["level"], r["text"]) for r in st.get("report", [])]
        self._set_text(self.report_text, "\n".join(lines))
        self.cfg_path_var.set("配置文件：%s" % st.get("cfg_path", ""))
        self.smtp_pw_state.set("密码已加密保存在配置文件里。QQ/163/企业邮箱请填“授权码”。"
                               if st["config"].get("smtp_password_set")
                               else "QQ/163/企业邮箱请填“授权码”，不是网页登录密码。")
        self.users_lb.delete(0, "end")
        for u in st["config"].get("users") or []:
            self.users_lb.insert("end", u)
        if not st["config"].get("users"):
            self.users_lb.insert("end", "（无账号）")
        self._render_alerts(st.get("alerts") or {})

    def _set_text(self, widget, text):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("end", text)
        widget.configure(state="disabled")

    def _refresh_sec(self):
        try:
            text = self.sa.security_summary(self.cfg)
            mon = self.sa.ALERTS.get("monitor")
            if mon is not None:
                snap = mon.snapshot()
                text = ("告警：%d 项（%s）    " % (len(snap["active"]), snap["worst"]) if snap["active"]
                        else "告警：无    ") + ("[推送静默至 %s]    " % snap["quiet"]["until"]
                                              if snap.get("quiet") else "") + text
            self.sec_var.set(text)
        except Exception:  # noqa
            pass
        if not self._closed:
            self.root.after(5000, self._refresh_sec)

    # ---- 原图形窗口的快捷功能 ----
    def _open_security(self):
        self.sa.SecurityWindow(self.root, self.cfg, self.sa._TkDialogs(self.root))

    def _gui_transmission(self, send):
        sa, cfg = self.sa, self.cfg

        def work():
            import asyncio
            ep = sa._local_endpoint(cfg["endpoint"])
            base = cfg.get("_base_dir") or sa.program_dir()
            cd = cfg.get("cert_dir") or "certs"
            cert_dir = cd if os.path.isabs(cd) else os.path.join(base, cd)
            sa.get_log().info("开始%s（%s）…", "发送测试数据并检测" if send else "数据传输检测", ep)
            result = asyncio.run(sa.transmission_check_async(
                ep, namespace_uri=cfg.get("namespace_uri") or "urn:chromextract:opcua",
                security=cfg.get("security", "none"), cert_dir=cert_dir,
                duration_sec=6, do_send_test=bool(send)))
            for s in result.get("steps", []):
                sa.get_log().info("%s %s：%s", "[ 通过 ]" if s["ok"] else "[ 失败 ]", s["name"], s["detail"])
            sa.get_log().info("数据传输检测%s", "通过：数据传输正常" if result.get("ok") else "未通过，见上面失败步骤")
        self._bg(work)

    def _gui_diagnose(self):
        sa, cfg = self.sa, self.cfg

        def work():
            import asyncio
            ep = sa._local_endpoint(cfg["endpoint"])
            base = cfg.get("_base_dir") or sa.program_dir()
            cd = cfg.get("cert_dir") or "certs"
            cert_dir = cd if os.path.isabs(cd) else os.path.join(base, cd)
            sa.get_log().info("开始智能诊断（%s）…", ep)
            tx = asyncio.run(sa.transmission_check_async(
                ep, namespace_uri=cfg.get("namespace_uri") or "urn:chromextract:opcua",
                security=cfg.get("security", "none"), cert_dir=cert_dir,
                duration_sec=6, do_send_test=False))
            for s in tx.get("steps", []):
                sa.get_log().info("%s %s：%s", "[ 通过 ]" if s["ok"] else "[ 失败 ]", s["name"], s["detail"])
            result = sa.diagnose(cfg, transmission_result=tx)
            icons = {"ok": "[ 通过 ]", "warn": "[ 注意 ]", "fail": "[ 失败 ]"}
            for f in result.get("findings", []):
                sa.get_log().info("%s %s：%s", icons.get(f["level"], "[ ? ]"), f["name"], f["evidence"])
                if f["level"] != "ok":
                    sa.get_log().info("处理：%s", f["advice"])
            sa.get_log().info("智能诊断结论：%s", result.get("overall", ""))
            for i, a in enumerate(result.get("actions", []), 1):
                sa.get_log().info("处理清单 %d. %s", i, a)
        self._bg(work)

    def _gui_rtu485(self):
        port = self.sd.askstring("485配型诊断", "串口（例如 COM3）：", parent=self.root)
        if not port:
            return
        sa = self.sa

        def work():
            import nettest
            sa.get_log().info("开始 485 配型诊断（%s，自动试参数，只读）…", port)
            r = nettest.diagnose_rtu485(port.strip(), progress=lambda s: sa.get_log().info("%s", s))
            sa.get_log().info("%s %s", "[ 通过 ]" if r["ok"] else "[ 失败 ]", r["summary"])
            for line in r["details"]:
                for sub in str(line).split("\n"):
                    sa.get_log().info("%s", sub)
        self._bg(work)

    # ---- 标签页 1：运行状态 ----
    def _build_status_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        from tkinter import scrolledtext
        tab = tk.Frame(nb)
        nb.add(tab, text="运行状态")
        inner = _scrollable(tab, tk, ttk)

        sec = self._section(inner, "运行告警")
        self.al_sum = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.al_sum, anchor="w", fg="#666").pack(fill="x")
        self.al_quiet = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.al_quiet, anchor="w", fg="#856404").pack(fill="x")
        self.al_active = scrolledtext.ScrolledText(sec, height=5, state="disabled", wrap="word")
        self.al_active.pack(fill="x", pady=4)
        tk.Label(sec, text="最近的告警记录", anchor="w").pack(fill="x")
        self.al_hist = scrolledtext.ScrolledText(sec, height=5, state="disabled", wrap="word")
        self.al_hist.pack(fill="x", pady=4)

        sec = self._section(inner, "设备检测")
        tk.Label(sec, text="服务运行时按间隔自动检测关键设备（DCS、网关、分析仪等），连续失败达到次数就告警，"
                           "一次成功自动恢复；告警走告警与通知页设置的群机器人 / 邮件，遵守静默时段和渠道级别。"
                           "添加方法：在“诊断工具”的连接测试里选好类型和参数、测通后，点“加为设备检测”。",
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        self.pr_sum = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.pr_sum, anchor="w", fg="#666").pack(fill="x")
        cols = ("name", "target", "cond", "state", "last", "uptime")
        self.pr_tree = ttk.Treeview(sec, columns=cols, show="headings", height=6)
        for col, title, width in (("name", "名称", 130), ("target", "检测", 220), ("cond", "间隔 / 告警条件", 150),
                                  ("state", "状态", 110), ("last", "最近结果", 260), ("uptime", "可用率", 90)):
            self.pr_tree.heading(col, text=title)
            self.pr_tree.column(col, width=width, anchor="w")
        self.pr_tree.pack(fill="x", pady=4)
        btns = tk.Frame(sec)
        btns.pack(fill="x")
        for text, cmd in (("立即检测", self._pr_run), ("编辑", self._pr_edit),
                          ("启用/停用", self._pr_toggle), ("删除", self._pr_remove),
                          ("刷新", self._poll_probes_now)):
            tk.Button(btns, text=text, command=cmd).pack(side="left", padx=4)

        sec = self._section(inner, "通道告警设置")
        tk.Label(sec, text="长期停用或检修中的流路可以屏蔽告警；轮换间隔长的流路可以单独加大“无新周期”分钟数。"
                           "屏蔽时该通道的告警直接移除，不发“恢复”；可设到期时间自动解除。选中一行点“编辑规则”修改。",
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        cols = ("channel", "cycle", "state", "mute", "stale", "until", "note")
        self.ch_tree = ttk.Treeview(sec, columns=cols, show="headings", height=7)
        for col, title, width in (("channel", "通道", 240), ("cycle", "最后周期", 130), ("state", "状态", 130),
                                  ("mute", "屏蔽告警", 70), ("stale", "无新周期告警（分钟）", 130),
                                  ("until", "到期", 130), ("note", "备注", 110)):
            self.ch_tree.heading(col, text=title)
            self.ch_tree.column(col, width=width, anchor="w")
        self.ch_tree.pack(fill="x", pady=4)
        tk.Button(sec, text="编辑选中通道的规则…", command=self._ch_edit).pack(anchor="w", pady=2)
        self._ch_rows = {}

    def _render_alerts(self, a):
        if not a:
            return
        # 静默横幅与列表（静默列表在告警页渲染）
        q = a.get("quiet")
        self.al_quiet.set("推送静默中，至 %s%s" % (q["until"], "（%s）" % q["note"] if q.get("note") else "") if q else "")
        self._render_quiet_list(a.get("quiet_list") or [])
        if a.get("offline"):
            self.al_sum.set("（服务未运行：只打开了桌面设置窗口）")
            self._set_text(self.al_active, "")
            self._set_text(self.al_hist, "")
        else:
            self.al_sum.set("最近扫描 %s · %s 个通道" % (a.get("last_scan") or "尚未完成", a.get("channels", 0)))
            act = a.get("active") or []
            self._set_text(self.al_active, "\n".join(
                "[%s] %s（自 %s）" % (x["level"], x["text"], x["since"]) for x in act) or "[正常] 没有告警")
            hist = a.get("history") or []
            self._set_text(self.al_hist, "\n".join(
                "%s %s：%s" % (h["time"], "✓ 恢复" if h["event"] == "恢复" else "⚠ " + h["level"], h["text"])
                for h in hist) or "（无）")
        self._render_channels(a)

    def _render_channels(self, a):
        rows = (a or {}).get("channel_list") or []
        self._ch_rows = {}
        for item in self.ch_tree.get_children():
            self.ch_tree.delete(item)
        if not rows:
            self.ch_tree.insert("", "end", values=(
                "服务未运行，看不到通道。启动服务后这里会列出所有通道。" if (a or {}).get("offline")
                else "还没有扫描到通道。", "", "", "", "", "", ""))
            return
        for c in rows:
            if c.get("present") is None:
                state = "—"
            elif not c["present"]:
                state = "丢失"
            elif c.get("alerting"):
                state = "告警"
            else:
                state = "正常"
            if c.get("idle_min") is not None:
                state += "（%s 分钟前更新）" % c["idle_min"]
            iid = self.ch_tree.insert("", "end", values=(
                c["label"], c.get("cycle_time") or "", state,
                "是" if c.get("muted") else "否",
                "" if c.get("stale_minutes") in (None, "") else c["stale_minutes"],
                c.get("until") or "", c.get("note") or ""))
            self._ch_rows[iid] = c

    def _ch_edit(self):
        tk = self.tk
        sel = self.ch_tree.selection()
        if not sel or sel[0] not in self._ch_rows:
            return self._show(False, "请先选中一个通道。")
        c = self._ch_rows[sel[0]]
        win = tk.Toplevel(self.root)
        win.title("通道告警规则")
        win.geometry("460x280")
        tk.Label(win, text=c["label"], font=("Microsoft YaHei", 11, "bold"), anchor="w").pack(
            fill="x", padx=12, pady=(10, 4))
        mute = tk.BooleanVar(value=bool(c.get("muted")))
        tk.Checkbutton(win, text="屏蔽该通道的告警", variable=mute).pack(anchor="w", padx=12)
        stale = tk.StringVar(value="" if c.get("stale_minutes") in (None, "") else str(c["stale_minutes"]))
        until = tk.StringVar(value=c.get("until") or "")
        note = tk.StringVar(value=c.get("note") or "")
        for label, var, hint in (("无新周期告警（分钟，留空=默认）", stale, ""),
                                 ("到期时间（YYYY-MM-DD HH:MM，留空=不自动解除）", until, ""),
                                 ("备注", note, "")):
            tk.Label(win, text=label, anchor="w").pack(fill="x", padx=12, pady=(8, 0))
            tk.Entry(win, textvariable=var).pack(fill="x", padx=12)

        def save():
            body = {"rule": channel_rule_body(c["key"], mute.get(), stale.get().strip(),
                                              until.get().strip(), note.get())}
            win.destroy()
            self._do_action("channel_rule", body)
        tk.Button(win, text="保存", command=save).pack(pady=12)

    # ---- 设备检测（状态页表格）----
    def _poll_probes_now(self):
        self._bg(self.logic.probes, lambda r, e: None if e else self._render_probes(r))

    def _poll_probes(self):
        self._poll_probes_now()
        if not self._closed:
            self.root.after(10000, self._poll_probes)

    def _render_probes(self, j):
        rows = (j or {}).get("probes") or []
        self._pr_list = rows
        self.pr_sum.set(("" if (j or {}).get("running") else "（服务未运行，只能查看和立即检测）")
                        + " 共 %d 个，告警中 %d 个" % (len(rows), sum(1 for x in rows if x["alerting"]))
                        if rows else "还没有设备检测。")
        for item in self.pr_tree.get_children():
            self.pr_tree.delete(item)
        for p in rows:
            if not p["enabled"]:
                state = "停用"
            elif p["alerting"]:
                state = p["level"]
            elif p.get("last"):
                state = "正常" if p["last"]["ok"] else "失败 %d/%d" % (p["fails"], p["fail_threshold"])
            else:
                state = "待检测"
            last = ""
            if p.get("last"):
                last = "%s %s" % (p["last"]["time"], (p["last"]["summary"] or "")[:60])
            uptime = "—" if p.get("uptime_pct") is None else "%s%%（%d 次）" % (p["uptime_pct"], p["checks"])
            self.pr_tree.insert("", "end", iid=p["id"], values=(
                p["label"], "%s %s" % (p["kind_name"], p["target"]),
                "每 %s 秒，连续 %s 次失败→%s" % (p["interval"], p["fail_threshold"], p["level"]),
                state, last, uptime))

    def _pr_selected(self):
        sel = self.pr_tree.selection()
        if not sel:
            self._show(False, "请先选中一个设备检测。")
            return None
        return next((x for x in self._pr_list if x["id"] == sel[0]), None)

    def _pr_run(self):
        p = self._pr_selected()
        if p is None:
            return

        def done(result, err):
            if err is not None:
                return self._show(False, str(err))
            self._render_nettest_result({"test": result, "msg": result.get("summary", "")})
            self._poll_probes_now()
        self._bg(lambda: self.logic.action("probe_run", {"id": p["id"]}), done)

    def _pr_edit(self):
        p = self._pr_selected()
        if p is None:
            return
        self.nb.select(2)     # 跳到诊断工具页
        self._nt_show(p["kind"])
        for k, v in (p.get("params") or {}).items():
            if k in self._nt_vars:
                var, _w, typ = self._nt_vars[k]
                if typ == "chk":
                    var.set(bool(v))
                else:
                    var.set("" if v is None else str(v))
        self.pr_label.set(p["label"])
        self.pr_iv.set(str(p["interval"]))
        self.pr_thr.set(str(p["fail_threshold"]))
        self.pr_lv.set(p["level"])
        self._pr_editing = p["id"]
        self._show(True, "已把「%s」的参数填到连接测试里，改完点“加为设备检测”保存%s。"
                   % (p["label"], "（密码留空 = 保留原密码）" if p["kind"] == "opcua" else ""))

    def _pr_toggle(self):
        p = self._pr_selected()
        if p is not None:
            self._do_action("probe_toggle", {"id": p["id"]}, refresh=False)
            self.root.after(600, self._poll_probes_now)

    def _pr_remove(self):
        p = self._pr_selected()
        if p is not None and self.mb.askyesno("请确认", "删除设备检测「%s」？" % p["label"], parent=self.root):
            self._do_action("probe_remove", {"id": p["id"]}, refresh=False)
            self.root.after(600, self._poll_probes_now)

    # ---- 标签页 2：告警与通知 ----
    def _build_alerts_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        tab = tk.Frame(nb)
        nb.add(tab, text="告警与通知")
        inner = _scrollable(tab, tk, ttk)

        sec = self._section(inner, "告警设置")
        self._field(sec, "alert_stale_minutes", "通道无新周期告警（分钟）",
                    "超过这么久没有新周期就告警（分析仪停机、数据库不再更新）；按分析周期设，例如周期 10 分钟可设 60；0 = 不检查", "num")
        self._field(sec, "alert_scan_errors", "连续扫描出错告警（次）", "0 = 不检查", "num")
        self._field(sec, "alert_webhook", "告警推送地址（可选）",
                    "企业微信 / 钉钉 / 飞书群机器人的 Webhook 地址，或任意接收 JSON 的 https 地址；告警出现和恢复时各推送一次。钉钉机器人请在安全设置里加关键词“色谱”", "str")
        self._field(sec, "alert_webhook_level", "群机器人发送级别", "", "select",
                    SELECT_OPTIONS["alert_webhook_level"])
        tk.Button(sec, text="发送测试消息", command=lambda: self._do_action("test_webhook")).pack(anchor="w", pady=4)

        sec = self._section(inner, "静默时段（计划停机 / 检修时不推送）")
        tk.Label(sec, text="静默期间群机器人和邮件都不发；日志、本窗口和 OPC UA 告警节点照常显示。"
                           "静默结束时仍未恢复的告警会补发一次；静默期间出现又恢复的不再打扰。"
                           "时间按本机时间，添加后立即生效。",
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        self.quiet_lb = tk.Listbox(sec, height=4, exportselection=False)
        self.quiet_lb.pack(fill="x", pady=4)
        tk.Button(sec, text="删除选中静默时段", command=self._quiet_remove).pack(anchor="w")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=(8, 2))
        tk.Label(row, text="临时静默", width=26, anchor="w").pack(side="left")
        for text, cmd in (("2 小时", lambda: self._do_action("quiet_now", {"hours": 2})),
                          ("8 小时", lambda: self._do_action("quiet_now", {"hours": 8})),
                          ("24 小时", lambda: self._do_action("quiet_now", {"hours": 24})),
                          ("立即结束静默", lambda: self._do_action("quiet_end"))):
            tk.Button(row, text=text, command=cmd).pack(side="left", padx=4)
        self.q_s, self.q_e, self.q_n = tk.StringVar(), tk.StringVar(), tk.StringVar()
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        tk.Label(row, text="一次性时段", width=26, anchor="w").pack(side="left")
        for var, wdt in ((self.q_s, 18), (self.q_e, 18), (self.q_n, 14)):
            tk.Entry(row, textvariable=var, width=wdt).pack(side="left", padx=3)
        tk.Button(row, text="添加", command=self._quiet_once).pack(side="left", padx=4)
        tk.Label(sec, text="格式：开始/结束填 YYYY-MM-DD HH:MM，第三框备注（如 大修）；最长 31 天",
                 fg="#777", anchor="w").pack(fill="x", padx=(190, 0))
        self.d_s, self.d_e, self.d_n = tk.StringVar(), tk.StringVar(), tk.StringVar()
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        tk.Label(row, text="每天重复时段", width=26, anchor="w").pack(side="left")
        for var, wdt in ((self.d_s, 10), (self.d_e, 10), (self.d_n, 14)):
            tk.Entry(row, textvariable=var, width=wdt).pack(side="left", padx=3)
        tk.Button(row, text="添加", command=self._quiet_daily).pack(side="left", padx=4)
        row = tk.Frame(sec)
        row.pack(fill="x", padx=(190, 0))
        tk.Label(row, text="星期（都不勾 = 每天）：", fg="#777").pack(side="left")
        self._day_vars = []
        for i, name in enumerate("一二三四五六日", 1):
            v = tk.BooleanVar(value=False)
            self._day_vars.append(v)
            tk.Checkbutton(row, text="周" + name, variable=v).pack(side="left")
        tk.Label(sec, text="开始/结束填 HH:MM；结束早于开始表示跨午夜，例如 22:00～06:00",
                 fg="#777", anchor="w").pack(fill="x", padx=(190, 0))

        sec = self._section(inner, "邮件通知（可选）")
        self._field(sec, "alert_email_to", "收件人",
                    "多个用逗号隔开；留空 = 不发邮件。告警出现和恢复时各发一封，每小时最多 20 封", "str")
        self._field(sec, "alert_email_level", "邮件发送级别",
                    "严重 = 扫描卡死、连续扫描出错；警告 = 无数据、数据源丢失、通道长时间不更新。恢复通知只发给收到过该告警的渠道",
                    "select", SELECT_OPTIONS["alert_email_level"])
        self._field(sec, "smtp_host", "SMTP 服务器", "", "str")
        self._field(sec, "smtp_port", "端口", "", "num")
        self._field(sec, "smtp_security", "加密方式", "", "select", SELECT_OPTIONS["smtp_security"])
        self._field(sec, "smtp_user", "用户名", "免登录的内网中继可留空", "str")
        self._field(sec, "smtp_from", "发件人地址", "留空 = 用户名", "str")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=3)
        tk.Label(row, text="密码 / 授权码", width=26, anchor="w").pack(side="left")
        self.smtp_pw = tk.StringVar()
        tk.Entry(row, textvariable=self.smtp_pw, show="*").pack(side="left", fill="x", expand=True)
        tk.Button(row, text="保存密码", command=self._set_smtp_pw).pack(side="left", padx=4)
        tk.Button(row, text="清除", command=self._clear_smtp_pw).pack(side="left", padx=4)
        self.smtp_pw_state = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.smtp_pw_state, fg="#777", anchor="w").pack(fill="x", padx=(190, 0))
        tk.Button(sec, text="发送测试邮件", command=lambda: self._do_action("test_email")).pack(anchor="w", pady=4)
        tk.Label(sec, text="先点底部“保存设置”，再测试", fg="#777", anchor="w").pack(fill="x")

    def _render_quiet_list(self, items):
        self.quiet_lb.delete(0, "end")
        for it in items:
            self.quiet_lb.insert("end", it.get("text") or str(it))
        if not items:
            self.quiet_lb.insert("end", "（没有静默时段）")

    def _quiet_remove(self):
        sel = self.quiet_lb.curselection()
        if not sel:
            return self._show(False, "请先选中一条静默时段。")
        self._do_action("quiet_remove", {"index": sel[0]})

    def _quiet_once(self):
        self._do_action("quiet_add", {"item": quiet_once_item(self.q_s.get(), self.q_e.get(), self.q_n.get())})

    def _quiet_daily(self):
        days = [i + 1 for i, v in enumerate(self._day_vars) if v.get()]
        self._do_action("quiet_add", {"item": quiet_daily_item(self.d_s.get(), self.d_e.get(), days, self.d_n.get())})

    def _set_smtp_pw(self):
        pw = self.smtp_pw.get()
        if not pw:
            return self._show(False, "请输入密码或授权码。")

        def done(msg, err):
            if err is not None:
                return self._show(False, str(err))
            self.smtp_pw.set("")
            self._show(True, str(msg))
            self._reload()
        self._bg(lambda: self.logic.action("set_smtp_password", {"password": pw}), done)

    def _clear_smtp_pw(self):
        if self.mb.askyesno("请确认", "确定清除 SMTP 密码吗？", parent=self.root):
            self._do_action("set_smtp_password", {"password": ""})

    # ---- 标签页 3：诊断工具 ----
    def _build_tools_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        from tkinter import scrolledtext
        tab = tk.Frame(nb)
        nb.add(tab, text="诊断工具")
        inner = _scrollable(tab, tk, ttk)

        sec = self._section(inner, "驱动与软件环境检测")
        tk.Label(sec, text="只读检查，不会安装或修改任何东西：串口设备和驱动（CH340 / FTDI / CP210x / PL2303 等，"
                           "含黄色感叹号设备）、运行库、防火墙服务、开机自启任务、OPC UA 端口、数据目录。"
                           "现场装好后、或串口突然不能用时先跑一遍。",
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        self._field(sec, "env_check_on_start", "服务启动时自动检测",
                    "启动后约 8 秒在后台检测；“问题”级别的项会告警（警告级），复查时消失自动恢复；“注意”只记日志",
                    "bool", restart=True)
        self._field(sec, "env_check_hours", "定期复查（小时）", "0 = 只在启动时检查；改完点底部“保存设置”", "num")
        self.env_last = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.env_last, anchor="w", fg="#555").pack(fill="x")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=4)
        self.dc_btn = tk.Button(row, text="开始检测", command=self._dc_run)
        self.dc_btn.pack(side="left")
        self.dc_time = tk.StringVar(value="")
        tk.Label(row, textvariable=self.dc_time, fg="#777").pack(side="left", padx=8)
        self.dc_out = scrolledtext.ScrolledText(sec, height=8, state="disabled", wrap="word")
        self.dc_out.pack(fill="x", pady=4)

        sec = self._section(inner, "连接测试")
        tk.Label(sec, text="现场排查用：网络（Ping / TCP 端口 / DNS / OPC UA / Modbus TCP）和串口"
                           "（RS-232 / RS-485 / RS-422，Modbus RTU）。测试在本机执行。",
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        kind_row = tk.Frame(sec)
        kind_row.pack(fill="x", pady=4)
        self._nt_kind_btns = {}
        for kind, spec in NETTEST_SPECS.items():
            b = tk.Button(kind_row, text=spec["t"], command=lambda k=kind: self._nt_show(k))
            b.pack(side="left", padx=2, pady=2)
            self._nt_kind_btns[kind] = b
        self.nt_tip = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.nt_tip, fg="#856404", anchor="w").pack(fill="x")
        self.nt_form = tk.Frame(sec)
        self.nt_form.pack(fill="x")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=4)
        self.nt_btn = tk.Button(row, text="开始测试", command=self._nt_run)
        self.nt_btn.pack(side="left")
        self.nt_time = tk.StringVar(value="")
        tk.Label(row, textvariable=self.nt_time, fg="#777").pack(side="left", padx=8)
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        tk.Label(row, text="加为设备检测：", font=("Microsoft YaHei", 10, "bold")).pack(side="left")
        self.pr_label, self.pr_iv, self.pr_thr, self.pr_lv = (tk.StringVar(), tk.StringVar(value="60"),
                                                              tk.StringVar(value="3"), tk.StringVar(value="警告"))
        tk.Label(row, text="名称").pack(side="left", padx=(6, 2))
        tk.Entry(row, textvariable=self.pr_label, width=14).pack(side="left")
        tk.Label(row, text="间隔(秒)").pack(side="left", padx=(6, 2))
        tk.Entry(row, textvariable=self.pr_iv, width=6).pack(side="left")
        tk.Label(row, text="连续失败").pack(side="left", padx=(6, 2))
        tk.Entry(row, textvariable=self.pr_thr, width=5).pack(side="left")
        tk.Label(row, text="次告警").pack(side="left", padx=2)
        ttk.Combobox(row, textvariable=self.pr_lv, values=["警告", "严重"], state="readonly", width=7).pack(side="left")
        tk.Button(row, text="加为设备检测", command=self._pr_add).pack(side="left", padx=6)
        self.nt_out = scrolledtext.ScrolledText(sec, height=7, state="disabled", wrap="word")
        self.nt_out.pack(fill="x", pady=4)

        sec = self._section(inner, "长时间稳定性测试")
        tk.Label(sec, text="用上面同样的参数反复测试，统计丢包率、延迟和错误。支持 Ping / TCP 端口 / DNS / OPC UA / "
                           "Modbus TCP / Modbus RTU / 串口收发；关掉窗口也会在后台继续。",
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x")
        row = tk.Frame(sec)
        row.pack(fill="x", pady=3)
        tk.Label(row, text="间隔(秒)").pack(side="left")
        self.st_iv = tk.StringVar(value="1")
        tk.Entry(row, textvariable=self.st_iv, width=8).pack(side="left", padx=4)
        tk.Label(row, text="时长").pack(side="left", padx=(8, 2))
        self.st_min = tk.StringVar(value="10 分钟")
        ttk.Combobox(row, textvariable=self.st_min, state="readonly", width=12,
                     values=["10 分钟", "1 小时", "8 小时", "24 小时", "72 小时"]).pack(side="left")
        tk.Button(row, text="开始稳定性测试", command=self._st_start).pack(side="left", padx=6)
        tk.Button(row, text="停止", command=lambda: self._do_action("stab_stop", refresh=False)).pack(side="left")
        tk.Label(sec, text="多目标对比：在上面依次选好每个目标的参数，点“加入对比列表”，最多 16 个；"
                           "同一串口上的多个站会自动按顺序轮询（RS-485 不能同时通信），不同串口和网络目标真正并行。"
                           "结束后会分析各目标的失败是否同时发生。",
                 fg="#777", anchor="w", justify="left", wraplength=860).pack(fill="x", pady=(8, 2))
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        self.mt_label = tk.StringVar()
        tk.Entry(row, textvariable=self.mt_label, width=16).pack(side="left")
        tk.Button(row, text="加入对比列表", command=self._mt_add).pack(side="left", padx=4)
        tk.Button(row, text="开始多目标测试", command=self._mt_start).pack(side="left", padx=4)
        tk.Button(row, text="清空列表", command=self._mt_clear).pack(side="left", padx=4)
        self.mt_lb = tk.Listbox(sec, height=4, exportselection=False)
        self.mt_lb.pack(fill="x", pady=4)
        self.st_view = scrolledtext.ScrolledText(sec, height=10, state="disabled", wrap="word")
        self.st_view.pack(fill="x", pady=4)
        self.st_canvas = tk.Canvas(sec, height=150, bg="#fafbfc", highlightthickness=1, highlightbackground="#eee")
        self.st_canvas.pack(fill="x", pady=4)
        row = tk.Frame(sec)
        row.pack(fill="x")
        self.st_csv = tk.StringVar(value="")
        tk.Label(row, textvariable=self.st_csv, fg="#777").pack(side="left")
        tk.Button(row, text="打开明细 CSV", command=self._open_csv).pack(side="left", padx=6)
        self._st_csv_name = ""
        self._nt_show("ping")

    # ---- 连接测试表单 ----
    def _nt_show(self, kind):
        tk, ttk = self.tk, self.ttk
        self._nt_kind = kind
        for k, b in self._nt_kind_btns.items():
            b.configure(relief="sunken" if k == kind else "raised")
        self.nt_tip.set(NETTEST_SPECS[kind].get("tip", ""))
        for child in self.nt_form.winfo_children():
            child.destroy()
        self._nt_vars = {}
        fields = nettest_fields(kind)
        if not fields:
            tk.Label(self.nt_form, text="无需参数，直接点“开始测试”。", fg="#777").pack(anchor="w")
        for key, label, default, typ in fields:
            row = tk.Frame(self.nt_form)
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, width=26, anchor="w").pack(side="left")
            if typ == "chk":
                var = tk.BooleanVar(value=False)
                w = tk.Checkbutton(row, variable=var)
            elif typ == "password":
                var = tk.StringVar(value="")
                w = tk.Entry(row, textvariable=var, show="*")
            elif typ == "fc":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(row, textvariable=var, state="readonly",
                                 values=["1", "2", "3", "4"])
                tk.Label(row, text="（01 线圈 / 02 离散输入 / 03 保持寄存器 / 04 输入寄存器）",
                         fg="#777").pack(side="right")
            elif typ == "mode":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(row, textvariable=var, state="readonly", values=["hex", "text"])
            elif typ == "baud":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(row, textvariable=var,
                                 values=["1200", "2400", "4800", "9600", "19200", "38400", "57600", "115200"])
            elif typ == "bits":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(row, textvariable=var, state="readonly", values=["8", "7"])
            elif typ == "parity":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(row, textvariable=var, state="readonly", values=["N", "E", "O"])
            elif typ == "stop":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(row, textvariable=var, state="readonly", values=["1", "2"])
            elif typ == "port":
                var = tk.StringVar(value=default)
                w = ttk.Combobox(row, textvariable=var, values=self._nt_ports)
                tk.Button(row, text="扫描串口", command=self._nt_scan).pack(side="right", padx=4)
            else:
                var = tk.StringVar(value=default)
                w = tk.Entry(row, textvariable=var)
            w.pack(side="left", fill="x", expand=True)
            self._nt_vars[key] = (var, w, typ)

    def _nt_params(self):
        raw = {}
        for key, (var, _w, typ) in self._nt_vars.items():
            raw[key] = bool(var.get()) if typ == "chk" else var.get()
        return build_nettest_params(self._nt_kind, raw)

    def _nt_scan(self):
        def done(result, err):
            if err is not None:
                return self._show(False, str(err))
            self._render_nettest_result({"test": result, "msg": result.get("summary", "")})
            if result.get("ok"):
                self._nt_ports = [d.split()[0] for d in result.get("details", []) if d.split()]
                cur = self._nt_vars.get("port", (None,))[0]
                cur_val = cur.get() if cur else ""
                self._nt_show(self._nt_kind)
                if "port" in self._nt_vars:
                    self._nt_vars["port"][0].set(cur_val or (self._nt_ports[0] if self._nt_ports else ""))
        self._bg(lambda: self.logic.nettest("serial_list", {}), done)

    def _render_nettest_result(self, j):
        t = (j or {}).get("test")
        if not t:
            self._set_text(self.nt_out, (j or {}).get("msg") or "测试失败")
            return
        text = ("✓ " if t["ok"] else "✗ ") + t["summary"]
        if t.get("details"):
            text += "\n" + "\n".join(t["details"])
        self._set_text(self.nt_out, text)
        self.nt_time.set("耗时 %s ms" % t.get("elapsed_ms", 0))

    def _nt_run(self):
        kind, params = self._nt_kind, self._nt_params()
        self.nt_btn.configure(state="disabled", text="测试中…")
        self.nt_time.set("")

        def done(result, err):
            self.nt_btn.configure(state="normal", text="开始测试")
            if err is not None:
                self._render_nettest_result({"msg": str(err)})
                return
            self._render_nettest_result({"test": result, "msg": result.get("summary", "")})
        self._bg(lambda: self.logic.nettest(kind, params), done)

    def _pr_add(self):
        if self._nt_kind not in PROBE_KINDS:
            return self._show(False, "“%s”不能做设备检测，请选 Ping / TCP / DNS / OPC UA / Modbus / 串口收发。"
                              % NETTEST_SPECS[self._nt_kind]["t"])
        label = self.pr_label.get().strip()
        if not label:
            return self._show(False, "请填写设备检测的名称。")
        old = next((x for x in self._pr_list if x["id"] == self._pr_editing), None)
        body = {"probe": probe_body(self._pr_editing, label, self._nt_kind, self._nt_params(),
                                    self.pr_iv.get(), self.pr_thr.get(), self.pr_lv.get(),
                                    old["enabled"] if old else True)}

        def done(msg, err):
            if err is not None:
                return self._show(False, str(err))
            self.pr_label.set("")
            self._pr_editing = ""
            self._show(True, str(msg))
            self._poll_probes_now()
        self._bg(lambda: self.logic.action("probe_save", body), done)

    # ---- 驱动检测 ----
    def _dc_run(self):
        self.dc_btn.configure(state="disabled", text="检测中（约 10～30 秒）…")

        def done(result, err):
            self.dc_btn.configure(state="normal", text="开始检测")
            if err is not None:
                return self._show(False, str(err))
            d = (result or {}).get("drivercheck")
            if not d:
                return self._show(False, "检测失败")
            self._dc_show(d, "刚刚")
            self._poll_env()
        self._bg(lambda: self.logic.action("drivercheck"), done)

    def _dc_show(self, d, when=""):
        order = {"问题": 0, "注意": 1, "正常": 2}
        self.dc_time.set("%s · 耗时 %s ms%s" % (d["summary"], d["elapsed_ms"], " · " + when if when else ""))
        lines = []
        for it in sorted(d["items"], key=lambda x: order.get(x["level"], 3)):
            lines.append("[%s] %s：%s" % (it["level"], it["name"], it["text"]))
            if it.get("fix"):
                lines.append("    处理：%s" % it["fix"])
        self._set_text(self.dc_out, "\n".join(lines))

    def _poll_env(self):
        def done(e, err):
            if err is not None or not e:
                if not e:
                    self.env_last.set("服务未运行：自动检测只在服务运行时进行，可以点“开始检测”手动检查。")
                return
            if e.get("running") and not e.get("result"):
                self.env_last.set("正在进行启动检测…")
                self.root.after(3000, self._poll_env)
                return
            if not e.get("result"):
                self.env_last.set("启动检测还没开始（服务启动约 8 秒后进行，或已在设置里关闭）。")
                self.root.after(5000, self._poll_env)
                return
            self.env_last.set("上次自动检测：%s · %s" % (e["time"], e["result"]["summary"]))
            if self._dc_shown_time != e["time"] and not self.dc_out.get("1.0", "end").strip():
                self._dc_show(e["result"], e["time"])
            self._dc_shown_time = e["time"]
        self._bg(self.logic.env, done)

    # ---- 稳定性测试 ----
    _ST_MINUTES = {"10 分钟": "10", "1 小时": "60", "8 小时": "480", "24 小时": "1440", "72 小时": "4320"}

    def _st_start(self):
        if self._nt_kind not in STAB_KINDS:
            return self._show(False, "“%s”不支持长时间测试，请先选 Ping / TCP / DNS / OPC UA / Modbus / 串口收发。"
                              % NETTEST_SPECS[self._nt_kind]["t"])
        body = {"kind": self._nt_kind, "params": self._nt_params(), "interval": self.st_iv.get(),
                "minutes": self._ST_MINUTES.get(self.st_min.get(), "10")}

        def done(msg, err):
            self._show(err is None, str(msg if err is None else err))
            self._poll_stability_now()
        self._bg(lambda: self.logic.action("stab_start", body), done)

    def _mt_add(self):
        if self._nt_kind not in STAB_KINDS:
            return self._show(False, "“%s”不支持长时间测试。" % NETTEST_SPECS[self._nt_kind]["t"])
        if len(self._mt_list) >= 16:
            return self._show(False, "最多 16 个目标。")
        params = self._nt_params()
        params.pop("password", None)
        self._mt_list.append({"kind": self._nt_kind, "params": params,
                              "label": self.mt_label.get().strip() or "目标%d" % (len(self._mt_list) + 1)})
        self.mt_label.set("")
        self._mt_show()
        self._show(True, "已加入对比列表，共 %d 个。" % len(self._mt_list))

    def _mt_show(self):
        self.mt_lb.delete(0, "end")
        for t in self._mt_list:
            self.mt_lb.insert("end", "%s · %s" % (t["label"], NETTEST_SPECS[t["kind"]]["t"]))
        if not self._mt_list:
            self.mt_lb.insert("end", "（列表为空）")

    def _mt_clear(self):
        self._mt_list = []
        self._mt_show()

    def _mt_start(self):
        if len(self._mt_list) < 2:
            return self._show(False, "多目标对比至少需要 2 个目标（只有 1 个请直接点“开始稳定性测试”）。")
        body = {"targets": self._mt_list, "interval": self.st_iv.get(),
                "minutes": self._ST_MINUTES.get(self.st_min.get(), "10")}

        def done(msg, err):
            self._show(err is None, str(msg if err is None else err))
            self._poll_stability_now()
        self._bg(lambda: self.logic.action("stab_start", body), done)

    def _poll_stability_now(self):
        self._bg(self.logic.stability, lambda s, e: None if e else self._st_render(s))

    def _poll_stability(self):
        def done(s, err):
            if err is None:
                self._st_render(s)
            delay = 2000 if s and s.get("state") == "running" else 15000
            if not self._closed:
                self.root.after(delay, self._poll_stability)
        self._bg(self.logic.stability, done)

    def _st_render(self, s):
        if not s:
            self._set_text(self.st_view, "")
            self.st_canvas.delete("all")
            self.st_csv.set("")
            self._st_csv_name = ""
            return
        if s.get("multi"):
            self._mt_render(s)
            return
        state_txt = {"running": "运行中", "finished": "已完成", "stopped": "已手动停止",
                     "error": "出错：" + str(s.get("error"))}.get(s["state"], s["state"])

        def num(v, u=""):
            return "—" if v is None else "%s%s" % (v, u)
        lines = ["%s %s · %s · %s / %s · 每 %s 秒" % (s["kind_name"], s["target"], state_txt,
                                                     s["elapsed_text"], s["total_text"], s["interval"]),
                 "进度 %s%%" % s["progress"],
                 s["verdict"],
                 "测试次数 %s，成功 %s，失败 %s，失败率 %s，最长连续失败 %s%s" % (
                     s["n"], s["ok"], s["fail"], num(s["loss_pct"], "%"), s["max_streak"],
                     "（%s 起）" % s["max_streak_at"] if s.get("max_streak_at") else ""),
                 "最小 %s，平均 %s，P95 %s，P99 %s，最大 %s，抖动 %s" % (
                     num(s["min"], " ms"), num(s["avg"], " ms"), num(s["p95"], " ms"),
                     num(s["p99"], " ms"), num(s["max"], " ms"), num(s["jitter"], " ms"))]
        if s.get("errors"):
            lines.append("错误分布：" + "；".join("%d 次：%s" % (e["count"], e["text"]) for e in s["errors"]))
        if s.get("hints"):
            lines.append("排查建议：" + "；".join(s["hints"]))
        self._set_text(self.st_view, "\n".join(lines))
        self._st_chart(s)
        self._st_csv_name = s.get("csv") or ""
        self.st_csv.set("明细 CSV：logs/stability/%s（%s）" % (self._st_csv_name,
                        "每 20 次写盘一次" if s["state"] == "running" else "已保存") if self._st_csv_name else "")

    def _mt_render(self, s):
        state_txt = {"running": "运行中", "finished": "已完成", "stopped": "已手动停止"}.get(s["state"], s["state"])
        lines = ["多目标稳定性测试 · %d 个目标 · %s · %s / %s · 每 %s 秒" % (
            s["count"], state_txt, s["elapsed_text"], s["total_text"], s["interval"]),
            "进度 %s%%" % s["progress"], s["verdict"]]
        lines += list(s.get("notes") or [])
        for r in s["rows"]:
            lines.append("%s（%s %s）：%d 次，失败率 %s%%，最长连续失败 %s，平均/P95/最大 %s/%s/%s ms" % (
                r["label"], r["kind_name"], r["target"], r["n"], r["loss_pct"], r["max_streak"],
                r["avg"], r["p95"], r["max"]))
            if r.get("errors"):
                lines.append("    %d 次：%s" % (r["errors"][0]["count"], r["errors"][0]["text"]))
        self._set_text(self.st_view, "\n".join(lines))
        self.st_canvas.delete("all")
        self._st_csv_name = s.get("summary_csv") or ""
        self.st_csv.set("对比汇总 CSV：logs/stability/%s" % self._st_csv_name if self._st_csv_name else "")

    def _st_chart(self, s):
        c = self.st_canvas
        c.delete("all")
        buckets = s.get("buckets") or []
        if not buckets:
            return
        w = max(c.winfo_width(), 600)
        h, pad = 130, 34
        n = max(1, int(s["duration"] / s["bucket_sec"]) if s.get("bucket_sec") else len(buckets))
        mx = max([1] + [b.get("max") or 0 for b in buckets])
        c.create_text(2, 8, text="%.0fms" % mx, anchor="nw", fill="#666", font=("Microsoft YaHei", 8))
        c.create_line(pad, h, w, h, fill="#ccc")
        pts = []
        for b in buckets:
            x = pad + (b["i"] / n) * (w - pad - 5)
            bw = max(1.5, (w - pad - 5) / n - 1)
            if b.get("fail"):
                bh = max(3, (b["fail"] / b["n"]) * (h - 20)) if b["n"] else 3
                c.create_rectangle(x, h - bh, x + bw, h, fill="#dc3545", outline="")
            if b.get("avg") is not None:
                pts += [x + bw / 2, h - (b["avg"] / mx) * (h - 20)]
        if len(pts) >= 4:
            c.create_line(*pts, fill="#1f4e79", width=2)
        c.create_text(pad, h + 4, text="0", anchor="nw", fill="#666", font=("Microsoft YaHei", 8))
        c.create_text(w - 70, h + 4, text=s.get("total_text", ""), anchor="nw", fill="#666",
                      font=("Microsoft YaHei", 8))
        c.create_text(w / 2 - 130, h + 4, text="蓝线 = 平均延迟，红柱 = 失败比例（每段 %s 秒）" % s.get("bucket_sec"),
                      anchor="nw", fill="#666", font=("Microsoft YaHei", 8))

    def _open_csv(self):
        path = self.logic.stab_csv_path(self._st_csv_name)
        if not path or not os.path.isfile(path):
            return self._show(False, "还没有明细 CSV 文件。")
        try:
            os.startfile(path)          # Windows
        except Exception:  # noqa
            self._show(True, "明细 CSV 在：%s" % path)

    # ---- 标签页 4：系统设置 ----
    def _build_system_tab(self, nb):
        tk, ttk = self.tk, self.ttk
        from tkinter import scrolledtext
        tab = tk.Frame(nb)
        nb.add(tab, text="系统设置")
        inner = _scrollable(tab, tk, ttk)

        sec = self._section(inner, "数据源")
        self._field(sec, "watch_dirs", "监控目录（每行一个）",
                    "存放 *_V3.db / *.mbd 的文件夹，必须真实存在，例如 D:\\GCP\\DataBrowser", "text")
        self._field(sec, "poll_seconds", "扫描周期（秒）", "", "num")
        self._field(sec, "last_n", "每流路最近周期数", "", "num")
        self._field(sec, "max_points", "单曲线最大发布点数", "", "num")

        sec = self._section(inner, "OPC UA 服务")
        self._field(sec, "endpoint", "监听地址 endpoint", "", "str", restart=True)
        self._field(sec, "server_name", "服务器名称", "", "str", restart=True)
        self._field(sec, "max_connections", "最大连接数", "", "num", restart=True)
        self._field(sec, "security", "通信安全模式", "", "select", SELECT_OPTIONS["security"], restart=True)
        self._field(sec, "allow_anonymous", "允许匿名只读连接", "", "bool")
        self._field(sec, "allow_insecure_fallback", "保留不加密备用入口", "仅排障用，用完请关闭", "bool", restart=True)

        sec = self._section(inner, "CSV 导出")
        self._field(sec, "remote_export", "允许远程调用 ExportCSV", "", "bool")
        self._field(sec, "export_min_interval_sec", "远程导出最短间隔（秒）", "", "num")
        self._field(sec, "csv_export_dir", "导出目录", "", "str")

        sec = self._section(inner, "高级设置")
        self._field(sec, "max_trace_points", "单条谱图最大采样点", "", "num")
        self._field(sec, "max_file_mb", "单个 .mbd 上限（MB）", "", "num")
        self._field(sec, "log_max_mb", "单个日志大小（MB）", "", "num")
        self._field(sec, "log_backups", "保留历史日志个数", "", "num")
        self._field(sec, "web_ui", "启用设置网页", "桌面窗口是本机默认入口；网页仍可开启供远程访问", "bool", restart=True)
        self._field(sec, "web_host", "设置网页监听地址",
                    "127.0.0.1 = 只有本机能打开（推荐）；改成其它地址后必须用账号登录", "str", restart=True)
        self._field(sec, "web_port", "设置网页端口", "", "num", restart=True)
        self._field(sec, "web_https", "HTTPS",
                    "未指定证书时自动生成自签名证书；浏览器首次会提示“不安全”，核对指纹一致后继续即可",
                    "select", SELECT_OPTIONS["web_https"], restart=True)
        self._field(sec, "web_cert_file", "自有证书文件（PEM）", "留空 = 自动生成", "str", restart=True)
        self._field(sec, "web_key_file", "自有私钥文件（PEM）", "留空 = 自动生成", "str", restart=True)

        sec = self._section(inner, "登录账号")
        self.users_lb = tk.Listbox(sec, height=4, exportselection=False)
        self.users_lb.pack(fill="x", pady=4)
        row = tk.Frame(sec)
        row.pack(fill="x", pady=2)
        self.u_name, self.u_pw, self.u_pw2 = tk.StringVar(), tk.StringVar(), tk.StringVar()
        tk.Entry(row, textvariable=self.u_name, width=16).pack(side="left", padx=3)
        tk.Entry(row, textvariable=self.u_pw, show="*", width=18).pack(side="left", padx=3)
        tk.Entry(row, textvariable=self.u_pw2, show="*", width=18).pack(side="left", padx=3)
        tk.Button(row, text="添加 / 重设", command=self._add_user).pack(side="left", padx=4)
        tk.Button(row, text="删除选中账号", command=self._remove_user).pack(side="left", padx=4)
        tk.Label(sec, text="依次填：用户名、密码（至少 8 位）、再输入一次", fg="#777", anchor="w").pack(fill="x")
        tk.Button(sec, text="一键加固（开启加密 + 关闭匿名 + 用上面的账号）",
                  command=self._harden).pack(anchor="w", pady=4)

        sec = self._section(inner, "安全体检")
        self.report_text = scrolledtext.ScrolledText(sec, height=8, state="disabled", wrap="word")
        self.report_text.pack(fill="x", pady=4)
        self.cfg_path_var = tk.StringVar(value="")
        tk.Label(sec, textvariable=self.cfg_path_var, fg="#888", anchor="w").pack(fill="x")

    def _pw_body(self):
        name, pw = self.u_name.get().strip(), self.u_pw.get()
        if not name:
            self._show(False, "请填写用户名。")
            return None
        if pw != self.u_pw2.get():
            self._show(False, "两次输入的密码不一致。")
            return None
        return {"name": name, "password": pw}

    def _add_user(self):
        body = self._pw_body()
        if body is None:
            return

        def done(msg, err):
            if err is not None:
                return self._show(False, str(err))
            self.u_pw.set("")
            self.u_pw2.set("")
            self._show(True, str(msg))
            self._reload()
        self._bg(lambda: self.logic.action("add_user", body), done)

    def _remove_user(self):
        sel = self.users_lb.curselection()
        if not sel:
            return self._show(False, "请先选中要删除的账号。")
        name = self.users_lb.get(sel[0])
        if name != "（无账号）" and self.mb.askyesno("请确认", "确定删除账号 %s 吗？" % name, parent=self.root):
            self._do_action("remove_user", {"name": name})

    def _harden(self):
        body = self._pw_body()
        if body is None:
            return
        if not self.mb.askyesno("请确认", "一键加固会开启加密、关闭匿名访问。现有 DCS/OPC 客户端需要同步改成"
                                          "加密+账号登录，否则会连不上。继续吗？", parent=self.root):
            return

        def done(msg, err):
            if err is not None:
                return self._show(False, str(err))
            self.u_pw.set("")
            self.u_pw2.set("")
            self._show(True, str(msg))
            self._reload()
        self._bg(lambda: self.logic.action("harden", body), done)

    # ---- 状态轮询 ----
    def _poll_alerts(self):
        def done(a, err):
            if err is None:
                self._render_alerts(a)
            if not self._closed:
                self.root.after(15000, self._poll_alerts)
        self._bg(self.logic.alerts, done)


def run_desktop(cfg, start_service=True, background_note=""):
    """打开桌面窗口。start_service=True 时服务在后台线程同时启动（原 --gui 语义）；
    False 时只开设置窗口（原 --settings 语义，状态页如实显示服务未运行）。"""
    win = DesktopWindow(cfg, start_service=start_service, background_note=background_note)
    win.root.mainloop()
    return 0
