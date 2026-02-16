import os
import json
import hashlib
import html as html_escape
import pathlib
import re
from datetime import datetime
from collections import defaultdict
from typing import Dict, Any, List, Tuple, Optional

import pytz
import requests

from .sources import fetch_all_sources
from .emailer import send_email
from .utils import load_settings
from .summarise import summarise_item

CACHE_DIR = pathlib.Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)
STATE_PATH = CACHE_DIR / "state.json"

USER_AGENT = "DraftCore-Immigration-Intel-Brief/1.1"


# ----------------------------
# Time gating (DST-safe)
# ----------------------------
def should_send_now(tz_name: str, send_hour_local: int) -> bool:
    tz = pytz.timezone(tz_name)
    now_local = datetime.now(tz)
    # small window so the double-UTC cron doesn't send twice
    return now_local.hour == send_hour_local and now_local.minute < 20


# ----------------------------
# State storage
# ----------------------------
def load_state() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return {"items": {}}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"items": {}}


def save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


# ----------------------------
# Helpers
# ----------------------------
def normalise_url(url: str) -> str:
    u = (url or "").split("#")[0].strip()
    if u.endswith("/"):
        u = u[:-1]
    return u


def sha256_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", errors="ignore")).hexdigest()


def strip_html_basic(html_text: str) -> str:
    if not html_text:
        return ""
    t = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html_text)
    t = re.sub(r"(?is)<.*?>", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


# ----------------------------
# Relevance scoring (reduce GOV.UK noise)
# ----------------------------
STRONG_TERMS = [
    "immigration", "ukvi", "visa", "visas", "evisa", "e-visa", "eta",
    "asylum", "refugee", "nationality", "citizenship", "border",
    "immigration rules", "statement of changes", "leave to remain", "ilr",
    "indefinite leave", "settlement", "sponsor licence", "sponsor license",
    "skilled worker", "student visa", "family visa", "human rights",
    "deportation", "removal", "immigration enforcement",
    "modern slavery", "trafficking", "national referral mechanism", "nrm",
    "utiac", "upper tribunal", "first-tier tribunal",
]

MEDIUM_TERMS = [
    "fees", "guidance", "policy", "consultation", "sponsor", "compliance",
    "right to work", "right to rent", "civil penalty", "sanctions",
]

EXCLUDE_PHRASES = [
    "police custody",
    "pre-charge bail",
    "strip searches",
]

def relevance_score(text: str) -> int:
    t = (text or "").lower()
    score = 0
    for term in STRONG_TERMS:
        if term in t:
            score += 3
    for term in MEDIUM_TERMS:
        if term in t:
            score += 1
    for phrase in EXCLUDE_PHRASES:
        if phrase in t:
            score -= 3
    return score


def fetch_full_text(item: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """
    Returns (plain_text, last_modified_hint).
    Uses GOV.UK Content API when possible; falls back to page fetch.
    """
    url = normalise_url(item.get("url", ""))
    if not url:
        return "", None

    # GOV.UK Content API
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

    # Fallback: fetch HTML page
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        lm = r.headers.get("Last-Modified") or r.headers.get("ETag")
        plain = strip_html_basic(r.text)
        return plain[:20000], lm
    except Exception:
        summary = item.get("summary", "") or ""
        title = item.get("title", "") or ""
        return (title + "\n" + summary).strip(), None


def filter_items(items: List[Dict[str, Any]], keywords: List[str]) -> List[Dict[str, Any]]:
    # Stage 1: keyword prefilter (fast)
    kws = [k.lower() for k in (keywords or [])]
    out: List[Dict[str, Any]] = []

    for it in items:
        hay = " ".join(
            [it.get("title", ""), it.get("summary", ""), it.get("source", ""), it.get("url", "")]
        ).lower()

        if kws and not any(k in hay for k in kws):
            continue

        # Stage 2: score threshold
        score = relevance_score(hay)
        if score >= 3:
            it["__relevance_score"] = score
            out.append(it)

    out.sort(key=lambda x: x.get("__relevance_score", 0), reverse=True)
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


def build_subject(tz_name: str) -> str:
    tz = pytz.timezone(tz_name)
    d = datetime.now(tz).strftime("%Y-%m-%d")
    return f"Immigration Intelligence Brief — {d}"


def _ai_to_html(ai_text: str) -> str:
    """
    Converts AI output into nice HTML:
    - if it contains bullet lines, render as <ul>
    - otherwise render as paragraphs
    """
    lines = [ln.strip() for ln in (ai_text or "").splitlines() if ln.strip()]
    bullets = [ln for ln in lines if ln.startswith(("-", "•", "*"))]
    if bullets:
        lis = "".join(
            f"<li style='margin:0 0 6px 0;'>{html_escape.escape(ln.lstrip('-•* ').strip())}</li>"
            for ln in bullets[:12]
        )
        return f"<ul style='margin:10px 0 0 18px;color:#0F172A;font-size:13px;line-height:1.45;'>{lis}</ul>"
    # fallback
    text = html_escape.escape(" ".join(lines)[:1200])
    return f"<div style='margin-top:10px;color:#0F172A;font-size:13px;line-height:1.5;'>{text}</div>"


def render_email_html(items_new: List[Dict[str, Any]], items_updated: List[Dict[str, Any]], tz_name: str) -> str:
    subject = build_subject(tz_name)

    # TL;DR top 3
    combined = []
    for it in (items_updated + items_new):
        score = it.get("__relevance_score")
        if score is None:
            score = relevance_score(" ".join([it.get("title",""), it.get("summary",""), it.get("source",""), it.get("url","")]))
        combined.append((int(score), it))
    combined.sort(key=lambda x: x[0], reverse=True)
    top3 = [it for _, it in combined[:3]]

    def card(it: Dict[str, Any], badge: str, prev: Optional[str] = None) -> str:
        title = html_escape.escape(it.get("title", "(untitled)"))
        url = html_escape.escape(it.get("url", ""))
        ai_summary = it.get("ai_summary", "")

        prev_html = ""
        if prev:
            prev_html = (
                "<div style='color:#64748B;font-size:12px;margin-top:6px;'>"
                f"Previously covered: <b>{html_escape.escape(prev)}</b>"
                "</div>"
            )

        badge_bg = "#EFF6FF" if badge == "NEW" else "#FFF7ED"
        badge_border = "#BFDBFE" if badge == "NEW" else "#FED7AA"
        badge_text = "#1D4ED8" if badge == "NEW" else "#9A3412"

        return f"""
          <div style="margin:0 0 14px 0;padding:14px;border:1px solid #E5E7EB;border-radius:14px;background:#FFFFFF;">
            <div style="display:flex;gap:10px;align-items:center;margin-bottom:6px;">
              <span style="font-size:11px;font-weight:800;padding:4px 10px;border-radius:999px;background:{badge_bg};border:1px solid {badge_border};color:{badge_text};letter-spacing:0.2px;">
                {html_escape.escape(badge)}
              </span>
              <div style="font-weight:800;font-size:14px;color:#0F172A;">
                <a href="{url}" style="color:#0F172A;text-decoration:none;">{title}</a>
              </div>
            </div>
            {prev_html}
            {_ai_to_html(ai_summary)}
          </div>
        """

    # group items
    buckets_new = defaultdict(list)
    for it in items_new:
        buckets_new[classify_section(it.get("source", ""))].append(it)

    buckets_upd = defaultdict(list)
    for it in items_updated:
        buckets_upd[classify_section(it.get("source", ""))].append(it)

    parts: List[str] = []

    # Header counts
    parts.append(
        f"<div style='margin:10px 0 14px 0;color:#334155;font-size:13px;'>"
        f"<b>Updated:</b> {len(items_updated)} &nbsp;|&nbsp; <b>New:</b> {len(items_new)}"
        f"</div>"
    )

    # TL;DR
    if top3:
        lis = []
        for it in top3:
            badge = "UPDATED" if it in items_updated else "NEW"
            title = html_escape.escape(it.get("title","(untitled)"))
            url = html_escape.escape(it.get("url",""))
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

    # Updated section first
    if items_updated:
        parts.append("<div style='margin:18px 0 10px 0;border-top:1px solid #E5E7EB;'></div>")
        parts.append("<h2 style='margin:14px 0 10px 0;font-size:16px;color:#0F172A;'>Updated since last brief</h2>")
        for section in ["GOV.UK", "Parliament", "Legislation", "Courts & Tribunals", "Other"]:
            if not buckets_upd.get(section):
                continue
            parts.append(f"<h3 style='margin:14px 0 8px 0;font-size:14px;color:#0F172A;'>{html_escape.escape(section)}</h3>")
            for it in buckets_upd[section][:12]:
                parts.append(card(it, "UPDATED", prev=it.get("previously_covered")))

    # New section
    if items_new:
        parts.append("<div style='margin:18px 0 10px 0;border-top:1px solid #E5E7EB;'></div>")
        parts.append("<h2 style='margin:14px 0 10px 0;font-size:16px;color:#0F172A;'>New items</h2>")
        for section in ["GOV.UK", "Parliament", "Legislation", "Courts & Tribunals", "Other"]:
            if not buckets_new.get(section):
                continue
            parts.append(f"<h3 style='margin:14px 0 8px 0;font-size:14px;color:#0F172A;'>{html_escape.escape(section)}</h3>")
            for it in buckets_new[section][:12]:
                parts.append(card(it, "NEW"))

    if not items_new and not items_updated:
        parts.append(
            "<div style='margin-top:12px;padding:14px;border:1px solid #E5E7EB;border-radius:14px;background:#FFFFFF;color:#334155;'>"
            "<b>No material updates detected</b> for this run."
            "</div>"
        )

    inner = "\n".join(parts)

    # Nice outer template
    return f"""\
<html>
  <body style="margin:0;padding:0;background:#F1F5F9;font-family:Arial, sans-serif;color:#0F172A;">
    <div style="max-width:820px;margin:0 auto;padding:22px;">
      <div style="padding:18px;border:1px solid #E5E7EB;border-radius:16px;background:#FFFFFF;">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:14px;">
          <div>
            <div style="font-size:12px;color:#64748B;font-weight:700;letter-spacing:0.3px;">
              DraftCore • by Rushi Trivedi
            </div>
            <h1 style="margin:6px 0 0 0;font-size:22px;line-height:1.2;">{html_escape.escape(subject)}</h1>
          </div>
        </div>

        <div style="margin-top:10px;color:#475569;font-size:12.5px;line-height:1.45;">
          Information only — <b>not legal advice</b>. Always verify against the primary source before relying on it in practice.
        </div>

        <div style="margin-top:14px;">
          {inner}
        </div>

        <div style="margin-top:18px;border-top:1px solid #E5E7EB;padding-top:12px;color:#64748B;font-size:12px;">
          Sources monitored: GOV.UK, Parliament, legislation.gov.uk, and Courts/Tribunals (where available).
        </div>
      </div>
    </div>
  </body>
</html>
"""


def main():
    settings = load_settings("config/settings.yaml")

    # Scheduled runs: only send at configured local hour.
    # Manual runs: send immediately.
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        if not should_send_now(settings["timezone"], settings["send_hour_local"]):
            return

    state = load_state()
    state_items: Dict[str, Any] = state.get("items", {})

    raw_items = fetch_all_sources(settings)
    filtered = filter_items(raw_items, settings.get("keywords", []))

    tz = pytz.timezone(settings["timezone"])
    now_local = datetime.now(tz).strftime("%Y-%m-%d")

    new_items: List[Dict[str, Any]] = []
    updated_items: List[Dict[str, Any]] = []

    MAX_CANDIDATES = 30  # caps OpenAI cost
    processed = 0

    for it in filtered:
        if processed >= MAX_CANDIDATES:
            break

        url = normalise_url(it.get("url", ""))
        if not url:
            continue

        prev = state_items.get(url)
        prior_last_seen = prev.get("last_seen") if prev else None
        prior_hash = prev.get("last_content_hash") if prev else None
        prior_last_modified = prev.get("last_modified") if prev else None

        full_text, lm_hint = fetch_full_text(it)
        content_hash = sha256_text(full_text)

        if prev is None:
            status = "NEW"
        else:
            if content_hash != prior_hash:
                status = "UPDATED"
            elif lm_hint and (prior_last_modified is None or str(lm_hint) != str(prior_last_modified)):
                status = "UPDATED"
            else:
                status = "SKIP"

        if status == "SKIP":
            prev["last_seen"] = now_local
            continue

        try:
            ai = summarise_item(
                title=it.get("title", ""),
                content=full_text,
                is_update=(status == "UPDATED"),
            )
        except Exception:
            ai = (
                "- Summary unavailable (AI error)\n"
                "- Please open the source link for details."
            )

        out = dict(it)
        out["ai_summary"] = ai

        if status == "UPDATED" and prior_last_seen:
            out["previously_covered"] = prior_last_seen

        # Update state
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

    # If nothing to report: optionally still send a 'no material updates' brief
    if not new_items and not updated_items and not settings.get("always_send", False):
        return

    subject = build_subject(settings["timezone"])
    html_email = render_email_html(new_items, updated_items, settings["timezone"])

    send_email(
        smtp_host=os.environ["SMTP_HOST"],
        smtp_port=int(os.environ["SMTP_PORT"]),
        smtp_user=os.environ["SMTP_USER"],
        smtp_pass=os.environ["SMTP_PASS"],
        from_email=os.environ["FROM_EMAIL"],
        to_email=os.environ["TO_EMAIL"],
        subject=subject,
        html=html_email,
    )


if __name__ == "__main__":
    main()
