"""Zeneipari állásfigyelő.

Végigjárja a sources.yaml-ban felsorolt cégek állásoldalait és néhány
álláskereső portált, kiszűri a nem zenei állásokat, megjelöli az újakat,
és e-mailben (Brevo) elküldi az összesítőt.

Környezeti változók (GitHub Secrets):
    BREVO_API_KEY   – Brevo API-kulcs
    SENDER_EMAIL    – a Brevóban hitelesített feladó címe
    RECIPIENTS      – címzett(ek), vesszővel elválasztva
Ha BREVO_API_KEY nincs megadva, a levél csak preview.html-be íródik.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote_plus, urljoin, urlparse, urlunparse

import requests
import yaml
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / "sources.yaml"
STATE_PATH = ROOT / "data" / "seen.json"
PREVIEW_PATH = ROOT / "preview.html"

FORGET_AFTER_DAYS = 60  # ennyi nap után "elfelejti" a már nem látott hirdetést
REQUEST_PAUSE = 2.0     # másodperc két kérés között (kíméljük az oldalakat)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"
    ),
    "Accept-Language": "hu-HU,hu;q=0.9,en;q=0.8",
}

# Állásoldalra mutató link felismerése (URL-ben vagy a link szövegében)
CAREER_LINK_RE = re.compile(
    r"karrier|career|állás|allas|jobs?\b|munkatárs|munkatars|csatlakozz|"
    r"join[\s_-]?us|dolgozz|work[\s_-]?with[\s_-]?us|hiring|álláslehetőség",
    re.I,
)
# Ezek nem állásoldalak, csak hasonló szavakat tartalmaznak (csapatoldal, önkéntesség, fellépőknek)
NOT_CAREER_RE = re.compile(
    r"munkatársaink|munkatarsaink|csapatunk|rólunk|rolunk|about|team|önkéntes|onkentes|"
    r"volunteer|fellépő|fellepo|artist|zenekar|press|sajtó|sajto",
    re.I,
)
# Állásnak tűnő linkszöveg az állásoldalon
JOB_TITLE_RE = re.compile(
    r"munkatárs|menedzser|manager|koordinátor|coordinator|asszisztens|assistant|"
    r"vezető|head of|lead|specialista|specialist|referens|producer|booker|"
    r"szerkesztő|editor|gyakornok|intern|technikus|technician|hangmérnök|"
    r"engineer|developer|fejlesztő|designer|grafikus|marketing|pr\b|"
    r"kommunikáció|értékesít|sales|ügyintéző|officer|director|igazgató|"
    r"keresünk|hiring|pozíció|position|állás",
    re.I,
)


# ─────────────────────────────── modellek ───────────────────────────────

@dataclass
class Job:
    title: str
    url: str
    source: str            # pl. "Budapest Park" vagy "LinkedIn"
    company: str = ""
    location: str = ""
    is_new: bool = False

    @property
    def key(self) -> str:
        return normalize_url(self.url)


@dataclass
class SiteStatus:
    name: str
    status: str            # "found" | "no_page" | "changed" | "unchanged" | "error" | "linkedin_only"
    detail: str = ""
    url: str = ""


@dataclass
class Report:
    jobs: list[Job] = field(default_factory=list)
    statuses: list[SiteStatus] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ─────────────────────────────── segédek ────────────────────────────────

def normalize_url(url: str) -> str:
    p = urlparse(url)
    # LinkedIn: a job-azonosító elég, a többi paraméter követőkód
    m = re.search(r"linkedin\.com/jobs/view/(?:[^/]*-)?(\d+)", url)
    if m:
        return f"linkedin:{m.group(1)}"
    return urlunparse((p.scheme, p.netloc.lower().removeprefix("www."), p.path.rstrip("/"), "", "", ""))


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def term_regex(terms: list[str]) -> re.Pattern:
    """Szó eleji egyezés: a 'zene' egyezik a 'zenei'-vel, de az 'adjunktus' nem a 'dj'-vel."""
    parts = [r"(?<![0-9a-záéíóöőúüű])" + re.escape(t.lower()) for t in terms if t]
    return re.compile("|".join(parts) if parts else r"(?!x)x", re.I)


class Fetcher:
    def __init__(self):
        self.s = requests.Session()
        self.s.headers.update(HEADERS)
        self._last = 0.0

    def get(self, url: str, retries: int = 2) -> requests.Response:
        for attempt in range(retries + 1):
            wait = REQUEST_PAUSE - (time.time() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.time()
            try:
                r = self.s.get(url, timeout=25)
                if r.status_code == 429 and attempt < retries:
                    time.sleep(15 * (attempt + 1))
                    continue
                r.raise_for_status()
                return r
            except requests.RequestException:
                if attempt == retries:
                    raise
                time.sleep(4)
        raise RuntimeError("unreachable")

    def soup(self, url: str) -> BeautifulSoup:
        r = self.get(url)
        r.encoding = r.apparent_encoding or r.encoding
        return BeautifulSoup(r.text, "html.parser")


class Filter:
    def __init__(self, cfg: dict):
        self.music = term_regex(cfg.get("music_terms", []))
        self.exclude = term_regex(cfg.get("exclude_terms", []))
        # Cégnév: kis- és nagybetű számít, és egész szóként kell szerepelnie
        # (így a "Sziget" nem egyezik a "Szigetszentmiklós"-sal, a "Believe" nem a "believe"-vel).
        names = [re.escape(c["name"]) for c in cfg["companies"]]
        self.company_re = re.compile(r"(?<![\wÁÉÍÓÖŐÚÜŰáéíóöőúüű])(?:" + "|".join(names) + r")(?![\wÁÉÍÓÖŐÚÜŰáéíóöőúüű])")

    def known_company(self, text: str) -> bool:
        return bool(self.company_re.search(text))

    def excluded(self, text: str) -> bool:
        return bool(self.exclude.search(text.lower()))

    def is_music(self, title: str, context: str = "") -> bool:
        blob = f"{title} {context}".lower()
        if self.excluded(title):
            return False
        return bool(self.music.search(blob)) or self.known_company(context)


# ─────────────────────────── cégek saját oldalai ───────────────────────────

def same_site(a: str, b: str) -> bool:
    ha = urlparse(a).netloc.lower().removeprefix("www.")
    hb = urlparse(b).netloc.lower().removeprefix("www.")
    return ha == hb or ha.endswith("." + hb) or hb.endswith("." + ha)


def find_career_pages(home_url: str, soup: BeautifulSoup) -> list[str]:
    found: list[str] = []
    for a in soup.find_all("a", href=True):
        href = urljoin(home_url, a["href"])
        if not href.startswith("http") or not same_site(href, home_url):
            continue
        text = clean(a.get_text(" "))
        path = urlparse(href).path
        strong = re.search(r"karrier|career|jobs|allas|állás", f"{path} {text}", re.I)
        if not strong and (NOT_CAREER_RE.search(path) or NOT_CAREER_RE.search(text)):
            continue
        if CAREER_LINK_RE.search(path) or (len(text) < 40 and CAREER_LINK_RE.search(text)):
            n = normalize_url(href)
            if n not in [normalize_url(f) for f in found]:
                found.append(href)
    # rövidebb (általánosabb) útvonal előre: /karrier előbb, mint /karrier/xy-pozicio
    found.sort(key=lambda u: len(urlparse(u).path))
    return found[:3]


def extract_jobs_from_page(page_url: str, soup: BeautifulSoup, nav_links: set[str]) -> list[tuple[str, str]]:
    for tag in soup(["nav", "header", "footer", "script", "style", "noscript"]):
        tag.decompose()
    jobs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a["href"])
        if not href.startswith("http"):
            continue
        n = normalize_url(href)
        if n in nav_links or n == normalize_url(page_url) or n in seen:
            continue
        text = clean(a.get_text(" "))
        if not (6 <= len(text) <= 140):
            continue
        if JOB_TITLE_RE.search(text) or CAREER_LINK_RE.search(urlparse(href).path):
            seen.add(n)
            jobs.append((text, href))
    # Ha linkek nincsenek, de címsorok igen (pl. egy oldalon felsorolt pozíciók)
    if not jobs:
        for h in soup.find_all(["h2", "h3", "h4"]):
            text = clean(h.get_text(" "))
            if 6 <= len(text) <= 140 and JOB_TITLE_RE.search(text):
                jobs.append((text, page_url))
    return jobs


def page_fingerprint(soup: BeautifulSoup) -> str:
    main = soup.find("main") or soup.body or soup
    return hashlib.sha256(clean(main.get_text(" ")).encode()).hexdigest()[:16]


def scan_company(company: dict, fetcher: Fetcher, flt: Filter, state: dict, report: Report) -> None:
    name, site = company["name"], company.get("site")
    if not site:
        report.statuses.append(SiteStatus(name, "linkedin_only"))
        return
    try:
        home = fetcher.soup(site)
    except Exception as e:  # noqa: BLE001
        report.statuses.append(SiteStatus(name, "error", f"a főoldal nem töltött be ({short_err(e)})", site))
        return

    nav_links = {normalize_url(urljoin(site, a["href"])) for a in home.find_all("a", href=True)}
    pages = [company["careers_url"]] if company.get("careers_url") else find_career_pages(site, home)
    if not pages:
        report.statuses.append(SiteStatus(name, "no_page", "nincs állásoldal – portálokon figyeljük", site))
        return

    page = pages[0]
    try:
        soup = fetcher.soup(page)
    except Exception as e:  # noqa: BLE001
        report.statuses.append(SiteStatus(name, "error", f"az állásoldal nem töltött be ({short_err(e)})", page))
        return

    fp = page_fingerprint(soup)
    fp_key = f"page:{normalize_url(page)}"
    old_fp = state["pages"].get(fp_key)
    state["pages"][fp_key] = fp

    found = 0
    for title, url in extract_jobs_from_page(page, soup, nav_links):
        if flt.excluded(title):
            continue
        report.jobs.append(Job(title=title, url=url, source=name, company=name))
        found += 1

    if found:
        report.statuses.append(SiteStatus(name, "found", f"{found} hirdetés", page))
    elif old_fp and old_fp != fp:
        report.statuses.append(SiteStatus(name, "changed", "az állásoldal tartalma megváltozott – érdemes ránézni", page))
    else:
        report.statuses.append(SiteStatus(name, "unchanged", "van állásoldal, most nincs rajta hirdetés", page))


# ──────────────────────────────── portálok ────────────────────────────────

def scan_linkedin(query: str, fetcher: Fetcher, flt: Filter, report: Report, must_match_company: str | None = None) -> None:
    url = (
        "https://www.linkedin.com/jobs-api/seeMoreJobPostings/search"
        f"?keywords={quote_plus(query)}&location=Hungary&f_TPR=r691200&start=0"
    )
    soup = fetcher.soup(url)
    for card in soup.select("div.base-card, li"):
        link = card.select_one("a.base-card__full-link, a[href*='/jobs/view/']")
        title_el = card.select_one(".base-search-card__title, h3")
        if not link or not title_el:
            continue
        title = clean(title_el.get_text(" "))
        company = clean((card.select_one(".base-search-card__subtitle, h4") or title_el).get_text(" "))
        location = clean((card.select_one(".job-search-card__location") or BeautifulSoup("", "html.parser")).get_text(" "))
        if must_match_company:
            a, b = must_match_company.lower(), company.lower()
            if a not in b and b not in a:
                continue
        if not flt.is_music(title, company):
            continue
        report.jobs.append(Job(title, link["href"].split("?")[0], "LinkedIn", company, location))


def scan_generic_portal(name: str, url_template: str, href_re: str, query: str,
                        fetcher: Fetcher, flt: Filter, report: Report) -> None:
    url = url_template.format(q=quote_plus(query))
    soup = fetcher.soup(url)
    pat = re.compile(href_re)
    for a in soup.find_all("a", href=True):
        href = urljoin(url, a["href"])
        if not pat.search(href):
            continue
        title = clean(a.get_text(" "))
        if not (6 <= len(title) <= 160):
            continue
        # a hirdetés "kártyája": pár szinttel feljebb ott a cégnév, helyszín
        # (csak addig megyünk felfelé, amíg a szülőben ez az egyetlen hirdetés)
        ctx_el = a
        for _ in range(3):
            parent = ctx_el.parent
            if parent is None or len(clean(parent.get_text(" "))) >= 600:
                break
            links_in_parent = {urljoin(url, x["href"]) for x in parent.find_all("a", href=True)
                               if pat.search(urljoin(url, x["href"]))}
            if len(links_in_parent) > 1:
                break
            ctx_el = parent
        context = clean(ctx_el.get_text(" "))
        if not flt.is_music(title, context):
            continue
        report.jobs.append(Job(title, href, name, company=guess_company(context, title)))


def guess_company(context: str, title: str) -> str:
    rest = context.replace(title, " ", 1)
    return clean(rest)[:80]


PORTALS = [
    # (név, kereső-URL sablon, állás-link mintája)
    ("Profession.hu", "https://www.profession.hu/allasok/1,0,0,{q}", r"profession\.hu/allas/"),
    ("Dreamjobs", "https://dreamjobs.hu/hu/allasok?search={q}", r"dreamjobs\.hu/(hu|en)/(job|allas)/"),
]


# ─────────────────────────────── állapot ───────────────────────────────

def load_state() -> dict:
    if STATE_PATH.exists():
        s = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        s.setdefault("jobs", {})
        s.setdefault("pages", {})
        return s
    return {"jobs": {}, "pages": {}}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")


def mark_new(jobs: list[Job], state: dict, first_run: bool) -> list[Job]:
    today = date.today().isoformat()
    unique: dict[str, Job] = {}
    for j in jobs:
        unique.setdefault(j.key, j)
    for key, j in unique.items():
        rec = state["jobs"].get(key)
        j.is_new = (rec is None) and not first_run
        state["jobs"][key] = {"first": rec["first"] if rec else today, "last": today, "title": j.title}
    cutoff = (date.today() - timedelta(days=FORGET_AFTER_DAYS)).isoformat()
    state["jobs"] = {k: v for k, v in state["jobs"].items() if v["last"] >= cutoff}
    return list(unique.values())


# ─────────────────────────────── e-mail ───────────────────────────────

def short_err(e: Exception) -> str:
    if isinstance(e, requests.HTTPError) and e.response is not None:
        return f"HTTP {e.response.status_code}"
    return type(e).__name__


def esc(s: str) -> str:
    return html.escape(s or "", quote=True)


def job_row(j: Job) -> str:
    meta = " · ".join(x for x in [j.company if j.company != j.source else "", j.location] if x)
    badge = '<span style="background:#e8452c;color:#fff;font-size:11px;font-weight:700;padding:2px 6px;border-radius:3px;margin-right:6px">ÚJ</span>' if j.is_new else ""
    return (
        f'<tr><td style="padding:10px 0;border-bottom:1px solid #eee">{badge}'
        f'<a href="{esc(j.url)}" style="color:#111;font-weight:600;text-decoration:none">{esc(j.title)}</a>'
        f'<div style="color:#666;font-size:13px;margin-top:2px">{esc(j.source)}{(" · " + esc(meta)) if meta else ""}</div>'
        f"</td></tr>"
    )


def build_email(report: Report, first_run: bool) -> tuple[str, str]:
    new = [j for j in report.jobs if j.is_new]
    old = [j for j in report.jobs if not j.is_new]
    for lst in (new, old):
        lst.sort(key=lambda j: (j.source.lower(), j.title.lower()))

    today = datetime.now(timezone.utc).strftime("%Y.%m.%d.")
    if first_run:
        subject = f"Zeneipari állások – első kör, {len(report.jobs)} hirdetés ({today})"
    elif new:
        subject = f"Zeneipari állások – {len(new)} új hirdetés ({today})"
    else:
        subject = f"Zeneipari állások – nincs új hirdetés ({today})"

    parts = [
        '<div style="font-family:Helvetica,Arial,sans-serif;max-width:640px;margin:0 auto;color:#111">',
        f'<h1 style="font-size:22px;margin:0 0 4px">Zeneipari állások</h1>',
        f'<div style="color:#666;font-size:13px;margin-bottom:20px">Heti összesítő · {today}</div>',
    ]

    if first_run:
        parts.append('<p style="font-size:14px;color:#444">Ez az első futás, ezért minden talált hirdetés lent szerepel. '
                     "Jövő héttől az újak külön, felül jelennek meg.</p>")
        parts.append(section("Talált hirdetések", report.jobs))
    else:
        parts.append(section(f"Új a múlt hét óta ({len(new)})", new) if new else
                     '<p style="font-size:14px">Ezen a héten nem jelent meg új zenei hirdetés.</p>')
        if old:
            parts.append(section(f"Továbbra is fent ({len(old)})", old, muted=True))

    parts.append(status_section(report))
    if report.errors:
        parts.append('<h3 style="font-size:14px;margin:24px 0 6px;color:#999">Hibák a futás közben</h3><ul style="color:#999;font-size:12px">'
                     + "".join(f"<li>{esc(e)}</li>" for e in report.errors) + "</ul>")
    parts.append('<p style="color:#aaa;font-size:11px;margin-top:28px">A figyelt cégeket és kulcsszavakat a repó sources.yaml fájljában tudod módosítani.</p></div>')
    return subject, "\n".join(parts)


def section(title: str, jobs: list[Job], muted: bool = False) -> str:
    color = "#888" if muted else "#111"
    rows = "".join(job_row(j) for j in jobs)
    return (f'<h2 style="font-size:16px;margin:24px 0 4px;color:{color}">{esc(title)}</h2>'
            f'<table style="width:100%;border-collapse:collapse">{rows}</table>')


def status_section(report: Report) -> str:
    labels = {
        "found": "✅ hirdetés van",
        "changed": "🔔 változott az állásoldal",
        "unchanged": "▫️ állásoldal üres",
        "no_page": "– nincs állásoldal",
        "linkedin_only": "– csak LinkedInen figyelve",
        "error": "⚠️ hiba",
    }
    order = ["changed", "found", "error", "unchanged", "no_page", "linkedin_only"]
    rows = []
    for st in sorted(report.statuses, key=lambda s: (order.index(s.status), s.name)):
        name = f'<a href="{esc(st.url)}" style="color:#444">{esc(st.name)}</a>' if st.url else esc(st.name)
        rows.append(f'<tr><td style="padding:3px 8px 3px 0;font-size:12px">{name}</td>'
                    f'<td style="padding:3px 0;font-size:12px;color:#777">{labels[st.status]}'
                    f'{(" – " + esc(st.detail)) if st.detail and st.status in ("error", "changed") else ""}</td></tr>')
    return ('<h3 style="font-size:14px;margin:28px 0 6px;color:#555">Figyelt cégoldalak</h3>'
            f'<table style="border-collapse:collapse">{"".join(rows)}</table>')


def send_email(subject: str, body: str) -> None:
    key = os.environ.get("BREVO_API_KEY")
    if not key:
        print("Nincs BREVO_API_KEY – csak preview.html készül.")
        return
    sender = os.environ["SENDER_EMAIL"]
    to = [{"email": a.strip()} for a in os.environ["RECIPIENTS"].split(",") if a.strip()]
    r = requests.post(
        "https://api.brevo.com/v3/smtp/email",
        headers={"api-key": key, "Content-Type": "application/json", "accept": "application/json"},
        json={"sender": {"email": sender, "name": "Zeneipari állásfigyelő"},
              "to": to, "subject": subject, "htmlContent": body},
        timeout=30,
    )
    if r.status_code >= 300:
        sys.exit(f"Brevo hiba: {r.status_code} {r.text}")
    print(f"E-mail elküldve {len(to)} címre.")


# ─────────────────────────────── futtatás ───────────────────────────────

def run(fetcher: Fetcher | None = None) -> tuple[str, str]:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    fetcher = fetcher or Fetcher()
    flt = Filter(cfg)
    state = load_state()
    first_run = not state["jobs"]
    report = Report()

    for company in cfg["companies"]:
        print(f"[cég] {company['name']}")
        scan_company(company, fetcher, flt, state, report)

    for kw in cfg.get("search_keywords", []):
        print(f"[LinkedIn] {kw}")
        try:
            scan_linkedin(kw, fetcher, flt, report)
        except Exception as e:  # noqa: BLE001
            report.errors.append(f"LinkedIn – „{kw}”: {short_err(e)}")
        for name, tmpl, href_re in PORTALS:
            print(f"[{name}] {kw}")
            try:
                scan_generic_portal(name, tmpl, href_re, kw, fetcher, flt, report)
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"{name} – „{kw}”: {short_err(e)}")

    for company in cfg["companies"]:
        if company.get("linkedin", True):
            print(f"[LinkedIn cég] {company['name']}")
            try:
                scan_linkedin(f'"{company["name"]}"', fetcher, flt, report, must_match_company=company["name"])
            except Exception as e:  # noqa: BLE001
                report.errors.append(f"LinkedIn – {company['name']}: {short_err(e)}")

    report.jobs = mark_new(report.jobs, state, first_run)
    subject, body = build_email(report, first_run)
    PREVIEW_PATH.write_text(f"<!-- {subject} -->\n{body}", encoding="utf-8")
    save_state(state)
    print(f"Kész: {len(report.jobs)} hirdetés, ebből új: {sum(j.is_new for j in report.jobs)}")
    return subject, body


if __name__ == "__main__":
    subj, html_body = run()
    send_email(subj, html_body)
