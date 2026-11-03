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
from collections import Counter
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


# Science section: no newspapers. Three sites have no working feed of their own and go through Google News.
SCIENCE_TOPICS = {  # topic -> [(name, feed, language)]
    "Nederlandstalig": [
        ("Scientias", "https://scientias.nl/feed/", "NL"),
        ("New Scientist NL", "https://www.newscientist.nl/feed/", "NL"),
        ("NEMO Kennislink", digest.google_news_feed("site:nemokennislink.nl", "nl", "NL"), "NL"),
        ("Quest", "https://www.quest.nl/rss", "NL"),
        ("Psychologie Magazine", "https://www.psychologiemagazine.nl/rss", "NL"),
        ("Historiek", "https://historiek.net/feed/", "NL"),
    ],
    "Tijdschriften": [
        ("Nature", "https://www.nature.com/nature.rss", "EN"),
        ("Science", "https://www.science.org/rss/news_current.xml", "EN"),
    ],
    "Nieuwe onderzoeken": [
        ("EurekAlert", digest.google_news_feed("site:eurekalert.org"), "EN"),
        ("ScienceDaily", "https://www.sciencedaily.com/rss/top/science.xml", "EN"),
        ("Phys.org", "https://phys.org/rss-feed/", "EN"),
        ("Medical Xpress", "https://medicalxpress.com/rss-feed/", "EN"),
        ("Tech Xplore", "https://techxplore.com/rss-feed/", "EN"),
        ("Futurity", "https://www.futurity.org/feed/", "EN"),
    ],
    "Magazines": [
        ("New Scientist", digest.google_news_feed("site:newscientist.com"), "EN"),
        ("Scientific American", "http://rss.sciam.com/ScientificAmerican-Global", "EN"),
        ("Science News", "https://www.sciencenews.org/feed", "EN"),
        ("Quanta Magazine", "https://www.quantamagazine.org/feed/", "EN"),
        ("Live Science", "https://www.livescience.com/feeds.xml", "EN"),
        ("Ars Technica Science", "https://feeds.arstechnica.com/arstechnica/science", "EN"),
        ("Nautilus", "https://nautil.us/feed/", "EN"),
        ("Undark", "https://undark.org/feed/", "EN"),
        ("Knowable Magazine", "https://knowablemagazine.org/rss", "EN"),
        ("Smithsonian", "https://www.smithsonianmag.com/rss/science-nature/", "EN"),
        ("The Conversation", "https://theconversation.com/global/articles.atom", "EN"),
    ],
    "Klimaat en natuur": [
        ("Yale Environment 360", "https://e360.yale.edu/feed.xml", "EN"),
        ("Mongabay", "https://news.mongabay.com/feed/", "EN"),
        ("Eos (AGU)", "https://eos.org/feed", "EN"),
    ],
    "Gezondheid en psychologie": [
        ("STAT News", "https://www.statnews.com/feed/", "EN"),
        ("Neuroscience News", "https://neurosciencenews.com/feed/", "EN"),
        ("PsyPost", "https://www.psypost.org/feed/", "EN"),
    ],
    "Techniek en ruimte": [
        ("MIT Technology Review", "https://www.technologyreview.com/feed/", "EN"),
        ("ESA", "https://www.esa.int/rssfeed/Our_Activities/Space_Science", "EN"),
    ],
    "Maatschappij": [
        ("Aeon", "https://aeon.co/feed.rss", "EN"),
    ],
}
MAX_SCIENCE_ITEMS = 25
MAX_SCIENCE_PER_SOURCE = 2
MAX_SCIENCE_PER_TOPIC = 7
MAX_SCIENCE_AGE_DAYS = 2
MIN_SCIENCE_SCORE = 2
# What usually reaches a BNR listener: it touches work, money, health, technology or daily behaviour.
SCIENCE_REASONS = {name: re.compile(pattern, re.I) for name, pattern in {
    "Werk en geld": r"\b(?:work(?:ers?|place|ing)?|jobs?|employ\w*|salar\w*|wages?|money|econom\w*|compan(?:y|ies)|business\w*|costs?|prices?|tax\w*|werk\w*|banen|baan|salaris\w*|geld|econom\w*|bedrijf\w*|kosten|prijs\w*|belasting\w*|collega\w*)\b",
    "Gezondheid": r"\b(?:health\w*|cancer\w*|dementia|alzheimer\w*|sleep\w*|diet\w*|obes\w*|vaccin\w*|heart|stroke|diabetes|flu|virus\w*|aging|ageing|drugs?|hospitals?|patients?|smok\w*|alcohol|gezond\w*|kanker|slaap\w*|voeding|dieet|vaccin\w*|griep|ziekte\w*|ziekenhui\w*|patiënt\w*|medicijn\w*|roken|veroudering)\b",
    "Technologie": r"\b(?:(?-i:AI)|artificial intelligence|chatbots?|robots?|algorithms?|batter(?:y|ies)|nuclear|self-driving|kunstmatige intelligentie|robot\w*|algoritme\w*|batterij\w*|kernenergie|technologie\w*)\b",
    "Gedrag": r"\b(?:brains?|relationships?|parents?|children|teens?|voters?|social media|loneliness|memory|stress|anxiety|depress\w*|hersenen|relatie\w*|ouders|kinderen|jongeren|kiezers?|sociale media|eenzaam\w*|geheugen|angst\w*|depressie\w*)\b",
    "Klimaat en energie": r"\b(?:climate|heat ?waves?|energy|microplastics?|pollution|klimaat\w*|hittegolf\w*|energie\w*|plastic|fijnstof|luchtvervuiling)\b",
}.items()}
DUTCH_HOOK = re.compile(r"\b(?:nederland\w*|dutch|netherlands|amsterdam|rotterdam|utrecht|leiden|delft|wageningen|groningen|maastricht|eindhoven|tilburg|nijmegen)\b", re.I)
QUESTION = re.compile(r"\?\s*$|^(?:why|how|can|does|do|is|are|what|waarom|hoe|kan|kunnen|is|zijn|wat)\b", re.I)
# Hard to explain on air, or far away from the listener. Lowers the score but can be outweighed.
SCIENCE_JARGON = re.compile(r"\b(?:proteins?|genom\w*|receptors?|peptides?|enzym\w*|isotop\w*|in vitro|nanoparticl\w*|phylogen\w*|meta-analysis|eiwit\w*|electrolytes?|polymers?|sodium|lithium|atoms?)\b", re.I)
SCIENCE_FAR_AWAY = re.compile(r"\b(?:mumm(?:y|ies)|inca|dinosaur\w*|fossils?|sharks?|corals?|termites?|moths?|dinosauri\w*|fossiel\w*|haai\w*|koraal|termiet\w*)\b", re.I)
# Journal housekeeping, not stories ("Retraction Watch" itself is fine: only titles starting with these words).
SCIENCE_NOISE = re.compile(r"^(?:\[[^\]]+\]\s*)?(?:(?:author|publisher)\s+correction|correction\b|retraction note|erratum|department of error|opinion:)", re.I)


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


# Repeats: compared with everything the feeds still offer, about the last 5 days. Words too common in
# science headlines say nothing about a story, so they never count.
SCIENCE_COMMON_WORDS = set("""
study studies scientists science scientific researchers research finds find found reveals reveal new
brain brains health world earth global life help helps linked risk disease human humans people test
countries surprising data hidden single trial protein time without biggest long process online made
make fast mental other worlds story matter affect heart survival home experts protect ancient self
dark early could really think know want better based shows show using used many much even
during
onderzoek onderzoekers wetenschappers wetenschap studie mens mensen achter nieuwe gezondheid hersenen
""".split())
TERM_IGNORED_WORDS = {"down", "america", "american", "blood", "plant", "plants", "lower"}  # say nothing as a term
# Fine as a recurring term, but sharing them does not make two headlines the same story.
STORY_IGNORED_WORDS = {"hubble", "webb", "nasa", "discover", "discovers", "quantum", "physics"}
REPEAT_RARE_WORD = 2         # a word in at most this many headlines is rare enough to identify a story
REPEAT_TERM_SOURCES = 3      # a recurring term appears at this many sources...
REPEAT_TERM_DAYS = 2         # ...on this many different days
MAX_TERMS_PER_ITEM = 2
MAX_MENTIONS = 5             # other headlines listed under a marked story or term
REPEAT_STORY_BONUS = 3       # a story several sources pick up is a sign that it travels


def story_words(article: dict) -> set[str]:
    return article["tok"] - SCIENCE_COMMON_WORDS - STORY_IGNORED_WORDS


def term_words(article: dict) -> set[str]:
    return article["tok"] - SCIENCE_COMMON_WORDS - TERM_IGNORED_WORDS


def same_science_story(a: dict, b: dict, word_counts: Counter) -> bool:
    """Three shared words, or two at different sources that hardly appear elsewhere ("bull" and "donkey").
    The same headline does not count, nor does another piece from the same source on the same day."""
    if a["title"] == b["title"] or (a["source"] == b["source"] and a["published"].date() == b["published"].date()):
        return False
    shared = story_words(a) & story_words(b)
    return len(shared) >= 3 or (len(shared) == 2 and a["source"] != b["source"]
                                and all(word_counts[w] <= REPEAT_RARE_WORD for w in shared))


def mentions(articles) -> list[dict]:
    """Where else something appeared, newest first, so the reader can open those pieces."""
    newest = sorted(articles, key=lambda a: a["published"], reverse=True)[:MAX_MENTIONS]
    return [{"source": a["source"], "title": a["title"], "link": a["link"], "published": iso(a["published"])} for a in newest]


def add_repeats(item: dict, article: dict, everything: list[dict], word_counts: Counter, terms: dict[str, dict]) -> None:
    others = {b["title"]: b for b in everything if same_science_story(article, b, word_counts)}
    item["repeat"] = None
    if others:
        group = [article, *others.values()]
        item["repeat"] = {"count": len(group), "days": len({a["published"].date() for a in group}),
                          "mentions": mentions(others.values())}
    found = [word for word in term_words(article) if word in terms]
    found.sort(key=lambda word: (-terms[word]["days"], -terms[word]["sources"], word))
    item["terms"] = []
    for word in found[:MAX_TERMS_PER_ITEM]:
        elsewhere = {b["title"]: b for b in everything if b["title"] != article["title"] and word in term_words(b)}
        item["terms"].append({"term": word, **terms[word], "count": len(elsewhere), "mentions": mentions(elsewhere.values())})


def recurring_terms(everything: list[dict]) -> dict[str, dict]:
    sources: dict[str, set] = {}
    days: dict[str, set] = {}
    for article in everything:
        for word in term_words(article):
            sources.setdefault(word, set()).add(article["source"])
            days.setdefault(word, set()).add(article["published"].date())
    return {word: {"sources": len(sources[word]), "days": len(days[word])} for word in sources
            if len(sources[word]) >= REPEAT_TERM_SOURCES and len(days[word]) >= REPEAT_TERM_DAYS}


def score_science(article: dict, language: str) -> tuple[int, list[str]]:
    """How well a headline fits the BNR listener: a word match, no judgement of the article itself."""
    title = article["title"]  # summaries match too loosely: "work" there rarely means the job market
    score, reasons = 0, []
    for reason, pattern in SCIENCE_REASONS.items():
        if pattern.search(title):
            score += 2
            reasons.append(reason)
    if DUTCH_HOOK.search(f"{title} {article.get('snippet', '')}"):
        score += 2
        reasons.insert(0, "Nederlandse haak")
    score += bool(QUESTION.search(title)) + (language == "NL")
    score -= 3 * bool(SCIENCE_JARGON.search(title)) + 2 * bool(SCIENCE_FAR_AWAY.search(title))
    return score, reasons


def select_science(articles: list[dict], word_counts: Counter) -> list[dict]:
    """Best fit first, with limits per source and topic and one headline per story."""
    chosen: list[dict] = []
    per_source: dict[str, int] = {}
    per_topic: dict[str, int] = {}
    for article in sorted(articles, key=lambda a: (-a["score"], a["published"] and -a["published"].timestamp())):
        if len(chosen) == MAX_SCIENCE_ITEMS or article["score"] < MIN_SCIENCE_SCORE:
            break
        if per_source.get(article["source"], 0) >= MAX_SCIENCE_PER_SOURCE or per_topic.get(article["topic"], 0) >= MAX_SCIENCE_PER_TOPIC:
            continue
        if any(same_science_story(article, other, word_counts) for other in chosen):
            continue
        chosen.append(article)
        per_source[article["source"]] = per_source.get(article["source"], 0) + 1
        per_topic[article["topic"]] = per_topic.get(article["topic"], 0) + 1
    return chosen


def build_science(candidates: dict[str, list[dict]], now: datetime) -> dict:
    feeds =[(topic, *feed) for topic, members in SCIENCE_TOPICS.items() for feed in members]
    with ThreadPoolExecutor(8) as pool:
        fetched = list(pool.map(lambda feed: digest.fetch_articles(feed[1], feed[2]), feeds))

    news = [a for articles in candidates.values() for a in articles]
    cutoff = now - timedelta(days=MAX_SCIENCE_AGE_DAYS)
    everything = []
    for (topic, name, _url, language), articles in zip(feeds, fetched):
        for article in articles:
            tidy_title(article)
            if digest.is_excluded(article) or SCIENCE_NOISE.match(article["title"]) or not article["published"]:
                continue
            article.update(source=name, topic=topic, language=language, tok=digest.tokens(article["title"]))
            article["score"], article["reasons"] = score_science(article, language)
            everything.append(article)
    pool_of_articles = [a for a in everything if a["published"] >= cutoff]
    word_counts = Counter(word for a in everything for word in story_words(a))
    terms = recurring_terms(everything)
    for article in pool_of_articles:
        if any(same_science_story(article, other, word_counts) for other in everything):
            article["score"] += REPEAT_STORY_BONUS

    items = []
    for a in select_science(pool_of_articles, word_counts):
        item = {
            "title": a["title"], "link": a["link"], "snippet": a["snippet"], "source": a["source"],
            "language": a["language"], "published": iso(a["published"]), "reasons": a["reasons"],
            "echoes": sorted({n["source"] for n in news if len(a["tok"] & n["tok"]) >= MIN_SHARED_WORDS}),
        }
        add_repeats(item, a, everything, word_counts, terms)
        items.append(item)
    items.sort(key=lambda a: a["published"], reverse=True)  # chosen by fit, shown newest first
    return {"items": items, "scanned": len(pool_of_articles), "maxAgeDays": MAX_SCIENCE_AGE_DAYS,
            "sourcesTotal": len(feeds), "sourcesUsed": len({a["source"] for a in pool_of_articles})}


def collect() -> dict:
    now = datetime.now(timezone.utc)
    news = build_news()
    candidates = news.pop("_candidates")
    return {"generated": now.isoformat(), "news": news, "research": build_research(candidates, now),
            "science": build_science(candidates, now)}


def strip_snippets(data: dict) -> None:
    """For a publicly reachable page: keep headlines and links, drop the article text from the feeds."""
    articles = ([a for outlet in data["news"]["outlets"] for a in outlet["articles"]]
                + data["research"]["items"] + data.get("science", {}).get("items", []))
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
