#!/bin/bash

echo ""
PID=$(pgrep -f "dashboard.py")
if [ -z "$PID" ]; then
    echo "评论系统未在运行"
else
    kill $PID
    echo "评论系统已停止 (PID: $PID)"
fi
echo ""
