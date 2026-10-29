#!/usr/bin/env python3
"""reality 借用目标池（REALITY_SNI_POOL）—— 仓库主人 10/18：「再去找十几个支持抗量子加密的，把那些不支持的删掉，能凑够 20 多个」
  洛杉矶一台实测（openssl：TLS1.3 / h2；xray tls ping：X25519MLKEM768 / 证书链长）：
  原池 21 个只有 9 个能开 ML-DSA-65；不够格的 12 个拿掉，40 个大厂候选里补实测全过的 10 个，共 19 个；同日再补 6 个，共 25 个。
  10/29 仓库主人：「那两个估计是黑名单的给他删掉吧然后把这里面你估计是白名单的都加进去」——拿掉国内打不开的
  www.docker.com、react.dev，补洛杉矶实测过、国内能开的 5 个，共 28 个。
"""
import os, re, sys, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("xy", os.path.join(ROOT, "xy-installer.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)

POOL = list(m.REALITY_SNI_POOL)
KEEP = ["www.cisco.com", "www.oracle.com", "www.ibm.com", "www.vmware.com", "www.qualcomm.com",
        "www.samsung.com", "academy.nvidia.com", "addons.mozilla.org"]
ADD = ["www.nvidia.com", "developer.nvidia.com", "www.broadcom.com", "www.nxp.com", "www.analog.com",
       "www.atlassian.com", "www.mastercard.com", "www.ebay.com", "www.mozilla.org",
       # 同日第二批（45 个测过 14 个，挑链最长的 6 个）
       "www.boeing.com", "www.airbus.com", "www.fedex.com", "www.schneider-electric.com", "www.3m.com", "www.infineon.com",
       # 10/29 第三批（洛杉矶实测过、在华有正式业务）
       "www.st.com", "www.appliedmaterials.com", "www.lamresearch.com", "www.paypal.com", "www.americanexpress.com"]
CN_BLOCKED = ["www.docker.com", "react.dev"]   # 国内打不开 / 时好时坏：墙看 SNI 就掐
GONE = ["www.python.org", "www.mysql.com", "www.mongodb.com", "redis.io", "www.intel.com", "www.amd.com",
        "www.dell.com", "www.bing.com", "www.tesla.com", "www.asus.com", "vuejs.org", "www.swift.com",
        "s0.awsstatic.com", "updates.cdn-apple.com", "swcdn.apple.com", "cdn-dynmedia-1.microsoft.com"]
check(f"国内打不开的不在池里（{sorted(set(CN_BLOCKED) & set(POOL))}）", not set(CN_BLOCKED) & set(POOL))
check(f"原池里实测够格的 8 个都留着（缺 {[x for x in KEEP if x not in POOL]}）", all(x in POOL for x in KEEP))
check(f"新补的 20 个都在（缺 {[x for x in ADD if x not in POOL]}）", all(x in POOL for x in ADD))
check(f"开不了抗量子签名 / 没 h2 / CDN 资源域名都不在（{sorted(set(GONE) & set(POOL))}）", not set(GONE) & set(POOL))
check(f"一共 28 个、没有重复（{len(POOL)}）", len(POOL) == 28 == len(set(POOL)))
check("都是合法的小写主机名", all(re.fullmatch(r"(?:[a-z0-9-]+\.)+[a-z]{2,}", x) for x in POOL))
check(f"兜底默认值 {m.G['sni']} 在池里", m.G["sni"] in POOL)
doc = open(os.path.join(ROOT, "docs/01-install.md"), encoding="utf-8").read()
check("docs/01-install.md 写的个数跟池子一致、写明都能开抗量子签名", f"等 {len(POOL)} 个大站主站" in doc and "都能开抗量子签名" in doc)
print("ALL PASS" if not bad else f"{bad} 项没过"); sys.exit(1 if bad else 0)
