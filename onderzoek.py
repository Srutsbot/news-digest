#!/usr/bin/env python3
"""Daily digest of NEW investigative-journalism articles from ~30 platforms.

Usage:
    python onderzoek.py             # fetch, send the email, remember what was sent
    python onderzoek.py --dry-run   # fetch and print the email; sends nothing, remembers nothing

Needs digest.py in the same folder (it reuses its helpers) and the same environment
variables as digest.py: SMTP_USER, SMTP_PASSWORD, EMAIL_TO.

"New" means: not seen in an earlier run. The links that were already reported are kept in
onderzoek_seen.json (the GitHub workflow saves that file back to the repository).
"""
import html
import json
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import requests

import digest  # reuse: clean, truncate, tokens, find_onmisbaar, send_email

GOOGLE_NEWS = "https://news.google.com/rss/search"


def google_news(domain: str, hl: str = "en-US", ceid: str = "US:en") -> str:
    """Feed with the newest articles of a site that has no feed of its own (via Google News).
    The links in the mail then go through Google first and end up at the original article."""
    return f"{GOOGLE_NEWS}?q=site:{domain}&hl={hl}&gl={ceid.split(':')[0]}&ceid={ceid}"


# name, homepage, language, optional feed URL. If the feed URL is missing or does not work,
# the script looks for the feed itself (the <link rel="alternate"> tag on the homepage, then
# common paths such as /feed/ and /rss).
PLATFORMS = [
    # --- Nederland ---
    ("Follow the Money", "https://www.ftm.nl/", "NL/EN", None),
    ("Investico", "https://www.platform-investico.nl/", "NL", None),
    ("Lighthouse Reports", "https://www.lighthousereports.com/", "EN", None),
    ("Bellingcat", "https://www.bellingcat.com/", "EN", "https://www.bellingcat.com/feed/"),
    ("Pointer (KRO-NCRV)", "https://pointer.kro-ncrv.nl/", "NL", google_news("pointer.kro-ncrv.nl", "nl", "NL:nl")),
    # --- Wereldwijd ---
    ("ICIJ", "https://www.icij.org/", "EN", "https://www.icij.org/feed/"),
    ("OCCRP", "https://www.occrp.org/", "EN", None),
    ("Forbidden Stories", "https://forbiddenstories.org/", "EN/FR", google_news("forbiddenstories.org")),
    ("The Bureau of Investigative Journalism", "https://www.thebureauinvestigates.com/", "EN", google_news("thebureauinvestigates.com", "en-GB", "GB:en")),
    # --- Europa ---
    ("Correctiv", "https://correctiv.org/", "DE", "https://correctiv.org/feed/"),
    ("Mediapart", "https://www.mediapart.fr/", "FR", "https://www.mediapart.fr/articles/feed"),
    ("IrpiMedia", "https://irpimedia.irpi.eu/", "IT", None),
    ("BIRN / Balkan Insight", "https://balkaninsight.com/", "EN", "https://balkaninsight.com/feed/"),
    ("The Insider", "https://theins.ru/en", "EN/RU", None),
    ("Investigate Europe", "https://www.investigate-europe.eu/", "EN", google_news("investigate-europe.eu")),
    # --- Noord-Amerika ---
    ("ProPublica", "https://www.propublica.org/", "EN", "https://feeds.propublica.org/propublica/main"),
    ("The Intercept", "https://theintercept.com/", "EN", "https://theintercept.com/feed/?rss"),
    # --- Midden-Amerika ---
    ("El Faro", "https://elfaro.net/", "ES/EN", None),
    # --- Zuid-Amerika ---
    ("Agência Pública", "https://apublica.org/", "PT", None),
    ("OjoPúblico", "https://ojo-publico.com/", "ES", google_news("ojo-publico.com", "es-419", "PE:es-419")),
    ("CIPER Chile", "https://www.ciperchile.cl/", "ES", None),
    # --- Midden-Oosten ---
    ("ARIJ", "https://arij.net/", "AR/EN", None),
    ("Mada Masr", "https://www.madamasr.com/", "EN/AR", None),
    ("Daraj", "https://daraj.media/", "AR/EN", None),
    ("Haaretz", "https://www.haaretz.com/", "EN", "https://www.haaretz.com/srv/haaretz-latest-headlines"),
    # --- Afrika ---
    ("amaBhungane", "https://amabhungane.org/", "EN", None),
    ("Premium Times", "https://www.premiumtimesng.com/", "EN", None),
    ("Inkyfada", "https://inkyfada.com/", "FR/AR", None),
    # --- Azië ---
    ("Rappler", "https://www.rappler.com/", "EN", None),
    ("Newstapa", "https://newstapa.org/", "KO/EN", google_news("newstapa.org", "ko", "KR:ko")),
    ("The Reporters' Collective", "https://www.reporters-collective.in/", "EN", google_news("reporters-collective.in", "en-IN", "IN:en")),
]

MAX_PER_PLATFORM = 3   # at most this many new articles per platform in one mail
MAX_TOTAL = 15         # at most this many articles in one mail (spread over the platforms)
SEEN_FILE = Path(__file__).with_name("onderzoek_seen.json")
SEEN_LIMIT = 5000      # how many old links to remember
EPOCH = datetime.min.replace(tzinfo=timezone.utc)
COMMON_FEED_PATHS = ("feed/", "rss", "rss.xml", "feed", "atom.xml", "index.xml",
                     "feed.xml", "rss/", "en/feed/", "?feed=rss2")
# Some sites refuse visitors that announce themselves as a bot, so this script presents itself
# like a normal browser (digest.py itself is not changed).
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": ("application/rss+xml, application/atom+xml, application/xml;q=0.9, "
               "text/xml;q=0.9, text/html;q=0.8, */*;q=0.5"),
    "Accept-Language": "en,nl;q=0.8",
}
FEED_TAG = re.compile(r'<link\b[^>]*?type="application/(?:rss|atom)\+xml"[^>]*>', re.I)


# ---------------------------------------------------------------- fetching
def read_feed(url: str):
    """Entries of the feed at url, or None if it is unreachable or not a feed."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    return feedparser.parse(resp.content).entries or None


def feed_candidates(homepage: str):
    """Possible feed URLs for a site: feed links in its HTML first, then common paths."""
    try:
        resp = requests.get(homepage, headers=HEADERS, timeout=20)
        resp.raise_for_status()
        for tag in FEED_TAG.findall(resp.text):
            match = re.search(r'href="([^"]+)"', tag)
            if match:
                url = urljoin(homepage, html.unescape(match.group(1)))
                if "comment" not in url.lower():  # WordPress also lists a comments feed
                    yield url
    except requests.RequestException:
        pass
    for path in COMMON_FEED_PATHS:
        yield urljoin(homepage, path)


def fetch_platform(name: str, homepage: str, language: str, hint: str | None):
    """Return (articles, feed_url). articles is [] when no working feed was found."""
    tried = set()
    for url in ([hint] if hint else []) + list(feed_candidates(homepage)):
        if url in tried:
            continue
        tried.add(url)
        entries = read_feed(url)
        if not entries:
            continue
        articles, seen_links = [], set()
        for entry in entries:
            link, title = entry.get("link"), digest.clean(entry.get("title", ""))
            via_google = url.startswith(GOOGLE_NEWS)
            if via_google:
                outlet = (entry.get("source") or {}).get("title", "")
                if outlet and title.endswith(f" - {outlet}"):
                    title = title[: -len(f" - {outlet}")].strip()
            if not link or not title or link in seen_links:
                continue
            seen_links.add(link)
            stamp = entry.get("published_parsed") or entry.get("updated_parsed")
            published = datetime(*stamp[:6], tzinfo=timezone.utc) if stamp else None
            articles.append({
                "source": name, "language": language, "title": title, "link": link,
                "snippet": "" if via_google else digest.truncate(digest.clean(entry.get("summary", "")), 200),
                "published": published,
            })
        if articles:
            return articles, url
    return [], None


# ---------------------------------------------------------------- state
def load_seen() -> list[str]:
    try:
        return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def save_seen(old: list[str], fetched_links: list[str]) -> None:
    known = set(old)
    merged = old + [link for link in fetched_links if link not in known]
    SEEN_FILE.write_text(json.dumps(merged[-SEEN_LIMIT:], indent=0), encoding="utf-8")


# ---------------------------------------------------------------- selection
def select_new(new_by_platform: dict[str, list[dict]]) -> list[dict]:
    """Newest first within a platform; then spread over platforms: everyone's newest, then everyone's second..."""
    for arts in new_by_platform.values():
        arts.sort(key=lambda a: a["published"] or EPOCH, reverse=True)
    picked: list[dict] = []
    for rank in range(MAX_PER_PLATFORM):
        layer = [arts[rank] for arts in new_by_platform.values() if len(arts) > rank]
        layer.sort(key=lambda a: a["published"] or EPOCH, reverse=True)
        picked += layer
    return picked[:MAX_TOTAL]


def shared_stories(picked: list[dict]) -> list[dict]:
    """Stories that at least two platforms picked up (matched on shared headline words)."""
    by_source: dict[str, list[dict]] = {}
    for a in picked:
        a["tok"] = digest.tokens(a["title"])
        by_source.setdefault(a["source"], []).append(a)
    # digest.find_onmisbaar takes no options: it reads these two module settings when it runs.
    # This script is a separate process, so changing them here does not affect digest.py itself.
    digest.MIN_SOURCES_FOR_ONMISBAAR = 2
    digest.MAX_ONMISBAAR = 5
    return digest.find_onmisbaar(by_source)


# ---------------------------------------------------------------- email
def build_email(picked, stories, failed, checked):
    today = date.today().strftime("%A %d %B %Y")
    esc = html.escape
    text, page = [f"Onderzoeksjournalistiek – {today}\n"], [
        '<div style="font-family:Georgia,serif;max-width:640px;margin:auto;color:#222">',
        f'<h1 style="font-size:22px;border-bottom:2px solid #222;padding-bottom:8px">'
        f"Onderzoeksjournalistiek – {esc(today)}</h1>",
    ]

    if not picked:
        line = f"Vandaag geen nieuwe onderzoeksartikelen bij de {checked} platforms die ik kon bereiken."
        text.append(line)
        page.append(f'<p style="font-size:16px">{esc(line)}</p>')
    else:
        in_story = {id(a) for s in stories for a in s["members"]}
        if stories:
            text.append("\n== Door meerdere platforms opgepakt ==")
            page.append(
                '<div style="background:#f3f6fb;border-left:4px solid #0b3d91;padding:12px 16px;margin:18px 0">'
                '<div style="font-size:13px;font-weight:bold;letter-spacing:2px;color:#0b3d91">'
                "DOOR MEERDERE PLATFORMS OPGEPAKT</div>"
            )
            for s in stories:
                text.append(f"- {s['title']}")
                links = " · ".join(
                    f'<a href="{esc(a["link"])}" style="color:#0b3d91">{esc(a["source"])}</a>'
                    for a in s["members"]
                )
                for a in s["members"]:
                    text.append(f"    {a['source']}: {a['link']}")
                page.append(
                    f'<p style="margin:12px 0 0"><b style="font-size:17px">{esc(s["title"])}</b><br>'
                    f'<span style="font-size:13px;color:#555">{links}</span></p>'
                )
            page.append("</div>")

        order = {name: i for i, (name, *_rest) in enumerate(PLATFORMS)}
        by_source: dict[str, list[dict]] = {}
        for a in picked:
            if id(a) not in in_story:
                by_source.setdefault(a["source"], []).append(a)
        for source in sorted(by_source, key=lambda n: order.get(n, 99)):
            text.append(f"\n== {source} ==")
            page.append(
                f'<h2 style="font-size:16px;text-transform:uppercase;letter-spacing:1px;'
                f'color:#666;margin-top:28px">{esc(source)}</h2>'
            )
            for a in by_source[source]:
                text.append(f"- {a['title']} [{a['language']}]\n  {a['link']}")
                if a["snippet"]:
                    text.append(f"  {a['snippet']}")
                page.append(
                    f'<p style="margin:14px 0"><a href="{esc(a["link"])}" '
                    f'style="font-size:17px;font-weight:bold;color:#0b3d91;text-decoration:none">'
                    f'{esc(a["title"])}</a> <span style="font-size:12px;color:#888">{esc(a["language"])}</span>'
                    + (f'<br><span style="font-size:14px;color:#444">{esc(a["snippet"])}</span>' if a["snippet"] else "")
                    + "</p>"
                )

    footer = "Koppen linken naar de originele artikelen; sommige zitten achter een betaalmuur. Koppen worden niet vertaald."
    text.append(f"\n{footer}")
    page.append(f'<p style="font-size:12px;color:#888;margin-top:32px;border-top:1px solid #ddd;padding-top:10px">{esc(footer)}')
    if failed:
        note = "Geen werkende feed gevonden voor: " + ", ".join(failed)
        text.append(note)
        page.append(f"<br>{esc(note)}")
    page.append("</p></div>")
    return "\n".join(text), "".join(page)


# ---------------------------------------------------------------- main
def main() -> int:
    dry_run = "--dry-run" in sys.argv
    old_seen = load_seen()
    seen = set(old_seen)

    new_by_platform: dict[str, list[dict]] = {}
    fetched_links: list[str] = []
    failed: list[str] = []
    for name, homepage, language, hint in PLATFORMS:
        articles, feed_url = fetch_platform(name, homepage, language, hint)
        if not articles:
            failed.append(name)
            print(f"[warn] {name}: no working feed found", file=sys.stderr)
            continue
        fresh = [a for a in articles if a["link"] not in seen]
        fetched_links += [a["link"] for a in articles]
        if fresh:
            new_by_platform[name] = fresh
        print(f"[ok] {name}: {len(articles)} items, {len(fresh)} new  ({feed_url})", file=sys.stderr)

    if len(failed) == len(PLATFORMS):
        print("No platform could be read; not sending.", file=sys.stderr)
        return 1

    picked = select_new(new_by_platform)
    stories = shared_stories(picked) if picked else []
    text, page = build_email(picked, stories, failed, len(PLATFORMS) - len(failed))

    if dry_run:
        print(text)
        return 0

    digest.send_email(f"Onderzoeksjournalistiek – {date.today().isoformat()}", text, page)
    save_seen(old_seen, fetched_links)
    print(f"Sent: {len(picked)} article(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
