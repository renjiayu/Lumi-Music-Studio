"""
UnblockNeteaseMusic 生命周期管理

自动启动/停止 unblockneteasemusic 进程，为 GStreamer/请求库提供代理配置。
"""
import os
import subprocess
import sys
import threading
from pathlib import Path

# 确保能找到 api 模块（for api.c）
sys.path.insert(0, str(Path(__file__).parent))
import config as _cfg

_proc = None
_unblock_port = 0
_started = False
_lock = threading.Lock()


def _find_binary() -> str:
    """查找 unblockneteasemusic 可执行文件"""
    candidates = [
        "unblockneteasemusic",
        str(Path.home() / ".local" / "bin" / "unblockneteasemusic"),
        "/usr/local/bin/unblockneteasemusic",
        "/usr/bin/unblockneteasemusic",
    ]
    for c in candidates:
        try:
            result = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return c
        except (FileNotFoundError, PermissionError, subprocess.TimeoutExpired):
            continue
    return ""


def start() -> bool:
    """启动 unblockneteasemusic 进程（守护式）"""
    global _proc, _unblock_port, _started
    with _lock:
        if _started:
            return True
        cfg = _cfg.load()
        if not cfg.get("unblock", True):
            return False
        port = int(cfg.get("unblock_port", 5200))

        binary = _find_binary()
        if not binary:
            print("  ⚠ UnblockNeteaseMusic 未安装 (npm i -g @unblockneteasemusic/server)", file=sys.stderr)
            return False

        try:
            _proc = subprocess.Popen(
                [binary, "-p", str(port), "-a", "127.0.0.1"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            # 等待进程启动成功
            import time as _time
            for _ in range(50):
                if _proc.poll() is not None:
                    raise RuntimeError(f"进程已退出 (code={_proc.returncode})")
                try:
                    s = __import__("socket").socket()
                    s.settimeout(0.1)
                    s.connect(("127.0.0.1", port))
                    s.close()
                    break
                except (ConnectionRefusedError, OSError):
                    _time.sleep(0.1)
            else:
                raise RuntimeError("启动超时")
            _unblock_port = port
            _started = True
            return True
        except Exception as e:
            print(f"  ✗ UnblockNeteaseMusic 启动失败: {e}", file=sys.stderr)
            return False


def stop():
    """停止 unblockneteasemusic 进程"""
    global _proc, _started
    with _lock:
        if _proc is not None and _proc.poll() is None:
            _proc.terminate()
            try:
                _proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _proc.kill()
                _proc.wait()
        _proc = None
        _started = False


def proxy_url() -> str:
    """返回 http_proxy 格式的地址，供 souphttpsrc 或 requests 使用"""
    with _lock:
        if not _started or _unblock_port == 0:
            return ""
        return f"http://127.0.0.1:{_unblock_port}"


def proxy_for_requests() -> dict:
    """返回 requests 库用的 proxies 字典"""
    url = proxy_url()
    if not url:
        return {}
    return {"http": url, "https": url}
