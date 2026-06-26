"""
网易云音乐 API 封装
- /api/ 端点使用明文参数 (GET)
- 播放链接需要登录 Cookie
"""
import json
import os
import shutil
from pathlib import Path
from typing import Optional
import requests

# brotli 解压支持 (网易部分接口返回 br 压缩)
try:
    import brotli
    HAS_BROTLI = True
except ImportError:
    HAS_BROTLI = False

BASE_URL = "https://music.163.com"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://music.163.com/",
}

# ========== 终端颜色 ==========
_COLOR_MAP = {"red":31,"green":32,"yellow":33,"blue":34,"magenta":35,"cyan":36,"bold":1,"dim":2}


def c(s, code):
    """将 ANSI 颜色码包裹到字符串两端。code 可为预定义名称或数字色码。"""
    numeric = _COLOR_MAP.get(code, code)
    return f"\033[{numeric}m{s}\033[0m"


_session = None


def _detect_proxy() -> dict:
    """检测可用代理: 环境变量 → Clash Verge → UnblockNeteaseMusic"""
    # 1. 环境变量显式指定（最高优先级）
    for var in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        val = os.environ.get(var, "").strip()
        if val:
            return {"http": val, "https": val}

    candidates = []

    # 2. Clash Verge (标准端口)
    try:
        s = __import__("socket").socket()
        s.settimeout(0.3)
        s.connect(("127.0.0.1", 7897))
        s.close()
        candidates.append("http://127.0.0.1:7897")
    except (OSError, ImportError):
        pass

    # 3. UnblockNeteaseMusic
    try:
        import unblock
        url = unblock.proxy_url()
        if url:
            candidates.append(url)
    except ImportError:
        pass

    if not candidates:
        return {}
    url = candidates[0]
    return {"http": url, "https": url}


def get_session() -> requests.Session:
    """获取或创建全局 session"""
    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update(HEADERS)
    proxy = _detect_proxy()
    if proxy:
        _session.proxies.update(proxy)
    else:
        _session.proxies.clear()
    return _session


def set_cookie(cookie_str: str):
    """设置登录 Cookie (从浏览器复制)"""
    s = get_session()
    for item in cookie_str.split(";"):
        item = item.strip()
        if "=" in item:
            k, v = item.split("=", 1)
            s.cookies.set(k.strip(), v.strip())
    # 验证登录
    try:
        r = s.get(f"{BASE_URL}/api/nuser/account/get")
        if r.json().get("code") == 200:
            return True, "登录成功"
        return False, r.json().get("message", "未知错误")
    except Exception as e:
        return False, str(e)


def auto_load_firefox_cookie() -> bool:
    """从 Firefox 自动读取 MUSIC_U cookie (使用系统 sqlite3 命令)

    通过系统 sqlite3 命令读取 cookies.sqlite，避免 Python sqlite3 模块
    在某些环境下可能遇到的问题。需要 __csrf 一起才能正常请求 API。
    """
    import subprocess

    for base_dir in (
        Path.home() / ".mozilla/firefox",
        Path.home() / ".var/app/org.mozilla.firefox/.mozilla/firefox",
    ):
        if not base_dir.exists():
            continue
        profiles = [p for p in base_dir.iterdir() if p.is_dir()]
        for profile in profiles:
            db_path = profile / "cookies.sqlite"
            if not db_path.exists():
                continue
            try:
                # 用 cp 避免锁表, sqlite3 命令行查询
                # 分两次独立查询, 避免 shlex.quote 在 SQL 值中转义错误
                tmp = f"/tmp/ncm_cookies_{os.getpid()}_{profile.name}.sqlite"
                shutil.copy2(str(db_path), tmp)

                MUSIC_U_RESULT = "MUSIC_U"  # sentinel for regex
                result = subprocess.run(
                    ["sqlite3", tmp,
                     "SELECT name || '=' || value FROM moz_cookies "
                     "WHERE host LIKE '%music.163.com%' "
                     "AND name IN ('MUSIC_U', '__csrf') "
                     "ORDER BY name;"],
                    capture_output=True, text=True, timeout=5,
                )
                os.remove(tmp)
                if result.returncode != 0:
                    continue
                cookies = {}
                for line in result.stdout.strip().split("\n"):
                    if "=" in line:
                        k, v = line.split("=", 1)
                        cookies[k] = v
                if "MUSIC_U" in cookies and cookies["MUSIC_U"]:
                    cookie_str = f"MUSIC_U={cookies['MUSIC_U']}"
                    if "__csrf" in cookies:
                        cookie_str += f"; __csrf={cookies['__csrf']}"
                    ok, _ = set_cookie(cookie_str)
                    if ok:
                        return True
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue
            except Exception:
                continue
    return False


def auto_load_browser_cookie() -> bool:
    """尝试从已安装浏览器自动加载登录态"""
    return auto_load_firefox_cookie()


def account_profile() -> dict:
    """获取当前登录账号信息"""
    return _get("/api/nuser/account/get")


def get_login_uid() -> Optional[int]:
    """返回已登录用户 uid, 未登录返回 None"""
    r = account_profile()
    if r.get("code") != 200:
        return None
    profile = r.get("profile") or r.get("account") or {}
    uid = profile.get("userId") or profile.get("id")
    return int(uid) if uid else None


def _decode_response(r):
    """解压 brotli 编码的响应，失败时回退到纯文本"""
    if HAS_BROTLI and r.headers.get('content-encoding') == 'br':
        try:
            return brotli.decompress(r.content).decode('utf-8')
        except Exception:
            pass
    return r.text


def _get(uri: str, params: dict = None) -> dict:
    """GET 请求，自动处理拼接 JSON 和 brotli 压缩"""
    s = get_session()
    r = s.get(f"{BASE_URL}{uri}", params=params)
    text = _decode_response(r)
    return _parse_json(text)


def _post_weapi(uri: str, data: dict = None, csrf_token: str = "") -> dict:
    """POST 请求，使用 WeAPI (AES+RSA) 加密"""
    import weapi
    s = get_session()
    payload = data or {}
    payload["csrf_token"] = csrf_token
    params, enc_sec_key = weapi.encrypt(payload)
    r = s.post(
        f"{BASE_URL}{uri}?csrf_token={csrf_token}",
        data={"params": params, "encSecKey": enc_sec_key},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    text = _decode_response(r)
    return _parse_json(text)


def _parse_json(text: str) -> dict:
    """统一的 JSON 解析，支持拼接 JSON"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        obj, _ = json.JSONDecoder().raw_decode(text)
        return obj
    except json.JSONDecodeError:
        return {"code": -1, "error": "JSON解析失败", "raw": text[:200]}


def search(keyword: str, stype: int = 1, limit: int = 30, offset: int = 0) -> dict:
    """
    搜索 (单曲/专辑/歌手/歌单)
    stype: 1=单曲 10=专辑 100=歌手 1000=歌单 1014=视频
    """
    return _get("/api/search/get", {
        "s": keyword, "type": stype,
        "limit": limit, "offset": offset,
    })

def song_url(song_id: int, br: int = 320000) -> dict:
    """
    获取歌曲播放地址
    br: 128000=128k, 320000=320k, 999000=无损
    注意: 部分歌曲即使有链接也可能因版权无法播放
    """
    return _get("/api/song/enhance/player/url", {
        "ids": f"[{song_id}]", "br": br,
    })


def song_urls(song_ids: list, br: int = 320000) -> dict:
    """批量获取播放地址"""
    ids_str = ",".join(map(str, song_ids))
    return _get("/api/song/enhance/player/url", {
        "ids": f"[{ids_str}]", "br": br,
    })


def resolve_song_url(song_id: int, preferred_br: int = 320000) -> tuple:
    """按优先级尝试码率，返回 (url, br) 或 (None, 0)"""
    for br in dict.fromkeys([preferred_br, 320000, 128000]):
        r = song_url(song_id, br=br)
        data = (r.get("data") or [{}])[0]
        url = data.get("url")
        if url:
            return url, data.get("br", 0)
    return None, 0


def lyric(song_id: int) -> dict:
    """获取歌词"""
    return _get("/api/song/lyric", {"id": song_id, "lv": -1, "tv": -1})


def song_detail(song_ids) -> dict:
    """获取歌曲详情 (支持单个ID或ID列表)"""
    if isinstance(song_ids, int):
        song_ids = [song_ids]
    ids_str = f"[{','.join(map(str, song_ids))}]"
    return _get("/api/v3/song/detail", {"c": ids_str})


def playlist_detail(playlist_id: int, limit: int = 100, offset: int = 0) -> dict:
    """获取歌单详情

    注意: 端点使用非标准参数名 n/s (对应 limit/offset),
    这是 Netease API 的历史遗留.
    """
    return _get("/api/v6/playlist/detail", {
        "id": playlist_id, "n": limit, "s": offset,
    })


def top_list() -> dict:
    """获取排行榜列表"""
    return _get("/api/toplist")


def artist_top_songs(artist_id: int, limit: int = 50) -> dict:
    """获取歌手热门歌曲"""
    return _get("/api/artist/top/song", {
        "id": artist_id, "limit": limit,
    })


def artist_detail(artist_id: int) -> dict:
    """获取歌手详情"""
    return _get("/api/artist/detail", {"id": artist_id})


def album_detail(album_id: int) -> dict:
    """获取专辑详情"""
    return _get("/api/album", {"id": album_id})


def daily_recommend() -> dict:
    """每日推荐歌曲 (需要登录)"""
    return _get("/api/v1/discovery/recommend/songs")


def user_playlist(uid: int, limit: int = 30, offset: int = 0) -> dict:
    """获取用户歌单"""
    return _get("/api/user/playlist", {
        "uid": uid, "limit": limit, "offset": offset,
    })


# ========== 扫码登录 ==========

import uuid

def qrcode_unikey() -> Optional[str]:
    """获取二维码登录的 unikey，返回 key 或 None"""
    r = _post_weapi("/weapi/login/qrcode/unikey", {
        "key": str(uuid.uuid4()),
        "type": 1,
    })
    if r.get("code") != 200:
        return None
    return r.get("unikey")


def qrcode_login_check(key: str) -> dict:
    """轮询二维码登录状态

    返回: {"code": int, "message": str}
    code: 800=过期/错误, 801=等待扫码, 802=已扫码待确认, 803=登录成功
    """
    return _post_weapi("/weapi/login/qrcode/client/login", {
        "key": key,
        "type": 1,
    })


def login_by_qrcode() -> Optional[str]:
    """完整扫码登录流程: 生成二维码并轮询

    CLI 调用示例:
        key = api.qrcode_unikey()
        # 显示二维码 (url = f"https://music.163.com/login?codekey={key}")
        # 循环调用 api.qrcode_login_check(key)
    """
    return None


def playlist_detail_all(playlist_id: int, page_size: int = 100) -> dict:
    """自动翻页获取歌单全部曲目，返回 {"name": str, "tracks": [track, ...]}

    /api/v6/playlist/detail 单次最多返回 100 首，此函数自动处理翻页。
    """
    all_tracks = []
    offset = 0
    max_tracks = None
    name = ""
    is_first = True
    while True:
        r = playlist_detail(playlist_id, limit=page_size, offset=offset)
        playlist = r.get("playlist", {})
        if is_first:
            name = playlist.get("name", str(playlist_id))
            is_first = False
        tracks = playlist.get("tracks", [])
        if not tracks:
            break
        all_tracks.extend(tracks)
        if max_tracks is None:
            max_tracks = playlist.get("trackCount", 0) or len(tracks)
        if len(tracks) < page_size or len(all_tracks) >= max_tracks:
            break
        offset += page_size
    return {"name": name, "tracks": all_tracks}


def check_playable(song_ids: list, br: int = 320000) -> set:
    """批量检测哪些歌曲可播放, 返回可播ID集合"""
    if not song_ids:
        return set()
    r = song_urls(song_ids, br=br)
    playable = set()
    for d in r.get("data", []):
        if d.get("url"):
            playable.add(d["id"])
    return playable


# ========== 数据提取辅助 ==========

def get_artists(song: dict) -> list:
    """从歌曲对象中提取歌手列表 (兼容不同接口的字段名)"""
    for key in ("ar", "artists"):
        if key in song:
            return [{"name": a.get("name", ""), "id": a.get("id", 0)}
                    for a in song[key]]
    return []


def get_album(song: dict) -> dict:
    """从歌曲对象中提取专辑信息"""
    for key in ("al", "album"):
        if key in song:
            return {"name": song[key].get("name", ""),
                    "id": song[key].get("id", 0)}
    return {"name": "", "id": 0}


def format_artists(artists: list) -> str:
    """从歌手列表生成 '歌手1, 歌手2' 字符串"""
    return ", ".join(a["name"] for a in artists)


def normalize_song(song: dict) -> dict:
    """标准化歌曲数据"""
    return {
        "id": song.get("id", 0),
        "name": song.get("name", ""),
        "artists": get_artists(song),
        "album": get_album(song),
        "duration": song.get("dt", song.get("duration", 0)),
        "fee": song.get("fee", 0),
        "mvid": song.get("mv", song.get("mvid", 0)),
    }


# ========== 测试 ==========
if __name__ == "__main__":
    print("=" * 50)
    print("🔍 搜索: 张学友")
    r = search("张学友", limit=3)
    songs = r.get("result", {}).get("songs", [])
    for i, s in enumerate(songs):
        ns = normalize_song(s)
        artists = format_artists(ns["artists"])
        print(f"  [{i}] {ns['name']} — {artists}  ID:{ns['id']}")

    print()
    print("📋 歌单: 热歌榜")
    r = playlist_detail(3778678, limit=3)
    pl = r.get("playlist", {})
    print(f"  名称: {pl.get('name')}")
    for t in pl.get("tracks", [])[:3]:
        ns = normalize_song(t)
        artists = format_artists(ns["artists"])
        print(f"  🎵 {ns['name']} — {artists}  ID:{ns['id']}")

    print()
    print("🎤 歌词测试:")
    r = lyric(108914)
    lrc = r.get("lrc", {}).get("lyric", "")
    if lrc:
        for line in lrc.split("\n")[:5]:
            print(f"  {line}")

    print()
    print("🔗 播放链接测试:")
    r = song_url(1973665667)  # 热歌榜歌曲
    data = r.get("data", [{}])[0] if r.get("data") else {}
    print(f"  url={'✓ 可播放' if data.get('url') else '✗ 不可播放 (版权/地域限制)'}")
    if data.get("url"):
        print(f"  码率: {data.get('br',0)//1000}kbps")
        print(f"  大小: {data.get('size',0)//1024//1024}MB")
