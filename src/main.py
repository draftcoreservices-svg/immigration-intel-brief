# src/main.py
import os
import re
import json
import hashlib
import pathlib
import html
from datetime import datetime
from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

import pytz
import requests

from .utils import load_settings
from .sources import fetch_all_sources
from .summarise import summarise_item
from .emailer import send_email

USER_AGENT = "DraftCore-Immigration-Intel-Brief/2.1"

CACHE_DIR = pathlib.Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)
STATE_PATH = CACHE_DIR / "state.json"


# -----------------------------
# Scheduling gate (DST-safe)
# -----------------------------
def should_send_now(tz_name: str, send_hour_local: int) -> bool:
    """
    GitHub Actions cron uses UTC. We run two crons (07:00 + 08:00 UTC) and gate here
    based on Europe/London local hour. This ensures the job sends once at the desired
    local time even across DST.
    """
    tz = pytz.timezone(tz_name)
    now_local = datetime.now(tz)
    return now_local.hour == int(send_hour_local) and now_local.minute < 20


# -----------------------------
# State (dedupe + update detect)
# -----------------------------
def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"items": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"items": {}}


def save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def normalise_url(url: str) -> str:
    u = (url or "").strip()
    u = u.split("#")[0]
    if u.endswith("/"):
        u = u[:-1]
    return u


def sha256_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", errors="ignore")).hexdigest()


# -----------------------------
# Text extraction helpers
# -----------------------------
def strip_html_basic(html_text: str) -> str:
    if not html_text:
        return ""
    t = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html_text)
    t = re.sub(r"(?is)<.*?>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def fetch_full_text(item: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """
    Returns (plain_text, last_modified_hint)
    - For GOV.UK pages: tries /api/content (best structured signal + updated_at).
    - Otherwise: fetches page and strips HTML.
    """
    url = normalise_url(item.get("url", ""))
    if not url:
        return "", None

    # GOV.UK structured API for richer extract + update date
    if url.startswith("https://www.gov.uk/"):
        try:
            path = url.replace("https://www.gov.uk", "")
            api_url = f"https://www.gov.uk/api/content{path}"
            r = requests.get(api_url, timeout=30, headers={"User-Agent": USER_AGENT})
            if r.status_code == 200:
                data = r.json()
                updated_at = data.get("updated_at") or data.get("public_updated_at")
                title = data.get("title") or item.get("title") or ""
                desc = data.get("description") or ""
                details = data.get("details") or {}

                body = ""
                if isinstance(details.get("body"), str):
                    body = details.get("body", "")
                elif isinstance(details.get("parts"), list):
                    parts_text = []
                    for p in details["parts"]:
                        parts_text.append(str(p.get("body", "")))
                    body = "\n".join(parts_text)

                plain = "\n".join([title, desc, strip_html_basic(body)]).strip()
                if plain:
                    return plain, updated_at
        except Exception:
            pass

    # Fallback generic fetch
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        lm = r.headers.get("Last-Modified") or r.headers.get("ETag")
        plain = strip_html_basic(r.text)
        return plain[:20000], lm
    except Exception:
        # last resort: title + summary
        title = item.get("title", "") or ""
        summary = item.get("summary", "") or ""
        return (title + "\n" + summary).strip(), None


# -----------------------------
# Tight relevance + critical alerts
# -----------------------------
CRITICAL_TRIGGERS = [
    "statement of changes",
    "immigration rules",
    "appendix",
    "fees",
    "fee increase",
    "fee changes",
    "comes into force",
    "effective from",
    "takes effect",
    "in force",
    "cpin",
    "country policy and information note",
    "caseworker guidance",
    "staff guidance",
    "sponsor licence suspended",
    "sponsor license suspended",
    "sponsor licence revoked",
    "sponsor license revoked",
    "civil penalty",
    "compliance visit",
]


def critical_score(text: str) -> int:
    t = (text or "").lower()
    score = 0
    for s in CRITICAL_TRIGGERS:
        if s in t:
            score += 2
    if "statement of changes" in t or "immigration rules" in t:
        score += 4
    return score


def filter_items(items: List[Dict[str, Any]], keywords: List[str]) -> List[Dict[str, Any]]:
    """
    Stage-1 filter: keep items that match keywords OR are clearly core immigration instruments.
    Then attach critical score and sort critical-first.
    """
    kws = [k.lower() for k in (keywords or [])]
    out: List[Dict[str, Any]] = []
    for it in items:
        hay = " ".join(
            [
                it.get("title", ""),
                it.get("summary", ""),
                it.get("source", ""),
                it.get("url", ""),
            ]
        ).lower()

        core_override = (
            "statement of changes" in hay
            or "immigration rules" in hay
            or "cpin" in hay
            or "country policy and information note" in hay
            or "caseworker guidance" in hay
            or "staff guidance" in hay
            or "sponsor guidance" in hay
        )

        if kws and not any(k in hay for k in kws) and not core_override:
            continue

        it["__critical_score"] = critical_score(hay)
        out.append(it)

    out.sort(key=lambda x: int(x.get("__critical_score", 0)), reverse=True)
    return out


def classify_section(source: str) -> str:
    s = source or ""
    if "GOV.UK" in s:
        return "GOV.UK"
    if "Parliament" in s:
        return "Parliament"
    if "legislation.gov.uk" in s:
        return "Legislation"
    if "Judiciary" in s or "UTIAC" in s:
        return "Courts & Tribunals"
    return "Other"


# -----------------------------
# Mailing list
# -----------------------------
def load_mailing_list() -> List[str]:
    """
    Reads config/mailing_list.txt (one email per line).
    Lines starting with # are ignored. Returns deduped list.
    """
    p = pathlib.Path("config/mailing_list.txt")
    if not p.exists():
        return []
    emails: List[str] = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#"):
            continue
        emails.append(ln)

    seen: Set[str] = set()
    out: List[str] = []
    for e in emails:
        k = e.lower()
        if k in seen:
            continue
        seen.add(k)
        out.append(e)
    return out


# -----------------------------
# Email rendering
# -----------------------------
def build_subject(tz_name: str) -> str:
    tz = pytz.timezone(tz_name)
    d = datetime.now(tz).strftime("%Y-%m-%d")
    return f"Immigration Intelligence Brief — {d}"


def ai_to_html(ai_text: str) -> str:
    lines = [ln.strip() for ln in (ai_text or "").splitlines() if ln.strip()]
    bullets = [ln for ln in lines if ln.startswith(("-", "•", "*"))]
    if bullets:
        lis = "".join(
            f"<li style='margin:0 0 6px 0;'>{html.escape(ln.lstrip('-•* ').strip())}</li>"
            for ln in bullets[:12]
        )
        return (
            "<ul style='margin:10px 0 0 18px;color:#0F172A;font-size:13px;line-height:1.45;'>"
            f"{lis}</ul>"
        )
    text = html.escape(" ".join(lines)[:1200])
    return f"<div style='margin-top:10px;color:#0F172A;font-size:13px;line-height:1.5;'>{text}</div>"


def render_email_html(
    new_items: List[Dict[str, Any]],
    updated_items: List[Dict[str, Any]],
    tz_name: str,
) -> str:
    subject = build_subject(tz_name)

    combined = [(int(it.get("__critical_score", 0)), it) for it in (updated_items + new_items)]
    combined.sort(key=lambda x: x[0], reverse=True)
    top3 = [it for _, it in combined[:3]]

    critical_items = [
        it for it in (updated_items + new_items) if int(it.get("__critical_score", 0)) >= 4
    ][:6]

    def badge_style(label: str) -> Tuple[str, str, str]:
        if label == "NEW":
            return ("#EFF6FF", "#BFDBFE", "#1D4ED8")
        return ("#FFF7ED", "#FED7AA", "#9A3412")

    def card(it: Dict[str, Any], badge: str, prev: Optional[str] = None) -> str:
        title = html.escape(it.get("title", "(untitled)"))
        url = html.escape(it.get("url", ""))
        ai_summary = it.get("ai_summary", "")

        prev_html = ""
        if prev:
            prev_html = (
                "<div style='color:#64748B;font-size:12px;margin-top:6px;'>"
                f"Previously covered: <b>{html.escape(prev)}</b>"
                "</div>"
            )

        bg, border, color = badge_style(badge)

        return f"""
<div style="margin:0 0 14px 0;padding:14px;border:1px solid #E5E7EB;border-radius:14px;background:#FFFFFF;">
  <div style="display:flex;gap:10px;align-items:center;margin-bottom:6px;">
    <span style="font-size:11px;font-weight:800;padding:4px 10px;border-radius:999px;background:{bg};border:1px solid {border};color:{color};letter-spacing:0.2px;">
      {html.escape(badge)}
    </span>
    <div style="font-weight:800;font-size:14px;color:#0F172A;">
      <a href="{url}" style="color:#0F172A;text-decoration:none;">{title}</a>
    </div>
  </div>
  {prev_html}
  {ai_to_html(ai_summary)}
</div>
"""

    # Bucket by section
    buckets_new: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for it in new_items:
        buckets_new[classify_section(it.get("source", ""))].append(it)

    buckets_upd: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for it in updated_items:
        buckets_upd[classify_section(it.get("source", ""))].append(it)

    parts: List[str] = []

    parts.append(
        f"<div style='margin:10px 0 14px 0;color:#334155;font-size:13px;'>"
        f"<b>Updated:</b> {len(updated_items)} &nbsp;|&nbsp; <b>New:</b> {len(new_items)}"
        f"</div>"
    )

    # Critical alerts banner
    if critical_items:
        lis = []
        for it in critical_items:
            title = html.escape(it.get("title", "(untitled)"))
            url = html.escape(it.get("url", ""))
            badge = "UPDATED" if it in updated_items else "NEW"
            lis.append(
                f"<li style='margin:0 0 6px 0;'>"
                f"<span style='font-size:11px;font-weight:900;padding:2px 8px;border-radius:999px;border:1px solid #FECACA;background:#FFFFFF;margin-right:6px;color:#991B1B;'>{badge}</span>"
                f"<a href='{url}' style='color:#991B1B;text-decoration:none;font-weight:800;'>{title}</a>"
                f"</li>"
            )
        parts.append(
            "<div style='margin:14px 0 16px 0;padding:14px;border:1px solid #FECACA;border-radius:14px;background:#FEF2F2;'>"
            "<div style='font-weight:900;font-size:14px;color:#991B1B;margin:0 0 8px 0;'>🚨 Critical alerts</div>"
            f"<ul style='margin:0;padding-left:18px;color:#991B1B;font-size:13px;line-height:1.45;'>{''.join(lis)}</ul>"
            "</div>"
        )

    # TL;DR
    if top3:
        lis = []
        for it in top3:
            badge = "UPDATED" if it in updated_items else "NEW"
            title = html.escape(it.get("title", "(untitled)"))
            url = html.escape(it.get("url", ""))
            lis.append(
                f"<li style='margin:0 0 6px 0;'>"
                f"<span style='font-size:11px;font-weight:800;padding:2px 8px;border-radius:999px;border:1px solid #E5E7EB;background:#FFFFFF;margin-right:6px;color:#0F172A;'>{badge}</span>"
                f"<a href='{url}' style='color:#2563EB;text-decoration:none;'>{title}</a>"
                f"</li>"
            )
        parts.append(
            "<div style='margin:14px 0 16px 0;padding:14px;border:1px solid #E5E7EB;border-radius:14px;background:#F9FAFB;'>"
            "<div style='font-weight:800;font-size:14px;color:#0F172A;margin:0 0 8px 0;'>TL;DR — Top highlights</div>"
            f"<ul style='margin:0;padding-left:18px;color:#0F172A;font-size:13px;line-height:1.45;'>{''.join(lis)}</ul>"
            "</div>"
        )

    # Updated section
    if updated_items:
        parts.append("<div style='margin:18px 0 10px 0;border-top:1px solid #E5E7EB;'></div>")
        parts.append("<h2 style='margin:14px 0 10px 0;font-size:16px;color:#0F172A;'>Updated since last brief</h2>")
        for section in ["GOV.UK", "Parliament", "Legislation", "Courts & Tribunals", "Other"]:
            if not buckets_upd.get(section):
                continue
            parts.append(
                f"<h3 style='margin:14px 0 8px 0;font-size:14px;color:#0F172A;'>{html.escape(section)}</h3>"
            )
            for it in buckets_upd[section][:12]:
                parts.append(card(it, "UPDATED", prev=it.get("previously_covered")))

    # New section
    if new_items:
        parts.append("<div style='margin:18px 0 10px 0;border-top:1px solid #E5E7EB;'></div>")
        parts.append("<h2 style='margin:14px 0 10px 0;font-size:16px;color:#0F172A;'>New items</h2>")
        for section in ["GOV.UK", "Parliament", "Legislation", "Courts & Tribunals", "Other"]:
            if not buckets_new.get(section):
                continue
            parts.append(
                f"<h3 style='margin:14px 0 8px 0;font-size:14px;color:#0F172A;'>{html.escape(section)}</h3>"
            )
            for it in buckets_new[section][:12]:
                parts.append(card(it, "NEW"))

    if not new_items and not updated_items:
        parts.append(
            "<div style='margin-top:12px;padding:14px;border:1px solid #E5E7EB;border-radius:14px;background:#FFFFFF;color:#334155;'>"
            "<b>No material updates detected</b> for this run."
            "</div>"
        )

    inner = "\n".join(parts)

    return f"""
<html>
  <body style="margin:0;padding:0;background:#F1F5F9;font-family:Arial, sans-serif;color:#0F172A;">
    <div style="max-width:820px;margin:0 auto;padding:22px;">
      <div style="padding:18px;border:1px solid #E5E7EB;border-radius:16px;background:#FFFFFF;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:14px;">
          <div>
            <div style="font-size:12px;color:#64748B;font-weight:700;letter-spacing:0.3px;">
              DraftCore • by Rushi Trivedi
            </div>
            <h1 style="margin:6px 0 0 0;font-size:22px;line-height:1.2;">{html.escape(subject)}</h1>
          </div>
        </div>

        <div style="margin-top:10px;color:#475569;font-size:12.5px;line-height:1.45;">
          Information only — <b>not legal advice</b>. Always verify against the primary source before relying on it in practice.
        </div>

        <div style="margin-top:14px;">
          {inner}
        </div>

        <div style="margin-top:18px;border-top:1px solid #E5E7EB;padding-top:12px;color:#64748B;font-size:12px;">
          Sources monitored: GOV.UK (targeted), Parliament, legislation.gov.uk, Courts/Tribunals (where available).
        </div>
      </div>
    </div>
  </body>
</html>
"""


# -----------------------------
# Main pipeline
# -----------------------------
def main() -> None:
    settings = load_settings("config/settings.yaml")
    tz_name = settings.get("timezone", "Europe/London")
    send_hour_local = int(settings.get("send_hour_local", settings.get("send_hour_local", 7)))
    always_send = bool(settings.get("always_send", True))

    # Gate only for scheduled runs; manual dispatch should always run
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        if not should_send_now(tz_name, send_hour_local):
            return

    state = load_state()
    state_items: Dict[str, Any] = state.get("items", {})

    raw_items = fetch_all_sources(settings)
    filtered = filter_items(raw_items, settings.get("keywords", []))

    tz = pytz.timezone(tz_name)
    now_local = datetime.now(tz).strftime("%Y-%m-%d")

    new_items: List[Dict[str, Any]] = []
    updated_items: List[Dict[str, Any]] = []

    MAX_CANDIDATES = int(settings.get("max_candidates", 25))
    processed = 0

    for it in filtered:
        if processed >= MAX_CANDIDATES:
            break

        url = normalise_url(it.get("url", ""))
        if not url:
            continue

        prev = state_items.get(url)
        prior_seen = prev.get("last_seen") if prev else None
        prior_hash = prev.get("last_content_hash") if prev else None
        prior_lm = prev.get("last_modified") if prev else None

        full_text, lm_hint = fetch_full_text(it)
        content_hash = sha256_text(full_text)

        if prev is None:
            status = "NEW"
        else:
            if content_hash != prior_hash:
                status = "UPDATED"
            elif lm_hint and (prior_lm is None or str(lm_hint) != str(prior_lm)):
                status = "UPDATED"
            else:
                status = "SKIP"

        if status == "SKIP":
            # Touch last_seen so it doesn’t look stale in state
            try:
                prev["last_seen"] = now_local
            except Exception:
                pass
            continue

        ai = summarise_item(title=it.get("title", ""), content=full_text, is_update=(status == "UPDATED"))

        out = dict(it)
        out["ai_summary"] = ai

        if status == "UPDATED" and prior_seen:
            out["previously_covered"] = prior_seen

        # Update state record
        if prev is None:
            state_items[url] = {
                "first_seen": now_local,
                "last_seen": now_local,
                "last_modified": lm_hint or it.get("published") or None,
                "last_content_hash": content_hash,
                "last_title": it.get("title", ""),
                "last_source": it.get("source", ""),
            }
        else:
            prev["last_seen"] = now_local
            prev["last_modified"] = lm_hint or it.get("published") or prev.get("last_modified")
            prev["last_content_hash"] = content_hash
            prev["last_title"] = it.get("title", prev.get("last_title", ""))
            prev["last_source"] = it.get("source", prev.get("last_source", ""))

        if status == "NEW":
            new_items.append(out)
        else:
            updated_items.append(out)

        processed += 1

    state["items"] = state_items
    save_state(state)

    if not new_items and not updated_items and not always_send:
        return

    subject = build_subject(tz_name)
    html_email = render_email_html(new_items, updated_items, tz_name)

    # Recipients: TO_EMAIL + config/mailing_list.txt
    recipients: List[str] = []
    env_to = os.environ.get("TO_EMAIL", "").strip()
    if env_to:
        recipients.append(env_to)
    recipients.extend(load_mailing_list())

    # Final dedupe
    seen: Set[str] = set()
    final: List[str] = []
    for r in recipients:
        k = r.lower()
        if k in seen:
            continue
        seen.add(k)
        final.append(r)

    send_email(
        smtp_host=os.environ["SMTP_HOST"],
        smtp_port=int(os.environ["SMTP_PORT"]),
        smtp_user=os.environ["SMTP_USER"],
        smtp_pass=os.environ["SMTP_PASS"],
        from_email=os.environ["FROM_EMAIL"],
        to_email=final,
        subject=subject,
        html=html_email,
    )


if __name__ == "__main__":
    main()
