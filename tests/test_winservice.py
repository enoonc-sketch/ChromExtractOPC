# -*- coding: utf-8 -*-
"""Windows 服务：注册/更新/启动/停止/删除流程（模拟 sc.exe，含中文系统输出），参数安全，脚本和安装程序接入。"""
import os, sys, tempfile, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import winservice as ws   # noqa: E402


class P:
    def __init__(self, rc, out):
        self.returncode, self.stdout, self.stderr = rc, out.encode("gbk"), b""


class FakeSC:
    def __init__(self):
        self.exists, self.state, self.calls, self.deny = False, 1, [], False

    def __call__(self, a):
        a = a[1:]
        self.calls.append(a)
        cmd = a[0]
        if cmd == "query":
            if not self.exists:
                return P(1060, "[SC] OpenService 失败 1060:\r\n\r\n指定的服务未安装。")
            return P(0, "SERVICE_NAME: ChromExtractOPC\r\n  STATE              : %d  X\r\n" % self.state)
        if cmd == "qc":
            return P(0, "  START_TYPE         : 2   AUTO_START  (DELAYED)\r\n"
                        '  BINARY_PATH_NAME   : "C:\\ChromExtractOPC\\ChromExtractOPC.exe" --service\r\n'
                        "  SERVICE_START_NAME : LocalSystem\r\n")
        if cmd == "create":
            if self.deny:
                return P(5, "拒绝访问。")
            self.exists = True
            return P(0, "")
        if cmd == "start":
            self.state = 4
            return P(0, "")
        if cmd == "stop":
            if self.state == 1:
                return P(1062, "")
            self.state = 1
            return P(0, "")
        if cmd == "delete":
            self.exists = False
            return P(0, "")
        return P(0, "")


class Service(unittest.TestCase):
    def setUp(self):
        self.sc = FakeSC()
        fd, self.exe = tempfile.mkstemp(suffix=".exe")
        os.close(fd)

    def tearDown(self):
        os.remove(self.exe)

    def test_full_lifecycle(self):
        self.assertIsNone(ws.query(self.sc))
        ws.install(self.exe, self.sc)
        create = [c for c in self.sc.calls if c[0] == "create"][0]
        self.assertIn("delayed-auto", create)
        self.assertEqual(create[create.index("binPath=") + 1], '"%s" --service' % self.exe)
        self.assertTrue(any(c[0] == "failure" for c in self.sc.calls))       # 崩溃自动重启
        info = ws.query(self.sc)
        self.assertEqual((info["start"], info["delayed"], info["account"]), (2, True, "LocalSystem"))
        self.assertEqual(ws.start(self.sc), "服务已启动。")
        self.assertEqual(ws.query(self.sc)["state"], 4)
        self.assertEqual(ws.stop(self.sc), "服务已停止。")
        self.assertEqual(ws.stop(self.sc), "服务没有在运行。")
        self.assertEqual(ws.uninstall(self.sc), "服务已删除。")
        self.assertIsNone(ws.query(self.sc))

    def test_reinstall_updates_instead_of_failing(self):
        ws.install(self.exe, self.sc)
        self.sc.calls.clear()
        ws.install(self.exe, self.sc)
        self.assertIn("config", [c[0] for c in self.sc.calls])
        self.assertNotIn("create", [c[0] for c in self.sc.calls])

    def test_errors_in_plain_language(self):
        self.sc.deny = True
        with self.assertRaises(ws.ServiceError) as cm:
            ws.install(self.exe, self.sc)
        self.assertIn("管理员", str(cm.exception))
        with self.assertRaises(ws.ServiceError):
            ws.start(FakeSC())                       # 没注册就启动
        with self.assertRaises(ws.ServiceError):
            ws.install(os.path.join(tempfile.gettempdir(), "nope.exe"), self.sc)

    def test_args_are_lists(self):
        seen = []
        ws._sc(["query", ws.SERVICE_NAME], runner=lambda a: (seen.append(a), P(1060, ""))[1])
        self.assertIsInstance(seen[0], list)          # 不经过命令行解释器


class Wiring(unittest.TestCase):
    def _t(self, n):
        return open(os.path.join(ROOT, n), encoding="utf-8-sig").read()

    def test_installer_and_scripts(self):
        iss = self._t("installer/ChromExtractOPC.iss")
        self.assertIn("autostart\\service", iss)
        self.assertIn("--install-service", iss)
        self.assertIn("--uninstall-service", iss)
        self.assertIn("--install-service", self._t("安装为Windows服务.bat"))
        self.assertIn("--uninstall-service", self._t("卸载Windows服务.bat"))
        self.assertIn("--uninstall-service", self._t("卸载.bat"))
        self.assertIn("Get-Service", self._t(".github/workflows/build-exe.yml"))


if __name__ == "__main__":
    unittest.main()
