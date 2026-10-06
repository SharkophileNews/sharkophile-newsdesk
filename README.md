# Sharkophile Newsdesk

An automated news desk for **sharkophile.com**. Twice a day it finds the latest shark news (science, conservation, incidents, fishing, diving and pop culture), writes a draft in Sharkophile's house style, illustrates it, optimizes it for search, files it in WordPress as a **draft** with categories and tags, and alerts the editor. **Nothing is ever published automatically** — a human editor reviews and clicks Publish.

```
 Bing News + science feeds ──► dedupe & cluster ──► triage editor (Claude Haiku)
                                                          │ picks up to 3 stories
                                                          ▼
 fetch sources ─► find primary source (web search) ─► write draft (Claude Opus, STYLE_GUIDE.md)
                                                          │
            integrity + SEO checks ◄──── fact-check (Claude Sonnet) ──► one auto-revision if needed
                                                          │
 AI illustration (OpenAI) ─► WordPress draft (image, categories, tags, SEO fields, social text)
                                                          │
                                       Slack / email alert to the editor with Edit + Preview links
```

## What's in the box

| Path | What it is |
|---|---|
| `STYLE_GUIDE.md` | Sharkophile's house style, derived from the site's archive. It is loaded verbatim into the writer's instructions — **edit this to change how stories read**. |
| `config.yaml` | Behavior: sources, schedule limits, models, image settings, categories, alerts. No secrets. |
| `newsdesk/` | The Python program. |
| `wordpress/sharkophile-newsdesk.php` | Small WordPress must-use plugin (see Step 1). |
| `.github/workflows/newsdesk.yml` | Runs everything on GitHub Actions on a schedule. |
| `state/newsdesk_state.json` | Memory of what's been seen/covered so stories aren't repeated. Updated automatically. |
| `tests/` | Self-tests that run before every scheduled run. |

## How each story is held to the site's standards

- **Facts only from sources.** The writer is given the full text of 1–3 source articles (preferring university releases, journals, agencies and major newsrooms; tabloids and MSN syndication are never used as sources) plus web-search notes with citations. It may not add facts beyond them.
- **Quotes are verified mechanically.** Every quotation in a draft is checked word-for-word against the sources. Fabricated or altered quotes trigger an automatic rewrite, and if one survives it's flagged in the alert.
- **No copying.** Any run of 12+ words lifted from a source outside quotation marks triggers a rewrite.
- **Links are real.** Links and listed sources must appear in the source material; invented URLs are stripped.
- **Independent fact-check.** A second model compares the draft to the sources and reports problems; major ones trigger one revision, and anything left is shown prominently to the editor (in the alert and in a "Newsdesk review notes" box on the WordPress edit screen).
- **House style.** Sentence-case headlines, AP-style numbers and dates, attribution in the lede, 300–600 words, "Source: … / Study: …" line at the end, a banned-phrase list (no "delve", "majestic", "shark-infested"…), careful handling of bite incidents.
- **SEO.** SEO title ≤ 60 characters, 3–7 word slug, 120–155 character meta description (also used as the excerpt and social card text), focus keyword in title/slug/meta/first paragraph, internal link to a related Sharkophile story, alt text, reading-grade check, H2 subheads for longer pieces, NewsArticle structured data (from the plugin).
- **Images.** Editorial illustrations generated to strict rules (accurate anatomy; no people, text, logos, blood or re-created incidents), captioned *"Illustration generated with AI for Sharkophile."*, resized to 1200 px JPEG with alt text, set as the featured image and placed at the top of the post like existing Sharkophile stories.
- **Categories.** Always *News* plus 1–2 topical categories from the site's existing set. *Featured*, *Sponsored*, *Products* and *Videos* are left to the editor.

---

## Setup (about 45 minutes, once)

You'll need: WordPress admin access to sharkophile.com, a GitHub account, an Anthropic API key, an OpenAI API key, and either a Slack workspace or an email account for alerts.

### Step 1 — WordPress

1. **Newsdesk account.** The newsdesk signs in as **Sharkophile Staff** (Editor), the account that also appears as the byline. Its application password is named "Newsdesk". To replace it, open *Users → All Users → Sharkophile Staff → Edit → Application Passwords* (not your own profile — a password only works for the account it was made on).
2. **Install the companion plugin — required on this host.** Sharkophile's host (Bluehost-style Apache) strips the standard login header from API requests, so WordPress never sees the application password. The plugin fixes that. In wp-admin go to *Plugins → Add New → Upload Plugin*, choose **`sharkophile-newsdesk-plugin.zip`**, click *Install Now*, then *Activate*. (Alternative: copy `wordpress/sharkophile-newsdesk.php` into `wp-content/mu-plugins/` with the host's File Manager.) It:
   - accepts the newsdesk's backup login header when the host strips the normal one (HTTPS only; WordPress still checks the application password as usual),
   - lets the newsdesk set the Genesis SEO title and meta description over the API,
   - shows a **"🦈 Newsdesk review notes"** box on each draft's edit screen (fact-check result, flagged issues, SEO checklist, sources),
   - adds NewsArticle structured data to posts (skipped automatically if Yoast or Rank Math is active).
3. **Byline.** `config.yaml` → `site.author_id: 2` is the *Sharkophile Staff* account.

### Step 2 — API keys

- **Anthropic (Claude):** create a key at console.anthropic.com → *API Keys*. Add a small monthly spend limit to start.
- **OpenAI (images):** create a key at platform.openai.com → *API keys*. Image generation may require organization verification on OpenAI's side. To run without images, set `images.provider: none` in `config.yaml`.

### Step 3 — Editor alerts (pick one or more)

- **Slack:** create an incoming webhook (api.slack.com/messaging/webhooks) for the channel your editor watches. Copy the `https://hooks.slack.com/services/...` URL.
- **Email via Gmail:** turn on 2-Step Verification on the sending Google account, then create an *App password* (myaccount.google.com/apppasswords). Use `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=587`, `SMTP_USER=` the Gmail address, `SMTP_PASSWORD=` the app password, `EDITOR_EMAIL=` who should get alerts (comma-separate several).
- **Anything else** (Zapier, Make, Discord, Teams via a relay): set `ALERT_WEBHOOK_URL` and `alerts.webhook: true`; it receives a JSON summary.

### Step 4 — GitHub

1. Create a **private** repository (e.g. `sharkophile-newsdesk`) and upload the contents of this folder, keeping the folder structure (including the hidden `.github` folder).
2. *Settings → Secrets and variables → Actions → New repository secret*. Add:

   | Secret | Value |
   |---|---|
   | `ANTHROPIC_API_KEY` | your Claude key |
   | `OPENAI_API_KEY` | your OpenAI key |
   | `WP_USER` | `Sharkophile Staff` (exactly, with the space) |
   | `WP_APP_PASSWORD` | the application password (spaces are fine) |
   | `SLACK_WEBHOOK_URL` | (if using Slack) |
   | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EDITOR_EMAIL` | (if using email) |
   | `WP_URL` | optional — only if the site URL changes from `https://www.sharkophile.com` |

3. *Settings → Actions → General → Workflow permissions* → choose **Read and write permissions** (so the run can save its memory file).
4. **Verify:** *Actions → Sharkophile newsdesk → Run workflow* → command **check**. Every line should show ✔. Then run **discover** to see today's candidate stories.
5. **Try a dry run:** Run workflow with **Dry run** ticked. When it finishes, open the run and download the `newsdesk-…` artifact: it contains an HTML preview of each draft, the image, and the alert text — nothing touches WordPress.
6. **Go live:** Run workflow with defaults. Drafts appear in *Posts → Drafts* and the alert arrives. From then on it runs automatically at about 7:10 a.m. and 3:10 p.m. Eastern.

To have it write up a specific article an editor found, use *Run workflow* and paste the URL into **url**.

---

## The editor's routine

When an alert arrives:

1. Click **Edit in WordPress**. Read the **Newsdesk review notes** box first.
2. Check anything flagged 🔴 or ⚠️ against the linked sources — especially numbers, names, and quotes.
3. Look at the image: is the species right? Is it appropriate for the story? Regenerate or swap in a licensed photo if not.
4. Optional: add *Featured*, adjust tags, edit the social message (Jetpack "Share" panel).
5. Publish (or schedule). Delete drafts you don't want — the newsdesk won't recreate them.

Tip: if a story type keeps coming out wrong, fix it in `STYLE_GUIDE.md` rather than editing every draft.

## Tuning

All in `config.yaml`:

- `run.max_drafts_per_run` (default 3), `run.min_score` (default 6 of 10) — volume and selectivity.
- `run.max_age_hours` — how fresh stories must be.
- `sources.bing_queries` / `sources.feeds` — what's monitored. Add a university press office or NOAA feed under `feeds`.
- `sources.exclude_patterns` — false positives to drop (San Jose Sharks, Shark Tank, SharkNinja and friends are already there).
- `site.post_status: pending` — use WordPress's *Pending Review* queue instead of *Draft*.
- `models` — which Claude model plays each role. `images.quality` — `low` / `medium` / `high`.
- The schedule lives in `.github/workflows/newsdesk.yml` (`cron` lines, in UTC).

## Cost (rough estimate)

Per draft, at published API prices: about **$0.30–$0.70** for Claude (writing on Opus 5.5, fact-check and research on Sonnet 5.5, triage on Haiku 4.5, including up to three web searches at $10 per 1,000; the high end is when a draft needs a revision), plus one image at OpenAI's current rate for `gpt-image-2` at your chosen quality. At 2 runs/day × up to 3 drafts, plan on roughly $2–4 a day for Claude. Each run's exact token use is in `run.json` in the run artifact. GitHub Actions usage for this job is small (a few minutes per run).

## Troubleshooting

- **`check` says WordPress ignored the login (`rest_not_logged_in`):** the host is stripping the `Authorization` header and the companion plugin isn't active — activate it under *Plugins* (Step 1). Alternatively add `SetEnvIf Authorization "(.*)" HTTP_AUTHORIZATION=$1` near the top of the site's `.htaccess`.
- **`check` says the password is incorrect (401 `incorrect_password` / `invalid_username`):** re-check the `WP_USER` and `WP_APP_PASSWORD` secrets, or create a new application password. Security plugins/firewalls can also block the REST API or GitHub's servers — allow `/wp-json/` for authenticated users.
- **"No featured image" in the review notes:** image generation failed; the note says why. "No credits remaining" means the OpenAI account needs credit (platform.openai.com → Settings → Billing). Add an image by hand for that draft; later drafts will get one again.
- **A secret shows as `***` in the check output, or Claude says `invalid x-api-key`:** the secret's value is wrong — often the secret's *name* was pasted into the value box. Edit the secret and paste only the key.
- **"SEO fields not writable":** the companion plugin isn't active (check *Plugins*, or *Plugins → Must-Use* if you copied the file).
- **"not enough readable source text":** every source was paywalled, script-rendered or blocked by robots.txt. The newsdesk won't write from headlines alone. Paste a readable URL via *Run workflow → url* if you still want it.
- **No drafts for a while:** run **discover**. If it lists nothing, Bing may be throttling GitHub's servers; add a few RSS feeds under `sources.feeds`.
- **Scheduled runs stopped:** GitHub can pause schedules in repositories it considers inactive. Re-enable the workflow under *Actions* (a manual run or any commit also counts as activity).

## Running it elsewhere

Any machine with Python 3.11+ works (a VPS, a home server):

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=... OPENAI_API_KEY=... WP_USER=newsdesk WP_APP_PASSWORD="..." SLACK_WEBHOOK_URL=...
python -m newsdesk check
python -m newsdesk run --dry-run
# crontab -e:  10 7,15 * * *  cd /path/to/sharkophile-newsdesk && python -m newsdesk run >> newsdesk.log 2>&1
```

Run the self-tests with `python -m unittest discover -s tests -t .`.
