#!/usr/bin/env python3
"""
调试: 测试网易云扫码登录全流程, 打印原始 API 响应
"""
import sys, json, uuid
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import api

# 1. 获取 unikey
print("\n=== 1. 获取 unikey ===")
key = api.qrcode_unikey()
if not key:
    print("✗ 获取 unikey 失败")
    sys.exit(1)
print(f"✓ unikey: {key}")

# 2. 生成二维码 URL
url = f"https://music.163.com/login?codekey={key}"
print(f"\n  扫码 URL: {url}")

# 3. 生成二维码 (终端显示)
try:
    import qrcode
    import io
    qr = qrcode.QRCode(border=2, box_size=1)
    qr.add_data(url)
    qr.make()
    buf = io.StringIO()
    qr.print_ascii(out=buf)
    print("\n" + buf.getvalue())
except ImportError:
    print("\n  (qrcode 模块未安装, 无法显示二维码)")

print("\n=== 2. 轮询登录状态 (每 2 秒一次, 最长 60 秒) ===")
print("请用手机网易云 APP 扫码确认")
print()

for i in range(30):
    r = api.qrcode_login_check(key)
    code = r.get("code", 0)
    msg = r.get("message", "")

    print(f"  [{i*2}s] code={code}", end="")
    if msg:
        print(f"  message={msg}", end="")
    if code == 802:
        nick = r.get("nickname", "")
        print(f"  nickname={nick}", end="")
    if code == 803:
        cookie = r.get("cookie", "")
        print(f"\n\n  ✓ 登录成功!")
        if cookie:
            print(f"  cookie: {cookie[:80]}...")
        # 从 cookie 提取 MUSIC_U
        music_u = None
        if cookie:
            for part in cookie.split(";"):
                part = part.strip()
                if part.startswith("MUSIC_U="):
                    music_u = part.split("=", 1)[1]
                    break
        if not music_u:
            for c in api.get_session().cookies:
                if c.name == "MUSIC_U" and c.value:
                    music_u = c.value
                    break
        if music_u:
            print(f"  MUSIC_U: {music_u[:20]}...")
            api.set_cookie(f"MUSIC_U={music_u}")
            api.save_cookie_jar()
            print("  ✓ Cookie 已保存")
        else:
            print(f"  ✗ 未找到 MUSIC_U")
            print(f"  session cookies: {dict((c.name, c.value[:10]) for c in api.get_session().cookies)}")
        break
    elif code == 800:
        print("  ✗ 二维码已过期")
        break
    elif code == 801:
        print("  (等待扫码...)")
    elif code == -1:
        print(f"  ✗ 错误: {r.get('error', '未知')}")
    else:
        print(f"  (未知 code, 完整响应: {json.dumps(r, ensure_ascii=False)[:200]})")

    import time
    time.sleep(2)
else:
    print("\n  ✗ 超时")
