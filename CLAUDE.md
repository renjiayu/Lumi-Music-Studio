# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project overview

Lumi Music Studio — 网易云音乐终端 TUI 播放器。Python 项目，无外部框架。通过逆向的 WeAPI 加密协议调用网易云音乐接口，GStreamer 流式播放，Curses TUI 界面。

## Commands

```bash
# 一键安装依赖 + 启动 TUI
./lumi.sh

# 直接启动 (跳过安装)
python3 tui.py

# 仅安装到 ~/.local/bin (之后全局可用 lumi)
./lumi.sh install

# 验证代码无语法错误
python3 -c "import py_compile; py_compile.compile('cli.py', doraise=True)"
python3 -c "import py_compile; py_compile.compile('tui.py', doraise=True)"
```

## Architecture

### Data flow

```
网易云 API ←→ api.py (weapi.py encrypts weapi params)
                ↓
           cli.py (playback engine, state machine)
                ↓
    ┌──────────┼──────────┐
    ↓                      ↓
  tui.py               mpris.py
  (Curses TUI)         (D-Bus MPRIS2)
```

### Key modules

| File | Role |
|------|------|
| `api.py` | HTTP wrapper — session management, proxy detection (Clash Verge → UnblockNeteaseMusic → direct), cookie loading from browsers/`config.json`, all API calls |
| `weapi.py` | Two-layer AES-128-CBC + RSA-1024 encryption for endpoints under `/weapi/` (login, QR, comments, etc.) |
| `cli.py` | Playback engine: GStreamer pipeline (souphttpsrc → decodebin → audioconvert → audioresample → tee → audiosink + spectrum), pause via pad probe on tee audio branch, position tracking, shuffle/loop state machine, play context (ctx dict with songs/order/index/playable) |
| `tui.py` | Curses TUI: split-panel layout (left=tracks, right=now playing+spectrum+lyrics+crab mascot), Tokyo Night color scheme, popup modals (search, charts, playlists, QR login, lyrics) |
| `mpris.py` | Linux desktop media integration (optional, dbus-python required) |
| `state.py` | Playback resume: saves ctx metadata to `~/.cache/lumi-music/state.json` on track change, restores on launch via `api.song_detail` + `play_song` |
| `config.py` | Atomic JSON config at `~/.config/lumi-music/config.json` (tempfile+rename pattern, 0600 perms). Keys: `music_u`, `device_id`, `download_dir`, `auto_next`, `unblock`, `unblock_port` |
| `unblock.py` | Lifecycle manager for UnblockNeteaseMusic binary (find→Popen→port probe→set proxy) |
| `visualizer.py` | Audio spectrum extracted from GStreamer tee branch, rendered as colored bars in terminal |
| `downloader.py` | Multi-threaded MP3 downloader with ID3 tagging and LRC saving |

### Auth flow

1. **Cookie Jar** (`~/.config/lumi-music/cookies.json`) — stores all `163.com` cookies (MUSIC_U, __csrf, MUSIC_A, etc.), not just MUSIC_U. Imitates musicfox's persistent-cookiejar.
2. **Token refresh** — `refresh_token()` calls `/weapi/login/token/refresh` on startup when jar exists.
3. **Device ID** — auto-generated 16-char hex `sDeviceId`, stored in config, injected into non-login WeAPI payloads.
4. **Startup priority**: Cookie Jar → config `music_u` → Firefox auto-read → prompt.
5. **Login methods**: QR scan (`Shift+Q` in TUI / `:login` in CLI), cookie paste (`:cookie <val>`).

### GStreamer pipeline

```
souphttpsrc ─→ decodebin ─→ audioconvert ─→ audioresample ─→ tee
                     (pad-added signal for dynamic format detection)
                                                               ├── queue(200ms) → audioconvert → audioresample → autoaudiosink
                                                               └── queue(200ms) → spectrum → fakesink
```

**Pause mechanism**: Pad probe (`BLOCK_DOWNSTREAM`) on tee audio branch pad. The pipeline stays in `PLAYING` state — only the audio branch is blocked. This avoids `souphttpsrc` HTTP disconnection that would occur with `set_state(PAUSED)`. **Critical ordering**: `_pause_event.clear()` BEFORE `add_probe()`, otherwise the probe fires immediately and doesn't block.

### Env vars

- `LUMI_MUSIC_ROOT` — redirects config/cache directories (mirrors MUSICFOX_ROOT pattern)
- `HTTPS_PROXY` / `HTTP_PROXY` — highest priority proxy detection

### Login

QR scan (`Shift+Q` in TUI / `:login` in CLI) or cookie paste (`:cookie <val>`). See **Auth flow** above.

### Audio backends

Priority: GStreamer (souphttpsrc streaming) → pygame (download-then-play fallback). `_audio_backend` set to `"gst"` or `"pygame"` at module load.

### Curses TUI keys

```
ENTER 播放      SPC 暂停      n 下一首     b 上一首     +/- 快进/退
r 随机          c 循环        s 搜索       p 榜单       y 每日推荐
m 我的歌单      v 频谱        f 筛选       d 下载       l 歌词
Q/Shift+Q      扫码登录      q 退出
```
