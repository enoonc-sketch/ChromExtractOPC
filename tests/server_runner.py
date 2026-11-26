# -*- coding: utf-8 -*-
"""测试专用：在子进程里运行一个真实 OPC UA 服务（不开窗口），供 test_security / test_transmission
用客户端去连。v2.9 起产品本身只有桌面窗口一种运行方式（不再有控制台/后台服务模式），
自动测试又需要一个可以被 terminate() 结束的无界面服务进程，所以放在 tests 目录里，不打包、不发布。

用法：python tests/server_runner.py 配置.json      结束：terminate() / Ctrl+C
（文件名不以 test 开头，unittest discover 不会把它当成测试收集。）
"""
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server_app as sa   # noqa: E402


def main(cfg_path):
    sa._force_utf8_stdout()
    base = sa.program_dir()
    cfg = sa.load_config(cfg_path)
    cfg["_base_dir"], cfg["_cfg_path"] = base, cfg_path
    try:
        sa.normalize_config(cfg, base)
    except sa.ConfigError as e:
        print("配置有误：%s" % e)
        return 2
    sa.setup_logging(cfg, base)
    for lv, msg in sa.security_report(cfg):
        if lv != "正常":
            sa.get_log().warning("安全提示[%s]：%s", lv, msg)
    web = sa.start_web_settings(cfg)
    try:
        asyncio.run(sa.run_server(cfg))
    except KeyboardInterrupt:
        pass
    except OSError as e:
        sa.get_log().error("服务无法启动：%s", e)
        return 3
    finally:
        if web is not None:
            web.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
