"""MPRIS D-Bus 媒体控制 (Linux 桌面集成, 可选)"""
import threading

_active = False
_thread = None
_bus = None
_player = None
_cli = None  # set by start()


def _available() -> bool:
    try:
        import dbus  # noqa: F401
        return True
    except ImportError:
        return False


def start(cli_module):
    """启动 MPRIS 服务; cli_module 为 cli 模块引用"""
    global _active, _thread, _cli
    _cli = cli_module
    if _active or not _available():
        return False
    try:
        import config
        if not config.get("mpris"):
            return False
    except ImportError:
        pass

    # 模块级导入 api 到闭包内, 避免 D-Bus handler 中动态 import
    import api as _api_module

    def _run():
        global _bus, _player, _active
        try:
            import dbus
            import dbus.service
            from dbus.mainloop.glib import DBusGMainLoop
            from gi.repository import GLib

            DBusGMainLoop(set_as_default=True)
            _bus = dbus.SessionBus()
            name = dbus.service.BusName(
                "org.mpris.MediaPlayer2.lumi", _bus)

            class Player(dbus.service.Object):
                def __init__(self):
                    super().__init__(_bus, "/org/mpris/MediaPlayer2")

                @dbus.service.method("org.freedesktop.DBus.Properties",
                                     in_signature="ss", out_signature="v")
                def Get(self, interface, prop):
                    return self.GetAll(interface)[prop]

                @dbus.service.method("org.freedesktop.DBus.Properties",
                                     in_signature="s", out_signature="a{sv}")
                def GetAll(self, interface):
                    import dbus
                    if interface == "org.mpris.MediaPlayer2":
                        return {
                            "CanQuit": False,
                            "CanRaise": False,
                            "HasTrackList": False,
                            "Identity": "Lumi Music Studio",
                            "DesktopEntry": "lumi-music-studio",
                            "SupportedUriSchemes": dbus.Array([], signature="s"),
                            "SupportedMimeTypes": dbus.Array([], signature="s"),
                        }
                    if interface != "org.mpris.MediaPlayer2.Player":
                        return {}
                    # 使用 cli 模块的 get_position_ms() 而非直接读 _position_ms
                    pos_us = int(cli_module.get_position_ms() * 1000)
                    # 读取 _playing/_paused 时使用锁保证一致性
                    with cli_module._position_lock:
                        playing = cli_module._playing
                        paused = cli_module._paused
                    status = "Stopped"
                    if playing:
                        status = "Paused" if paused else "Playing"
                    meta = {
                        "xesam:title": cli_module.get_now_playing_title(),
                        "xesam:artist": dbus.Array(
                            [cli_module.get_now_playing_artist()], signature="s"),
                        "xesam:album": cli_module.get_now_playing_album(),
                        "mpris:length": dbus.Int64(
                            cli_module.get_duration_ms() * 1000),
                    }
                    return {
                        "PlaybackStatus": status,
                        "LoopStatus": cli_module.get_loop_mpris(),
                        "Shuffle": cli_module._shuffle,
                        "Volume": 1.0,
                        "Position": dbus.Int64(pos_us),
                        "MinimumRate": 1.0,
                        "MaximumRate": 1.0,
                        "Rate": 1.0,
                        "CanControl": True,
                        "CanPlay": True,
                        "CanPause": True,
                        "CanSeek": cli_module.can_seek(),
                        "CanGoNext": cli_module._play_ctx is not None,
                        "CanGoPrevious": len(cli_module._history) >= 2,
                        "Metadata": meta,
                    }

                @dbus.service.method("org.mpris.MediaPlayer2.Player")
                def Play(self):
                    if cli_module._playing and cli_module._paused:
                        cli_module.toggle_pause()
                    elif not cli_module._playing and cli_module._play_ctx:
                        ctx = cli_module._play_ctx
                        songs = ctx["songs"]
                        idx = ctx["order"][ctx["index"]]
                        ns = _api_module.normalize_song(songs[idx])
                        cli_module.play_song(ns["id"], ns["name"], ctx=ctx)

                @dbus.service.method("org.mpris.MediaPlayer2.Player")
                def Pause(self):
                    if cli_module._playing and not cli_module._paused:
                        cli_module.toggle_pause()

                @dbus.service.method("org.mpris.MediaPlayer2.Player")
                def PlayPause(self):
                    if cli_module._playing:
                        cli_module.toggle_pause()
                    else:
                        self.Play()

                @dbus.service.method("org.mpris.MediaPlayer2.Player")
                def Stop(self):
                    cli_module.stop()

                @dbus.service.method("org.mpris.MediaPlayer2.Player")
                def Next(self):
                    cli_module.play_next()

                @dbus.service.method("org.mpris.MediaPlayer2.Player")
                def Previous(self):
                    cli_module.play_prev()

                @dbus.service.method("org.mpris.MediaPlayer2.Player",
                                     in_signature="x")
                def Seek(self, offset_us):
                    cli_module.seek_relative(int(offset_us / 1000))

                @dbus.service.method("org.mpris.MediaPlayer2.Player",
                                     in_signature="ox")
                def SetPosition(self, track_id, position_us):
                    cli_module.seek_to(position_us // 1000)

                @dbus.service.signal("org.freedesktop.DBus.Properties",
                                     signature="sa{sv}as")
                def PropertiesChanged(self, interface, changed, invalidated):
                    pass

            _player = Player()
            _active = True
            loop = GLib.MainLoop()
            loop.run()
        except Exception:
            _active = False

    _thread = threading.Thread(target=_run, daemon=True)
    _thread.start()
    return True


def stop():
    global _active
    _active = False


def emit_properties_changed():
    """通知桌面环境播放状态已变"""
    if _player is None or _cli is None:
        return
    try:
        import dbus
        # 直接获取最新状态，避免 stale
        with _cli._position_lock:
            playing = _cli._playing
            paused = _cli._paused
        status = "Stopped"
        if playing:
            status = "Paused" if paused else "Playing"
        meta = {
            "xesam:title": _cli.get_now_playing_title(),
            "xesam:artist": dbus.Array(
                [_cli.get_now_playing_artist()], signature="s"),
            "xesam:album": _cli.get_now_playing_album(),
            "mpris:length": dbus.Int64(
                _cli.get_duration_ms() * 1000),
        }
        _player.PropertiesChanged(
            "org.mpris.MediaPlayer2.Player",
            {
                "PlaybackStatus": status,
                "Metadata": meta,
                "Position": dbus.Int64(int(_cli.get_position_ms() * 1000)),
            },
            [],
        )
    except Exception:
        pass
