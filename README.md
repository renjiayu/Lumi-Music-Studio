# 🎵 Lumi Music Studio

> 网易云音乐终端 TUI 播放器 — 支持扫码登录、灰歌解锁、GStreamer 流式播放、Curses 暗色 UI

## 功能

- 🎧 **流式播放** — GStreamer 管道 (souphttpsrc → decodebin)，支持 MP3/AAC/FLAC/OGG
- 🔓 **灰歌解锁** — 集成 UnblockNeteaseMusic，自动代理解锁灰色歌曲
- 📱 **扫码登录** — Shift+Q 弹出二维码，手机网易云 APP 扫码即登
- 🍪 **Cookie Jar** — 完整会话持久化 (MUSIC_U + __csrf + MUSIC_A)，自动刷新 Token
- 🔄 **断点续传** — 退出自动保存播放状态，下次启动恢复
- 🎨 **Curses TUI** — Tokyo Night 暗色主题，螃蟹吉祥物动画
- 📊 **频谱可视化** — 实时音频频谱 (GStreamer spectrum 插件)
- 📝 **同步歌词** — 时间轴 LRC 歌词 + 翻译
- 🔍 **搜索** — 歌曲 / 专辑 / 歌手 / 歌单综合搜索
- 🏆 **排行榜** — 官方排行榜 + 热门歌单
- 📂 **我的歌单** — 登录后查看收藏歌单
- 📥 **下载** — 多线程下载 (320kbps 优先)，自动 ID3 标签
- 🔊 **MPRIS** — Linux 桌面媒体控制集成 (可选)

## 截图

```
 ⬢ Lumi Music Studio                        ○ 空闲                 200 首
─── 歌曲列表 ─────────────────────────────────── ─── 正在播放 ─────────────────
 ▶   0  海屿你                           4:51  ████░░ │       标题  -- 无曲目 --
 ▶   1  玻璃                             3:05  ██░░░░ │       歌手
 ▶   2  两 难                            2:50  ██░░░░ │       专辑
 ▶   3  Баллада                         3:02  ██░░░░ │       码率  0 kbps
 ✗   4  Angel                            4:15  ███░░░ │       进度
 ▶   5  忘不掉的你                        2:52  ██░░░░ │       状态  ○ 空闲
 ✗   6  恋人                             4:35  ████░░ │
 ▶   7  最后的借口                        3:44  ███░░░ │
 ▶  14  把回忆拼好给你                    6:21  █████░ │         ▄▄▄▄▄▄
 ▶  15  罗生门（Follow）                  4:03  ███░░░ │        █ -  - █ z
 ▶  16  静音恋人 (两颗缠绕的心)            3:26  ███░░░ │        █  ~▿  █
 ✗  17  小半                             4:57  ████░░ │        ▀▄▄▄▄▄▄▀
 ▶  18  遐想                             3:09  ██░░░░ │         ▐▌  ▐▌
 [全部] 200 首                                          ▀    ▀
──────────────────────────────────────────────────────────────────────────
 [ENTER]播放 [SPC]暂停 [n]下一首 [b]上一首 [+/-]快进/退 [r]随机 [c]循环
 [s]搜索 [p]榜单 [y]每日推荐 [m]我的歌单 [v]频谱 [f]筛选 [d]下载 [l]歌词
 [Q]扫码登录 [q]退出
```

## 安装

### 依赖

```bash
# 系统包 (Debian/Ubuntu/Kali)
sudo apt install python3-gi gir1.2-gstreamer-1.0 gstreamer1.0-plugins-bad

# Python 包
pip install -r requirements.txt
# 或一键安装
pip install PyGObject requests brotli pygame mutagen pycryptodome qrcode dbus-python
```

### 安装 UnblockNeteaseMusic (可选，解锁灰歌)

```bash
npm install -g @unblockneteasemusic/server
```

### 安装 Lumi

```bash
cd ~/Projects/lumi-music-studio
./lumi.sh install     # 安装到 ~/.local/bin/lumi
# 确保 ~/.local/bin 在 PATH 中
```

## 使用

```bash
# 一键启动
./lumi.sh

# 或直接启动 TUI
python3 tui.py

# 已安装后全局启动
lumi
```

### 快捷键

| 键 | 功能 |
|-----|------|
| `ENTER` | 播放选中歌曲 |
| `SPC` | 暂停 / 继续 |
| `n` / `b` | 下一首 / 上一首 |
| `+` / `-` | 快进 / 快退 10 秒 |
| `r` | 随机播放 |
| `c` | 循环模式 (关→单曲→列表) |
| `s` | 搜索 |
| `p` | 排行榜 |
| `y` | 每日推荐 (需登录) |
| `m` | 我的歌单 (需登录) |
| `v` | 频谱开关 |
| `f` | 筛选 (全部/可播) |
| `d` | 下载当前歌曲 |
| `l` | 歌词弹窗 |
| `Shift+Q` | **扫码登录** |
| `q` | 退出 |

### 登录

**扫码登录 (推荐):** 启动 TUI → 按 `Shift+Q` → 手机网易云 APP 扫码

**手动 Cookie:** 启动后输入 `:cookie <你的MUSIC_U值>`

**自动读取:** 已登录 Firefox 浏览器的 Cookie 会被自动读取

## 配置

`~/.config/lumi-music/config.json`:
```json
{
  "music_u": "",
  "device_id": "auto-generated",
  "default_br": 320000,
  "download_dir": "~/Music/网易云",
  "auto_next": true,
  "unblock": true,
  "unblock_port": 5200
}
```

环境变量:
- `LUMI_MUSIC_ROOT` — 重定向 config/cache 目录
- `HTTPS_PROXY` / `HTTP_PROXY` — 代理设置

## 项目结构

```
lumi-music-studio/
├── api.py          # 网易云 API 封装 (WeAPI + Cookie Jar + Device ID)
├── weapi.py        # AES-128-CBC + RSA-1024 加密
├── cli.py          # 播放引擎 (GStreamer 管道 + 状态机)
├── tui.py          # Curses TUI 界面
├── config.py       # JSON 配置持久化
├── state.py        # 播放状态持久化 (断点续传)
├── unblock.py      # UnblockNeteaseMusic 生命周期
├── visualizer.py   # 终端频谱可视化
├── downloader.py   # 多线程 MP3 下载器
├── mpris.py        # D-Bus MPRIS2 桌面集成
├── lumi.sh         # 一键启动脚本
└── CLAUDE.md       # Claude Code 指导文档
```

## API 模块

可直接作为 Python 库使用：

```python
import api

# 搜索
r = api.search("周杰伦", stype=1, limit=10)

# 歌单详情 (自动翻页)
r = api.playlist_detail_all(3778678)

# 歌词
r = api.lyric(108914)

# 扫码登录
key = api.qrcode_unikey()
# 显示二维码: https://music.163.com/login?codekey={key}
r = api.qrcode_login_check(key)  # 轮询直到 code=803

# Cookie 管理
api.set_cookie("MUSIC_U=xxx")
api.save_cookie_jar()
api.load_cookie_jar()
```

## License

MIT
