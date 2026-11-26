# -*- coding: utf-8 -*-
"""打包与部署相关功能的测试：自检、数据目录设置、目录检查、控制台判断、部署脚本完整性、
不再有后台运行（v2.9 桌面软件）。"""
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
    REQUIRED = ["安装.bat", "卸载.bat", "启动图形界面.bat", "打开设置页面.bat", "设置防火墙.bat",
                "安全管理.bat", "使用说明_OPCUA.md"]
    # v2.9 起为桌面软件：这些后台运行相关文件已删除，不能再被打包或引用
    REMOVED = ["bgrun.py", "winservice.py", "install_autostart.ps1", "后台启动.bat", "停止后台服务.bat",
               "安装为Windows服务.bat", "卸载Windows服务.bat", "安装开机自启.bat", "启动服务.bat"]

    def _read(self, name):
        with open(os.path.join(ROOT, name), "rb") as f:
            return f.read()

    def test_files_exist(self):
        for n in self.REQUIRED:
            self.assertTrue(os.path.exists(os.path.join(ROOT, n)), n)

    def test_background_files_removed(self):
        for n in self.REMOVED:
            self.assertFalse(os.path.exists(os.path.join(ROOT, n)), "应已删除：" + n)

    def test_build_script_and_ci_copy_every_file(self):
        build = self._read("build_exe.bat").decode("utf-8")
        ci = self._read(".github/workflows/build-exe.yml").decode("utf-8")
        for n in self.REQUIRED:
            self.assertIn(n, build, "build_exe.bat 没有复制 " + n)
            self.assertIn(n, ci, "CI 没有复制 " + n)

    def test_nothing_references_removed_files(self):
        names = ["build_exe.bat", "build_installer.bat", ".github/workflows/build-exe.yml",
                 "installer/ChromExtractOPC.iss"] + [n for n in self.REQUIRED if n.endswith(".bat")]
        for n in names:
            text = self._read(n).decode("utf-8-sig")
            for gone in self.REMOVED:
                self.assertNotIn(gone, text, "%s 还引用了已删除的 %s" % (n, gone))

    def test_no_background_operation_anywhere(self):
        """不允许再创建后台运行项：后台参数、Windows 服务注册、计划任务自启。
        （安装/卸载脚本里允许出现“删除旧版计划任务/服务”的清理命令。）"""
        forbidden = ["--background", "--install-service", "--start-service", "--service-status",
                     "Register-ScheduledTask", "schtasks /Create", "sc create", "sc.exe create",
                     "/TASKS=autostart", "autostart\\service", "--hidden-import bgrun", "--hidden-import winservice"]
        names = ["build_exe.bat", "build_installer.bat", ".github/workflows/build-exe.yml",
                 "installer/ChromExtractOPC.iss"] + [n for n in self.REQUIRED if n.endswith(".bat")]
        for n in names:
            text = self._read(n).decode("utf-8-sig")
            for f in forbidden:
                if n.endswith(".yml") and f == "--background":
                    continue                    # CI 专门验证 --background 会被拒绝
                self.assertNotIn(f, text, "%s 仍包含后台运行相关内容：%s" % (n, f))

    def test_bat_files_use_crlf(self):
        for n in self.REQUIRED + ["build_exe.bat", "build_installer.bat"]:
            if n.endswith(".bat"):
                data = self._read(n)
                self.assertNotIn(b"\n", data.replace(b"\r\n", b""), n + " 必须是 CRLF 换行")

    def test_scripts_use_right_exe(self):
        """需要控制台/返回码的脚本用命令行版，打开窗口的脚本用窗口版。"""
        self.assertIn("ChromExtractOPC-cli.exe --admin", self._read("安全管理.bat").decode("utf-8"))
        inst = self._read("安装.bat").decode("utf-8")
        self.assertIn("ChromExtractOPC-cli.exe\" --no-pause --selftest", inst)
        self.assertIn("ChromExtractOPC-cli.exe\" --no-pause --set-watch-dir", inst)
        self.assertIn('start "" ChromExtractOPC.exe', self._read("启动图形界面.bat").decode("utf-8"))
        self.assertIn("--settings", self._read("打开设置页面.bat").decode("utf-8"))


HIDDEN_IMPORTS = ("web_settings", "demo_data", "config_tools", "alerts", "nettest", "stability", "probes",
                  "drivercheck", "envwatch", "instance_lock", "modbus_tools", "desktop_ui", "desktop_features",
                  "commscan", "tkinter",
                  # v3.1
                  "i18n", "backup_tools", "history", "peaks", "modbus_server", "reports", "audit",
                  # v3.2
                  "ui_layout",
                  # v3.3
                  "theme")


class ExeBuild(unittest.TestCase):
    """打包时必须带上动态导入的模块；窗口版（无控制台）+ 命令行版各一个 EXE。"""

    def _t(self, f):
        return open(os.path.join(ROOT, f), encoding="utf-8").read()

    def test_hidden_imports(self):
        for f in ("build_exe.bat", ".github/workflows/build-exe.yml"):
            text = self._t(f)
            for mod in HIDDEN_IMPORTS:
                # 两个 EXE 都要带
                self.assertGreaterEqual(text.count("--hidden-import " + mod + " "), 2, f + " 缺少 " + mod)
            self.assertIn("selftest", text)
            self.assertIn("--icon assets\\app.ico", text, f + " 没有设置 EXE 图标")
            self.assertIn("SHA256", text)

    def test_windowed_and_cli_builds(self):
        for f in ("build_exe.bat", ".github/workflows/build-exe.yml"):
            text = self._t(f)
            self.assertIn("--windowed", text, f + "：主程序必须是窗口版（双击不弹黑色控制台）")
            self.assertIn("--name ChromExtractOPC ", text.replace("^", " ").replace("\n", " "))
            self.assertIn("--name ChromExtractOPC-cli", text)
            self.assertIn("ChromExtractOPC-cli.exe --no-pause --selftest", text, f + "：自检要用命令行版（有返回码）")

    def test_lazy_imports_are_listed(self):
        """server_app / desktop_ui 里函数内 import 的本地模块都要在 hidden-import 里。"""
        import ast
        local = {os.path.splitext(n)[0] for n in os.listdir(ROOT) if n.endswith(".py")}
        found = set()
        for name in ("server_app.py", "desktop_ui.py", "web_settings.py", "desktop_features.py", "commscan.py",
                     "backup_tools.py", "history.py", "peaks.py", "modbus_server.py", "reports.py", "audit.py",
                     "i18n.py", "ui_layout.py", "theme.py"):
            tree = ast.parse(self._t(name))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    found |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                    found.add(node.module.split(".")[0])
        needed = (found & local) - {"server_app", "chrom_core"}
        for mod in needed:
            self.assertIn(mod, HIDDEN_IMPORTS, "函数内导入的 %s 没有加入 --hidden-import" % mod)


class V31Modules(unittest.TestCase):
    """v3.1 新模块：逻辑与 tkinter 解耦、不引入新的第三方依赖、两个 EXE 都打包进去。"""
    NEW = ("i18n", "backup_tools", "history", "peaks", "modbus_server", "reports", "audit",
           "ui_layout",      # v3.2：导航结构也不依赖 tkinter
           "theme")          # v3.3：主题（调色板 / 样式表 / 着色规则）也不依赖 tkinter

    def test_new_modules_do_not_import_tkinter(self):
        import ast
        for mod in self.NEW:
            with open(os.path.join(ROOT, mod + ".py"), encoding="utf-8") as f:
                tree = ast.parse(f.read())
            names = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names |= {a.name.split(".")[0] for a in node.names}
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.add(node.module.split(".")[0])
            self.assertNotIn("tkinter", names, mod)
            self.assertFalse(names & {"numpy", "openpyxl", "xlsxwriter", "pandas", "scipy"}, mod)

    def test_no_new_dependencies(self):
        with open(os.path.join(ROOT, "requirements.txt"), encoding="utf-8") as f:
            req = f.read().lower()
        for pkg in ("openpyxl", "xlsxwriter", "numpy", "scipy", "pymodbus"):
            self.assertNotIn(pkg, req)
        self.assertIn("cryptography", req)          # 备份加密用的 AES-GCM 来自已有依赖

    def test_both_exes_bundle_new_modules(self):
        for f in ("build_exe.bat", ".github/workflows/build-exe.yml"):
            with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
                text = fh.read()
            import re
            for mod in self.NEW:
                n = len(re.findall(r"--hidden-import %s(?=[\s^])" % mod, text))
                self.assertEqual(n, 2, "%s：%s 应在两个 EXE 里各出现一次" % (f, mod))


class Installer(unittest.TestCase):
    """Inno Setup 脚本：引用的文件都存在、带 BOM、升级不覆盖配置、卸载清理、桌面软件不建后台项。"""

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

    def test_ships_both_exes(self):
        self.assertIn('#define MyCliExe "ChromExtractOPC-cli.exe"', self.text)
        self.assertIn('Source: "{#Dist}\\{#MyCliExe}"', self.text)
        self.assertIn('Source: "{#Dist}\\{#MyAppExe}"', self.text)

    def test_config_preserved(self):
        line = [l for l in self.text.splitlines() if 'config.json' in l and l.startswith("Source")][0]
        self.assertIn("onlyifdoesntexist", line)
        self.assertIn("uninsneveruninstall", line)

    def test_no_autostart_tasks(self):
        self.assertNotIn("autostart", self.text)
        self.assertNotIn("--background", self.text)
        self.assertNotIn("--install-service", self.text)
        run = self.text.split("[Run]", 1)[1].split("[Code]", 1)[0]
        self.assertIn("postinstall", run)          # 安装完可选“立即打开窗口”
        self.assertIn("skipifsilent", run)         # 静默安装不启动任何东西

    def test_optional_open_at_login_task(self):
        """v3.0：可选任务“登录后自动打开窗口”= 当前用户启动文件夹里的快捷方式（不是服务/计划任务），默认不勾选，卸载时删除。"""
        tasks = self.text.split("[Tasks]", 1)[1].split("[", 1)[0]
        line = [l for l in tasks.splitlines() if l.startswith('Name: "openatlogin"')][0]
        self.assertIn("unchecked", line)
        icons = self.text.split("[Icons]", 1)[1].split("\n[", 1)[0]
        self.assertIn('Name: "{userstartup}\\{#MyAppName}"; Filename: "{app}\\{#MyAppExe}"', icons)
        self.assertIn("Tasks: openatlogin", icons)
        self.assertIn('Type: files; Name: "{userstartup}\\{#MyAppName}.lnk"',
                      self.text.split("[UninstallDelete]", 1)[1])
        self.assertNotIn("{commonstartup}", self.text)

    def test_uninstall_cleans_up(self):
        self.assertIn("firewall delete rule", self.text)
        self.assertIn("--no-pause", self.text)
        # 清理旧版残留（只删除，不创建）
        self.assertIn("'/Delete /TN ' + TaskName", self.text)
        self.assertIn("'delete ' + TaskName", self.text)
        self.assertIn("RemoveLegacyBackground();", self.text.split("CurUninstallStepChanged", 1)[1])

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
        self.assertIn("sign.ps1 dist\\ChromExtractOPC-cli.exe", self._t("build_exe.bat"))
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
