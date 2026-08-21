#!/usr/bin/env python3
"""
每日財經新聞篩選機器人 - 雙語版 (中文 + English)
平日早上 9 點推送，週五加發本週回顧
"""

import os
import json
import re
import time
import logging
from datetime import datetime, timezone, timedelta
import feedparser
import requests

# ============ 設定 ============
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
CLAUDE_API_KEY = os.environ["CLAUDE_API_KEY"]

SG_TIME = timezone(timedelta(hours=8))
TODAY = datetime.now(SG_TIME)
TODAY_STR = TODAY.strftime("%Y年%m月%d日")
TODAY_STR_EN = TODAY.strftime("%B %d, %Y")
WEEKDAY = TODAY.weekday()  # Monday=0, Friday=4

RSS_SOURCES = {
    "🇸🇬 新加坡本地": [
        "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6936",
        "https://www.straitstimes.com/business/economy/rss.xml",
        "https://www.businesstimes.com.sg/rss",
        "https://mothership.sg/feed",
    ],
    "🌏 亞洲": [
        "https://asia.nikkei.com/rss-feeds/news",
        "https://www.scmp.com/rss/92/feed",
        "http://www.koreaherald.com/rss/020100000000.xml",
        "https://www.japantimes.co.jp/feed/business/",
    ],
    "🌍 全球": [
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://www.marketwatch.com/rss/topstories",
        "https://feeds.bloomberg.com/business/news.rss",
    ],
    "₿ 加密與科技": [
        "https://www.coindesk.com/arc/outboundfeeds/rss/",
        "https://cointelegraph.com/rss",
        "https://www.theblock.co/rss.xml",
        "https://techcrunch.com/feed/",
    ],
}

MAX_RAW_NEWS = 40
FINAL_COUNT = 6
CLAUDE_MODEL = "claude-3-5-haiku-20241022"

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def fetch_rss():
    """抓取所有 RSS 來源"""
    all_news = []
    for region, urls in RSS_SOURCES.items():
        for url in urls:
            try:
                logger.info(f"抓取中: {url}")
                feed = feedparser.parse(url)
                for entry in feed.entries[:6]:
                    title = entry.get("title", "").strip()
                    link = entry.get("link", "").strip()
                    summary = entry.get("summary", entry.get("description", "")).strip()
                    summary = re.sub(r'<[^>]+>', '', summary)[:250]
                    if title and link:
                        all_news.append({
                            "region": region,
                            "title": title,
                            "link": link,
                            "summary": summary,
                            "source": feed.feed.get("title", "Unknown")
                        })
            except Exception as e:
                logger.warning(f"RSS 失敗 ({url}): {e}")
                continue
    logger.info(f"共抓取 {len(all_news)} 篇原始新聞")
    return all_news[:MAX_RAW_NEWS]


def call_claude_daily(news_list):
    """平日：生成每日新聞（雙語）"""
    news_text = ""
    for i, n in enumerate(news_list, 1):
        news_text += f"{i}. [{n['region']}] {n['title']}\n   Source: {n['source']}\n   Summary: {n['summary']}\n   Link: {n['link']}\n\n"

    system_prompt = """You are a bilingual financial editor for young investors (18-35) in Singapore.
Select 6 most relevant stories and output BOTH Chinese and English in one JSON.

Selection criteria:
- Relevant to daily life: rates, inflation, jobs, housing, stocks, crypto, consumer trends
- Prioritize Singapore & Asia, include major global events
- Explain WHY it matters, not just WHAT happened
- Casual, friendly tone like chatting with a friend

Output strict JSON:
{
  "news": [
    {
      "tag_zh": "📈 股市動向",
      "tag_en": "📈 Markets",
      "title_zh": "Chinese headline",
      "title_en": "English headline",
      "why_zh": "Why it matters (Chinese)",
      "why_en": "Why it matters (English)",
      "impact_zh": "💡 Meaning for young investors (Chinese)",
      "impact_en": "💡 Meaning for young investors (English)",
      "link": "URL"
    }
  ],
  "intro_zh": "Warm opening (Chinese)",
  "intro_en": "Warm opening (English)",
  "outro_zh": "Disclaimer (Chinese)",
  "outro_en": "Disclaimer (English)"
}

Tags: 📈股市/Markets, 🏠房產/Property, 💰宏觀/Macro, ₿加密/Crypto, 🇨🇳中國/China, 🇺🇸美國/US, 🌏亞洲/Asia, 🇸🇬新加坡/Singapore, 💳理財/PersonalFinance, 💼職場/Career, 🔬科技/Tech

Output ONLY valid JSON. No markdown, no explanations."""

    user_prompt = f"""Today is {TODAY_STR} / {TODAY_STR_EN}. Weekday: {WEEKDAY}.
Here are today's financial news. Please select {FINAL_COUNT} stories and output bilingual JSON:

{news_text}

Output strict JSON only."""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 4000,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}]
            },
            timeout=90
        )
        response.raise_for_status()
        content = response.json()["content"][0]["text"]
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        raise ValueError("No JSON found")
    except Exception as e:
        logger.error(f"Claude daily failed: {e}")
        return None


def call_claude_weekly(news_list):
    """週五：生成本週回顧"""
    headlines = "\n".join([f"- {n['title']}" for n in news_list[:30]])

    system_prompt = """You are a financial editor. Today is Friday. Based on this week's headlines, generate a weekly review for young investors.
Output strict JSON:
{
  "themes": [
    {"emoji": "📈", "title_zh": "主題", "title_en": "Theme", "desc_zh": "描述", "desc_en": "Description"}
  ],
  "market_zh": "本周市場總結 (Chinese)",
  "market_en": "Market summary (English)",
  "watch_zh": "下周關注 (Chinese)",
  "watch_en": "Watch next week (English)",
  "quote_zh": "投資金句 (Chinese)",
  "quote_en": "Investment quote (English)"
}"""

    user_prompt = f"""This week's headlines:\n{headlines}\n\nGenerate weekly review JSON only."""

    try:
        response = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": CLAUDE_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 2000,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}]
            },
            timeout=60
        )
        response.raise_for_status()
        content = response.json()["content"][0]["text"]
        json_match = re.search(r'\{.*\}', content, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
        raise ValueError("No JSON found")
    except Exception as e:
        logger.error(f"Claude weekly failed: {e}")
        return None


def format_daily_zh(data):
    lines = [f"🇨🇳 中文版 | ☀️ {TODAY_STR} 財經快報", "─" * 22]
    lines.append(f"\n{data.get('intro_zh', '早安！來看看今天有哪些重要財經消息 👇')}\n")
    for item in data["news"]:
        lines.extend([
            f"{item['tag_zh']}",
            f"📰 {item['title_zh']}",
            f"📝 {item['why_zh']}",
            f"{item['impact_zh']}",
            f"🔗 {item['link']}",
            ""
        ])
    lines.append(f"{data.get('outro_zh', '💡 以上資訊僅供參考，不構成投資建議。')}")
    lines.append("📬 喜歡這類內容嗎？歡迎分享給朋友！")
    return "\n".join(lines)


def format_daily_en(data):
    lines = [f"🇬🇧 English Version | ☀️ Daily Finance Brief - {TODAY_STR_EN}", "─" * 22]
    lines.append(f"\n{data.get('intro_en', 'Good morning! Here are today\\'s top stories 👇')}\n")
    for item in data["news"]:
        lines.extend([
            f"{item['tag_en']}",
            f"📰 {item['title_en']}",
            f"📝 {item['why_en']}",
            f"{item['impact_en']}",
            f"🔗 {item['link']}",
            ""
        ])
    lines.append(f"{data.get('outro_en', '💡 For informational purposes only, not investment advice.')}")
    lines.append("📬 Find this helpful? Share it with a friend!")
    return "\n".join(lines)


def format_weekly(data):
    lines = ["📊 本周市場回顧 | Weekly Market Review", "─" * 22]
    lines.append("\n🔥 本周三大主題 / Top Themes:\n")
    for t in data["themes"]:
        lines.extend([
            f"{t['emoji']} {t['title_zh']} / {t['title_en']}",
            f"   {t['desc_zh']}",
            f"   {t['desc_en']}",
            ""
        ])
    lines.append(f"📈 {data['market_zh']}")
    lines.append(f"📈 {data['market_en']}")
    lines.append(f"\n👀 {data['watch_zh']}")
    lines.append(f"👀 {data['watch_en']}")
    lines.append(f"\n💬 {data['quote_zh']}")
    lines.append(f"💬 {data['quote_en']}")
    return "\n".join(lines)


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": False}
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error(f"Telegram failed: {e}")
        return False


def main():
    logger.info(f"啟動 - {TODAY_STR} (Weekday: {WEEKDAY})")
    
    raw_news = fetch_rss()
    if not raw_news:
        logger.error("沒有抓到新聞")
        return

    # === 平日：每日新聞 ===
    daily_data = call_claude_daily(raw_news)
    if daily_data:
        # 中文
        msg_zh = format_daily_zh(daily_data)
        if msg_zh and send_telegram(msg_zh):
            logger.info("中文版發送成功 ✅")
        time.sleep(3)
        
        # 英文
        msg_en = format_daily_en(daily_data)
        if msg_en and send_telegram(msg_en):
            logger.info("英文版發送成功 ✅")
    else:
        logger.error("每日新聞生成失敗")

    # === 週五：本週回顧 ===
    if WEEKDAY == 4:
        time.sleep(3)
        weekly_data = call_claude_weekly(raw_news)
        if weekly_data:
            msg_weekly = format_weekly(weekly_data)
            if msg_weekly and send_telegram(msg_weekly):
                logger.info("本週回顧發送成功 📊")
        else:
            logger.error("本週回顧生成失敗")

    logger.info("任務完成 🎉")


if __name__ == "__main__":
    main()
