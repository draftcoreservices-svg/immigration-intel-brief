import feedparser
import requests
from datetime import datetime, timedelta

def _parse_feed(url: str, source: str):
    feed = feedparser.parse(url)
    items = []
    for e in feed.entries[:60]:
        items.append({
            "source": source,
            "title": (e.get("title", "") or "").strip(),
            "url": (e.get("link", "") or "").strip(),
            "published": e.get("published", "") or e.get("updated", "") or "",
            "summary": (e.get("summary", "") or "").strip(),
        })
    return items

def _govuk_search(org_slug: str, days: int = 3):
    since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
    url = "https://www.gov.uk/api/search.json"
    params = {
        "filter_organisations": org_slug,
        "order": "-public_timestamp",
        "count": 50,
        "filter_public_timestamp": f"from:{since}",
        # Best-effort: limit to commonly useful formats for practitioners
        "filter_document_type": "guidance,news_story,policy_paper,consultation,publication,statistical_data_set",
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    items = []
    for res in data.get("results", []):
        link = res.get("link", "")
        if link and not link.startswith("http"):
            link = "https://www.gov.uk" + link
        items.append({
            "source": f"GOV.UK ({org_slug})",
            "title": (res.get("title", "") or "").strip(),
            "url": (link or "").strip(),
            "published": (res.get("public_timestamp", "") or "").strip(),
            "summary": (res.get("description", "") or "").strip(),
        })
    return items

def fetch_all_sources(settings: dict):
    items = []

    for org in settings["govuk"]["organisations"]:
        items.extend(_govuk_search(org_slug=org, days=3))

    items.extend(_parse_feed(settings["parliament"]["rss_all_bills"], "Parliament (Bills)"))
    items.extend(_parse_feed(settings["legislation"]["atom_new_legislation"], "legislation.gov.uk (New)"))
    items.extend(_parse_feed(settings["judiciary"]["rss_utiac"], "Judiciary (UTIAC)"))

    return items
