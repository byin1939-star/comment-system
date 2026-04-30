#!/bin/bash

echo ""
echo "================================================"
echo "  评论系统 - 启动"
echo "================================================"
echo ""

# 检测 Python
if command -v /usr/local/bin/python3 &>/dev/null; then
    PYTHON=/usr/local/bin/python3
elif command -v python3 &>/dev/null; then
    PYTHON=python3
elif command -v python &>/dev/null; then
    PYTHON=python
else
    echo "[错误] 未找到 Python，请先运行 install.sh"
    exit 1
fi

# 切换到脚本所在目录
cd "$(dirname "$0")"

ulimit -n 4096 2>/dev/null || true

if [ ! -f monitor_config.json ] && [ -f monitor_config.example.json ]; then
    cp monitor_config.example.json monitor_config.json
    echo "[提示] 已生成本地配置: monitor_config.json"
    echo "       请按需填写目标站点、API Key 和 Webhook"
fi

if [ ! -f history_comments.txt ] && [ -f history_comments.example.txt ]; then
    cp history_comments.example.txt history_comments.txt
    echo "[提示] 已生成本地评论库: history_comments.txt"
fi

# 检查是否已在运行
if pgrep -f "dashboard.py" > /dev/null; then
    echo "[提示] 系统已在运行中"
    echo "  打开浏览器访问: http://localhost:5001"
    open "http://localhost:5001" 2>/dev/null || true
    exit 0
fi

echo "正在启动评论系统..."
nohup $PYTHON dashboard.py > dashboard.out 2>&1 &
PID=$!
echo "  进程 PID: $PID"

# 等待启动
sleep 2
if kill -0 $PID 2>/dev/null; then
    echo "  启动成功 ✓"
    echo ""
    echo "  浏览器访问: http://localhost:5001"
    echo "  日志文件:   dashboard.out"
    echo ""
    open "http://localhost:5001" 2>/dev/null || true
else
    echo "[错误] 启动失败，查看 dashboard.out 了解详情"
    cat dashboard.out 2>/dev/null
fi
echo "================================================"
echo ""
