# ♫ 网易云音乐 CLI

基于 Python 的网易云音乐命令行工具，支持搜索、浏览歌单、查看歌词、播放歌曲。

## 功能

- 🔍 **搜索** — 歌曲/专辑/歌手/歌单 综合搜索
- 🏆 **排行榜** — 浏览官方排行榜和热门歌单
- 📝 **歌词** — 查看时间轴歌词 + 翻译
- ▶️ **播放** — 320kbps 高品质音频播放 (需登录 Cookie)

## 安装依赖

```bash
# Python 依赖 (均已系统预装)
python3 -c "import requests; import pygame"
```

## 使用

```bash
cd ~/Projects/ncm-api
python3 cli.py
```

### 命令

| 命令 | 说明 |
|------|------|
| `s` / `search` | 搜索歌曲 |
| `p` / `chart` | 浏览排行榜和歌单 |
| `.` / `stop` | 停止播放 |
| `:cookie <值>` | 设置登录 Cookie |
| `q` / `exit` | 退出 |
| `L<序号>` | 查看歌词 (如 `L3`) |

### 登录 (解锁播放功能)

1. 浏览器打开 https://music.163.com 并登录
2. F12 → Application → Cookies → music.163.com
3. 复制 `MUSIC_U` 的值
4. 在 CLI 中输入 `:cookie <你的MUSIC_U值>`

不登录也可以搜索、浏览歌单、看歌词。

## API 模块

可直接作为 Python 库使用：

```python
from api import search, playlist_detail, lyric, song_url, set_cookie, normalize_song

# 搜索
r = search("周杰伦", stype=1, limit=10)
for song in r["result"]["songs"]:
    ns = normalize_song(song)
    print(ns["name"], ns["artists"])

# 歌单详情
r = playlist_detail(3778678)  # 热歌榜

# 歌词
r = lyric(108914)
print(r["lrc"]["lyric"])

# 播放 (需要先登录)
set_cookie("MUSIC_U=你的Cookie值")
r = song_url(108914)
print(r["data"][0]["url"])
```

## 项目结构

```
ncm-api/
├── api.py        # API 封装 (搜索/歌单/歌词/播放)
├── cli.py        # CLI 交互播放器
├── encrypt.py    # weapi 加密模块 (高级接口预留)
└── README.md
```

## API 端点

| 端点 | 说明 | 需登录 |
|------|------|--------|
| `/api/search/get` | 搜索 | ❌ |
| `/api/cloudsearch/get/web` | 综合搜索 | ❌ |
| `/api/playlist/detail` | 歌单详情 | ❌ |
| `/api/toplist` | 排行榜列表 | ❌ |
| `/api/song/lyric` | 歌词 | ❌ |
| `/api/artist/top/song` | 歌手热门 | ❌ |
| `/api/v1/resource/comments/get` | 评论 | ❌ |
| `/api/song/enhance/player/url` | 播放链接 | ✅ |
| `/api/v1/discovery/recommend/songs` | 每日推荐 | ✅ |
