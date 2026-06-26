"""配置持久化: ~/.config/lumi-music/config.json"""
import json
import os
import threading
from pathlib import Path
import tempfile

# 支持 LUMI_MUSIC_ROOT 环境变量 (类似 MUSICFOX_ROOT)
_ROOT_OVERRIDE = os.environ.get("LUMI_MUSIC_ROOT")
if _ROOT_OVERRIDE:
    CONFIG_DIR = Path(_ROOT_OVERRIDE)
else:
    CONFIG_DIR = Path.home() / ".config" / "lumi-music"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULTS = {
    "music_u": "",
    "default_br": 320000,
    "show_viz": False,
    "auto_next": True,
    "download_dir": str(Path.home() / "Music" / "网易云"),
    "mpris": True,
    "unblock": True,         # 自动拉起 UnblockNeteaseMusic
    "unblock_port": 5200,    # 监听端口
    "device_id": "",         # WeAPI 设备 ID (首次运行时自动生成)
}

_cache = None
_cache_lock = threading.Lock()
_warned_permission = False


def load() -> dict:
    """返回配置的副本, 防止调用者意外修改全局缓存."""
    global _cache, _warned_permission
    with _cache_lock:
        if _cache is not None:
            return dict(_cache)
        cfg = dict(DEFAULTS)
        if CONFIG_FILE.exists() and not _warned_permission:
            # 检查文件权限, 提醒用户保护可能存储的 cookie
            try:
                file_mode = CONFIG_FILE.stat().st_mode & 0o777
                if file_mode != 0o600:
                    _warned_permission = True
                    print(
                        f"  ⚠ 配置文件权限 {oct(file_mode)}, 建议设为 0600 保护登录凭证: "
                        f"chmod 600 {CONFIG_FILE}"
                    )
            except OSError:
                pass
            try:
                with open(CONFIG_FILE, encoding="utf-8") as f:
                    cfg.update(json.load(f))
            except (json.JSONDecodeError, OSError):
                pass
        _cache = cfg
        return dict(_cache)


def save(cfg: dict):
    """原子写入: 先写临时文件再 rename, 避免进程被杀时配置文件损坏。
    写入后设置权限 0600 (仅所有者可读写), 保护可能存储的 cookie。"""
    global _cache
    with _cache_lock:
        _cache = {**DEFAULTS, **cfg}
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=str(CONFIG_DIR), suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(_cache, f, ensure_ascii=False, indent=2)
            # 设置仅所有者可读写 (保护可能存储的 MUSIC_U cookie)
            os.chmod(tmp_path, 0o600)
            os.replace(tmp_path, CONFIG_FILE)
        except Exception:
            # 清理临时文件, 保留旧配置
            try:
                os.unlink(tmp_path)
            except Exception:
                pass
            raise


def get(key: str):
    return load().get(key, DEFAULTS.get(key))


def set_key(key: str, value):
    """设置单个配置键。注意: load → 修改 → save 不是原子操作,
    多线程并发调用时可能有竞态。当前 CLI 单线程使用, 无实际风险。"""
    cfg = load()
    cfg[key] = value
    save(cfg)
