# 论坛评论自动化与舆情监控系统

一个基于 Python、Playwright 和 Flask 的评论运营控制台。项目把帖子抓取、评论生成、自动提交、去重记录、舆情关键词监控和 KPI 统计集中到一个 Web 面板里，适合放进作品集展示自动化运营工具的完整闭环。

> 默认示例配置不指向真实站点，也不包含任何 API Key、Webhook、数据库或历史评论内容。请只在你有权限运营或测试的站点上使用。

## 功能亮点

- Playwright 自动抓取列表页和帖子详情页，支持分页、日期过滤、页面超时重试。
- 支持评论库随机抽取、DeepSeek V4 Pro、OpenAI 兼容接口和 Gemini。
- AI 生成时会参考历史评论样本，失败时自动回退到评论库。
- SQLite 记录已处理帖子，避免重复评论，并保留成功、失败、跳过等状态。
- 可开启循环模式，一轮跑完后按配置间隔继续抓取并重新处理帖子。
- 舆情监控可扫描评论区关键词，单独入库展示。
- Flask 控制面板提供启动、停止、单次执行、配置编辑、评论库编辑、日志、记录和 KPI 图表。
- Google Sheets Webhook 同步评论记录，方便团队共享数据。

## 技术栈

- Python 3.8+
- Flask
- Playwright Chromium
- SQLite
- OpenAI SDK 兼容调用
- Chart.js

## 快速开始

```bash
git clone https://github.com/byin1939-star/comment-system.git
cd comment-system
bash install.sh
bash start.sh
```

启动后访问：

```text
http://localhost:5001
```

首次启动时，脚本会从示例文件生成本地配置：

- `monitor_config.json`
- `history_comments.txt`

这些本地文件不会提交到 GitHub。

## 配置说明

1. 复制或编辑 `monitor_config.json`。
2. 把 `forum` 里的站点地址和 CSS 选择器改成你的目标站点。
3. 如果使用 DeepSeek，把 `ai.provider` 改成 `deepseek`，填入自己的 `api_key`。
4. 如果需要同步到 Google Sheets，部署 `google_apps_script.js` 后，把 Webhook URL 填到 `google_sheets.webhook_url`。
5. 在控制面板的“评论库”页维护 `history_comments.txt`，AI 模式会从这里学习风格。

## 常用命令

```bash
bash install.sh
bash start.sh
bash stop.sh
python comment_monitor.py --once
python comment_monitor.py --visible --once
python export_kpi.py
```

## 公开仓库说明

以下内容已被 `.gitignore` 排除：

- API Key、Webhook、本地配置
- 运行日志
- SQLite 数据库
- 历史评论库
- 打包文件和本机缓存

仓库里只保留可公开展示和可二次配置运行的代码。
