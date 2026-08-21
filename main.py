#!/usr/bin/env python3
"""
每日財經新聞篩選機器人 - 雙語版 (中文 + English)
使用 Google Gemini API (Free Tier)
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
GEMINI_API_KEY = os.environ["CLAUDE_API_KEY"]

SG_TIME = timezone(timedelta(hours=8))
TODAY = datetime.now(SG_TIME)
TODAY_STR = TODAY.strftime("%Y年%m月%d日")
TODAY_STR_EN = TODAY.strftime("%B %d, %Y")
WEEKDAY = TODAY.weekday()

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
GEMINI_MODEL = "gemini-1.5-flash"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def fetch_rss():
    all_news = []
    for region, urls in RSS_SOURCES.items():
        for url in urls:
            try:
                logger.info("抓取中: " + url)
                feed = feedparser.parse(url)
                for entry in feed.entries[:6]:
                    title = entry.get("title", "").strip()
                    link = entry.get("link", "").strip()
                    summary = entry.get("summary", entry.get("description", "")).strip()
                    summary = re.sub(r"<[^>]+>", "", summary)[:250]
                    if title and link:
                        all_news.append({
                            "region": region,
                            "title": title,
                            "link": link,
                            "summary": summary,
                            "source": feed.feed.get("title", "Unknown")
                        })
            except Exception as e:
                logger.warning("RSS 失敗 (" + url + "): " + str(e))
                continue
    logger.info("共抓取 " + str(len(all_news)) + " 篇原始新聞")
    return all_news[:MAX_RAW_NEWS]


def call_gemini(prompt, max_tokens=4000):
    try:
        response = requests.post(
            "https://generativelanguage.googleapis.com/v1beta/models/" + GEMINI_MODEL + ":generateContent",
            headers={"Content-Type": "application/json"},
            params={"key": GEMINI_API_KEY},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": 0.7,
                    "maxOutputTokens": max_tokens
                }
            },
            timeout=90
        )
        response.raise_for_status()
        result = response.json()
        content = result["candidates"][0]["content"]["parts"][0]["text"]
        return content
    except Exception as e:
        logger.error("Gemini API 失敗: " + str(e))
        return None


def extract_json(text):
    if not text:
        return None
    text = re.sub(r"^```json\s*", "", text.strip())
    text = re.sub(r"\s*```$", "", text.strip())
    json_match = re.search(r"\{.*\}", text, re.DOTALL)
    if json_match:
        try:
            return json.loads(json_match.group())
        except json.JSONDecodeError:
            logger.error("JSON 解析失敗")
            return None
    return None


def generate_daily_prompt(news_list):
    news_text = ""
    for i, n in enumerate(news_list, 1):
        news_text += str(i) + ". [" + n["region"] + "] " + n["title"] + "\n"
        news_text += "   Source: " + n["source"] + "\n"
        news_text += "   Summary: " + n["summary"] + "\n"
        news_text += "   Link: " + n["link"] + "\n\n"

    prompt = "You are a bilingual financial editor for young investors (18-35) in Singapore.\n"
    prompt += "Today is " + TODAY_STR + " / " + TODAY_STR_EN + ". Weekday: " + str(WEEKDAY) + ".\n\n"
    prompt += "Here are today's financial news. Please select " + str(FINAL_COUNT) + " most relevant stories and output bilingual JSON.\n\n"
    prompt += "News:\n" + news_text + "\n"
    prompt += """Output strict JSON:
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
    return prompt


def generate_weekly_prompt(news_list):
    headlines = "\n".join(["- " + n["title"] for n in news_list[:30]])

    prompt = "You are a financial editor. Today is Friday. Based on this week's headlines, generate a weekly review for young investors.\n\n"
    prompt += "Headlines:\n" + headlines + "\n\n"
    prompt += """Output strict JSON:
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
}

Output ONLY valid JSON."""
    return prompt


def format_daily_zh(data):
    lines = ["🇨🇳 中文版 | ☀️ " + TODAY_STR + " 財經快報", "─" * 22]
    intro = data.get("intro_zh", "早安！來看看今天有哪些重要財經消息 👇")
    lines.append("")
    lines.append(intro)
    lines.append("")
    for item in data["news"]:
        lines.append(item["tag_zh"])
        lines.append("📰 " + item["title_zh"])
        lines.append("📝 " + item["why_zh"])
        lines.append(item["impact_zh"])
        lines.append("🔗 " + item["link"])
        lines.append("")
    outro = data.get("outro_zh", "💡 以上資訊僅供參考，不構成投資建議。")
    lines.append(outro)
    lines.append("📬 喜歡這類內容嗎？歡迎分享給朋友！")
    return "\n".join(lines)


def format_daily_en(data):
    lines = ["🇬🇧 English Version | ☀️ Daily Finance Brief - " + TODAY_STR_EN, "─" * 22]
    intro_en = "Good morning! Here are today's top stories 👇"
    intro = data.get("intro_en", intro_en)
    lines.append("")
    lines.append(intro)
    lines.append("")
    for item in data["news"]:
        lines.append(item["tag_en"])
        lines.append("📰 " + item["title_en"])
        lines.append("📝 " + item["why_en"])
        lines.append(item["impact_en"])
        lines.append("🔗 " + item["link"])
        lines.append("")
    outro = data.get("outro_en", "💡 For informational purposes only, not investment advice.")
    lines.append(outro)
    lines.append("📬 Find this helpful? Share it with a friend!")
    return "\n".join(lines)


def format_weekly(data):
    lines = ["📊 本周市場回顧 | Weekly Market Review", "─" * 22]
    lines.append("")
    lines.append("🔥 本周三大主題 / Top Themes:")
    lines.append("")
    for t in data["themes"]:
        lines.append(t["emoji"] + " " + t["title_zh"] + " / " + t["title_en"])
        lines.append("   " + t["desc_zh"])
        lines.append("   " + t["desc_en"])
        lines.append("")
    lines.append("📈 " + data["market_zh"])
    lines.append("📈 " + data["market_en"])
    lines.append("")
    lines.append("👀 " + data["watch_zh"])
    lines.append("👀 " + data["watch_en"])
    lines.append("")
    lines.append("💬 " + data["quote_zh"])
    lines.append("💬 " + data["quote_en"])
    return "\n".join(lines)


def send_telegram(message):
    url = "https://api.telegram.org/bot" + TELEGRAM_BOT_TOKEN + "/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "disable_web_page_preview": False}
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return True
    except Exception as e:
        logger.error("Telegram 發送失敗: " + str(e))
        return False


def main():
    logger.info("啟動 - " + TODAY_STR + " (Weekday: " + str(WEEKDAY) + ")")

    raw_news = fetch_rss()
    if not raw_news:
        logger.error("沒有抓到新聞")
        return

    daily_prompt = generate_daily_prompt(raw_news)
    daily_content = call_gemini(daily_prompt, max_tokens=4000)
    daily_data = extract_json(daily_content)

    if daily_data:
        msg_zh = format_daily_zh(daily_data)
        if msg_zh and send_telegram(msg_zh):
            logger.info("中文版發送成功")
        time.sleep(3)

        msg_en = format_daily_en(daily_data)
        if msg_en and send_telegram(msg_en):
            logger.info("英文版發送成功")
    else:
        logger.error("每日新聞生成失敗")

    if WEEKDAY == 4:
        time.sleep(3)
        weekly_prompt = generate_weekly_prompt(raw_news)
        weekly_content = call_gemini(weekly_prompt, max_tokens=2000)
        weekly_data = extract_json(weekly_content)

        if weekly_data:
            msg_weekly = format_weekly(weekly_data)
            if msg_weekly and send_telegram(msg_weekly):
                logger.info("本週回顧發送成功")
        else:
            logger.error("本週回顧生成失敗")

    logger.info("任務完成")


if __name__ == "__main__":
    main()
