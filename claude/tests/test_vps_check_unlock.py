#!/usr/bin/env python3
"""17 → 3 解锁检测（仓库主人 10/31：「都加上吧，再做个真解锁检测」）+ 可达性加上 Gemini / Spotify / Prime Video / HBO Max / B站港澳台。
  各家判断喂假返回逐个过一遍：能看 / 部分 / 不支持 / 连不上 / 页面对不上（报「?」不硬判）。"""
import os, re, io, sys, contextlib, importlib.util
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
spec = importlib.util.spec_from_file_location("v", os.path.join(ROOT, "vps-check.py"))
v = importlib.util.module_from_spec(spec); spec.loader.exec_module(v)
bad = 0
def check(l, ok):
    global bad; bad += not ok; print(("  ✔ " if ok else "  ✖ ") + l)
H = "https" + "://"
R = {}                                   # url 片段 → (code, final_url, body)
def fake(url, timeout=10, headers=None, data=None):
    for k, val in R.items():
        if k in url: return val
    return 0, url, ""
v._get = fake

print("可达性列表")
names = [n for n, _ in v.SERVICES]
check(f"加了 5 个（{'、'.join(names)}）", names[-5:] == ["Gemini", "Spotify", "Prime Video", "HBO Max", "B站港澳台"]
      and names[:8] == ["Google", "YouTube", "ChatGPT", "Claude", "Netflix", "Disney+", "TikTok", "GitHub"])
check("中文名按显示宽度对齐", v._pad("B站港澳台", 12) == "B站港澳台" + " " * 3 and v._pad("Netflix", 12) == "Netflix" + " " * 5)

print("Netflix")
R.clear(); R["81280792"] = (200, H + "www.netflix.com/title/81280792", '"requestCountry":{"id":"US"}')
check("非自制能看：全解锁 + 地区", v.ul_netflix() == ("ok", "全解锁（US）"))
R.clear(); R["81280792"] = (404, "", "Oh no!"); R["70143836"] = (404, "", "Oh no!")
R["80018499"] = (200, H + "www.netflix.com/jp/title/80018499", "")
check("只有自制剧：部分", v.ul_netflix() == ("part", "仅自制剧（JP）"))
R["80018499"] = (404, "", "")
check("都不行：不支持", v.ul_netflix()[0] == "no")
R.clear()
check("连不上：?", v.ul_netflix()[0] == "?")

print("YouTube Premium")
R.clear(); R["youtube.com/premium"] = (200, "", '"INNERTUBE_CONTEXT_GL":"US" YouTube Premium ad-free')
check("可用 + 地区", v.ul_youtube() == ("ok", "Premium 可用（US）"))
R["youtube.com/premium"] = (200, "", '"INNERTUBE_CONTEXT_GL":"CN" Premium is not available in your country')
check("不可用", v.ul_youtube()[0] == "no")
R["youtube.com/premium"] = (200, "", "<html>something else</html>")
check("页面对不上：?", v.ul_youtube()[0] == "?")

print("ChatGPT / Claude / Gemini")
R.clear(); R["cookie_requirements"] = (200, "", "{}"); R["chatgpt.com/cdn-cgi"] = (200, "", "fl=1\nloc=US\n")
R["ios.chat"] = (200, "", "ok")
check("ChatGPT 可用", v.ul_chatgpt() == ("ok", "可用（US）"))
R["ios.chat"] = (403, "", "blocked: VPN detected")
check("ChatGPT App 被拦：部分", v.ul_chatgpt()[0] == "part")
R["cookie_requirements"] = (403, "", '{"cause":"unsupported_country"}')
check("ChatGPT 不支持地区", v.ul_chatgpt()[0] == "no")
R.clear(); R["claude.ai/cdn-cgi"] = (200, "", "loc=US\n"); R["claude.ai/"] = (200, H + "claude.ai/login", "")
check("Claude 可用（US）", v.ul_claude() == ("ok", "可用（US）"))
R["claude.ai/"] = (200, H + "claude.ai/app-unavailable-in-region", "")
check("Claude 跳到不可用页：不支持", v.ul_claude()[0] == "no")
R["claude.ai/"] = (403, H + "claude.ai/", ""); R["claude.ai/cdn-cgi"] = (200, "", "loc=HK\n")
check("Claude 地区在不服务名单：不支持", v.ul_claude() == ("no", "不支持（HK）"))
R.clear(); R["gemini"] = (200, "", 'x45631641,null,true,2,1,200,"USA"')
check("Gemini 可用", v.ul_gemini() == ("ok", "可用（USA）"))
R["gemini"] = (200, "", "nope")
check("Gemini 不支持", v.ul_gemini()[0] == "no")

print("其余几家")
R.clear(); R["tiktok"] = (200, "", '"region":"US"')
check("TikTok 地区", v.ul_tiktok() == ("ok", "可用（US）"))
R.clear(); R["spotify"] = (200, H + "www.spotify.com/us/", "")
check("Spotify 跳到 /us/", v.ul_spotify() == ("ok", "可用（US）"))
R["spotify"] = (200, H + "www.spotify.com/select-your-country-region/", "")
check("Spotify 不支持", v.ul_spotify()[0] == "no")
R.clear(); R["primevideo"] = (200, "", '"currentTerritory":"US"')
check("Prime Video 地区", v.ul_prime() == ("ok", "可用（US）"))
R.clear(); R["hbomax"] = (200, H + "www.hbomax.com/us/en", "")
check("HBO Max 跳到 /us/", v.ul_hbomax() == ("ok", "可用（US）"))
R.clear(); R["disneyplus"] = (200, H + "www.disneyplus.com/unavailable", "")
check("Disney+ 不可用", v.ul_disney()[0] == "no")
R.clear(); R["bilibili"] = (200, "", '{"code":-10403}')
check("B站港澳台：非港澳台 IP 不支持", v.ul_bilibili()[0] == "no")
R["bilibili"] = (200, "", '{"code":0}')
check("B站港澳台：能播", v.ul_bilibili()[0] == "ok")

print("整屏")
R.clear(); R["81280792"] = (200, "", '"requestCountry":{"id":"US"}')
def boom(): raise RuntimeError("x")
v.UNLOCKS = v.UNLOCKS + [("坏的", boom)]
b = io.StringIO()
with contextlib.redirect_stdout(b):
    v.check_unlock()
out = re.sub(r"\033\[[0-9;]*m", "", b.getvalue())
check("每家一行、Netflix 全解锁、连不上的报 ?、出错的不炸", "✓ Netflix" in out and "全解锁（US）" in out
      and "? Gemini" in out and "检测出错" in out and out.count("\n  ") >= 12)
check("main 认 --unlock", "--unlock" in open(os.path.join(ROOT, "vps-check.py"), encoding="utf-8").read())
xy = open(os.path.join(ROOT, "xy-installer.py"), encoding="utf-8").read()
check("17 菜单：3 解锁检测（流媒体 / AI），原来 1 / 2 不动", '(3, "解锁检测（流媒体 / AI）", None)' in xy
      and '(1, "本机 IP 检测（约 2 分钟）", None), (2, "外部 IP 检测", None)' in xy and 'arg = " --unlock" if c == "3"' in xy)
check("解锁检测用浏览器 UA，不自报家门", "nodekit" not in v.BROWSER_UA and "Mozilla/5.0" in v.BROWSER_UA)
print("\nALL PASS" if not bad else f"\nFAIL {bad}"); sys.exit(1 if bad else 0)
