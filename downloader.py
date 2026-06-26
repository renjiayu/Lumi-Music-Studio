#!/usr/bin/env python3
"""
网易云音乐离线下载器
- 下载单曲/歌单/专辑 (320kbps优先)
- 自动嵌入 MP3 标签 (标题/歌手/专辑/封面)
- 结构化目录: ~/Music/<歌手>/<歌名>.mp3
"""
import sys
import threading
from pathlib import Path
import requests as req
sys.path.insert(0, str(Path(__file__).parent))
import api
from api import c

try:
    import config
    _cfg = config.load()
    OUTPUT_DIR = Path(_cfg.get("download_dir", str(Path.home() / "Music" / "网易云")))
    _default_br = int(_cfg.get("default_br", 320000))
except ImportError:
    OUTPUT_DIR = Path.home() / "Music" / "网易云"
    _default_br = 320000

_UNSAFE_FILENAME_RE = __import__("re").compile(r'[\\/:*?"<>|]')


def _safe_filename(s: str) -> str:
    """将字符串转为安全文件名"""
    return _UNSAFE_FILENAME_RE.sub("_", s)


try:
    from mutagen.id3 import ID3, TIT2, TPE1, TALB, APIC
    from mutagen.mp3 import MP3
    TAG_OK = True
except ImportError:
    TAG_OK = False
    print(c("  ⚠ mutagen 未安装, ID3 标签和封面嵌入将跳过 (pip install mutagen)", "yellow"))


def fetch_cover(url: str) -> bytes:
    """下载封面图"""
    try:
        r = req.get(url, headers=api.HEADERS, timeout=10)
        return r.content
    except Exception:
        return None


def tag_mp3(filepath: Path, song: dict):
    """给 MP3 写入 ID3 标签"""
    if not TAG_OK:
        return
    ns = api.normalize_song(song)
    try:
        audio = MP3(str(filepath), ID3=ID3)
        audio.tags.add(TIT2(encoding=3, text=ns["name"]))
        artist = api.format_artists(ns["artists"])
        audio.tags.add(TPE1(encoding=3, text=artist))
        audio.tags.add(TALB(encoding=3, text=ns["album"]["name"]))
        # 封面
        pic_url = song.get("al", {}).get("picUrl", "")
        if not pic_url:
            pic_url = song.get("album", {}).get("picUrl", "")
        if pic_url:
            cover = fetch_cover(pic_url)
            if cover:
                audio.tags.add(APIC(
                    encoding=3, mime="image/jpeg", type=3,
                    desc="Cover", data=cover
                ))
        audio.save()
    except Exception as e:
        print(c(f"    ⚠ 标签写入失败: {e}", "yellow"))


def save_lrc(filepath: Path, song_id: int):
    """保存 LRC 歌词文件"""
    try:
        r = api.lyric(song_id)
        lrc = r.get("lrc", {}).get("lyric", "")
        if lrc:
            lrc_path = filepath.with_suffix(".lrc")
            lrc_path.write_text(lrc, encoding="utf-8")
    except Exception:
        pass


def download_song(song_id: int, title: str = "", artist: str = "",
                  out_dir: Path = None, song_obj: dict = None,
                  idx: int = 0, total: int = 1, threads: int = 8) -> bool:
    """下载单首歌曲 (多线程加速), 返回是否成功"""
    out_dir = out_dir or OUTPUT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    url, _ = api.resolve_song_url(song_id, _default_br)
    if not url:
        print(c(f"  [{idx}/{total}] ✗ {title} — 无播放链接", "red"))
        return False

    # 安全文件名
    safe_name = _safe_filename(title)
    if artist:
        safe_artist = _safe_filename(artist)
        filepath = out_dir / safe_artist / f"{safe_name}.mp3"
    else:
        filepath = out_dir / f"{safe_name}.mp3"
    filepath.parent.mkdir(parents=True, exist_ok=True)

    if filepath.exists():
        print(c(f"  [{idx}/{total}] ⊘ {title} — 已存在, 跳过", "yellow"))
        return True

    # 获取文件大小, 测试 Range 支持
    accept_ranges = False
    try:
        head = req.head(url, headers=api.HEADERS, timeout=10)
        total_size = int(head.headers.get("content-length", 0))
        # 实际测试 Range (即使没声明 Accept-Ranges)
        test = req.get(url, headers={**api.HEADERS, "Range": "bytes=0-0"}, timeout=10)
        accept_ranges = test.status_code == 206
    except Exception:
        total_size = 0

    # === 多线程分块下载 ===
    if total_size > 512 * 1024 and accept_ranges:
        success = _download_mt(url, filepath, total_size, threads, title, idx, total)
    else:
        success = _download_st(url, filepath, total_size, title, idx, total)

    if success:
        print(f"\r  [{idx}/{total}] {c('✓','green')} {title[:30]}  "
              f"({total_size//1024//1024}MB x{threads if accept_ranges else 1}线程)")
        if song_obj:
            tag_mp3(filepath, song_obj)
        save_lrc(filepath, song_id)
    return success


def _download_st(url: str, filepath: Path, total_size: int,
                 title: str, idx: int, total: int) -> bool:
    """单线程下载"""
    try:
        downloaded = 0
        last_pct = -1
        with open(filepath, "wb") as f:
            resp = req.get(url, headers=api.HEADERS, timeout=60, stream=True)
            resp.raise_for_status()
            for chunk in resp.iter_content(65536):
                f.write(chunk)
                downloaded += len(chunk)
                if total_size > 0:
                    pct = downloaded * 100 // total_size
                    if pct != last_pct:
                        bar = "█" * (pct // 5) + "░" * (20 - pct // 5)
                        print(f"\r  [{idx}/{total}] ⏳ [{bar}] {pct}% {title[:20]}",
                              end="", flush=True)
                        last_pct = pct
        return True
    except Exception as e:
        print(f"\r  [{idx}/{total}] {c('✗','red')} {title[:30]} — {e}")
        try:
            if filepath.exists():
                filepath.unlink()
        except OSError:
            pass
        return False


def _download_mt(url: str, filepath: Path, total_size: int, threads: int,
                 title: str, idx: int, total: int) -> bool:
    """多线程分块下载 (IDM 模式)

    策略: 每个线程先将完整分块下载到内存 bytearray, 完成后按顺序
    串行写入文件. 避免多线程并发 open/seek/write 导致的竞争.
    """
    chunk_size = total_size // threads
    chunks = []
    for i in range(threads):
        start = i * chunk_size
        end = start + chunk_size - 1 if i < threads - 1 else total_size - 1
        chunks.append({"start": start, "end": end, "data": None, "done": False, "fail": False})

    def _download_chunk(ci: int):
        """下载单个分块到内存"""
        try:
            start = chunks[ci]["start"]
            end = chunks[ci]["end"]
            headers = {**api.HEADERS, "Range": f"bytes={start}-{end}"}
            resp = req.get(url, headers=headers, timeout=60, stream=True)
            resp.raise_for_status()
            buf = bytearray()
            for chunk in resp.iter_content(65536):
                buf.extend(chunk)
            chunks[ci]["data"] = buf
            chunks[ci]["done"] = True
        except Exception:
            chunks[ci]["fail"] = True

    try:
        # 预分配文件
        with open(filepath, "wb") as f:
            f.truncate(total_size)

        # 启动多线程 (仅做网络下载, 不涉及文件 I/O)
        ts = []
        for ci in range(threads):
            t = threading.Thread(target=_download_chunk, args=(ci,), daemon=True)
            t.start()
            ts.append(t)
        for t in ts:
            t.join()

        if any(ch["fail"] for ch in chunks):
            raise Exception("分块下载失败")

        # 串行写入文件 (无竞争)
        with open(filepath, "r+b") as f:
            for ch in chunks:
                if ch["data"] is not None:
                    f.seek(ch["start"])
                    f.write(ch["data"])

        # 验证文件大小
        if filepath.stat().st_size < total_size * 0.95:
            raise Exception("文件不完整")

        return True

    except Exception as e:
        print(f"\r  [{idx}/{total}] {c('✗','red')} {title[:30]} — {e}")
        try:
            if filepath.exists():
                filepath.unlink()
        except OSError:
            pass
        return False


def download_playlist(playlist_id: int, page_size: int = 100):
    """下载整个歌单 (自动翻页)"""
    print(c("📋 获取歌单信息...", "dim"))
    # 先取第一页拿去歌名和总曲数, 再取全部曲目
    result = api.playlist_detail_all(playlist_id, page_size)
    name = result["name"]
    tracks = result["tracks"]
    if not tracks:
        print(c("✗ 歌单为空", "red"))
        return

    out_dir = OUTPUT_DIR / name
    print(c(f"\n💿 {name} — {len(tracks)} 首", "bold"))
    print(c(f"📁 保存到: {out_dir}", "dim"))
    print()

    # 批量检测可播
    print(c("🔗 检测播放状态...", "dim"))
    ids = [api.normalize_song(t)["id"] for t in tracks]
    playable = api.check_playable(ids)
    print(c(f"  可播: {len(playable)}/{len(tracks)}", "dim"))
    print()

    ok = fail = skip = 0
    for i, t in enumerate(tracks):
        ns = api.normalize_song(t)
        sid = ns["id"]
        if sid not in playable:
            print(f"  [{i+1}/{len(tracks)}] ⊘ {ns['name'][:30]} — 不可播")
            skip += 1
            continue
        artist = api.format_artists(ns["artists"])[:40]
        if download_song(sid, ns["name"], artist, out_dir, song_obj=t,
                         idx=i+1, total=len(tracks)):
            ok += 1
        else:
            fail += 1

    print()
    print(c(f"✓ 完成: 下载{ok} 跳过{skip} 失败{fail}", "green"))
    print(c(f"📁 {out_dir}", "cyan"))


def download_single(song_id: int):
    """下载单曲"""
    r = api.song_detail(song_id)
    songs = r.get("songs", [])
    if not songs:
        print(c("✗ 歌曲不存在", "red"))
        return
    ns = api.normalize_song(songs[0])
    artist = api.format_artists(ns["artists"])
    print(c(f"\n🎵 {ns['name']} — {artist}", "bold"))
    download_song(song_id, ns["name"], artist, song_obj=songs[0], idx=1, total=1)


# ========== CLI ==========
if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="网易云音乐下载器")
    p.add_argument("-s", "--song", type=int, help="下载单曲 (歌曲ID)")
    p.add_argument("-p", "--playlist", type=int, help="下载歌单 (歌单ID)")
    p.add_argument("-l", "--limit", type=int, default=100, help="歌单每页请求曲数")
    p.add_argument("--dir", type=str, default=str(OUTPUT_DIR), help="输出目录")
    args = p.parse_args()

    api.auto_load_browser_cookie()

    if args.dir:
        OUTPUT_DIR = Path(args.dir)

    if args.song:
        download_single(args.song)
    elif args.playlist:
        download_playlist(args.playlist, args.limit)
    else:
        print("用法: python3 downloader.py -s <歌曲ID> | -p <歌单ID>")
        print("示例: python3 downloader.py -p 3778678  # 下载热歌榜")
