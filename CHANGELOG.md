# Changelog

## [2.1.0] — 2026-06-27

### Fixed
- **扫码登录**: 修复网易云新增的 8821 安全校验问题，参照 musicfox 实现:
  - 请求参数添加 `noCheckToken: true`
  - 请求前注入 `os=pc` + `NMTID` + `appver` cookie
  - 二维码 URL 添加 `chainId` 参数
- **HTTP 超时**: 所有 API 请求添加 timeout，`_get`/`_post_weapi` 超时返回错误码而非永久阻塞
- **扫码轮询**: 弹窗不再卡死，网络错误有提示
- **中文弹窗宽度**: 新增 `_display_width()` 正确计算 CJK 字符终端宽度

### Added
- **GitHub Actions CI**: ruff 检查 + 语法校验
- **`.editorconfig`**: 统一编辑器缩进配置
- **`debug_qr.py`**: 扫码登录调试脚本

### Changed
- `requirements.txt`: 补上缺失的 `qrcode` 依赖
- `pyproject.toml`: 修复 build backend (`setuptools.build_meta`)
- `.gitignore`: 补充 IDE/OS/临时文件忽略规则
- 清理多处未使用的导入和变量

## [2.0.0] — 2026-06-27

### Added
- **扫码登录**: `Shift+Q` 弹出二维码，手机网易云 APP 扫码登录
- **Cookie Jar**: 完整会话持久化 (`MUSIC_U` + `__csrf` + `MUSIC_A`)，自动刷新 Token
- **断点续传**: 退出自动保存播放状态，下次启动恢复
- **MPRIS**: Linux 桌面媒体控制集成
- **频谱可视化**: GStreamer spectrum 插件实时渲染
- **同步歌词**: 时间轴 LRC 歌词 + 翻译
- **搜索**: 歌曲/专辑/歌手/歌单综合搜索
- **排行榜**: 官方排行榜 + 热门歌单
- **我的歌单**: 登录后查看收藏歌单
- **下载**: 多线程下载 (320kbps 优先)，自动 ID3 标签

### Changed
- CLAUDE.md 快捷键标签改为中文
- 扫码登录快捷键从 `Shift+L` 改为 `Shift+Q`

### Fixed
- 设备 ID 不注入 login 系列端点
- 暂停和扫码登录崩溃问题
- 15 个 code review 发现的 bug

## [1.0.0] — 2026-06-27

### Added
- 初始版本发布
- GStreamer 流式播放
- Curses TUI Tokyo Night 暗色主题
- 搜索/歌单/歌词基础功能
