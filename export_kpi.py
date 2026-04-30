#!/usr/bin/env python3
"""生成独立的 KPI 面板 HTML 文件，可直接发到群里打开查看"""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path


def get_kpi_data(days=30):
    """获取KPI数据"""
    # 自动评论
    auto_stats = {}
    try:
        with sqlite3.connect("monitor_comments.db") as conn:
            rows = conn.execute(
                """SELECT date(created_at) as d, COUNT(*) as c
                   FROM processed_posts WHERE status='success'
                   AND date(created_at) >= date('now', ?)
                   GROUP BY date(created_at)""",
                (f"-{days} days",),
            ).fetchall()
            for row in rows:
                auto_stats[row[0]] = row[1]
    except Exception:
        pass

    # 手动评论
    manual_stats = {}
    try:
        with sqlite3.connect("kpi_data.db") as conn:
            rows = conn.execute(
                "SELECT date, manual_comments, note FROM kpi_manual WHERE date >= date('now', ?)",
                (f"-{days} days",),
            ).fetchall()
            for row in rows:
                manual_stats[row[0]] = {"count": row[1], "note": row[2]}
    except Exception:
        pass

    # 合并
    today = datetime.now().date()
    result = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        auto = auto_stats.get(d_str, 0)
        m = manual_stats.get(d_str, {"count": 0, "note": ""})
        result.append({
            "date": d_str,
            "auto": auto,
            "manual": m["count"],
            "total": auto + m["count"],
            "note": m["note"],
        })
    return result


def get_summary(data):
    today = datetime.now().date()
    today_str = today.strftime("%Y-%m-%d")
    week_start = today - timedelta(days=today.weekday())
    month_start = today.replace(day=1)

    today_total = 0
    week_total = 0
    month_total = 0

    for item in data:
        d = datetime.strptime(item["date"], "%Y-%m-%d").date()
        if item["date"] == today_str:
            today_total = item["total"]
        if d >= week_start:
            week_total += item["total"]
        if d >= month_start:
            month_total += item["total"]

    non_zero = sum(1 for item in data if item["total"] > 0)
    total_all = sum(item["total"] for item in data)
    daily_avg = round(total_all / max(non_zero, 1), 1)

    return {
        "today": today_total,
        "week": week_total,
        "month": month_total,
        "avg": daily_avg,
    }


def generate_html(data, summary):
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    labels = json.dumps([d["date"][5:] for d in data])
    auto_data = json.dumps([d["auto"] for d in data])
    manual_data = json.dumps([d["manual"] for d in data])
    total_data = json.dumps([d["total"] for d in data])

    # 有数据的行
    table_rows = ""
    for d in reversed(data):
        if d["auto"] == 0 and d["manual"] == 0:
            continue
        note_html = f'<span style="color:#a0a0b0">{d["note"]}</span>' if d["note"] else ""
        table_rows += f"""<tr>
            <td>{d["date"]}</td>
            <td style="color:#42a5f5">{d["auto"]}</td>
            <td style="color:#ffa726">{d["manual"]}</td>
            <td style="font-weight:700;color:#e94560">{d["total"]}</td>
            <td>{note_html}</td>
        </tr>\n"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>KPI 面板 - {now}</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
:root {{
  --bg: #1a1a2e; --bg2: #16213e; --bg3: #0f3460;
  --text: #e0e0e0; --text2: #a0a0b0; --accent: #e94560;
  --green: #00c853; --yellow: #ffd600; --border: #2a2a4a;
}}
body {{ font-family: -apple-system, 'Segoe UI', sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; padding: 24px; }}
.header {{ text-align:center; margin-bottom:24px; }}
.header h1 {{ font-size:22px; margin-bottom:6px; }}
.header h1 span {{ color: var(--accent); }}
.header .time {{ color: var(--text2); font-size:13px; }}
.stats-row {{ display:grid; grid-template-columns:repeat(4,1fr); gap:16px; margin-bottom:24px; max-width:900px; margin-left:auto; margin-right:auto; }}
.stat-card {{ background:var(--bg2); border:1px solid var(--border); border-radius:8px; padding:20px; text-align:center; }}
.stat-num {{ font-size:36px; font-weight:700; }}
.stat-num.green {{ color:var(--green); }}
.stat-num.yellow {{ color:var(--yellow); }}
.stat-label {{ font-size:12px; color:var(--text2); margin-top:4px; }}
.card {{ background:var(--bg2); border:1px solid var(--border); border-radius:8px; padding:20px; margin-bottom:20px; max-width:900px; margin-left:auto; margin-right:auto; }}
.card-title {{ font-size:14px; color:var(--text2); margin-bottom:12px; text-transform:uppercase; letter-spacing:1px; }}
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ background:var(--bg3); padding:10px 12px; text-align:left; color:var(--text2); font-weight:600; }}
td {{ padding:10px 12px; border-bottom:1px solid var(--border); }}
tr:hover td {{ background:rgba(233,69,96,0.05); }}
</style>
</head>
<body>
<div class="header">
  <h1><span>&#9670;</span> KPI 面板</h1>
  <div class="time">生成时间：{now}</div>
</div>

<div class="stats-row">
  <div class="stat-card"><div class="stat-num green">{summary["today"]}</div><div class="stat-label">今日总评论</div></div>
  <div class="stat-card"><div class="stat-num">{summary["week"]}</div><div class="stat-label">本周总评论</div></div>
  <div class="stat-card"><div class="stat-num yellow">{summary["month"]}</div><div class="stat-label">本月总评论</div></div>
  <div class="stat-card"><div class="stat-num" style="color:#64b5f6">{summary["avg"]}</div><div class="stat-label">日均评论</div></div>
</div>

<div class="card">
  <div class="card-title">评论趋势图（最近30天）</div>
  <div style="position:relative;height:320px;">
    <canvas id="chart"></canvas>
  </div>
</div>

<div class="card">
  <div class="card-title">每日明细</div>
  <table>
    <thead><tr><th>日期</th><th>自动评论</th><th>官方后台</th><th>总计</th><th>备注</th></tr></thead>
    <tbody>{table_rows}</tbody>
  </table>
</div>

<script>
new Chart(document.getElementById('chart').getContext('2d'), {{
  type: 'bar',
  data: {{
    labels: {labels},
    datasets: [
      {{ label:'自动评论', data:{auto_data}, backgroundColor:'rgba(66,165,245,0.7)', borderColor:'rgba(66,165,245,1)', borderWidth:1, stack:'s', order:2 }},
      {{ label:'官方后台', data:{manual_data}, backgroundColor:'rgba(255,167,38,0.7)', borderColor:'rgba(255,167,38,1)', borderWidth:1, stack:'s', order:2 }},
      {{ label:'总计趋势', data:{total_data}, type:'line', borderColor:'#e94560', backgroundColor:'rgba(233,69,96,0.1)', borderWidth:2, pointRadius:3, pointBackgroundColor:'#e94560', fill:false, tension:0.3, order:1 }}
    ]
  }},
  options: {{
    responsive:true, maintainAspectRatio:false,
    plugins: {{ legend: {{ labels: {{ color:'#a0a0b0', font:{{size:12}} }} }} }},
    scales: {{
      x: {{ ticks:{{color:'#a0a0b0',font:{{size:11}},maxRotation:45}}, grid:{{color:'rgba(255,255,255,0.05)'}}, stacked:true }},
      y: {{ ticks:{{color:'#a0a0b0',beginAtZero:true}}, grid:{{color:'rgba(255,255,255,0.08)'}}, stacked:true }}
    }}
  }}
}});
</script>
</body>
</html>"""


if __name__ == "__main__":
    data = get_kpi_data(30)
    summary = get_summary(data)
    html = generate_html(data, summary)
    out = Path("kpi_report.html")
    out.write_text(html, encoding="utf-8")
    size = out.stat().st_size / 1024
    print(f"已生成: {out.absolute()} ({size:.1f} KB)")
