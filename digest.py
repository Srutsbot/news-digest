#!/usr/bin/env python3
"""Daily news digest: pulls the top headlines from RSS feeds and emails them.

Usage:
    python digest.py             # fetch feeds and send the email
    python digest.py --dry-run   # fetch feeds and print the email, don't send

Required environment variables (only when sending):
    SMTP_USER      e.g. your Gmail address
    SMTP_PASSWORD  a Gmail *app password* (not your normal password)
    EMAIL_TO       recipient address (can be the same as SMTP_USER)
Optional:
    SMTP_HOST      default: smtp.gmail.com
    SMTP_PORT      default: 465 (SSL)
"""
import html
import os
import re
import smtplib
import sys
import unicodedata
from datetime import date
from email.message import EmailMessage
from urllib.parse import urljoin, urlparse

import feedparser
import requests

# name -> (feed URL, copyright holder used in the email footer)
FEEDS = {
    # --- Dutch ---
    "de Volkskrant": ("https://www.volkskrant.nl/voorpagina/rss.xml", "de Volkskrant"),
    "NRC": ("https://www.nrc.nl/rss/", "NRC"),
    "FD": ("https://fd.nl/?rss", "Het Financieele Dagblad"),
    "De Telegraaf": ("https://www.telegraaf.nl/rss", "De Telegraaf"),
    "AD": ("https://www.ad.nl/home/rss.xml", "AD"),
    "Het Parool": ("https://www.parool.nl/voorpagina/rss.xml", "Het Parool"),
    "Vrij Nederland": ("https://www.vn.nl/feed/", "Vrij Nederland"),
    "BNR": ("https://www.bnr.nl/", "FD Mediagroep"),  # no working RSS: headlines are scraped from the homepage
    # "Follow the Money": no public RSS feed found on ftm.nl -- add a URL here if you find one
    # --- International ---
    "The Guardian (Europe)": ("https://www.theguardian.com/europe/rss", "Guardian News & Media"),
    "The New Yorker": ("https://www.newyorker.com/feed/everything", "Condé Nast"),
    "The New York Times": ("https://rss.nytimes.com/services/xml/rss/nyt/HomePage.xml", "The New York Times Company"),
    "The Wall Street Journal": ("https://feeds.a.dj.com/rss/RSSWorldNews.xml", "Dow Jones & Company"),
}

ARTICLES_PER_SOURCE = 3        # headlines shown per outlet
CANDIDATES_PER_SOURCE = 15     # headlines per outlet that are scanned for big stories / trending words
MIN_SOURCES_FOR_ONMISBAAR = 4  # a story in at least 4 outlets goes in the ONMISBAAR box
MIN_SOURCES_FOR_TERM = 3       # a word in the headlines of MORE than 2 outlets is "trending"
MIN_SHARED_WORDS = 2           # two headlines are "the same story" if they share this many words
MAX_ONMISBAAR = 8              # max number of stories in the ONMISBAAR box
MAX_TERM_ARTICLES = 15         # safety cap on extra articles added because of trending words

# --- Opinion pieces (shown at the bottom) ------------------------------------------------
OPINION_COUNT = 3
OPINION_CANDIDATES = 8                   # headlines read per opinion feed
OPINION_SKIP_OUTLETS = {"De Telegraaf"}  # never show opinion from these outlets
OPINION_FEEDS = {                        # dedicated opinion feeds (a bad URL only gives a warning)
    "de Volkskrant": "https://www.volkskrant.nl/columns-opinie/rss.xml",
    "The Guardian": "https://www.theguardian.com/commentisfree/rss",
    "The New York Times": "https://rss.nytimes.com/services/xml/rss/nyt/Opinion.xml",
    "The Wall Street Journal": "https://feeds.a.dj.com/rss/RSSOpinion.xml",
}
# Articles from the normal feeds whose URL has one of these sections are also treated as opinion
OPINION_URL_SEGMENTS = {"opinie", "opinion", "opinions", "commentisfree", "columns-opinie", "columns", "column"}
SNIPPET_CHARS = 150       # FD's feed terms allow at most ~150 chars of article text
HEADERS = {"User-Agent": "Mozilla/5.0 (personal news digest bot)"}
SKIP_LINK_PARTS = ("/puzzels/",)  # links containing any of these are not news (e.g. NRC puzzles)

# --- Filters: no sport, no gossip -----------------------------------------------------
# A headline is dropped when a part of its URL, or a word in its title/summary, is on these
# lists. They are a starting point: add or remove words to taste.
EXCLUDE_URL_SEGMENTS = {
    # sport
    "sport", "sports", "voetbal", "football", "soccer", "wielrennen", "tennis", "darts",
    "formule-1", "f1", "schaatsen", "hockey", "basketball", "cricket", "rugby",
    "sportwereld", "atletiek", "handbal", "honkbal", "shorttrack", "volleybal", "basketbal",
    "motorsport", "autosport", "zwemmen", "paardensport", "wielersport",
    # gossip / celebrities
    "entertainment", "sterren", "celebrity", "celebrities", "show", "royals", "roddels", "showbizz",
}
EXCLUDE_WORDS = [
    # sport
    "voetbal", "eredivisie", "premier league", "champions league", "europa league", "wedstrijd",
    "doelpunt", "wielrennen", "tour de france", "formule 1", "verstappen", "tennis", "schaatsen",
    "olympisch", "football", "soccer", "nfl", "nba", "cricket", "feyenoord", "ajax", "psv",
    "bondscoach", "interland", "wielrenner", "peloton", "grand prix", "grote prijs", "atletiek",
    "handbal", "honkbal", "shorttrack", "basketbal", "volleybal", "darts", "hockey", "leeuwinnen",
    # gossip / celebrities
    "roddel", "bn'er", "bn’er", "bekende nederlander", "celebrity", "celebrities", "showbizz",
    "showbiz", "gossip", "royals",
]
EXCLUDE_RE = re.compile(r"(?<!\w)(?:" + "|".join(re.escape(w) for w in EXCLUDE_WORDS) + ")", re.I)

# Words that never count as a "term" (little meaning on their own)
STOPWORDS = set("""
voor naar niet maar door over wordt worden werd werden zijn heeft hebben hadden deze meer onder
tegen tussen zonder tijdens volgens eerste tweede derde nieuwe nieuw krijgt krijgen gaat gaan
kunnen moet moeten zegt zeggen willen alle alles weer veel goed jaar jaren dagen week weken uren
live liveblog video foto kijk lees vraagt waarom waar dit dat die zich haar hun onze boven binnen
buiten zelfs altijd nooit steeds opnieuw toch echt zeer slechts bijna vanaf sinds omdat terwijl
hoewel alleen allemaal geen enige andere anders samen eerst later laatste aantal procent miljoen
miljard euro dollar komt komen maakt maken blijft blijven geeft geven ziet zien vindt vinden doet
doen laat laten eens mogelijk tijd nodig houdt houden
that this with from have will says said after before about into their they them what when where
which while would could should more most than then been were also just only your here there some
every like look first last next year years still under between against amid news
""".split())

# Words that are too general to be "trending" even if they appear in many headlines
GENERIC_TERMS = {"nederland", "nederlandse", "nederlanders", "mensen", "wereld"}

# English -> Dutch, so English headlines can be matched with Dutch ones
ALIASES = {
    "greenland": "groenland", "denmark": "denemarken", "danish": "deense", "russia": "rusland",
    "russian": "russische", "ukraine": "oekraine", "ukrainian": "oekraiense", "germany": "duitsland",
    "german": "duitse", "france": "frankrijk", "french": "franse", "israeli": "israelische",
    "palestinians": "palestijnen", "hostage": "gijzelaars", "hostages": "gijzelaars",
    "netherlands": "nederland", "dutch": "nederlandse", "european": "europese", "europe": "europa",
    "nato": "navo", "tariffs": "tarieven", "election": "verkiezingen", "elections": "verkiezingen",
    "war": "oorlog",
}

MONTHS_NL = "januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december"


def clean(text: str) -> str:
    """Strip HTML tags/entities and collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0].rstrip(",.;:") + "…"


def fetch_articles(name: str, url: str) -> list[dict]:
    if name in SCRAPERS:
        return SCRAPERS[name](name, url)

    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[warn] {name}: could not fetch feed ({exc})", file=sys.stderr)
        return []

    feed = feedparser.parse(resp.content)
    if not feed.entries:
        print(f"[warn] {name}: feed contained no entries", file=sys.stderr)
        return []

    articles, seen = [], set()
    # Feeds are ordered by the outlet itself (front page / latest first),
    # so the first N entries are our proxy for "most prominent".
    for entry in feed.entries:
        link = entry.get("link")
        title = clean(entry.get("title", ""))
        if not link or not title or link in seen:
            continue
        if any(part in link for part in SKIP_LINK_PARTS):
            continue
        seen.add(link)
        snippet = truncate(clean(entry.get("summary", "")), SNIPPET_CHARS)
        categories = [t.get("term", "") for t in (entry.get("tags") or []) if t.get("term")]
        articles.append({"title": title, "link": link, "snippet": snippet, "categories": categories})
        if len(articles) >= CANDIDATES_PER_SOURCE:
            break
    return articles


def scrape_bnr(name: str, url: str) -> list[dict]:
    """BNR's RSS link returns plain HTML, so read the top stories off the homepage.

    Article links look like /nieuws/<section>/<numeric id>/<slug>. The homepage lists
    the lead stories first, so the first N unique links are the most prominent ones.
    """
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[warn] {name}: could not fetch page ({exc})", file=sys.stderr)
        return []

    anchor = re.compile(r'<a\b[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
    date_nl = re.compile(rf"(?<!\d)\d{{1,2}} (?:{MONTHS_NL}) \d{{4}}")
    article_path = re.compile(r"/nieuws/[a-z0-9-]+/\d{6,}/")

    articles, seen = [], set()
    for href, inner in anchor.findall(resp.text):
        if not article_path.search(href):
            continue
        link = urljoin(url, href.split("#")[0])
        if link in seen:
            continue
        title = date_nl.sub(" ", clean(inner))
        title = re.sub(r"\s+", " ", title).strip()
        repeated = re.fullmatch(r"(.+?)\s*\1", title)  # card text sometimes repeats the title
        if repeated:
            title = repeated.group(1).strip()
        if len(title) < 15:  # image-only links and labels
            continue
        seen.add(link)
        articles.append({"title": title, "link": link, "snippet": ""})
        if len(articles) >= CANDIDATES_PER_SOURCE:
            break

    if not articles:
        print(f"[warn] {name}: no headlines found on the page", file=sys.stderr)
    return articles


SCRAPERS = {"BNR": scrape_bnr}  # sources without a usable RSS feed

NOS_LATEST_URL = "https://nos.nl/nieuws/laatste"


def fetch_wekdienst() -> dict | None:
    """Find today's NOS Wekdienst (the morning news briefing) on NOS 'Laatste nieuws'.

    NOS publishes it every morning as an article whose URL contains the date, e.g.
    /artikel/2631601-wekdienst-19-9-protesten-in-den-haag-... (19 September).
    Only today's edition is returned, so you never get yesterday's by accident.
    """
    try:
        resp = requests.get(NOS_LATEST_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as exc:
        print(f"[warn] NOS Wekdienst: could not fetch page ({exc})", file=sys.stderr)
        return None

    today = date.today()
    slug_today = f"wekdienst-{today.day}-{today.month}-"
    anchor = re.compile(r'<a\b[^>]*?href="([^"]+)"[^>]*>(.*?)</a>', re.S | re.I)
    time_marker = re.compile(
        r"\s+(?:vandaag|gisteren|maandag|dinsdag|woensdag|donderdag|vrijdag|zaterdag|zondag),?\s*\d{1,2}:\d{2}\s*"
    )

    best = None
    for href, inner in anchor.findall(resp.text):
        if "/artikel/" not in href or slug_today not in href.lower():
            continue
        link = urljoin(NOS_LATEST_URL, href.split("#")[0])
        if best is None:  # safe fallback if the card text can't be parsed
            best = {"title": "NOS Wekdienst", "link": link, "snippet": ""}
        # Card text looks like: "<title> vandaag, 07:23 <teaser>"
        parts = time_marker.split(clean(inner), maxsplit=1)
        if parts[0].lower().startswith("wekdienst"):
            teaser = parts[1].strip() if len(parts) > 1 else ""
            best = {"title": parts[0].strip(), "link": link, "snippet": truncate(teaser, SNIPPET_CHARS)}
            break

    if best is None:
        print("[warn] NOS Wekdienst: today's edition not found (yet)", file=sys.stderr)
    return best


def _sport_or_gossip_section(segment: str) -> bool:
    """True if a URL segment is a sport/gossip section, e.g. 'voetbal', 'sportwereld', 'overig-sport'."""
    if segment in EXCLUDE_URL_SEGMENTS or segment.startswith("sport"):
        return True
    parts = segment.split("-")
    # short segments only, so long article slugs that merely contain the word don't match
    return len(parts) <= 3 and any(part in EXCLUDE_URL_SEGMENTS for part in parts)


def is_excluded(article: dict) -> bool:
    """True for sport and gossip: URL sections, feed categories, and words in title/summary."""
    segments = [seg for seg in urlparse(article["link"]).path.lower().split("/") if seg]
    if any(_sport_or_gossip_section(seg) for seg in segments):
        return True
    for category in article.get("categories", []):
        category = category.lower().strip()
        if category in EXCLUDE_URL_SEGMENTS or EXCLUDE_RE.search(category):
            return True
    return bool(EXCLUDE_RE.search(f"{article['title']} {article.get('snippet', '')}"))


def is_opinion(article: dict) -> bool:
    segments = {seg for seg in urlparse(article["link"]).path.lower().split("/") if seg}
    return bool(segments & OPINION_URL_SEGMENTS)


def tokens(title: str) -> set[str]:
    """The meaningful words of a headline: lowercase, no accents, no stopwords."""
    text = unicodedata.normalize("NFKD", title.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    words = [ALIASES.get(w, w) for w in re.findall(r"[a-z0-9]+", text)]
    return {w for w in words if len(w) >= 4 and not w.isdigit() and w not in STOPWORDS}


def find_onmisbaar(candidates: dict[str, list[dict]]) -> list[dict]:
    """Stories that appear in at least MIN_SOURCES_FOR_ONMISBAAR different outlets.

    Two headlines count as the same story when they share MIN_SHARED_WORDS meaningful words.
    Each story is built around one 'seed' headline (not by chaining), which avoids merging
    unrelated stories.
    """
    pool = [a for arts in candidates.values() for a in arts]
    order = {name: i for i, name in enumerate(FEEDS)}
    used: set[int] = set()
    clusters: list[dict] = []

    while len(clusters) < MAX_ONMISBAAR:
        best = None
        for seed in pool:
            if id(seed) in used:
                continue
            members = {seed["source"]: (seed, 0)}  # one article per outlet: the best match
            for other in pool:
                if id(other) in used or other["source"] == seed["source"]:
                    continue
                shared = len(seed["tok"] & other["tok"])
                if shared >= MIN_SHARED_WORDS and shared > members.get(other["source"], (None, 0))[1]:
                    members[other["source"]] = (other, shared)
            if len(members) >= MIN_SOURCES_FOR_ONMISBAAR and (best is None or len(members) > len(best)):
                best = [article for article, _ in members.values()]
        if best is None:
            break
        best.sort(key=lambda a: order.get(a["source"], 99))  # Dutch outlets first
        used.update(id(a) for a in best)
        clusters.append({"title": best[0]["title"], "members": best})
    return clusters


def trending_terms(candidates: dict[str, list[dict]]) -> dict[str, int]:
    """Words that occur in the headlines of at least MIN_SOURCES_FOR_TERM outlets -> number of outlets."""
    term_sources: dict[str, set[str]] = {}
    for name, arts in candidates.items():
        for a in arts:
            a["tok"] = tokens(a["title"])
            for term in a["tok"]:
                term_sources.setdefault(term, set()).add(name)
    return {
        t: len(srcs)
        for t, srcs in term_sources.items()
        if len(srcs) >= MIN_SOURCES_FOR_TERM and t not in GENERIC_TERMS
    }


def select_articles(candidates: dict[str, list[dict]]) -> tuple[list[dict], dict[str, list[dict]]]:
    """Return (ONMISBAAR stories, articles to show per outlet).

    Per outlet: the first ARTICLES_PER_SOURCE headlines, plus any further headline that
    contains a "trending" word (a word in the headlines of MIN_SOURCES_FOR_TERM+ outlets).
    """
    trending = trending_terms(candidates)
    onmisbaar = find_onmisbaar(candidates)
    in_box = {id(a) for c in onmisbaar for a in c["members"]}

    selected: dict[str, list[dict]] = {}
    extras = []
    for name, arts in candidates.items():
        rest = [a for a in arts if id(a) not in in_box]
        selected[name] = rest[:ARTICLES_PER_SOURCE]
        for a in rest[ARTICLES_PER_SOURCE:]:
            hits = sorted(a["tok"] & trending.keys(), key=lambda t: (-trending[t], t))
            if hits:
                a["hits"] = hits[:2]
                extras.append((trending[hits[0]], len(hits), name, a))
    extras.sort(key=lambda e: (-e[0], -e[1]))
    for _, _, name, a in extras[:MAX_TERM_ARTICLES]:
        selected[name].append(a)
    return onmisbaar, selected


def select_opinion(pool: list[dict], trending: dict[str, int]) -> list[dict]:
    """Pick OPINION_COUNT opinion pieces: those closest to today's news first, one per outlet."""
    seen, ranked = set(), []
    for i, a in enumerate(pool):
        outlet = a["source"].split(" (")[0]
        if outlet in OPINION_SKIP_OUTLETS or a["link"] in seen:
            continue
        seen.add(a["link"])
        score = sum(trending.get(t, 0) for t in tokens(a["title"]))
        ranked.append((-score, i, outlet, a))
    ranked.sort(key=lambda r: (r[0], r[1]))

    chosen, outlets = [], set()
    for _, _, outlet, a in ranked:
        if outlet not in outlets and len(chosen) < OPINION_COUNT:
            chosen.append(a)
            outlets.add(outlet)
    for _, _, _, a in ranked:  # too few outlets: allow a second piece from the same outlet
        if len(chosen) >= OPINION_COUNT:
            break
        if all(a is not c for c in chosen):
            chosen.append(a)
    return chosen


def build_email(
    digest: dict[str, list[dict]],
    wekdienst: dict | None = None,
    onmisbaar: list[dict] | None = None,
    opinion: list[dict] | None = None,
) -> tuple[str, str]:
    """Return (plain_text, html_body)."""
    today = date.today().strftime("%A %d %B %Y")
    text_parts = [f"Your news digest – {today}\n"]
    html_parts = [
        '<div style="font-family:Georgia,serif;max-width:640px;margin:auto;color:#222">',
        f'<h1 style="font-size:22px;border-bottom:2px solid #222;padding-bottom:8px">'
        f"News digest – {html.escape(today)}</h1>",
    ]

    if wekdienst:
        text_parts.append(f"== NOS Wekdienst ==\n{wekdienst['title']}\n  {wekdienst['link']}")
        if wekdienst["snippet"]:
            text_parts.append(f"  {wekdienst['snippet']}")
        html_parts.append(
            '<div style="background:#f3f6fb;border-left:4px solid #0b3d91;padding:12px 16px;margin:18px 0">'
            '<div style="font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#666">'
            "NOS Wekdienst</div>"
            f'<p style="margin:6px 0 0"><a href="{html.escape(wekdienst["link"])}" '
            f'style="font-size:18px;font-weight:bold;color:#0b3d91;text-decoration:none">'
            f'{html.escape(wekdienst["title"])}</a>'
            + (
                f'<br><span style="font-size:14px;color:#444">{html.escape(wekdienst["snippet"])}</span>'
                if wekdienst["snippet"]
                else ""
            )
            + "</p></div>"
        )

    if onmisbaar:
        text_parts.append("\n== ONMISBAAR ==")
        html_parts.append(
            '<div style="background:#fff4f2;border-left:4px solid #c62828;padding:12px 16px;margin:18px 0">'
            '<div style="font-size:14px;font-weight:bold;letter-spacing:3px;color:#c62828">ONMISBAAR</div>'
        )
        for story in onmisbaar:
            n = len(story["members"])
            text_parts.append(f"- {story['title']}  ({n} bronnen)")
            for a in story["members"]:
                text_parts.append(f"    {a['source']}: {a['link']}")
            links = " · ".join(
                f'<a href="{html.escape(a["link"])}" style="color:#0b3d91">{html.escape(a["source"])}</a>'
                for a in story["members"]
            )
            html_parts.append(
                f'<p style="margin:12px 0 0"><b style="font-size:17px">{html.escape(story["title"])}</b><br>'
                f'<span style="font-size:13px;color:#555">{n} bronnen: {links}</span></p>'
            )
        html_parts.append("</div>")

    for source, articles in digest.items():
        if not articles:
            continue
        text_parts.append(f"\n== {source} ==")
        html_parts.append(
            f'<h2 style="font-size:16px;text-transform:uppercase;letter-spacing:1px;'
            f'color:#666;margin-top:28px">{html.escape(source)}</h2>'
        )
        for a in articles:
            hits = a.get("hits") or []
            tag_text = f"  [veelgenoemd: {', '.join(hits)}]" if hits else ""
            tag_html = (
                f' <span style="font-size:12px;color:#b45309;font-weight:normal">'
                f"· veelgenoemd: {html.escape(', '.join(hits))}</span>"
                if hits
                else ""
            )
            text_parts.append(f"- {a['title']}{tag_text}\n  {a['link']}")
            if a["snippet"]:
                text_parts.append(f"  {a['snippet']}")
            html_parts.append(
                f'<p style="margin:14px 0"><a href="{html.escape(a["link"])}" '
                f'style="font-size:17px;font-weight:bold;color:#0b3d91;text-decoration:none">'
                f'{html.escape(a["title"])}</a>'
                + tag_html
                + (
                    f'<br><span style="font-size:14px;color:#444">{html.escape(a["snippet"])}</span>'
                    if a["snippet"]
                    else ""
                )
                + "</p>"
            )

    if opinion:
        text_parts.append("\n== Opinie ==")
        html_parts.append(
            '<h2 style="font-size:16px;text-transform:uppercase;letter-spacing:1px;'
            'color:#666;margin-top:28px">Opinie</h2>'
        )
        for a in opinion:
            outlet = a["source"].split(" (")[0]
            text_parts.append(f"- {a['title']} ({outlet})\n  {a['link']}")
            html_parts.append(
                f'<p style="margin:14px 0"><a href="{html.escape(a["link"])}" '
                f'style="font-size:17px;font-weight:bold;color:#0b3d91;text-decoration:none">'
                f'{html.escape(a["title"])}</a><br>'
                f'<span style="font-size:13px;color:#888">{html.escape(outlet)}</span>'
                + (
                    f'<br><span style="font-size:14px;color:#444">{html.escape(a["snippet"])}</span>'
                    if a.get("snippet")
                    else ""
                )
                + "</p>"
            )

    year = date.today().year
    holders = dict.fromkeys(holder for _, holder in FEEDS.values())  # de-duplicated, ordered
    if wekdienst:
        holders["NOS"] = None
    credits = " · ".join(f"© {holder} {year}" for holder in holders)
    text_parts.append(f"\n{credits}\nHeadlines link to the original articles (subscription may be required).")
    html_parts.append(
        f'<p style="font-size:12px;color:#888;margin-top:32px;border-top:1px solid #ddd;padding-top:10px">'
        f"{html.escape(credits)}<br>Headlines link to the original articles "
        f"(subscription may be required).</p></div>"
    )
    return "\n".join(text_parts), "".join(html_parts)


def send_email(subject: str, text: str, html_body: str) -> None:
    user = os.environ["SMTP_USER"]
    password = os.environ["SMTP_PASSWORD"]
    to = os.environ["EMAIL_TO"]
    host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    port = int(os.environ.get("SMTP_PORT", "465"))

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = to
    msg.set_content(text)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL(host, port) as server:
        server.login(user, password)
        server.send_message(msg)


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    candidates = {name: fetch_articles(name, url) for name, (url, _) in FEEDS.items()}
    for name, arts in candidates.items():
        for a in arts:
            a["source"] = name
    candidates = {name: [a for a in arts if not is_excluded(a)] for name, arts in candidates.items()}

    # Opinion pieces go in their own block at the bottom, not among the news
    opinion_pool: list[dict] = []
    for name in list(candidates):
        opinion_pool += [a for a in candidates[name] if is_opinion(a)]
        candidates[name] = [a for a in candidates[name] if not is_opinion(a)]
    for name, url in OPINION_FEEDS.items():
        for a in fetch_articles(name, url)[:OPINION_CANDIDATES]:
            a["source"] = name
            if not is_excluded(a):
                opinion_pool.append(a)

    onmisbaar, digest = select_articles(candidates)
    opinion = select_opinion(opinion_pool, trending_terms(candidates))
    wekdienst = fetch_wekdienst()
    if not wekdienst and not onmisbaar and not any(digest.values()):
        print("No articles fetched from any source; not sending.", file=sys.stderr)
        return 1

    text, html_body = build_email(digest, wekdienst, onmisbaar, opinion)
    subject = f"News digest – {date.today().isoformat()}"

    if dry_run:
        print(text)
        return 0

    send_email(subject, text, html_body)
    print("Digest sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
