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

# Expect you created src/summarise.py with summarise_item(...)
from .summarise import summarise_item


CACHE_DIR = pathlib.Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)
STATE_PATH = CACHE_DIR / "state.json"

USER_AGENT = "DraftCore-Immigration-Intel-Brief/1.0 (+https://draftcore.co.uk)"


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
    # strip fragments + trailing slash normalisation
    u = (url or "").split("#")[0].strip()
    if u.endswith("/"):
        u = u[:-1]
    return u


def sha256_text(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8", errors="ignore")).hexdigest()


def parse_iso(dt_str: str) -> Optional[str]:
    # Keep as string but try to normalise if it looks ISO-ish
    if not dt_str:
        return None
    return dt_str.strip()


def strip_html_basic(html_text: str) -> str:
    # Basic fallback: remove scripts/styles, tags, compress whitespace
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
    "skilled worker", "student visa", "family visa", "human rights claim",
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

    # GOV.UK Content API path:
    # https://www.gov.uk/something -> https://www.gov.uk/api/content/something
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
                # Try to extract body from details.parts or details.body
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
                    return plain, parse_iso(updated_at)
        except Exception:
            pass

    # Fallback: fetch the HTML page (works for Parliament / Judiciary / legislation pages too)
    try:
        r = requests.get(url, timeout=30, headers={"User-Agent": USER_AGENT})
        r.raise_for_status()
        lm = r.headers.get("Last-Modified") or r.headers.get("ETag")
        plain = strip_html_basic(r.text)
        return plain[:20000], parse_iso(lm)  # cap for cost
    except Exception:
        # fallback to whatever we had
        summary = item.get("summary", "") or ""
        title = item.get("title", "") or ""
        return (title + "\n" + summary).strip(), None


def filter_items(items: List[Dict[str, Any]], keywords: List[str]) -> List[Dict[str, Any]]:
    # Two-stage relevance:
    # (1) keyword prefilter (fast)
    # (2) relevance scoring to reduce false positives (especially GOV.UK/Home Office noise)
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

        if kws and not any(k in hay for k in kws):
            continue

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


def render_email_html(items_new: List[Dict[str, Any]], items_updated: List[Dict[str, Any]], tz_name: str) -> str:
    subject = build_subject(tz_name)

    # TL;DR: top 3 highlights across UPDATED + NEW (by relevance score)
    combined = []
    for it in (items_updated + items_new):
        score = it.get("__relevance_score")
        if score is None:
            score = relevance_score(" ".join([it.get("title",""), it.get("summary",""), it.get("source",""), it.get("url","")]))
        combined.append((int(score), it))
    combined.sort(key=lambda x: x[0], reverse=True)
    top3 = [it for _, it in combined[:3]]

    tz = pytz.timezone(tz_name)
    generated_at = datetime.now(tz).strftime("%Y-%m-%d %H:%M")

    total_new = len(items_new)
    total_updated = len(items_updated)

    def badge_style(badge: str) -> str:
        # Email-safe inline styles
        if badge == "NEW":
            return "background:#ECFDF5;border:1px solid #A7F3D0;color:#065F46;"
        if badge == "UPDATED":
            return "background:#FFFBEB;border:1px solid #FDE68A;color:#92400E;"
        return "background:#F3F4F6;border:1px solid #E5E7EB;color:#111827;"

    def section_anchor(name: str) -> str:
        return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")

    def card(it: Dict[str, Any], badge: str, prev: Optional[str] = None) -> str:
        title = html_escape.escape(it.get("title", "(untitled)"))
        url = html_escape.escape(it.get("url", ""))
        summary_text = (it.get("ai_summary", "") or "").strip()

        lines = [ln.strip() for ln in summary_text.splitlines() if ln.strip()]
        bullet_lines = [ln for ln in lines if ln.startswith(("-", "•", "*"))]

        if bullet_lines:
            bullets = "".join(
                [f"<li style='margin:0 0 6px 0;'>{html_escape.escape(ln.lstrip('-•* ').strip())}</li>"
                 for ln in bullet_lines]
            )
            summary_html = f"<ul style='margin:10px 0 0 18px;padding:0;'>{bullets}</ul>"
        else:
            summary_html = (
                "<div style='margin-top:10px;color:#111827;font-size:13px;line-height:1.45;'>"
                + html_escape.escape(summary_text[:1200])
                + "</div>"
            )

        prev_html = ""
        if prev:
            prev_html = (
                "<div style='color:#6b7280;font-size:12px;margin-top:6px;'>"
                f"Previously covered: <b>{html_escape.escape(prev)}</b>"
                "</div>"
            )

        return f"""
          <div style="margin:0 0 12px 0;padding:14px;border:1px solid #E5E7EB;border-radius:14px;background:#FFFFFF;">
            <div style="display:flex;gap:10px;align-items:flex-start;">
              <span style="flex:0 0 auto;font-size:11px;font-weight:700;padding:4px 10px;border-radius:999px;{badge_style(badge)}">
                {html_escape.escape(badge)}
              </span>
              <div style="flex:1 1 auto;">
                <div style="font-weight:700;font-size:14px;line-height:1.35;margin:0 0 2px 0;">
                  <a href="{url}" style="color:#0F172A;text-decoration:underline;">{title}</a>
                </div>
                {prev_html}
              </div>
            </div>
            {summary_html}
          </div>
        """

    # Group items by section
    buckets_new = defaultdict(list)
    for it in items_new:
        buckets_new[classify_section(it.get("source", ""))].append(it)

    buckets_upd = defaultdict(list)
    for it in items_updated:
        buckets_upd[classify_section(it.get("source", ""))].append(it)

    sections_order = ["GOV.UK", "Parliament", "Legislation", "Courts & Tribunals", "Other"]

    # Build a small TOC
    toc_links = []
    if items_updated:
        toc_links.append(f"<a href='#{section_anchor('Updated')}' style='color:#2563EB;text-decoration:none;'>Updated</a>")
    if items_new:
        toc_links.append(f"<a href='#{section_anchor('New')}' style='color:#2563EB;text-decoration:none;'>New items</a>")
    toc_html = " &nbsp;|&nbsp; ".join(toc_links) if toc_links else ""

    parts: List[str] = []

    # TL;DR block
    if top3:
        parts.append(
            "<div style='margin:14px 0 16px 0;padding:14px;border:1px solid #E5E7EB;border-radius:14px;background:#F9FAFB;'>"
            "<div style='font-weight:700;font-size:14px;color:#0F172A;margin:0 0 8px 0;'>TL;DR — Top highlights</div>"
            "<ul style='margin:0;padding-left:18px;color:#111827;font-size:13px;line-height:1.45;'>"
        )
        for it in top3:
            title = html_escape.escape(it.get('title','(untitled)'))
            url = html_escape.escape(it.get('url',''))
            badge = 'UPDATED' if it in items_updated else 'NEW'
            parts.append(
                f"<li style='margin:0 0 6px 0;'><span style='font-size:11px;font-weight:700;padding:2px 8px;border-radius:999px;border:1px solid #E5E7EB;background:#FFFFFF;margin-right:6px;'>{badge}</span> "
                f"<a href='{url}' style='color:#2563EB;text-decoration:none;'>{title}</a></li>"
            )
        parts.append("</ul></div>")

    # Updated section first
    if items_updated:
        parts.append(
            f"""<div id="{section_anchor('Updated')}" style="margin-top:18px;">
                <div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;">
                  <h2 style="margin:0;font-size:18px;color:#0F172A;">Updated since last brief</h2>
                  <div style="font-size:12px;color:#6b7280;">{total_updated} item(s)</div>
                </div>
                <div style="height:1px;background:#E5E7EB;margin:10px 0 14px 0;"></div>
            </div>"""
        )
        for section in sections_order:
            if not buckets_upd.get(section):
                continue
            parts.append(
                f"""<h3 style="margin:14px 0 8px 0;font-size:14px;color:#111827;">
                    {html_escape.escape(section)}
                  </h3>"""
            )
            for it in buckets_upd[section][:12]:
                parts.append(card(it, "UPDATED", prev=it.get("previously_covered")))

    # New section
    if items_new:
        parts.append(
            f"""<div id="{section_anchor('New')}" style="margin-top:18px;">
                <div style="display:flex;align-items:baseline;justify-content:space-between;gap:12px;">
                  <h2 style="margin:0;font-size:18px;color:#0F172A;">New items</h2>
                  <div style="font-size:12px;color:#6b7280;">{total_new} item(s)</div>
                </div>
                <div style="height:1px;background:#E5E7EB;margin:10px 0 14px 0;"></div>
            </div>"""
        )
        for section in sections_order:
            if not buckets_new.get(section):
                continue
            parts.append(
                f"""<h3 style="margin:14px 0 8px 0;font-size:14px;color:#111827;">
                    {html_escape.escape(section)}
                  </h3>"""
            )
            for it in buckets_new[section][:12]:
                parts.append(card(it, "NEW"))

    if not parts:
        parts.append(
            "<div style='padding:14px;border:1px solid #E5E7EB;border-radius:14px;background:#FFFFFF;color:#374151;'>"
            "No new or updated immigration-relevant items were detected for this run."
            "</div>"
        )

    body = "
".join(parts)

    header = f"""
    <div style="padding:18px 18px 14px 18px;border-radius:18px;background:#FFFFFF;border:1px solid #E5E7EB;">
      <div style="display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap;">
        <div>
          <div style="font-size:12px;color:#6b7280;margin-bottom:6px;">
            DraftCore • by Rushi Trivedi
          </div>
          <div style="font-size:22px;font-weight:800;letter-spacing:-0.2px;color:#0F172A;margin:0 0 6px 0;">
            {html_escape.escape(subject)}
          </div>
          <div style="font-size:12px;color:#6b7280;line-height:1.4;">
            Generated {generated_at} ({html_escape.escape(tz_name)}). Sources: GOV.UK, Parliament, legislation.gov.uk, Judiciary.
          </div>
        </div>
        <div style="text-align:right;">
          <div style="font-size:12px;color:#6b7280;">This run</div>
          <div style="margin-top:4px;">
            <span style="display:inline-block;font-size:11px;font-weight:700;padding:4px 10px;border-radius:999px;{badge_style('UPDATED')}">UPDATED: {total_updated}</span>
            <span style="display:inline-block;font-size:11px;font-weight:700;padding:4px 10px;border-radius:999px;{badge_style('NEW')}margin-left:6px;">NEW: {total_new}</span>
          </div>
        </div>
      </div>

      <div style="height:1px;background:#E5E7EB;margin:14px 0 12px 0;"></div>

      <div style="font-size:13px;color:#111827;line-height:1.45;">
        <b>Important:</b> This newsletter is for information only and <b>does not</b> constitute legal advice.
        Always verify details against the primary source before relying on them in client work.
      </div>

      {f"<div style='margin-top:10px;font-size:12px;color:#6b7280;'>Jump to: {toc_html}</div>" if toc_html else ""}
    </div>
    """

    return f"""
    <html>
      <body style="margin:0;padding:0;background:#F6F7FB;">
        <div style="max-width:820px;margin:0 auto;padding:18px;">
          {header}
          <div style="margin-top:14px;">
            {body}
          </div>
          <div style="color:#94a3b8;font-size:11px;line-height:1.4;margin:16px 4px 0 4px;">
            Automated monitoring only. Always read the primary source before relying on it in advice.
          </div>
        </div>
      </body>
    </html>
    """

# ----------------------------
# Main pipeline
# ----------------------------
def main():
    settings = load_settings("config/settings.yaml")

    # Scheduled runs: only send at configured local hour.
    # Manual runs: send immediately.
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        if not should_send_now(settings["timezone"], settings["send_hour_local"]):
            return

    # Load state
    state = load_state()
    state_items: Dict[str, Any] = state.get("items", {})

    # Fetch + filter
    raw_items = fetch_all_sources(settings)
    filtered = filter_items(raw_items, settings["keywords"])

    # Process candidates
    tz = pytz.timezone(settings["timezone"])
    now_local = datetime.now(tz).strftime("%Y-%m-%d")

    new_items: List[Dict[str, Any]] = []
    updated_items: List[Dict[str, Any]] = []

    # Hard caps to control cost
    MAX_CANDIDATES = 30  # only NEW/UPDATED will be summarised
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

        # Fetch full text and compute content hash
        full_text, lm_hint = fetch_full_text(it)
        content_hash = sha256_text(full_text)

        # Determine NEW / UPDATED / SKIP
        status = None
        if prev is None:
            status = "NEW"
        else:
            # Update detection: content hash changed OR last_modified hint changed
            if content_hash != prior_hash:
                status = "UPDATED"
            elif lm_hint and (prior_last_modified is None or str(lm_hint) != str(prior_last_modified)):
                status = "UPDATED"
            else:
                status = "SKIP"

        if status == "SKIP":
            # still refresh last_seen so we know it remains present, but don't email it
            prev["last_seen"] = now_local
            continue

        # Summarise via AI (only for NEW/UPDATED)
        try:
            ai = summarise_item(
                title=it.get("title", ""),
                content=full_text,
                is_update=(status == "UPDATED"),
            )
        except Exception as e:
            # fallback: at least provide something
            ai = f"- Summary unavailable (AI error)\n- Title: {it.get('title','')}\n- Source: {it.get('source','')}\n- Link: {url}"

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

    # Save updated state
    state["items"] = state_items
    save_state(state)

    # If nothing to report: optionally still send a 'no material updates' brief
    if not new_items and not updated_items and not settings.get('always_send', False):
        return

    # Render + send
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
