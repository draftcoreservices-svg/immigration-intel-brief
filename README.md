# Immigration Intelligence Brief (GitHub Actions)

This repo runs a daily GitHub Actions workflow that:
- Pulls updates from GOV.UK, Parliament RSS, legislation.gov.uk Atom, and Judiciary (UTIAC) RSS
- Filters for immigration relevance (scoring + hard exclusions)
- Deduplicates previously-sent items using a small cache
- Prioritises items into High impact / Watchlist / Background
- Sends a single HTML email brief to your inbox

## One-time setup (GitHub)
1. Upload these files into a GitHub repo.
2. Add GitHub Secrets (Settings → Secrets and variables → Actions):
   - SMTP_HOST
   - SMTP_PORT
   - SMTP_USER
   - SMTP_PASS
   - FROM_EMAIL
   - TO_EMAIL
3. Run the workflow once (Actions tab → run manually).

## Scheduling
The workflow runs twice daily (07:00 and 08:00 UTC) to handle UK daylight saving time.
The script only sends when it's the configured `send_hour_local` in `config/settings.yaml` (default: 08:00 Europe/London).
