"""测试 auth/uamtk 需要的前置 Cookie。"""
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/126.0.0.0",
      "Referer": "https://kyfw.12306.cn/otn/passport?redirect=/otn/login/userLogin",
      "X-Requested-With": "XMLHttpRequest"}
URL = "https://kyfw.12306.cn/passport/web/auth/uamtk"

# 场景1：先访问 passport/web/login 拿 _passport_session，再调 auth/uamtk
s = requests.Session()
s.cookies.set("uamtk", "bogusvalue123", domain="kyfw.12306.cn", path="/passport")
r0 = s.get("https://kyfw.12306.cn/passport/web/login", headers=UA)
print("web/login cookies:", [c.name for c in s.cookies])
r = s.post(URL, data={"appid": "otn"}, headers=UA, allow_redirects=False)
print("场景1 POST:", r.status_code, r.text[:200])

# 场景2：不访问 web/login，只带 uamtk
s2 = requests.Session()
s2.cookies.set("uamtk", "bogusvalue123", domain="kyfw.12306.cn", path="/passport")
r2 = s2.post(URL, data={"appid": "otn"}, headers=UA, allow_redirects=False)
print("场景2 POST:", r2.status_code, r2.text[:200])
