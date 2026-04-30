"""
comment_poster.py - 基于 Playwright 的通用论坛自动回帖与监控系统

核心模块：负责帖子抓取、评论生成、自动回帖、防封禁策略
"""

import json
import logging
import random
import re
import sqlite3
import string
import time
import ssl
import urllib.request
import urllib.error
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# ============================================================
# 日志配置
# ============================================================

logger = logging.getLogger("comment_poster")


def setup_logging(config: dict) -> None:
    """初始化日志系统"""
    log_cfg = config.get("logging", {})
    log_file = log_cfg.get("log_file", "comment_poster.log")
    log_level = getattr(logging, log_cfg.get("log_level", "INFO").upper(), logging.INFO)

    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)

    logger.setLevel(log_level)
    logger.addHandler(file_handler)
    logger.addHandler(console_handler)


# ============================================================
# 配置加载
# ============================================================


def load_config(config_path: str = "monitor_config.json") -> dict:
    """加载 JSON 配置文件"""
    path = Path(config_path)
    if not path.exists() and path.name == "monitor_config.json":
        example_path = path.with_name("monitor_config.example.json")
        if example_path.exists():
            path.write_text(example_path.read_text(encoding="utf-8"), encoding="utf-8")

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ============================================================
# SQLite 去重逻辑
# ============================================================


@contextmanager
def db_connection(db_path: str):
    """打开 SQLite 连接，并确保每次使用后关闭文件句柄。"""
    conn = sqlite3.connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


class PostDatabase:
    """管理已处理帖子的 SQLite 数据库，防止重复评论"""

    def __init__(self, db_path: str = "monitor_comments.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with db_connection(self.db_path) as conn:
            # 检查旧表是否存在且用的是 post_id 主键
            cursor = conn.execute("SELECT sql FROM sqlite_master WHERE name='processed_posts'")
            row = cursor.fetchone()
            if row and "PRIMARY KEY" in (row[0] or "") and "AUTOINCREMENT" not in (row[0] or ""):
                # 旧表结构，需要迁移：改为自增ID，允许同一帖子多次评论
                conn.execute("ALTER TABLE processed_posts RENAME TO processed_posts_old")
                conn.execute("""
                    CREATE TABLE processed_posts (
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        post_id   TEXT,
                        title     TEXT,
                        url       TEXT,
                        comment   TEXT,
                        nickname  TEXT,
                        status    TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)
                conn.execute("""
                    INSERT INTO processed_posts (post_id, title, url, comment, nickname, status, created_at)
                    SELECT post_id, title, url, comment, nickname, status, created_at FROM processed_posts_old
                """)
                conn.execute("DROP TABLE processed_posts_old")
            elif not row:
                conn.execute("""
                    CREATE TABLE processed_posts (
                        id        INTEGER PRIMARY KEY AUTOINCREMENT,
                        post_id   TEXT,
                        title     TEXT,
                        url       TEXT,
                        comment   TEXT,
                        nickname  TEXT,
                        status    TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                """)

    def is_processed(self, post_id: str) -> bool:
        """检查帖子是否已处理"""
        with db_connection(self.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM processed_posts WHERE post_id = ?", (post_id,)
            ).fetchone()
            return row is not None

    def get_used_comments(self, post_id: str) -> list[str]:
        """获取某帖子已使用的评论内容，用于去重"""
        with db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT comment FROM processed_posts WHERE post_id=? AND status='success'",
                (post_id,),
            ).fetchall()
            return [r[0] for r in rows if r[0]]

    def mark_processed(
        self,
        post_id: str,
        title: str,
        url: str,
        comment: str,
        nickname: str,
        status: str = "success",
    ) -> None:
        """记录已处理的帖子（允许同一帖子多次评论）"""
        with db_connection(self.db_path) as conn:
            conn.execute(
                """INSERT INTO processed_posts
                   (post_id, title, url, comment, nickname, status)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (post_id, title, url, comment, nickname, status),
            )

    def get_stats(self) -> dict:
        """获取统计信息"""
        with db_connection(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM processed_posts").fetchone()[0]
            success = conn.execute(
                "SELECT COUNT(*) FROM processed_posts WHERE status='success'"
            ).fetchone()[0]
            failed = conn.execute(
                "SELECT COUNT(*) FROM processed_posts WHERE status='failed'"
            ).fetchone()[0]
            today_total = conn.execute(
                "SELECT COUNT(*) FROM processed_posts WHERE date(created_at)=date('now')"
            ).fetchone()[0]
            today_success = conn.execute(
                "SELECT COUNT(*) FROM processed_posts WHERE status='success' AND date(created_at)=date('now')"
            ).fetchone()[0]
        return {"total": total, "success": success, "failed": failed,
                "today_total": today_total, "today_success": today_success}


# ============================================================
# 舆论监控数据库
# ============================================================


class SentimentDatabase:
    """存储其他用户的负面/异常评论（卡、看不了等）"""

    def __init__(self, db_path: str = "sentiment_monitor.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with db_connection(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS sentiment_comments (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id     TEXT,
                    post_title  TEXT,
                    post_url    TEXT,
                    author      TEXT,
                    comment     TEXT,
                    matched_keywords TEXT,
                    comment_time TEXT,
                    captured_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def add(self, post_id: str, post_title: str, post_url: str,
            author: str, comment: str, matched_keywords: list, comment_time: str) -> None:
        """记录一条匹配的舆论评论"""
        with db_connection(self.db_path) as conn:
            # 先检查是否已存在（同帖子+同作者+同内容 = 重复）
            exists = conn.execute(
                "SELECT 1 FROM sentiment_comments WHERE post_id=? AND author=? AND comment=?",
                (post_id, author, comment),
            ).fetchone()
            if exists:
                return
            conn.execute(
                """INSERT INTO sentiment_comments
                   (post_id, post_title, post_url, author, comment, matched_keywords, comment_time)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (post_id, post_title, post_url, author, comment,
                 ",".join(matched_keywords), comment_time),
            )

    def get_records(self, limit: int = 50, offset: int = 0) -> tuple[list, int]:
        """获取舆论记录"""
        with db_connection(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            total = conn.execute("SELECT COUNT(*) FROM sentiment_comments").fetchone()[0]
            rows = conn.execute(
                "SELECT * FROM sentiment_comments ORDER BY captured_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows], total

    def get_stats(self) -> dict:
        with db_connection(self.db_path) as conn:
            total = conn.execute("SELECT COUNT(*) FROM sentiment_comments").fetchone()[0]
            today = conn.execute(
                "SELECT COUNT(*) FROM sentiment_comments WHERE date(captured_at)=date('now')"
            ).fetchone()[0]
        return {"total": total, "today": today}


# ============================================================
# KPI 数据库
# ============================================================


class KpiDatabase:
    """管理 KPI 手动输入数据（官方后台评论等）"""

    def __init__(self, db_path: str = "kpi_data.db", posts_db_path: str = "monitor_comments.db"):
        self.db_path = db_path
        self.posts_db_path = posts_db_path
        self._init_db()

    def _init_db(self) -> None:
        with db_connection(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS kpi_manual (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    date        TEXT UNIQUE NOT NULL,
                    manual_comments INTEGER DEFAULT 0,
                    note        TEXT DEFAULT '',
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

    def add_or_update(self, date: str, manual_comments: int, note: str = "") -> None:
        """添加或更新某天的手动评论数"""
        with db_connection(self.db_path) as conn:
            exists = conn.execute(
                "SELECT 1 FROM kpi_manual WHERE date=?", (date,)
            ).fetchone()
            if exists:
                conn.execute(
                    "UPDATE kpi_manual SET manual_comments=?, note=?, updated_at=CURRENT_TIMESTAMP WHERE date=?",
                    (manual_comments, note, date),
                )
            else:
                conn.execute(
                    "INSERT INTO kpi_manual (date, manual_comments, note) VALUES (?, ?, ?)",
                    (date, manual_comments, note),
                )

    def delete_record(self, date: str) -> bool:
        """删除某天的手动记录"""
        with db_connection(self.db_path) as conn:
            cursor = conn.execute("DELETE FROM kpi_manual WHERE date=?", (date,))
            return cursor.rowcount > 0

    def get_daily_data(self, days: int = 30) -> list[dict]:
        """
        获取最近N天的KPI数据，合并自动评论和手动评论。
        返回按日期升序排列的列表。
        """
        # 1. 从 processed_posts 聚合自动评论数
        auto_stats = {}
        try:
            with db_connection(self.posts_db_path) as conn:
                rows = conn.execute(
                    """SELECT date(created_at) as d, COUNT(*) as c
                       FROM processed_posts
                       WHERE status='success'
                         AND date(created_at) >= date('now', ?)
                       GROUP BY date(created_at)""",
                    (f"-{days} days",),
                ).fetchall()
                for row in rows:
                    auto_stats[row[0]] = row[1]
        except Exception:
            pass

        # 2. 从 kpi_manual 读取手动记录
        manual_stats = {}
        with db_connection(self.db_path) as conn:
            rows = conn.execute(
                "SELECT date, manual_comments, note FROM kpi_manual WHERE date >= date('now', ?)",
                (f"-{days} days",),
            ).fetchall()
            for row in rows:
                manual_stats[row[0]] = {"count": row[1], "note": row[2]}

        # 3. 合并：生成最近 N 天的完整列表
        result = []
        today = datetime.now().date()
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            d_str = d.strftime("%Y-%m-%d")
            auto = auto_stats.get(d_str, 0)
            manual_info = manual_stats.get(d_str, {"count": 0, "note": ""})
            manual = manual_info["count"]
            result.append({
                "date": d_str,
                "auto_comments": auto,
                "manual_comments": manual,
                "total": auto + manual,
                "note": manual_info["note"],
            })
        return result

    def get_summary(self, days: int = 30) -> dict:
        """汇总统计"""
        data = self.get_daily_data(days)
        today = datetime.now().date()

        today_str = today.strftime("%Y-%m-%d")
        today_total = 0
        week_total = 0
        month_total = 0

        week_start = today - timedelta(days=today.weekday())  # 本周一
        month_start = today.replace(day=1)

        for item in data:
            d = datetime.strptime(item["date"], "%Y-%m-%d").date()
            if item["date"] == today_str:
                today_total = item["total"]
            if d >= week_start:
                week_total += item["total"]
            if d >= month_start:
                month_total += item["total"]

        non_zero_days = sum(1 for item in data if item["total"] > 0)
        total_all = sum(item["total"] for item in data)
        daily_avg = round(total_all / max(non_zero_days, 1), 1)

        return {
            "today": today_total,
            "week": week_total,
            "month": month_total,
            "daily_avg": daily_avg,
        }


# ============================================================
# 评论内容生成
# ============================================================


def _load_sample_comments(config: dict, count: int = 15) -> list[str]:
    """从评论库随机抽取若干条作为风格样本"""
    posting_cfg = config.get("posting", {})
    filepath = posting_cfg.get("history_comments_file", "history_comments.txt")
    path = Path(filepath)
    if not path.exists():
        return []
    lines = [
        l.strip()
        for l in path.read_text(encoding="utf-8").splitlines()
        if _has_visible_text(l)
    ]
    if not lines:
        return []
    return random.sample(lines, min(count, len(lines)))


def _has_visible_text(text: str) -> bool:
    """过滤普通空白和零宽字符"""
    return bool(re.sub(r"[\s\u200b\u200c\u200d\ufeff]+", "", text or ""))


def _format_prompt_template(template: str, title: str, context: str) -> str:
    """安全替换 prompt 模板变量"""
    return (
        (template or "")
        .replace("{title}", title or "")
        .replace("{context}", context or "")
        .strip()
    )


def _comment_prefix(text: str) -> str:
    """取评论开头用于去重和风格观察"""
    cleaned = re.sub(r"[\s，。！？,.!?\-—~～…]+", "", text or "")
    return cleaned[:4]


def _clean_ai_comment(text: str) -> str:
    """清理 AI 输出中的说明、引号和多余格式"""
    result = (text or "").strip()
    result = result.splitlines()[0].strip()
    result = re.sub(r"^(评论|回复|输出|答案)\s*[:：]\s*", "", result)
    result = result.strip(" \t\r\n\"'“”‘’`，。")
    result = re.sub(r"\s+", "", result)
    return result


def _extract_chat_content(response) -> str:
    """从 OpenAI 兼容响应中安全提取文本"""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""

    message = getattr(choices[0], "message", None)
    if not message:
        return ""

    content = getattr(message, "content", "") or ""
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(getattr(item, "text", "") or item))
        return "".join(parts)
    return str(content)


def _is_bad_ai_comment(text: str, samples: list[str] = None) -> bool:
    """识别明显不像真人、过长或重复的评论"""
    if not text:
        return True

    samples = samples or []
    generic_starts = (
        "这标题",
        "这个标题",
        "这篇",
        "本文",
        "好文章",
        "感谢分享",
        "谢谢分享",
        "支持一下",
        "作为",
        "根据",
        "绷不住",
        "不是哥们",
        "我服了",
        "哈人",
        "离谱",
    )
    generic_phrases = (
        "作为一个",
        "值得一看",
        "内容丰富",
        "引人深思",
        "让人印象深刻",
        "这篇文章",
        "不要太正式",
        "只输出",
    )

    if text in samples:
        return True
    if len(text) < 4 or len(text) > 36:
        return True
    compact = re.sub(r"[\s，。！？,.!?\-—~～…]+", "", text)
    if any(compact.startswith(prefix) for prefix in generic_starts):
        return True
    if any(phrase in text for phrase in generic_phrases):
        return True
    if re.search(r"(AI|ai|人工智能).*?(生成|模型|语言模型)", text):
        return True
    return False


def _fallback_comment(samples: list[str] = None) -> str:
    """AI 不可用时使用更自然的兜底短评"""
    cleaned_samples = [s.strip() for s in (samples or []) if _has_visible_text(s)]
    if cleaned_samples:
        return random.choice(cleaned_samples)

    builtin = [
        "先码住慢慢看",
        "这个有点意思",
        "看着还挺上头",
        "感觉可以蹲一波",
        "这波有点东西",
    ]
    return random.choice(builtin)


def generate_comment(title: str, context: str = "", config: dict = None) -> str:
    """
    生成评论内容。

    AI 模式下：从评论库抽取样本喂给 AI，让它模仿风格和语气，
    根据帖子标题生成一条新的、不重复的评论。
    """
    config = config or {}
    ai_cfg = config.get("ai", {})
    provider = ai_cfg.get("provider", "file")

    if provider == "file":
        posting_cfg = config.get("posting", {})
        comments_file = posting_cfg.get("history_comments_file", "history_comments.txt")
        return _load_random_comment(comments_file)

    # AI 模式：读取评论库样本，构建带样本的 prompt
    samples = _load_sample_comments(config, count=35)

    if provider in ("openai", "deepseek"):
        return _generate_via_openai(title, context, ai_cfg, samples)

    elif provider == "gemini":
        return _generate_via_gemini(title, context, ai_cfg, samples)

    else:
        logger.warning(f"未知的 AI provider: {provider}，回退到文件模式")
        posting_cfg = config.get("posting", {})
        return _load_random_comment(posting_cfg.get("history_comments_file", "history_comments.txt"))


def _generate_via_openai(title: str, context: str, ai_cfg: dict, samples: list[str] = None) -> str:
    """通过 OpenAI 兼容 API 生成评论（支持自定义 base_url）"""
    try:
        import openai
    except ImportError:
        logger.error("openai 库未安装，请执行: pip install openai")
        return _fallback_comment(samples)

    api_key = ai_cfg.get("api_key", "")
    if not api_key:
        logger.error("API Key 未配置")
        return _fallback_comment(samples)

    try:
        client_kwargs = {"api_key": api_key}
        base_url = ai_cfg.get("base_url", "")
        if base_url:
            client_kwargs["base_url"] = base_url

        client = openai.OpenAI(**client_kwargs)
        provider = ai_cfg.get("provider", "deepseek")
        default_model = "deepseek-v4-pro" if provider == "deepseek" else "gpt-4o-mini"
        model = ai_cfg.get("model", default_model)

        # 构建带评论库样本的 prompt
        samples_text = ""
        if samples:
            samples_text = "\n".join(f"- {s}" for s in samples)

        sample_prefixes = []
        for sample in samples or []:
            prefix = _comment_prefix(sample)
            if prefix and prefix not in sample_prefixes:
                sample_prefixes.append(prefix)
        sample_prefixes = random.sample(sample_prefixes, min(10, len(sample_prefixes)))

        style_cards = [
            "主要学评论库，不要发明固定网梗",
            "像评论库里的短句，直接、粗糙、欲望很直白",
            "学样本的词序和节奏，不要用同一个开头",
            "从样本里换一种开头方式，别总感叹",
            "像老帖吧友随手回，别像 AI 套话",
            "短一点，狠一点，但别复读固定口癖",
        ]
        chosen_style = random.choice(style_cards)
        custom_prompt = _format_prompt_template(
            ai_cfg.get("prompt_template", ""), title, context[:220]
        )

        system_prompt = (
            "你是中文论坛/贴吧里混久了的老用户，只写一条短评论。\n"
            "目标：优先模仿评论库里的真实样本，不像 AI、不像客服、不像正常点评。\n"
            "硬性要求：\n"
            "1. 6-22 个中文字符左右，宁可短，不要完整作文。\n"
            "2. 学样本的开头、词序、粗糙感和直给语气，但不要照抄样本。\n"
            "3. 只抓标题里的一个点回应，不要复述整句标题。\n"
            "4. 不要连续使用同类开头，尤其别复读固定网梗。\n"
            "5. 禁止这些偷懒开头：绷不住了、不是哥们、我服了、哈人、离谱。\n"
            "6. 禁止模板腔：这标题、好文章、感谢分享、支持一下、值得一看、不错。\n"
            "7. 不要道德评价、不要解释、不要序号、不要引号，不要 emoji。\n"
            "8. 只输出评论内容本身。"
        )

        user_prompt = (
            f"【本次语气】\n{chosen_style}\n\n"
            f"【历史真实评论样本】\n{samples_text or '无'}\n\n"
            f"【这次可参考的样本开头类型】\n{'、'.join(sample_prefixes) or '无'}\n\n"
            f"【帖子标题】\n{title}\n\n"
            f"【页面片段】\n{context[:180] or '无'}\n\n"
        )
        if custom_prompt:
            user_prompt += f"【额外风格要求】\n{custom_prompt}\n\n"
        user_prompt += "现在生成一条更像评论库的新短评：别正经，别礼貌，别用固定网梗开头，别照抄样本。"

        # 过滤模板腔和重复样本，最多重试4次
        result = ""
        for attempt in range(4):
            request_kwargs = {
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 80,
                "temperature": 1.15,
                "top_p": 0.92,
            }
            if provider == "deepseek" and model.startswith("deepseek-v4"):
                request_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            try:
                response = client.chat.completions.create(
                    **request_kwargs,
                    presence_penalty=0.4,
                    frequency_penalty=0.5,
                )
            except Exception as exc:
                message = str(exc).lower()
                if "presence_penalty" not in message and "frequency_penalty" not in message:
                    raise
                response = client.chat.completions.create(**request_kwargs)
            content = _extract_chat_content(response)
            if not content:
                logger.info(f"AI 返回空内容，重试 {attempt+1}/4")
                continue

            result = _clean_ai_comment(content)
            if not _is_bad_ai_comment(result, samples):
                break
            logger.info(f"AI 评论像模板或重复，重试 {attempt+1}/4: {result}")
        if _is_bad_ai_comment(result, samples):
            fallback = _fallback_comment(samples)
            logger.info(f"AI 多次未生成可用评论，回退评论: {fallback}")
            return fallback
        logger.info(f"AI 生成评论: {result}")
        return result
    except Exception as e:
        logger.error(f"API 调用失败: {e}")
        # 失败时从评论库随机抽取一条兜底
        fallback = _fallback_comment(samples)
        logger.info(f"回退评论: {fallback}")
        return fallback


def _generate_via_gemini(title: str, context: str, ai_cfg: dict, samples: list[str] = None) -> str:
    """通过 Google Gemini API 生成评论"""
    try:
        import google.generativeai as genai
    except ImportError:
        logger.error("google-generativeai 库未安装，请执行: pip install google-generativeai")
        return _fallback_comment(samples)

    api_key = ai_cfg.get("api_key", "")
    if not api_key:
        logger.error("Gemini API Key 未配置")
        return _fallback_comment(samples)

    try:
        genai.configure(api_key=api_key)
        model_name = ai_cfg.get("model", "gemini-pro")
        model = genai.GenerativeModel(model_name)

        samples_text = "\n".join(f"- {s}" for s in (samples or []))
        prompt = (
            "你是中文论坛普通用户，只写一条像真人随手回的短评论。\n"
            "要求：8-24个中文字符，只抓标题一个点，不复述标题，不要“这标题/好文章/感谢分享/支持一下”，不要解释，不要emoji。\n"
            f"【样本】\n{samples_text or '无'}\n\n【标题】{title}\n\n只输出评论内容："
        )

        response = model.generate_content(prompt)
        result = _clean_ai_comment(response.text)
        if _is_bad_ai_comment(result, samples):
            result = _fallback_comment(samples)
        logger.info(f"Gemini 生成评论: {result}")
        return result
    except Exception as e:
        logger.error(f"Gemini API 调用失败: {e}")
        return _fallback_comment(samples)


def _load_random_comment(filepath: str) -> str:
    """从文件中随机抽取一行评论"""
    path = Path(filepath)
    if not path.exists():
        logger.warning(f"评论文件不存在: {filepath}，使用默认评论")
        return "好文章，感谢分享！"

    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not lines:
        return "好文章，感谢分享！"

    return random.choice(lines)


# ============================================================
# Google Sheets 同步
# ============================================================


def sync_to_google_sheets(post: dict, comment: str, nickname: str, config: dict) -> None:
    """将评论记录同步到 Google Sheets（通过 Apps Script Web App）"""
    webhook_url = config.get("google_sheets", {}).get("webhook_url", "")
    if not webhook_url:
        return

    try:
        today = datetime.now().strftime("%Y/%m/%d")
        payload = json.dumps({
            "post_id": post.get("post_id", ""),
            "nickname": nickname,
            "comment": comment,
            "date": today,
            "title": post.get("title", ""),
        }, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        resp = urllib.request.urlopen(req, timeout=15, context=ctx)
        logger.debug(f"Google Sheets 同步成功: {resp.status}")
    except Exception as e:
        logger.warning(f"Google Sheets 同步失败（不影响评论）: {e}")


# ============================================================
# 随机昵称生成
# ============================================================


def generate_nickname(min_len: int = 5, max_len: int = 8) -> str:
    """生成随机中文昵称"""
    surnames = list("赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳酆鲍史唐费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆萧尹")
    names_1 = list("伟芳娜敏静丽强磊洋艳勇军杰娟涛超明华雪飞平刚蕾桂英梅翠萍春菊兰霞玉秀珍红金素玲桂")
    names_2 = list("子小大阿老天宇浩然晨辰星云雨风雪花月光明亮晓学志文武建国庆民安康宁祥瑞福德才智勇毅")
    endings = ["呀", "吖", "哈", "嘿", "耶", "噢", "咯", "啦", "呢", "吧", "鸭", ""]

    style = random.choice(["chinese", "net_name", "cute"])

    if style == "chinese":
        # 传统中文名：姓 + 1-2个字
        surname = random.choice(surnames)
        if random.random() < 0.5:
            return surname + random.choice(names_1)
        else:
            return surname + random.choice(names_2) + random.choice(names_1)

    elif style == "net_name":
        # 网名风格
        prefixes = ["小", "大", "阿", "老", "超级", "快乐", "暴躁", "佛系", "摸鱼", "干饭",
                     "追剧", "熬夜", "吃瓜", "咸鱼", "躺平", "卷王", "打工", "社恐", "i人"]
        words = ["猫咪", "狗子", "兔子", "熊猫", "鸽子", "青蛙", "鱼", "虾",
                 "少年", "少女", "大叔", "姐姐", "哥哥", "同学", "选手", "达人",
                 "战士", "骑士", "玩家", "路人", "观众", "粉丝", "用户"]
        name = random.choice(prefixes) + random.choice(words)
        if random.random() < 0.3:
            name += random.choice(endings)
        return name

    else:
        # 可爱风格
        cute_names = [
            "橘子味汽水", "草莓蛋糕", "芒果冰沙", "奶茶续命", "西瓜甜甜",
            "柠檬不酸", "蜜桃乌龙", "椰子鸡汤", "抹茶拿铁", "葡萄果冻",
            "布丁味的猫", "芝士焗番薯", "冰糖葫芦", "棉花糖云朵", "焦糖玛奇朵",
            "泡芙小姐", "饼干先生", "薯条配番茄", "烤红薯味道", "糖醋排骨",
            "星星月亮", "晚风微甜", "温柔半两", "清风不解语", "浅笑安然",
            "像风一样", "半夏微凉", "南风知我意", "春风十里", "云淡风轻",
        ]
        return random.choice(cute_names)


# ============================================================
# 防封禁：随机延迟与人类行为模拟
# ============================================================


def random_sleep(min_sec: float, max_sec: float) -> None:
    """随机等待"""
    delay = random.uniform(min_sec, max_sec)
    logger.debug(f"随机等待 {delay:.1f} 秒")
    time.sleep(delay)


def simulate_human_scroll(page: Page, config: dict) -> None:
    """模拟人类滚动行为：分段滚动到底部"""
    timing = config.get("timing", {})
    min_pause = timing.get("min_scroll_pause", 0.5)
    max_pause = timing.get("max_scroll_pause", 1.5)

    total_height = page.evaluate("document.body.scrollHeight")
    viewport_height = page.evaluate("window.innerHeight")
    current = 0

    while current < total_height:
        scroll_step = random.randint(200, 500)
        current = min(current + scroll_step, total_height)
        page.evaluate(f"window.scrollTo(0, {current})")
        random_sleep(min_pause, max_pause)

    logger.debug("页面滚动到底部完成")


def simulate_mouse_movement(page: Page) -> None:
    """模拟随机鼠标移动"""
    for _ in range(random.randint(2, 5)):
        x = random.randint(100, 800)
        y = random.randint(100, 600)
        page.mouse.move(x, y, steps=random.randint(5, 15))
        time.sleep(random.uniform(0.1, 0.3))


def _get_timing_int(config: dict, key: str, default: int) -> int:
    """读取 timing 中的整数配置，配置异常时回退默认值"""
    try:
        return int(config.get("timing", {}).get(key, default))
    except (TypeError, ValueError):
        return default


def _get_timing_float(config: dict, key: str, default: float) -> float:
    """读取 timing 中的小数配置，配置异常时回退默认值"""
    try:
        return float(config.get("timing", {}).get(key, default))
    except (TypeError, ValueError):
        return default


def _stop_loading(page: Page) -> None:
    """尽力停止仍在进行的页面加载"""
    try:
        page.evaluate("window.stop()")
    except Exception:
        pass


def _page_has_content(page: Page, required_selector: str = "") -> bool:
    """判断导航超时后页面是否已经有可用 DOM"""
    try:
        if required_selector:
            return page.query_selector(required_selector) is not None
        return bool(page.evaluate("""
            Boolean(document.body && document.body.innerText.trim().length > 0)
        """))
    except Exception:
        return False


def _safe_goto(
    page: Page,
    url: str,
    config: dict,
    *,
    purpose: str = "页面",
    wait_until: str = "domcontentloaded",
    required_selector: str = "",
) -> None:
    """
    更耐受的页面导航。

    目标站偶发慢响应或连接被重置时，不让一次 goto 直接打断整轮流程。
    """
    timeout_ms = _get_timing_int(config, "navigation_timeout_ms", 60000)
    retries = max(1, _get_timing_int(config, "navigation_retries", 3))
    retry_wait = max(0.0, _get_timing_float(config, "navigation_retry_wait", 5.0))
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            page.goto(url, wait_until=wait_until, timeout=timeout_ms)
            return
        except PlaywrightTimeoutError as exc:
            last_error = exc
            if _page_has_content(page, required_selector):
                _stop_loading(page)
                logger.warning(
                    f"{purpose}导航超时，但已检测到页面内容，继续处理: {url}"
                )
                return
            logger.warning(
                f"{purpose}导航超时({attempt}/{retries}): {url}"
            )
        except PlaywrightError as exc:
            last_error = exc
            message = str(exc).splitlines()[0]
            logger.warning(
                f"{purpose}导航失败({attempt}/{retries}): {message}"
            )

        if attempt < retries:
            _stop_loading(page)
            try:
                page.goto("about:blank", timeout=5000)
            except Exception:
                pass
            time.sleep(retry_wait)

    raise last_error or RuntimeError(f"{purpose}导航失败: {url}")


# ============================================================
# 帖子抓取
# ============================================================


def dismiss_popups(page: Page, config: dict) -> None:
    """关闭页面弹窗广告"""
    forum_cfg = config.get("forum", {})
    close_sel = forum_cfg.get("popup_close_selector", "")

    if close_sel:
        for sel in close_sel.split(","):
            sel = sel.strip()
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    logger.debug(f"关闭弹窗: {sel}")
                    time.sleep(0.5)
            except Exception:
                pass

    # 通用：移除遮罩层
    page.evaluate("""
        document.querySelectorAll('[class*="popup"], [class*="modal"], [class*="overlay"]')
            .forEach(el => { if (el.style) el.style.display = 'none'; });
    """)


def _parse_post_date(date_text: str) -> Optional[datetime]:
    """
    解析帖子日期文本，如 '2026 年 03 月 27 日'。
    返回 datetime 对象或 None。
    """
    match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", date_text)
    if match:
        try:
            return datetime(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        except ValueError:
            return None
    return None


def _build_post_date_filter(config: dict) -> dict:
    """根据配置生成帖子日期过滤规则。"""
    posting_cfg = config.get("posting", {})
    mode = posting_cfg.get("post_date_mode", "yesterday")
    today = datetime.now().date()

    if mode == "today":
        return {"mode": "today", "start": today, "end": today, "label": f"{today}（今天）"}
    if mode == "recent":
        days = max(1, int(posting_cfg.get("post_recent_days", 7)))
        start = today - timedelta(days=days - 1)
        return {"mode": "recent", "start": start, "end": today, "label": f"最近 {days} 天"}
    if mode == "all":
        return {"mode": "all", "start": None, "end": None, "label": "不限日期"}

    yesterday = today - timedelta(days=1)
    return {"mode": "yesterday", "start": yesterday, "end": yesterday, "label": f"{yesterday}（昨天）"}


def _post_matches_date(post_date: datetime, date_filter: dict) -> bool:
    """判断帖子日期是否在目标范围内。"""
    if date_filter["mode"] == "all":
        return True
    post_day = post_date.date()
    return date_filter["start"] <= post_day <= date_filter["end"]


def _should_stop_paging(post_date: datetime, date_filter: dict) -> bool:
    """列表按时间倒序时，遇到早于目标范围的帖子即可停止翻页。"""
    if date_filter["mode"] == "all":
        return False
    return post_date.date() < date_filter["start"]


def _extract_posts_from_page(page: Page, config: dict, date_filter: dict, seen_ids: set) -> tuple[list[dict], bool]:
    """
    从当前已加载的列表页提取帖子。

    返回: (帖子列表, 是否应继续翻页)
    - 如果当前页已经出现早于目标范围的帖子，说明目标范围已抓完，停止翻页。
    """
    forum_cfg = config["forum"]
    id_pattern = forum_cfg.get("post_id_pattern", r"/archives/(\d+)/")

    try:
        page.wait_for_selector("article", timeout=15000)
    except Exception:
        return [], False

    articles = page.query_selector_all("article")
    posts = []
    found_older = False

    for article in articles:
        link_el = article.query_selector("a[href*='/archives/']")
        if not link_el:
            continue

        href = link_el.get_attribute("href") or ""
        if not href:
            continue

        el_class = link_el.get_attribute("class") or ""
        if "tjtagmanager" in el_class:
            continue

        # 提取日期
        info_el = article.query_selector(".post-card-info")
        date_text = (info_el.inner_text() if info_el else article.inner_text()) or ""
        post_date = _parse_post_date(date_text)

        if post_date is None:
            continue

        # 早于目标范围的帖子 → 标记停止翻页
        if _should_stop_paging(post_date, date_filter):
            found_older = True
            continue

        if not _post_matches_date(post_date, date_filter):
            continue

        # 提取标题
        title_el = link_el.query_selector("h2.post-card-title")
        if title_el:
            title = (title_el.inner_text() or "").strip()
        else:
            title = (link_el.inner_text() or "").strip()

        title = title.split("\n")[0].strip()
        if not title or len(title) < 5:
            continue

        match = re.search(id_pattern, href)
        post_id = match.group(1) if match else href.strip("/").split("/")[-1]

        if post_id in seen_ids:
            continue
        seen_ids.add(post_id)

        if href.startswith("/"):
            base = forum_cfg["base_url"].rstrip("/")
            full_url = f"{base}{href}"
        elif href.startswith("http"):
            full_url = href
        else:
            base = forum_cfg["base_url"].rstrip("/")
            full_url = f"{base}/{href}"

        posts.append({
            "post_id": post_id,
            "title": title,
            "url": full_url,
            "date": str(post_date.date()),
        })

    # 继续翻页的条件：本页没出现更早日期的帖子
    should_continue = not found_older
    return posts, should_continue


def fetch_post_list(page: Page, config: dict) -> list[dict]:
    """
    从分类页逐页抓取目标日期范围内的帖子。

    自动翻页，直到出现早于目标范围的帖子为止。

    分页 URL 格式：/category/mrds/{page}/
    """
    forum_cfg = config["forum"]
    timing = config.get("timing", {})
    page_pattern = forum_cfg.get("list_page_pattern", "")
    max_pages = forum_cfg.get("max_pages", 10)

    date_filter = _build_post_date_filter(config)
    logger.info(f"目标范围: {date_filter['label']}")

    all_posts = []
    seen_ids = set()

    for page_num in range(1, max_pages + 1):
        # 构建分页 URL
        if page_num == 1:
            url = forum_cfg["list_page_url"]
        elif page_pattern:
            url = page_pattern.replace("{page}", str(page_num))
        else:
            break

        logger.info(f"抓取第 {page_num} 页: {url}")
        try:
            _safe_goto(
                page,
                url,
                config,
                purpose=f"列表第 {page_num} 页",
                required_selector="article",
            )
        except Exception as e:
            logger.error(f"第 {page_num} 页打开失败，停止抓取: {e}")
            break
        random_sleep(
            timing.get("min_page_load_wait", 2),
            timing.get("max_page_load_wait", 5),
        )

        # 关闭弹窗（只在第1页）
        if page_num == 1:
            dismiss_popups(page, config)

        posts, should_continue = _extract_posts_from_page(page, config, date_filter, seen_ids)
        all_posts.extend(posts)

        logger.info(f"第 {page_num} 页找到 {len(posts)} 个目标帖子")

        if not should_continue:
            logger.info(f"已出现早于目标范围的帖子，停止翻页")
            break

        # 翻页间随机等待
        random_sleep(1, 3)

    logger.info(f"共抓取到 {len(all_posts)} 个目标帖子（去重后）")
    return all_posts


# ============================================================
# 核心：自动回帖
# ============================================================


def post_comment(page: Page, post: dict, config: dict, used_comments: list = None) -> dict:
    """
    进入帖子详情页并提交评论。

    流程：
    1. 导航到帖子页面
    2. 模拟滚动到评论区
    3. 填写昵称和评论
    4. 点击提交
    5. 验证提交结果

    返回: {"success": bool, "comment": str, "nickname": str}
    """
    forum_cfg = config["forum"]
    timing = config.get("timing", {})
    posting_cfg = config.get("posting", {})

    url = post["url"]
    title = post["title"]

    try:
        # --- 1. 进入帖子详情页 ---
        logger.info(f"进入帖子: {title} -> {url}")
        comment_form_sel = forum_cfg.get("comment_form_selector", "#comment-form")
        _safe_goto(
            page,
            url,
            config,
            purpose="帖子详情页",
            required_selector=comment_form_sel,
        )
        random_sleep(
            timing.get("min_page_load_wait", 2),
            timing.get("max_page_load_wait", 5),
        )

        # --- 2. 关闭弹窗广告 ---
        dismiss_popups(page, config)

        # --- 3. 模拟鼠标移动 + 滚动到评论区 ---
        simulate_mouse_movement(page)
        simulate_human_scroll(page, config)

        # --- 4. 等待评论表单出现并滚动到可见 ---
        try:
            page.wait_for_selector(comment_form_sel, timeout=10000)
            page.evaluate(f'document.querySelector("{comment_form_sel}").scrollIntoView({{behavior:"smooth",block:"center"}})')
            random_sleep(1, 2)
        except Exception:
            logger.warning("评论表单未检测到，继续尝试填写")

        random_sleep(
            timing.get("min_action_interval", 1),
            timing.get("max_action_interval", 3),
        )

        # --- 5. 获取页面上下文并生成评论 ---
        page_context = ""
        try:
            body_text = page.inner_text("body")
            page_context = body_text[:200] if body_text else ""
        except Exception:
            pass

        # 生成评论，确保不重复
        used = set(used_comments or [])
        comment_text = generate_comment(title, page_context, config)
        for _retry in range(5):
            if comment_text not in used:
                break
            logger.info(f"评论重复，重新生成...")
            comment_text = generate_comment(title, page_context, config)
        nickname = generate_nickname(
            posting_cfg.get("nickname_length_min", 5),
            posting_cfg.get("nickname_length_max", 8),
        )

        # --- 6. 先填写评论内容（textarea#textarea） ---
        comment_sel = forum_cfg["comment_input_selector"]
        logger.debug(f"填写评论: {comment_text}")
        page.click(comment_sel)
        random_sleep(0.3, 0.8)
        page.fill(comment_sel, "")
        page.type(
            comment_sel,
            comment_text,
            delay=random.randint(
                timing.get("min_typing_delay", 50),
                timing.get("max_typing_delay", 150),
            ),
        )

        random_sleep(0.5, 1.5)

        # --- 7. 填写昵称（input#author） ---
        nickname_sel = forum_cfg["nickname_input_selector"]
        logger.debug(f"填写昵称: {nickname}")
        page.click(nickname_sel)
        random_sleep(0.3, 0.8)
        page.fill(nickname_sel, "")
        typing_delay = random.randint(
            timing.get("min_typing_delay", 50),
            timing.get("max_typing_delay", 150),
        )
        page.type(nickname_sel, nickname, delay=typing_delay)

        random_sleep(
            timing.get("min_action_interval", 1),
            timing.get("max_action_interval", 3),
        )

        # --- 8. 模拟鼠标移动到提交按钮附近 ---
        simulate_mouse_movement(page)
        random_sleep(0.5, 1.0)

        # --- 9. 点击提交（input#submit） ---
        submit_sel = forum_cfg["submit_button_selector"]
        logger.info(f"点击提交评论按钮 | 昵称={nickname} | 评论={comment_text}")
        page.click(submit_sel)

        # --- 10. 等待页面响应并验证结果 ---
        random_sleep(3, 5)

        # 检查方式1: 页面是否跳转（评论提交后通常会刷新或跳转）
        current_url = page.url
        logger.debug(f"提交后页面URL: {current_url}")

        # 检查方式2: 成功指示器
        success_sel = forum_cfg.get("success_indicator_selector")
        if success_sel:
            try:
                page.wait_for_selector(success_sel, timeout=8000)
                logger.info(f"[成功] 帖子 [{title}] 评论提交成功 | 昵称={nickname}")
                return {"success": True, "comment": comment_text, "nickname": nickname}
            except Exception:
                # 可能页面已刷新，评论已提交但未找到指示器
                logger.warning(f"[未确认] 帖子 [{title}] 未检测到成功提示，但可能已提交")
                return {"success": True, "comment": comment_text, "nickname": nickname}
        else:
            logger.info(f"[完成] 帖子 [{title}] 评论已提交（无成功指示器验证）")
            return {"success": True, "comment": comment_text, "nickname": nickname}

    except Exception as e:
        logger.error(f"[失败] 帖子 [{title}] 评论失败: {e}")
        return {"success": False, "comment": "", "nickname": ""}


# ============================================================
# 浏览器管理
# ============================================================


def create_browser_context(playwright, config: dict) -> tuple[Browser, BrowserContext]:
    """创建浏览器实例和上下文"""
    browser_cfg = config.get("browser", {})
    proxy_server = (browser_cfg.get("proxy_server") or "").strip()
    launch_options = {
        "headless": browser_cfg.get("headless", True),
        "slow_mo": browser_cfg.get("slow_mo", 50),
    }
    if proxy_server:
        launch_options["proxy"] = {"server": proxy_server}

    browser = playwright.chromium.launch(**launch_options)

    context = browser.new_context(
        viewport={
            "width": browser_cfg.get("viewport_width", 1280),
            "height": browser_cfg.get("viewport_height", 800),
        },
        user_agent=browser_cfg.get("user_agent"),
        locale=browser_cfg.get("locale", "zh-CN"),
        timezone_id=browser_cfg.get("timezone", "Asia/Shanghai"),
        ignore_https_errors=browser_cfg.get("ignore_https_errors", False),
    )
    context.set_default_navigation_timeout(
        _get_timing_int(config, "navigation_timeout_ms", 60000)
    )
    context.set_default_timeout(
        _get_timing_int(config, "selector_timeout_ms", 15000)
    )

    return browser, context


# ============================================================
# 舆论监控：扫描帖子评论区的负面评论
# ============================================================


def scan_comments_for_sentiment(page: Page, post: dict, config: dict, sentiment_db: SentimentDatabase) -> int:
    """
    扫描当前帖子详情页的所有用户评论，匹配关键词并记录。

    评论结构（目标网站）：
    - .comment-body 是每条评论容器
    - 全文包含：作者名\n回复\nX天前\n\n评论内容

    返回: 匹配到的评论数
    """
    sentiment_cfg = config.get("sentiment", {})
    if not sentiment_cfg.get("enabled", False):
        return 0

    keywords = sentiment_cfg.get("keywords", [])
    if not keywords:
        return 0

    matched_count = 0

    try:
        comment_bodies = page.query_selector_all(".comment-body")
        for cb in comment_bodies:
            full_text = (cb.inner_text() or "").strip()
            if not full_text:
                continue

            # 解析评论结构：作者\n回复\n时间\n\n正文
            lines = full_text.split("\n")
            author = lines[0].strip() if lines else "未知"

            # 提取时间
            comment_time = ""
            for line in lines:
                line = line.strip()
                if re.match(r"\d+\s*(天|小时|分钟|星期|周)前", line):
                    comment_time = line
                    break

            # 提取评论正文（跳过 作者/回复/时间 行）
            content_lines = []
            skip_header = True
            for line in lines:
                line = line.strip()
                if skip_header:
                    if line in ("回复", "") or re.match(r"\d+\s*(天|小时|分钟|星期|周)前", line) or line == author:
                        continue
                    skip_header = False
                if not skip_header:
                    content_lines.append(line)
            comment_text = " ".join(content_lines).strip()

            if not comment_text:
                continue

            # 匹配关键词
            matched = [kw for kw in keywords if kw in comment_text]
            if matched:
                sentiment_db.add(
                    post_id=post["post_id"],
                    post_title=post["title"],
                    post_url=post["url"],
                    author=author,
                    comment=comment_text,
                    matched_keywords=matched,
                    comment_time=comment_time,
                )
                matched_count += 1
                logger.info(f"[舆论] 匹配关键词 {matched} | 作者={author} | 内容={comment_text[:50]}")

    except Exception as e:
        logger.error(f"舆论扫描异常: {e}")

    return matched_count


# ============================================================
# 主流程
# ============================================================


def _safe_close_browser(page, context, browser):
    """安全关闭浏览器，忽略所有异常"""
    for obj in [page, context, browser]:
        try:
            if obj:
                obj.close()
        except Exception:
            pass


def _process_single_post(page: Page, post: dict, config: dict,
                         db: PostDatabase, sentiment_db: SentimentDatabase,
                         stats: dict, max_retries: int = 2) -> None:
    """处理单个帖子，带重试机制"""
    # 获取该帖子已用过的评论，避免重复
    used_comments = db.get_used_comments(post["post_id"])

    for attempt in range(1, max_retries + 1):
        try:
            result = post_comment(page, post, config, used_comments)
            success = result["success"]
            status = "success" if success else "failed"

            db.mark_processed(
                post["post_id"], post["title"], post["url"],
                result.get("comment", ""), result.get("nickname", ""), status,
            )

            # 顺便扫描该帖子的舆论评论
            try:
                sentiment_count = scan_comments_for_sentiment(page, post, config, sentiment_db)
                stats["sentiment_matched"] += sentiment_count
            except Exception:
                pass

            stats["processed"] += 1
            if success:
                stats["success"] += 1
                # 同步到 Google Sheets
                sync_to_google_sheets(
                    post, result.get("comment", ""),
                    result.get("nickname", ""), config,
                )
            else:
                stats["failed"] += 1
            return  # 成功，退出重试循环

        except Exception as e:
            logger.warning(f"帖子 [{post['title'][:30]}] 第 {attempt} 次尝试失败: {e}")
            if attempt < max_retries:
                logger.info(f"等待 5 秒后重试...")
                time.sleep(5)
                # 尝试刷新页面恢复
                try:
                    page.goto("about:blank", timeout=5000)
                    time.sleep(1)
                except Exception:
                    pass
            else:
                logger.error(f"帖子 [{post['title'][:30]}] 重试 {max_retries} 次仍失败，跳过")
                db.mark_processed(
                    post["post_id"], post["title"], post["url"],
                    "(失败)", "(失败)", "failed",
                )
                stats["processed"] += 1
                stats["failed"] += 1


def run_posting_cycle(config: dict) -> dict:
    """
    执行一轮完整的抓取+回帖流程。

    健壮性设计：
    - 每个帖子独立 try/catch + 2次重试
    - 每 15 个帖子自动重启浏览器（防内存泄漏/页面卡死）
    - 单个帖子超时不影响后续帖子
    - 浏览器崩溃自动重建
    """
    db = PostDatabase(config.get("database", {}).get("db_path", "monitor_comments.db"))
    sentiment_cfg = config.get("sentiment", {})
    sentiment_db = SentimentDatabase(sentiment_cfg.get("db_path", "sentiment_monitor.db"))
    posting_cfg = config.get("posting", {})
    timing = config.get("timing", {})
    max_posts = posting_cfg.get("max_posts_per_run", 9999)

    # 每隔多少个帖子重启浏览器
    restart_every = 15

    stats = {"processed": 0, "success": 0, "failed": 0, "skipped": 0, "sentiment_matched": 0}

    pw = None
    browser = None
    context = None
    page = None

    try:
        pw = sync_playwright().start()
        browser, context = create_browser_context(pw, config)
        page = context.new_page()

        # 1. 抓取帖子列表（内部自动翻页+关闭弹窗）
        posts = fetch_post_list(page, config)

        # 2. 默认允许下一轮继续处理同一批帖子，实现循环评论。
        repeat_processed = posting_cfg.get("repeat_processed_posts", True)
        if repeat_processed:
            new_posts = posts
            logger.info(f"循环模式已开启，待处理帖子: {len(new_posts)} / 总帖子: {len(posts)}")
        else:
            new_posts = [post for post in posts if not db.is_processed(post["post_id"])]
            logger.info(f"去重模式已开启，待处理帖子: {len(new_posts)} / 总帖子: {len(posts)}")

        # 3. 限制每轮处理数量
        posts_to_process = new_posts[:max_posts]
        total = len(posts_to_process)

        if total == 0:
            logger.info("没有新帖子需要处理")
            return stats

        logger.info(f"开始处理 {total} 个帖子（每 {restart_every} 个重启浏览器）")

        # 4. 逐个回帖
        for i, post in enumerate(posts_to_process):
            logger.info(f"===== [{i+1}/{total}] {post['title'][:40]} =====")

            # 定期重启浏览器防卡死
            if i > 0 and i % restart_every == 0:
                logger.info(f"已处理 {i} 个帖子，重启浏览器防卡死...")
                _safe_close_browser(page, context, browser)
                try:
                    browser, context = create_browser_context(pw, config)
                    page = context.new_page()
                    logger.info("浏览器重启成功")
                except Exception as e:
                    logger.error(f"浏览器重启失败: {e}，尝试完全重建...")
                    try:
                        pw.stop()
                    except Exception:
                        pass
                    pw = sync_playwright().start()
                    browser, context = create_browser_context(pw, config)
                    page = context.new_page()
                    logger.info("浏览器完全重建成功")

            # 处理帖子（带重试）
            _process_single_post(page, post, config, db, sentiment_db, stats, max_retries=2)

            # 帖子间等待（缩短为 8-20 秒）
            if i < total - 1:
                wait = random.uniform(
                    timing.get("min_between_posts", 8),
                    timing.get("max_between_posts", 20),
                )
                logger.info(f"下一个帖子等待 {wait:.0f} 秒 | 进度 {i+1}/{total}")
                time.sleep(wait)

        stats["skipped"] = len(new_posts) - len(posts_to_process)

    except Exception as e:
        logger.error(f"执行流程异常: {e}", exc_info=True)
    finally:
        _safe_close_browser(page, context, browser)
        try:
            if pw:
                pw.stop()
        except Exception:
            pass

    # 输出统计
    db_stats = db.get_stats()
    logger.info(
        f"本轮统计: 处理={stats['processed']}, 成功={stats['success']}, "
        f"失败={stats['failed']}, 跳过={stats['skipped']}"
    )
    logger.info(f"历史总计: {db_stats}")

    return stats
