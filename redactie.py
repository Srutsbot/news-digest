#!/usr/bin/env python3
"""Sjoernalistieke Bot: builds one self-contained web page from the digest and investigative sources.

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

import agenda
import digest
import onderzoek

HERE = Path(__file__).parent
# MyMemory: free and without a key, with a daily limit per IP address. Headlines that do not get
# translated keep their original text and a button to translate them by hand.
TRANSLATE_URL = "https://api.mymemory.translated.net/get"
LATIN_LANGUAGES = {"DE", "FR", "IT", "ES", "PT"}
TEMPLATE = HERE / "redactie_template.html"
DATA_MARKER = "/*__DATA__*/null"
# The browser tab logo: a white S on bordeaux. The image is a fallback for browsers without SVG icons.
LOGO_SVG = '%3Csvg%20xmlns%3D%22http%3A%2F%2Fwww.w3.org%2F2000%2Fsvg%22%20viewBox%3D%220%200%2064%2064%22%3E%3Crect%20width%3D%2264%22%20height%3D%2264%22%20rx%3D%2213%22%20fill%3D%22%238f1d21%22%2F%3E%3Ctext%20x%3D%2232%22%20y%3D%2248%22%20text-anchor%3D%22middle%22%20font-family%3D%22Times%20New%20Roman%2C%20Times%2C%20serif%22%20font-size%3D%2250%22%20font-weight%3D%22700%22%20fill%3D%22%23fcf9f2%22%3ES%3C%2Ftext%3E%3C%2Fsvg%3E'
LOGO_PNG = "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"

MAX_RESEARCH_AGE_DAYS = 14    # older investigative pieces stay out of the page
MAX_RESEARCH_PER_PLATFORM = 8
MAX_MUST_READ_STORIES = 9     # the page shows three rows of three; the mail keeps its own limit
FIRST_INTERNATIONAL_OUTLET = "The Guardian (Europe)"  # digest.FEEDS lists the Dutch outlets before this one
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
    stories = digest.find_onmisbaar(candidates, max_clusters=MAX_MUST_READ_STORIES)
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
    research = build_research(candidates, now)
    mark_not_in_dutch(news, candidates)
    return {"generated": now.isoformat(), "news": news, "research": research,
            "science": build_science(candidates, now), "agenda": agenda.build_agenda(now)}


# "Possibly not in Dutch yet": translate an international headline, look for it among the Dutch headlines
# on the page, then search Google News Netherlands. An unsure case gets no label.
GAP_SKIP_TITLE = re.compile(r"^(?:opinion\b|exclusive \| opinion)|\breview\b|\blive\b|^how we\b|^see how\b|\| [A-Z][\w'’. -]+$", re.I)
GAP_SKIP_WORDS = set("""opinie exclusief zegt zeggen zeggen volgens kunnen zullen zouden hebben heeft twee drie vijf zeven
hoe waarom wat wie een leeftijd nee binnen functionarissen duizenden""".split())
GAP_LONGEST_WORD = 16        # longer words are usually machine-made compounds that nobody searches for


def gap_query_words(dutch_title: str) -> list[str]:
    words = re.findall(r"[A-Za-zÀ-ÿ0-9'’-]+", dutch_title)
    names = [w.strip("'’-").lower() for i, w in enumerate(words) if i and w[0].isupper() and len(w) >= 3]
    other = sorted(digest.tokens(dutch_title), key=len, reverse=True)
    chosen = [w for w in dict.fromkeys(names + other) if w not in GAP_SKIP_WORDS and "-" not in w and len(w) <= GAP_LONGEST_WORD]
    return chosen[:3]


def seems_not_in_dutch(title: str, dutch_headlines: list[set[str]]) -> bool:
    if GAP_SKIP_TITLE.search(title):
        return False
    translated = translate_title(title, "en")
    if not translated or any(len(digest.tokens(translated) & known) >= MIN_SHARED_WORDS for known in dutch_headlines):
        return False
    words = gap_query_words(translated)
    if len(words) < 2:
        return False
    for query in (words, words[:2]):
        if digest.fetch_articles("Nederlandse dekking", digest.google_news_feed(" ".join(query) + " when:3d", "nl", "NL")):
            return False
    return True


def mark_not_in_dutch(news: dict, candidates: dict[str, list[dict]]) -> None:
    """Runs after the translations of the research section, which matter more if the daily limit is reached."""
    names = list(candidates)
    first_foreign = names.index(FIRST_INTERNATIONAL_OUTLET)
    dutch_outlets = set(names[:first_foreign])
    dutch_headlines = [a["tok"] for name in names[:first_foreign] for a in candidates[name]]
    foreign_items = [a for outlet in news["outlets"] if outlet["name"] not in dutch_outlets for a in outlet["articles"]]
    foreign_stories = [s for s in news["onmisbaar"] if not any(m["source"] in dutch_outlets for m in s["members"])]
    titles = list(dict.fromkeys([a["title"] for a in foreign_items] + [s["title"] for s in foreign_stories]))
    with ThreadPoolExecutor(3) as pool:
        answers = dict(zip(titles, pool.map(lambda title: seems_not_in_dutch(title, dutch_headlines), titles)))
    for entry in foreign_items + foreign_stories:
        entry["notInDutch"] = answers[entry["title"]]
    marked = sum(answers.values())
    print(f"[info] {marked} of {len(titles)} international headlines possibly not in Dutch yet", file=sys.stderr)


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
    return ('<!doctype html><html lang="nl" data-theme="light"><head><meta charset="utf-8">'
            '<meta name="color-scheme" content="light">'
            '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            '<meta name="robots" content="noindex">'
            f'<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,{LOGO_SVG}">'
            f'<link rel="alternate icon" type="image/png" href="data:image/png;base64,{LOGO_PNG}">'
            f'<link rel="apple-touch-icon" href="data:image/png;base64,{LOGO_PNG}">'
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
