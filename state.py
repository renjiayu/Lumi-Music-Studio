"""
播放状态持久化 (断点续传)

退出时保存: 播放队列、当前索引、播放位置、循环/随机模式
启动时恢复: 继续播放（可选）
"""
import json
import os
from pathlib import Path

# 支持 LUMI_MUSIC_ROOT 环境变量 (类似 MUSICFOX_ROOT)
_ROOT = os.environ.get("LUMI_MUSIC_ROOT")
if _ROOT:
    CACHE_DIR = Path(_ROOT)
else:
    CACHE_DIR = Path.home() / ".cache" / "lumi-music"
STATE_FILE = CACHE_DIR / "state.json"


def _ensure_dir():
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def save():
    """保存当前播放状态到缓存文件"""
    import cli

    # 只保存有意义的快照
    ctx = cli._play_ctx
    if not ctx or not ctx.get("songs"):
        # 无上下文时不覆盖旧状态（避免清空有效快照）
        return

    # 只保存歌曲 ID 列表，不保存完整 songs（可能很大）
    songs_meta = []
    for s in ctx["songs"]:
        from api import normalize_song
        ns = normalize_song(s)
        songs_meta.append({
            "id": ns["id"],
            "name": ns["name"],
            "artist": ", ".join(a["name"] for a in ns["artists"]),
        })

    state = {
        "songs": songs_meta,
        "order": ctx["order"],
        "index": ctx["index"],
        "position_ms": cli.get_position_ms(),
        "shuffle": cli._shuffle,
        "loop_mode": cli._loop_mode,
        "volume": 1.0,
        "timestamp": __import__("time").time(),
    }
    _ensure_dir()
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def try_restore() -> bool:
    """尝试从缓存恢复播放状态，返回是否已恢复"""
    import cli
    import api

    if not STATE_FILE.exists():
        return False
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return False

    songs_meta = state.get("songs", [])
    if not songs_meta:
        return False

    # 按 ID 重新获取歌曲详情（时间窗口内 API 可能已变化，但最稳妥的方式）
    song_ids = [s["id"] for s in songs_meta]
    r = api.song_detail(song_ids)
    songs = r.get("songs", [])
    if not songs:
        return False

    # 重建上下文
    playable = api.check_playable(song_ids)
    order = state.get("order", list(range(len(songs))))
    idx = state.get("index", 0)
    ctx = {"songs": songs, "index": idx, "playable": playable, "order": order}

    cli._shuffle = bool(state.get("shuffle", False))
    cli._loop_mode = state.get("loop_mode", "off")
    cli._play_ctx = ctx

    # 从当前索引开始播放
    if idx < len(order):
        song_idx = order[idx]
        if song_idx < len(songs):
            ns = api.normalize_song(songs[song_idx])
            if ns["id"] in playable:
                cli.play_song(ns["id"], ns["name"], ctx=ctx, song_obj=songs[song_idx])
                # 尝试恢复播放位置（GStreamer 需要管道就绪后 seek）
                pos = state.get("position_ms", 0)
                if pos > 5000:
                    import threading
                    threading.Thread(
                        target=_delayed_seek,
                        args=(ns["id"], pos),
                        daemon=True,
                    ).start()
                return True
    return False


def _delayed_seek(song_id, target_ms):
    """等待管道就绪后 seek 到指定位置"""
    import cli
    import time
    for _ in range(50):
        if cli._current_song_id == song_id and cli._gst_pipeline is not None:
            try:
                cli.seek_to(target_ms)
            except Exception:
                pass
            return
        time.sleep(0.1)


def clear():
    """清除保存的状态"""
    if STATE_FILE.exists():
        try:
            STATE_FILE.unlink()
        except Exception:
            pass
