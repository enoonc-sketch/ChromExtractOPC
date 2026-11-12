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


async def main():
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
    kwargs = {}
    if sec != "none":
        from asyncua.crypto.security_policies import SecurityPolicyBasic256Sha256
        cert = open("%s/server_cert.der" % cert_dir, "rb").read()
        key = open("%s/server_key.pem" % cert_dir, "rb").read()
        kwargs["security_policy"] = [SecurityPolicyBasic256Sha256,
                                     ua.MessageSecurityMode.SignAndEncrypt
                                     if sec == "sign_encrypt" else ua.MessageSecurityMode.Sign]
        kwargs["certificate"] = cert
        kwargs["private_key"] = key
    async with Client(url=endpoint, **kwargs) as client:
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
