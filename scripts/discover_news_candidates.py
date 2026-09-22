#!/usr/bin/env python3
"""Discover broad/in-depth wildlife trafficking reporting for editorial review.

This script does NOT publish directly to news.json. It maintains a conservative
news-candidates.json queue so the public collection keeps its editorial standard:
networks, markets, drivers, governance, digital trade, laundering, conservation
and impacts. Brief isolated seizure/arrest stories are filtered out.

Discovery source: GDELT DOC 2.0 API (no API key required), queried with multilingual
search phrases. Candidate pages are then lightly inspected for canonical URL,
description, author and publication date when accessible.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import time
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

GDELT_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
UA = (
    "Mozilla/5.0 (compatible; WildlifeTraffickingObservatoryNews/1.0; "
    "+https://github.com/acarvalho-wcs/wildlife-trafficking-news)"
)

QUERIES = [
    ("en", "wildlife trafficking"),
    ("en", "illegal wildlife trade"),
    ("en", "wildlife crime network"),
    ("en", "online wildlife trade"),
    ("pt", "tráfico de animais silvestres"),
    ("pt", "tráfico de fauna"),
    ("pt", "comércio ilegal de animais silvestres"),
    ("es", "tráfico de fauna silvestre"),
    ("es", "comercio ilegal de fauna"),
    ("es", "tráfico de animales silvestres"),
    ("fr", "trafic d'animaux sauvages"),
    ("fr", "commerce illégal d'espèces sauvages"),
    ("id", "perdagangan satwa liar ilegal"),
    ("id", "penyelundupan satwa liar"),
    ("ms", "penyeludupan hidupan liar"),
    ("vi", "buôn bán động vật hoang dã trái phép"),
    ("th", "ค้าสัตว์ป่าผิดกฎหมาย"),
    ("sw", "biashara haramu ya wanyamapori"),
    ("zh", "野生动物 非法 贸易"),
    ("ar", "الاتجار غير المشروع بالحياة البرية"),
    ("hi", "वन्यजीव तस्करी"),
    ("ru", "незаконная торговля дикими животными"),
]

BROAD_TERMS = [
    "investig", "report", "analysis", "network", "market", "demand", "route",
    "organized crime", "organised crime", "syndicate", "launder", "corrupt",
    "governance", "policy", "prosecution", "court", "digital", "online", "social media",
    "tourism", "supply chain", "trade chain", "smuggling route", "crime network",
    "tráfico", "trafico", "comercio", "mercado", "redes", "rutas", "investigación",
    "lavado", "corrupción", "gobernanza", "comércio", "mercado", "redes", "rotas",
    "lavagem", "corrupção", "perdagangan", "jaringan", "pasar", "trafic", "commerce",
]

SEIZURE_TERMS = [
    "seized", "seizure", "confiscated", "arrested", "detained", "intercepted",
    "apreende", "apreensão", "apreendido", "preso", "detido",
    "incauta", "incautación", "decomisa", "decomiso", "detenido",
    "saisie", "saisi", "arrêté", "ditangkap", "diamankan", "disita",
]

WILDLIFE_TERMS = [
    "wildlife", "fauna", "animal", "species", "satwa", "hidupan liar", "animaux sauvages",
    "espèces sauvages", "vida silvestre", "fauna silvestre", "野生动物", "الحياة البرية",
    "pangolin", "tiger", "elephant", "ivory", "rhino", "parrot", "macaw", "shark",
    "ray", "reptile", "bird", "primate", "turtle", "tortoise", "orangutan",
]

TRADE_TERMS = [
    "traffick", "smuggl", "illegal trade", "illicit trade", "crime", "contraband",
    "tráfico", "trafico", "contrabando", "comercio ilegal", "comércio ilegal",
    "perdagangan ilegal", "penyelundupan", "trafic", "commerce illégal",
    "非法 贸易", "走私", "الاتجار", "تهريب", "तस्करी", "незаконная торговля",
]


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def clean_text(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def normalize_url(url: str) -> str:
    try:
        p = urlparse(url)
        host = p.netloc.lower().replace("www.", "")
        path = re.sub(r"/+$", "", p.path or "/")
        return urlunparse((p.scheme.lower() or "https", host, path, "", "", ""))
    except Exception:
        return url.strip()


def title_key(title: str) -> str:
    t = clean_text(title).lower()
    t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:220]


def safe_id(url: str, title: str) -> str:
    raw = normalize_url(url) + "|" + title_key(title)
    return "candidate-" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def relevant(text: str) -> bool:
    low = text.lower()
    has_wildlife = any(x in low for x in WILDLIFE_TERMS)
    has_trade = any(x in low for x in TRADE_TERMS)
    return has_wildlife and has_trade


def broad_score(title: str, description: str) -> int:
    low = f"{title} {description}".lower()
    score = 0
    score += sum(2 for x in BROAD_TERMS if x in low)
    if any(x in title.lower() for x in BROAD_TERMS):
        score += 3
    if len(description) >= 180:
        score += 2
    if len(description) >= 320:
        score += 1

    seizure = any(x in title.lower() for x in SEIZURE_TERMS)
    broader = any(x in low for x in BROAD_TERMS)
    if seizure and not broader:
        score -= 8
    elif seizure:
        score -= 2
    return score


def gdelt_query(session: requests.Session, query: str, days: int, maxrecords: int) -> list[dict]:
    params = {
        "query": query,
        "mode": "ArtList",
        "maxrecords": str(maxrecords),
        "format": "json",
        "sort": "DateDesc",
        "timespan": f"{days}d",
    }
    url = GDELT_ENDPOINT + "?" + urlencode(params)
    try:
        r = session.get(url, timeout=30)
        r.raise_for_status()
        payload = r.json()
        return payload.get("articles", []) or []
    except Exception:
        return []


def first_meta(soup: BeautifulSoup, selectors: list[tuple[str, dict, str]]) -> str:
    for tag, attrs, field in selectors:
        node = soup.find(tag, attrs=attrs)
        if node and node.get(field):
            return clean_text(node.get(field))
    return ""


def inspect_article(session: requests.Session, url: str) -> dict:
    out = {
        "canonical_url": normalize_url(url),
        "description": "",
        "author": "",
        "publication_date": None,
        "page_title": "",
        "inspection_status": "UNAVAILABLE",
    }
    try:
        r = session.get(url, timeout=20, allow_redirects=True)
        if r.status_code >= 400:
            out["inspection_status"] = f"HTTP_{r.status_code}"
            return out
        ctype = (r.headers.get("content-type") or "").lower()
        if "html" not in ctype and "<html" not in r.text[:500].lower():
            out["inspection_status"] = "NON_HTML"
            return out
        soup = BeautifulSoup(r.text, "html.parser")
        canonical = soup.find("link", rel=lambda v: v and "canonical" in v)
        if canonical and canonical.get("href"):
            out["canonical_url"] = normalize_url(urljoin(r.url, canonical["href"]))
        else:
            out["canonical_url"] = normalize_url(r.url)

        out["description"] = first_meta(soup, [
            ("meta", {"property": "og:description"}, "content"),
            ("meta", {"name": "description"}, "content"),
            ("meta", {"name": "twitter:description"}, "content"),
        ])
        out["page_title"] = first_meta(soup, [
            ("meta", {"property": "og:title"}, "content"),
            ("meta", {"name": "twitter:title"}, "content"),
        ]) or clean_text(soup.title.string if soup.title and soup.title.string else "")
        out["author"] = first_meta(soup, [
            ("meta", {"name": "author"}, "content"),
            ("meta", {"property": "article:author"}, "content"),
        ])
        date_raw = first_meta(soup, [
            ("meta", {"property": "article:published_time"}, "content"),
            ("meta", {"name": "date"}, "content"),
            ("meta", {"name": "pubdate"}, "content"),
            ("meta", {"itemprop": "datePublished"}, "content"),
        ])
        if date_raw:
            m = re.search(r"(20\d{2}-\d{2}-\d{2})", date_raw)
            if m:
                out["publication_date"] = m.group(1)
        out["inspection_status"] = "OK"
        return out
    except Exception as exc:
        out["inspection_status"] = f"ERROR_{exc.__class__.__name__}"
        return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--news", default="news.json")
    ap.add_argument("--candidates", default="news-candidates.json")
    ap.add_argument("--report", default="news-discovery-report.json")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--max-per-query", type=int, default=100)
    ap.add_argument("--max-new", type=int, default=30)
    ap.add_argument("--min-score", type=int, default=3)
    ap.add_argument("--sleep", type=float, default=0.15)
    args = ap.parse_args()

    news = json.loads(Path(args.news).read_text(encoding="utf-8"))
    published = news.get("news", news if isinstance(news, list) else [])

    candidate_path = Path(args.candidates)
    if candidate_path.exists():
        stored = json.loads(candidate_path.read_text(encoding="utf-8"))
        existing_candidates = stored.get("candidates", [])
    else:
        stored = {"schema_version": "1.0", "candidates": []}
        existing_candidates = []

    published_urls = {normalize_url(x.get("source_url", "")) for x in published if x.get("source_url")}
    published_titles = {title_key(x.get("original_title") or x.get("title_en") or x.get("title_pt") or "") for x in published}
    candidate_urls = {normalize_url(x.get("source_url", "")) for x in existing_candidates if x.get("source_url")}
    candidate_titles = {title_key(x.get("original_title") or "") for x in existing_candidates}

    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept-Language": "en-US,en;q=0.9,pt-BR;q=0.8,es;q=0.7,id;q=0.6,fr;q=0.5",
    })

    discovered = []
    seen_run_urls = set()
    seen_run_titles = set()
    queries_run = 0

    for query_lang, query in QUERIES:
        articles = gdelt_query(session, query, args.days, args.max_per_query)
        queries_run += 1
        for item in articles:
            raw_url = clean_text(item.get("url"))
            raw_title = clean_text(item.get("title"))
            if not raw_url or not raw_title:
                continue

            nurl = normalize_url(raw_url)
            tkey = title_key(raw_title)
            if nurl in published_urls or nurl in candidate_urls or nurl in seen_run_urls:
                continue
            if tkey in published_titles or tkey in candidate_titles or tkey in seen_run_titles:
                continue

            inspection = inspect_article(session, raw_url)
            final_url = inspection.get("canonical_url") or nurl
            final_norm = normalize_url(final_url)
            title = inspection.get("page_title") or raw_title
            description = inspection.get("description") or ""
            combined = f"{title} {description} {query}"

            if not relevant(combined):
                continue

            score = broad_score(title, description)
            if score < args.min_score:
                continue

            seendate = clean_text(item.get("seendate"))
            gdelt_date = None
            m = re.match(r"(20\d{2})(\d{2})(\d{2})", seendate)
            if m:
                gdelt_date = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

            source_domain = clean_text(item.get("domain")) or urlparse(final_url).netloc.replace("www.", "")
            language = clean_text(item.get("language")) or query_lang

            cand = {
                "id": safe_id(final_url, title),
                "status": "CANDIDATE_UNREVIEWED",
                "publication_date": inspection.get("publication_date") or gdelt_date,
                "outlet_domain": source_domain,
                "authors_raw": [inspection["author"]] if inspection.get("author") else [],
                "original_language_hint": language,
                "original_title": title,
                "source_url": final_url,
                "source_description": description or None,
                "discovery_query": query,
                "discovery_query_language": query_lang,
                "broad_reporting_score": score,
                "inspection_status": inspection.get("inspection_status"),
                "discovered_at": utc_now(),
                "editorial_note": "Requires PT/EN/ES enrichment and human/AI editorial validation before publication in news.json.",
            }
            discovered.append(cand)
            seen_run_urls.add(final_norm)
            seen_run_titles.add(title_key(title))

            if args.sleep:
                time.sleep(args.sleep)

    discovered.sort(key=lambda x: (
        x.get("publication_date") or "",
        x.get("broad_reporting_score") or 0,
    ), reverse=True)
    new_items = discovered[: args.max_new]

    combined = existing_candidates + new_items
    # Keep a bounded editorial queue, newest/highest-score first.
    combined.sort(key=lambda x: (
        x.get("publication_date") or "",
        x.get("broad_reporting_score") or 0,
        x.get("discovered_at") or "",
    ), reverse=True)
    combined = combined[:250]

    out = {
        "schema_version": "1.0",
        "updated_at": utc_now(),
        "editorial_criterion": news.get("editorial_criterion"),
        "note": "Automated multilingual discovery queue. Items are not public until curated into news.json.",
        "count": len(combined),
        "candidates": combined,
    }
    candidate_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report = {
        "generated_at": utc_now(),
        "lookback_days": args.days,
        "queries_run": queries_run,
        "published_count": len(published),
        "existing_candidate_count": len(existing_candidates),
        "new_candidates_added": len(new_items),
        "resulting_candidate_count": len(combined),
        "languages_queried": sorted({lang for lang, _ in QUERIES}),
        "new_candidate_ids": [x["id"] for x in new_items],
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
