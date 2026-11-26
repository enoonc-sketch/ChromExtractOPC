# -*- coding: utf-8 -*-
"""v3.1 中英文界面（i18n.py）测试：

  - 扫描 desktop_ui.py / desktop_features.py 里所有 tr("…") 用到的中文原文，以及通过 tr() 显示的
    模块常量（连接测试表单、下拉选项、表头、常用设置字段、接线提示、峰表列名、角色名等），确认都有英文翻译；
  - 译文与原文的 % 占位符一致（否则英文界面格式化会出错）；英文译文不重复（切回中文时能唯一还原）；
  - 网页设置页的静态文字都有译文，页面注入了字典；
  - tr 缺失回退中文、英文键、auto 按系统语言、配置校验、切换监听、Msg / tr_msg、retranslate_text（整句 / 后缀 / 模板）。
"""
import ast
import json
import os
import re
import sys
import unittest
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import i18n                    # noqa: E402
import server_app as sa        # noqa: E402
import config_tools as ct      # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]")


def _read(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return f.read()


def tr_literals(src):
    """源码里 tr("…") / tr(a if c else "…") 的字符串字面量。"""
    out = set()
    for n in ast.walk(ast.parse(src)):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "tr" and n.args:
            a = n.args[0]
            cands = [a.body, a.orelse] if isinstance(a, ast.IfExp) else [a]
            for c in cands:
                if isinstance(c, ast.Constant) and isinstance(c.value, str):
                    out.add(c.value)
    return out


class Coverage(unittest.TestCase):
    def test_all_tr_keys_in_desktop_ui_have_english(self):
        keys = {k for k in tr_literals(_read("desktop_ui.py")) | tr_literals(_read("desktop_features.py"))
                if CJK.search(k)}
        self.assertGreater(len(keys), 500)
        self.assertEqual(i18n.missing_keys(keys), [])

    def test_constants_shown_through_tr_have_english(self):
        import commscan
        import desktop_features as df
        import desktop_ui as du
        import audit
        import peaks
        import modbus_server
        keys = set()
        for kind, spec in du.NETTEST_SPECS.items():
            keys.add(spec["t"])
            if spec.get("tip"):
                keys.add(spec["tip"])
            keys |= {f[1] for f in du.nettest_fields(kind)}
        for opts in du.SELECT_OPTIONS.values():
            keys |= {t for _v, t in opts}
        keys |= set(du.MODBUS_FUNC_LABELS) | set(du.MODBUS_AREA_LABELS) | {du.RESTART_TAG}
        keys |= set(du.DesktopWindow._ST_MINUTES)
        for c in (du.LANG_OPTIONS, du.HIST_SERIES, du.PEAK_FIELDS, du.REPORT_MODES, du.ROLE_OPTIONS, df.SERIES,
                  df.NOTIFY_LEVELS):
            keys |= {t for _a, t in c}
        keys |= {t for _c, t, _w in df.CHANNEL_COLUMNS}
        for f in df.ALL_QUICK_FIELDS:
            keys.add(f[1])
            if f[4]:
                keys.add(f[4])
            if f[2] == "select":
                keys |= {t for _a, t in f[3]}
        for v in commscan.INTERFACES.values():
            keys |= {v["duplex"], v["wiring"]}
        keys |= set(peaks.CSV_COLUMNS) | set(audit.ROLE_NAMES.values()) | set(modbus_server.FIELD_DESC.values())
        keys |= {"警告", "严重", "正常", "有告警", "无周期", "覆盖", "新增", "删除"}
        keys = {k for k in keys if CJK.search(k)}
        self.assertEqual(i18n.missing_keys(keys), [])

    def test_placeholders_match(self):
        bad = [(k, v) for k, v in i18n.EN.items() if i18n.placeholders(k) != i18n.placeholders(v)]
        self.assertEqual(bad, [])

    def test_english_values_unique(self):
        seen = {}
        dups = []
        for k, v in i18n.EN.items():
            if v in seen:
                dups.append((v, seen[v], k))
            seen[v] = k
        self.assertEqual(dups, [])

    def test_english_has_no_chinese_except_names(self):
        allowed = {"中文", "色谱"}         # 语言名本身；钉钉关键词必须保留中文
        bad = [(k, v) for k, v in i18n.EN.items()
               if CJK.search(re.sub("|".join(allowed), "", v)) and "自动备份" not in v]
        self.assertEqual(bad, [])

    def test_web_page_static_text_translated(self):
        import web_settings

        class P(HTMLParser):
            def __init__(self):
                super().__init__()
                self.t, self.skip = set(), False

            def handle_starttag(self, tag, attrs):
                self.skip = tag in ("script", "style")
                for a, v in attrs:
                    if a in ("placeholder", "title") and v and CJK.search(v):
                        self.t.add(v.strip())

            def handle_endtag(self, tag):
                self.skip = False

            def handle_data(self, data):
                if not self.skip and CJK.search(data):
                    self.t.add(re.sub(r"\s+", " ", data).strip())
        page = web_settings.PAGE
        p = P()
        p.feed(page[:page.find("<script>")])
        missing = sorted(k for k in p.t if k not in i18n.EN)
        self.assertEqual(missing, [])
        self.assertIn("__I18N__", page)
        self.assertIn('id="langsel"', page)


class Behaviour(unittest.TestCase):
    def tearDown(self):
        i18n.set_language("zh")

    def test_tr_and_fallback(self):
        i18n.set_language("zh")
        self.assertEqual(i18n.tr("▶ 启动服务"), "▶ 启动服务")
        i18n.set_language("en")
        self.assertEqual(i18n.tr("▶ 启动服务"), "▶ Start service")
        self.assertEqual(i18n.tr("一句没有翻译的话"), "一句没有翻译的话")      # 缺失回退中文
        self.assertEqual(i18n.tr("app.title"), "ChromExtract OPC UA Server")
        self.assertEqual(i18n.tr(123), 123)
        self.assertEqual(i18n.tr("%d 分钟前") % 5, "5 min ago")
        self.assertEqual(i18n.tr("▶ 启动服务", "zh"), "▶ 启动服务")
        self.assertEqual(i18n.untr("▶ Start service"), "▶ 启动服务")
        self.assertEqual(i18n.untr("not a translation"), "not a translation")

    def test_auto_language(self):
        self.assertEqual(i18n.resolve_language("auto", lambda: 0x0804), "zh")     # 简体中文
        self.assertEqual(i18n.resolve_language("auto", lambda: 0x0404), "zh")     # 繁体中文
        self.assertEqual(i18n.resolve_language("auto", lambda: 0x0409), "en")
        self.assertEqual(i18n.resolve_language("auto", lambda: "zh_CN"), "zh")
        self.assertEqual(i18n.resolve_language("auto", lambda: "de_DE"), "en")
        self.assertEqual(i18n.resolve_language("EN"), "en")
        self.assertEqual(i18n.resolve_language("xx"), "zh")
        self.assertEqual(i18n.system_ui_language(lambda: 1 / 0), "zh")       # 取不到时按中文

    def test_config_key(self):
        self.assertEqual(sa.DEFAULT_CONFIG["desktop_language"], "auto")
        self.assertIs(ct.EDITABLE["desktop_language"], str)
        for good in ("zh", "EN", "auto"):
            cfg = sa.load_config(None)
            cfg["desktop_language"] = good
            sa.normalize_config(cfg, ".")
            self.assertEqual(cfg["desktop_language"], good.lower())
        cfg = sa.load_config(None)
        cfg["desktop_language"] = "fr"
        with self.assertRaises(sa.ConfigError):
            sa.normalize_config(cfg, ".")
        with open(os.path.join(ROOT, "config.json"), encoding="utf-8-sig") as f:
            self.assertIn("desktop_language", json.load(f))

    def test_listener(self):
        seen = []
        fn = lambda o, n: seen.append((o, n))  # noqa: E731
        i18n.set_language("zh")
        i18n.add_listener(fn)
        try:
            i18n.set_language("en")
            i18n.set_language("en")
            i18n.set_language("zh")
        finally:
            i18n.remove_listener(fn)
        self.assertEqual(seen, [("zh", "en"), ("en", "zh")])

    def test_msg(self):
        m = i18n.M("找不到数据目录：%s", "D:\\data")
        self.assertEqual(str(m), "找不到数据目录：D:\\data")          # 日志 / 异常里照常是中文
        i18n.set_language("en")
        self.assertEqual(i18n.tr_msg(m), "Data folder not found: D:\\data")
        e = ValueError(i18n.M("%s（已回滚，配置保持还原前的状态）", ValueError(i18n.M("备份包缺少文件：%s", "x"))))
        self.assertEqual(i18n.tr_msg(e), "File missing from the backup: x (rolled back; configuration unchanged)")
        self.assertEqual(i18n.tr_msg("没有改动。"), "No changes.")
        self.assertEqual(i18n.tr_msg(i18n.M("坏模板 %d", "x")), "坏模板 %d")

    def test_retranslate(self):
        rt = i18n.retranslate_text
        self.assertEqual(rt("运行状态", "zh", "en"), "Service status")
        self.assertEqual(rt("Service status", "en", "zh"), "运行状态")
        self.assertIsNone(rt("COM3", "zh", "en"))                    # 用户输入的值不动
        self.assertIsNone(rt("运行状态", "zh", "zh"))
        self.assertEqual(rt("监听地址（需重启）", "zh", "en"), "Listen address (restart needed)")
        self.assertEqual(rt("Listen address (restart needed)", "en", "zh"), "监听地址（需重启）")
        self.assertEqual(rt("快捷方式位置：C:\\x.lnk", "zh", "en"), "Shortcut location: C:\\x.lnk")
        self.assertEqual(rt("Shortcut location: C:\\x.lnk", "en", "zh"), "快捷方式位置：C:\\x.lnk")
        self.assertEqual(rt("端口写法：502,4840,8000-8010；一次最多 1024 个。只做 TCP 连接，连上立即断开。", "zh", "en"),
                         "Port syntax: 502,4840,8000-8010; at most 1024 per scan. TCP connect only, closed immediately.")


if __name__ == "__main__":
    unittest.main()
