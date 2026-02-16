import pathlib
from datetime import datetime, timedelta
from typing import Any, Dict, List
import requests
import feedparser
import yaml

USER_AGENT = "DraftCore-Immigration-Intel-Brief/2.0"

def _load_targets() -> Dict[str, Any]:
    p = pathlib.Path("config/targets.yaml")
    if not p.exists():
        return {"govuk_queries": []}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {"govuk_queries": []}

def _since_days(days: int) -> str:
    return (datetime.utcnow() - timedelta(days=days)).date().isoformat()

def fetch_govuk_targeted(days: int = 14) -> List[Dict[str, Any]]:
    targets = _load_targets().get("govuk_queries", [])
    since = _since_days(days)
    out: List[Dict[str, Any]] = []

    for t in targets:
        q = t.get("q", "")
        doc_types = t.get("document_types", "")
        params = {
            "q": q,
            "count": 40,
            "filter_public_timestamp": f"from:{since}",
        }
        if doc_types:
            params["filter_document_type"] = doc_types

        url = "https://www.gov.uk/api/search.json"
        r = requests.get(url, params=params, timeout=30, headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            continue

        data = r.json()
        for res in data.get("results", []):
            link = res.get("link")
            if not link:
                continue
            out.append(
                {
                    "source": "GOV.UK",
                    "title": res.get("title") or "",
                    "summary": res.get("description") or "",
                    "url": "https://www.gov.uk" + link if link.startswith("/") else link,
                    "published": res.get("public_timestamp") or res.get("public_updated_at"),
                }
            )

    return out

def fetch_parliament_rss() -> List[Dict[str, Any]]:
    feeds = [
        "https://bills.parliament.uk/rss/allbills.rss",
        "https://www.parliament.uk/business/publications/business-papers/commons/ministerial-statements/rss/",
    ]
    out: List[Dict[str, Any]] = []
    for f in feeds:
        d = feedparser.parse(f)
        for e in d.entries[:30]:
            out.append(
                {
                    "source": "Parliament",
                    "title": getattr(e, "title", "") or "",
                    "summary": getattr(e, "summary", "") or "",
                    "url": getattr(e, "link", "") or "",
                    "published": getattr(e, "published", "") or "",
                }
            )
    return out

def fetch_legislation_atom() -> List[Dict[str, Any]]:
    feed = "https://www.legislation.gov.uk/uksi/atom.xml"
    out: List[Dict[str, Any]] = []
    d = feedparser.parse(feed)
    for e in d.entries[:40]:
        out.append(
            {
                "source": "legislation.gov.uk",
                "title": getattr(e, "title", "") or "",
                "summary": getattr(e, "summary", "") or "",
                "url": getattr(e, "link", "") or "",
                "published": getattr(e, "published", "") or "",
            }
        )
    return out

def fetch_utiac_rss() -> List[Dict[str, Any]]:
    feed = "https://www.judiciary.uk/tribunals/upper-tribunal-immigration-and-asylum-chamber/feed/"
    out: List[Dict[str, Any]] = []
    d = feedparser.parse(feed)
    for e in d.entries[:40]:
        out.append(
            {
                "source": "Judiciary (UTIAC)",
                "title": getattr(e, "title", "") or "",
                "summary": getattr(e, "summary", "") or "",
                "url": getattr(e, "link", "") or "",
                "published": getattr(e, "published", "") or "",
            }
        )
    return out

def fetch_all_sources(settings: Dict[str, Any]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    items.extend(fetch_govuk_targeted(days=14))
    items.extend(fetch_parliament_rss())
    items.extend(fetch_legislation_atom())
    items.extend(fetch_utiac_rss())
    return items
