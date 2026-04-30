#!/bin/bash

echo ""
echo "================================================"
echo "  评论系统 - 一键安装"
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
    echo "[错误] 未找到 Python，请先安装 Python 3.8+"
    echo "  Mac: brew install python3"
    echo "  官网: https://www.python.org/downloads/"
    exit 1
fi

echo "[1/3] 使用 Python: $($PYTHON --version)"
echo ""

# 安装依赖
echo "[2/3] 安装 Python 依赖..."
if [ -f requirements.txt ]; then
    $PYTHON -m pip install -r requirements.txt --quiet
else
    $PYTHON -m pip install playwright flask openai --quiet
fi
if [ $? -ne 0 ]; then
    echo "[错误] 依赖安装失败，请检查网络或 pip 是否可用"
    exit 1
fi
echo "      依赖安装完成 ✓"
echo ""

# 安装 Chromium
echo "[3/3] 安装 Chromium 浏览器（约 150MB，请耐心等待）..."
$PYTHON -m playwright install chromium
if [ $? -ne 0 ]; then
    echo "[错误] Chromium 安装失败，请检查网络"
    exit 1
fi
echo "      Chromium 安装完成 ✓"
echo ""

echo "================================================"
echo "  安装完成！"
echo ""
echo "  首次运行前会自动从示例文件生成本地配置。"
echo ""
echo "  启动方式："
echo "    Mac/Linux: bash start.sh"
echo "    或直接运行: $PYTHON dashboard.py"
echo ""
echo "  启动后打开浏览器访问: http://localhost:5001"
echo "================================================"
echo ""
