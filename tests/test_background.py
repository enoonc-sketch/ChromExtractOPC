# -*- coding: utf-8 -*-
"""后台静默运行：状态文件、心跳过期判断、正常停止请求、单实例互斥。"""
import json, os, shutil, sys, tempfile, threading, time, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import bgrun   # noqa: E402


class Background(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_status_and_graceful_stop(self):
        self.assertFalse(bgrun.read_status(self.tmp)[0])
        stopped = threading.Event()
        k = bgrun.StatusKeeper(self.tmp, {"version": "x", "web": "http://127.0.0.1:8840/"},
                               stop_cb=stopped.set, poll=0.1).start()
        running, st, _ = bgrun.read_status(self.tmp)
        self.assertTrue(running)
        self.assertEqual(st["pid"], os.getpid())
        threading.Thread(target=lambda: (stopped.wait(5), k.close())).start()
        ok, msg = bgrun.request_stop(self.tmp, timeout=5, force=False)
        self.assertTrue(ok, msg)
        self.assertTrue(stopped.is_set())
        self.assertFalse(bgrun.read_status(self.tmp)[0])

    def test_stale_heartbeat_means_not_running(self):
        k = bgrun.StatusKeeper(self.tmp, {}, stop_cb=lambda: None, poll=60).start()
        p = bgrun.status_path(self.tmp)
        d = json.load(open(p, encoding="utf-8"))
        d["heartbeat_ts"] = time.time() - 10 * bgrun.HEARTBEAT_SEC
        json.dump(d, open(p, "w", encoding="utf-8"))
        running, _, msg = bgrun.read_status(self.tmp)
        self.assertFalse(running)
        self.assertIn("异常退出", msg)
        k.evt.set()

    def test_stop_when_not_running(self):
        self.assertTrue(bgrun.request_stop(self.tmp, timeout=1)[0])

    def test_leftover_stop_request_ignored_on_start(self):
        open(bgrun.stop_path(self.tmp), "w").close()      # 上次遗留的停止请求
        stopped = threading.Event()
        k = bgrun.StatusKeeper(self.tmp, {}, stop_cb=stopped.set, poll=0.1).start()
        time.sleep(0.4)
        self.assertFalse(stopped.is_set())
        k.close()

    def test_single_instance(self):
        a, b = bgrun.SingleInstance(self.tmp), bgrun.SingleInstance(self.tmp)
        self.assertTrue(a.acquire())
        try:
            self.assertFalse(b.acquire())
        finally:
            a.release()
        self.assertTrue(b.acquire())
        b.release()


if __name__ == "__main__":
    unittest.main()
