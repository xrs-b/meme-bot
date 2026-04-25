#!/bin/bash
# Meme Bot 服务管理脚本
# 用法: ./manage.sh [start|stop|restart|status|logs|log]

set -e

SCANNER_SVC="meme-bot-scanner.service"
TRADING_SVC="meme-bot-trading.service"
SCANNER_LOG="/tmp/meme-bot-scanner.log"
TRADING_LOG="/tmp/meme-bot-trading.log"

function start() {
    echo "🚀 启动 Meme Bot 服务..."
    systemctl daemon-reload
    systemctl start $SCANNER_SVC
    systemctl start $TRADING_SVC
    sleep 2
    status
}

function stop() {
    echo "🛑 停止 Meme Bot 服务..."
    systemctl stop $TRADING_SVC 2>/dev/null || true
    systemctl stop $SCANNER_SVC 2>/dev/null || true
    echo "✅ 已停止"
}

function restart() {
    echo "🔄 重启 Meme Bot 服务..."
    systemctl restart $SCANNER_SVC
    systemctl restart $TRADING_SVC
    sleep 2
    status
}

function status() {
    echo ""
    echo "📊 Scanner 服务状态:"
    systemctl is-active --quiet $SCANNER_SVC && echo "  ✅ Scanner: 运行中" || echo "  ❌ Scanner: 未运行"
    echo "📊 Trading 服务状态:"
    systemctl is-active --quiet $TRADING_SVC && echo "  ✅ Trading: 运行中" || echo "  ❌ Trading: 未运行"
    echo ""
    echo "📋 最近日志 (Scanner):"
    tail -3 $SCANNER_LOG 2>/dev/null || echo "  (无日志)"
    echo ""
    echo "📋 最近日志 (Trading):"
    tail -3 $TRADING_LOG 2>/dev/null || echo "  (无日志)"
}

function logs() {
    echo "📋 Scanner 日志 (实时，按 Ctrl+C 退出):"
    tail -f $SCANNER_LOG
}

function log() {
    echo "📋 Trading 日志 (实时，按 Ctrl+C 退出):"
    tail -f $TRADING_LOG
}

function enable() {
    echo "✅ 设置开机自启动..."
    systemctl daemon-reload
    systemctl enable $SCANNER_SVC
    systemctl enable $TRADING_SVC
    echo "✅ 完成。下次开机自动启动。"
}

case "$1" in
    start)    start ;;
    stop)     stop ;;
    restart)  restart ;;
    status)   status ;;
    logs)     logs ;;
    log)      log ;;
    enable)   enable ;;
    *)
        echo "用法: $0 {start|stop|restart|status|logs|log|enable}"
        echo ""
        echo "  start   - 启动所有服务"
        echo "  stop    - 停止所有服务"
        echo "  restart - 重启所有服务"
        echo "  status  - 查看状态和最近日志"
        echo "  logs    - 查看 Scanner 日志 (实时)"
        echo "  log     - 查看 Trading 日志 (实时)"
        echo "  enable  - 设置开机自启动"
        exit 1
        ;;
esac
