#!/usr/bin/env python3
"""Daily bilingual news and content-opportunity scanner for Malaysians in Singapore."""

import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests


SG_TIME = timezone(timedelta(hours=8))
NOW = datetime.now(SG_TIME)
TODAY_STR = NOW.strftime("%Y年%m月%d日")
TODAY_STR_EN = NOW.strftime("%B %d, %Y")
WEEKDAY = NOW.weekday()

# ── FIX 1: Read BOT_MODE so internal and external can behave differently ──────
BOT_MODE = os.getenv("BOT_MODE", "external")   # "internal" or "external"

GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "30"))
FINAL_COUNT = int(os.getenv("FINAL_COUNT", "5"))
MIN_RELEVANCE_SCORE = int(os.getenv("MIN_RELEVANCE_SCORE", "5"))
MAX_ARTICLE_AGE_HOURS = int(os.getenv("MAX_ARTICLE_AGE_HOURS", "36"))
MONDAY_MAX_ARTICLE_AGE_HOURS = int(os.getenv("MONDAY_MAX_ARTICLE_AGE_HOURS", "72"))
MAX_ARTICLES_PER_SOURCE = 5

# Feeds are intentionally local and practical. Broad global-market and crypto feeds
# were removed because they produced stories with no clear cross-border money decision.
RSS_SOURCES = {
    # =========================================================
    # MALAYSIA
    # =========================================================
    "🇲🇾 Malaysia money & policy": [
        # News
        "https://www.malaymail.com/feed/rss/money",
        "https://www.malaymail.com/feed/rss/malaysia",
        "https://www.bernama.com/en/rssfeed.php",
        "https://www.thestar.com.my/rss/Business/Business-News/",

        # High-value official / primary sources
        "https://www.bnm.gov.my/rss",
        "https://www.dosm.gov.my/",
        "https://www.mof.gov.my/",
        "https://www.kwsp.gov.my/",
        "https://www.hasil.gov.my/",

        # Stronger business reporting
        "https://theedgemalaysia.com/",
    ],

    # =========================================================
    # SINGAPORE
    # =========================================================
    "🇸🇬 Singapore work & economy": [
        # News
        "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=10416",
        "https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml&category=6936",
        "https://www.businesstimes.com.sg/rss/economy-policy",
        "https://www.businesstimes.com.sg/rss/working-life",
        "https://www.businesstimes.com.sg/rss/personal-finance",

        # Work / career
        "https://hrmasia.com/feed/",

        # Official sources
        "https://www.mom.gov.sg/newsroom",
        "https://www.iras.gov.sg/latest-updates",
        "https://www.mof.gov.sg/news-resources/newsroom/",
        "https://www.singstat.gov.sg/",

        # Social / trending
        "https://mothership.sg/feed",
    ],

    # =========================================================
    # MALAYSIA <-> SINGAPORE
    # =========================================================
    "🇲🇾↔️🇸🇬 Cross-border life": [
        "https://www.ica.gov.sg/news-and-publications/newsroom",
        "https://www.lta.gov.sg/content/ltagov/en/newsroom.html",
        "https://www.mot.gov.my/",
        "https://www.jpj.gov.my/",
    ],

    # =========================================================
    # PERSONAL FINANCE
    # =========================================================
    "💳 Personal finance": [
        # Singapore
        "https://dollarsandsense.sg/feed/",
        "https://blog.moneysmart.sg/feed/",
        "https://blog.seedly.sg/feed/",

        # Malaysia
        "https://ringgitplus.com/en/blog/feed/",
        "https://www.imoney.my/articles/feed/",
        "https://ringgitohringgit.com/feed/",
    ],

    # =========================================================
    # PROPERTY / COST OF LIVING
    # =========================================================
    "🏠 Property, rent & living costs": [
        # Malaysia
        "https://www.propertyguru.com.my/news-rss/guru-views",
        "https://www.edgeprop.my/",

        # Singapore primary data
        "https://www.ura.gov.sg/Corporate/Media-Room/Media-Releases",
    ],

    # =========================================================
    # PODCASTS / EXPERT COMMENTARY
    # =========================================================
    "🎙️ Expert commentary & content ideas": [
        # Malaysia
        "https://www.bfm.my/",

        # Singapore
        "https://omny.fm/shows/moneyfm-893/playlists/podcast",

        # Existing Malaysian podcast
        "https://www.omnycontent.com/d/playlist/de62ff84-6498-49d0-a266-a9d50120c712/1139cb70-e7fa-476c-9ccc-ab090040379e/acb27c03-f82a-4061-9a6c-ab09004037a3/podcast.rss",
    ],
}

# Weighted toward decisions this audience actually makes. Multi-word phrases are
# deliberate: they distinguish actionable stories from generic country mentions.
KEYWORD_WEIGHTS = {
    7: [
        "sgd/myr", "myr/sgd", "singapore dollar", "malaysian workers in singapore",
        "malaysians working in singapore", "johor-singapore", "johor singapore",
        "cross-border worker", "cross border worker", "rts link", "rapid transit system",
    ],
    5: [
        "work pass", "employment pass", "s pass", "work permit", "foreign manpower",
        "foreign worker", "exchange rate", "currency conversion", "remittance",
        "money transfer", "epf", "kwsp", "cpf", "lhdn", "iras", "income tax",
        "tax relief", "double taxation", "js-sez", "special economic zone",
    ],
    3: [
        "malaysia", "malaysian", "ringgit", "myr", "singapore", "sgd", "johor",
        "causeway", "woodlands", "tuas", "bank negara", "mas", "interest rate",
        "fixed deposit", "savings account", "salary", "wages", "hiring", "layoff",
        "retrenchment", "job market", "rent", "rental", "housing", "property",
        "commute", "commuting", "customs", "immigration", "insurance", "healthcare",
        "medical cost", "cost of living", "inflation", "petrol", "toll", "visa",
        "credit card", "budget", "budgeting", "debt", "retirement", "savings",
        "financial planning",
    ],
    1: [
        "saving", "loan", "mortgage", "bank", "career", "consumer", "investment",
        "transport", "train", "bus",
    ],
}

MALAYSIA_TERMS = (
    "malaysia", "malaysian", "ringgit", "myr", "johor", "epf", "kwsp", "lhdn",
    "bank negara", "causeway", "iskandar",
)
SINGAPORE_TERMS = (
    "singapore", "singaporean", "sgd", "cpf", "iras", "mas", "work pass",
    "employment pass", "s pass", "woodlands", "tuas",
)
DECISION_TERMS = (
    "salary", "wage", "tax", "rent", "saving", "rate", "cost", "price", "fee",
    "loan", "mortgage", "insurance", "remittance", "exchange", "pass", "job",
    "hiring", "layoff", "commute", "property", "housing", "cpf", "epf", "kwsp",
    "credit card", "budget", "budgeting", "savings", "debt", "retirement",
    "healthcare", "financial planning",
)
GLOBAL_MARKET_TERMS = (
    "wall street", "nasdaq", "s&p 500", "dow jones", "bitcoin", "crypto",
    "federal reserve", "oil prices", "gold prices", "global stocks",
)

MALAYSIA_SOURCE_HOSTS = (
    "malaymail.com", "bernama.com", "thestar.com.my", "ringgitplus.com",
    "imoney.my", "ringgitohringgit.com", "propertyguru.com.my",
    "bnm.gov.my", "dosm.gov.my", "mof.gov.my", "kwsp.gov.my",
    "hasil.gov.my", "theedgemalaysia.com", "edgeprop.my", "bfm.my",
    "mot.gov.my", "jpj.gov.my",
)
SINGAPORE_SOURCE_HOSTS = (
    "channelnewsasia.com", "businesstimes.com.sg", "mothership.sg",
    "dollarsandsense.sg", "moneysmart.sg", "seedly.sg",
    "hrmasia.com", "mom.gov.sg", "iras.gov.sg", "mof.gov.sg",
    "singstat.gov.sg", "ica.gov.sg", "lta.gov.sg", "ura.gov.sg",
    "omny.fm",
)

TOPIC_RULES = {
    "💱 FX & remittance": ("sgd", "myr", "ringgit", "exchange", "remittance", "money transfer"),
    "💼 Jobs & passes": ("salary", "wage", "job", "hiring", "layoff", "work pass", "employment pass", "s pass"),
    "🧾 Tax & retirement": ("tax", "iras", "lhdn", "cpf", "epf", "kwsp", "retirement"),
    "🏠 Housing & costs": ("rent", "rental", "housing", "property", "cost of living", "inflation"),
    "🚆 Cross-border life": ("johor", "causeway", "rts", "commute", "customs", "immigration", "woodlands", "tuas"),
    "🏦 Banking & protection": ("bank", "interest rate", "fixed deposit", "loan", "insurance", "healthcare"),
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def clean_text(value, limit=500):
    value = html.unescape(re.sub(r"<[^>]+>", " ", value or ""))
    return re.sub(r"\s+", " ", value).strip()[:limit]


def canonical_url(url):
    parts = urlsplit(url.strip())
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def contains_term(text, term):
    """Match a word or phrase without treating it as part of a longer word."""
    return bool(re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", text))


def entry_datetime(entry):
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if not value:
            continue
        try:
            return datetime(*value[:6], tzinfo=timezone.utc).astimezone(SG_TIME)
        except (TypeError, ValueError, OverflowError):
            continue

    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if not value:
            continue
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except (TypeError, ValueError, OverflowError):
                continue
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(SG_TIME)
    return None


def article_age_hours(published_at):
    return max(0, (NOW - published_at).total_seconds() / 3600)


def freshness_limit_hours():
    return MONDAY_MAX_ARTICLE_AGE_HOURS if WEEKDAY == 0 else MAX_ARTICLE_AGE_HOURS


def is_fresh_article(published_at):
    # An article without a reliable timestamp cannot be guaranteed to be daily news.
    return published_at is not None and article_age_hours(published_at) <= freshness_limit_hours()


def relevance_score(title, summary, published_at=None, country_context=None):
    text = (title + " " + summary).lower()
    score = 0
    matched = []
    for weight, terms in KEYWORD_WEIGHTS.items():
        hits = [term for term in terms if contains_term(text, term)]
        if hits:
            score += min(len(hits), 3) * weight
            matched.extend(hits[:3])

    has_my = country_context == "malaysia" or any(contains_term(text, term) for term in MALAYSIA_TERMS)
    has_sg = country_context == "singapore" or any(contains_term(text, term) for term in SINGAPORE_TERMS)
    has_decision = any(contains_term(text, term) for term in DECISION_TERMS)
    if has_my and has_sg:
        score += 8
        matched.append("Malaysia + Singapore")
    if has_decision and (has_my or has_sg):
        score += 3
    if any(contains_term(text, term) for term in GLOBAL_MARKET_TERMS) and not (has_my or has_sg):
        score -= 8
    if not has_my and not has_sg:
        score -= 4

    if published_at:
        age_hours = article_age_hours(published_at)
        if age_hours <= 30:
            score += 2
        elif age_hours > 120:
            score -= 3
    return score, list(dict.fromkeys(matched))[:6]


def source_country_context(url):
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    if any(host == item or host.endswith("." + item) for item in MALAYSIA_SOURCE_HOSTS):
        return "malaysia"
    if any(host == item or host.endswith("." + item) for item in SINGAPORE_SOURCE_HOSTS):
        return "singapore"
    # This Omny playlist is BFM 89.9's Malaysian Ringgit & Sense programme.
    if host == "omnycontent.com":
        return "malaysia"
    return None


def classify_topic(title, summary):
    text = (title + " " + summary).lower()
    best_topic, best_hits = "🇲🇾🇸🇬 Policy & economy", 0
    for topic, terms in TOPIC_RULES.items():
        hits = sum(contains_term(text, term) for term in terms)
        if hits > best_hits:
            best_topic, best_hits = topic, hits
    return best_topic


def fetch_rss():
    candidates, seen = [], set()
    stale_count, undated_count = 0, 0
    headers = {"User-Agent": "MY-SG-News-Bot/2.0 (+RSS reader)"}
    for section, urls in RSS_SOURCES.items():
        for url in urls:
            try:
                logger.info("Fetching: %s", url)
                response = requests.get(url, headers=headers, timeout=25)
                response.raise_for_status()
                feed = feedparser.parse(response.content)
                if feed.bozo and not feed.entries:
                    raise ValueError(str(feed.bozo_exception))
                source = clean_text(feed.feed.get("title", "Unknown"), 80)
                for entry in feed.entries[:12]:
                    title = clean_text(entry.get("title", ""), 240)
                    link = entry.get("link", "").strip()
                    summary = clean_text(entry.get("summary", entry.get("description", "")))
                    key = canonical_url(link) if link else title.lower()
                    if not title or not link or key in seen:
                        continue
                    seen.add(key)
                    published_at = entry_datetime(entry)
                    if published_at is None:
                        undated_count += 1
                        continue
                    if not is_fresh_article(published_at):
                        stale_count += 1
                        continue
                    score, matched = relevance_score(
                        title, summary, published_at, source_country_context(url)
                    )
                    if score < MIN_RELEVANCE_SCORE:
                        continue
                    candidates.append({
                        "section": section,
                        "topic": classify_topic(title, summary),
                        "title": title,
                        "link": link,
                        "summary": summary,
                        "source": source,
                        "score": score,
                        "matched": matched,
                        "published_at": published_at.isoformat() if published_at else "",
                    })
            except Exception as exc:
                logger.warning("RSS failed (%s): %s", url, exc)

    candidates.sort(key=lambda item: item["score"], reverse=True)
    selected, source_counts = [], {}
    for item in candidates:
        count = source_counts.get(item["source"], 0)
        if count >= MAX_ARTICLES_PER_SOURCE:
            continue
        item["id"] = len(selected) + 1
        selected.append(item)
        source_counts[item["source"]] = count + 1
        if len(selected) >= MAX_CANDIDATES:
            break
    logger.info(
        "Kept %d relevant stories; rejected %d stale and %d undated entries (freshness limit: %dh)",
        len(selected), stale_count, undated_count, freshness_limit_hours(),
    )
    return selected


def call_groq(prompt, max_tokens=3500, retries=3):
    api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        logger.error("Missing GROQ_API_KEY")
        return None
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                json={
                    "model": GROQ_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.35,
                    "max_completion_tokens": max_tokens,
                    "reasoning_effort": "low",
                    "response_format": {"type": "json_object"},
                },
                timeout=90,
            )
            if response.status_code in (429, 503) and attempt < retries:
                time.sleep(5 * attempt)
                continue
            response.raise_for_status()
            return response.json()["choices"][0]["message"]["content"]
        except Exception as exc:
            logger.error("Groq attempt %d failed: %s", attempt, exc)
            if attempt < retries:
                time.sleep(5 * attempt)
    return None


def extract_json(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        try:
            return json.loads(match.group()) if match else None
        except json.JSONDecodeError:
            logger.error("Could not parse model JSON")
            return None


def news_for_prompt(news_list):
    blocks = []
    for n in news_list:
        blocks.append(
            f"ID {n['id']} | score {n['score']} | {n['topic']} | {n['source']}\n"
            f"Title: {n['title']}\nSummary: {n['summary']}\n"
            f"Signals: {', '.join(n['matched'])}"
        )
    return "\n\n".join(blocks)


# ── FIX 3 & 4: Separate prompts per mode ─────────────────────────────────────

def generate_internal_prompt(news_list):
    """Internal team prompt: focus on content opportunity for PYTCH creators."""
    return f"""You are the bilingual editor and content strategist for PYTCH. Your exact audience is Malaysians aged roughly 20-45 who work in Singapore, including daily commuters and people living in Singapore.

Today is {TODAY_STR_EN}. Choose exactly {min(FINAL_COUNT, len(news_list))} stories from the vetted candidates below.

Internal editorial test: for each story, ask "Can this become a strong PYTCH content idea that hits a real cross-border pain point for Malaysians working in Singapore?"

Rules:
- Prioritise stories with the strongest content opportunity: SGD/MYR, remittance, salaries, jobs and passes, tax, CPF/EPF, rent/property, cost of living, banking, insurance/healthcare, Causeway/RTS/JS-SEZ.
- Reject generic global markets, company earnings, crypto and investment-price updates unless there is a concrete MY-SG household consequence.
- Prefer a useful mix of topics and no more than two stories on the same topic.
- Use only candidate IDs. Never invent facts, URLs, numbers or policy details.
- 'decision' must be a specific action or question for the audience, not generic investment advice.
- 'content_angle' should be a practical PYTCH explainer, calculator, checklist, comparison, myth-buster or audience poll — be specific and creative.

Candidates:
{news_for_prompt(news_list)}

Return ONLY valid JSON:
{{
  "intro_en": "one-sentence opening for the team briefing",
  "news": [
    {{
      "source_id": 1,
      "tag_en": "emoji + English topic",
      "title_en": "English headline",
      "why_en": "why this specifically matters to Malaysians working in Singapore",
      "decision_en": "✅ one concrete decision/question",
      "content_angle_en": "🎬 PYTCH content opportunity — specific format and angle"
    }}
  ],
  "outro_en": "short internal note or editorial observation"
}}"""


def generate_external_prompt(news_list):
    """External audience prompt: bilingual, focus on relevance and actionability."""
    return f"""You are the bilingual editor and content strategist for PYTCH. Your exact audience is Malaysians aged roughly 20-45 who work in Singapore, including daily commuters and people living in Singapore.

Today is {TODAY_STR_EN}. Choose exactly {min(FINAL_COUNT, len(news_list))} stories from the vetted candidates below.

Editorial test: every selected story must answer, 'Is this relevant to Malaysians working in Singapore, and what money, career, housing, tax, banking, protection, or commuting decision does it create?'

Rules:
- Prioritise direct Malaysia-Singapore consequences: SGD/MYR, remittance, salaries, jobs and passes, tax, CPF/EPF, rent/property, cost of living, banking, insurance/healthcare, Causeway/RTS/JS-SEZ.
- Reject generic global markets, company earnings, crypto and investment-price updates unless the candidate has a concrete MY-SG household consequence.
- Prefer a useful mix of topics and no more than two stories on the same topic.
- Use only candidate IDs. Never invent facts, URLs, numbers or policy details.
- 'decision' must be a specific action or question for the audience, not generic investment advice.
- Use natural Simplified Chinese and Malaysian/Singaporean English. Keep every field concise.

Candidates:
{news_for_prompt(news_list)}

Return ONLY valid JSON:
{{
  "intro_zh": "one-sentence opening",
  "intro_en": "one-sentence opening",
  "news": [
    {{
      "source_id": 1,
      "tag_zh": "emoji + Chinese topic",
      "tag_en": "emoji + English topic",
      "title_zh": "Chinese headline",
      "title_en": "English headline",
      "why_zh": "why this specifically matters to Malaysians working in Singapore",
      "why_en": "why this specifically matters to Malaysians working in Singapore",
      "decision_zh": "✅ one concrete decision/question",
      "decision_en": "✅ one concrete decision/question"
    }}
  ],
  "outro_zh": "short informational disclaimer",
  "outro_en": "short informational disclaimer"
}}"""


def hydrate_internal(data, news_list):
    """Validate and hydrate internal (English-only) daily data."""
    lookup = {n["id"]: n for n in news_list}
    clean_items, used = [], set()
    if not isinstance(data, dict) or not isinstance(data.get("news"), list):
        return None
    required = ("tag_en", "title_en", "why_en", "decision_en", "content_angle_en")
    for item in data["news"]:
        try:
            source_id = int(item.get("source_id"))
        except (TypeError, ValueError):
            continue
        source = lookup.get(source_id)
        if not source or source_id in used or not all(item.get(key) for key in required):
            continue
        used.add(source_id)
        item["link"] = source["link"]
        item["source"] = source["source"]
        clean_items.append(item)
    if not clean_items:
        return None
    data["news"] = clean_items[:FINAL_COUNT]
    return data


def hydrate_external(data, news_list):
    """Validate and hydrate external (bilingual) daily data."""
    lookup = {n["id"]: n for n in news_list}
    clean_items, used = [], set()
    if not isinstance(data, dict) or not isinstance(data.get("news"), list):
        return None
    required = (
        "tag_zh", "tag_en", "title_zh", "title_en",
        "why_zh", "why_en", "decision_zh", "decision_en",
    )
    for item in data["news"]:
        try:
            source_id = int(item.get("source_id"))
        except (TypeError, ValueError):
            continue
        source = lookup.get(source_id)
        if not source or source_id in used or not all(item.get(key) for key in required):
            continue
        used.add(source_id)
        item["link"] = source["link"]
        item["source"] = source["source"]
        clean_items.append(item)
    if not clean_items:
        return None
    data["news"] = clean_items[:FINAL_COUNT]
    return data


def generate_weekly_prompt(news_list):
    return f"""You are PYTCH's bilingual content strategist. It is Friday. Based only on these vetted MY-SG candidate headlines, identify three audience themes and next week's content plan for Malaysians working in Singapore.

{news_for_prompt(news_list)}

Return ONLY valid JSON:
{{"themes":[{{"emoji":"💱","title_zh":"主题","title_en":"Theme","desc_zh":"具体影响","desc_en":"Concrete impact"}}],"watch_zh":"下周留意的决定","watch_en":"Decision to watch next week","content_zh":"下周最值得制作的PYTCH内容","content_en":"Best PYTCH content opportunity for next week"}}"""


# ── FIX 3: Internal format — English only, includes content_angle ─────────────

def format_internal(data):
    lines = [
        "🔒 PYTCH Internal | Content Radar · " + TODAY_STR_EN,
        "─" * 22,
        "",
        data.get("intro_en", ""),
        "",
    ]
    for item in data["news"]:
        lines.extend([
            item["tag_en"],
            "📰 " + item["title_en"],
            "📝 " + item["why_en"],
            item["decision_en"],
            item["content_angle_en"],
            "🔗 " + item["link"],
            "",
        ])
    lines.append(data.get("outro_en", "Internal use only."))
    return "\n".join(lines)


# ── FIX 3: External format — bilingual, NO content_angle ─────────────────────

def format_external(data, language):
    zh = language == "zh"
    lines = [
        ("🇨🇳 中文版 | 🇲🇾→🇸🇬 跨境钱事 · " + TODAY_STR) if zh
        else ("🇬🇧 English | 🇲🇾→🇸🇬 Cross-Border Money Brief · " + TODAY_STR_EN),
        "─" * 22,
        "",
        data.get("intro_zh" if zh else "intro_en", ""),
        "",
    ]
    suffix = "zh" if zh else "en"
    for item in data["news"]:
        lines.extend([
            item[f"tag_{suffix}"],
            "📰 " + item[f"title_{suffix}"],
            "📝 " + item[f"why_{suffix}"],
            item[f"decision_{suffix}"],
            # NOTE: content_angle intentionally omitted — internal only
            "🔗 " + item["link"],
            "",
        ])
    fallback = "💡 仅供参考，不构成财务、税务或移民建议。" if zh else "💡 For information only; not financial, tax or immigration advice."
    lines.append(data.get("outro_zh" if zh else "outro_en", fallback))
    return "\n".join(lines)


def format_weekly(data):
    lines = ["📊 MY-SG Weekly Content Radar", "─" * 22, ""]
    for theme in data.get("themes", [])[:3]:
        lines.extend([
            f"{theme.get('emoji', '🔎')} {theme.get('title_zh', '')} / {theme.get('title_en', '')}",
            theme.get("desc_zh", ""), theme.get("desc_en", ""), "",
        ])
    lines.extend([
        "👀 " + data.get("watch_zh", ""),
        "👀 " + data.get("watch_en", ""), "",
        "🎬 " + data.get("content_zh", ""),
        "🎬 " + data.get("content_en", ""),
    ])
    return "\n".join(lines)


# ── FIX 2: Pass message_thread_id to Telegram when env var is set ────────────

def send_telegram(message):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    thread_id = os.getenv("TELEGRAM_MESSAGE_THREAD_ID")   # only set for internal group topics

    if not token or not chat_id:
        logger.error("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
        return False

    # Telegram text messages are limited to 4,096 characters. Split on paragraph
    # boundaries so a richer content-opportunity brief still arrives reliably.
    chunks, current = [], ""
    for paragraph in message.split("\n\n"):
        proposed = paragraph if not current else current + "\n\n" + paragraph
        if len(proposed) <= 4000:
            current = proposed
        else:
            if current:
                chunks.append(current)
            current = paragraph
    if current:
        chunks.append(current)

    for chunk in chunks:
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if thread_id:
            payload["message_thread_id"] = int(thread_id)   # route to correct topic thread

        try:
            response = requests.post(
                "https://api.telegram.org/bot" + token + "/sendMessage",
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
        except Exception as exc:
            logger.error("Telegram send failed: %s", exc)
            return False
        time.sleep(1)
    return True


# ── FIX 1 & 4: Branch on BOT_MODE in main() ──────────────────────────────────

def main():
    logger.info("Starting MY-SG news scan for %s (mode: %s)", TODAY_STR_EN, BOT_MODE)
    raw_news = fetch_rss()
    if not raw_news:
        logger.error("No stories met the MY-SG relevance threshold")
        return False

    if BOT_MODE == "internal":
        # Internal channel: English only, includes content angles for the PYTCH team
        daily = hydrate_internal(
            extract_json(call_groq(generate_internal_prompt(raw_news))),
            raw_news,
        )
        if not daily:
            logger.error("Internal daily brief generation or validation failed")
            return False
        ok = send_telegram(format_internal(daily))

        # Friday weekly content radar (internal only)
        if WEEKDAY == 4:
            weekly = extract_json(call_groq(generate_weekly_prompt(raw_news), max_tokens=1800))
            if weekly:
                time.sleep(2)
                ok = send_telegram(format_weekly(weekly)) and ok
            else:
                logger.error("Weekly content radar generation failed")
                ok = False

    else:
        # External channel: bilingual (Chinese then English), no content angles
        daily = hydrate_external(
            extract_json(call_groq(generate_external_prompt(raw_news))),
            raw_news,
        )
        if not daily:
            logger.error("External daily brief generation or validation failed")
            return False
        ok = send_telegram(format_external(daily, "zh"))
        time.sleep(2)
        ok = send_telegram(format_external(daily, "en")) and ok

    logger.info("Task completed (mode: %s)", BOT_MODE)
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
