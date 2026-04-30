#!/usr/bin/env python3
"""
dashboard.py - 评论系统 Web 控制面板

启动方式: python dashboard.py
打开浏览器访问: http://localhost:5001
"""

import json
import sqlite3
import threading
import time
import webbrowser
from collections import deque
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from comment_poster import (
    KpiDatabase,
    PostDatabase,
    SentimentDatabase,
    load_config,
    logger,
    run_posting_cycle,
    setup_logging,
)

app = Flask(__name__)

# ============================================================
# 全局状态
# ============================================================

CONFIG_PATH = "monitor_config.json"
monitor_state = {
    "running": False,
    "thread": None,
    "cycle_count": 0,
    "last_error": "",
    "current_action": "idle",
}
monitor_lock = threading.Lock()

# 内存日志缓冲（最近500行），供前端实时读取
log_buffer = deque(maxlen=500)


class BufferLogHandler:
    """将日志同时写入内存缓冲"""

    def __init__(self, buffer):
        self.buffer = buffer

    def write(self, msg):
        if msg.strip():
            self.buffer.append(msg.strip())

    def flush(self):
        pass


import logging


class DequeHandler(logging.Handler):
    def __init__(self, buffer):
        super().__init__()
        self.buffer = buffer

    def emit(self, record):
        self.buffer.append(self.format(record))


# ============================================================
# 后台监控线程
# ============================================================


def monitor_loop(config):
    """后台持续监控循环"""
    timing = config.get("timing", {})
    interval = timing.get("monitor_interval_minutes", 10)

    while monitor_state["running"]:
        monitor_state["cycle_count"] += 1
        monitor_state["current_action"] = f"第 {monitor_state['cycle_count']} 轮执行中"
        try:
            run_posting_cycle(config)
            monitor_state["last_error"] = ""
        except Exception as e:
            monitor_state["last_error"] = str(e)
            logger.error(f"监控循环异常: {e}", exc_info=True)

        if not monitor_state["running"]:
            break

        monitor_state["current_action"] = f"等待 {interval} 分钟..."
        for _ in range(interval * 60):
            if not monitor_state["running"]:
                break
            time.sleep(1)

    monitor_state["current_action"] = "idle"


# ============================================================
# 页面路由
# ============================================================


@app.route("/")
def index():
    return render_template("index.html")


# ============================================================
# API: 状态
# ============================================================


@app.route("/api/status")
def api_status():
    config = load_config(CONFIG_PATH)
    db = PostDatabase(config.get("database", {}).get("db_path", "monitor_comments.db"))
    stats = db.get_stats()
    return jsonify({
        "running": monitor_state["running"],
        "cycle_count": monitor_state["cycle_count"],
        "current_action": monitor_state["current_action"],
        "last_error": monitor_state["last_error"],
        "db_stats": stats,
    })


# ============================================================
# API: 启动 / 停止 / 单次执行
# ============================================================


@app.route("/api/start", methods=["POST"])
def api_start():
    with monitor_lock:
        thread = monitor_state.get("thread")
        if monitor_state["running"] or (thread and thread.is_alive()):
            return jsonify({"ok": False, "msg": "已在运行中"})

        config = load_config(CONFIG_PATH)
        monitor_state["running"] = True
        monitor_state["cycle_count"] = 0
        monitor_state["last_error"] = ""

        t = threading.Thread(target=monitor_loop, args=(config,), daemon=True)
        t.start()
        monitor_state["thread"] = t
    logger.info("Dashboard: 监控已启动")
    return jsonify({"ok": True, "msg": "监控已启动"})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    with monitor_lock:
        thread = monitor_state.get("thread")
        if not monitor_state["running"] and not (thread and thread.is_alive()):
            return jsonify({"ok": False, "msg": "当前未运行"})

        monitor_state["running"] = False
    logger.info("Dashboard: 监控已停止")
    return jsonify({"ok": True, "msg": "已发送停止信号，等待当前任务完成"})


@app.route("/api/run-once", methods=["POST"])
def api_run_once():
    if monitor_state["running"]:
        return jsonify({"ok": False, "msg": "监控正在运行，请先停止"})

    def run():
        monitor_state["running"] = True
        monitor_state["current_action"] = "单次执行中"
        try:
            config = load_config(CONFIG_PATH)
            run_posting_cycle(config)
        except Exception as e:
            monitor_state["last_error"] = str(e)
            logger.error(f"单次执行异常: {e}", exc_info=True)
        finally:
            monitor_state["running"] = False
            monitor_state["current_action"] = "idle"

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "单次执行已启动"})


# ============================================================
# API: 配置
# ============================================================


@app.route("/api/config")
def api_get_config():
    config = load_config(CONFIG_PATH)
    return jsonify(config)


@app.route("/api/config", methods=["POST"])
def api_save_config():
    try:
        new_config = request.get_json()
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(new_config, f, ensure_ascii=False, indent=4)
        return jsonify({"ok": True, "msg": "配置已保存"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


# ============================================================
# API: 评论库 (history_comments.txt)
# ============================================================


@app.route("/api/comments")
def api_get_comments():
    config = load_config(CONFIG_PATH)
    filepath = config.get("posting", {}).get("history_comments_file", "history_comments.txt")
    path = Path(filepath)
    content = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = [l for l in content.splitlines() if l.strip()]
    return jsonify({"content": content, "count": len(lines)})


@app.route("/api/comments", methods=["POST"])
def api_save_comments():
    try:
        data = request.get_json()
        content = data.get("content", "")
        config = load_config(CONFIG_PATH)
        filepath = config.get("posting", {}).get("history_comments_file", "history_comments.txt")
        Path(filepath).write_text(content, encoding="utf-8")
        lines = [l for l in content.splitlines() if l.strip()]
        return jsonify({"ok": True, "msg": f"已保存 {len(lines)} 条评论", "count": len(lines)})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


# ============================================================
# API: 评论记录
# ============================================================


@app.route("/api/records")
def api_records():
    config = load_config(CONFIG_PATH)
    db_path = config.get("database", {}).get("db_path", "monitor_comments.db")
    status_filter = request.args.get("status", "all")
    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))

    try:
        with sqlite3.connect(db_path) as conn:
            conn.row_factory = sqlite3.Row
            if status_filter == "all":
                rows = conn.execute(
                    "SELECT * FROM processed_posts ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()
                total = conn.execute("SELECT COUNT(*) FROM processed_posts").fetchone()[0]
            else:
                rows = conn.execute(
                    "SELECT * FROM processed_posts WHERE status=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    (status_filter, limit, offset),
                ).fetchall()
                total = conn.execute(
                    "SELECT COUNT(*) FROM processed_posts WHERE status=?", (status_filter,)
                ).fetchone()[0]

        records = [dict(r) for r in rows]
        return jsonify({"records": records, "total": total})
    except Exception as e:
        return jsonify({"records": [], "total": 0, "error": str(e)})


# ============================================================
# API: 舆论监控
# ============================================================


@app.route("/api/sentiment")
def api_sentiment():
    config = load_config(CONFIG_PATH)
    db_path = config.get("sentiment", {}).get("db_path", "sentiment_monitor.db")
    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))
    try:
        sdb = SentimentDatabase(db_path)
        records, total = sdb.get_records(limit, offset)
        stats = sdb.get_stats()
        return jsonify({"records": records, "total": total, "stats": stats})
    except Exception as e:
        return jsonify({"records": [], "total": 0, "stats": {}, "error": str(e)})


@app.route("/api/sentiment/config")
def api_sentiment_config():
    config = load_config(CONFIG_PATH)
    return jsonify(config.get("sentiment", {}))


@app.route("/api/sentiment/config", methods=["POST"])
def api_save_sentiment_config():
    try:
        data = request.get_json()
        config = load_config(CONFIG_PATH)
        config["sentiment"] = data
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False, indent=4)
        return jsonify({"ok": True, "msg": "舆论监控配置已保存"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


# ============================================================
# API: KPI 面板
# ============================================================


@app.route("/api/kpi")
def api_kpi():
    config = load_config(CONFIG_PATH)
    days = int(request.args.get("days", 30))
    db_path = config.get("database", {}).get("db_path", "monitor_comments.db")
    try:
        kpi = KpiDatabase(posts_db_path=db_path)
        data = kpi.get_daily_data(days)
        return jsonify({"data": data})
    except Exception as e:
        return jsonify({"data": [], "error": str(e)})


@app.route("/api/kpi", methods=["POST"])
def api_kpi_save():
    config = load_config(CONFIG_PATH)
    db_path = config.get("database", {}).get("db_path", "monitor_comments.db")
    try:
        body = request.get_json()
        date = body.get("date", "")
        manual_comments = int(body.get("manual_comments", 0))
        note = body.get("note", "")
        if not date:
            return jsonify({"ok": False, "msg": "日期不能为空"})
        kpi = KpiDatabase(posts_db_path=db_path)
        kpi.add_or_update(date, manual_comments, note)
        return jsonify({"ok": True, "msg": f"已保存 {date} 的记录"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/kpi", methods=["DELETE"])
def api_kpi_delete():
    config = load_config(CONFIG_PATH)
    db_path = config.get("database", {}).get("db_path", "monitor_comments.db")
    try:
        body = request.get_json()
        date = body.get("date", "")
        if not date:
            return jsonify({"ok": False, "msg": "日期不能为空"})
        kpi = KpiDatabase(posts_db_path=db_path)
        deleted = kpi.delete_record(date)
        if deleted:
            return jsonify({"ok": True, "msg": f"已删除 {date} 的记录"})
        else:
            return jsonify({"ok": False, "msg": f"{date} 无手动记录"})
    except Exception as e:
        return jsonify({"ok": False, "msg": str(e)})


@app.route("/api/kpi/summary")
def api_kpi_summary():
    config = load_config(CONFIG_PATH)
    db_path = config.get("database", {}).get("db_path", "monitor_comments.db")
    try:
        kpi = KpiDatabase(posts_db_path=db_path)
        summary = kpi.get_summary()
        return jsonify(summary)
    except Exception as e:
        return jsonify({"today": 0, "week": 0, "month": 0, "daily_avg": 0, "error": str(e)})


# ============================================================
# API: 日志
# ============================================================


@app.route("/api/logs")
def api_logs():
    lines = int(request.args.get("lines", 100))
    recent = list(log_buffer)[-lines:]
    return jsonify({"logs": recent, "total": len(log_buffer)})


# ============================================================
# 启动
# ============================================================


def init_app():
    """初始化日志 + 数据库"""
    config = load_config(CONFIG_PATH)
    setup_logging(config)

    # 将日志同时输出到内存缓冲供前端读取
    handler = DequeHandler(log_buffer)
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    ))
    logger.addHandler(handler)

    # 确保数据库初始化
    db_path = config.get("database", {}).get("db_path", "monitor_comments.db")
    PostDatabase(db_path)
    SentimentDatabase(config.get("sentiment", {}).get("db_path", "sentiment_monitor.db"))
    KpiDatabase(posts_db_path=db_path)

    logger.info("Dashboard 已初始化")


if __name__ == "__main__":
    init_app()
    print("\n" + "=" * 50)
    print("  评论系统控制面板")
    print("  打开浏览器访问: http://localhost:5001")
    print("=" * 50 + "\n")
    webbrowser.open("http://localhost:5001")
    app.run(host="0.0.0.0", port=5001, debug=False)
