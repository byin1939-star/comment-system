#!/usr/bin/env python3
"""批量补传评论记录到 Google Sheets"""

import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.request
from pathlib import Path
from datetime import datetime


def get_webhook_url():
    """从环境变量或本地配置读取 Google Sheets Webhook。"""
    env_url = os.getenv("GOOGLE_SHEETS_WEBHOOK_URL", "").strip()
    if env_url:
        return env_url

    config_path = Path("monitor_config.json")
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        return config.get("google_sheets", {}).get("webhook_url", "").strip()

    return ""


def sync_one(row, ctx, webhook_url):
    """同步一条记录"""
    post_id, title, comment, nickname, created_at = row
    # 日期格式 2026-04-22 -> 2026/4/22
    date_str = created_at.split(' ')[0] if created_at else ''
    if date_str:
        y, m, d = date_str.split('-')
        date_str = f'{y}/{int(m)}/{int(d)}'

    data = json.dumps({
        'post_id': post_id or '',
        'nickname': nickname or '',
        'comment': comment or '',
        'date': date_str,
        'title': title or '',
    }, ensure_ascii=False).encode('utf-8')

    req = urllib.request.Request(
        webhook_url, data=data,
        headers={'Content-Type': 'application/json'}, method='POST',
    )
    resp = urllib.request.urlopen(req, timeout=20, context=ctx)
    return resp.status == 200


def main():
    # 获取起始日期参数（默认 2026-04-08）
    start_date = sys.argv[1] if len(sys.argv) > 1 else '2026-04-08'
    webhook_url = get_webhook_url()
    if not webhook_url:
        print("错误：未配置 Google Sheets Webhook。")
        print("请在 monitor_config.json 的 google_sheets.webhook_url 填写，")
        print("或设置环境变量 GOOGLE_SHEETS_WEBHOOK_URL。")
        sys.exit(1)

    conn = sqlite3.connect('monitor_comments.db')
    rows = conn.execute('''
        SELECT post_id, title, comment, nickname, created_at
        FROM processed_posts
        WHERE status='success'
          AND comment != '(见日志)' AND comment != '(失败)'
          AND comment != '' AND comment IS NOT NULL
          AND date(created_at) >= ?
        ORDER BY created_at ASC
    ''', (start_date,)).fetchall()
    conn.close()

    total = len(rows)
    print(f'准备同步 {total} 条记录（从 {start_date} 起）...')
    print()

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    ok = 0
    failed = 0
    start = time.time()

    for i, row in enumerate(rows, 1):
        # 重试 3 次
        for attempt in range(3):
            try:
                if sync_one(row, ctx, webhook_url):
                    ok += 1
                    break
            except Exception as e:
                if attempt == 2:
                    failed += 1
                    print(f'  [失败 {i}/{total}] {e}')
                else:
                    time.sleep(2)

        # 每 20 条显示进度
        if i % 20 == 0 or i == total:
            elapsed = time.time() - start
            rate = i / elapsed if elapsed > 0 else 0
            eta = (total - i) / rate if rate > 0 else 0
            print(f'进度 {i}/{total} ({i*100//total}%) | 成功 {ok} 失败 {failed} | '
                  f'{rate:.1f}条/秒 | 预计剩余 {eta:.0f}秒')

        # 限速：避免触发 Google 限流
        time.sleep(0.3)

    print()
    print(f'=== 完成 ===')
    print(f'总计: {total}, 成功: {ok}, 失败: {failed}')
    print(f'耗时: {time.time()-start:.0f}秒')


if __name__ == '__main__':
    main()
