# -*- coding: utf-8 -*-
"""打包与部署相关功能的测试：自检、数据目录设置、目录检查、控制台判断、部署脚本完整性。"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import chrom_core as cc          # noqa: E402
import config_tools as ct        # noqa: E402
import demo_data                 # noqa: E402
import server_app as sa          # noqa: E402


class SelfTest(unittest.TestCase):
    def test_selftest_passes(self):
        self.assertEqual(sa.run_selftest(), 0)

    def test_version_matches(self):
        self.assertEqual(sa.__version__, cc.__version__)


class SetWatchDirs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, "config.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"poll_seconds": 30, "users": {"u": "pbkdf2_sha256$1$aa$bb"}}, f)
        self.data = os.path.join(self.tmp, "数据 目录")
        os.makedirs(self.data)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_sets_absolute_path_and_keeps_other_keys(self):
        got = ct.set_watch_dirs(self.cfg, ['"%s"' % self.data])
        self.assertEqual(got, [os.path.abspath(self.data)])
        saved = ct.read_raw(self.cfg)
        self.assertEqual(saved["watch_dirs"], got)
        self.assertEqual(saved["poll_seconds"], 30)
        self.assertIn("u", saved["users"])

    def test_missing_dir_rejected_and_file_untouched(self):
        before = open(self.cfg, "rb").read()
        with self.assertRaises(ct.ConfigToolError):
            ct.set_watch_dirs(self.cfg, [os.path.join(self.tmp, "不存在")])
        self.assertEqual(open(self.cfg, "rb").read(), before)

    def test_empty_rejected(self):
        with self.assertRaises(ct.ConfigToolError):
            ct.set_watch_dirs(self.cfg, ["", "  "])


class WatchDirWarnings(unittest.TestCase):
    def test_cases(self):
        tmp = tempfile.mkdtemp()
        try:
            self.assertTrue(sa.watch_dir_warnings({"watch_dirs": []}))
            self.assertTrue(sa.watch_dir_warnings({"watch_dirs": [os.path.join(tmp, "nope")]}))
            self.assertTrue(sa.watch_dir_warnings({"watch_dirs": [tmp]}))      # 空目录
            demo_data.make_demo_db(os.path.join(tmp, "a_V3.db"))
            self.assertEqual(sa.watch_dir_warnings({"watch_dirs": [tmp]}), [])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class Console(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "非 Windows 才有此保证")
    def test_not_windows_never_own_console(self):
        self.assertFalse(sa._own_console())

    def test_pause_never_blocks_when_not_frozen(self):
        sa._pause_before_exit(3)       # 不是打包环境，必须直接返回而不是等输入


class Deployment(unittest.TestCase):
    """发布文件夹里必须有的脚本，build_exe.bat 与 CI 都要复制它们（之前漏过，导致装不上）。"""
    REQUIRED = ["安装.bat", "卸载.bat", "启动服务.bat", "启动图形界面.bat", "安装开机自启.bat",
                "install_autostart.ps1", "设置防火墙.bat", "安全管理.bat", "打开设置页面.bat", "后台启动.bat", "停止后台服务.bat", "安装为Windows服务.bat", "卸载Windows服务.bat", "使用说明_OPCUA.md"]

    def _read(self, name):
        with open(os.path.join(ROOT, name), "rb") as f:
            return f.read()

    def test_files_exist(self):
        for n in self.REQUIRED:
            self.assertTrue(os.path.exists(os.path.join(ROOT, n)), n)

    def test_build_script_and_ci_copy_every_file(self):
        build = self._read("build_exe.bat").decode("utf-8")
        ci = self._read(".github/workflows/build-exe.yml").decode("utf-8")
        for n in self.REQUIRED:
            self.assertIn(n, build, "build_exe.bat 没有复制 " + n)
            self.assertIn(n, ci, "CI 没有复制 " + n)

    def test_bat_files_use_crlf(self):
        for n in self.REQUIRED:
            if n.endswith(".bat"):
                data = self._read(n)
                self.assertNotIn(b"\n", data.replace(b"\r\n", b""), n + " 必须是 CRLF 换行")

    def test_ps1_has_bom(self):
        self.assertTrue(self._read("install_autostart.ps1").startswith(b"\xef\xbb\xbf"))

    def test_autostart_has_no_72h_limit(self):
        ps = self._read("install_autostart.ps1").decode("utf-8-sig")
        self.assertIn("ExecutionTimeLimit", ps)
        self.assertIn("RestartCount", ps)


class ExeBuild(unittest.TestCase):
    """打包时必须带上动态导入的模块，否则 EXE 里打不开设置页面。"""

    def test_hidden_imports(self):
        # workflow 文件无法经连接器推送（403），线上 workflow 可能落后于本地；
        # modbus_tools 在 server_app/desktop_ui 里是静态 import，PyInstaller
        # 静态分析能跟到，hidden-import 只是双保险，故 workflow 不强制查它，
        # build_exe.bat（可推送）仍全查。
        mods = ("web_settings", "demo_data", "config_tools", "alerts", "nettest", "stability", "probes", "drivercheck", "envwatch", "bgrun", "winservice", "modbus_tools")
        for f in ("build_exe.bat", ".github/workflows/build-exe.yml"):
            text = open(os.path.join(ROOT, f), encoding="utf-8").read()
            for mod in mods:
                if f.endswith(".yml") and mod == "modbus_tools":
                    continue
                self.assertIn("--hidden-import " + mod, text, f + " 缺少 " + mod)
            self.assertIn("selftest", text)
            self.assertIn("--icon assets\\app.ico", text, f + " 没有设置 EXE 图标")
            self.assertIn("SHA256", text)


class Installer(unittest.TestCase):
    """Inno Setup 脚本：引用的文件都存在、带 BOM、升级不覆盖配置、卸载清理任务和防火墙。"""

    def setUp(self):
        with open(os.path.join(ROOT, "installer", "ChromExtractOPC.iss"), "rb") as f:
            self.raw = f.read()
        self.text = self.raw.decode("utf-8-sig")

    def test_icon(self):
        self.assertIn("SetupIconFile=..\\assets\\app.ico", self.text)
        import struct
        with open(os.path.join(ROOT, "assets", "app.ico"), "rb") as f:
            data = f.read()
        reserved, typ, n = struct.unpack("<HHH", data[:6])
        self.assertEqual((reserved, typ), (0, 1), "不是有效的 .ico 文件")
        sizes = {data[6 + 16 * i] or 256 for i in range(n)}   # 宽度字节，0 表示 256
        for s in (16, 32, 48, 256):
            self.assertIn(s, sizes)

    def test_bom(self):
        self.assertTrue(self.raw.startswith(b"\xef\xbb\xbf"), "iss 必须是 UTF-8 带 BOM")

    def test_sources_exist(self):
        import re
        for src in re.findall(r'Source: "\.\.\\([^"]+)"', self.text):
            self.assertTrue(os.path.exists(os.path.join(ROOT, src)), src)

    def test_config_preserved(self):
        line = [l for l in self.text.splitlines() if 'config.json' in l and l.startswith("Source")][0]
        self.assertIn("onlyifdoesntexist", line)
        self.assertIn("uninsneveruninstall", line)

    def test_uninstall_cleans_up(self):
        self.assertIn("-Remove", self.text)
        self.assertIn("firewall delete rule", self.text)
        self.assertIn("--no-pause", self.text)

    def test_build_installer_bat_crlf(self):
        data = open(os.path.join(ROOT, "build_installer.bat"), "rb").read()
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))


class CodeSigning(unittest.TestCase):
    """签名必须是可选的（没证书也能打包），配置了就覆盖 EXE、安装程序和卸载程序。"""

    def _t(self, n):
        return open(os.path.join(ROOT, n), encoding="utf-8-sig").read()

    def test_sign_script(self):
        ps = self._t("sign.ps1")
        for k in ("SIGN_THUMBPRINT", "SIGN_PFX", "SIGN_PFX_BASE64", "/fd sha256", "/tr", "verify"):
            self.assertIn(k, ps)
        self.assertTrue(open(os.path.join(ROOT, "sign.ps1"), "rb").read().startswith(b"\xef\xbb\xbf"))

    def test_wired_everywhere(self):
        self.assertIn("sign.ps1 -Check", self._t("build_exe.bat"))
        self.assertIn("/DSIGN", self._t("build_installer.bat"))
        self.assertIn("SignedUninstaller=yes", self._t("installer/ChromExtractOPC.iss"))
        self.assertIn("secrets.SIGN_PFX_BASE64", self._t(".github/workflows/build-exe.yml"))

    def test_no_private_keys_committed(self):
        self.assertIn("*.pfx", self._t(".gitignore"))
        for dp, _, fn in os.walk(ROOT):
            if "build_env" in dp:
                continue
            for f in fn:
                self.assertFalse(f.lower().endswith((".pfx", ".p12")), os.path.join(dp, f))


if __name__ == "__main__":
    unittest.main()
