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
from urllib.parse import urljoin, urlsplit, urlunsplit

import feedparser
import requests


SG_TIME = timezone(timedelta(hours=8))
NOW = datetime.now(SG_TIME)
TODAY_STR = NOW.strftime("%Y年%m月%d日")
TODAY_STR_EN = NOW.strftime("%B %d, %Y")
WEEKDAY = NOW.weekday()

# ── FIX 1: Read BOT_MODE so internal and external can behave differently ──────
BOT_MODE = os.getenv("BOT_MODE", "external")   # "internal" or "external"

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "30"))
# Internal has no editorial cap (see generate_internal_prompt) — this is only a
# safety valve against a malformed/runaway model response, not a target count.
INTERNAL_SAFETY_CAP = int(os.getenv("INTERNAL_SAFETY_CAP", "20"))
EXTERNAL_MAX_STORIES = int(os.getenv("EXTERNAL_MAX_STORIES", "6"))
MIN_RELEVANCE_SCORE = int(os.getenv("MIN_RELEVANCE_SCORE", "5"))
MAX_ARTICLE_AGE_HOURS = int(os.getenv("MAX_ARTICLE_AGE_HOURS", "36"))
MONDAY_MAX_ARTICLE_AGE_HOURS = int(os.getenv("MONDAY_MAX_ARTICLE_AGE_HOURS", "72"))
# The internal workflow runs at ~12am and ~9am SGT; the Friday weekly recap
# should only send once, so it's gated to runs at/after this SGT hour.
WEEKLY_RECAP_MIN_HOUR = int(os.getenv("WEEKLY_RECAP_MIN_HOUR", "6"))
MAX_ARTICLES_PER_SOURCE = 5
FX_MEANINGFUL_MOVE_PCT = float(os.getenv("FX_MEANINGFUL_MOVE_PCT", "0.5"))
FX_EXAMPLE_MYR_AMOUNT = 5000

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
        "https://www.thestar.com.my/rss/Business/Business-News/",  # TODO(dead): verified 404 Sep 2026 — thestar.com.my dropped RSS entirely (no autodiscovery link on homepage, all /rss/* paths 404). No replacement feed found; needs a WEB_SOURCES scraper if kept.

        # High-value official / primary sources
        # BNM moved to WEB_SOURCES (scraped from /pr) — /rss was never a real feed.
        "https://www.dosm.gov.my/",  # TODO(web-scrape): homepage has no server-rendered listing at all, needs a JS-capable fetch (not attempted — adds a headless-browser dependency)
        "https://www.mof.gov.my/",  # TODO(web-scrape): homepage has no server-rendered listing at all, needs research for a real newsroom subpage
        "https://www.kwsp.gov.my/",  # TODO(web-scrape): verified Sep 2026 — Cloudflare bot-challenge (403, cf-mitigated: challenge), not just a missing feed. Not scrapeable without a headless browser.
        "https://www.hasil.gov.my/feed/",  # real WordPress feed (site's homepage URL is NOT a feed)

        # Stronger business reporting
        "https://theedgemalaysia.com/",  # TODO(dead): verified Sep 2026 — bare homepage, no RSS autodiscovery link, no /sitemap.xml. feeds.theedgemarkets.com (old domain) also 404s. Needs a WEB_SOURCES scraper if kept.
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

        # Official sources — MOM and MOF SG moved to WEB_SOURCES (scraped, see
        # below); neither ever had a real RSS feed to begin with.
        "https://www.singstat.gov.sg/",  # TODO(web-scrape): homepage has no article-style listing (release-calendar site), needs a different approach or skip

        # Social / trending
        "https://mothership.sg/feed",
    ],

    # =========================================================
    # MALAYSIA <-> SINGAPORE
    # =========================================================
    "🇲🇾↔️🇸🇬 Cross-border life": [
        "https://www.ica.gov.sg/news-and-publications/newsroom",  # TODO(web-scrape): no working RSS, currently yields 0 candidates
        # LTA moved to WEB_SOURCES (scraped) — its page never had a real RSS feed.
        "https://www.mot.gov.my/",  # TODO(web-scrape): no working RSS, currently yields 0 candidates
        "https://www.jpj.gov.my/",  # TODO(web-scrape): no working RSS, currently yields 0 candidates
    ],

    # =========================================================
    # PERSONAL FINANCE
    # =========================================================
    "💳 Personal finance": [
        # Singapore
        "https://dollarsandsense.sg/feed/",  # TODO(dead): verified Sep 2026 — now serves a JS-reload bot-challenge page (not RSS) to plain HTTP clients regardless of User-Agent, same as kwsp.gov.my. Not fetchable without a headless browser.
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
        "https://www.edgeprop.my/",  # TODO(dead): verified Sep 2026 — the site's own "subscribe to RSS" page (edgeprop.my/content/subscribe-malaysia-rss) 307-redirects to /news with no feed content. No working RSS found.

        # Singapore primary data
        "https://www.ura.gov.sg/Corporate/Media-Room/Media-Releases",
    ],

    # =========================================================
    # PODCASTS / EXPERT COMMENTARY
    # =========================================================
    "🎙️ Expert commentary & content ideas": [
        # Malaysia: bfm.my itself has no RSS (bare homepage, verified Sep 2026,
        # dropped from this list) — BFM's "Ringgit & Sense" show is already
        # covered below via its real Omny feed, confirmed against Apple
        # Podcasts' own feedUrl for the show (id 430785286).

        # Singapore: omny.fm/shows/moneyfm-893/playlists/podcast is a webpage,
        # not a feed (0 entries) — this is its real RSS feed, found by
        # following the redirect from the site's own "podcast.rss" link.
        # ?pageSize=20 is load-bearing: the unpaginated feed is the station's
        # entire back-catalog (75MB, tens of thousands of entries, ~17s to
        # download) since it's a 24/7 radio playlist, not a normal podcast.
        "https://www.omnycontent.com/d/playlist/d9486183-3dd4-4ad6-aebe-a4c1008455d5/2894f3bc-c57f-4983-8afd-b321006effbf/91bdd19a-daf2-4cac-817e-b321006f0542/podcast.rss?pageSize=20",

        # Existing Malaysian podcast (BFM's Ringgit & Sense)
        "https://www.omnycontent.com/d/playlist/de62ff84-6498-49d0-a266-a9d50120c712/1139cb70-e7fa-476c-9ccc-ab090040379e/acb27c03-f82a-4061-9a6c-ab09004037a3/podcast.rss?pageSize=20",
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
# Boss's explicit downrank list: corporate/market noise that isn't a household
# decision for this audience, even when it technically mentions MY/SG.
DOWNRANK_TERMS = (
    "ceo appointment", "appointed as group ceo", "appointed as ceo",
    "quarterly earnings", "full-year results", "q1 results", "q2 results",
    "q3 results", "q4 results", "ipo", "initial public offering",
    "credit card promotion", "cashback promotion", "sign-up bonus",
    "welcome bonus", "cabinet reshuffle", "by-election", "merger and acquisition",
)

MALAYSIA_SOURCE_HOSTS = (
    "malaymail.com", "bernama.com", "thestar.com.my", "ringgitplus.com",
    "imoney.my", "ringgitohringgit.com", "propertyguru.com.my",
    "bnm.gov.my", "dosm.gov.my", "mof.gov.my", "kwsp.gov.my",
    "hasil.gov.my", "theedgemalaysia.com", "edgeprop.my",
    "mot.gov.my", "jpj.gov.my",
)
SINGAPORE_SOURCE_HOSTS = (
    "channelnewsasia.com", "businesstimes.com.sg", "mothership.sg",
    "dollarsandsense.sg", "moneysmart.sg", "seedly.sg",
    "hrmasia.com", "mom.gov.sg", "iras.gov.sg", "mof.gov.sg",
    "singstat.gov.sg", "ica.gov.sg", "lta.gov.sg", "ura.gov.sg",
)

# Boss's source hierarchy: Tier 1 (official/primary) > Tier 2 (established media)
# > Tier 3 (audience angles / personal finance / commentary). Unlisted sources
# (e.g. HRMASIA, URA) get no tier boost but aren't excluded.
TIER1_HOSTS = (
    "mom.gov.sg", "iras.gov.sg", "ica.gov.sg", "lta.gov.sg", "singstat.gov.sg",
    "mof.gov.sg", "bnm.gov.my", "dosm.gov.my", "kwsp.gov.my", "hasil.gov.my",
    "mof.gov.my", "mot.gov.my", "jpj.gov.my",
)
TIER2_HOSTS = (
    "channelnewsasia.com", "businesstimes.com.sg", "bernama.com",
    "thestar.com.my", "malaymail.com", "theedgemalaysia.com",
)
TIER3_HOSTS = (
    "omnycontent.com", "moneysmart.sg", "seedly.sg",
    "dollarsandsense.sg", "ringgitplus.com", "imoney.my", "ringgitohringgit.com",
    "mothership.sg", "propertyguru.com.my", "edgeprop.my",
)

TOPIC_RULES = {
    "💱 FX & remittance": ("sgd", "myr", "ringgit", "exchange", "remittance", "money transfer"),
    "💼 Jobs & passes": ("salary", "wage", "job", "hiring", "layoff", "work pass", "employment pass", "s pass"),
    "🧾 Tax & retirement": ("tax", "iras", "lhdn", "cpf", "epf", "kwsp", "retirement"),
    "🏠 Housing & costs": ("rent", "rental", "housing", "property", "cost of living", "inflation"),
    "🚆 Cross-border life": ("johor", "causeway", "rts", "commute", "customs", "immigration", "woodlands", "tuas"),
    "🏦 Banking & protection": ("bank", "interest rate", "fixed deposit", "loan", "insurance", "healthcare"),
}

# Tier 1 government sites that don't publish RSS/Atom at all (per the boss's
# spec: "Web: Scrape newsroom ... pages when no reliable RSS feed exists. Do
# not invent RSS feeds."). Each entry's item_pattern is reverse-engineered
# against the site's actual markup — verified working as of Sep 2026; a site
# redesign will silently break it (fetch_web logs a warning and yields 0
# candidates for that source, same as a dead RSS feed would).
WEB_SOURCES = {
    "🇲🇾 Malaysia money & policy": [
        {
            # BNM press releases: a plain HTML table, <td><p>DD Mon YYYY</p></td>
            # followed by <td><p><a href="...">Title</a></p></td>. Absolute
            # links already, so "base" is unused here but kept for consistency.
            "url": "https://www.bnm.gov.my/pr",
            "base": "https://www.bnm.gov.my",
            "item_pattern": re.compile(
                r'<tr>\s*<td>\s*<p>(?P<date>\d{1,2}\s+\w+\s+\d{4})</p>\s*</td>\s*'
                r'<td>\s*<p><a href="(?P<link>[^"]+)"[^>]*>(?P<title>[^<]+)</a></p>',
                re.S,
            ),
            "date_format": "%d %b %Y",
        },
    ],
    "🇸🇬 Singapore work & economy": [
        {
            # MOM newsroom: <time datetime="YYYY-M-D">...</time> immediately
            # followed by the headline link.
            "url": "https://www.mom.gov.sg/newsroom",
            "base": "https://www.mom.gov.sg",
            "item_pattern": re.compile(
                r'<time[^>]*datetime="(?P<date>\d{4}-\d{1,2}-\d{1,2})"[^>]*>.*?'
                r'<a\s+href="(?P<link>[^"]+)"[^>]*>(?P<title>[^<]+)</a>',
                re.S,
            ),
            "date_format": "%Y-%m-%d",
        },
        {
            # IRAS latest-updates: <article class="eyd-article-item ..."> block
            # with a <h3><a href=...>Title</a></h3> and a "DD Mon YYYY" date span.
            "url": "https://www.iras.gov.sg/latest-updates",
            "base": "https://www.iras.gov.sg",
            "item_pattern": re.compile(
                r'<article class="eyd-article-item[^"]*">\s*'
                r'<section class="eyd-article-item__text">\s*'
                r'<h3><a href=["\']?(?P<link>[^"\'>]+)["\']?>(?P<title>[^<]+)</a></h3>.*?'
                r'meta--date">(?P<date>[^<]+)</span>',
                re.S,
            ),
            "date_format": "%d %b %Y",
        },
        {
            # MOF SG newsroom: <a href="/news-resources/newsroom/...">, a date
            # paragraph, then the title in a <span class="line-clamp-3">.
            "url": "https://www.mof.gov.sg/news-resources/newsroom/",
            "base": "https://www.mof.gov.sg",
            "item_pattern": re.compile(
                r'<a[^>]*href="(?P<link>/news-resources/newsroom/[^"]+)"[^>]*>\s*'
                r'<p[^>]*>(?P<date>\d{1,2}\s+\w+\s+\d{4})</p>.*?'
                r'<span class="line-clamp-3"[^>]*>(?P<title>[^<]+)</span>',
                re.S,
            ),
            "date_format": "%d %B %Y",
        },
    ],
    "🇲🇾↔️🇸🇬 Cross-border life": [
        {
            # LTA newsroom is a full archive back to 2020, oldest-first in the
            # HTML — the hidden <span class="date"> ISO string is what we sort
            # on; fetch_web re-sorts by date so ordering here doesn't matter.
            "url": "https://www.lta.gov.sg/content/ltagov/en/newsroom.html",
            "base": "https://www.lta.gov.sg",
            "item_pattern": re.compile(
                r'<h5[^>]*>\s*<a href="(?P<link>[^"]+)"[^>]*>(?P<title>[^<]+)</a>\s*</h5>.*?'
                r'<span class="date"[^>]*>(?P<date>\d{4}-\d{1,2}-\d{1,2})</span>',
                re.S,
            ),
            "date_format": "%Y-%m-%d",
        },
    ],
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


def relevance_score(title, summary, published_at=None, country_context=None, tier=None):
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
    if any(contains_term(text, term) for term in DOWNRANK_TERMS):
        score -= 5
    if not has_my and not has_sg:
        score -= 4

    if tier == 1:
        score += 6
        matched.append("Tier 1 source")
    elif tier == 2:
        score += 2

    if published_at:
        age_hours = article_age_hours(published_at)
        if age_hours <= 30:
            score += 2
        elif age_hours > 120:
            score -= 3
    return score, list(dict.fromkeys(matched))[:6]


def source_domain(url):
    """Registered host, used to cap MAX_ARTICLES_PER_SOURCE per publisher.
    Feed <title> tags are unreliable for this: some publishers give every
    section feed the same generic title (merging genuinely distinct feeds
    into one bucket), others give the same publisher different titles per
    section (letting one publisher blow past the cap). The domain is stable
    either way.
    """
    return urlsplit(url).netloc.lower().removeprefix("www.")


def source_country_context(url):
    host = urlsplit(url).netloc.lower().removeprefix("www.")
    if any(host == item or host.endswith("." + item) for item in MALAYSIA_SOURCE_HOSTS):
        return "malaysia"
    if any(host == item or host.endswith("." + item) for item in SINGAPORE_SOURCE_HOSTS):
        return "singapore"
    # omnycontent.com is shared podcast-hosting infra for both a Malaysian and
    # a Singaporean show, so the host alone can't tell them apart — key off
    # each feed's playlist ID instead.
    if host == "omnycontent.com":
        if "de62ff84-6498-49d0-a266-a9d50120c712" in url:  # BFM 89.9 Ringgit & Sense
            return "malaysia"
        if "d9486183-3dd4-4ad6-aebe-a4c1008455d5" in url:  # MONEY FM 89.3
            return "singapore"
    return None


def source_tier(url):
    host = urlsplit(url).netloc.lower().removeprefix("www.")

    def matches(hosts):
        return any(host == item or host.endswith("." + item) for item in hosts)

    if matches(TIER1_HOSTS):
        return 1
    if matches(TIER2_HOSTS):
        return 2
    if matches(TIER3_HOSTS):
        return 3
    return None


def classify_topic(title, summary):
    text = (title + " " + summary).lower()
    best_topic, best_hits = "🇲🇾🇸🇬 Policy & economy", 0
    for topic, terms in TOPIC_RULES.items():
        hits = sum(contains_term(text, term) for term in terms)
        if hits > best_hits:
            best_topic, best_hits = topic, hits
    return best_topic


def fetch_web(candidates, seen):
    """Scrape the Tier 1 sources with no working RSS/Atom feed (WEB_SOURCES),
    appending onto the shared `candidates` list and `seen` dedup set used by
    fetch_rss. Returns (stale_count, undated_count) for the combined log line.

    Sites list their archive in whatever order they please — some newest-first
    (MOM, IRAS, MOF SG), some oldest-first (LTA, whose page is a full archive
    back to 2020). So every match is parsed and sorted by date before capping
    to the most recent N, rather than trusting "the first matches in the HTML
    are the newest ones."
    """
    headers = {"User-Agent": "MY-SG-News-Bot/2.0 (+web reader)"}
    stale_count, undated_count = 0, 0
    for section, sources in WEB_SOURCES.items():
        for cfg in sources:
            url = cfg["url"]
            try:
                logger.info("Scraping: %s", url)
                response = requests.get(url, headers=headers, timeout=25)
                response.raise_for_status()
                body = response.text
                source_name = source_domain(url)
                tier = source_tier(url)

                parsed_items = []
                for match in cfg["item_pattern"].finditer(body):
                    title = clean_text(html.unescape(match.group("title")), 240)
                    link = urljoin(cfg["base"], match.group("link").strip())
                    if not title:
                        continue
                    try:
                        published_at = datetime.strptime(
                            match.group("date").strip(), cfg["date_format"]
                        ).replace(tzinfo=SG_TIME)
                    except ValueError:
                        undated_count += 1
                        continue
                    parsed_items.append((published_at, title, link))
                parsed_items.sort(key=lambda item: item[0], reverse=True)

                added = 0
                for published_at, title, link in parsed_items:
                    if added >= 12:
                        break
                    key = canonical_url(link)
                    if key in seen:
                        continue
                    seen.add(key)
                    if not is_fresh_article(published_at):
                        stale_count += 1
                        continue
                    score, matched = relevance_score(
                        title, "", published_at, source_country_context(url), tier
                    )
                    if score < MIN_RELEVANCE_SCORE:
                        continue
                    added += 1
                    candidates.append({
                        "section": section,
                        "topic": classify_topic(title, ""),
                        "title": title,
                        "link": link,
                        "summary": "",
                        "source": source_name,
                        "source_domain": source_name,
                        "tier": tier,
                        "score": score,
                        "matched": matched,
                        "published_at": published_at.isoformat(),
                    })
            except Exception as exc:
                logger.warning("Web scrape failed (%s): %s", url, exc)
    return stale_count, undated_count


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
                tier = source_tier(url)
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
                        title, summary, published_at, source_country_context(url), tier
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
                        "source_domain": source_domain(url),
                        "tier": tier,
                        "score": score,
                        "matched": matched,
                        "published_at": published_at.isoformat() if published_at else "",
                    })
            except Exception as exc:
                logger.warning("RSS failed (%s): %s", url, exc)

    web_stale, web_undated = fetch_web(candidates, seen)
    stale_count += web_stale
    undated_count += web_undated

    candidates.sort(key=lambda item: item["score"], reverse=True)
    selected, source_counts = [], {}
    for item in candidates:
        count = source_counts.get(item["source_domain"], 0)
        if count >= MAX_ARTICLES_PER_SOURCE:
            continue
        item["id"] = len(selected) + 1
        selected.append(item)
        source_counts[item["source_domain"]] = count + 1
        if len(selected) >= MAX_CANDIDATES:
            break
    logger.info(
        "Kept %d relevant stories; rejected %d stale and %d undated entries (freshness limit: %dh)",
        len(selected), stale_count, undated_count, freshness_limit_hours(),
    )
    return selected


def fetch_fx_move():
    """SGD/MYR spot rate + 7-day move, for the external channel's FX Radar block."""
    try:
        today = requests.get(
            "https://api.frankfurter.app/latest",
            params={"from": "SGD", "to": "MYR"},
            timeout=15,
        )
        today.raise_for_status()
        rate_now = today.json()["rates"]["MYR"]

        week_ago_date = (NOW - timedelta(days=7)).strftime("%Y-%m-%d")
        hist = requests.get(
            f"https://api.frankfurter.app/{week_ago_date}",
            params={"from": "SGD", "to": "MYR"},
            timeout=15,
        )
        hist.raise_for_status()
        rate_week_ago = hist.json()["rates"]["MYR"]

        pct_move = (rate_now - rate_week_ago) / rate_week_ago * 100
        return {
            "rate": rate_now,
            "pct_move": pct_move,
            "cost_now": FX_EXAMPLE_MYR_AMOUNT / rate_now,
            "cost_week_ago": FX_EXAMPLE_MYR_AMOUNT / rate_week_ago,
        }
    except Exception as exc:
        logger.warning("FX fetch failed: %s", exc)
        return None


def should_show_fx(fx):
    """Surface FX when the move is meaningful, or as a lightweight weekly (Monday) update."""
    if not fx:
        return False
    return abs(fx["pct_move"]) >= FX_MEANINGFUL_MOVE_PCT or WEEKDAY == 0


def call_openai(prompt, max_tokens=3500, retries=3):
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        logger.error("Missing OPENAI_API_KEY")
        return None
    for attempt in range(1, retries + 1):
        try:
            response = requests.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": "Bearer " + api_key, "Content-Type": "application/json"},
                json={
                    "model": OPENAI_MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.35,
                    "max_tokens": max_tokens,
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
            logger.error("OpenAI attempt %d failed: %s", attempt, exc)
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
        tier_label = f"Tier {n['tier']}" if n.get("tier") else "unlisted tier"
        blocks.append(
            f"ID {n['id']} | score {n['score']} | {n['topic']} | {n['source']} ({tier_label})\n"
            f"Title: {n['title']}\nSummary: {n['summary']}\n"
            f"Signals: {', '.join(n['matched'])}"
        )
    return "\n\n".join(blocks)


# ── Internal prompt: PYTCH Internal Content Radar spec ───────────────────────

def generate_internal_prompt(news_list):
    return f"""You are the PYTCH content strategist reviewing today's vetted MY-SG candidate stories for the internal team. You are not a general finance news summariser — your only question is "Should PYTCH make content about this?"

Today is {TODAY_STR_EN}.

Core audience: Malaysians aged roughly 23-35 who currently work in Singapore, are considering moving to Singapore for work, earn SGD while managing commitments in Malaysia, may eventually return to Malaysia, or are making financial/life decisions across both countries.

Apply the Mandatory PYTCH Content Test to every candidate:
1. Would a Malaysian working in Singapore genuinely care?
2. Does this affect their money, job, cost of living or major life decisions?
3. Is the MY/SG connection direct?
4. Is there a real audience tension or decision?
5. Can this become useful or engaging PYTCH content?
6. Is there a reason to cover it now?
7. Is the source reliable enough? Weight Tier 1 official sources highest, verify Tier 2 major policy claims against Tier 1 where possible, and use Tier 3 mainly for audience angles/pain points — never as primary evidence for a major policy claim.
8. Is this better than the other stories available today?

If the answer is weak on any of these, skip the story entirely — do not include it in your output.

Classify every story you keep as exactly one of:
- "🔥 TIMELY" — strong opportunity, ideally actioned within 24-72 hours
- "💡 EVERGREEN" — strong audience pain point/decision, no immediate urgency
- "👀 WATCH" — potentially important, but needs more information or confirmation

Actively downrank/drop unless there's a clear, direct MY→SG audience impact: US stock movements, random individual SG/MY stock movements, crypto price movements, global M&A, company earnings, Wall Street commentary, generic investment outlooks, CEO appointments, corporate press releases, general business news, political news with no direct audience impact, lifestyle stories with no money/work/cross-border relevance. Do not force a MY→SG angle onto weak stories.

There is no required minimum or maximum number of stories. Include every story that genuinely clears the Mandatory PYTCH Content Test, and none that don't — most days that means 1-4 stories. If you find yourself regularly returning more than 6-8, you are being too generous: tighten the bar rather than padding the list. One genuinely strong opportunity is better than five mediocre ideas.

For each story you keep:
- "story" explains what happened in one sentence.
- "audience_tension" must be the actual thought, fear, frustration or decision behind it — written as the audience's own inner voice (e.g. "I'm earning S$4K now, but can I actually afford to stop renting a room?"), never a generic description like "Rental affordability remains a concern."
- "why_it_matters" explains the direct MY→SG impact.
- "why_now" explains the urgency; if there is none, say plainly that it's evergreen.
- "pytch_angle" suggests one strong hook, question or framing, starting from the audience tension — do not automatically default to a calculator/checklist/explainer unless that's genuinely the strongest format.
- "best_format" must be exactly one of: "Timely Reel" (fast-moving news, one clear implication), "Carousel" (comparisons, step-by-step decisions, multiple implications or numbers), "Street Cents" (the audience itself has an interesting opinion/behaviour/decision to reveal), "Expert Reacts" (needs interpretation from a specialist), "FOMO FIX" (a larger life/financial decision with multiple trade-offs), or "Tool / Calculator / Guide" (only when the audience genuinely benefits from calculating or completing something).
- "audience_relevance", "content_potential" and "priority" must each be exactly "High", "Medium" or "Low".
- Use only candidate IDs below. Never invent facts, URLs, numbers or policy details.

Candidates:
{news_for_prompt(news_list)}

Return ONLY valid JSON:
{{
  "news": [
    {{
      "source_id": 1,
      "classification": "🔥 TIMELY",
      "topic": "short topic label",
      "story": "what happened, in one sentence",
      "audience_tension": "the audience's actual thought, worry or decision",
      "why_it_matters": "the direct MY→SG impact",
      "why_now": "the urgency, or a note that this is evergreen",
      "pytch_angle": "one strong hook, question or framing",
      "best_format": "Timely Reel",
      "audience_relevance": "High",
      "content_potential": "High",
      "priority": "High"
    }}
  ]
}}
If nothing clears the bar today, return {{"news": []}}."""


def hydrate_internal(data, news_list):
    """Validate and hydrate the internal PYTCH Content Radar output. An empty
    but well-formed 'news' array is valid — it means nothing cleared the bar."""
    lookup = {n["id"]: n for n in news_list}
    clean_items, used = [], set()
    if not isinstance(data, dict) or not isinstance(data.get("news"), list):
        return None
    required = (
        "classification", "topic", "story", "audience_tension", "why_it_matters",
        "why_now", "pytch_angle", "best_format", "audience_relevance",
        "content_potential", "priority",
    )
    allowed_classifications = {"🔥 TIMELY", "💡 EVERGREEN", "👀 WATCH"}
    allowed_formats = {
        "Timely Reel", "Carousel", "Street Cents", "Expert Reacts",
        "FOMO FIX", "Tool / Calculator / Guide",
    }
    allowed_levels = {"High", "Medium", "Low"}
    for item in data["news"]:
        try:
            source_id = int(item.get("source_id"))
        except (TypeError, ValueError):
            continue
        source = lookup.get(source_id)
        if not source or source_id in used or not all(item.get(key) for key in required):
            continue
        if item["classification"] not in allowed_classifications:
            continue
        if item["best_format"] not in allowed_formats:
            continue
        if not {item["audience_relevance"], item["content_potential"], item["priority"]} <= allowed_levels:
            continue
        used.add(source_id)
        item["link"] = source["link"]
        item["source"] = source["source"]
        item["tier"] = source["tier"]
        clean_items.append(item)
    data["news"] = clean_items[:INTERNAL_SAFETY_CAP]
    return data


# ── External prompt: PYTCH MY→SG Radar spec ──────────────────────────────────

def generate_external_prompt(news_list):
    return f"""You are curating today's PYTCH MY→SG Radar — a Telegram channel for Malaysians working in Singapore. This is a filter, not a feed: choose at most {EXTERNAL_MAX_STORIES} genuinely useful stories, fewer or zero is fine. One strong update beats several loosely related ones.

Today is {TODAY_STR_EN}.

Core audience: Malaysians aged roughly 23-35 who currently work in Singapore, earn SGD, send money or maintain financial commitments in Malaysia, rent or live in Singapore, commute between Malaysia and Singapore, are considering PR/citizenship/returning home, or manage savings, insurance, family or property across both countries.

Priority topics: Your SGD (SGD/MYR, remittance, transfer costs, notable FX movement, BNM/MAS developments that affect SGD/MYR), Your Work (salary, employment, hiring, retrenchment, EP/S Pass/work permit rules, employment rights, side-hustle rules), Your Costs (rent, housing, food, transport, JB-SG commuting, cost-of-living changes), Your Money (income tax, CPF/EPF, savings, banking, insurance, healthcare, debt, relevant investing rules), Your Life Back Home (property, family support, loans, parents, children, healthcare, marriage, retirement, returning to Malaysia, MY financial commitments).

Before choosing a story, ask:
1. Would a Malaysian working in Singapore genuinely care?
2. Does this directly affect their money, job or everyday life?
3. Is there a financial or practical consequence?
4. Is the MY/SG connection genuine?
5. Is this relevant now?
6. Can the impact be explained clearly in one or two sentences?
7. Is the source credible? Weight Tier 1 official sources highest, verify Tier 2 major claims against Tier 1 where possible, and use Tier 3 mainly for evergreen pain-point angles.
8. Is this important enough to justify a Telegram notification?

If the answer to any is not clearly yes, skip it. Do not publish unless there is a direct audience impact — drop: US stock movements, random SG/MY stock news, crypto prices, global M&A, company earnings, Wall Street commentary, generic investment outlooks, CEO appointments, corporate press releases, general business news, weak political stories, generic personal-finance advice, promotional bank or credit-card content. Never include a story just because it contains the words Singapore, Malaysia or money.

Classify each story you keep as exactly one of:
- "🚨 WHAT CHANGED" — a genuine development: policy, tax, work-pass rules, FX, transport, employment, rent, banking, healthcare, CPF/EPF, regulatory or cross-border travel/commuting change
- "💡 WORTH KNOWING" — a highly useful evergreen MY→SG pain point; never present this as breaking news

Aim for roughly 70% timely WHAT CHANGED and 30% WORTH KNOWING over time, but never force either, and never force a fixed count.

For each story:
- "hook" is a short, audience-facing hook with one emoji, understandable in a few seconds.
- "one_liner" explains what changed, or what's worth knowing, in one sentence.
- "why_you_care" is one sentence on the direct effect on Malaysians working in Singapore.
- "action" is either a genuine, specific practical action the story supports, or exactly "No action needed for now." or "Worth watching if this applies to you." if there's nothing concrete to do. Never force advice to negotiate salary, switch accounts, refinance debt, convert currencies, prepare documents, or buy/sell investments unless the story itself gives a clear reason to.
- Write short, conversational, practical, neutral, specific, non-alarmist language — like explaining it to someone checking Telegram on the MRT. Avoid jargon like "broader implications for wage expectations"; prefer plain statements, including "this doesn't directly change X, so we're skipping it" style framing where useful. Do not exaggerate relevance.
- Give both "_en" (natural Malaysian/Singaporean English) and "_zh" (natural Simplified Chinese) versions of every text field.
- Use only candidate IDs below. Never invent facts, URLs, numbers or policy details.

Candidates:
{news_for_prompt(news_list)}

Return ONLY valid JSON:
{{
  "posts": [
    {{
      "source_id": 1,
      "classification": "🚨 WHAT CHANGED",
      "hook_zh": "...", "hook_en": "...",
      "one_liner_zh": "...", "one_liner_en": "...",
      "why_you_care_zh": "...", "why_you_care_en": "...",
      "action_zh": "...", "action_en": "..."
    }}
  ]
}}
If nothing today clears the bar, return {{"posts": []}}."""


def hydrate_external(data, news_list):
    """Validate and hydrate the external MY→SG Radar output. An empty but
    well-formed 'posts' array is valid — it means nothing cleared the bar."""
    lookup = {n["id"]: n for n in news_list}
    clean_items, used = [], set()
    if not isinstance(data, dict) or not isinstance(data.get("posts"), list):
        return None
    required = (
        "classification", "hook_zh", "hook_en", "one_liner_zh", "one_liner_en",
        "why_you_care_zh", "why_you_care_en", "action_zh", "action_en",
    )
    allowed_classifications = {"🚨 WHAT CHANGED", "💡 WORTH KNOWING"}
    for item in data["posts"]:
        try:
            source_id = int(item.get("source_id"))
        except (TypeError, ValueError):
            continue
        source = lookup.get(source_id)
        if not source or source_id in used or not all(item.get(key) for key in required):
            continue
        if item["classification"] not in allowed_classifications:
            continue
        used.add(source_id)
        item["link"] = source["link"]
        item["source"] = source["source"]
        clean_items.append(item)
    data["posts"] = clean_items[:EXTERNAL_MAX_STORIES]
    return data


def generate_weekly_prompt(news_list):
    return f"""You are PYTCH's bilingual content strategist. It is Friday. Based only on these vetted MY-SG candidate headlines, identify three audience themes and next week's content plan for Malaysians working in Singapore.

{news_for_prompt(news_list)}

Return ONLY valid JSON:
{{"themes":[{{"emoji":"💱","title_zh":"主题","title_en":"Theme","desc_zh":"具体影响","desc_en":"Concrete impact"}}],"watch_zh":"下周留意的决定","watch_en":"Decision to watch next week","content_zh":"下周最值得制作的PYTCH内容","content_en":"Best PYTCH content opportunity for next week"}}"""


# ── Internal format: PYTCH Internal Content Radar output spec ────────────────

def format_internal(data):
    news_items = data.get("news", [])
    lines = ["🔒 PYTCH Internal | Content Radar · " + TODAY_STR_EN, "─" * 22, ""]
    if not news_items:
        lines.append("No strong MY→SG content opportunities today.")
        return "\n".join(lines)
    for item in news_items:
        tier_label = f"Tier {item['tier']}" if item.get("tier") else "unlisted source"
        lines.extend([
            f"{item['classification']} | {item['topic']}",
            "",
            "📰 Story",
            item["story"],
            "",
            "🔎 Source",
            f"{item['source']} ({tier_label})",
            "",
            "🧠 Audience tension",
            item["audience_tension"],
            "",
            "📌 Why it matters",
            item["why_it_matters"],
            "",
            "⏰ Why now",
            item["why_now"],
            "",
            "🎬 PYTCH angle",
            item["pytch_angle"],
            "",
            "🎞️ Best format: " + item["best_format"],
            f"📊 Audience relevance: {item['audience_relevance']} | "
            f"Content potential: {item['content_potential']} | "
            f"Priority: {item['priority']}",
            "",
            "🔗 " + item["link"],
            "",
        ])
    return "\n".join(lines).rstrip()


def format_fx_block(fx, zh):
    move_str = f"{'+' if fx['pct_move'] >= 0 else ''}{fx['pct_move']:.1f}%"
    diff = fx["cost_week_ago"] - fx["cost_now"]
    amount = FX_EXAMPLE_MYR_AMOUNT
    if zh:
        header, rate_line, move_line = "💱 新马汇率速览", f"S$1 = RM{fx['rate']:.2f}", f"7天变动：{move_str}"
        if abs(diff) < 1:
            meaning = "过去一周变动不大，现在汇款回家和一周前差别不大。"
        elif diff > 0:
            meaning = f"现在汇RM{amount:,}回家，比一周前大约便宜S${abs(diff):.0f}。"
        else:
            meaning = f"现在汇RM{amount:,}回家，比一周前大约贵S${abs(diff):.0f}。"
        meaning_label = "📌 这代表什么"
    else:
        header, rate_line, move_line = "💱 SGD/MYR Radar", f"S$1 = RM{fx['rate']:.2f}", f"7-day move: {move_str}"
        if abs(diff) < 1:
            meaning = "Not much movement this week — sending money home costs about the same as a week ago."
        elif diff > 0:
            meaning = f"Sending RM{amount:,} home today would cost around S${abs(diff):.0f} less than a week ago."
        else:
            meaning = f"Sending RM{amount:,} home today would cost around S${abs(diff):.0f} more than a week ago."
        meaning_label = "📌 What it means"
    return "\n".join([header, rate_line, move_line, "", meaning_label, meaning])


def format_external(data, fx, language):
    zh = language == "zh"
    suffix = "zh" if zh else "en"
    header = ("🇨🇳 中文版 | PYTCH MY→SG Radar · " + TODAY_STR) if zh else ("🇬🇧 English | PYTCH MY→SG Radar · " + TODAY_STR_EN)
    show_fx = should_show_fx(fx)
    posts = data.get("posts", []) if data else []

    if not posts and not show_fx:
        return None  # Nothing meaningful today — no filler, no message sent.

    lines = [header, "─" * 22, ""]
    if show_fx:
        lines.append(format_fx_block(fx, zh))
        lines.append("")
    for item in posts:
        lines.extend([
            item["classification"] + " " + item[f"hook_{suffix}"],
            "",
            item[f"one_liner_{suffix}"],
            "",
            ("💭 你为什么要关心" if zh else "💭 Why you care:"),
            item[f"why_you_care_{suffix}"],
            "",
            item[f"action_{suffix}"],
            "",
            ("🔗 来源：" if zh else "🔗 Source: ") + item["source"],
            "",
        ])
    fallback = "💡 仅供参考，不构成财务、税务或移民建议。" if zh else "💡 For information only; not financial, tax or immigration advice."
    lines.append(fallback)
    return "\n".join(lines).rstrip()


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

    if BOT_MODE == "internal":
        # Internal channel: English only, classified TIMELY/EVERGREEN/WATCH,
        # includes content angles for the PYTCH team. No candidates or nothing
        # clearing the editorial bar both mean "no opportunities today" — that's
        # a valid, expected daily state, not a failure.
        if not raw_news:
            logger.info("No candidates cleared the relevance threshold")
            ok = send_telegram(format_internal({"news": []}))
        else:
            daily = hydrate_internal(
                extract_json(call_openai(generate_internal_prompt(raw_news))),
                raw_news,
            )
            if daily is None:
                logger.error("Internal daily brief generation or validation failed")
                return False
            ok = send_telegram(format_internal(daily))

        # Friday weekly content radar (internal only). The workflow now runs
        # twice daily (~12am and ~9am SGT) — gate on hour so this only fires
        # once, on the morning run, instead of twice every Friday.
        if WEEKDAY == 4 and NOW.hour >= WEEKLY_RECAP_MIN_HOUR and raw_news:
            weekly = extract_json(call_openai(generate_weekly_prompt(raw_news), max_tokens=1800))
            if weekly:
                time.sleep(2)
                ok = send_telegram(format_weekly(weekly)) and ok
            else:
                logger.error("Weekly content radar generation failed")
                ok = False

    else:
        # External channel: bilingual (Chinese then English), FX Radar + up to
        # EXTERNAL_MAX_STORIES posts. A quiet day sends nothing — "no filler."
        fx = fetch_fx_move()
        daily = {"posts": []}
        if raw_news:
            hydrated = hydrate_external(
                extract_json(call_openai(generate_external_prompt(raw_news))),
                raw_news,
            )
            if hydrated is None:
                logger.warning("External story selection failed; continuing with FX-only update")
            else:
                daily = hydrated

        zh_message = format_external(daily, fx, "zh")
        en_message = format_external(daily, fx, "en")
        if zh_message is None and en_message is None:
            logger.info("Nothing meaningful today — no external update sent")
            ok = True
        else:
            ok = send_telegram(zh_message) if zh_message else True
            time.sleep(2)
            ok = (send_telegram(en_message) if en_message else True) and ok

    logger.info("Task completed (mode: %s)", BOT_MODE)
    return ok


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
