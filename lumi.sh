#!/bin/bash
# Lumi Music Studio — 一键启动/安装脚本
# 用法: ./lumi.sh [command]
# 命令: install, remove, 或无参数时启动 TUI

set -e

APP_NAME="lumi-music-studio"
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
BIN_DIR="${LUMI_MUSIC_ROOT:-$HOME/.local/share/lumi-music}"
LINK_DIR="$HOME/.local/bin"

install_local() {
    echo "  📦 安装 $APP_NAME..."

    # 安装 Python 依赖
    pip install -q -e "$PROJECT_DIR" 2>/dev/null || pip install --break-system-packages -q -e "$PROJECT_DIR" 2>/dev/null || true

    # 确保 ~/.local/bin 在 PATH 中
    mkdir -p "$LINK_DIR"

    # 创建启动脚本
    cat > "$LINK_DIR/lumi" << SCRIPT
#!/bin/bash
export LUMI_MUSIC_ROOT="\${LUMI_MUSIC_ROOT:-$HOME/.local/share/lumi-music}"
exec python3 "$PROJECT_DIR/tui.py" "\$@"
SCRIPT
    chmod +x "$LINK_DIR/lumi"

    echo "  ✓ 已安装: lumi"
    echo "  💡 将 $LINK_DIR 加入 PATH 即可使用"
}

remove_local() {
    echo "  🗑️  卸载 $APP_NAME..."
    rm -f "$LINK_DIR/lumi"
    echo "  ✓ 已卸载"
}

case "${1:-}" in
    install)
        install_local
        ;;
    remove|uninstall)
        remove_local
        ;;
    *)
        # 缺省: 确保安装 + 启动 TUI
        install_local
        echo
        exec "$LINK_DIR/lumi"
        ;;
esac
