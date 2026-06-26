#!/usr/bin/env python3
"""
网易云音乐 CLI 播放器
- 搜索 / 排行 / 歌单 / 歌词 / 每日推荐 / 我的歌单
- 需要登录 Cookie 才能播放 (从浏览器复制或配置文件)
"""
import sys
import os
import time
import re
import random
import tempfile
import threading
from collections import deque
from pathlib import Path

# 确保能找到 api 模块
sys.path.insert(0, str(Path(__file__).parent))

import api
from api import c
import config
import visualizer
import unblock
import state

try:
    import mpris
except ImportError:
    mpris = None

_cfg = config.load()
_show_viz = bool(_cfg.get("show_viz", False))
_auto_next = bool(_cfg.get("auto_next", True))
_default_br = int(_cfg.get("default_br", 320000))

TMP_DIR = Path(tempfile.gettempdir()) / "lumi-music"
TMP_DIR.mkdir(parents=True, exist_ok=True)

# 音频后端: 优先 GStreamer (流式), 回退 pygame
_audio_backend = None  # "gst" or "pygame"

try:
    import gi
    gi.require_version("Gst", "1.0")
    from gi.repository import Gst
    Gst.init(None)
    # 测试能否创建 playbin
    pipeline = Gst.ElementFactory.make("playbin", "test")
    if pipeline:
        _audio_backend = "gst"
        pipeline.set_state(Gst.State.NULL)
except Exception:
    pass

if _audio_backend != "gst":
    os.environ.setdefault("SDL_AUDIODRIVER", "pulseaudio")
    try:
        import pygame
        pygame.mixer.init(frequency=44100, size=-16, channels=2)
        _audio_backend = "pygame"
    except Exception:
        pass


def print_song(i, song, playable=None, highlight=False):
    ns = api.normalize_song(song)
    artists = api.format_artists(ns["artists"])
    album = ns["album"]["name"]
    dur = f"{ns['duration']//1000//60}:{ns['duration']//1000%60:02d}"
    # 播放状态指示
    if playable is True:
        status = c("▶", "green")
    elif playable is False:
        status = c("✗", "red")
    else:
        status = c("?", "dim")
    line = f" {status} {c(f'[{i:>2}]','green')} {c(ns['name'],'bold')}"
    line += f" — {c(artists,'yellow')}"
    if album:
        line += f" · {c(album,'dim')}"
    line += f"  {c(dur,'cyan')}"
    if highlight:
        line = f"\033[7m{line}\033[0m"
    print(line)


# ========== 播放控制 ==========
_playing = False
_paused = False           # 暂停状态
_thread = None
_shuffle = False          # 随机播放
_loop_mode = "off"        # "off" | "one" | "all"
_history = deque(maxlen=200)  # 播放历史 (capped at 200)
_gst_pipeline = None      # 当前 GStreamer 管道 (用于暂停/继续)
_tee_audio_pad = None     # tee 音频分支 src pad (pad probe 阻塞点)
_block_probe_id = None    # BLOCK_DOWNSTREAM 探针 ID
_pause_event = threading.Event()
_pause_event.set()        # 初始: 不阻塞
_play_ctx = None          # dict: {songs, index, playable, order} 或 None
_current_br = 0           # 当前播放歌曲码率 (供 TUI 读取)
_current_song_id = 0
_position_ms = 0
_duration_ms = 0
_position_lock = threading.Lock()
# GStreamer 管道全局状态的互斥锁 (_gst_pipeline, _tee_audio_pad, _block_probe_id)
_pipeline_lock = threading.Lock()
_pygame_start = 0.0       # pygame 播放起始 monotonic 时间
_now_playing = {"title": "", "artist": "", "album": ""}
_on_track_change = None   # callback(song_id, title, artist, album)

# 预编译频谱消息解析正则 (避免热路径重复编译)
_RE_MAGNITUDE = re.compile(r"magnitude=\(float\)\{([^}]+)\}")
_spectrum_regex_warned = False  # 首次匹配失败时是否已打印警告

# decodebin pad-added 回调: 连接音频 pad 到后续 pipeline
def _on_decodebin_pad(decodebin, pad, target):
    if pad.name.startswith("src_"):
        try:
            pad.link(target.get_static_pad("sink"))
        except Exception:
            pass

# TUI 模式标志: 为 True 时控制函数不 print (由 TUI 自行渲染状态)
_tui_mode = False


def _tui_print(*args, **kwargs):
    """print 包装: TUI 模式下静默, CLI 模式下正常输出"""
    if not _tui_mode:
        print(*args, **kwargs)


# 共享 LRC 歌词解析 (cli.py / tui.py 共用)
_LRC_TAG_RE = re.compile(r"\[(\d+):(\d+(?:\.\d+)?)\]")


def _lrc_time_to_ms(mins: str, secs: str) -> int:
    if "." in secs:
        s, cs = secs.split(".", 1)
        sec = int(s)
        frac = int(cs.ljust(2, "0")[:2])
    else:
        sec = int(secs)
        frac = 0
    return int(mins) * 60000 + sec * 1000 + frac * 10


def parse_lrc(lrc_text: str):
    """解析 LRC, 返回 [(time_ms, text), ...] 按时间排序"""
    lines = []
    for line in lrc_text.split("\n"):
        tags = list(_LRC_TAG_RE.finditer(line))
        if not tags:
            continue
        # 从匹配区间之间的文本提取歌词 (避免第二次 regex 扫描)
        text = "".join(
            line[tags[i].end():tags[i + 1].start()] if i + 1 < len(tags)
            else line[tags[i].end():]
            for i in range(len(tags))
        ).strip()
        if not text:
            continue
        for m in tags:
            lines.append((_lrc_time_to_ms(m.group(1), m.group(2)), text))
    lines.sort(key=lambda x: x[0])
    return lines


def format_time_ms(ms: int) -> str:
    ms = max(ms, 0)
    s = ms // 1000
    return f"{s // 60}:{s % 60:02d}"


def lrc_index_at_time(lines, pos_ms: int) -> int:
    """返回当前应高亮的歌词行索引"""
    if not lines:
        return 0
    idx = 0
    for i, (t, _) in enumerate(lines):
        if t <= pos_ms:
            idx = i
        else:
            break
    return idx


def _normalize_play_ctx(songs, song_idx, playable):
    """将歌曲列表索引转为播放上下文 (order 内索引)"""
    order = _build_order(songs, playable)
    order_idx = -1
    for oi, si in enumerate(order):
        if si == song_idx:
            order_idx = oi
            break
    if order_idx == -1:
        order_idx = 0  # fallback: song not in order, default to first
    return {"songs": songs, "index": order_idx, "playable": playable, "order": order}


def get_position_ms() -> int:
    with _position_lock:
        if _audio_backend == "pygame" and _playing and not _paused:
            try:
                if pygame.mixer.music.get_busy():
                    return int((time.monotonic() - _pygame_start) * 1000)
            except Exception:
                pass
        return _position_ms


def get_duration_ms() -> int:
    with _position_lock:
        return _duration_ms


def get_now_playing_title() -> str:
    return _now_playing.get("title", "")


def get_now_playing_artist() -> str:
    return _now_playing.get("artist", "")


def get_now_playing_album() -> str:
    return _now_playing.get("album", "")


def get_loop_mpris() -> str:
    return {"off": "None", "one": "Track", "all": "Playlist"}.get(_loop_mode, "None")


def can_seek() -> bool:
    return _audio_backend == "gst" and _playing


def seek_relative(delta_ms: int):
    """相对 seek (快进/快退)"""
    with _position_lock:
        new_ms = max(0, _position_ms + delta_ms)
    seek_to(new_ms)


def seek_to(ms: int):
    """绝对 seek (跳转)"""
    global _gst_pipeline
    with _pipeline_lock:
        if _audio_backend != "gst" or _gst_pipeline is None:
            return
    try:
        pos = max(0, ms) * 1_000_000
        _gst_pipeline.seek_simple(
            Gst.Format.TIME,
            Gst.SeekFlags.FLUSH | Gst.SeekFlags.KEY_UNIT,
            pos,
        )
        with _position_lock:
            _position_ms = ms
    except Exception:
        pass


def _update_position_from_pipeline():
    global _position_ms, _duration_ms
    with _pipeline_lock:
        if _audio_backend != "gst" or _gst_pipeline is None:
            return
    try:
        ok, pos = _gst_pipeline.query_position(Gst.Format.TIME)
        if ok:
            with _position_lock:
                _position_ms = pos // 1_000_000
        ok, dur = _gst_pipeline.query_duration(Gst.Format.TIME)
        if ok and dur > 0:
            with _position_lock:
                _duration_ms = dur // 1_000_000
    except Exception:
        pass


def _notify_track_change(song_id, title, song_obj=None):
    global _now_playing
    artist = ""
    album = ""
    if song_obj:
        ns = api.normalize_song(song_obj)
        artist = api.format_artists(ns["artists"])
        album = ns["album"]["name"]
    _now_playing = {"title": title, "artist": artist, "album": album}
    if _on_track_change:
        try:
            _on_track_change(song_id, title, artist, album)
        except Exception:
            pass
    if mpris:
        try:
            mpris.emit_properties_changed()
        except Exception:
            pass


def _build_order(songs, playable):
    """生成播放顺序列表 (受 shuffle 影响)"""
    n = len(songs)
    if _shuffle:
        order = list(range(n))
        random.shuffle(order)
        # 单次遍历区分可播/不可播
        playable_idx = []
        non_playable = []
        for si in order:
            if api.normalize_song(songs[si])["id"] in playable:
                playable_idx.append(si)
            else:
                non_playable.append(si)
        return playable_idx + non_playable
    return list(range(n))


def _get_next_index():
    """根据 loop_mode 计算下一首索引, 返回 (next_index, song_list_index) 或 None"""
    if not _play_ctx:
        return None
    idx = _play_ctx["index"]
    order = _play_ctx["order"]
    if _loop_mode == "one":
        return idx  # 重复当前
    nxt = idx + 1
    if nxt < len(order):
        return nxt
    if _loop_mode == "all":
        return 0  # 回绕
    return None  # 播完


def play_next():
    """切到下一首 (手动或自动)"""
    global _play_ctx
    if not _play_ctx:
        return
    songs = _play_ctx["songs"]
    order = _play_ctx["order"]
    playable = _play_ctx["playable"]
    tried = 0

    while tried < len(order):
        nxt = _get_next_index()
        if nxt is None:
            _tui_print(c("\n  ✓ 列表播放完毕", "dim"))
            _play_ctx = None
            state.clear()
            return
        _play_ctx["index"] = nxt
        song_idx = order[nxt]
        ns = api.normalize_song(songs[song_idx])
        tried += 1
        if ns["id"] in playable:
            break
    else:
        _tui_print(c("\n  ✗ 列表中无可播歌曲", "red"))
        _play_ctx = None
        state.clear()
        return

    _tui_print(c(f"\n  → {'🔀' if _shuffle else '▶'} 下一首 [{song_idx}]", "dim"))
    play_song(ns["id"], ns["name"], ctx=_play_ctx, song_obj=songs[song_idx])


def play_prev():
    """回到上一首"""
    global _play_ctx, _history
    if len(_history) < 2:
        _tui_print(c("  没有上一首", "dim"))
        return
    # pop 当前这首, 取再上一首
    _history.pop()  # 当前正在播的
    prev_id, prev_title, prev_ctx = _history.pop()
    _tui_print(c(f"\n  ← 上一首: {prev_title}", "dim"))
    play_song(prev_id, prev_title, ctx=prev_ctx)


def _pad_probe_cb(pad, info, *_unused):
    """tee 播放分支 pad 探针回调: 阻塞直到 _pause_event 被 set"""
    _pause_event.wait()
    return Gst.PadProbeReturn.OK


def _install_pause_probe():
    """安装暂停探针: 阻塞播放分支的数据流.

    ⚠ 关键: 必须先 clear event, 再装探针. 否则探针立即触发时 event 仍为 set,
    回调返回 OK 而不阻塞, 导致暂停无效.
    """
    global _block_probe_id
    _pause_event.clear()  # ← 必须在 add_probe 之前
    with _pipeline_lock:
        if _tee_audio_pad is not None and _block_probe_id is None:
            _block_probe_id = _tee_audio_pad.add_probe(
                Gst.PadProbeType.BLOCK_DOWNSTREAM,
                _pad_probe_cb, None)


def _remove_pause_probe():
    """移除暂停探针: 先唤醒事件, 再移除探针"""
    global _block_probe_id
    _pause_event.set()
    with _pipeline_lock:
        if _block_probe_id is not None and _tee_audio_pad is not None:
            try:
                _tee_audio_pad.remove_probe(_block_probe_id)
            except Exception:
                pass
        _block_probe_id = None


def toggle_pause():
    """暂停 / 继续 — pad probe 阻塞播放分支, 管道保持 PLAYING"""
    global _paused
    if not _playing:
        _tui_print(c("  当前无播放", "dim"))
        return
    _paused = not _paused
    if _audio_backend == "gst":
        try:
            if _paused:
                _install_pause_probe()
                visualizer._drain_queue()
            else:
                _remove_pause_probe()
        except Exception:
            pass
    elif _audio_backend == "pygame":
        try:
            if _paused:
                pygame.mixer.music.pause()
            else:
                pygame.mixer.music.unpause()
        except Exception:
            pass
    _tui_print(c(f"  {'⏸ 暂停' if _paused else '▶ 继续'}", "yellow"))


def toggle_shuffle():
    """切换随机播放"""
    global _shuffle
    _shuffle = not _shuffle
    if _play_ctx:
        old_song_idx = _play_ctx["order"][_play_ctx["index"]]
        _play_ctx["order"] = _build_order(_play_ctx["songs"], _play_ctx["playable"])
        for oi, si in enumerate(_play_ctx["order"]):
            if si == old_song_idx:
                _play_ctx["index"] = oi
                break
        _tui_print(c(f"  🔀 随机: {'✓ 开' if _shuffle else '✗ 关'}", "yellow"))
    state.save()


def toggle_loop():
    """循环切换 off → one → all → off"""
    global _loop_mode
    modes = {"off": "one", "one": "all", "all": "off"}
    _loop_mode = modes[_loop_mode]
    labels = {"off": "✗ 关", "one": "🔂 单曲", "all": "🔁 列表"}
    _tui_print(c(f"  循环: {labels[_loop_mode]}", "yellow"))
    state.save()


def _auto_play_next():
    """自动播放下一首 (歌曲结束时的回调, 尊重 auto_next)"""
    global _play_ctx
    if not _auto_next or not _play_ctx:
        return
    if _loop_mode == "one":
        # 单曲循环直接重播当前
        songs = _play_ctx["songs"]
        order = _play_ctx["order"]
        idx = _play_ctx["index"]
        song_idx = order[idx]
        ns = api.normalize_song(songs[song_idx])
        _tui_print(c(f"\n  🔂 单曲循环 [{song_idx}]", "dim"))
        play_song(ns["id"], ns["name"], ctx=_play_ctx, song_obj=songs[song_idx])
        return
    play_next()


def _gst_play_with_viz(url, title):
    """GStreamer 流式播放 + 频谱可视化 — souphttpsrc 显式管道, 无 uridecodebin"""
    global _playing, _paused, _gst_pipeline, _block_probe_id, _tee_audio_pad
    pipeline = None
    naturally_ended = False
    try:
        pipeline = Gst.Pipeline.new("player")

        # HTTP 源 (避免 uridecodebin 的状态切换 bug)
        src = Gst.ElementFactory.make("souphttpsrc", "src")
        if not src: raise RuntimeError("创建 souphttpsrc 失败 (需要 gst-plugins-bad)")
        src.set_property("location", url)
        src.set_property("user-agent", api.HEADERS["User-Agent"])
        _unblock_proxy = unblock.proxy_url()
        if _unblock_proxy:
            src.set_property("proxy", _unblock_proxy)

        # decodebin 自动检测音频格式 (MP3/AAC/FLAC/OGG)
        decodebin = Gst.ElementFactory.make("decodebin", "decodebin")
        if not decodebin: raise RuntimeError("创建 decodebin 失败 (需要 gst-plugins-base)")
        dec_conv = Gst.ElementFactory.make("audioconvert", "dec_conv")
        if not dec_conv: raise RuntimeError("创建 audioconvert 失败")
        dec_resample = Gst.ElementFactory.make("audioresample", "dec_resample")
        if not dec_resample: raise RuntimeError("创建 audioresample 失败")

        # tee 分流
        tee = Gst.ElementFactory.make("tee", "tee")
        if not tee: raise RuntimeError("创建 tee 失败")

        # 播放分支 (限制缓冲降低暂停延迟, 200ms 平衡延迟与稳定性)
        queue_play = Gst.ElementFactory.make("queue", "queue_play")
        if not queue_play: raise RuntimeError("创建 queue 失败")
        queue_play.set_property("max-size-time", 200_000_000)  # 200ms
        conv_play = Gst.ElementFactory.make("audioconvert", "conv_play")
        if not conv_play: raise RuntimeError("创建 audioconvert 失败")
        resample_play = Gst.ElementFactory.make("audioresample", "resample_play")
        if not resample_play: raise RuntimeError("创建 audioresample 失败")
        audiosink = Gst.ElementFactory.make("autoaudiosink", "audiosink")
        if not audiosink: raise RuntimeError("创建 autoaudiosink 失败 (需要 gst-plugins-good)")

        # 频谱分支 (同样限制缓冲)
        SPECTRUM_BANDS = 32
        queue_spec = Gst.ElementFactory.make("queue", "queue_spec")
        if not queue_spec: raise RuntimeError("创建 queue 失败")
        queue_spec.set_property("max-size-time", 200_000_000)  # 200ms
        spec = Gst.ElementFactory.make("spectrum", "spec")
        if not spec: raise RuntimeError("创建 spectrum 失败 (需要 gst-plugins-bad)")
        spec.set_property("bands", SPECTRUM_BANDS)
        spec.set_property("threshold", -70)
        spec.set_property("interval", 60_000_000)  # 60ms
        spec.set_property("post-messages", True)
        fakesink = Gst.ElementFactory.make("fakesink", "fakesink")
        if not fakesink: raise RuntimeError("创建 fakesink 失败")

        for elem in [src, decodebin, dec_conv, dec_resample, tee,
                     queue_play, conv_play, resample_play, audiosink,
                     queue_spec, spec, fakesink]:
            pipeline.add(elem)

        # 静态链接: souphttpsrc → decodebin → audioconvert → audioresample → tee
        src.link(decodebin)
        # decodebin 是动态 pad, 连接其 src pad 到 conv1
        decodebin.connect("pad-added", _on_decodebin_pad, dec_conv)
        dec_conv.link(dec_resample)
        dec_resample.link(tee)

        # 播放分支: tee → queue → audioconvert → audioresample → autoaudiosink
        tee_pad = tee.get_request_pad("src_%u")
        tee_pad.link(queue_play.get_static_pad("sink"))
        _tee_audio_pad = tee_pad  # 保存引用, 用于暂停探针
        queue_play.link(conv_play)
        conv_play.link(resample_play)
        resample_play.link(audiosink)

        # 频谱分支: tee → queue → spectrum → fakesink
        tee_pad2 = tee.get_request_pad("src_%u")
        tee_pad2.link(queue_spec.get_static_pad("sink"))
        queue_spec.link(spec)
        spec.link(fakesink)

        bus = pipeline.get_bus()
        with _pipeline_lock:
            _gst_pipeline = pipeline
            _tee_audio_pad = tee_pad
        pipeline.set_state(Gst.State.PLAYING)
        # 如果切歌前已暂停, 补装探针
        if _paused:
            _install_pause_probe()
            visualizer._drain_queue()
        # 重置正则匹配警告, 切歌后如果格式变化仍需检测
        global _spectrum_regex_warned
        _spectrum_regex_warned = False

        visualizer.start()

        while _playing:
            _update_position_from_pipeline()
            msg = bus.timed_pop_filtered(
                60_000_000,  # 60ms, 与 spectrum interval 一致
                Gst.MessageType.ERROR | Gst.MessageType.EOS | Gst.MessageType.ELEMENT,
            )
            if not msg:
                continue

            t = msg.type
            if t == Gst.MessageType.ERROR:
                err, dbg = msg.parse_error()
                _tui_print(f"\r  {c(f'✗ 播放错误: {err.message}', 'red')}    ")
                break
            elif t == Gst.MessageType.EOS:
                naturally_ended = True
                break
            elif t == Gst.MessageType.ELEMENT:
                if _show_viz:
                    s = msg.get_structure()
                    if s and s.get_name() == "spectrum":
                        raw = s.to_string()
                        m = _RE_MAGNITUDE.search(raw)
                        if m:
                            vals = [float(x.strip()) for x in m.group(1).split(",")]
                            visualizer.push_spectrum(vals[:SPECTRUM_BANDS])
                        elif not _spectrum_regex_warned:
                            _spectrum_regex_warned = True
                            _tui_print(c("  ⚠ 频谱消息格式不匹配, 可视化可能异常", "yellow"))

    except Exception as e:
        _tui_print(f"\r  {c(f'✗ 播放失败: {e}', 'red')}    ")
    finally:
        _playing = False
        _paused = False
        with _pipeline_lock:
            _pause_event.set()
            if _block_probe_id is not None and _tee_audio_pad is not None:
                try:
                    _tee_audio_pad.remove_probe(_block_probe_id)
                except Exception:
                    pass
            _block_probe_id = None
            _gst_pipeline = None
            _tee_audio_pad = None
        if pipeline is not None:
            try:
                pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
        visualizer.stop()
        if naturally_ended:
            _auto_play_next()


def _pygame_download_play(url, title):
    """pygame 回退方案: 下载后播放"""
    global _playing, _pygame_start, _position_ms, _duration_ms
    import requests as req
    import hashlib
    naturally_ended = False
    fpath = TMP_DIR / f"play_{hashlib.md5(url.encode()).hexdigest()[:10]}.mp3"
    try:
        resp = req.get(url, headers=api.HEADERS, timeout=60, stream=True)
        resp.raise_for_status()
        total = int(resp.headers.get("content-length", 0))
        with _position_lock:
            _duration_ms = 0  # pygame 难以获取精确时长
        downloaded = 0
        last_pct = -1
        with open(fpath, "wb") as f:
            for chunk in resp.iter_content(32768):
                if not _playing:
                    return
                f.write(chunk)
                downloaded += len(chunk)
                if total > 0:
                    pct = downloaded * 100 // total
                    if pct != last_pct:
                        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
                        _tui_print(f"\r  ⏳ 缓冲 [{bar}] {pct}%", end="", flush=True)
                        last_pct = pct
        if not _playing:
            return
        _tui_print(f"\r  ▶ {c(title, 'bold')} 播放中...    ", flush=True)
        pygame.mixer.music.load(str(fpath))
        pygame.mixer.music.play()
        _pygame_start = time.monotonic()
        with _position_lock:
            _position_ms = 0
        while pygame.mixer.music.get_busy() and _playing:
            with _position_lock:
                if not _paused:
                    _position_ms = int((time.monotonic() - _pygame_start) * 1000)
            time.sleep(0.2)
        if _playing:
            naturally_ended = True
    except Exception as e:
        _tui_print(f"\r  {c(f'✗ 播放失败: {e}', 'red')}    ")
    finally:
        _playing = False
        if fpath.exists():
            try:
                fpath.unlink()
            except Exception:
                pass
        if naturally_ended:
            _auto_play_next()


def _stream_play(url, title):
    """根据可用后端选择播放方式"""
    if _audio_backend == "gst":
        # 始终使用带频谱的管道, _show_viz 只控制 TUI 是否渲染
        _gst_play_with_viz(url, title)
    else:
        _pygame_download_play(url, title)


def play_song(song_id, title="", ctx=None, song_obj=None):
    """
    播放歌曲
    ctx: 播放上下文 dict 或 tuple (songs, song_list_index, playable)
    """
    global _playing, _thread, _play_ctx, _history, _paused, _current_br
    global _current_song_id, _position_ms, _duration_ms
    url = None
    br_val = 0
    url, br_val = api.resolve_song_url(song_id, _default_br)
    if not url:
        _tui_print(c("  ✗ 该歌曲暂无播放链接", "red"))
        return False
    _current_br = br_val
    _current_song_id = song_id
    with _position_lock:
        _position_ms = 0
        _duration_ms = 0
    stop()
    _playing = True
    if isinstance(ctx, tuple):
        songs, song_idx, pl = ctx
        ctx = _normalize_play_ctx(songs, song_idx, pl)
    _play_ctx = ctx
    if song_obj is None and ctx:
        try:
            song_obj = ctx["songs"][ctx["order"][ctx["index"]]]
        except (KeyError, IndexError, TypeError):
            song_obj = None
    _notify_track_change(song_id, title, song_obj)
    _history.append((song_id, title, ctx))
    _tui_print(c(f"\n  ▶ {title} [{br_val//1000}kbps]", "green"))
    _thread = threading.Thread(target=_stream_play, args=(url, title), daemon=True)
    _thread.start()
    # 每次切歌保存状态用于恢复
    try:
        state.save()
    except Exception as e:
        _tui_print(c(f"  ⚠ 保存播放状态失败: {e}", "yellow"))
    return True


def stop():
    global _playing, _paused, _gst_pipeline, _tee_audio_pad, _thread, _block_probe_id
    _playing = False
    _paused = False
    with _pipeline_lock:
        _pause_event.set()
        if _block_probe_id is not None and _tee_audio_pad is not None:
            try:
                _tee_audio_pad.remove_probe(_block_probe_id)
            except Exception:
                pass
        _block_probe_id = None
        if _gst_pipeline is not None:
            try:
                _gst_pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
        _gst_pipeline = None
        _tee_audio_pad = None
    visualizer.stop()
    if _audio_backend == "pygame":
        try:
            pygame.mixer.music.stop()
        except Exception:
            pass
    # 等待播放线程退出 (避免创建新管道时资源冲突)
    # 注意: 自动播放下一首时 stop() 在播放线程自身内调用, 不能 join 自己
    if _thread is not None and _thread.is_alive() and threading.current_thread() is not _thread:
        _thread.join(timeout=1.0)  # 延长超时, 给 GStreamer 更充分的管道清理时间
        if _thread.is_alive():
            # 如果仍未退出, 清空引用避免下次 play_song 发现旧管道残留
            _thread = None


# ========== 歌词显示 ==========
def show_lyric(song_id, title="", sync=False):
    r = api.lyric(song_id)
    lrc = r.get("lrc", {}).get("lyric", "")
    tlyric = r.get("tlyric", {}).get("lyric", "")
    if not lrc:
        print(c("  暂无歌词", "dim"))
        return []
    print(c(f"\n  🎵 {title}", "bold"))
    tl_map = {}
    if tlyric:
        for line in tlyric.split("\n"):
            for m in _LRC_TAG_RE.finditer(line):
                key = _lrc_time_to_ms(m.group(1), m.group(2))
                text = _LRC_TAG_RE.sub("", line).strip()
                if text:
                    tl_map[key] = text
                break
    lines = parse_lrc(lrc)
    if sync and _playing and song_id == _current_song_id:
        cur = lrc_index_at_time(lines, get_position_ms())
        for i, (t_ms, text) in enumerate(lines):
            ts = format_time_ms(t_ms)
            mark = "▶" if i == cur else " "
            disp = f" {mark} [{c(ts,'cyan')}] {text}"
            if t_ms in tl_map:
                disp += f"\n       {c(tl_map[t_ms],'dim')}"
            print(disp)
    else:
        for t_ms, text in lines:
            ts = format_time_ms(t_ms)
            disp = f"  [{c(ts,'cyan')}] {text}"
            if t_ms in tl_map:
                disp += f"\n       {c(tl_map[t_ms],'dim')}"
            print(disp)
    return lines


# ========== 交互逻辑 ==========

def _run_song_list(songs, label=""):
    """通用歌曲列表交互循环"""
    if not songs:
        print(c("  ✗ 列表为空", "red"))
        return
    print(c("  🔗 检测播放状态...", "dim"))
    ids = [api.normalize_song(s)["id"] for s in songs]
    playable = api.check_playable(ids)
    print(c(f"     可播: {len(playable)}/{len(songs)} 首", "dim"))
    if label:
        print(c(f"\n  📋 {label}", "bold"))

    show_all = True

    def _display():
        nonlocal show_all
        count = 0
        for i, s in enumerate(songs):
            ns = api.normalize_song(s)
            is_playable = ns["id"] in playable
            if not show_all and not is_playable:
                continue
            print_song(i, s, playable=is_playable)
            count += 1
        return count

    count = _display()
    print(c(f"\n  {'📋 全部' if show_all else '🎧 仅可播'} {count} 首", "bold"))

    while True:
        hint = (
            "序号 | L歌词 | d下载 | f过滤 | a自动 | 空格暂停 | "
            "n下一 b上一 | r随机 c循环 | v频谱 | +10/-10 seek | q返回"
        )
        cmd = input(c(f"\n  ▶ {hint}: ", "bold")).strip()
        if cmd == "q":
            break
        if cmd == "f":
            show_all = not show_all
            count = _display()
            print(c(f"\n  {'📋 全部' if show_all else '🎧 仅可播'} {count} 首", "bold"))
            continue
        if cmd == "a":
            global _auto_next
            _auto_next = not _auto_next
            config.set_key("auto_next", _auto_next)
            print(c(f"  自动下一首: {'✓ 开' if _auto_next else '✗ 关'}", "yellow"))
            continue
        if cmd == "v":
            global _show_viz
            _show_viz = not _show_viz
            config.set_key("show_viz", _show_viz)
            print(c(f"  频谱: {'✓ 开' if _show_viz else '✗ 关'}", "yellow"))
            continue
        if cmd in ("", " "):
            toggle_pause()
            continue
        if cmd == "n":
            play_next()
            continue
        if cmd == "b":
            play_prev()
            continue
        if cmd == "r":
            toggle_shuffle()
            continue
        if cmd == "c":
            toggle_loop()
            continue
        if cmd in ("+", "++", "+10"):
            seek_relative(10000)
            print(c(f"  ⏩ {format_time_ms(get_position_ms())}", "cyan"))
            continue
        if cmd in ("-", "--", "-10"):
            seek_relative(-10000)
            print(c(f"  ⏪ {format_time_ms(get_position_ms())}", "cyan"))
            continue
        if cmd.lower().startswith("l"):
            try:
                idx = int(cmd[1:])
                ns = api.normalize_song(songs[idx])
                show_lyric(ns["id"], ns["name"], sync=(ns["id"] == _current_song_id))
            except (ValueError, IndexError):
                print(c("  无效序号", "red"))
        elif cmd.lower().startswith("d"):
            try:
                idx = int(cmd[1:])
                ns = api.normalize_song(songs[idx])
                artist = ", ".join(a["name"] for a in ns["artists"])
                from downloader import download_song
                download_song(ns["id"], ns["name"], artist, song_obj=songs[idx], idx=1, total=1)
            except (ValueError, IndexError):
                print(c("  无效序号", "red"))
        else:
            try:
                idx = int(cmd)
                ns = api.normalize_song(songs[idx])
                if ns["id"] not in playable:
                    print(c("  ✗ 该歌曲不可播，请尝试其他歌曲", "red"))
                    continue
                play_song(ns["id"], ns["name"], ctx=(songs, idx, playable), song_obj=songs[idx])
            except (ValueError, IndexError):
                print(c("  无效序号", "red"))


def do_search():
    kw = input(c("  搜索: ", "bold")).strip()
    if not kw:
        return
    print(c("  类型: [1]单曲 [10]专辑 [100]歌手 [1000]歌单 (回车=单曲)", "dim"))
    type_in = input(c("  类型: ", "bold")).strip()
    stype = int(type_in) if type_in.isdigit() else 1

    print(c("  🔍 搜索中...", "dim"))
    r = api.search(kw, stype=stype, limit=30)
    if r.get("code") != 200:
        print(c(f"  ✗ 搜索失败 (code={r.get('code')})", "red"))
        return

    result = r.get("result", {})
    if stype == 1:
        songs = result.get("songs", [])
        if not songs:
            print(c("  ✗ 无结果", "red"))
            return
        _run_song_list(songs, f"搜索: {kw}")
        return

    if stype == 1000:
        playlists = result.get("playlists", [])
        if not playlists:
            print(c("  ✗ 无歌单", "red"))
            return
        for i, pl in enumerate(playlists[:20]):
            print(f" {c(f'[{i:>2}]','green')} {c(pl.get('name',''),'bold')}  "
                  f"{c(str(pl.get('trackCount',0))+'首','dim')}")
        cmd = input(c("\n  ▶ 序号进入歌单 | q返回: ", "bold")).strip()
        if cmd == "q":
            return
        try:
            idx = int(cmd)
            pl = playlists[idx]
            pid = pl["id"]
            result = api.playlist_detail_all(pid)
            _run_song_list(result["tracks"], result["name"])
        except (ValueError, IndexError):
            print(c("  无效序号", "red"))
        return

    if stype == 100:
        artists = result.get("artists", [])
        if not artists:
            print(c("  ✗ 无歌手", "red"))
            return
        for i, ar in enumerate(artists[:20]):
            print(f" {c(f'[{i:>2}]','green')} {c(ar.get('name',''),'bold')}")
        cmd = input(c("\n  ▶ 序号查看热门 | q返回: ", "bold")).strip()
        if cmd == "q":
            return
        try:
            idx = int(cmd)
            ar = artists[idx]
            r2 = api.artist_top_songs(ar["id"], limit=50)
            songs = r2.get("songs", [])
            _run_song_list(songs, ar.get("name", ""))
        except (ValueError, IndexError):
            print(c("  无效序号", "red"))
        return

    if stype == 10:
        albums = result.get("albums", [])
        if not albums:
            print(c("  ✗ 无专辑", "red"))
            return
        for i, al in enumerate(albums[:20]):
            artist = ", ".join(a.get("name", "") for a in al.get("artists", []))
            print(f" {c(f'[{i:>2}]','green')} {c(al.get('name',''),'bold')}  {c(artist,'dim')}")
        cmd = input(c("\n  ▶ 序号播放专辑 | q返回: ", "bold")).strip()
        if cmd == "q":
            return
        try:
            idx = int(cmd)
            al = albums[idx]
            r2 = api.album_detail(al["id"])
            songs = r2.get("songs", [])
            _run_song_list(songs, al.get("name", ""))
        except (ValueError, IndexError):
            print(c("  无效序号", "red"))
        return

    print(c("  ✗ 无结果", "red"))


def do_daily():
    print(c("  ☀ 获取每日推荐...", "dim"))
    r = api.daily_recommend()
    if r.get("code") != 200:
        print(c(f"  ✗ 需要登录 (code={r.get('code')})", "red"))
        return
    songs = r.get("data", {}).get("dailySongs", [])
    if not songs:
        songs = r.get("recommend", [])
    _run_song_list(songs, "每日推荐")


def do_mine():
    uid = api.get_login_uid()
    if not uid:
        print(c("  ✗ 请先登录 (:cookie)", "red"))
        return
    print(c("  📂 获取我的歌单...", "dim"))
    r = api.user_playlist(uid, limit=50)
    playlists = r.get("playlist", [])
    if not playlists:
        print(c("  ✗ 无歌单", "red"))
        return
    for i, pl in enumerate(playlists):
        print(f" {c(f'[{i:>2}]','green')} {c(pl.get('name',''),'bold')}  "
              f"{c(str(pl.get('trackCount',0))+'首','dim')}")
    while True:
        cmd = input(c("\n  ▶ 序号进入歌单 | q返回: ", "bold")).strip()
        if cmd == "q":
            break
        try:
            idx = int(cmd)
            pl = playlists[idx]
            pid = pl["id"]
            result = api.playlist_detail_all(pid)
            _run_song_list(result["tracks"], result["name"])
        except (ValueError, IndexError):
            print(c("  无效序号", "red"))


def do_playlist():
    print(c("  📋 获取排行榜...", "dim"))
    r = api.top_list()
    lists = r.get("list", [])
    if not lists:
        print(c("  ✗ 获取失败", "red"))
        return
    print(c("\n  🏆 排行榜:", "bold"))
    for i, pl in enumerate(lists[:25]):
        name = pl.get("name", "")
        desc = pl.get("description", "") or pl.get("updateFrequency", "") or ""
        print(f" {c(f'[{i:>2}]','green')} {c(name,'bold')}  {c(desc,'dim')}")

    while True:
        cmd = input(c("\n  ▶ 序号进入歌单 | q返回: ", "bold")).strip()
        if cmd == "q":
            break
        try:
            idx = int(cmd)
            pid = lists[idx]["id"]
            pname = lists[idx]["name"]
            print(c(f"\n  📋 {pname} 加载中...", "dim"))
            result = api.playlist_detail_all(pid)
            _run_song_list(result["tracks"], result["name"])
        except (ValueError, IndexError):
            print(c("  无效序号", "red"))


def do_cookie():
    """设置登录 Cookie"""
    print(c("\n  🔑 设置登录 Cookie", "bold"))
    print(c("  从浏览器获取 MUSIC_U 值:", "dim"))
    print(c("  1. 打开 https://music.163.com 并登录", "dim"))
    print(c("  2. F12 → Application → Cookies → music.163.com", "dim"))
    print(c("  3. 复制 MUSIC_U 的值", "dim"))
    print()
    val = input(c("  MUSIC_U 值: ", "bold")).strip()
    if val:
        ok, msg = api.set_cookie(f"MUSIC_U={val}")
        if ok:
            config.set_key("music_u", val)
            api.save_cookie_jar()
            print(c(f"  ✓ {msg} (已保存 Cookie Jar 和配置)", "green"))
        else:
            print(c(f"  ✗ {msg}", "red"))


def do_qrcode_login():
    """扫码登录: 生成二维码 → 等待扫码 → 完成 → 保存 Cookie Jar"""
    key = api.qrcode_unikey()
    if not key:
        print(c("  ✗ 获取二维码失败", "red"))
        return
    url = f"https://music.163.com/login?codekey={key}"
    try:
        import qrcode
        # 生成二维码
        qr = qrcode.QRCode(border=2, box_size=1)
        qr.add_data(url)
        qr.make()
        print(c("\n  📱 请使用网易云音乐 APP 扫码登录", "bold"))
        print()
        qr.print_ascii(invert=True)
    except TypeError:
        # 部分旧版本 qrcode 不支持 invert
        try:
            qr_old = qrcode.QRCode(border=2)
            qr_old.add_data(url)
            qr_old.make()
            qr_old.print_ascii()
        except Exception:
            print(c(f"  {url}", "dim"))
    except ImportError:
        print(c("  ⚠ 请安装 qrcode 依赖: pip install 'qrcode[pil]'", "yellow"))
        print(c(f"  或者直接打开链接扫码: {url}", "dim"))
        return

    print()
    print(c("  ⏳ 等待扫码... (最长120秒)", "dim"))
    waited = 0
    while waited < 120:
        r = api.qrcode_login_check(key)
        code = r.get("code", 0)
        if code == 803:
            # 登录成功, code 为 803 时可能直接返回 Cookie
            print(c("  ✓ 扫码登录成功!", "green"))
            # 保存 Cookie Jar + config
            api.save_cookie_jar()
            music_u = None
            for cookie in api.get_session().cookies:
                if cookie.name == "MUSIC_U" and cookie.value:
                    music_u = cookie.value
                    break
            if music_u:
                config.set_key("music_u", music_u)
                print(c("  ✓ Cookie 已保存到配置", "green"))
            return
        elif code == 800:
            print(c("  ✗ 二维码已过期，请重试", "red"))
            return
        elif code == 802:
            # 已扫码待确认
            nick = r.get("nickname", "")
            print(f"\r  {c('✓','green')} 已扫码 ({nick}), 请在手机上确认...", end="", flush=True)
        elif code == 801:
            dots = "." * (waited % 4)
            print(f"\r  {c('⏳','yellow')} 等待扫码{dots:<4}", end="", flush=True)
        waited += 2
        time.sleep(2)
    print(c("\n  ✗ 扫码超时", "red"))


def _init_auth():
    """加载登录态: Cookie Jar → config music_u → 浏览器 → token 刷新"""
    # 1. 优先从 Cookie Jar 恢复 (含 __csrf, MUSIC_A 等完整会话)
    if api.load_cookie_jar():
        # 刷新 token 延长有效期
        try:
            if api.refresh_token():
                print(c("  ✓ Cookie Jar 已恢复并刷新", "green"))
            else:
                print(c("  ✓ Cookie Jar 已恢复", "green"))
        except Exception:
            print(c("  ✓ Cookie Jar 已恢复", "green"))
        return True
    # 2. 回退: 从配置的 music_u 加载
    music_u = _cfg.get("music_u", "")
    if music_u:
        ok, msg = api.set_cookie(f"MUSIC_U={music_u}")
        if ok:
            api.save_cookie_jar()
            print(c("  ✓ 已从配置文件加载登录态", "green"))
            return True
    # 3. 最后尝试: 浏览器自动读取
    if api.auto_load_browser_cookie():
        api.save_cookie_jar()
        print(c("  ✓ 已自动加载浏览器登录态", "green"))
        return True
    print(c("  ⓘ 未检测到登录态, VIP歌曲可能无法播放", "dim"))
    print(c("  提示: 浏览器登录后自动读取, 或用 :cookie 手动设置", "dim"))
    return False


# ========== 主入口 ==========
def main():
    global _show_viz
    print()
    print(c("  ╔══════════════════════════════╗", "magenta"))
    print(c("  ║   ♫ Lumi Music Studio ♫    ║", "magenta"))
    print(c("  ║   网易云音乐终端播放器       ║", "magenta"))
    print(c("  ╚══════════════════════════════╝", "magenta"))

    if not _audio_backend:
        print(c("  ⚠ 无可用音频后端", "yellow"))
    else:
        label = "GStreamer 流式" if _audio_backend == "gst" else "pygame 下载式"
        print(c(f"  🎧 音频: {label}", "dim"))

    _init_auth()

    # 启动 UnblockNeteaseMusic (必须在 restore 之前, 否则恢复地理锁歌曲会失败)
    if unblock.start():
        print(c(f"  ✓ UnblockNeteaseMusic (127.0.0.1:{_cfg.get('unblock_port', 5200)})", "dim"))

    # 尝试恢复上次播放
    restored = state.try_restore()
    if restored:
        print(c("  ✓ 已恢复上次播放", "dim"))

    if mpris and _audio_backend == "gst":
        if mpris.start(sys.modules[__name__]):
            print(c("  ✓ MPRIS 媒体控制已启用", "dim"))

    while True:
        status = ""
        if _playing:
            pos = format_time_ms(get_position_ms())
            dur = format_time_ms(get_duration_ms())
            prog = f" {pos}/{dur} |" if get_duration_ms() > 0 else " "
            if _paused:
                status = c(f" ⏸ 暂停{prog}", "yellow")
            else:
                status = c(f" ♫ 播放{prog}", "green")
            if _now_playing.get("title"):
                status += c(f" {_now_playing['title'][:20]} |", "bold")
        if _shuffle:
            status += "🔀 "
        if _loop_mode == "one":
            status += "🔂 "
        elif _loop_mode == "all":
            status += "🔁 "
        if _show_viz:
            status += c("频谱 |", "magenta")
        print()
        hint = (
            f"{c('[S]','green')}搜 {c('[P]','green')}榜 {c('[Y]','green')}荐 "
            f"{c('[M]','green')}我的 "
        )
        if _playing:
            hint += (
                f"{c('[空格]','yellow')}暂停 {c('[N]','yellow')}下 "
                f"{c('[B]','yellow')}上 {c('[+]','yellow')}{c('[-]','yellow')}seek "
            )
        hint += (
            f"{c('[R]','cyan')}随机 {c('[C]','cyan')}循环 "
            f"{c('[V]','magenta')}频谱 {c('[.]','red')}停 {c('[Q]','green')}退"
        )
        print(f"{status}{hint}")
        cmd = input(c(" ▶ ", "bold")).strip().lower()
        if cmd in ("q", "quit", "exit"):
            state.save()
            stop()
            if mpris:
                mpris.stop()
            unblock.stop()
            print(c("  拜拜~", "dim"))
            break
        elif cmd in ("s", "search"):
            do_search()
        elif cmd in ("p", "playlist", "top", "chart"):
            do_playlist()
        elif cmd in ("y", "daily"):
            do_daily()
        elif cmd in ("m", "mine", "my"):
            do_mine()
        elif cmd in ("", " "):
            toggle_pause()
        elif cmd == "n":
            play_next()
        elif cmd == "b":
            play_prev()
        elif cmd in ("+", "++"):
            seek_relative(10000)
        elif cmd in ("-", "--"):
            seek_relative(-10000)
        elif cmd == "r":
            toggle_shuffle()
        elif cmd == "c":
            toggle_loop()
        elif cmd == "v":
            _show_viz = not _show_viz
            config.set_key("show_viz", _show_viz)
            print(c(f"  频谱: {'✓ 开' if _show_viz else '✗ 关'}", "yellow"))
        elif cmd in (".", "stop"):
            stop()
            print(c("  ⏸ 已停止", "yellow"))
        elif cmd.startswith(":cookie"):
            val = cmd.split(":cookie", 1)[-1].strip()
            if val:
                ok, msg = api.set_cookie(f"MUSIC_U={val}")
                if ok:
                    config.set_key("music_u", val)
                    api.save_cookie_jar()
                print(c(f"  {'✓' if ok else '✗'} {msg}", "green" if ok else "red"))
            else:
                do_cookie()
        elif cmd.startswith(":login"):
            do_qrcode_login()
        elif cmd.startswith(":unblock"):
            _cfg2 = config.load()
            current = bool(_cfg2.get("unblock", True))
            config.set_key("unblock", not current)
            if not current:
                unblock.start()
                print(c("  ✓ UnblockNeteaseMusic 已启用", "green"))
            else:
                unblock.stop()
                print(c("  ✗ UnblockNeteaseMusic 已停用", "yellow"))
        elif cmd in ("h", "help", "?"):
            print(c("""
  命令:
    s / search     综合搜索 (单曲/专辑/歌手/歌单)
    p / chart      浏览排行榜 & 歌单
    y / daily      每日推荐 (需登录)
    m / mine       我的歌单 (需登录)
    空格            暂停 / 继续
    n / b          下一首 / 上一首
    + / -          快进/快退 10 秒 (GStreamer)
    r / c          随机 / 循环 (off→one→all)
    . / stop       停止播放
    v              频谱开关
    :cookie <值>   设置并保存 Cookie
    :login         扫码登录
    :unblock       切换 UnblockNeteaseMusic
    q / exit       退出
  配置: ~/.config/lumi-music/config.json
            """, "dim"))
        else:
            print(c("  未知命令, h 查看帮助", "red"))


if __name__ == "__main__":
    main()
