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
from datetime import date
from email.message import EmailMessage
from urllib.parse import urljoin

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

ARTICLES_PER_SOURCE = 3   # how many headlines per outlet (13 sources x 3 = ~39 headlines)
SNIPPET_CHARS = 150       # FD's feed terms allow at most ~150 chars of article text
HEADERS = {"User-Agent": "Mozilla/5.0 (personal news digest bot)"}
SKIP_LINK_PARTS = ("/puzzels/",)  # links containing any of these are not news (e.g. NRC puzzles)

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
        articles.append({"title": title, "link": link, "snippet": snippet})
        if len(articles) >= ARTICLES_PER_SOURCE:
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
        if len(articles) >= ARTICLES_PER_SOURCE:
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


def build_email(digest: dict[str, list[dict]], wekdienst: dict | None = None) -> tuple[str, str]:
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

    for source, articles in digest.items():
        if not articles:
            continue
        text_parts.append(f"\n== {source} ==")
        html_parts.append(
            f'<h2 style="font-size:16px;text-transform:uppercase;letter-spacing:1px;'
            f'color:#666;margin-top:28px">{html.escape(source)}</h2>'
        )
        for a in articles:
            text_parts.append(f"- {a['title']}\n  {a['link']}")
            if a["snippet"]:
                text_parts.append(f"  {a['snippet']}")
            html_parts.append(
                f'<p style="margin:14px 0"><a href="{html.escape(a["link"])}" '
                f'style="font-size:17px;font-weight:bold;color:#0b3d91;text-decoration:none">'
                f'{html.escape(a["title"])}</a>'
                + (
                    f'<br><span style="font-size:14px;color:#444">{html.escape(a["snippet"])}</span>'
                    if a["snippet"]
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

    digest = {name: fetch_articles(name, url) for name, (url, _) in FEEDS.items()}
    wekdienst = fetch_wekdienst()
    if not wekdienst and not any(digest.values()):
        print("No articles fetched from any source; not sending.", file=sys.stderr)
        return 1

    text, html_body = build_email(digest, wekdienst)
    subject = f"News digest – {date.today().isoformat()}"

    if dry_run:
        print(text)
        return 0

    send_email(subject, text, html_body)
    print("Digest sent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
