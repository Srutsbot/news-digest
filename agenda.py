#!/usr/bin/env python3
"""Agenda for the page: what is planned on which day, collected from public calendars.

The page has no server, so the whole period is stored in the page and its date search looks in there.
A source that fails is reported on the page and never stops the others.
"""
import html
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from digest import HEADERS

AMSTERDAM = ZoneInfo("Europe/Amsterdam")
NEW_YORK = ZoneInfo("America/New_York")
DAYS_BACK = 7
DAYS_AHEAD = 90
THEME_DAYS_AHEAD = 30    # Wikipedia is asked once per day, so keep this period short

TK_URL = "https://gegevensmagazijn.tweedekamer.nl/OData/v4/2.0/Activiteit"
CBS_URL = "https://www.cbs.nl/nl-nl/publicatieplanning"
DNB_URL = "https://www.dnb.nl/statistieken/publicatiekalender/"
ECB_URL = "https://www.ecb.europa.eu/press/calendars/weekly/html/index.en.html"
BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BLS_URL = "https://www.bls.gov/schedule/news_release/current_year.asp"
BEA_URL = "https://www.bea.gov/news/schedule"
ONS_URL = "https://www.ons.gov.uk/releasecalendar?release-type=type-upcoming&sort=date-newest"
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
FF_PAGE = "https://www.forexfactory.com/calendar"
CURIA_URL = "https://curia.europa.eu/jcms/jcms/j_6/en/"

# Activities without news value for a reader outside the Kamer.
TK_SKIPPED_TYPES = {"Procedurevergadering", "Regeling van werkzaamheden", "Werkbezoek", "Vergadering"}
BLS_MAJOR = re.compile(r"Employment Situation|Consumer Price Index|Producer Price Index|Job Openings|Real Earnings", re.I)
MONTHS_NL = ["januari", "februari", "maart", "april", "mei", "juni", "juli", "augustus", "september", "oktober",
             "november", "december"]
MONTHS_NL_SHORT = {"jan": 1, "feb": 2, "mrt": 3, "apr": 4, "mei": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "okt": 10,
                   "nov": 11, "dec": 12}
MONTHS_EN = {m: i + 1 for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                             "September", "October", "November", "December"])}
FF_REGIONS = {"USD": "VS", "EUR": "Eurozone", "GBP": "VK", "JPY": "Japan", "CAD": "Canada", "AUD": "Australië",
              "NZD": "Nieuw-Zeeland", "CHF": "Zwitserland", "CNY": "China"}

# First match wins, so the specific themes come before the broad economic one.
THEMES = [(name, re.compile(pattern, re.I)) for name, pattern in [
    ("Zorg", r"zorg|gezondheid|ziekenhui|medisch|ggz|sterfte|\bhealth|\bdeaths?\b|\bbirths?\b|dakloos"),
    ("Klimaat en energie", r"klimaat|energie|stikstof|duurzaam|verduurzam|luchtvaart|climate|\benergy\b"),
    ("Landbouw en natuur", r"landbouw|natuur|visserij|voedsel|agricultur"),
    ("Wonen", r"woning|\bwonen\b|huur|volkshuisvesting|ruimtelijke|housing"),
    ("Migratie", r"asiel|migrati|vluchteling|statushouder|nieuwkomer"),
    ("Justitie en veiligheid", r"justitie|politie|veiligheid|rechtspraak|juridisch|strafrecht|\bcrime\b"),
    ("Onderwijs en wetenschap", r"onderwijs|wetenschap|universiteit|\bschool|cultuur|auteursrecht|\bmedia\b|education"),
    ("Digitaal", r"digita|cyber|privacy|generatieve|\b(?-i:AI)\b"),
    ("Verkeer", r"\bspoor|openbaar vervoer|mobiliteit|infrastructuur|verkeer"),
    ("Bestuur", r"binnenlandse zaken|gemeente|provincie|koninkrijk|kieswet|grondwet"),
    ("Werk en inkomen", r"arbeid|werkloos|werkgeleg|pensioen|\bloon|salaris|employ|labou?r|\bjobs?\b|wages|earnings|unemployment"),
    ("Buitenland en defensie", r"buitenland|defensie|navo|\bnato\b|oekra|rusland|ontwikkelingssamenwerking|europese zaken|raad van europa|minister-president van|\belections?\b|vrouwenrechten"),
    ("Economie en geld", r"financiën|economi|begroting|belasting|inflatie|inflation|export|\bbbp\b|\bgdp\b|rente|interest|\bbank|fiscale|\btrade\b|handel|eurogroep|ecofin|consumer price|producer price|retail|monetar"),
]]
ECONOMY = "Economie en geld"
POLITICS = "Politiek"
THEME_DAY = "Themadag"
RELIGIOUS_DAY = re.compile(r"heilig|zalig|\bsint\b|\bsan\b|kerk|katholie|orthodox|protestant|apostel|martelaar|bisschop|paus|kalender", re.I)


def theme_of(text: str, fallback: str) -> str:
    return next((name for name, pattern in THEMES if pattern.search(text)), fallback)


def get(url: str, **kwargs) -> requests.Response:
    resp = requests.get(url, headers=HEADERS, timeout=25, **kwargs)
    resp.raise_for_status()
    return resp


def text_lines(page: str) -> list[str]:
    page = re.sub(r"(?is)<(script|style|nav|footer).*?</\1>", " ", page)
    lines = html.unescape(re.sub(r"<[^>]+>", "\n", page)).splitlines()
    return [line.strip() for line in lines if line.strip()]


def strip_tags(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


def event(day: date, time: str, title: str, source: str, link: str, theme: str, detail: str = "") -> dict:
    return {"date": day.isoformat(), "time": time, "title": title[:200], "source": source, "link": link,
            "theme": theme, "detail": detail[:120]}


def fetch_tweede_kamer(first: date, last: date) -> list[dict]:
    url, params = TK_URL, {
        "$filter": f"Verwijderd eq false and Besloten eq false and Datum ge {first}T00:00:00Z and Datum lt {last + timedelta(days=1)}T00:00:00Z",
        "$orderby": "Datum", "$select": "Soort,Nummer,Onderwerp,Aanvangstijd,Datum,Status,Voortouwnaam",
    }
    events = []
    while url:
        data = get(url, params=params).json()
        for item in data.get("value", []):
            kind = item.get("Soort") or ""
            if kind in TK_SKIPPED_TYPES or kind.startswith("Inbreng") or item.get("Status") in ("Geannuleerd", "Verplaatst"):
                continue
            when = datetime.fromisoformat(item.get("Aanvangstijd") or item["Datum"]).astimezone(AMSTERDAM)
            committee = item.get("Voortouwnaam") or ""
            title = item.get("Onderwerp") or kind
            section = "plenaire_vergaderingen/details/activiteit" if committee == "TK" else "commissievergaderingen/details"
            events.append(event(when.date(), when.strftime("%H:%M") if item.get("Aanvangstijd") else "", title, "Tweede Kamer",
                                f"https://www.tweedekamer.nl/debat_en_vergadering/{section}?id={item['Nummer']}",
                                theme_of(f"{title} {committee}", POLITICS), kind))
        url, params = data.get("@odata.nextLink"), None
    return events


def fetch_cbs(first: date, last: date) -> list[dict]:
    events = []
    for stamp, row in re.findall(r'(?is)<tr\b[^>]*\bid="(\d{8})-[^"]*"[^>]*>(.*?)</tr>', get(CBS_URL).text):
        day = datetime.strptime(stamp, "%Y%m%d").date()
        title = re.search(r'(?is)<h3[^>]*>(.*?)</h3>', row)
        time = re.search(r'(?is)headers="hdr-time"[^>]*>(.*?)</td>', row)
        description = re.search(r'(?is)class="pub-description[^"]*"[^>]*>(.*?)(?:<span|</p>)', row)
        if title and first <= day <= last:
            name, about = strip_tags(title.group(1)), strip_tags(description.group(1)) if description else ""
            events.append(event(day, strip_tags(time.group(1)) if time else "", name, "CBS", CBS_URL,
                                theme_of(name, theme_of(about, ECONOMY)), about))
    return events


def fetch_dnb(first: date, last: date) -> list[dict]:
    lines = text_lines(get(DNB_URL).text)
    events = []
    for i, line in enumerate(lines[:-1]):
        if re.fullmatch(r"\d\d-\d\d-\d{4}", line):
            day = datetime.strptime(line, "%d-%m-%Y").date()
            if first <= day <= last:
                events.append(event(day, "", lines[i + 1], "DNB", DNB_URL, theme_of(lines[i + 1], ECONOMY), "Nieuwe cijfers"))
    return events


def fetch_ecb(first: date, last: date) -> list[dict]:
    labels = {"Board member:", "Event:", "Time:", "Venue:", "Contact number:", "E-mail address:", "Text:", "Notes:"}
    lines = text_lines(get(ECB_URL).text)
    events = []
    for i, line in enumerate(lines):
        match = re.fullmatch(r"\w+,\s+(\d{1,2}) (\w+) (\d{4})", line)
        if not match or match.group(2) not in MONTHS_EN:
            continue
        day = date(int(match.group(3)), MONTHS_EN[match.group(2)], int(match.group(1)))
        fields, key = {}, None
        for entry in lines[i + 1:]:
            if entry.startswith("Last modified") or re.fullmatch(r"\w+,\s+\d{1,2} \w+ \d{4}", entry):
                break
            if entry in labels:
                key = entry
            elif key:
                fields.setdefault(key, entry)
        if first <= day <= last and "Event:" in fields:
            time = re.match(r"\d\d:\d\d", fields.get("Time:", ""))
            events.append(event(day, time.group(0) if time else "", fields["Event:"], "ECB", ECB_URL,
                                theme_of(fields["Event:"], ECONOMY), fields.get("Board member:", "")))
    return events


def fetch_bls(first: date, last: date) -> list[dict]:
    events = []
    for block in get(BLS_ICS_URL).text.split("BEGIN:VEVENT")[1:]:
        start = re.search(r"DTSTART[^:]*:(\d{8}T\d{4})", block)
        summary = re.search(r"SUMMARY:(.*)", block)
        if not start or not summary or not BLS_MAJOR.search(summary.group(1)):
            continue
        when = datetime.strptime(start.group(1), "%Y%m%dT%H%M").replace(tzinfo=NEW_YORK).astimezone(AMSTERDAM)
        title = summary.group(1).strip().replace("\\,", ",")
        if first <= when.date() <= last:
            events.append(event(when.date(), when.strftime("%H:%M"), f"VS: {title}", "BLS (VS)", BLS_URL, theme_of(title, ECONOMY)))
    return events


def fetch_bea(first: date, last: date) -> list[dict]:
    lines = text_lines(get(BEA_URL).text)
    year_line = next((line for line in lines if re.fullmatch(r"Year \d{4}", line)), f"Year {first.year}")
    year = int(year_line.split()[1])
    events = []
    for i, line in enumerate(lines):
        match = re.fullmatch(r"(\w+) (\d{1,2})", line)
        if not match or match.group(1) not in MONTHS_EN or i + 4 >= len(lines):
            continue
        clock = re.fullmatch(r"(\d{1,2}:\d\d) ([AP]M)", lines[i + 1])
        if not clock:
            continue
        title = next((entry for entry in lines[i + 2:i + 6] if len(entry) > 8), "")
        moment = datetime.strptime(f"{year} {match.group(1)} {match.group(2)} {lines[i + 1]}", "%Y %B %d %I:%M %p")
        when = moment.replace(tzinfo=NEW_YORK).astimezone(AMSTERDAM)
        if title and first <= when.date() <= last:
            events.append(event(when.date(), when.strftime("%H:%M"), f"VS: {title}", "BEA (VS)", BEA_URL, theme_of(title, ECONOMY)))
    return events


def fetch_ons(first: date, last: date) -> list[dict]:
    lines = text_lines(get(ONS_URL).text)
    events = []
    for i, line in enumerate(lines):
        match = re.fullmatch(r"(\d{1,2}) (\w+) (\d{4}) (\d{1,2}:\d\d)(am|pm)", line)
        if not match or match.group(2) not in MONTHS_EN or i < 2 or lines[i - 1] != "Release date:":
            continue
        day = date(int(match.group(3)), MONTHS_EN[match.group(2)], int(match.group(1)))
        moment = datetime.strptime(f"{match.group(4)} {match.group(5)}", "%I:%M %p")
        when = moment.replace(year=day.year, month=day.month, day=day.day, tzinfo=ZoneInfo("Europe/London")).astimezone(AMSTERDAM)
        title = lines[i - 2]
        if first <= day <= last:
            events.append(event(day, when.strftime("%H:%M"), f"VK: {title}", "ONS (VK)", ONS_URL, theme_of(title, ECONOMY)))
    return events


def fetch_economic_calendar(first: date, last: date) -> list[dict]:
    events = []
    for item in get(FF_URL).json():
        if item.get("impact") != "High":
            continue
        when = datetime.fromisoformat(item["date"]).astimezone(AMSTERDAM)
        region = FF_REGIONS.get(item.get("country"), item.get("country", ""))
        if first <= when.date() <= last:
            events.append(event(when.date(), when.strftime("%H:%M"), f"{region}: {item['title']}", "Economische kalender",
                                FF_PAGE, theme_of(item["title"], ECONOMY)))
    return events


def fetch_eu_court(first: date, last: date) -> list[dict]:
    """Hearings the EU Court of Justice announces on its home page."""
    lines = text_lines(get(CURIA_URL).text)
    events = []
    for i, line in enumerate(lines):
        match = re.match(r"On (\d{1,2}) (\w+) (\d{4}), the Court of Justice will hear", line)
        if match and match.group(2) in MONTHS_EN and i and lines[i - 1].startswith("Hearing"):
            day = date(int(match.group(3)), MONTHS_EN[match.group(2)], int(match.group(1)))
            if first <= day <= last:
                events.append(event(day, "", f"Hof van Justitie EU: {lines[i - 1].removeprefix('Hearing: ')}", "Hof van Justitie EU",
                                    CURIA_URL, "Justitie en veiligheid", "Zitting"))
    return events


def clean_wikitext(line: str) -> str:
    line = re.sub(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]", r"\1", line)
    line = re.sub(r"\{\{[^{}]*\}\}|<[^>]+>|'{2,}", "", line)
    return " ".join(line.split()).strip(" -–:")


def fetch_theme_days(first: date, last: date) -> list[dict]:
    """Remembrance and theme days from the Dutch Wikipedia day pages. Saints' days are left out."""
    today = datetime.now(AMSTERDAM).date()
    days = [today + timedelta(days=n) for n in range(THEME_DAYS_AHEAD + 1)]

    def one_day(day: date) -> list[dict]:
        page = f"{day.day}_{MONTHS_NL[day.month - 1]}"
        section = re.search(r"(?ms)^==\s*Viering/herdenking\s*==\s*$(.*?)(?=^==[^=]|\Z)", get(f"https://nl.wikipedia.org/wiki/{page}?action=raw").text)
        found, heading = [], ""
        for line in (section.group(1).splitlines() if section else []):
            heading_match = re.fullmatch(r"===\s*(.*?)\s*===\s*", line)
            if heading_match:
                heading = clean_wikitext(heading_match.group(1))
            if not line.startswith("* ") or RELIGIOUS_DAY.search(heading):
                continue
            title = clean_wikitext(line[2:])
            if title and not RELIGIOUS_DAY.search(title):
                found.append(event(day, "", f"{heading}: {title}" if heading else title, "Wikipedia",
                                   f"https://nl.wikipedia.org/wiki/{page}", THEME_DAY))
        return found

    with ThreadPoolExecutor(4) as pool:
        return [item for found in pool.map(one_day, days) for item in found if first <= day_of(item) <= last]


def day_of(item: dict) -> date:
    return date.fromisoformat(item["date"])


SOURCES = [
    ("Tweede Kamer", fetch_tweede_kamer), ("CBS", fetch_cbs), ("DNB", fetch_dnb), ("ECB", fetch_ecb),
    ("BLS (VS)", fetch_bls), ("BEA (VS)", fetch_bea), ("ONS (VK)", fetch_ons),
    ("Economische kalender", fetch_economic_calendar), ("Hof van Justitie EU", fetch_eu_court),
    ("Wikipedia", fetch_theme_days),
]


def build_agenda(now: datetime) -> dict:
    today = now.astimezone(AMSTERDAM).date()
    first, last = today - timedelta(days=DAYS_BACK), today + timedelta(days=DAYS_AHEAD)

    def run(source: tuple) -> tuple[str, list[dict] | None]:
        name, fetch = source
        try:
            return name, fetch(first, last)
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
            print(f"[warn] agenda {name}: {exc}", file=sys.stderr)
            return name, None

    with ThreadPoolExecutor(len(SOURCES)) as pool:
        results = list(pool.map(run, SOURCES))
    events = sorted((e for _name, found in results for e in found or []), key=lambda e: (e["date"], e["time"] or "99", e["title"]))
    return {"from": first.isoformat(), "to": last.isoformat(), "events": events,
            "sources": [{"name": name, "ok": found is not None, "count": len(found or [])} for name, found in results]}
