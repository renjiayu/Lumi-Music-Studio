#!/usr/bin/env python3
"""
终端音频频谱可视化
- 从播放管道接收频谱数据并渲染彩色条形图 (起降平滑 + 律动感)
- 由 cli.py 的 GStreamer 管道驱动, 共用同一音频流 (tee 分流)
"""
import os
import time
import threading
from collections import deque

# === 颜色 / 字符 ===
BARS = "▁▂▃▄▅▆▇█"
_BAR_LAST = len(BARS) - 1  # 7
THEME_RAINBOW = [196, 202, 208, 214, 220, 226, 190, 154, 118, 82, 46, 47, 48, 49, 50, 51]
_THEME = THEME_RAINBOW
_THEME_LEN = len(_THEME)  # 16

# === dB 窗口: 覆盖实际音乐动态范围 ===
# 实测: 多数频段在 -70~-50dB, 活跃频段在 -30~-15dB
_DB_FLOOR = -65.0   # 低于此视为静音
_DB_CEIL  = -15.0   # 高于此视为满格
_DB_SPAN  = _DB_CEIL - _DB_FLOOR  # 50 dB

# === 起降平滑系数 (律动感来源) ===
_ATTACK = 0.55   # 上升速度 (越大越快, 跟随鼓点)
_DECAY  = 0.10   # 下降速度 (越小越慢, 制造"余韵"呼吸感)

# === 全局状态 ===
_active = False
_render_thread = None
_viz_queue = deque(maxlen=64)
_viz_lock = threading.Lock()

# 终端宽度缓存
_cached_tw = 80
_cache_tw = False

# 各频段平滑值 (持续追踪)
SPECTRUM_BANDS = 32
_smooth = [0.0] * SPECTRUM_BANDS


def _get_term_width():
    global _cache_tw, _cached_tw
    if not _cache_tw:
        try:
            _cached_tw = min(os.get_terminal_size().columns - 2, 80)
        except Exception:
            _cached_tw = 78
        _cache_tw = True
    return _cached_tw


def _reset_tw_cache():
    global _cache_tw
    _cache_tw = False


def _color_for_val(v: float) -> str:
    """v 在 [0, 1], 映射到主题色"""
    idx = min(int(v * _THEME_LEN), _THEME_LEN - 1)
    return f"\033[38;5;{_THEME[idx]}m"


def _normalize_frame(vals: list) -> list:
    """
    单帧归一化 + 起降平滑 + 幂曲线, 产生律动感
    返回 [0, 1] 归一化值列表
    """
    out = []
    for i, v in enumerate(vals[:SPECTRUM_BANDS]):
        # 1. 钳位 dB 到可视窗口
        db = v
        if db < _DB_FLOOR:
            db = _DB_FLOOR
        elif db > _DB_CEIL:
            db = _DB_CEIL
        norm = (db - _DB_FLOOR) / _DB_SPAN

        # 2. 幂曲线: sqrt 压低底部/拉升中部, 让更多频段可见
        curved = norm ** 0.5

        # 3. 起降平滑 (attack/decay envelope)
        prev = _smooth[i]
        if curved > prev:
            _smooth[i] = prev + (curved - prev) * _ATTACK
        else:
            _smooth[i] = prev + (curved - prev) * _DECAY

        out.append(_smooth[i])
    return out


def _drain_queue():
    with _viz_lock:
        _viz_queue.clear()


def pop_frame():
    """Thread-safe: pop and return the next raw frame, or None.

    Callers should call _normalize_frame on the returned list before rendering.
    (Previously pop_frame normalized here, but _render_loop also normalized,
    causing a double-normalize bug.)
    """
    with _viz_lock:
        try:
            return _viz_queue.popleft()
        except IndexError:
            return None


def push_spectrum(mags: list):
    if not _active:
        return
    with _viz_lock:
        _viz_queue.append(mags)


def _render_loop():
    _reset_tw_cache()
    print()  # 占一行
    _idle_event = threading.Event()
    while _active:
        with _viz_lock:
            raw = _viz_queue.popleft() if _viz_queue else None
        if raw is None:
            if _active:
                _idle_event.wait(0.03)  # 比 sleep 更易中断
            continue

        try:
            mags_norm = _normalize_frame(raw)
            tw = _get_term_width()
            bw = max(tw // SPECTRUM_BANDS, 1)
            line = "\r  "
            for mag in mags_norm:
                ci = min(int(mag * _BAR_LAST), _BAR_LAST)
                line += _color_for_val(mag) + BARS[ci] * bw
            line += "\033[0m\033[K"
            print(line, end="", flush=True)
        except Exception:
            pass


def start():
    global _active, _render_thread, _smooth
    # 始终重置平滑状态和清空队列, 避免切歌时旧频谱数据残留
    _smooth = [0.0] * SPECTRUM_BANDS
    _drain_queue()
    if _active:
        return
    _active = True
    if _tui_mode:
        return  # TUI 自行渲染, 不启动 CLI 的 print 线程
    _render_thread = threading.Thread(target=_render_loop, daemon=True)
    _render_thread.start()


# TUI 模式标志 — 为 True 时 stop() 不 print (避免破坏 curses 画面)
_tui_mode = False


def stop():
    global _active
    if not _active:
        return
    _active = False
    if not _tui_mode and _render_thread is not None and _render_thread.is_alive():
        _render_thread.join(timeout=0.3)
    _drain_queue()
    if not _tui_mode:
        print("\r\033[K", end="", flush=True)


if __name__ == "__main__":
    print("频谱模块 — 由 cli.py 驱动, 按 v 键切换")
    print("直接运行 cli.py 即可测试: python3 cli.py")
