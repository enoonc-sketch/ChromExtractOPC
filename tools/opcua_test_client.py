#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
opcua_test_client.py —— OPC UA 验收测试客户端

用法：
  python opcua_test_client.py [endpoint] [--sec none|sign|sign_encrypt] [--cert 目录]

连接后浏览 Chromatograph 节点，读取每个检测器通道的元数据和曲线摘要。
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _force_utf8_stdout():
    """Windows 控制台默认编码（如 cp1252）写不下中文会直接崩溃；
    程序输出中文，启动时就把 stdout/stderr 强制切到 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            if stream is not None:
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


async def main():
    _force_utf8_stdout()
    endpoint = "opc.tcp://127.0.0.1:4840/ChromExtract/"
    sec = "none"
    cert_dir = "certs"
    args = sys.argv[1:]
    if args and not args[0].startswith("--"):
        endpoint = args.pop(0)
    if "--sec" in args:
        sec = args[args.index("--sec") + 1]
    if "--cert" in args:
        cert_dir = args[args.index("--cert") + 1]

    from asyncua import Client, ua
    client = Client(url=endpoint)
    if sec != "none":
        from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
        # 测试用：直接拿服务端自签名证书兼作客户端证书（应用 URI 需与证书一致）
        client.application_uri = "urn:chromextract:opcua:server"
        await client.set_security(
            SecurityPolicyBasic256Sha256,
            certificate="%s/server_cert.der" % cert_dir,
            private_key="%s/server_key.pem" % cert_dir,
            mode=(ua.MessageSecurityMode.SignAndEncrypt if sec == "sign_encrypt"
                  else ua.MessageSecurityMode.Sign))
    async with client:
        ns = await client.get_namespace_index("urn:chromextract:opcua")
        root = await client.nodes.objects.get_child(["%s:Chromatograph" % ns])
        print("已连接：%s" % endpoint)
        ver = await (await root.get_child(["%s:ServerVersion" % ns])).read_value()
        print("服务版本：", ver)
        for st in await root.get_children():
            name = (await st.read_browse_name()).Name
            if name.startswith("Station_"):
                print("== %s" % name)
                for dev in await st.get_children():
                    dname = (await dev.read_browse_name()).Name
                    if dname in ("Info",):
                        for v in await dev.get_children():
                            print("   %s.%s = %s" % (dname, (await v.read_browse_name()).Name,
                                                     await v.read_value()))
                        continue
                    vals = {}
                    for grp in await dev.get_children():
                        gname = (await grp.read_browse_name()).Name
                        for v in await grp.get_children():
                            vn = (await v.read_browse_name()).Name
                            val = await v.read_value()
                            if isinstance(val, list):
                                vals[vn] = "%.4g ~ %.4g (%d 点)" % (min(val), max(val), len(val))
                            else:
                                vals[vn] = val
                    print("   [%s]" % dname)
                    for k in sorted(vals):
                        print("      %s = %s" % (k, vals[k]))


if __name__ == "__main__":
    asyncio.run(main())
