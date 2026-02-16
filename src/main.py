import os, pathlib, json, hashlib, html
import pytz
from datetime import datetime
from collections import defaultdict
from .sources import fetch_all_sources
from .emailer import send_email
from .utils import load_settings

CACHE_DIR = pathlib.Path(".cache")
CACHE_DIR.mkdir(exist_ok=True)
SEEN_PATH = CACHE_DIR / "seen.json"

def should_send_now(tz_name: str, send_hour_local: int) -> bool:
    tz = pytz.timezone(tz_name)
    now_local = datetime.now(tz)
    return now_local.hour == send_hour_local and now_local.minute < 20

def load_seen():
    if not SEEN_PATH.exists():
        return set()
    return set(json.loads(SEEN_PATH.read_text(encoding="utf-8")))

def save_seen(seen_set):
    SEEN_PATH.write_text(json.dumps(sorted(list(seen_set))[-5000:]), encoding="utf-8")

def key_for(item):
    url = (item.get("url") or "").split("#")[0].strip().lower()
    title = (item.get("title") or "").strip().lower()
    raw = f"{url}|{title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def filter_items(items, keywords):
    kws = [k.lower() for k in keywords]
    out = []
    for it in items:
        hay = " ".join([
            it.get("title",""),
            it.get("summary",""),
            it.get("source",""),
            it.get("url",""),
        ]).lower()
        if any(k in hay for k in kws):
            out.append(it)
    return out

def build_subject(tz_name):
    tz = pytz.timezone(tz_name)
    d = datetime.now(tz).strftime("%Y-%m-%d")
    return f"Immigration Intelligence Brief — {d}"

def takeaway(it):
    t = (it.get("title","") + " " + it.get("summary","")).lower()
    if "immigration rules" in t or "statement of changes" in t:
        return "Potential changes to the Immigration Rules; check effective dates and transitional provisions."
    if "guidance" in t:
        return "Guidance may have changed; check applicability to live matters and evidence requirements."
    if "bill" in t:
        return "Legislative activity that may signal future policy shifts; track stages and commencement."
    if "regulation" in t or "order" in t or "statutory instrument" in t:
        return "Possible regulatory change affecting procedure, fees, eligibility, or enforcement."
    if "upper tribunal" in t or "utiac" in t:
        return "Potentially useful authority on appeals/country issues; assess relevance to current cases."
    return "Scan for client impact, compliance changes, or developing policy direction."

def render_email_html(items, tz_name):
    buckets = defaultdict(list)
    for it in items:
        src = it.get("source","Other")
        if "GOV.UK" in src:
            buckets["GOV.UK"].append(it)
        elif "Parliament" in src:
            buckets["Parliament"].append(it)
        elif "legislation.gov.uk" in src:
            buckets["Legislation"].append(it)
        elif "Judiciary" in src or "UTIAC" in src:
            buckets["Courts & Tribunals"].append(it)
        else:
            buckets["Other"].append(it)

    def item_html(it):
        title = html.escape(it.get("title","(untitled)"))
        url = html.escape(it.get("url",""))
        summary = html.escape((it.get("summary","") or "")[:260])
        tk = html.escape(takeaway(it))
        return f"""
          <div style="margin:0 0 14px 0;padding:12px;border:1px solid #e5e7eb;border-radius:10px;">
            <div style="font-weight:700;margin:0 0 6px 0;"><a href="{url}">{title}</a></div>
            <div style="color:#374151;font-size:13px;margin:0 0 8px 0;">{summary}</div>
            <ul style="margin:0;padding-left:18px;color:#111827;font-size:13px;">
              <li><b>Why it matters:</b> {tk}</li>
            </ul>
          </div>
        """

    sections = ""
    for section in ["GOV.UK","Parliament","Legislation","Courts & Tribunals","Other"]:
        if not buckets.get(section):
            continue
        sections += f"<h2 style='margin:22px 0 10px 0;'>{section}</h2>"
        for it in buckets[section][:12]:
            sections += item_html(it)

    subject = build_subject(tz_name)
    return f"""
    <html>
      <body style="font-family:Arial, sans-serif; background:#ffffff; color:#111827; max-width:760px; margin:0 auto; padding:22px;">
        <h1 style="margin:0 0 8px 0;">{html.escape(subject)}</h1>
        <div style="color:#6b7280;font-size:13px;margin:0 0 18px 0;">
          Automated daily brief from official sources (GOV.UK, Parliament, legislation.gov.uk, Judiciary).
        </div>
        {sections}
        <hr style="margin:24px 0;border:none;border-top:1px solid #e5e7eb;" />
        <div style="color:#6b7280;font-size:12px;">
          Automated monitoring only. Always read the primary source before relying on it in advice.
        </div>
      </body>
    </html>
    """

def main():
    settings = load_settings("config/settings.yaml")

    # Scheduled runs: only send at 08:00 London time (DST-safe).
    # Manual runs: send immediately.
    if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
        if not should_send_now(settings["timezone"], settings["send_hour_local"]):
            return

    seen = load_seen()

    raw_items = fetch_all_sources(settings)
    filtered = filter_items(raw_items, settings["keywords"])

    deduped = []
    for it in filtered:
        k = key_for(it)
        if k in seen:
            continue
        seen.add(k)
        deduped.append(it)

    if not deduped:
        return

    html_email = render_email_html(deduped, settings["timezone"])
    subject = build_subject(settings["timezone"])

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

    save_seen(seen)

if __name__ == "__main__":
    main()
