#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
web_settings.py —— 浏览器里的设置页面（替代手改 config.json）

  - 只用 Python 标准库（http.server），打包不需要新依赖；
  - 默认只监听 127.0.0.1：只有本机能打开，网络上的人访问不到；
  - 若把 web_host 改成对外地址，则必须用 config.json 里的账号登录才能修改；
  - 每次启动生成随机令牌，所有修改请求必须带上它（防止别的网页偷偷提交 CSRF）；
  - 写配置仍走 config_tools：先校验、备份、原子替换；密码只存哈希。
  - 保存后由服务的热加载机制生效；需要重启的项会在页面上标明。
  - HTTPS：web_https=auto（默认，对外地址自动启用）/ on / off。
    未指定证书时自动生成本页面专用的自签名证书 certs/web_cert.pem + web_key.pem
    （与 OPC UA 证书分开，互不影响）；也可用 web_cert_file / web_key_file 指定自己的证书。
"""
import base64
import hmac
import json
import os
import secrets
import ssl
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import config_tools

MAX_BODY = 64 * 1024
LOOPBACK = ("127.0.0.1", "localhost", "::1")


def _host_ips(extra_host=None):
    import ipaddress
    import socket
    dns, ips = {"localhost"}, {"127.0.0.1"}
    try:
        h = socket.gethostname()
        dns.add(h)
        for info in socket.getaddrinfo(h, None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:  # noqa
        pass
    if extra_host and extra_host not in ("0.0.0.0", "::", ""):
        try:
            ipaddress.ip_address(extra_host)
            ips.add(extra_host)
        except ValueError:
            dns.add(extra_host)
    return dns, ips


def cert_fingerprint(cert_path):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    with open(cert_path, "rb") as f:
        c = x509.load_pem_x509_certificate(f.read())
    fp = c.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(fp[i:i + 2] for i in range(0, len(fp), 2))


def ensure_web_cert(cert_dir, host=None, log=None):
    """生成/复用设置页面专用的自签名证书。证书里缺当前地址（换了 IP/主机名）或快过期时自动重新生成。
    返回 (证书路径, 私钥路径)。"""
    import datetime
    import ipaddress
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    os.makedirs(cert_dir, exist_ok=True)
    cert_p = os.path.join(cert_dir, "web_cert.pem")
    key_p = os.path.join(cert_dir, "web_key.pem")
    dns, ips = _host_ips(host)
    now = datetime.datetime.now(datetime.timezone.utc)

    def ok():
        try:
            with open(cert_p, "rb") as f:
                c = x509.load_pem_x509_certificate(f.read())
            san = c.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            have_dns = set(san.get_values_for_type(x509.DNSName))
            have_ip = {str(i) for i in san.get_values_for_type(x509.IPAddress)}
            return (dns <= have_dns and ips <= have_ip and os.path.exists(key_p)
                    and c.not_valid_after_utc - now > datetime.timedelta(days=30))
        except Exception:  # noqa
            return False

    if ok():
        return cert_p, key_p
    if log:
        log.info("生成设置页面 HTTPS 证书到 %s ...", cert_dir)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, u"ChromExtractOPC 设置页面")])
    san = [x509.DNSName(d) for d in sorted(dns)] + [x509.IPAddress(ipaddress.ip_address(i)) for i in sorted(ips)]
    cert = (x509.CertificateBuilder()
            .subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=825))   # 浏览器对证书有效期有上限
            .add_extension(x509.SubjectAlternativeName(san), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_encipherment=False,
                                         content_commitment=False, data_encipherment=False,
                                         key_agreement=False, key_cert_sign=False, crl_sign=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(key, hashes.SHA256()))
    tmp_k, tmp_c = key_p + ".tmp", cert_p + ".tmp"
    fd = os.open(tmp_k, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # 私钥尽量只给本账号读
    with os.fdopen(fd, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    with open(tmp_c, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    os.replace(tmp_k, key_p)
    os.replace(tmp_c, cert_p)
    return cert_p, key_p


def make_ssl_context(cert_p, key_p):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert_p, key_p)
    return ctx


def _public_view(raw, cfg_mod):
    """返回给前端的配置（合并默认值；不含密码哈希）。"""
    view = dict(cfg_mod.DEFAULT_CONFIG)
    view.update({k: v for k, v in raw.items() if k not in ("users", "smtp_password")})
    view.pop("smtp_password", None)
    view["smtp_password_set"] = bool(raw.get("smtp_password"))
    view["users"] = sorted((raw.get("users") or {}).keys())
    for k in ("web_ui", "web_host", "web_port", "web_https", "web_cert_file", "web_key_file"):
        view.setdefault(k, cfg_mod.DEFAULT_CONFIG.get(k))
    return view


def _smtp_hint(e):
    """把常见的 SMTP 错误翻译成人话。"""
    import smtplib
    import socket
    import ssl
    if isinstance(e, smtplib.SMTPAuthenticationError):
        return "用户名或密码不对（QQ/163/企业邮箱一般要用“授权码”而不是登录密码）。%s" % e
    if isinstance(e, ssl.SSLError):
        return "加密握手失败：端口和加密方式可能不匹配（465 用 SSL，587 用 STARTTLS）。%s" % e
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "连接超时：检查服务器地址、端口，以及本机防火墙是否允许访问外网邮件服务器。"
    if isinstance(e, (ConnectionRefusedError, socket.gaierror)):
        return "连不上邮件服务器：%s" % e
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        return "收件人被服务器拒收：%s" % e
    return str(e)


_PAGE_DICT = {}


def page_dictionary():
    """网页用的翻译字典（JSON，已转义 </ 防止提前结束 script）。"""
    import i18n
    if _PAGE_DICT.get("n") != len(i18n.EN):
        _PAGE_DICT["n"] = len(i18n.EN)
        _PAGE_DICT["json"] = json.dumps(i18n.EN, ensure_ascii=False).replace("</", "<\\/")
    return _PAGE_DICT["json"]


class _Catch:
    """把推送时的错误日志收集起来，直接显示在网页上。"""

    def __init__(self, box):
        self.box = box

    def warning(self, fmt, *a):
        self.box["err"] = fmt % a

    error = warning

    def info(self, *a):
        pass


class SettingsServer:
    """cfg_mod：server_app 模块（提供 DEFAULT_CONFIG / load_config / normalize_config /
    security_report / HOT_KEYS / RESTART_KEYS / ConfigError）。"""

    def __init__(self, cfg_path, base_dir, cfg_mod, host="127.0.0.1", port=8840, log=None,
                 https="auto", cert_file="", key_file="", cert_dir=None):
        self.cfg_path, self.base_dir, self.m = cfg_path, base_dir, cfg_mod
        self.host, self.port, self.log = host, int(port), log
        self.token = secrets.token_urlsafe(24)
        self.need_login = host not in LOOPBACK
        https = str(https or "auto").lower()
        self.https = https == "on" or (https == "auto" and self.need_login)
        self.cert_file, self.key_file = cert_file or "", key_file or ""
        self.cert_dir = cert_dir or os.path.join(base_dir, "certs")
        self.fingerprint = None
        self.httpd = None
        self._test_lock = threading.Lock()     # 同一时间只跑一个连接测试（串口不能被同时打开）
        self.stab = None                        # 当前/最近一次稳定性测试

    # ---- 校验：与服务启动时完全相同的规则 ----
    def _validate(self, data):
        cfg = self.m.load_config(None)
        cfg.update(data)
        cfg["_base_dir"] = self.base_dir
        self.m.normalize_config(cfg, self.base_dir)
        return cfg

    def _check_auth(self, header):
        if not self.need_login:
            return True
        if not header or not header.startswith("Basic "):
            return False
        try:
            name, pw = base64.b64decode(header[6:]).decode("utf-8").split(":", 1)
        except Exception:  # noqa
            return False
        users = config_tools.read_raw(self.cfg_path).get("users") or {}
        h = users.get(name)
        return bool(h) and config_tools.verify_password(pw, h)

    def state(self):
        raw = config_tools.read_raw(self.cfg_path)
        try:
            cfg = self._validate(raw)
            report = [{"level": lv, "text": t} for lv, t in self.m.security_report(cfg)]
            err = None
        except Exception as e:  # noqa
            report, err = [], str(e)
        return {"config": _public_view(raw, self.m), "report": report, "error": err,
                "cfg_path": self.cfg_path, "hot_keys": list(self.m.HOT_KEYS),
                "restart_keys": list(self.m.RESTART_KEYS) + ["web_ui", "web_host", "web_port"]}

    def _quiet_action(self, action, body):
        import alerts as _al
        from datetime import datetime, timedelta
        p = self.cfg_path
        now = datetime.now()
        fmt = "%Y-%m-%d %H:%M"
        cur = list(config_tools.read_raw(p).get("alert_quiet") or [])
        if action == "quiet_add":
            cur.append(body.get("item") or {})
            msg = "已添加静默时段。"
        elif action == "quiet_remove":
            try:
                cur.pop(int(body.get("index")))
            except (TypeError, ValueError, IndexError):
                raise config_tools.ConfigToolError("没有这条静默时段，请刷新页面。")
            msg = "已删除静默时段。"
        elif action == "quiet_now":
            try:
                hours = float(body.get("hours"))
            except (TypeError, ValueError):
                raise config_tools.ConfigToolError("时长不对。")
            if not 0 < hours <= 72:
                raise config_tools.ConfigToolError("临时静默只能 0～72 小时。")
            end = now + timedelta(hours=hours)
            cur.append({"type": "once", "start": now.strftime(fmt), "end": end.strftime(fmt),
                        "note": str(body.get("note") or "临时静默")})
            msg = "推送已静默到 %s。期间告警照常记录，结束时仍未恢复的会补发。" % end.strftime(fmt)
        else:   # quiet_end：删掉正在生效的一次性静默；每天重复的时段不动
            keep = []
            for it in cur:
                try:
                    active = it.get("type") == "once" and _al.quiet_status(_al.normalize_quiet([it])) is not None
                except ValueError:
                    active = False
                if not active:
                    keep.append(it)
            if len(keep) == len(cur):
                raise config_tools.ConfigToolError("当前没有生效中的一次性静默（每天重复的时段请在列表里删除）。")
            cur = keep
            msg = "已结束静默，仍未恢复的告警会在 15 秒内补发。"
        # 清掉已经过期的一次性静默，列表不会越积越长
        nowtxt = now.strftime(fmt)
        cur = [it for it in cur if not (isinstance(it, dict) and it.get("type") == "once"
                                        and str(it.get("end", "9999")) <= nowtxt)]
        config_tools.set_quiet(p, cur, _al.normalize_quiet)
        return msg

    def _stab_dir(self):
        return os.path.join(self.base_dir, "logs", "stability")

    def _probe_action(self, action, body):
        import probes
        p = self.cfg_path
        cur = list(config_tools.read_raw(p).get("alert_probes") or [])
        pid = str(body.get("id") or "")
        if action == "probe_run":
            mon = getattr(self.m, "ALERTS", {}).get("probes")
            if mon is not None and mon.has(pid):
                try:
                    r = mon.run_now(pid)
                except RuntimeError as e:
                    raise config_tools.ConfigToolError(str(e))
            else:
                item = next((x for x in probes.normalize_probes(cur) if x["id"] == pid), None)
                if item is None:
                    raise config_tools.ConfigToolError("没有这个设备检测，请刷新页面。")
                r = probes.run_probe_once(item)
            return {"summary": r.get("summary", ""), "ok": r.get("ok"), "details": r.get("details", []),
                    "elapsed_ms": r.get("elapsed_ms", 0), "kind": r.get("kind", "")}
        if action == "probe_save":
            item = dict(body.get("probe") or {})
            item["params"] = dict(item.get("params") or {})
            if item.get("kind") == "opcua":
                pw = item["params"].pop("password", "")
                old = next((x for x in cur if x.get("id") == item.get("id")), None)
                if pw:
                    item["params"]["password_enc"] = config_tools.protect_secret(pw)
                elif old and (old.get("params") or {}).get("password_enc"):
                    item["params"]["password_enc"] = old["params"]["password_enc"]   # 编辑时不填 = 保留原密码
            else:
                item["params"].pop("password", None)
            if not item.get("id"):
                import secrets as _s
                item["id"] = _s.token_hex(4)
            idx = next((i for i, x in enumerate(cur) if x.get("id") == item["id"]), None)
            if idx is None:
                cur.append(item)
                msg = "已添加设备检测「%s」，服务运行中会在几秒内开始检测。" % item.get("label", "")
            else:
                cur[idx] = item
                msg = "已保存设备检测「%s」。" % item.get("label", "")
        elif action == "probe_remove":
            cur = [x for x in cur if x.get("id") != pid]
            msg = "已删除设备检测（它的告警会直接移除，不发恢复）。"
        else:
            hit = False
            for x in cur:
                if x.get("id") == pid:
                    x["enabled"] = not x.get("enabled", True)
                    hit, state = True, x["enabled"]
            if not hit:
                raise config_tools.ConfigToolError("没有这个设备检测，请刷新页面。")
            msg = "已%s。" % ("启用" if state else "停用（告警直接移除，不发恢复）")
        config_tools.set_probes(p, cur, probes.normalize_probes)
        return msg

    def env_status(self):
        env = getattr(self.m, "ALERTS", {}).get("env")
        return env.status() if env is not None else None

    def probe_status(self):
        mon = getattr(self.m, "ALERTS", {}).get("probes")
        if mon is not None:
            return {"running": True, "probes": mon.status()}
        import probes
        return {"running": False, "probes": probes.status_from_config(config_tools.read_raw(self.cfg_path).get("alert_probes"))}

    def _busy_ports(self):
        run = self.stab
        if run is None or run.state != "running":
            return set()
        if hasattr(run, "busy_ports"):
            return run.busy_ports()
        return {str(run.params.get("port", "")).upper()} if run.kind in ("modbus_rtu", "serial") else set()

    def _stab_action(self, action, body):
        import stability
        run = self.stab
        if action == "stab_stop":
            if run is None or run.state != "running":
                raise config_tools.ConfigToolError("没有正在运行的稳定性测试。")
            run.stop()
            return "已请求停止，当前这一次测试完成后结束。"
        if run is not None and run.state == "running":
            raise config_tools.ConfigToolError("已有稳定性测试在运行，请先停止。")
        if body.get("targets"):
            try:
                run = stability.MultiRun(body.get("targets"), body.get("interval"), body.get("minutes"),
                                         out_dir=self._stab_dir(), log=self.log)
            except stability.StabilityError as e:
                raise config_tools.ConfigToolError(str(e))
            self.stab = run.start()
            msg = "多目标稳定性测试已开始：%d 个目标，每 %g 秒，共 %g 分钟。" % (len(run.runs), run.interval, run.duration / 60)
            for port, n in run.shared_ports().items():
                msg += " %s 上的 %d 个目标会按顺序轮询。" % (port, n)
            return msg + " 测试在程序里继续运行（关闭桌面主窗口会停止测试）。"
        try:
            run = stability.StabilityRun(str(body.get("kind") or ""), body.get("params") or {},
                                         body.get("interval"), body.get("minutes"),
                                         out_dir=self._stab_dir(), log=self.log)
        except stability.StabilityError as e:
            raise config_tools.ConfigToolError(str(e))
        self.stab = run.start()
        return "稳定性测试已开始：%s %s，每 %g 秒一次，共 %g 分钟。测试在程序里继续运行（关闭桌面主窗口会停止测试）。" % (
            run.stats()["kind_name"], run.target(), run.interval, run.duration / 60)

    def stab_status(self):
        if self.stab is None:
            return None
        s = self.stab.stats()
        s["multi"] = hasattr(self.stab, "runs")
        return s

    def alerts(self):
        mon = getattr(self.m, "ALERTS", {}).get("monitor")
        if mon is not None:
            return mon.snapshot()
        # 服务没运行（只开了设置页面）：也要能管理静默时段
        import alerts as _al
        try:
            q = _al.normalize_quiet(config_tools.read_raw(self.cfg_path).get("alert_quiet"))
        except Exception:  # noqa
            q = []
        try:
            rules = _al.normalize_channel_rules(config_tools.read_raw(self.cfg_path).get("alert_channel_rules"))
        except Exception:  # noqa
            rules = []
        chans = [{"key": r["channel"], "label": r["channel"].replace("|", " / "), "cycle_time": "",
                  "present": None, "idle_min": None, "muted": r["mute"], "stale_minutes": r["stale_minutes"],
                  "until": r["until"], "note": r["note"], "alerting": False} for r in rules]
        return {"offline": True, "active": [], "history": [], "last_scan": "", "channels": 0,
                "channel_list": chans,
                "worst": "正常", "quiet": _al.quiet_status(q),
                "quiet_list": [dict(it, text=_al.describe_quiet(it)) for it in q]}

    def handle_action(self, action, body, actor=None):
        """执行动作并写审计日志（logs/audit.log，带哈希链）。actor 形如 "web:用户名" / "desktop:用户名"。"""
        import audit as _audit
        actor = actor or "web"
        source = actor.split(":", 1)[0] if ":" in actor else actor
        user = actor.split(":", 1)[1] if ":" in actor else actor
        try:
            before = config_tools.read_raw(self.cfg_path)
        except Exception:  # noqa
            before = {}
        log = _audit.get_log(os.path.join(self.base_dir, "logs", "audit.log"))
        target = action
        if action == "save":
            target = ",".join(sorted((body.get("settings") or {}).keys()))[:200]
        elif action in ("add_user", "remove_user", "harden"):
            target = str(body.get("name") or "")
        try:
            res = self._handle_action(action, body)
        except Exception as e:  # noqa
            try:
                log.record(user, action, target, source=source, ok=False, detail=str(e))
            except Exception:  # noqa
                pass
            raise
        try:
            after = config_tools.read_raw(self.cfg_path)
            ch = _audit.diff(before, after)
            log.record(user, action, target, {k: v[0] for k, v in ch.items()} or None,
                       {k: v[1] for k, v in ch.items()} or None, source=source, ok=True,
                       detail=(res.get("summary", "") if isinstance(res, dict) else str(res))[:300])
        except Exception as e:  # noqa
            if self.log:
                self.log.error("审计日志写入失败：%s", e)
        return res

    def page(self):
        """设置页 HTML：注入防 CSRF 令牌、中英文字典（与桌面窗口同一份 i18n.EN）和默认语言。"""
        try:
            lang = str(config_tools.read_raw(self.cfg_path).get("desktop_language") or "auto").lower()
        except Exception:  # noqa
            lang = "auto"
        if lang not in ("zh", "en", "auto"):
            lang = "auto"
        return (PAGE.replace("__TOKEN__", self.token).replace("__I18N__", page_dictionary())
                .replace("__LANG__", lang))

    def auth_user(self, header):
        """网页登录的用户名（本机免登录时为空）。"""
        if not self.need_login or not header or not header.startswith("Basic "):
            return ""
        try:
            return base64.b64decode(header[6:]).decode("utf-8").split(":", 1)[0]
        except Exception:  # noqa
            return ""

    def _handle_action(self, action, body):
        p = self.cfg_path
        if action == "test_webhook":
            import alerts as _al
            cfg = self._validate(config_tools.read_raw(p))
            if not cfg.get("alert_webhook"):
                raise config_tools.ConfigToolError("请先填写并保存告警推送地址。")
            box = {}
            _al.make_webhook_notifier(cfg, _Catch(box)).send(
                "【%s】这是一条测试消息，收到说明告警推送已配置好。" % (cfg.get("server_name") or "色谱提取 OPC UA 服务"))
            if box.get("err"):
                raise config_tools.ConfigToolError("发送失败：%s" % box["err"])
            return "测试消息已发送，请到群里确认是否收到。"
        if action in ("probe_save", "probe_remove", "probe_toggle", "probe_run"):
            return self._probe_action(action, body)
        if action == "drivercheck":
            env = getattr(self.m, "ALERTS", {}).get("env")
            if env is not None:
                r = env.run_once()               # 服务运行中：结果同时更新告警（问题出现/消失）
            else:
                import drivercheck
                r = drivercheck.run_all(self._validate(config_tools.read_raw(p)))
            if self.log:
                self.log.info("驱动与软件环境检测：%s", r["summary"])
            return {"summary": r["summary"], "ok": r["ok"], "drivercheck": r}
        if action in ("stab_start", "stab_stop"):
            return self._stab_action(action, body)
        if action == "nettest":
            import nettest
            port = str((body.get("params") or {}).get("port", "")).upper()
            if port and port in self._busy_ports():
                raise config_tools.ConfigToolError("%s 正在做稳定性测试，请先停止。" % port)
            if not self._test_lock.acquire(blocking=False):
                raise config_tools.ConfigToolError("上一个测试还没结束，请稍候。")
            try:
                r = nettest.run_test(str(body.get("kind") or ""), body.get("params") or {})
            finally:
                self._test_lock.release()
            if self.log:
                self.log.info("连接测试[%s]：%s", r.get("kind"), r.get("summary"))
            return r
        if action == "channel_rule":
            import alerts as _al
            r = body.get("rule") or {}
            if not isinstance(r, dict) or not r.get("channel"):
                raise config_tools.ConfigToolError("缺少通道。")
            config_tools.set_channel_rule(p, r, _al.normalize_channel_rules)
            if r.get("mute"):
                return "已屏蔽该通道的告警%s。" % ("，至 %s 自动恢复" % r["until"] if r.get("until") else "")
            if r.get("stale_minutes") not in (None, ""):
                return "已为该通道单独设置：%s 分钟无新周期才告警。" % r["stale_minutes"]
            return "已恢复该通道的默认告警规则。"
        if action in ("quiet_add", "quiet_remove", "quiet_now", "quiet_end"):
            return self._quiet_action(action, body)
        if action == "set_smtp_password":
            pw = body.get("password") or ""
            config_tools.set_smtp_password(p, pw)
            return "SMTP 密码已%s。" % ("加密保存" if pw else "清除")
        if action == "test_email":
            import alerts as _al
            cfg = self._validate(config_tools.read_raw(p))
            try:
                _al.send_email(cfg, "[测试] %s 告警邮件" % (cfg.get("server_name") or "色谱提取 OPC UA 服务"),
                               "这是一封测试邮件。收到说明告警邮件已配置好。\n\n收件人：%s" % cfg.get("alert_email_to"))
            except Exception as e:  # noqa
                raise config_tools.ConfigToolError("发送失败：%s" % _smtp_hint(e))
            return "测试邮件已发送到 %s，请查收（也看看垃圾邮件箱）。" % cfg.get("alert_email_to")
        if action == "save":
            changed = config_tools.update_settings(p, body.get("settings") or {}, self._validate)
            restart = [k for k in changed if k in self.m.RESTART_KEYS or k.startswith("web_")]
            desk = [k for k in changed if k.startswith("desktop_")]
            msg = "已保存：%s。" % ("、".join(changed) if changed else "没有改动")
            if restart:
                msg += " 以下项需要重启服务才会生效：%s。" % "、".join(restart)
            elif changed and len(desk) < len(changed):
                msg += " 将在下一轮扫描内自动生效。"
            if desk:
                msg += " 桌面窗口设置（%s）在窗口下次刷新设置时生效。" % "、".join(desk)
            return msg
        if action == "add_user":
            name, pw = (body.get("name") or "").strip(), body.get("password") or ""
            r = config_tools.add_user(p, name, pw)
            return "账号 %s %s。" % (name, "已重设密码" if r == "updated" else "已添加")
        if action == "remove_user":
            config_tools.remove_user(p, (body.get("name") or "").strip())
            return "账号已删除。"
        if action == "harden":
            name, pw = (body.get("name") or "").strip(), body.get("password") or ""
            return "已完成：" + "；".join(config_tools.harden(p, name, pw)) + "。请重启服务让加密生效。"
        raise config_tools.ConfigToolError("未知操作：%s" % action)

    # ---- HTTP ----
    def _make_handler(self):
        srv = self

        class H(BaseHTTPRequestHandler):
            server_version = "ChromExtractSettings"

            def log_message(self, fmt, *a):  # 不往控制台刷访问日志
                pass

            def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
                data = body if isinstance(body, bytes) else body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy",
                                 "default-src 'self'; img-src 'self' data:; script-src 'unsafe-inline'; style-src 'unsafe-inline'")
                if srv.https:
                    self.send_header("Strict-Transport-Security", "max-age=31536000")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _json(self, code, obj):
                self._send(code, json.dumps(obj, ensure_ascii=False))

            def _authed(self):
                if srv._check_auth(self.headers.get("Authorization")):
                    return True
                self._send(401, "需要登录（使用 config.json 里的 OPC UA 账号）", "text/plain; charset=utf-8",
                           {"WWW-Authenticate": 'Basic realm="ChromExtract", charset="UTF-8"'})
                return False

            def do_GET(self):
                if not self._authed():
                    return
                path = self.path.split("?", 1)[0]
                if path == "/":
                    return self._send(200, srv.page(), "text/html; charset=utf-8")
                if path == "/api/state":
                    st = srv.state()
                    st["https"] = srv.https
                    st["fingerprint"] = srv.fingerprint
                    st["alerts"] = srv.alerts()
                    return self._json(200, st)
                if path == "/api/env":
                    return self._json(200, {"env": srv.env_status()})
                if path == "/api/probes":
                    return self._json(200, srv.probe_status())
                if path == "/api/stability":
                    return self._json(200, {"stability": srv.stab_status()})
                if path.startswith("/stability/"):
                    name = path.rsplit("/", 1)[-1]
                    import re as _re
                    if not _re.fullmatch(r"stability_[a-z_]+_\d{8}_\d{6}(?:_t\d{1,2})?\.csv", name):
                        return self._send(404, "not found", "text/plain")
                    fp = os.path.join(srv._stab_dir(), name)
                    if not os.path.isfile(fp):
                        return self._send(404, "not found", "text/plain")
                    with open(fp, "rb") as f:
                        data = f.read()
                    return self._send(200, data, "text/csv; charset=utf-8",
                                      {"Content-Disposition": 'attachment; filename="%s"' % name})
                if path == "/api/alerts":
                    return self._json(200, {"alerts": srv.alerts()})
                self._send(404, "not found", "text/plain")

            def do_POST(self):
                if not self._authed():
                    return
                if not hmac.compare_digest(self.headers.get("X-Token", ""), srv.token):
                    return self._json(403, {"ok": False, "msg": "页面已过期，请刷新后重试。"})
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    n = 0
                if n <= 0 or n > MAX_BODY:
                    return self._json(400, {"ok": False, "msg": "请求太大或为空。"})
                try:
                    body = json.loads(self.rfile.read(n).decode("utf-8"))
                    action = self.path.split("?", 1)[0].rsplit("/", 1)[-1]
                    actor = "web:" + (srv.auth_user(self.headers.get("Authorization")) or "本机")
                    msg = srv.handle_action(action, body if isinstance(body, dict) else {}, actor=actor)
                    if isinstance(msg, dict):                       # 连接测试：直接返回结果，不刷新配置
                        return self._json(200, {"ok": True, "msg": msg.get("summary", ""), "test": msg})
                    if srv.log:
                        srv.log.info("网页设置：%s", msg)
                    st = srv.state()
                    st["https"], st["fingerprint"] = srv.https, srv.fingerprint
                    st["alerts"] = srv.alerts()
                    self._json(200, {"ok": True, "msg": msg, "state": st})
                except (config_tools.ConfigToolError, ValueError) as e:
                    self._json(400, {"ok": False, "msg": str(e)})
                except Exception as e:  # noqa
                    self._json(500, {"ok": False, "msg": "内部错误：%s" % e})

        return H

    def url(self):
        h = "127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host
        return "%s://%s:%d/" % ("https" if self.https else "http", h, self.port)

    def _resolve(self, p):
        return p if os.path.isabs(p) else os.path.join(self.base_dir, p)

    def start(self, open_browser=False):
        ctx = None
        if self.https:
            if self.cert_file or self.key_file:
                if not (self.cert_file and self.key_file):
                    raise ValueError("web_cert_file 和 web_key_file 必须同时指定")
                cert_p, key_p = self._resolve(self.cert_file), self._resolve(self.key_file)
            else:
                cert_p, key_p = ensure_web_cert(self.cert_dir, self.host, self.log)
            ctx = make_ssl_context(cert_p, key_p)    # 证书/私钥不对会在这里报错，而不是等到有人访问
            try:
                self.fingerprint = cert_fingerprint(cert_p)
            except Exception:  # noqa
                self.fingerprint = None
        self.httpd = ThreadingHTTPServer((self.host, self.port), self._make_handler())
        if ctx is not None:
            self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        if self.log:
            self.log.info("设置页面：%s%s", self.url(), "（需要账号登录）" if self.need_login else "（仅本机可访问）")
            if self.fingerprint:
                self.log.info("设置页面证书 SHA-256 指纹：%s（浏览器提示证书不受信任时，核对此指纹一致再继续）",
                              self.fingerprint)
            if self.need_login and not self.https:
                self.log.warning("设置页面对外开放但未启用 HTTPS：登录密码会在网络上明文传输！")
        if open_browser:
            try:
                webbrowser.open(self.url())
            except Exception:  # noqa
                pass
        return self

    def stop(self):
        if self.stab is not None:
            self.stab.stop()
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


PAGE = r"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>色谱提取 OPC UA 服务 · 设置</title>
<link rel="icon" type="image/png" href="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAFzklEQVR4nMWXXYhdVxXHf2vvfc49c+/MZOZOJtMwH2XSJtRWm0A74INF0SKICVRlCH3QB7EPliIoaAXB4lOKoiD0TVHwQWoDtbZ5EDQIJQpFo0hrShUaSjKZaWYymWRy5957zt57+XDufM/tTIaKCzYc1jln/f97fey9FmyU6WnL/1q2YMimF2fPhrHPfrPuan3TGuNjaDhYfqOy1c7eRBRQMXZB0T8Xy+Gla+efv7GKtU6go7j35LefEJe9YFwyqqqgupHi/kQBEUQM0eezGvwz77125uVVTFl9GD/57CmbZq8SAzF4LyD7QRdRdJvDFAU11jkxlpjnX3jv3JlXmJ62AsjE5787INa8I8YMa/ABkX3lggBNb8hcxIiiCFYUVQgqoBrEOqMx3CyMHrv2yvOLBlCxnDZpz3CMhV8Fl324Pqhw4p5lABqFJffCQiNlqe1KX4rYGItg0mrdBZ4E1ACoyqfQqNJxuSrkPu595wJ5MIz3t/jp597hkcO3+ehwg8nBFj8+dYmvPDxLKxhEQBBKLD4J4EoDWgcVQaQIkZF6f+vUJx6e/dmrFyZTZ1Dt7nMRwWhkpTBMjd7iX9drPH5kkYqLfGxkmalj81BYbrYSzv37IP0VL6gKInUAs9VmVKWapX7qoXtva1fkEjzmEb/iy0JVoeIizcKSuggoI7UccgcuMlzNCVG2pfU2AgCqURrNdtdEFBFCKzB4fJDxL44TciVLIm++38ejo7f420w/f7kywPf/dD9vLdR48Y0JXn77EP2ZJ8TNFFw3EGNM9+0LaFCGPj7E4CODzJ2fI2t6Li3U+MU/Rjl/uc5iM8Ea5Y/v1mmHsjKcKFuNdiXwQaKqmMRgqxYiJH0J/o5HrPDzi2P0VjwHKh6lDE1qPaqyDXzfBFAwzuCqDkkEV3MQy9AMZAVRpaz7NcLda3rHHNgVPyomNdjMIiLYmmU1YQMCZu+HyN0TEEoPpAaTlr+7aukBBNQroRnY0d8fCgHKHLCZRRJBo+J63VpZVseqHH3mKLZq0bj7ZbYvAkRKgKAUSwWur0ylWET6H+xn5NMj1CZqxDwiu5zpd52EIuu7Vq/kt3LS/rTzEipDFUI7kB5MSw/sInsnsHEjndKLeaS90CY5kABgrCEZKJ8r9cqezO4tBJ2DR4syphqV5EBCaAZa8y1cv0OMIImQHCiJpYMpsodq+GACBsQIGpSkNyEbyVCviBHSeoq/42lfb5P0JeV50ONwVUfjcoN0KMUkpXmR7r1NVwImisZmJDQDsRU48tVJHvreg7iaxTc82XCFfCmnNdfC9ZYesFl5Niy9uUSlniJW8A1PaAU03/l63zEHNCp3siKtPdCH81C7r5eeIzUCyuEnxpj7wxw9kzXmLy5yZ2YFNTAwVScdTPEhsvDXGxx6fISBqTorV1ewmSUse/L59h4IiKCFMnO0GJh67kSRelHJjMz89gqNyw3uf/ook58ZpTnTJP6nRVUdzTduc/zrD6AK1353Fbnm8W83Of70RwjNgKtarv/zhn3rR5eMcWZTOLYRsCLcztvpozP1uanqobkYg0Sv5JNj6ISSziS4XkfeyAlfPobYMkeyuQoxKPl948h3BFSpzFbwIcpAqPpfXrww+vcQR3pSu+lKdACKLIKoomqNsLzSTp567lcnrEhULbuoVebqFY2KONl0yMROC2ec2abrRFWyikNjRKwocHONgER9HTGnyz5WyoZUBA9m1V1h1ZhQpm7szA1rodvy3QYdrHNVFMSIoq/TMSWhp/JizJs3jEksqmHj/2tLOmsnneyiW/ODBmMSG/PmzSLmvwbEMD1trp79wSJRvybGilhnVdWjGlA+pKVBVb1YZ8U40Riemj33kwWmp82m0Wzi5LNfMi55wbjKPaqR7u3wXUpnNAshfz/69jeuvPbDl9ZHs1XpKA6f/NbBJOk5LUEfQxhCdX835jq6YsyCQS40W/43c78/M79xON0s/4fx/L/WFNkoLTsvDQAAAABJRU5ErkJggg==">
<style>
body{font-family:"Microsoft YaHei",system-ui,sans-serif;background:#f4f6f9;margin:0;color:#222}
header{background:#1f4e79;color:#fff;padding:14px 24px;font-size:18px}
main{max-width:920px;margin:0 auto;padding:16px}
section{background:#fff;border-radius:8px;padding:16px 20px;margin-bottom:14px;box-shadow:0 1px 3px rgba(0,0,0,.08)}
h2{font-size:16px;margin:0 0 12px;color:#1f4e79}
.row{display:grid;grid-template-columns:220px 1fr;gap:8px 14px;align-items:center;margin-bottom:10px}
.row small{grid-column:2;color:#777;margin-top:-6px}
input[type=text],input[type=number],input[type=password],select,textarea{width:100%;box-sizing:border-box;padding:6px 8px;border:1px solid #ccc;border-radius:4px;font:inherit}
textarea{min-height:64px;font-family:Consolas,monospace}
button{background:#1f4e79;color:#fff;border:0;border-radius:4px;padding:8px 16px;cursor:pointer;font:inherit}
button.sec{background:#e8edf3;color:#1f4e79}button.danger{background:#b23b3b}
.tag{font-size:11px;padding:1px 6px;border-radius:3px;margin-left:6px;background:#fff3cd;color:#8a6d00}
.lv{display:inline-block;width:42px;text-align:center;border-radius:3px;font-size:12px;margin-right:8px}
.lv-严重{background:#dc3545;color:#fff}.lv-警告{background:#fff3cd;color:#856404}
.lv-正常{background:#d4edda;color:#155724}.lv-注意{background:#fff3cd;color:#856404}.lv-风险{background:#f8d7da;color:#721c24}
#msg{position:sticky;top:44px;z-index:4;padding:10px 14px;border-radius:6px;display:none;margin-bottom:12px}
#msg.ok{background:#d4edda;display:block}#msg.err{background:#f8d7da;display:block}
#tabs{display:flex;gap:4px;border-bottom:2px solid #1f4e79;margin:0 0 14px;position:sticky;top:0;background:#f4f6f9;z-index:5;padding-top:6px}
#tabs a{padding:8px 18px;border-radius:6px 6px 0 0;color:#1f4e79;text-decoration:none;background:#e8edf3;position:relative}
#tabs a.on{background:#1f4e79;color:#fff}
#tabs .dot{display:none;position:absolute;top:5px;right:6px;width:7px;height:7px;border-radius:50%;background:#e0a800}
#tabs .dot.show{display:block}
#tabs .badge{display:inline-block;min-width:16px;padding:0 5px;margin-left:6px;border-radius:8px;background:#dc3545;color:#fff;font-size:11px;line-height:16px;text-align:center}
.tab{display:none}.tab.on{display:block}
#savebar{position:fixed;left:0;right:0;bottom:0;display:flex;align-items:center;gap:8px;padding:10px 24px;background:#fff;border-top:1px solid #ddd;box-shadow:0 -2px 6px rgba(0,0,0,.06);z-index:6}
#savebar.dirty{background:#fff8e1;border-top-color:#e0a800}
main{padding-bottom:70px}
.changed{outline:2px solid #e0a800;outline-offset:1px}
#ch_table th,#ch_table td{border-bottom:1px solid #eee;padding:5px 4px;text-align:left;vertical-align:middle}
#ch_table input{padding:3px 5px}
ul{padding-left:0;list-style:none;margin:0}li{padding:4px 0}
.users li{display:flex;justify-content:space-between;border-bottom:1px solid #eee}
.inline{display:flex;gap:8px}.inline input{flex:1}
details summary{cursor:pointer;color:#1f4e79;margin-bottom:10px}
</style></head><body>
<header>色谱提取 OPC UA 服务 <small style="opacity:.75;font-size:13px;margin-left:8px">运行状态 · 告警 · 诊断 · 设置</small>
<select id="langsel" onchange="setLang(this.value)" title="界面语言" style="float:right;font-size:13px"><option value="zh">中文</option><option value="en">English</option></select></header><main>
<div id="msg"></div>
<nav id="tabs"><a href="#status" data-t="status">运行状态<span class="dot" id="dot_status"></span></a><a href="#alerts" data-t="alerts">告警与通知<span class="dot" id="dot_alerts"></span></a><a href="#tools" data-t="tools">诊断工具<span class="dot" id="dot_tools"></span></a><a href="#system" data-t="system">系统设置<span class="dot" id="dot_system"></span></a></nav>
<div class="tab" id="tab_status"><section><h2>运行告警 <small id="al_sum" style="font-weight:normal;color:#666"></small></h2>
<ul id="al_active"></ul>
<div id="al_quiet" style="margin:6px 0"></div>
<details><summary>最近的告警记录</summary><ul id="al_hist" style="font-size:13px;color:#555"></ul></details></section>

<section><h2>设备检测 <small id="pr_sum" style="font-weight:normal;color:#666"></small></h2>
<p style="color:#777;font-size:13px;margin-top:0">服务运行时按间隔自动检测关键设备（DCS、网关、分析仪等），连续失败达到次数就告警，一次成功自动恢复；告警走上面设置的群机器人 / 邮件，遵守静默时段和渠道级别。添加方法：在下面“连接测试”里选好类型和参数、测通后，点“加为设备检测”。</p>
<table id="pr_table" style="width:100%;border-collapse:collapse;font-size:13px"></table>
</section>

<section><h2>通道告警设置</h2>
<p style="color:#777;font-size:13px;margin-top:0">长期停用或检修中的流路可以屏蔽告警；轮换间隔长的流路可以单独加大"无新周期"分钟数。屏蔽时该通道的告警直接移除，不发"恢复"；可设到期时间自动解除。</p>
<table id="ch_table" style="width:100%;border-collapse:collapse;font-size:13px"></table>
</section></div>
<div class="tab" id="tab_alerts"><section><h2>告警设置</h2>
<div class="row"><label>通道无新周期告警（分钟）</label><input type="number" id="alert_stale_minutes" min="0">
<small>超过这么久没有新周期就告警（分析仪停机、数据库不再更新）；按分析周期设，例如周期 10 分钟可设 60；0 = 不检查</small></div>
<div class="row"><label>连续扫描出错告警（次）</label><input type="number" id="alert_scan_errors" min="0"><small>0 = 不检查</small></div>
<div class="row"><label>告警推送地址（可选）</label><input type="text" id="alert_webhook" placeholder="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...">
<small>企业微信 / 钉钉 / 飞书群机器人的 Webhook 地址，或任意接收 JSON 的 https 地址；告警出现和恢复时各推送一次。钉钉机器人请在安全设置里加关键词"色谱"</small></div>
<div class="row"><label>群机器人发送级别</label><select id="alert_webhook_level"><option value="all">严重和警告都发</option><option value="critical">只发严重告警</option><option value="off">不发</option></select></div>
<div class="row"><label></label><div><button class="sec" onclick="post('test_webhook',{})">发送测试消息</button></div></div>
<h2 style="margin-top:18px">静默时段（计划停机 / 检修时不推送）</h2>
<p style="color:#777;font-size:13px;margin-top:0">静默期间群机器人和邮件都不发；日志、本页面和 OPC UA 告警节点照常显示。静默结束时仍未恢复的告警会补发一次；静默期间出现又恢复的不再打扰。时间按本机时间，添加后立即生效。</p>
<ul id="quiet_list" class="users"></ul>
<div class="row"><label>临时静默</label><div class="inline">
<button class="sec" onclick="quietNow(2)">2 小时</button><button class="sec" onclick="quietNow(8)">8 小时</button>
<button class="sec" onclick="quietNow(24)">24 小时</button><button class="sec" onclick="post('quiet_end',{})">立即结束静默</button></div></div>
<div class="row"><label>一次性时段</label><div class="inline">
<input type="datetime-local" id="q_s"><input type="datetime-local" id="q_e"><input type="text" id="q_n" placeholder="备注，如 大修" style="max-width:140px">
<button class="sec" onclick="quietOnce()">添加</button></div><small>最长 31 天</small></div>
<div class="row"><label>每天重复时段</label><div class="inline">
<input type="time" id="d_s"><input type="time" id="d_e"><input type="text" id="d_n" placeholder="备注，如 夜班" style="max-width:120px">
<button class="sec" onclick="quietDaily()">添加</button></div>
<small id="d_days"></small></div>

<h2 style="margin-top:18px">邮件通知（可选）</h2>
<div class="row"><label>收件人</label><input type="text" id="alert_email_to" placeholder="a@company.com, b@company.com">
<small>多个用逗号隔开；留空 = 不发邮件。告警出现和恢复时各发一封，每小时最多 20 封</small></div>
<div class="row"><label>邮件发送级别</label><select id="alert_email_level"><option value="all">严重和警告都发</option><option value="critical">只发严重告警</option><option value="off">不发</option></select>
<small>严重 = 扫描卡死、连续扫描出错；警告 = 无数据、数据源丢失、通道长时间不更新。恢复通知只发给收到过该告警的渠道</small></div>
<div class="row"><label>SMTP 服务器</label><input type="text" id="smtp_host" placeholder="smtp.exmail.qq.com"></div>
<div class="row"><label>端口 / 加密方式</label><div class="inline"><input type="number" id="smtp_port" min="1" max="65535" style="max-width:110px">
<select id="smtp_security"><option value="ssl">SSL（一般 465）</option><option value="starttls">STARTTLS（一般 587）</option><option value="none">不加密（仅内网免登录中继，25）</option></select></div></div>
<div class="row"><label>用户名</label><input type="text" id="smtp_user" placeholder="alarm@company.com"><small>免登录的内网中继可留空</small></div>
<div class="row"><label>发件人地址</label><input type="text" id="smtp_from" placeholder="留空 = 用户名"></div>
<div class="row"><label>密码 / 授权码</label><div class="inline"><input type="password" id="smtp_pw" autocomplete="new-password" placeholder="">
<button class="sec" onclick="setSmtpPw()">保存密码</button><button class="sec" onclick="clearSmtpPw()">清除</button></div>
<small id="smtp_pw_state"></small></div>
<div class="row"><label></label><div><button class="sec" onclick="post('test_email',{})">发送测试邮件</button>
<small style="color:#777">先点下面的“保存设置”，再测试</small></div></div>
</section></div>
<div class="tab" id="tab_tools"><section><h2>驱动与软件环境检测</h2>
<p style="color:#777;font-size:13px;margin-top:0">只读检查，不会安装或修改任何东西：串口设备和驱动（CH340 / FTDI / CP210x / PL2303 等，含黄色感叹号设备）、运行库、防火墙服务、OPC UA 端口、数据目录、旧版本遗留的后台自启。现场装好后、或串口突然不能用时先跑一遍。</p>
<div class="row"><label>服务启动时自动检测<span class="tag">需重启</span></label><input type="checkbox" id="env_check_on_start">
<small>服务启动后约 8 秒自动检测；“问题”级别的项会告警（警告级），复查时消失自动恢复；“注意”只记日志</small></div>
<div class="row"><label>定期复查（小时）</label><input type="number" id="env_check_hours" min="0" max="168" style="max-width:100px"><small>0 = 只在启动时检查；改完点页面底部“保存设置”</small></div>
<p id="env_last" style="font-size:13px;color:#555;margin:4px 0 8px"></p>
<button onclick="dcRun()" id="dc_btn">开始检测</button> <small id="dc_time" style="color:#777"></small>
<div id="dc_out" style="margin-top:10px"></div>
</section>

<section><h2>连接测试</h2>
<p style="color:#777;font-size:13px;margin-top:0">现场排查用：网络（Ping / TCP 端口 / DNS / OPC UA / Modbus TCP）和串口（RS-232 / RS-485 / RS-422，Modbus RTU）。测试在<b>运行本服务的这台电脑</b>上执行。</p>
<div class="inline" style="flex-wrap:wrap;margin-bottom:10px" id="nt_tabs"></div>
<div id="nt_form"></div>
<div style="margin-top:8px"><button onclick="ntRun()" id="nt_btn">开始测试</button> <small id="nt_time" style="color:#777"></small></div>
<div class="inline" style="margin-top:8px;align-items:center;flex-wrap:wrap;font-size:13px" id="pr_add">
<b style="margin-right:6px">加为设备检测：</b>名称<input type="text" id="pr_label" placeholder="如 DCS 服务器" style="max-width:140px">
间隔(秒)<input type="number" id="pr_iv" value="60" min="10" style="max-width:80px">
连续失败<input type="number" id="pr_thr" value="3" min="1" style="max-width:60px">次告警
<select id="pr_lv" style="max-width:90px"><option value="警告">警告</option><option value="严重">严重</option></select>
<button class="sec" onclick="prAdd()">加为设备检测</button><input type="hidden" id="pr_edit"></div>
<div id="nt_out" style="margin-top:10px;display:none;border-radius:6px;padding:10px 12px;font-size:13px"></div>
<div id="st_box" style="margin-top:14px;border-top:1px dashed #ddd;padding-top:12px">
<b>长时间稳定性测试</b> <small style="color:#777">用上面同样的参数反复测试，统计丢包率、延迟和错误。支持 Ping / TCP 端口 / DNS / OPC UA / Modbus TCP / Modbus RTU / 串口收发；关掉网页不影响，关闭桌面主窗口会停止测试。</small>
<div class="inline" style="margin-top:8px;align-items:center">间隔(秒)<input type="number" id="st_iv" value="1" min="0.2" step="0.1" style="max-width:90px">
时长<select id="st_min" style="max-width:130px"><option value="10">10 分钟</option><option value="60">1 小时</option><option value="480">8 小时</option><option value="1440">24 小时</option><option value="4320">72 小时</option></select>
<button onclick="stStart()">开始稳定性测试</button><button class="danger" onclick="post('stab_stop',{}).then(stPoll)">停止</button></div>
<div style="margin-top:10px;font-size:13px"><b>多目标对比</b> <small style="color:#777">在上面依次选好每个目标的参数，点“加入对比列表”，最多 16 个；同一串口上的多个站会自动按顺序轮询（RS-485 不能同时通信），不同串口和网络目标真正并行。结束后会分析各目标的失败是否同时发生。</small>
<div class="inline" style="margin:6px 0;align-items:center"><input type="text" id="mt_label" placeholder="名称，如 GC-1" style="max-width:140px"><button class="sec" onclick="mtAdd()">加入对比列表</button>
<button onclick="mtStart()">开始多目标测试</button><button class="sec" onclick="mtList=[];mtSave();mtShow()">清空列表</button></div>
<ul id="mt_list" class="users"></ul></div>
<div id="st_view" style="margin-top:10px"></div></div>
</section></div>
<div class="tab" id="tab_system"><section><h2>数据源</h2>
<div class="row"><label>监控目录（每行一个）</label><textarea id="watch_dirs"></textarea>
<small>存放 *_V3.db / *.mbd 的文件夹，必须真实存在，例如 D:\GCP\DataBrowser</small></div>
<div class="row"><label>扫描周期（秒）</label><input type="number" id="poll_seconds" min="5" max="3600"></div>
<div class="row"><label>每流路最近周期数</label><input type="number" id="last_n" min="1" max="100"></div>
<div class="row"><label>单曲线最大发布点数</label><input type="number" id="max_points" min="100" max="200000"></div>
</section>

<section><h2>OPC UA 服务</h2>
<div class="row"><label>监听地址 endpoint<span class="tag">需重启</span></label><input type="text" id="endpoint"></div>
<div class="row"><label>服务器名称<span class="tag">需重启</span></label><input type="text" id="server_name"></div>
<div class="row"><label>最大连接数<span class="tag">需重启</span></label><input type="number" id="max_connections" min="1" max="200"></div>
<div class="row"><label>通信安全模式<span class="tag">需重启</span></label>
<select id="security"><option value="none">none（不加密）</option><option value="sign">sign（签名）</option><option value="sign_encrypt">sign_encrypt（签名+加密，推荐）</option></select></div>
<div class="row"><label>允许匿名只读连接</label><input type="checkbox" id="allow_anonymous"></div>
<div class="row"><label>保留不加密备用入口<span class="tag">需重启</span></label><input type="checkbox" id="allow_insecure_fallback">
<small>仅排障用，用完请关闭</small></div>
</section>

<section><h2>CSV 导出</h2>
<div class="row"><label>允许远程调用 ExportCSV</label><input type="checkbox" id="remote_export"></div>
<div class="row"><label>远程导出最短间隔（秒）</label><input type="number" id="export_min_interval_sec" min="0" max="3600"></div>
<div class="row"><label>导出目录</label><input type="text" id="csv_export_dir"></div>
</section>

<section><details><summary>高级设置</summary>
<div class="row"><label>单条谱图最大采样点</label><input type="number" id="max_trace_points"></div>
<div class="row"><label>单个 .mbd 上限（MB）</label><input type="number" id="max_file_mb"></div>
<div class="row"><label>单个日志大小（MB）</label><input type="number" id="log_max_mb"></div>
<div class="row"><label>保留历史日志个数</label><input type="number" id="log_backups"></div>
<div class="row"><label>启用设置网页<span class="tag">需重启</span></label><input type="checkbox" id="web_ui"></div>
<div class="row"><label>设置网页监听地址<span class="tag">需重启</span></label><input type="text" id="web_host">
<small>127.0.0.1 = 只有本机能打开（推荐）；改成其它地址后必须用账号登录</small></div>
<div class="row"><label>设置网页端口<span class="tag">需重启</span></label><input type="number" id="web_port"></div>
<div class="row"><label>HTTPS<span class="tag">需重启</span></label>
<select id="web_https"><option value="auto">auto（对外开放时自动启用，推荐）</option><option value="on">on（始终启用）</option><option value="off">off（不启用）</option></select>
<small>未指定证书时自动生成自签名证书；浏览器首次会提示"不安全"，核对页面顶部显示的指纹一致后继续即可</small></div>
<div class="row"><label>自有证书文件（PEM）<span class="tag">需重启</span></label><input type="text" id="web_cert_file" placeholder="留空 = 自动生成"></div>
<div class="row"><label>自有私钥文件（PEM）<span class="tag">需重启</span></label><input type="text" id="web_key_file" placeholder="留空 = 自动生成"></div>
</details></section>

<section><h2>登录账号</h2><ul class="users" id="users"></ul>
<div class="inline" style="margin-top:10px"><input type="text" id="u_name" placeholder="用户名">
<input type="password" id="u_pw" placeholder="密码（至少 8 位）"><input type="password" id="u_pw2" placeholder="再输入一次">
<button onclick="addUser()">添加 / 重设</button></div>
<p><button class="danger" onclick="harden()">一键加固（开启加密 + 关闭匿名 + 用上面的账号）</button></p>
</section>

<section><h2>安全体检</h2><ul id="report"></ul><small id="path" style="color:#888"></small><br><small id="tls" style="color:#888"></small></section></div>
<div id="savebar"><span id="dirty_txt">没有未保存的修改</span><span style="flex:1"></span>
<button class="sec" onclick="load()">放弃修改</button> <button onclick="save()">保存设置</button></div>
</main>
<script>
const TOKEN="__TOKEN__";
const F={str:["endpoint","server_name","security","csv_export_dir","web_host","web_https","web_cert_file","web_key_file","alert_webhook","alert_email_to","smtp_host","smtp_security","smtp_user","smtp_from","alert_webhook_level","alert_email_level"],
 num:["poll_seconds","last_n","max_points","max_connections","export_min_interval_sec","max_trace_points","max_file_mb","log_max_mb","log_backups","web_port","alert_stale_minutes","alert_scan_errors","smtp_port","env_check_hours"],
 bool:["allow_anonymous","allow_insecure_fallback","remote_export","web_ui","env_check_on_start"]};
let orig={};
const $=id=>document.getElementById(id);
const LS={get(k){try{return localStorage.getItem(k)}catch(e){return null}},set(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
/* v3.1 中英文切换：与桌面窗口同一份字典（i18n.EN，以中文原文为键）。只翻译界面文字，日志与节点名不翻译 */
const I18N=__I18N__;
let LANG=LS.get("lang")||"__LANG__";
if(LANG!=="zh"&&LANG!=="en")LANG=(navigator.language||"zh").toLowerCase().startsWith("zh")?"zh":"en";
const ORIGT=new WeakMap();let I18N_BUSY=false;const TITLE_ZH=document.title;
function T(s){if(LANG!=="en"||typeof s!=="string")return s;const k=s.replace(/\s+/g," ").trim();const v=I18N[k];
 if(!v)return s;return s.match(/^\s*/)[0]+v+s.match(/\s*$/)[0]}
function trNode(n){if(!n)return;
 if(n.nodeType===3){const p=n.parentNode;if(p&&(p.nodeName==="SCRIPT"||p.nodeName==="STYLE"||p.nodeName==="TEXTAREA"))return;
  let o=ORIGT.get(n);if(!o||o.shown!==n.nodeValue){o={zh:n.nodeValue,shown:null};ORIGT.set(n,o)}
  const v=LANG==="en"?T(o.zh):o.zh;o.shown=v;if(n.nodeValue!==v)n.nodeValue=v;return}
 if(n.nodeType!==1||n.nodeName==="SCRIPT"||n.nodeName==="STYLE")return;
 for(const a of ["placeholder","title"]){if(!n.hasAttribute(a))continue;
  if(!n.hasAttribute("data-zh-"+a)||n.getAttribute(a)!==n.getAttribute("data-shown-"+a))n.setAttribute("data-zh-"+a,n.getAttribute(a));
  const v=LANG==="en"?T(n.getAttribute("data-zh-"+a)):n.getAttribute("data-zh-"+a);n.setAttribute("data-shown-"+a,v);n.setAttribute(a,v)}
 for(const c of Array.from(n.childNodes))trNode(c)}
function applyLang(){I18N_BUSY=true;try{trNode(document.body);document.documentElement.lang=LANG==="en"?"en":"zh-CN";
 document.title=LANG==="en"?T(TITLE_ZH):TITLE_ZH;const s=$("langsel");if(s)s.value=LANG}finally{I18N_BUSY=false}}
function setLang(v){LANG=v==="en"?"en":"zh";LS.set("lang",LANG);applyLang()}
new MutationObserver(ms=>{if(I18N_BUSY)return;I18N_BUSY=true;try{for(const m of ms){if(m.type==="characterData")trNode(m.target);else m.addedNodes.forEach(trNode)}}finally{I18N_BUSY=false}})
 .observe(document.body,{subtree:true,childList:true,characterData:true});
const _confirm0=window.confirm.bind(window);window.confirm=m=>_confirm0(T(m));
let msgTimer=null;
function show(ok,t){const m=$("msg");m.className=ok?"ok":"err";m.textContent=t;clearTimeout(msgTimer);
 if(ok)msgTimer=setTimeout(()=>{m.className=""},6000);
 const r=m.getBoundingClientRect();if(r.top<0||r.top>innerHeight)window.scrollTo({top:0,behavior:"smooth"})}
const TABS=["status","alerts","tools","system"];
function tab(k){if(!TABS.includes(k))k="status";
 TABS.forEach(t=>{$("tab_"+t).classList.toggle("on",t===k);document.querySelector('#tabs a[data-t="'+t+'"]').classList.toggle("on",t===k)});
 if(location.hash!=="#"+k){try{history.replaceState(null,"","#"+k)}catch(e){}}LS.set("tab",k)}
document.querySelectorAll("#tabs a").forEach(a=>a.onclick=e=>{e.preventDefault();tab(a.dataset.t)});
window.addEventListener("hashchange",()=>tab(location.hash.slice(1)));
function fieldIds(){return F.str.concat(F.num,F.bool,["watch_dirs"])}
function curVal(k){const el=$(k);if(!el)return null;if(k==="watch_dirs")return JSON.stringify(el.value.split("\n").map(x=>x.trim()).filter(Boolean));
 return el.type==="checkbox"?el.checked:el.value}
function origVal(k){if(k==="watch_dirs")return JSON.stringify(orig.watch_dirs||[]);if(F.bool.includes(k))return !!orig[k];return String(orig[k]??"")}
function markDirty(){let n=0;const tabs=new Set();
 fieldIds().forEach(k=>{const el=$(k);if(!el)return;const d=curVal(k)!==origVal(k);el.classList.toggle("changed",d);
  if(d){n++;const t=el.closest(".tab");if(t)tabs.add(t.id.slice(4))}});
 TABS.forEach(t=>$("dot_"+t).classList.toggle("show",tabs.has(t)));
 $("savebar").classList.toggle("dirty",n>0);
 $("dirty_txt").textContent=n?("有 "+n+" 项修改未保存"+(tabs.size?"（"+[...tabs].map(t=>document.querySelector('#tabs a[data-t="'+t+'"]').firstChild.textContent).join("、")+"）":"")):"没有未保存的修改";
 return n}
document.addEventListener("input",e=>{if(e.target.id&&fieldIds().includes(e.target.id))markDirty()});
document.addEventListener("change",e=>{if(e.target.id&&fieldIds().includes(e.target.id))markDirty()});
window.addEventListener("beforeunload",e=>{if(Object.keys(orig).length&&markDirty()){e.preventDefault();e.returnValue=""}});
function badge(t,n,lv){const a=document.querySelector('#tabs a[data-t="'+t+'"]');let b=a.querySelector(".badge");
 if(!n){if(b)b.remove();return}if(!b){b=document.createElement("span");b.className="badge";a.insertBefore(b,a.querySelector(".dot"))}
 b.textContent=n;b.style.background=lv==="严重"?"#dc3545":"#e0a800"}
function esc(s){const d=document.createElement("div");d.textContent=s;return d.innerHTML}
function render(st){const c=st.config;orig=c;
 F.str.concat(F.num).forEach(k=>$(k).value=c[k]??"");F.bool.forEach(k=>$(k).checked=!!c[k]);
 $("watch_dirs").value=(c.watch_dirs||[]).join("\n");
 setTimeout(markDirty,0);
 $("report").innerHTML=st.error?'<li><span class="lv lv-风险">错误</span>'+esc(st.error)+'</li>':
  st.report.map(r=>'<li><span class="lv lv-'+r.level+'">'+r.level+'</span>'+esc(r.text)+'</li>').join("");
 $("path").textContent="配置文件："+st.cfg_path;
 badge("system",(st.report||[]).filter(r=>r.level==="风险").length+(st.error?1:0),"严重");
 $("smtp_pw").placeholder=c.smtp_password_set?"已设置（不显示）":"未设置";
 $("smtp_pw_state").textContent=c.smtp_password_set?"密码已加密保存在配置文件里。QQ/163/企业邮箱请填“授权码”。":"QQ/163/企业邮箱请填“授权码”，不是网页登录密码。";
 $("tls").textContent=st.https?("本页面已使用 HTTPS 加密"+(st.fingerprint?"，证书指纹 SHA-256："+st.fingerprint:"")):"本页面未使用 HTTPS（仅本机访问时无影响）";
 $("users").innerHTML=(c.users.length?c.users:[]).map(u=>'<li><span>'+esc(u)+'</span><button class="sec" data-u="'+esc(u)+'">删除</button></li>').join("")||"<li>（无账号）</li>";
 document.querySelectorAll("#users button").forEach(b=>b.onclick=()=>removeUser(b.dataset.u));}
const WD="一二三四五六日";
function initDays(){let h="星期（都不勾 = 每天）：";for(let i=1;i<=7;i++)h+='<label style="margin-right:6px"><input type="checkbox" class="dd" value="'+i+'">周'+WD[i-1]+'</label>';
 h+="；结束早于开始表示跨午夜，例如 22:00～06:00";$("d_days").innerHTML=h}
function dt(v){return v?v.replace("T"," ").slice(0,16):""}
function quietNow(h){post("quiet_now",{hours:h})}
function quietOnce(){post("quiet_add",{item:{type:"once",start:dt($("q_s").value),end:dt($("q_e").value),note:$("q_n").value}})}
function quietDaily(){const days=[...document.querySelectorAll(".dd:checked")].map(x=>+x.value);
 post("quiet_add",{item:{type:"daily",start:$("d_s").value,end:$("d_e").value,days:days,note:$("d_n").value}})}
function renderQuiet(a){
 $("al_quiet").innerHTML=a&&a.quiet?'<span class="lv lv-警告" style="width:auto;padding:0 6px">推送静默中</span> 至 '+esc(a.quiet.until)+(a.quiet.note?"（"+esc(a.quiet.note)+"）":""):"";
 const L=(a&&a.quiet_list)||[];
 $("quiet_list").innerHTML=L.length?L.map((q,i)=>'<li><span>'+esc(q.text)+'</span><button class="sec" data-i="'+i+'">删除</button></li>').join(""):"<li>（没有静默时段）</li>";
 document.querySelectorAll("#quiet_list button").forEach(b=>b.onclick=()=>post("quiet_remove",{index:+b.dataset.i}));}
const NT={
 ping:{t:"Ping",f:[["host","目标地址","192.168.1.10"],["count","次数","4"],["timeout","每次超时(秒)","1"]]},
 tcp:{t:"TCP 端口",f:[["host","目标地址","192.168.1.10"],["port","端口","4840"],["timeout","超时(秒)","3"],["send_text","发送文本（可选）",""]]},
 dns:{t:"DNS 解析",f:[["host","主机名","dcs-server"]]},
 interfaces:{t:"本机网卡",f:[]},
 opcua:{t:"OPC UA",f:[["endpoint","服务器地址","opc.tcp://127.0.0.1:4840/ChromExtract"],["username","用户名（可选）",""],["password","密码（可选）","","password"],["timeout","超时(秒)","5"]]},
 modbus_tcp:{t:"Modbus TCP",f:[["host","设备地址","192.168.1.20"],["port","端口","502"],["unit","站号","1"],["func","功能码","3","fc"],["addr","起始地址（从 0 开始）","0"],["count","数量","10"],["timeout","超时(秒)","3"]]},
 serial_list:{t:"串口列表",f:[]},
 serial:{t:"串口收发 232/485",f:"S",x:[["send","发送内容（留空 = 只监听）","01 03 00 00 00 0A C5 CD"],["mode","格式","hex","mode"],["timeout","接收等待(秒)","1"],["rs485_rts","RTS 控制收发方向（无自动切换的 485 转换器）","","chk"]]},
 loopback:{t:"串口回环自检",f:"S",x:[["timeout","超时(秒)","1"]],tip:"先把串口 TX 和 RX 短接（DB9 的 2、3 脚），用于确认串口和转换器本身是好的。RS-485 半双工不适用。"},
 modbus_rtu:{t:"Modbus RTU",f:"S",x:[["unit","站号","1"],["func","功能码","3","fc"],["addr","起始地址（从 0 开始）","0"],["count","数量","10"],["timeout","超时(秒)","1"],["rs485_rts","RTS 控制收发方向","","chk"]]}};
const SER=[["port","串口","COM3","port"],["baud","波特率","9600","baud"],["bytesize","数据位","8","bits"],["parity","校验","N","parity"],["stopbits","停止位","1","stop"]];
let ntKind="ping",ntPorts=[];
function sel(id,opts,v){return '<select id="'+id+'">'+opts.map(o=>'<option value="'+o[0]+'"'+(String(o[0])===String(v)?" selected":"")+'>'+o[1]+'</option>').join("")+'</select>'}
function ntField(f){const [k,l,d,ty]=f,id="nt_"+k,v=LS.get(id)??d;let el;
 if(ty==="password")el='<input type="password" id="'+id+'" autocomplete="off">';
 else if(ty==="chk")el='<input type="checkbox" id="'+id+'"'+(v==="true"?" checked":"")+'>';
 else if(ty==="fc")el=sel(id,[[1,"01 读线圈"],[2,"02 读离散输入"],[3,"03 读保持寄存器"],[4,"04 读输入寄存器"]],v);
 else if(ty==="mode")el=sel(id,[["hex","十六进制"],["text","文本（\\r\\n 表示回车换行）"]],v);
 else if(ty==="baud")el=sel(id,[1200,2400,4800,9600,19200,38400,57600,115200].map(b=>[b,b]),v);
 else if(ty==="bits")el=sel(id,[[8,8],[7,7]],v);
 else if(ty==="parity")el=sel(id,[["N","无 N"],["E","偶 E"],["O","奇 O"]],v);
 else if(ty==="stop")el=sel(id,[[1,1],[2,2]],v);
 else if(ty==="port")el='<div class="inline"><input type="text" id="'+id+'" list="nt_portlist" value="'+esc(v)+'"><button class="sec" onclick="ntScan()">扫描串口</button></div><datalist id="nt_portlist">'+ntPorts.map(p=>'<option value="'+esc(p)+'">').join("")+'</datalist>';
 else el='<input type="text" id="'+id+'" value="'+esc(v)+'" placeholder="'+esc(d)+'">';
 return '<div class="row"><label>'+l+'</label>'+el+'</div>'}
function ntShow(k){ntKind=k;const c=NT[k];
 $("nt_tabs").innerHTML=Object.keys(NT).map(x=>'<button class="'+(x===k?"":"sec")+'" onclick="ntShow(\''+x+'\')" style="margin:0 6px 6px 0">'+NT[x].t+'</button>').join("");
 const fs=c.f==="S"?SER.concat(c.x):c.f;
 $("nt_form").innerHTML=(c.tip?'<p style="color:#856404;font-size:13px">'+c.tip+'</p>':'')+fs.map(ntField).join("")||'<p style="color:#777">无需参数，直接点“开始测试”。</p>';}
function ntParams(){const c=NT[ntKind],fs=c.f==="S"?SER.concat(c.x):c.f,p={};
 fs.forEach(f=>{const el=$("nt_"+f[0]);if(!el)return;p[f[0]]=el.type==="checkbox"?el.checked:el.value;
  if(f[3]!=="password")LS.set("nt_"+f[0],String(p[f[0]]));});return p}
async function apiPost(path,body){let r;
 try{r=await fetch(path,{method:"POST",headers:{"Content-Type":"application/json","X-Token":TOKEN},body:JSON.stringify(body)})}
 catch(e){return {ok:false,msg:"连接不到服务：服务可能已停止或正在重启。请确认桌面窗口仍然打开、服务在运行（窗口顶部显示“服务运行中”），然后刷新页面。"}}
 if(r.status===401)return {ok:false,msg:"需要重新登录：请刷新页面。"};
 if(r.status===403)return {ok:false,msg:"页面已过期（服务重启过）：请刷新页面后重试。"};
 try{return await r.json()}catch(e){return {ok:false,msg:"服务返回了无法识别的内容（HTTP "+r.status+"），请刷新页面后重试。"}}}
async function ntCall(kind,params){return apiPost("/api/nettest",{kind:kind,params:params})}
async function ntScan(){const j=await ntCall("serial_list",{});
 if(j.test&&j.test.ok){ntPorts=j.test.details.map(d=>d.split(/\s+/)[0]);const cur=$("nt_port").value;ntShow(ntKind);$("nt_port").value=cur||ntPorts[0]||""}
 ntRender(j)}
function ntRender(j){const o=$("nt_out");o.style.display="block";
 if(!j.test){o.style.background="#f8d7da";o.textContent=j.msg||"测试失败";return}
 const t=j.test;o.style.background=t.ok?"#d4edda":"#f8d7da";
 o.innerHTML='<b>'+(t.ok?"✓ ":"✗ ")+esc(t.summary)+'</b>'+(t.details.length?'<pre style="white-space:pre-wrap;margin:8px 0 0;font-family:Consolas,monospace">'+esc(t.details.join("\n"))+'</pre>':'');
 $("nt_time").textContent="耗时 "+t.elapsed_ms+" ms";}
async function ntRun(){const b=$("nt_btn");b.disabled=true;b.textContent="测试中…";$("nt_time").textContent="";
 try{ntRender(await ntCall(ntKind,ntParams()))}catch(e){ntRender({msg:"请求失败："+e})}finally{b.disabled=false;b.textContent="开始测试"}}
const ST_OK=["ping","tcp","dns","opcua","modbus_tcp","modbus_rtu","serial"];
async function stStart(){if(!ST_OK.includes(ntKind))return show(false,"“"+NT[ntKind].t+"”不支持长时间测试，请先选 Ping / TCP / DNS / OPC UA / Modbus / 串口收发。");
 const p=ntParams();const j=await apiPost("/api/stab_start",{kind:ntKind,params:p,interval:$("st_iv").value,minutes:$("st_min").value});show(j.ok,j.msg);stPoll()}
function stNum(v,u){return v==null?"—":v+(u||"")}
function stChart(s){const B=s.buckets;if(!B.length)return"";const W=860,H=150,P=30,n=Math.max(1,Math.ceil(s.duration/s.bucket_sec));
 const mx=Math.max(1,...B.map(b=>b.max||0));let bars="",line="",pts=[];
 B.forEach(b=>{const x=P+(b.i/n)*(W-P-5),bw=Math.max(1.5,(W-P-5)/n-1);
  if(b.fail){const h=Math.max(3,(b.fail/b.n)*(H-20));bars+='<rect x="'+x+'" y="'+(H-h)+'" width="'+bw+'" height="'+h+'" fill="#dc3545" opacity=".75"><title>失败 '+b.fail+'/'+b.n+'</title></rect>'}
  if(b.avg!=null)pts.push((x+bw/2).toFixed(1)+","+(H-(b.avg/mx)*(H-20)).toFixed(1))});
 line=pts.length?'<polyline points="'+pts.join(" ")+'" fill="none" stroke="#1f4e79" stroke-width="1.6"/>':"";
 return '<svg viewBox="0 0 '+W+' '+(H+18)+'" style="width:100%;background:#fafbfc;border:1px solid #eee;border-radius:4px">'+
  '<text x="2" y="12" font-size="10" fill="#666">'+mx.toFixed(0)+'ms</text><text x="2" y="'+H+'" font-size="10" fill="#666">0</text>'+
  '<line x1="'+P+'" y1="'+H+'" x2="'+W+'" y2="'+H+'" stroke="#ccc"/>'+bars+line+
  '<text x="'+P+'" y="'+(H+14)+'" font-size="10" fill="#666">0</text><text x="'+(W-60)+'" y="'+(H+14)+'" font-size="10" fill="#666">'+s.total_text+'</text>'+
  '<text x="'+(W/2-90)+'" y="'+(H+14)+'" font-size="10" fill="#666">蓝线 = 平均延迟，红柱 = 失败比例（每段 '+s.bucket_sec+' 秒）</text></svg>'}
function stRender(s){const v=$("st_view");if(!s){v.innerHTML="";return}
 if(s.multi)return mtRender(s);
 const run=s.state==="running",bad=s.fail>0;
 const stt={running:"运行中",finished:"已完成",stopped:"已手动停止",error:"出错："+esc(s.error)}[s.state]||s.state;
 let h='<div style="padding:10px 12px;border-radius:6px;background:'+(run?"#e8f0f8":(bad?"#fff3cd":"#d4edda"))+'">'+
  '<b>'+esc(s.kind_name)+' '+esc(s.target)+'</b> · '+stt+' · '+s.elapsed_text+' / '+s.total_text+' · 每 '+s.interval+' 秒'+
  '<div style="height:6px;background:#dde;border-radius:3px;margin:6px 0"><div style="height:6px;width:'+s.progress+'%;background:#1f4e79;border-radius:3px"></div></div>'+
  '<div style="font-size:15px;margin:4px 0"><b>'+esc(s.verdict)+'</b></div>'+
  '<table style="width:100%;font-size:13px;border-collapse:collapse"><tr>'+
  [["测试次数",s.n],["成功",s.ok],["失败",s.fail],["失败率",stNum(s.loss_pct,"%")],["最长连续失败",s.max_streak+(s.max_streak_at?"（"+esc(s.max_streak_at)+" 起）":"")]].map(x=>'<td><small style="color:#666">'+x[0]+'</small><br><b>'+x[1]+'</b></td>').join("")+'</tr><tr>'+
  [["最小",stNum(s.min," ms")],["平均",stNum(s.avg," ms")],["P95",stNum(s.p95," ms")],["P99",stNum(s.p99," ms")],["最大",stNum(s.max," ms")],["抖动",stNum(s.jitter," ms")]].map(x=>'<td><small style="color:#666">'+x[0]+'</small><br><b>'+x[1]+'</b></td>').join("")+'</tr></table></div>';
 h+='<div style="margin-top:8px">'+stChart(s)+'</div>';
 if(s.errors.length)h+='<div style="margin-top:8px"><b>错误分布</b><ul style="font-size:13px">'+s.errors.map(e=>'<li>'+e.count+' 次：'+esc(e.text)+'</li>').join("")+'</ul></div>';
 if(s.hints.length)h+='<div style="margin-top:6px;font-size:13px;color:#856404"><b>排查建议</b><ul>'+s.hints.map(x=>'<li>'+esc(x)+'</li>').join("")+'</ul></div>';
 if(s.csv)h+='<div style="margin-top:6px;font-size:13px"><a href="/stability/'+encodeURIComponent(s.csv)+'">下载明细 CSV</a> <small style="color:#888">（logs/stability/'+esc(s.csv)+'，'+(run?"每 20 次写盘一次":"已保存")+'）</small></div>';
 v.innerHTML=h}
let mtList=JSON.parse(LS.get("mt_list")||"[]");
function mtSave(){LS.set("mt_list",JSON.stringify(mtList))}
function mtDesc(t){const p=t.params;if(t.kind==="modbus_rtu"||t.kind==="serial")return (p.port||"")+" "+(p.baud||9600)+","+(p.bytesize||8)+","+(p.parity||"N")+","+(p.stopbits||1)+(t.kind==="modbus_rtu"?" 站号"+(p.unit||1):"");
 if(t.kind==="opcua")return p.endpoint||"";return (p.host||"")+(p.port?":"+p.port:"")}
function mtShow(){$("mt_list").innerHTML=mtList.length?mtList.map((t,i)=>'<li><span><b>'+esc(t.label)+'</b> · '+esc(NT[t.kind].t)+' · '+esc(mtDesc(t))+'</span><button class="sec" data-i="'+i+'">移除</button></li>').join(""):"<li>（列表为空）</li>";
 document.querySelectorAll("#mt_list button").forEach(b=>b.onclick=()=>{mtList.splice(+b.dataset.i,1);mtSave();mtShow()})}
function mtAdd(){if(!ST_OK.includes(ntKind))return show(false,"“"+NT[ntKind].t+"”不支持长时间测试。");if(mtList.length>=16)return show(false,"最多 16 个目标。");
 const p=ntParams();delete p.password;mtList.push({kind:ntKind,params:p,label:$("mt_label").value.trim()||("目标"+(mtList.length+1))});$("mt_label").value="";mtSave();mtShow();show(true,"已加入对比列表，共 "+mtList.length+" 个。")}
async function mtStart(){if(mtList.length<2)return show(false,"多目标对比至少需要 2 个目标（只有 1 个请直接点“开始稳定性测试”）。");
 const j=await apiPost("/api/stab_start",{targets:mtList,interval:$("st_iv").value,minutes:$("st_min").value});show(j.ok,j.msg);stPoll()}
function mtStrip(r,n){const W=300,H=14;return '<svg viewBox="0 0 '+W+' '+H+'" style="width:300px;height:14px;background:#e9f5ec;border-radius:2px">'+
 r.strip.map(([i,f])=>'<rect x="'+(i/n*W).toFixed(1)+'" y="0" width="'+Math.max(1.5,W/n).toFixed(1)+'" height="'+H+'" fill="#dc3545" opacity="'+(0.35+0.65*f).toFixed(2)+'"/>').join("")+'</svg>'}
function mtRender(s){const run=s.state==="running";const stt={running:"运行中",finished:"已完成",stopped:"已手动停止"}[s.state]||s.state;
 let h='<div style="padding:10px 12px;border-radius:6px;background:'+(run?"#e8f0f8":(s.rows.some(r=>r.fail)?"#fff3cd":"#d4edda"))+'">'+
 '<b>多目标稳定性测试 · '+s.count+' 个目标</b> · '+stt+' · '+s.elapsed_text+' / '+s.total_text+' · 每 '+s.interval+' 秒'+
 '<div style="height:6px;background:#dde;border-radius:3px;margin:6px 0"><div style="height:6px;width:'+s.progress+'%;background:#1f4e79;border-radius:3px"></div></div>'+
 '<div style="font-size:15px"><b>'+esc(s.verdict)+'</b></div>'+(s.notes.length?'<ul style="font-size:13px;color:#856404;margin:6px 0 0">'+s.notes.map(x=>'<li>'+esc(x)+'</li>').join("")+'</ul>':'')+'</div>';
 h+='<table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:8px"><tr style="text-align:left"><th>名称</th><th>目标</th><th>次数</th><th>失败率</th><th>最长连续失败</th><th>平均 / P95 / 最大 (ms)</th><th>失败时间分布（红 = 失败）</th><th></th></tr>'+
 s.rows.map(r=>'<tr style="border-top:1px solid #eee'+(r.label===s.worst?';background:#fff8e1':'')+'"><td><b>'+esc(r.label)+'</b></td><td>'+esc(r.kind_name)+'<br><small>'+esc(r.target)+'</small></td><td>'+r.n+'</td>'+
  '<td>'+(r.fail?'<b style="color:#b23b3b">'+stNum(r.loss_pct,"%")+'</b>':stNum(r.loss_pct,"%"))+'</td><td>'+r.max_streak+(r.max_streak_at?'<br><small>'+esc(r.max_streak_at)+' 起</small>':'')+'</td>'+
  '<td>'+stNum(r.avg)+' / '+stNum(r.p95)+' / '+stNum(r.max)+'</td><td>'+mtStrip(r,s.n_buckets)+(r.errors.length?'<br><small>'+esc(r.errors[0].count+" 次："+r.errors[0].text)+'</small>':'')+'</td>'+
  '<td>'+(r.csv?'<a href="/stability/'+encodeURIComponent(r.csv)+'">明细</a>':'')+'</td></tr>').join("")+'</table>';
 if(s.summary_csv)h+='<div style="margin-top:6px;font-size:13px"><a href="/stability/'+encodeURIComponent(s.summary_csv)+'">下载对比汇总 CSV</a></div>';
 $("st_view").innerHTML=h}
let stTimer=null;
async function stPoll(){try{const j=await (await fetch("/api/stability")).json();stRender(j.stability);
 clearTimeout(stTimer);stTimer=setTimeout(stPoll,j.stability&&j.stability.state==="running"?2000:15000)}catch(e){stTimer=setTimeout(stPoll,5000)}}
const PR_OK=["ping","tcp","dns","opcua","modbus_tcp","modbus_rtu","serial"];
let prList=[];
async function prAdd(){if(!PR_OK.includes(ntKind))return show(false,"“"+NT[ntKind].t+"”不能做设备检测，请选 Ping / TCP / DNS / OPC UA / Modbus / 串口收发。");
 const label=$("pr_label").value.trim();if(!label)return show(false,"请填写设备检测的名称。");
 const id=$("pr_edit").value,old=prList.find(x=>x.id===id);
 const ok=await post("probe_save",{probe:{id:id||"",label:label,kind:ntKind,params:ntParams(),interval:$("pr_iv").value,
  fail_threshold:$("pr_thr").value,level:$("pr_lv").value,enabled:old?old.enabled:true}});
 if(ok){$("pr_label").value="";$("pr_edit").value="";prPoll()}}
function prEdit(id){const p=prList.find(x=>x.id===id);if(!p)return;ntShow(p.kind);
 Object.entries(p.params||{}).forEach(([k,v])=>{const el=$("nt_"+k);if(!el)return;if(el.type==="checkbox")el.checked=!!v;else el.value=v});
 $("pr_label").value=p.label;$("pr_iv").value=p.interval;$("pr_thr").value=p.fail_threshold;$("pr_lv").value=p.level;$("pr_edit").value=p.id;
 $("pr_add").scrollIntoView({behavior:"smooth",block:"center"});show(true,"已把「"+p.label+"」的参数填到连接测试里，改完点“加为设备检测”保存"+(p.kind==="opcua"?"（密码留空 = 保留原密码）":"")+"。")}
async function prRun(id){const j=await apiPost("/api/probe_run",{id:id});if(j.test){ntRender(j);$("nt_out").scrollIntoView({behavior:"smooth",block:"center"})}else show(j.ok,j.msg);prPoll()}
function prSpark(h){if(!h.length)return"";return '<svg viewBox="0 0 '+(h.length*4)+' 10" style="width:'+Math.min(180,h.length*4)+'px;height:10px">'+h.map((v,i)=>'<rect x="'+(i*4)+'" y="0" width="3" height="10" fill="'+(v?"#28a745":"#dc3545")+'"/>').join("")+'</svg>'}
function prRender(j){prList=(j.probes||[]).map(x=>x);const L=prList;
 $("pr_sum").textContent=L.length?(j.running?"":"（服务未运行，只能查看和立即检测）")+" 共 "+L.length+" 个，告警中 "+L.filter(x=>x.alerting).length+" 个":"";
 if(!L.length){$("pr_table").innerHTML='<tr><td style="color:#777">还没有设备检测。</td></tr>';return}
 $("pr_table").innerHTML='<tr style="text-align:left"><th>名称</th><th>检测</th><th>间隔 / 告警条件</th><th>状态</th><th>最近结果</th><th>可用率</th><th></th></tr>'+
 L.map(p=>{const st=!p.enabled?'<span class="lv" style="background:#eee;color:#666">停用</span>':(p.alerting?'<span class="lv lv-'+p.level+'">'+p.level+'</span>':(p.last?(p.last.ok?'<span class="lv lv-正常">正常</span>':'<span class="lv lv-注意">失败 '+p.fails+'/'+p.fail_threshold+'</span>'):'<span class="lv" style="background:#eee;color:#666">待检测</span>'));
  return '<tr style="border-top:1px solid #eee"><td><b>'+esc(p.label)+'</b></td><td>'+esc(p.kind_name)+'<br><small>'+esc(p.target)+'</small></td>'+
  '<td>每 '+p.interval+' 秒<br><small>连续 '+p.fail_threshold+' 次失败 → '+esc(p.level)+'</small></td><td>'+st+'</td>'+
  '<td>'+(p.last?'<small>'+esc(p.last.time)+'</small><br>'+esc(p.last.summary).slice(0,80)+'<br>'+prSpark(p.history):'—')+'</td>'+
  '<td>'+(p.uptime_pct==null?"—":p.uptime_pct+"%<br><small>"+p.checks+" 次</small>")+'</td>'+
  '<td style="white-space:nowrap" data-id="'+esc(p.id)+'"><button class="sec" data-a="run">立即检测</button> <button class="sec" data-a="edit">编辑</button> '+
  '<button class="sec" data-a="toggle">'+(p.enabled?"停用":"启用")+'</button> <button class="sec" data-a="del">删除</button></td></tr>'}).join("");
 document.querySelectorAll("#pr_table [data-a]").forEach(b=>b.onclick=()=>{const id=b.parentNode.dataset.id,a=b.dataset.a,p=prList.find(x=>x.id===id);
  if(a==="run")prRun(id);else if(a==="edit")prEdit(id);else if(a==="toggle")post("probe_toggle",{id:id}).then(prPoll);
  else if(a==="del"&&confirm("删除设备检测「"+(p?p.label:"")+"」？"))post("probe_remove",{id:id}).then(prPoll)})}
let prTimer=null;
async function prPoll(){try{prRender(await (await fetch("/api/probes")).json())}catch(e){}clearTimeout(prTimer);prTimer=setTimeout(prPoll,10000)}
async function dcRun(){const b=$("dc_btn");b.disabled=true;b.textContent="检测中（约 10～30 秒）…";
 try{const j=await apiPost("/api/drivercheck",{});
  if(!j.test||!j.test.drivercheck){show(false,j.msg||"检测失败");return}
  dcShow(j.test.drivercheck,"刚刚");envPoll()}
 catch(e){show(false,"请求失败："+e)}finally{b.disabled=false;b.textContent="开始检测"}}
function dcShow(d,when){const ord={"问题":0,"注意":1,"正常":2};
  $("dc_time").textContent=d.summary+" · 耗时 "+d.elapsed_ms+" ms"+(when?" · "+when:"");
  $("dc_out").innerHTML='<table style="width:100%;border-collapse:collapse;font-size:13px">'+d.items.slice().sort((a,b)=>ord[a.level]-ord[b.level]).map(it=>
   '<tr style="border-top:1px solid #eee"><td style="width:62px"><span class="lv lv-'+(it.level==="问题"?"风险":it.level)+'">'+it.level+'</span></td><td style="width:200px"><b>'+esc(it.name)+'</b></td><td>'+esc(it.text)+
   (it.fix?'<br><small style="color:#856404">处理：'+esc(it.fix)+'</small>':'')+'</td></tr>').join("")+'</table>'}
let envShown="";
async function envPoll(){try{const e=(await (await fetch("/api/env")).json()).env;
 if(!e){$("env_last").textContent="服务未运行：自动检测只在服务运行时进行，可以点“开始检测”手动检查。";return}
 if(e.running&&!e.result){$("env_last").textContent="正在进行启动检测…";setTimeout(envPoll,3000);return}
 if(!e.result){$("env_last").textContent="启动检测还没开始（服务启动约 8 秒后进行，或已在设置里关闭）。";setTimeout(envPoll,5000);return}
 $("env_last").innerHTML="上次自动检测："+esc(e.time)+" · <b>"+esc(e.result.summary)+"</b>";
 if(envShown!==e.time&&!$("dc_out").innerHTML){dcShow(e.result,e.time)}envShown=e.time}catch(x){}}
function renderChannels(a){
 const L=(a&&a.channel_list)||[];
 if(!L.length){$("ch_table").innerHTML="<tr><td>"+(a&&a.offline?"服务未运行，看不到通道。启动服务后这里会列出所有通道。":"还没有扫描到通道。")+"</td></tr>";return}
 let h='<tr><th>通道</th><th>最后周期</th><th>状态</th><th>屏蔽告警</th><th>无新周期告警（分钟）</th><th>到期</th><th>备注</th><th></th></tr>';
 L.forEach((c,i)=>{
  const st=c.present===null?"—":(!c.present?'<span class="lv lv-警告">丢失</span>':(c.alerting?'<span class="lv lv-警告">告警</span>':'<span class="lv lv-正常">正常</span>'))+(c.idle_min!=null?' <small>'+c.idle_min+' 分钟前更新</small>':'');
  h+='<tr data-k="'+esc(c.key)+'"><td>'+esc(c.label)+'</td><td>'+esc(c.cycle_time||"")+'</td><td>'+st+'</td>'+
   '<td><input type="checkbox" class="c_mute"'+(c.muted?" checked":"")+'></td>'+
   '<td><input type="number" class="c_sm" min="0" style="width:80px" placeholder="默认" value="'+(c.stale_minutes??"")+'"></td>'+
   '<td><input type="datetime-local" class="c_until" value="'+esc((c.until||"").replace(" ","T"))+'"></td>'+
   '<td><input type="text" class="c_note" style="width:110px" value="'+esc(c.note||"")+'"></td>'+
   '<td><button class="sec c_save">保存</button></td></tr>';});
 $("ch_table").innerHTML=h;
 document.querySelectorAll("#ch_table .c_save").forEach(b=>b.onclick=()=>{const tr=b.closest("tr");
  post("channel_rule",{rule:{channel:tr.dataset.k,mute:tr.querySelector(".c_mute").checked,
   stale_minutes:tr.querySelector(".c_sm").value,until:dt(tr.querySelector(".c_until").value),note:tr.querySelector(".c_note").value}})});}
function renderAlerts(a){
 renderQuiet(a);
 if(!a||a.offline)badge("status",0);
 if(!document.activeElement||!document.activeElement.closest||!document.activeElement.closest("#ch_table"))renderChannels(a);
 if(!a||a.offline){$("al_sum").textContent="（服务未运行：只打开了设置页面）";$("al_active").innerHTML="";$("al_hist").innerHTML="";return}
 $("al_sum").textContent="最近扫描 "+(a.last_scan||"尚未完成")+" · "+a.channels+" 个通道";
 $("al_active").innerHTML=a.active.length?a.active.map(x=>'<li><span class="lv lv-'+x.level+'">'+x.level+'</span>'+esc(x.text)+' <small style="color:#888">自 '+esc(x.since)+'</small></li>').join(""):'<li><span class="lv lv-正常">正常</span>没有告警</li>';
 $("al_hist").innerHTML=a.history.map(h=>'<li>'+esc(h.time)+' '+(h.event==="恢复"?"✓ 恢复":"⚠ "+h.level)+'：'+esc(h.text)+'</li>').join("")||"<li>（无）</li>";
 document.title=(a.active.length?"⚠("+a.active.length+") ":"")+"色谱提取 OPC UA 服务 · 设置";
 badge("status",a.active.length,a.worst);}
async function load(){const r=await fetch("/api/state");const st=await r.json();render(st);renderAlerts(st.alerts);}
async function pollAlerts(){try{const r=await fetch("/api/alerts");renderAlerts((await r.json()).alerts)}catch(e){}}
setInterval(pollAlerts,15000);
async function post(a,body){const j=await apiPost("/api/"+a,body);show(j.ok,j.msg);if(j.state){render(j.state);renderAlerts(j.state.alerts)}return j.ok}
function save(){const s={};
 F.str.forEach(k=>{if($(k).value!==String(orig[k]??""))s[k]=$(k).value});
 F.num.forEach(k=>{if($(k).value!==String(orig[k]??""))s[k]=$(k).value});
 F.bool.forEach(k=>{if($(k).checked!==!!orig[k])s[k]=$(k).checked});
 const wd=$("watch_dirs").value.split("\n").map(x=>x.trim()).filter(Boolean);
 if(JSON.stringify(wd)!==JSON.stringify(orig.watch_dirs||[]))s.watch_dirs=wd;
 if(!Object.keys(s).length)return show(true,"没有改动。");post("save",{settings:s});}
function pw(){const n=$("u_name").value.trim(),p=$("u_pw").value;
 if(!n){show(false,"请填写用户名。");return null} if(p!==$("u_pw2").value){show(false,"两次输入的密码不一致。");return null}
 return {name:n,password:p}}
async function addUser(){const b=pw();if(b&&await post("add_user",b)){$("u_pw").value=$("u_pw2").value=""}}
async function setSmtpPw(){const v=$("smtp_pw").value;if(!v)return show(false,"请输入密码或授权码。");
 if(await post("set_smtp_password",{password:v}))$("smtp_pw").value=""}
function clearSmtpPw(){if(confirm("确定清除 SMTP 密码吗？"))post("set_smtp_password",{password:""})}
function removeUser(u){if(confirm("确定删除账号 "+u+" 吗？"))post("remove_user",{name:u})}
async function harden(){const b=pw();if(!b)return;
 if(!confirm("一键加固会开启加密、关闭匿名访问。现有 DCS/OPC 客户端需要同步改成加密+账号登录，否则会连不上。继续吗？"))return;
 if(await post("harden",b)){$("u_pw").value=$("u_pw2").value=""}}
initDays();
applyLang();
tab(location.hash.slice(1)||LS.get("tab")||"status");
ntShow("ping");
mtShow();
load();
stPoll();
prPoll();
envPoll();
</script></body></html>"""
