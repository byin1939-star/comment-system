#!/usr/bin/env python3
"""
comment_monitor.py - 论坛自动回帖系统主入口

功能：
- 定时循环执行抓取+回帖流程
- 支持命令行参数控制
- 支持单次运行或持续监控模式

用法：
    python comment_monitor.py                  # 持续监控模式
    python comment_monitor.py --once           # 单次执行
    python comment_monitor.py --config my.json # 指定配置文件
    python comment_monitor.py --visible        # 有头浏览器模式（调试用）
"""

import argparse
import signal
import sys
import time

from comment_poster import (
    PostDatabase,
    load_config,
    logger,
    run_posting_cycle,
    setup_logging,
)

# 全局停止标志
_running = True


def signal_handler(sig, frame):
    global _running
    logger.info("收到停止信号，等待当前任务完成后退出...")
    _running = False


def parse_args():
    parser = argparse.ArgumentParser(description="论坛自动回帖监控系统")
    parser.add_argument(
        "--config", default="monitor_config.json", help="配置文件路径 (默认: monitor_config.json)"
    )
    parser.add_argument(
        "--once", action="store_true", help="仅执行一次后退出"
    )
    parser.add_argument(
        "--visible", action="store_true", help="使用有头浏览器模式（调试用）"
    )
    parser.add_argument(
        "--stats", action="store_true", help="仅显示统计信息后退出"
    )
    return parser.parse_args()


def get_cycle_interval_seconds(config):
    timing = config.get("timing", {})
    seconds = timing.get("monitor_interval_seconds")
    if seconds in (None, ""):
        seconds = float(timing.get("monitor_interval_minutes", 10)) * 60
    return max(1, int(float(seconds)))


def main():
    global _running

    args = parse_args()

    # 加载配置
    try:
        config = load_config(args.config)
    except FileNotFoundError:
        print(f"错误：配置文件 {args.config} 不存在")
        sys.exit(1)
    except Exception as e:
        print(f"错误：配置文件加载失败: {e}")
        sys.exit(1)

    # 初始化日志
    setup_logging(config)
    logger.info("=" * 50)
    logger.info("论坛自动回帖系统启动")
    logger.info("=" * 50)

    # 命令行覆盖配置
    if args.visible:
        config["browser"]["headless"] = False
        logger.info("已切换为有头浏览器模式")

    # 仅查看统计
    if args.stats:
        db = PostDatabase(config.get("database", {}).get("db_path", "monitor_comments.db"))
        stats = db.get_stats()
        print(f"历史统计: 总计={stats['total']}, 成功={stats['success']}, 失败={stats['failed']}")
        return

    # 注册信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    interval_seconds = get_cycle_interval_seconds(config)

    if args.once:
        # 单次执行
        logger.info("单次执行模式")
        run_posting_cycle(config)
        logger.info("单次执行完成")
    else:
        # 持续监控循环
        logger.info(f"持续监控模式，间隔 {interval_seconds} 秒")
        cycle = 0

        while _running:
            cycle += 1
            logger.info(f"===== 第 {cycle} 轮开始 =====")

            try:
                run_posting_cycle(config)
            except Exception as e:
                logger.error(f"第 {cycle} 轮执行异常: {e}", exc_info=True)

            if not _running:
                break

            logger.info(f"等待 {interval_seconds} 秒后开始下一轮...")

            # 分段 sleep 以便及时响应停止信号
            for _ in range(interval_seconds):
                if not _running:
                    break
                time.sleep(1)

        logger.info("监控系统已安全退出")


if __name__ == "__main__":
    main()
