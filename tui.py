#!/usr/bin/env python3
"""
网易云音乐 Curses TUI 播放器 — 终端工具黑客风
"""
import sys
import time
import curses
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import api
import visualizer as _vz
import unblock
import state

import cli
from cli import (
    play_song, stop, play_next, play_prev,
    toggle_pause, toggle_shuffle, toggle_loop,
    get_position_ms, get_duration_ms, format_time_ms,
    lrc_index_at_time, seek_relative,
)

# ========== UI 状态 ==========
_songs = []
_playable = set()
_cursor = 0
_display_indices = []
_scroll_offset = 0
_show_all = True
_title = "网易云音乐"
_now_playing = {"title": "", "artist": "", "album": ""}
_last_input = ""
_lyrics_lines = []        # [(time_ms, text), ...] — protected by _lyrics_lock
_lyrics_lock = threading.Lock()
_lyrics_thread_lock = threading.Lock()
_lyrics_scroll = 0
_lyrics_song_id = 0
_lyrics_snapshot = []  # for key handler access
_max_duration = 0  # 列表中歌曲的最大时长 (秒), 用于微型进度条缩放
_crab_tick = 0     # 螃蟹动画帧计数
_lyrics_thread = None  # 当前歌词获取线程 (防止重复创建)

# 螃蟹吉祥物 ASCII art — 各播放状态表情
_CRAB = {
    "sleep": [
        "   ▄▄▄▄▄▄   ",
        "  █ -  - █ z",
        "  █  ~▿  █  ",
        "  ▀▄▄▄▄▄▄▀  ",
        "   ▐▌  ▐▌   ",
        "   ▀    ▀   ",
    ],
    "idle": [
        "   ▄▄▄▄▄▄   ",
        "  █ ○  ○ █  ",
        "  █  ▿▿  █  ",
        "  ▀▄▄▄▄▄▄▀  ",
        "   ▐▌  ▐▌   ",
        "   ▀    ▀   ",
    ],
    "playing": [
        "   ▄▄▄▄▄▄  ♪",
        "  █ ◕  ◕ █  ",
        "  █  ◡◡  █  ",
        "  ▀▄▄▄▄▄▄▀  ",
        "   ▐▌  ▐▌   ",
        "   ▀    ▀   ",
    ],
    "playing_l": [
        " ♪ ▄▄▄▄▄▄   ",
        "  █ ◕  ◕ █  ",
        "  █  ◡◡  █  ",
        "  ▀▄▄▄▄▄▄▀  ",
        "   ▐▌  ▐▌   ",
        "   ▀    ▀   ",
    ],
    "paused": [
        "   ▄▄▄▄▄▄   ",
        "  █ ⊙  ⊙ █  ",
        "  █  ▂▂  █  ",
        "  ▀▄▄▄▄▄▄▀  ",
        "   ▐▌  ▐▌   ",
        "   ▀    ▀   ",
    ],
    "peak": [
        "   ▄▄▄▄▄▄  ♫",
        "  █ ✧  ✧ █  ",
        "  █  ◡◡  █  ",
        "  ▀▄▄▄▄▄▄▀  ",
        "   ▐▌  ▐▌   ",
        "   ▀    ▀   ",
    ],
    "peak_l": [
        " ♫ ▄▄▄▄▄▄   ",
        "  █ ✧  ✧ █  ",
        "  █  ◡◡  █  ",
        "  ▀▄▄▄▄▄▄▀  ",
        "   ▐▌  ▐▌   ",
        "   ▀    ▀   ",
    ],
}

# ========== Curses 颜色 ==========

# Tokyo Night 配色方案 (在 _init_colors 中初始化)
HL = 0      # 高亮选中行
GR = 0      # 主文字 / 播放中
RD = 0      # 不可播 / 错误
YW = 0      # 暂停 / 警告
CY = 0      # 标题 / 标号
MG = 0      # 频谱 / 强调
FG = 0      # 普通文字
DM = 0      # 次要文字

SB = 0      # 状态栏反色, 在 _init_colors 中初始化

# 频谱渐变 (低→高: 蓝 → 青 → 绿 → 金 → 红 → 紫)
SP_LO = 0   # 低 — 蓝
SP_ML = 0   # 中低 — 天青
SP_MD = 0   # 中 — 柔绿
SP_MH = 0   # 中高 — 暖金
SP_HI = 0   # 高 — 珊瑚
SP_PK = 0   # 峰 — 薰紫

def _init_colors():
    global HL, GR, RD, YW, CY, MG, FG, DM, SB
    global SP_LO, SP_ML, SP_MD, SP_MH, SP_HI, SP_PK
    curses.start_color()
    curses.use_default_colors()
    # === Tokyo Night 配色 ===
    curses.init_pair(1, 81, -1)    # 青蓝 — 可播/播放中
    curses.init_pair(2, 210, -1)   # 柔和珊瑚红 — 不可播
    curses.init_pair(3, 179, -1)   # 暖金 — 暂停/警告
    curses.init_pair(4, 111, -1)   # 柔和蓝 — 标题/标号
    curses.init_pair(5, 141, -1)   # 薰衣草紫 — 频谱/强调
    curses.init_pair(6, 252, -1)   # 浅银灰 — 普通文字
    curses.init_pair(7, 235, 111)  # 暗面底蓝字 — 高亮选中
    curses.init_pair(8, 60, -1)    # 暗紫灰 — 次要文字/注释
    curses.init_pair(9, 235, 81)   # 反色 — 状态栏: 暗底青字
    # 频谱渐变 (低→高: 蓝 → 青 → 绿 → 金 → 红 → 紫)
    curses.init_pair(20, 111, -1)  # 蓝   — 低
    curses.init_pair(21, 117, -1)  # 天青 — 中低
    curses.init_pair(22, 113, -1)  # 柔绿 — 中
    curses.init_pair(23, 179, -1)  # 暖金 — 中高
    curses.init_pair(24, 210, -1)  # 珊瑚 — 高
    curses.init_pair(25, 141, -1)  # 薰紫 — 峰

    HL = curses.color_pair(7) | curses.A_BOLD
    GR = curses.color_pair(1) | curses.A_BOLD
    RD = curses.color_pair(2)
    YW = curses.color_pair(3)
    CY = curses.color_pair(4)
    MG = curses.color_pair(5)
    FG = curses.color_pair(6)
    DM = curses.color_pair(8) | curses.A_DIM
    SB = curses.color_pair(9)
    SP_LO = curses.color_pair(20)
    SP_ML = curses.color_pair(21)
    SP_MD = curses.color_pair(22)
    SP_MH = curses.color_pair(23)
    SP_HI = curses.color_pair(24)
    SP_PK = curses.color_pair(25)


def _safe_addstr(win, y, x, text, attr=0):
    if y < 0 or x < 0: return
    h, w = win.getmaxyx()
    if y >= h or x >= w: return
    try:
        win.addstr(y, x, text[:w - x], attr)
    except curses.error:
        pass

# ========== 绘制辅助 ==========

def _hline(win, y, x, width, attr=DM):
    _safe_addstr(win, y, x, "─" * width, attr)

def _draw_section_header(win, y, x, title, width, attr=CY):
    """分区标题: ─── TITLE ──────────────────────────"""
    head = f"─── {title} "
    _safe_addstr(win, y, x, head, attr | curses.A_BOLD)
    remaining = width - len(head)
    if remaining > 3:
        _safe_addstr(win, y, x + len(head), "─" * remaining, DM)

def _draw_vsep(win, y_start, y_end, x):
    """垂直分隔线 │"""
    for row in range(y_start, y_end):
        _safe_addstr(win, row, x, "│", DM)

def _draw_mini_bar(win, y, x, dur_sec, max_dur_sec, width=6):
    """微型时长条: ████░░ 表示歌曲时长占列表中最大时长的比例"""
    if max_dur_sec <= 0:
        _safe_addstr(win, y, x, "░" * width, DM)
        return
    ratio = min(dur_sec / max_dur_sec, 1.0)
    filled = max(int(ratio * width), 0)
    bar = "█" * filled + "░" * (width - filled)
    # 用频谱绿/金色
    if ratio > 0.7:
        attr = SP_MH
    elif ratio > 0.4:
        attr = SP_MD
    else:
        attr = SP_LO
    _safe_addstr(win, y, x, bar, attr)

# ========== 螃蟹吉祥物 ==========

def _draw_crab(win, y, x):
    """右下角 ASCII 螃蟹, 根据播放状态变换表情"""
    global _crab_tick
    _crab_tick += 1
    t = _crab_tick

    # 选择表情状态
    if not cli._playing:
        state = "sleep"
    elif cli._paused:
        state = "paused"
    else:
        # 跟随节奏: 大部分时间 vibing, 偶尔 high 起来
        beat = (t // 20) % 12
        if beat in (0, 5, 11):
            alt = (t % 30) < 15
            state = "peak_l" if alt else "peak"
        elif beat in (1, 6, 10):
            alt = (t % 40) < 20
            state = "playing_l" if alt else "playing"
        else:
            alt = (t % 50) < 25
            state = "playing_l" if alt else "playing"

    frames = _CRAB.get(state, _CRAB["idle"])

    for i, line in enumerate(frames):
        if i in (0, 3):
            attr = MG          # 壳边 — 薰衣草紫
        elif i in (4, 5):
            attr = YW          # 腿脚 — 暖金
        else:
            attr = GR          # 眼睛和脸 — 青蓝
        _safe_addstr(win, y + i, x, line, attr)

# ========== 频谱渲染 ==========

_SPECTRUM_BARS = " ▁▂▃▄▅▆▇█"

def _draw_spectrum(win, y, x, height, width):
    if not cli._show_viz or not cli._playing:
        return
    mags = _vz.pop_frame()
    if mags is None:
        return
    mags_norm = _vz._normalize_frame(mags)
    bands = min(width, 32)
    bw = max(width // bands, 1)
    for i in range(bands):
        curved = mags_norm[i] if i < len(mags_norm) else 0.0
        bar_h = int(curved * height)
        col_x = x + i * bw
        if curved < 0.15:
            attr = SP_LO
        elif curved < 0.30:
            attr = SP_ML
        elif curved < 0.45:
            attr = SP_MD
        elif curved < 0.60:
            attr = SP_MH
        elif curved < 0.80:
            attr = SP_HI
        else:
            attr = SP_PK | curses.A_BOLD
        for row in range(height):
            cy = y + height - 1 - row
            if row < bar_h:
                ch = "█" if row < bar_h - 1 else _SPECTRUM_BARS[max(min(bar_h - row, 8), 0)]
                _safe_addstr(win, cy, col_x, ch * bw, attr)
            else:
                _safe_addstr(win, cy, col_x, " " * bw)

# ========== 面板绘制 ==========

def _draw_status_bar(win):
    """tmux 风格顶部状态栏"""
    h, w = win.getmaxyx()
    # 整行用反色底板
    _safe_addstr(win, 0, 0, " " * w, SB)

    # 左: 品牌图标
    brand = " ⬢ Lumi Music Studio "
    _safe_addstr(win, 0, 0, brand, SB | curses.A_BOLD)
    x = len(brand)

    # 状态指示灯
    if cli._playing:
        if cli._paused:
            _safe_addstr(win, 0, x, " ● 已暂停 ", SB | curses.A_BOLD)
        else:
            _safe_addstr(win, 0, x, " ● 播放中 ", SB | curses.A_BOLD)
    else:
        _safe_addstr(win, 0, x, " ○ 空闲 ", SB)
    x += 10

    # 模式标签
    if cli._shuffle:
        label = " 随机 "
        _safe_addstr(win, 0, x, label, SB | curses.A_BOLD)
        x += len(label)
    if cli._loop_mode == "one":
        label = " 单曲循环 "
        _safe_addstr(win, 0, x, label, SB)
        x += len(label)
    elif cli._loop_mode == "all":
        label = " 列表循环 "
        _safe_addstr(win, 0, x, label, SB)
        x += len(label)
    if cli._show_viz:
        label = " 频谱 "
        _safe_addstr(win, 0, x, label, SB)
        x += len(label)

    # 右: 歌曲计数
    count = f" {len(_songs)} 首 "
    _safe_addstr(win, 0, w - len(count), count, SB)
    win.noutrefresh()


def _draw_left_panel(win, content_h, content_y, left_w):
    """左侧歌单列表 — 无边框, 直接渲染"""
    global _scroll_offset
    h, w = win.getmaxyx()

    # 分区标题
    _draw_section_header(win, content_y - 1, 0, "歌曲列表", left_w)

    max_visible = content_h - 1  # 留最后一行给 info bar
    if max_visible < 1:
        return

    # 滚动钳位
    if _cursor < _scroll_offset:
        _scroll_offset = _cursor
    elif _cursor >= _scroll_offset + max_visible:
        _scroll_offset = _cursor - max_visible + 1

    # 计算最大时长 (在渲染侧懒加载, 确保首次绘制时可用)
    global _max_duration
    if _max_duration == 0 and _songs:
        _max_duration = max(
            (api.normalize_song(s).get("duration", 0) // 1000
             for s in _songs),
            default=0,
        )

    # 计算列宽分配
    mini_bar_w = 6   # 微型时长条宽度
    dur_w = 5         # M:SS
    idx_w = 4         # " 001"
    mark_w = 2        # "▶ "

    fixed_w = mark_w + idx_w + dur_w + mini_bar_w + 6  # 6 = 间距/余量
    name_max = max(left_w - fixed_w, 8)

    for screen_row in range(max_visible):
        di = _scroll_offset + screen_row
        if di >= len(_display_indices): break
        song_idx = _display_indices[di]
        if song_idx >= len(_songs): break
        s = _songs[song_idx]
        ns = api.normalize_song(s)
        ok = ns["id"] in _playable
        name = ns["name"][:name_max]
        dur_sec = ns.get("duration", 0) // 1000
        dur_str = f"{dur_sec // 60}:{dur_sec % 60:02d}"
        row = content_y + screen_row

        mark = "▶" if ok else "✗"
        idx_str = f"{di:>3}"

        if di == _cursor:
            # 高亮整行
            line = f" {mark} {idx_str}  {name}"
            line = line.ljust(left_w - dur_w - mini_bar_w - 2)
            line += f" {dur_str}  "
            _safe_addstr(win, row, 0, line[:left_w], HL)
            _draw_mini_bar(win, row, left_w - mini_bar_w, dur_sec, _max_duration, mini_bar_w)
        else:
            mark_color = GR if ok else RD
            _safe_addstr(win, row, 0, f" {mark}", mark_color)
            _safe_addstr(win, row, 2, f"{idx_str}", DM)
            _safe_addstr(win, row, 6, f" {name}"[:left_w - dur_w - mini_bar_w - 4])
            _safe_addstr(win, row, left_w - dur_w - mini_bar_w - 1, f"{dur_str}", DM)
            _draw_mini_bar(win, row, left_w - mini_bar_w, dur_sec, _max_duration, mini_bar_w)

    # 底部信息行 (content 最后一行, 与歌曲列表不重叠)
    filter_label = "全部" if _show_all else "可播"
    info = f" [{filter_label}] {len(_display_indices)} 首"
    if _scroll_offset > 0:
        visible_end = min(_scroll_offset + max_visible, len(_display_indices))
        info += f"  ·  {_scroll_offset}-{visible_end}/{len(_display_indices)}"
    _safe_addstr(win, content_y + content_h - 1, 0, info[:left_w], DM)


def _draw_right_panel(win, content_h, content_y, split_x):
    """右侧播放信息 — 键值卡 + 频谱 + 歌词"""
    h, w = win.getmaxyx()
    right_w = w - split_x - 1
    if right_w < 15:
        return
    x0 = split_x + 1

    # 分区标题
    _draw_section_header(win, content_y - 1, x0, "正在播放", right_w)

    y = content_y
    np = _now_playing

    # 播放状态
    if cli._playing:
        status = "● 播放中" if not cli._paused else "● 已暂停"
    else:
        status = "○ 空闲"

    # 键值卡 (标签 10 字符宽, 右对齐)
    label_w = 9
    val_x = x0 + label_w + 1

    def _kv(label, value, color=FG):
        nonlocal y
        if y >= content_y + content_h: return
        _safe_addstr(win, y, x0, label.rjust(label_w), DM)
        _safe_addstr(win, y, val_x, str(value)[:right_w - label_w - 2], color)
        y += 1

    if np["title"]:
        _kv("标题", np["title"], GR if cli._playing else FG)
    else:
        _kv("标题", "-- 无曲目 --", DM)
        y += 1  # extra blank

    if np["artist"]:
        _kv("歌手", np["artist"], YW)
    if np["album"]:
        _kv("专辑", np["album"], DM)
    _kv("码率", f"{cli._current_br // 1000} kbps", CY)
    pos = get_position_ms()
    dur = get_duration_ms()
    if cli._playing and dur > 0:
        pct = min(pos / dur, 1.0)
        bar_w = min(right_w - 2, 24)
        filled = int(pct * bar_w)
        bar = "█" * filled + "░" * (bar_w - filled)
        _kv("进度", f"{format_time_ms(pos)}/{format_time_ms(dur)}", CY)
        if y < content_y + content_h:
            _safe_addstr(win, y, val_x, bar[:right_w - label_w - 2], GR)
            y += 1
    _kv("状态", status, GR if cli._playing else DM)

    y += 1  # 空行

    remaining_h = content_y + content_h - y
    if remaining_h < 3:
        return

    # --- SPECTRUM ---
    if cli._show_viz and cli._playing:
        spec_h = min(remaining_h // 3, 6)
        if spec_h >= 2:
            _draw_section_header(win, y, x0, "频谱", right_w, MG)
            y += 1
            _draw_spectrum(win, y, x0, spec_h, right_w)
            y += spec_h + 1
            remaining_h = content_y + content_h - y

    if remaining_h < 2:
        return

    # --- LYRICS ---
    global _lyrics_snapshot
    lyrics_snapshot = []
    with _lyrics_lock:
        lyrics_snapshot = list(_lyrics_lines)
    _lyrics_snapshot = lyrics_snapshot
    if lyrics_snapshot:
        _draw_section_header(win, y, x0, "歌词", right_w, DM)
        y += 1
        remaining_h = content_y + content_h - y
        visible = min(len(lyrics_snapshot), max(remaining_h, 1))
        cur_idx = lrc_index_at_time(lyrics_snapshot, get_position_ms()) if cli._playing else 0
        scroll = max(0, min(cur_idx - visible // 2, len(lyrics_snapshot) - visible))
        _lyrics_scroll = scroll
        for i in range(visible):
            if y + i >= content_y + content_h:
                break
            li = scroll + i
            if li >= len(lyrics_snapshot):
                break
            t_ms, text = lyrics_snapshot[li]
            ts = format_time_ms(t_ms)
            prefix = "▶" if li == cur_idx and cli._playing else " "
            display = f"{prefix}[{ts}] {text}"
            attr = GR | curses.A_BOLD if li == cur_idx and cli._playing else DM
            _safe_addstr(win, y + i, x0, display[:right_w - 1], attr)
        if len(lyrics_snapshot) > visible:
            _safe_addstr(win, y + visible, x0 + right_w - 4, " ▼ ", CY)
    elif cli._playing:
        _safe_addstr(win, y, x0, "获取歌词中...", DM)

    # 右下角螃蟹 (需要至少 6 行空间)
    crab_h = 6
    crab_w = 12
    remaining_h = content_y + content_h - y
    if remaining_h >= crab_h and right_w >= crab_w + 2:
        crab_y = content_y + content_h - crab_h
        crab_x = w - crab_w - 1
        _draw_crab(win, crab_y, crab_x)


def _draw_help_bar(win):
    """底部命令栏 (最后一行)"""
    h, w = win.getmaxyx()
    help_y = h - 1

    # 分隔线
    _hline(win, help_y - 1, 0, w, DM)

    keys = [
        ("ENTER", "播放", GR),
        ("SPC", "暂停", YW),
        ("n", "下一首", FG),
        ("b", "上一首", FG),
        ("+/-", "快进/退", FG),
        ("r", "随机", FG),
        ("c", "循环", FG),
        ("s", "搜索", CY),
        ("p", "榜单", CY),
        ("y", "每日", CY),
        ("m", "我的", CY),
        ("v", "频谱", MG),
        ("f", "筛选", FG),
        ("d", "下载", FG),
        ("l", "歌词", FG),
        ("Q", "扫码登录", YW),
        ("q", "退出", RD),
    ]
    x = 0
    for key, label, color in keys:
        seg = f" [{key}]{label}"
        _safe_addstr(win, help_y, x, seg, color)
        x += len(seg)
    win.noutrefresh()

# ========== 弹窗 ==========

def _modal_input(screen, prompt):
    h, w = screen.getmaxyx()
    pw = min(_display_width(prompt) + 40, w - 4)
    popup = curses.newwin(3, pw, h // 2 - 1, (w - pw) // 2)
    popup.border()
    _safe_addstr(popup, 0, 2, f" {prompt} ", CY | curses.A_BOLD)
    popup.refresh()
    input_win = popup.derwin(1, pw - 4, 1, 2)
    try:
        curses.echo()
        curses.curs_set(1)
        result = input_win.getstr(0, 0, pw - 5).decode("utf-8", errors="replace").strip()
    finally:
        curses.noecho()
        curses.curs_set(0)
    return result

def _popup_lyrics(screen, song_id, title):
    with _lyrics_lock:
        parsed = list(_lyrics_lines) if _lyrics_lines and isinstance(_lyrics_lines[0], tuple) and _lyrics_song_id == song_id else None
    if not parsed:
        r = api.lyric(song_id)
        lrc = r.get("lrc", {}).get("lyric", "")
        if not lrc:
            return
        parsed = cli.parse_lrc(lrc)
        if not parsed:
            return
    lines = [f"{format_time_ms(t)} {txt}" for t, txt in parsed]
    h, w = screen.getmaxyx()
    ph = min(h - 4, 24); pw = min(w - 4, 60)
    popup = curses.newwin(ph, pw, (h - ph) // 2, (w - pw) // 2)
    popup.border()
    _safe_addstr(popup, 0, 2, f" 歌词: {title[:pw-12]} ", CY | curses.A_BOLD)
    scroll = 0
    while True:
        for i in range(ph - 2):
            li = scroll + i
            if li < len(lines):
                attr = GR if i == 0 else DM
                _safe_addstr(popup, i + 1, 1, f" {lines[li]}"[:pw - 2], attr)
            else:
                _safe_addstr(popup, i + 1, 1, " " * (pw - 2))
        _safe_addstr(popup, ph - 1, 2, " 上/下滚动  Q=关闭 ", DM)
        popup.refresh()
        key = screen.getch()
        if key == ord("q"): break
        elif key == curses.KEY_UP and scroll > 0: scroll -= 1
        elif key == curses.KEY_DOWN and scroll < len(lines) - ph + 2: scroll += 1

def _display_width(text: str) -> int:
    """计算字符串在终端中的视觉宽度 (CJK 字符占 2 列)"""
    width = 0
    for ch in text:
        cp = ord(ch)
        if cp >= 0x2E80 and cp <= 0x9FFF:   # CJK Radicals + Unified Ideographs
            width += 2
        elif cp >= 0x3000 and cp <= 0x303F:  # CJK Symbols and Punctuation
            width += 2
        elif cp >= 0xFF00 and cp <= 0xFFEF:  # Fullwidth Forms
            width += 2
        else:
            width += 1
    return width


def _popup_message(screen, text, color=GR, duration=1.5):
    h, w = screen.getmaxyx()
    tw = _display_width(text)
    pw = min(tw + 6, w - 4)
    popup = curses.newwin(3, pw, h // 2 - 1, (w - pw) // 2)
    popup.border()
    _safe_addstr(popup, 1, 2, text, color | curses.A_BOLD)
    popup.refresh()
    time.sleep(duration)

# ========== 数据加载 ==========

def _load_search(keyword):
    global _songs, _playable, _display_indices, _cursor, _title, _show_all, _last_input
    _last_input = keyword
    _show_all = True
    r = api.search(keyword, stype=1, limit=50)
    if r.get("code") != 200: return False
    _songs = r.get("result", {}).get("songs", [])
    ids = [api.normalize_song(s)["id"] for s in _songs]
    _playable = api.check_playable(ids)
    _refresh_display()
    _title = f"搜索: {keyword}"
    _max_duration = 0
    return len(_songs) > 0

def _load_playlist(playlist_id, name=""):
    global _songs, _playable, _display_indices, _cursor, _title, _show_all
    _show_all = True
    result = api.playlist_detail_all(playlist_id)
    _songs = result["tracks"]
    ids = [api.normalize_song(t)["id"] for t in _songs]
    _playable = api.check_playable(ids)
    _refresh_display()
    _title = f"歌单: {name}" if name else f"#{playlist_id}"
    _max_duration = 0
    return len(_songs) > 0

_lyrics_gen = 0

def _fetch_lyrics(song_id):
    global _lyrics_lines, _lyrics_scroll, _lyrics_gen, _lyrics_song_id, _lyrics_thread, _lyrics_thread_lock
    try:
        _lyrics_gen += 1
        my_gen = _lyrics_gen
        r = api.lyric(song_id)
        lrc = r.get("lrc", {}).get("lyric", "")
        if not lrc:
            if my_gen == _lyrics_gen:
                with _lyrics_lock:
                    _lyrics_lines = []
                _lyrics_scroll = 0
                _lyrics_song_id = song_id
            return
        lines = cli.parse_lrc(lrc)
        if my_gen == _lyrics_gen:
            with _lyrics_lock:
                _lyrics_lines = lines
            _lyrics_scroll = 0
            _lyrics_song_id = song_id
    finally:
        _lyrics_thread = None


def _on_track_change(song_id, title, artist, album):
    global _now_playing, _lyrics_thread, _lyrics_thread_lock
    _now_playing = {"title": title, "artist": artist, "album": album}
    with _lyrics_lock:
        _lyrics_lines.clear()
    with _lyrics_thread_lock:
        if _lyrics_thread is not None and _lyrics_thread.is_alive():
            return  # 上一次歌词请求仍在进行中, 跳过
        _lyrics_thread = threading.Thread(target=_fetch_lyrics, args=(song_id,), daemon=True)
        _lyrics_thread.start()

def _refresh_display():
    global _display_indices, _cursor, _scroll_offset
    _display_indices = []
    for i, s in enumerate(_songs):
        ns = api.normalize_song(s)
        if not _show_all and ns["id"] not in _playable: continue
        _display_indices.append(i)
    if _cursor >= len(_display_indices): _cursor = max(len(_display_indices) - 1, 0)
    _scroll_offset = 0

def _toggle_filter():
    global _show_all
    _show_all = not _show_all
    _refresh_display()

# ========== 主循环 ==========

def main(stdscr):
    global _cursor, _lyrics_scroll
    cli._tui_mode = True
    cli._on_track_change = _on_track_change
    _vz._tui_mode = True
    _init_colors()
    curses.curs_set(0)
    stdscr.timeout(60)
    stdscr.clear()

    import config
    logged_in = False
    # 优先从 Cookie Jar 恢复完整会话
    if api.load_cookie_jar():
        logged_in = True
    else:
        # 回退: config music_u → 浏览器
        music_u = config.load().get("music_u", "")
        if music_u:
            ok, _ = api.set_cookie(f"MUSIC_U={music_u}")
            if ok:
                api.save_cookie_jar()
                logged_in = True
        if not logged_in and api.auto_load_browser_cookie():
            api.save_cookie_jar()
            logged_in = True
    # 尝试刷新 token
    if logged_in:
        try:
            api.refresh_token()
        except Exception:
            pass
    unblock.start()
    if not state.try_restore():
        _load_playlist(3778678, "热歌榜")
    _vz._active = True

    # 首次启动未登录时主动提示
    if not logged_in:
        _popup_message(stdscr, "未登录, 按 Shift+Q 扫码登录", YW, 2.0)

    while True:
        h, w = stdscr.getmaxyx()

        # === 布局计算 ===
        # Row 0: 状态栏 (1 行)
        # Row 1: 分区标题行 (1 行，含 ─── TRACKS ─── 和 ─── NOW PLAYING ───)
        # Row 2 .. h-3: 内容区
        # Row h-2: 命令分隔线
        # Row h-1: 命令栏
        content_y = 2
        content_h = max(h - 2 - content_y, 1)  # 内容填到 h-3, 分隔线在 h-2, 命令在 h-1
        left_w = max(w * 3 // 5, 35)  # 左侧 60%, 最少 35 列
        split_x = left_w  # 竖线位置

        stdscr.erase()

        # 绘制各区域
        _draw_status_bar(stdscr)
        _draw_left_panel(stdscr, content_h, content_y, left_w)
        _draw_vsep(stdscr, content_y, content_y + content_h, split_x)
        _draw_right_panel(stdscr, content_h, content_y, split_x)
        _draw_help_bar(stdscr)
        curses.doupdate()

        key = stdscr.getch()
        if key == -1: continue

        # --- 全局控制 ---
        if key == ord("q"): break
        elif key == ord(" "): toggle_pause()
        elif key == ord("n"): play_next()
        elif key == ord("b"): play_prev()
        elif key == ord("r"): toggle_shuffle()
        elif key == ord("c"): toggle_loop()
        elif key == ord("v"): cli._show_viz = not cli._show_viz
        elif key in (ord("+"), ord("=")):
            seek_relative(10000)
        elif key == ord("-"):
            seek_relative(-10000)
        elif key == ord("a"):
            cli._auto_next = not cli._auto_next
            _popup_message(stdscr, f"自动下一首: {'开' if cli._auto_next else '关'}", GR, 1.0)

        # --- 列表操作 ---
        elif key == ord("f"): _toggle_filter()
        elif key == ord("j") or key == curses.KEY_DOWN:
            if _display_indices and _cursor < len(_display_indices) - 1: _cursor += 1
        elif key == ord("k") or key == curses.KEY_UP:
            if _cursor > 0: _cursor -= 1
        elif key == curses.KEY_NPAGE:
            if _display_indices:
                page = max(content_h - 1, 5)
                _cursor = min(_cursor + page, len(_display_indices) - 1)
        elif key == curses.KEY_PPAGE:
            page = max(content_h - 1, 5)
            _cursor = max(_cursor - page, 0)
        elif key in (ord("g"), curses.KEY_HOME): _cursor = 0
        elif key in (ord("G"), curses.KEY_END): _cursor = max(len(_display_indices) - 1, 0)

        # --- 歌词滚动 ---
        elif key == ord("]") and _lyrics_snapshot:
            _lyrics_scroll = min(_lyrics_scroll + 1, max(len(_lyrics_snapshot) - 1, 0))
        elif key == ord("[") and _lyrics_scroll > 0: _lyrics_scroll -= 1

        # --- 播放 ---
        elif key == ord("\n"):
            global _lyrics_thread
            if _display_indices and _cursor < len(_display_indices):
                song_idx = _display_indices[_cursor]
                ns = api.normalize_song(_songs[song_idx])
                if ns["id"] in _playable:
                    ctx = cli._normalize_play_ctx(_songs, song_idx, _playable)
                    play_song(ns["id"], ns["name"], ctx=ctx, song_obj=_songs[song_idx])
                    with _lyrics_lock:
                        _lyrics_lines.clear()
                    _lyrics_scroll = 0
                    with _lyrics_thread_lock:
                        if _lyrics_thread is not None and _lyrics_thread.is_alive():
                            _lyrics_thread = None  # 等旧线程自行退出
                        _lyrics_thread = threading.Thread(
                            target=_fetch_lyrics, args=(ns["id"],), daemon=True)
                        _lyrics_thread.start()

        # --- 搜索 ---
        elif key == ord("s"):
            kw = _modal_input(stdscr, "搜索")
            if kw:
                _popup_message(stdscr, "搜索中...", GR, 0.5)
                if _load_search(kw):
                    _popup_message(stdscr, f"找到 {len(_songs)} 首", GR, 1.0)
                else:
                    _popup_message(stdscr, "无结果", RD, 1.5)

        # --- 排行榜 ---
        elif key == ord("p"):
            r = api.top_list()
            lists = r.get("list", [])[:25]
            if not lists: _popup_message(stdscr, "获取失败", RD, 1.5); continue
            ph = min(len(lists) + 2, h - 4); pw = min(50, w - 4)
            popup = curses.newwin(ph, pw, (h - ph) // 2, (w - pw) // 2)
            popup.border()
            _safe_addstr(popup, 0, 2, " 排行榜 ", CY | curses.A_BOLD)
            sel = 0
            while True:
                for i, pl in enumerate(lists[:ph - 2]):
                    name = pl.get("name", "")[:pw - 8]
                    if i == sel:
                        _safe_addstr(popup, i + 1, 2, f" ▶ {i:>2}. {name} ", HL)
                    else:
                        _safe_addstr(popup, i + 1, 2, f"   {i:>2}. {name} ")
                popup.refresh()
                k = stdscr.getch()
                if k == ord("q"): break
                elif k == curses.KEY_UP and sel > 0: sel -= 1
                elif k == curses.KEY_DOWN and sel < len(lists) - 1: sel += 1
                elif k == ord("\n"):
                    pid = lists[sel]["id"]; pname = lists[sel]["name"]
                    _popup_message(stdscr, f"加载中: {pname}...", GR, 0.5)
                    try:
                        ok = _load_playlist(pid, pname)
                    except Exception as e:
                        _popup_message(stdscr, f"加载失败: {e}", RD, 1.5)
                        break
                    if ok:
                        _popup_message(stdscr, f"{pname}: {len(_songs)} 首", GR, 1.0)
                    break

        # --- 歌词 ---
        elif key == ord("l"):
            if _display_indices and _cursor < len(_display_indices):
                song_idx = _display_indices[_cursor]
                ns = api.normalize_song(_songs[song_idx])
                _popup_lyrics(stdscr, ns["id"], ns["name"])

        # --- 扫码登录 ---
        elif key == ord("L") or key == ord("Q"):
            _do_qrcode_login(stdscr)

        # --- 下载 ---
        elif key == ord("d"):
            if _display_indices and _cursor < len(_display_indices):
                song_idx = _display_indices[_cursor]
                ns = api.normalize_song(_songs[song_idx])
                artists = api.format_artists(ns["artists"])
                _popup_message(stdscr, f"下载: {ns['name']}...", GR, 1.0)
                from downloader import download_song as ds
                threading.Thread(target=ds, args=(ns["id"], ns["name"], artists), daemon=True).start()

        elif key == ord("y"):
            _popup_message(stdscr, "加载每日推荐...", GR, 0.5)
            r = api.daily_recommend()
            if r.get("code") == 200:
                songs = r.get("data", {}).get("dailySongs") or r.get("recommend", [])
                if songs:
                    _songs[:] = songs
                    _show_all = True
                    ids = [api.normalize_song(s)["id"] for s in _songs]
                    _playable.clear()
                    _playable.update(api.check_playable(ids))
                    _refresh_display()
                    _max_duration = 0
                    _title = "每日推荐"
                    _popup_message(stdscr, f"每日推荐: {len(_songs)} 首", GR, 1.0)
                else:
                    _popup_message(stdscr, "每日推荐为空", RD, 1.5)
            else:
                _popup_message(stdscr, "需要登录", RD, 1.5)

        elif key == ord("m"):
            uid = api.get_login_uid()
            if not uid:
                _popup_message(stdscr, "需要登录", RD, 1.5)
            else:
                r = api.user_playlist(uid, limit=30)
                pls = r.get("playlist", [])
                if not pls:
                    _popup_message(stdscr, "无歌单", RD, 1.5)
                else:
                    ph = min(len(pls) + 2, h - 4)
                    pw = min(50, w - 4)
                    popup = curses.newwin(ph, pw, (h - ph) // 2, (w - pw) // 2)
                    popup.border()
                    _safe_addstr(popup, 0, 2, " 我的歌单 ", CY | curses.A_BOLD)
                    sel = 0
                    while True:
                        for i, pl in enumerate(pls[:ph - 2]):
                            name = pl.get("name", "")[:pw - 8]
                            if i == sel:
                                _safe_addstr(popup, i + 1, 2, f" ▶ {i:>2}. {name} ", HL)
                            else:
                                _safe_addstr(popup, i + 1, 2, f"   {i:>2}. {name} ")
                        popup.refresh()
                        k = stdscr.getch()
                        if k == ord("q"):
                            break
                        elif k == curses.KEY_UP and sel > 0:
                            sel -= 1
                        elif k == curses.KEY_DOWN and sel < len(pls) - 1:
                            sel += 1
                        elif k == ord("\n"):
                            pid = pls[sel]["id"]
                            pname = pls[sel]["name"]
                            _popup_message(stdscr, f"加载中: {pname}...", GR, 0.5)
                            _load_playlist(pid, pname)
                            break

def _do_qrcode_login(screen):
    """TUI 扫码登录弹窗"""
    key = api.qrcode_unikey()
    if not key:
        _popup_message(screen, "获取二维码失败", RD, 1.5)
        return
    url = f"https://music.163.com/login?codekey={key}"

    h, w = screen.getmaxyx()
    # 渲染二维码到字符串
    qr_lines = []
    try:
        import qrcode
        import io
        qr = qrcode.QRCode(border=2, box_size=1)
        qr.add_data(url)
        qr.make()
        buf = io.StringIO()
        try:
            qr.print_ascii(invert=True, out=buf)
        except TypeError:
            qr.print_ascii(out=buf)
        buf.seek(0)
        qr_lines = buf.read().split("\n")
    except ImportError:
        qr_lines = [url[:w - 4]]

    ph = min(len(qr_lines) + 7, h - 4)
    pw = min(50, w - 4)
    popup = curses.newwin(ph, pw, (h - ph) // 2, (w - pw) // 2)
    popup.border()
    _safe_addstr(popup, 0, 2, " 扫码登录 ", CY | curses.A_BOLD)
    for i, line in enumerate(qr_lines):
        if i + 1 < ph - 1:
            _safe_addstr(popup, i + 1, (pw - len(line) - 2) // 2, line, FG)
    y = len(qr_lines) + 1
    _safe_addstr(popup, y, 1, " 请用网易云 APP 扫码 ", YW)
    _safe_addstr(popup, y + 1, 1, " 等待中... (Q=退出) ", DM)

    waited = 0
    try:
        orig_timeout = screen.gettimeout()
    except AttributeError:
        orig_timeout = 60  # stdscr.timeout(60) default in main()
    screen.timeout(800)  # 800ms 轮询
    while waited < 150:
        _safe_addstr(popup, y + 2, 1, f" {' ' * 14} ", DM)
        dots = "." * (1 + waited % 10)
        _safe_addstr(popup, y + 2, 1, f" {dots} ", DM)
        popup.refresh()
        k = screen.getch()
        if k == ord("q") or k == ord("Q"):
            screen.timeout(orig_timeout)
            return
        try:
            r = api.qrcode_login_check(key)
        except Exception:
            # 极低概率的未捕获异常, 保持弹窗不崩溃
            _safe_addstr(popup, y + 2, 1, " 网络异常, 重试中  ", RD)
            popup.refresh()
            waited += 1
            continue
        code = r.get("code", 0)
        if code == 803:
            screen.timeout(orig_timeout)
            # 优先从响应体 cookie 字段提取, 其次从 session cookies
            cookie_str = r.get("cookie", "")
            music_u = None
            if cookie_str:
                for part in cookie_str.split(";"):
                    part = part.strip()
                    if part.startswith("MUSIC_U="):
                        music_u = part.split("=", 1)[1]
                        break
            if not music_u:
                api.save_cookie_jar()
                for cookie in api.get_session().cookies:
                    if cookie.name == "MUSIC_U" and cookie.value:
                        music_u = cookie.value
                        break
            if music_u:
                api.set_cookie(f"MUSIC_U={music_u}")
                api.save_cookie_jar()
                import config
                config.set_key("music_u", music_u)
            api.save_cookie_jar()
            _popup_message(screen, "登录成功!", GR, 1.0)
            return
        elif code == 800:
            screen.timeout(orig_timeout)
            _popup_message(screen, "二维码已过期", RD, 1.5)
            return
        elif code == -1:
            err = r.get("error", "请求失败")
            _safe_addstr(popup, y + 2, 1, f" {err}, 重试中    ", RD)
            popup.refresh()
            waited += 1
            continue
        elif code == 802:
            nick = r.get("nickname", "")
            _safe_addstr(popup, y + 1, 1, f" {nick} 已扫码, 请在 APP 确认 ", GR | curses.A_BOLD)
        elif code == 8821:
            # 安全校验跳转: 跟随 redirectUrl 获取额外 cookie
            redirect_url = r.get("redirectUrl", "")
            _safe_addstr(popup, y + 2, 1, " 安全校验中...           ", YW)
            popup.refresh()
            if redirect_url:
                api.qrcode_follow_redirect(redirect_url)
            # 继续轮询, 等待 code 803
        elif code == 801:
            _safe_addstr(popup, y + 1, 1, " 等待扫码... (Q=退出)        ", DM)
        else:
            # 显示未知响应码 (用于调试 API 变化)
            _safe_addstr(popup, y + 2, 1, f" 响应码: {code}           ", YW)
        waited += 1
    screen.timeout(orig_timeout)
    _popup_message(screen, "扫码登录超时", RD, 1.5)

# ========== 入口 ==========

def run():
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    finally:
        state.save()
        stop()
        _vz._active = False
        _vz.stop()
        unblock.stop()
        cli._tui_mode = False
        cli._on_track_change = None
        print("\033[?25h", end="", flush=True)

if __name__ == "__main__":
    run()
