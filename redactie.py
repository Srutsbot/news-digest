#!/usr/bin/env python3
"""Vroege Dienst: builds one self-contained web page from the digest and investigative sources.

Usage:
    python redactie.py                       # fetch everything and write docs/index.html
    python redactie.py --out pad/naar.html   # write the page somewhere else
    python redactie.py --save-json data.json # also keep the fetched data
    python redactie.py --from-json data.json # rebuild the page without fetching

Reuses the feeds and helpers of digest.py and onderzoek.py. Sends no email and does not touch
onderzoek_seen.json, so it can run next to the two mails without influencing them.
"""
import argparse
import html
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import digest
import onderzoek

HERE = Path(__file__).parent
# MyMemory: free and without a key, with a daily limit per IP address. Headlines that do not get
# translated keep their original text and a button to translate them by hand.
TRANSLATE_URL = "https://api.mymemory.translated.net/get"
LATIN_LANGUAGES = {"DE", "FR", "IT", "ES", "PT"}
TEMPLATE = HERE / "redactie_template.html"
DATA_MARKER = "/*__DATA__*/null"

MAX_RESEARCH_AGE_DAYS = 14    # older investigative pieces stay out of the page
MAX_RESEARCH_PER_PLATFORM = 8
MIN_SHARED_WORDS = 2          # same rule as digest.py: two shared headline words = same story

REGIONS = {
    "Follow the Money": "Nederland", "Investico": "Nederland", "Lighthouse Reports": "Nederland",
    "Bellingcat": "Nederland", "Pointer (KRO-NCRV)": "Nederland",
    "ICIJ": "Wereldwijd", "OCCRP": "Wereldwijd", "Forbidden Stories": "Wereldwijd",
    "The Bureau of Investigative Journalism": "Wereldwijd",
    "Correctiv": "Europa", "Mediapart": "Europa", "IrpiMedia": "Europa",
    "BIRN / Balkan Insight": "Europa", "The Insider": "Europa", "Investigate Europe": "Europa",
    "ProPublica": "Noord-Amerika", "The Intercept": "Noord-Amerika",
    "El Faro": "Midden-Amerika",
    "Agência Pública": "Zuid-Amerika", "OjoPúblico": "Zuid-Amerika", "CIPER Chile": "Zuid-Amerika",
    "ARIJ": "Midden-Oosten", "Mada Masr": "Midden-Oosten", "Daraj": "Midden-Oosten", "Haaretz": "Midden-Oosten",
    "amaBhungane": "Afrika", "Premium Times": "Afrika", "Inkyfada": "Afrika",
    "Rappler": "Azië", "Newstapa": "Azië", "The Reporters' Collective": "Azië",
}


# These feeds carry mostly daily news next to their investigations. The page hides them unless
# the reader switches them on. Replace an entry's feed in onderzoek.py if a purely investigative one appears.
GENERAL_FEEDS = {"BIRN / Balkan Insight", "The Insider", "Mada Masr", "Haaretz", "Premium Times", "Rappler"}


def iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment else None


def tidy_title(article: dict) -> None:
    """Google News adds ' - Outlet' to headlines; that word would also disturb story matching."""
    if "news.google.com" in article["link"]:
        article["title"] = re.sub(r"\s+-\s+[^-]{1,40}$", "", article["title"])


def fetch_outlet(item: tuple[str, tuple[str, str]]) -> tuple[str, list[dict]]:
    name, (url, _holder) = item
    articles = digest.fetch_articles(name, url)
    if not articles and name in digest.FEED_FALLBACKS:
        fb = digest.FEED_FALLBACKS[name]
        articles = digest.fetch_with_discovery(name, fb["homepage"], fb["hints"], fb.get("google_query"),
                                               fb.get("google_lang", "en"), discover=fb.get("discover", True))
    return name, articles


def fetch_platform(platform: tuple) -> tuple[str, list[dict]]:
    return platform[0], onderzoek.fetch_platform(*platform)[0]


def source_language(title: str, label: str) -> str | None:
    """Language to translate from. None for Dutch and English, and for mixed labels such as ES/EN
    where a headline in Latin script could be either language."""
    if re.search(r"[\u0600-\u06ff]", title):
        return "ar"
    if re.search(r"[\uac00-\ud7af]", title):
        return "ko"
    if re.search(r"[\u0400-\u04ff]", title):
        return "ru"
    codes = label.split("/")
    latin = [code for code in codes if code in LATIN_LANGUAGES]
    return latin[0].lower() if latin and "EN" not in codes else None


def translate_title(title: str, source: str) -> str | None:
    try:
        resp = requests.get(TRANSLATE_URL, timeout=15, params={"q": title, "langpair": f"{source}|nl"})
        resp.raise_for_status()
        data = resp.json()
        if data.get("quotaFinished") or str(data.get("responseStatus")) != "200":
            return None
        text = html.unescape(data["responseData"]["translatedText"]).strip()
        return None if not text or text.upper().startswith("MYMEMORY WARNING") else text
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return None


def add_translations(items: list[dict]) -> None:
    """Hidden general-news feeds come last, so a daily limit hits the investigative headlines last."""
    todo = [(item, source) for item in sorted(items, key=lambda i: i["general"])
            if (source := source_language(item["title"], item["language"]))]
    with ThreadPoolExecutor(3) as pool:
        results = pool.map(lambda pair: translate_title(pair[0]["title"], pair[1]), todo)
        for (item, _source), translated in zip(todo, results):
            if translated and translated != item["title"]:
                item["titleNl"] = translated
    done = sum("titleNl" in item for item, _source in todo)
    print(f"[info] translated {done} of {len(todo)} foreign-language headlines", file=sys.stderr)


def news_item(article: dict, trending: dict[str, int]) -> dict:
    return {
        "title": article["title"], "link": article["link"], "snippet": article.get("snippet", ""),
        "published": iso(article.get("published")),
        "terms": sorted(article.get("tok", set()) & trending.keys(), key=lambda t: (-trending[t], t)),
    }


def build_news() -> dict:
    with ThreadPoolExecutor(8) as pool:
        fetched = dict(pool.map(fetch_outlet, digest.FEEDS.items()))

    candidates: dict[str, list[dict]] = {}
    opinion_pool: list[dict] = []
    for name, articles in fetched.items():
        candidates[name] = []
        for article in articles:
            tidy_title(article)
            article["source"] = name
            if digest.is_excluded(article):
                continue
            (opinion_pool if digest.is_opinion(article) else candidates[name]).append(article)
    for name, cfg in digest.OPINION_FEEDS.items():
        found = digest.fetch_with_discovery(name, cfg["homepage"], cfg["hints"], cfg.get("google_query"),
                                            cfg.get("google_lang", "en"))
        for article in found[:digest.OPINION_CANDIDATES]:
            tidy_title(article)
            article["source"] = name
            if not digest.is_excluded(article):
                opinion_pool.append(article)

    trending = digest.trending_terms(candidates)
    stories = digest.find_onmisbaar(candidates)
    in_box = {id(a) for story in stories for a in story["members"]}
    return {
        "wekdienst": digest.fetch_wekdienst(),
        "onmisbaar": [{
            "title": story["title"],
            "members": [{"source": a["source"], "title": a["title"], "link": a["link"]} for a in story["members"]],
        } for story in stories],
        "trending": [{"term": t, "outlets": n} for t, n in sorted(trending.items(), key=lambda kv: (-kv[1], kv[0]))],
        "outlets": [{
            "name": name,
            "ok": bool(fetched[name]),
            "articles": [news_item(a, trending) for a in articles if id(a) not in in_box],
        } for name, articles in candidates.items()],
        "opinion": [{"source": a["source"], "title": a["title"], "link": a["link"]}
                    for a in digest.select_opinion(opinion_pool, trending)],
        "_candidates": candidates,
    }


def build_research(candidates: dict[str, list[dict]], now: datetime) -> dict:
    with ThreadPoolExecutor(8) as pool:
        fetched = dict(pool.map(fetch_platform, onderzoek.PLATFORMS))

    cutoff = now - timedelta(days=MAX_RESEARCH_AGE_DAYS)
    news = [a for articles in candidates.values() for a in articles]
    by_platform: dict[str, list[dict]] = {}
    platforms = []
    for name, _homepage, language, _hint in onderzoek.PLATFORMS:
        articles = fetched[name]
        recent = [a for a in articles if not a["published"] or a["published"] >= cutoff]
        recent.sort(key=lambda a: a["published"] or onderzoek.EPOCH, reverse=True)
        unique, titles = [], set()  # Google News can list one story under several links
        for article in recent:
            tidy_title(article)
            if article["title"] not in titles:
                titles.add(article["title"])
                article["tok"] = digest.tokens(article["title"])
                unique.append(article)
        recent = unique[:MAX_RESEARCH_PER_PLATFORM]
        if recent:
            by_platform[name] = recent
        platforms.append({"name": name, "region": REGIONS.get(name, "Overig"), "language": language,
                          "ok": bool(articles), "count": len(recent), "general": name in GENERAL_FEEDS})

    # Two shared words give false matches across these varied platforms ("South Africa"), so ask for
    # three. The news section is already built at this point and keeps its own rule of two.
    digest.MIN_SHARED_WORDS = 3
    clusters = digest.find_onmisbaar(by_platform, min_sources=2, max_clusters=5)
    cluster_of = {id(a): i for i, story in enumerate(clusters) for a in story["members"]}
    items = []
    for name, articles in by_platform.items():
        for article in articles:
            echoes = sorted({n["source"] for n in news if len(article["tok"] & n["tok"]) >= MIN_SHARED_WORDS})
            snippet = article["snippet"]
            if snippet.startswith("The post "):  # WordPress boilerplate, not a summary
                snippet = ""
            items.append({
                "title": article["title"], "link": article["link"], "snippet": snippet,
                "source": name, "language": article["language"], "region": REGIONS.get(name, "Overig"),
                "published": iso(article["published"]), "story": cluster_of.get(id(article)), "echoes": echoes,
                "general": name in GENERAL_FEEDS,
            })
    items.sort(key=lambda a: a["published"] or "", reverse=True)
    add_translations(items)
    return {"platforms": platforms, "items": items, "maxAgeDays": MAX_RESEARCH_AGE_DAYS,
            "stories": [story["title"] for story in clusters]}


def collect() -> dict:
    now = datetime.now(timezone.utc)
    news = build_news()
    candidates = news.pop("_candidates")
    return {"generated": now.isoformat(), "news": news, "research": build_research(candidates, now)}


def strip_snippets(data: dict) -> None:
    """For a publicly reachable page: keep headlines and links, drop the article text from the feeds."""
    articles = [a for outlet in data["news"]["outlets"] for a in outlet["articles"]] + data["research"]["items"]
    if data["news"]["wekdienst"]:
        articles.append(data["news"]["wekdienst"])
    for article in articles:
        article["snippet"] = ""


def render_fragment(data: dict) -> str:
    """The page without its document wrapper. '<' is escaped so feed text cannot close the script tag."""
    payload = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")
    return TEMPLATE.read_text(encoding="utf-8").replace(DATA_MARKER, payload)


def render_page(data: dict) -> str:
    return ('<!doctype html><html lang="nl"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<meta name="robots" content="noindex">'
            f"</head><body>{render_fragment(data)}</body></html>")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=HERE / "docs" / "index.html")
    parser.add_argument("--save-json", type=Path)
    parser.add_argument("--from-json", type=Path)
    parser.add_argument("--no-snippets", action="store_true", help="show only headlines and links")
    args = parser.parse_args()

    data = json.loads(args.from_json.read_text(encoding="utf-8")) if args.from_json else collect()
    if args.save_json:
        args.save_json.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if args.no_snippets:
        strip_snippets(data)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(render_page(data), encoding="utf-8")
    print(f"Written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
