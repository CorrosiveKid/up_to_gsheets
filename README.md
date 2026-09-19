# Up Bank → Google Sheets sync

Pulls your [Up Bank](https://up.com.au) transactions — including category and
tags — into a Google Sheet you own, on a schedule, for free.

It's self-hosted: you run your own copy, and your data only ever moves
between Up's API, your Google Sheet, and whatever you run the sync on. There's
no service in the middle, and nobody else gets a copy of your banking data.

Safe to run repeatedly. New transactions are appended, and if something
already in the sheet gets recategorised or retagged in Up, the existing row is
updated in place rather than duplicated. It never deletes rows.

> **You'll need an Up Bank account**, which is Australian-only. If you don't
> bank with Up, this won't be useful to you.

What you end up with:

| id | date | description | amount | status | account | category | tags |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 9b1f… | 2026-09-14 | Coffee Supreme | -5.50 | SETTLED | Up | restaurants-and-cafes | coffee |
| 3c7a… | 2026-09-14 | Transfer to Rainy Day | -100.00 | SETTLED | Up | | |

`category` and `tags` are Up's own identifiers, so they stay stable and are
easy to pivot on.

## What you'll need

- An **Up Bank account** and a personal access token (free, 2 minutes).
- A **Google account**, to hold the sheet and a free service account.
- Either:
  - a **Vercel account** + **GitHub account** to run it in the cloud on a
    daily schedule (free), **or**
  - **Python 3.12+** on a machine of your own, if you'd rather run it there.

Everything below fits inside the free tier of all three services. See
[Costs](#costs).

## Step 1 — Get your own copy

This isn't a hosted service — you deploy your own instance.

**[Fork this repository](https://github.com/CorrosiveKid/up_to_gsheets/fork)**
to your GitHub account. A fork is what lets Vercel redeploy automatically when
you change the schedule or settings later.

Your fork can be public — no secrets ever get committed. `.env` and
`service_account.json` are both gitignored, and every credential lives in
environment variables instead.

If you only intend to run it on your own machine, a plain clone is fine:

```bash
git clone https://github.com/CorrosiveKid/up_to_gsheets.git
cd up_to_gsheets
```

## Step 2 — Get an Up personal access token

1. Go to https://api.up.com.au and log in with your Up account.
2. Generate a Personal Access Token. Copy it — you won't see it again.

Treat it like a password. If it ever leaks, revoke it on that same page and
issue a new one.

## Step 3 — Set up Google Sheets access

This is the fiddliest part, and it's a one-time thing.

1. Go to https://console.cloud.google.com and create a project (or use an
   existing one).
2. Enable the **Google Sheets API** for that project
   (APIs & Services → Enable APIs and Services → search "Google Sheets API").
3. Create a **Service Account** (APIs & Services → Credentials → Create
   Credentials → Service Account). No roles needed at the project level.
4. Open the service account, go to the **Keys** tab, and create a new JSON
   key. It downloads a `.json` file — save it into this folder as
   `service_account.json`.
5. Open the JSON file and copy the `client_email` value
   (looks like `something@your-project.iam.gserviceaccount.com`).
6. Create (or open) the Google Sheet you want to sync into, click **Share**,
   and share it with that service account email as an **Editor**.
   This step is easy to miss and is the most common cause of a
   "permission denied" error.
7. Copy the Sheet ID out of its URL:
   `https://docs.google.com/spreadsheets/d/THIS_PART/edit`

A service account can only touch sheets you explicitly share with it, so it
has no access to the rest of your Google Drive.

## Step 4 — Run it

Pick whichever suits you. They're not exclusive — plenty of people do the
first backfill locally and then let Vercel handle the daily top-up.

### Option A — Vercel (free, daily, nothing of yours stays running)

`api/sync.py` wraps the sync in a serverless function at `/api/sync`, and
`vercel.json` has Vercel Cron call it once a day.

1. Import your fork at https://vercel.com/new. There's no framework and no
   build step — accept the defaults.

   [![Deploy with Vercel](https://vercel.com/button)](https://vercel.com/new/clone?repository-url=https%3A%2F%2Fgithub.com%2FCorrosiveKid%2Fup_to_gsheets&env=UP_TOKEN,SHEET_ID,GOOGLE_SERVICE_ACCOUNT_JSON,CRON_SECRET,LOOKBACK_DAYS)

   (That button copies the repo into your own GitHub account and deploys it,
   prompting for the variables below — an alternative to forking by hand.)

2. Turn your service account key into a single-line value, because Vercel has
   no filesystem to put `service_account.json` on:

   ```bash
   base64 -w0 service_account.json    # Linux
   base64 -i  service_account.json    # macOS
   ```

3. Generate a secret for the cron endpoint:

   ```bash
   openssl rand -hex 32
   ```

4. In the Vercel project, go to **Settings → Environment Variables** and add
   these (for Production at minimum):

   | Variable | Value |
   | --- | --- |
   | `UP_TOKEN` | your Up personal access token |
   | `SHEET_ID` | the Sheet ID from its URL |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | the base64 string from step 2 |
   | `CRON_SECRET` | the secret from step 3 |
   | `LOOKBACK_DAYS` | `14` — see the notes below |

   Anything else from the [configuration reference](#configuration) can be
   added the same way; defaults are identical to local runs.

5. **Redeploy.** Environment variable changes don't apply to an
   already-built deployment.

6. Check it works by calling the endpoint yourself:

   ```bash
   curl -H "Authorization: Bearer YOUR_CRON_SECRET" \
     https://your-project.vercel.app/api/sync
   ```

   You should get a JSON summary back (`{"ok": true, "added": 12, ...}`) and
   see rows land in the sheet. Without the header you get a 401 — that's the
   endpoint refusing anyone who stumbles onto the URL.

Vercel Cron then calls it on the schedule in `vercel.json`:

```json
"crons": [{ "path": "/api/sync", "schedule": "0 19 * * *" }]
```

Cron schedules are **UTC**. `0 19 * * *` is about 5am in Sydney during winter
(AEST) and 6am during daylight saving (AEDT) — Vercel doesn't adjust for DST,
so the local time shifts by an hour twice a year. Edit the expression and
redeploy to move it.

Things to know about Vercel's free (Hobby) plan:

- **Once a day, and only roughly on time.** Hobby allows up to 2 cron jobs and
  they must be daily or less frequent; the invocation can drift by up to an
  hour. Fine here — the sync is idempotent and catches up on whatever it
  missed.
- **Keep `LOOKBACK_DAYS` modest.** A function is capped at 60 seconds. The
  default 90 makes every run re-fetch 90 days from Up and re-read the whole
  sheet to compare, which can run long. On a daily schedule `14` still gives
  you a fortnight to catch recategorised transactions, and finishes in
  seconds.
- **Do the first backfill locally.** Run it once with `LOOKBACK_DAYS=365` on
  your own machine (no timeout there), then let Vercel take over.
- **`CRON_SECRET` is required.** The handler returns a 500 instead of running
  if it isn't set, so a half-configured deploy can't leave an open endpoint
  that anyone could use to burn your Up API and Sheets quota.

### Option B — Your own machine

```bash
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and fill in `UP_TOKEN` and `SHEET_ID`. Leave the rest as defaults
unless you want a different tab name or lookback window. Then:

```bash
python sync.py
```

The first run creates the sheet tab with headers and populates it with the
last `LOOKBACK_DAYS` (default 90) of transactions. Every run after that
appends anything new and fixes up category/tags on rows that changed —
transactions older than the lookback window are left untouched, so the sheet
keeps growing without every run getting slower.

To schedule it:

**macOS / Linux (cron)** — run `crontab -e` and add a line to sync every
30 minutes:

```
*/30 * * * * cd /full/path/to/up_to_gsheets && /usr/bin/python3 sync.py >> sync.log 2>&1
```

**Windows** — use Task Scheduler to run
`python C:\path\to\up_to_gsheets\sync.py` on a repeating trigger, with
"Start in" set to the project folder (so it finds `.env` and
`service_account.json`).

## Configuration

Every setting is an environment variable, read from `.env` locally or from
Vercel's environment variables in the cloud. `.env.example` documents them all
with comments.

| Variable | Default | What it does |
| --- | --- | --- |
| `UP_TOKEN` | *required* | Your Up personal access token. |
| `SHEET_ID` | *required* | The ID from your Google Sheet's URL. |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | `service_account.json` | Path to the key file. Used locally. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | — | The key as raw JSON or base64, for Vercel. Takes precedence over the file. |
| `CRON_SECRET` | — | Bearer token protecting `/api/sync`. Required on Vercel. |
| `SHEET_NAME` | `Transactions` | Tab to sync into. Created if missing. Ignored when `SPLIT_BY_ACCOUNT=true`. |
| `LOOKBACK_DAYS` | `90` | How far back to fetch and to check for category/tag changes. |
| `SPLIT_BY_ACCOUNT` | `false` | Give each Up account its own tab instead of one combined tab. |
| `ACCOUNT_SCOPE` | `all` | `personal`, `joint`, or `all`. |
| `SPENDING_ONLY` | `false` | Exclude Saver accounts, syncing only transactional ones. |
| `SORT_ORDER` | `asc` | `asc` (oldest at top) or `desc` (newest at top). |

## How the sync behaves

- **Transfers between your own Up accounts** typically have no category —
  they'll show up in the sheet with a blank `category` cell, which is
  expected, not a bug.
- **`LOOKBACK_DAYS`** controls both how far back new transactions are
  fetched *and* how far back category/tag changes are detected. If you
  recategorise something older than this window, it won't be picked up.
  Bump it temporarily (e.g. to `365`) and run once if you need a deeper
  backfill or a wider correction pass.
- **Multiple accounts** (e.g. Spending + Saver) go into one combined tab
  by default, distinguished by an `account` column showing the account's
  display name (e.g. "Up", "Rainy Day"). Set `SPLIT_BY_ACCOUNT=true` to
  instead give each account its own tab, named the same way.
  `SHEET_NAME` is ignored in this mode, and the `account` column is
  dropped entirely — every row on a tab already belongs to one account,
  so the column would just repeat the tab name. Each account's tab is
  synced independently, so the upsert/lookback logic works the same way
  per tab as it does for the single combined sheet. If an account's name
  contains characters Google Sheets doesn't allow in tab titles, those
  are stripped automatically.
- **Which accounts get synced at all** is controlled by two flags that
  combine together:
  - `ACCOUNT_SCOPE` — `personal` (individually-owned accounts), `joint`
    (jointly-owned accounts), or `all` (default, everything).
  - `SPENDING_ONLY` — if `true`, excludes Saver accounts and syncs only
    spending/transactional accounts (e.g. "Up" but not "Rainy Day").
    Defaults to `false` (Savers included).

  These stack: `ACCOUNT_SCOPE=personal` + `SPENDING_ONLY=true` syncs
  just your personal "Up" account and nothing else. `ACCOUNT_SCOPE=all`
  + `SPENDING_ONLY=true` syncs both spending accounts ("Up" and "2Up")
  but no savers on either side. Both filters are applied before
  `SPLIT_BY_ACCOUNT`, so they combine naturally with tab-splitting too.
  If you have an Up Home loan account, it's neither a spending account
  nor a Saver, so it's excluded whenever `SPENDING_ONLY=true` — only
  `SPENDING_ONLY=false` (the default) will include it.
- **`SORT_ORDER`** controls whether the sheet reads oldest-to-newest
  (`asc`, default — new rows appended at the bottom) or newest-to-oldest
  (`desc` — new rows inserted just below the header, pushing everything
  else down). This only decides where *new* rows land; it won't
  retroactively re-sort rows already in the sheet, so pick an order
  before you've synced much history rather than switching later. Within
  a single run, transactions are also sorted correctly relative to each
  other — including same-day ones, using their full timestamp — so a
  90-day initial backfill lands in one consistent order rather than
  whatever order Up's API happened to return them in.
- The script never deletes rows, so it's safe to re-run after errors —
  worst case it does a bit of redundant comparison work, never data loss.

## Troubleshooting

**"permission denied" / 403 from Google** — you almost certainly skipped
sharing the sheet with the service account's `client_email` as an Editor
(step 3.6). This is the single most common setup mistake.

**401 from Up** — the token is wrong, or was revoked. Generate a new one at
https://api.up.com.au.

**`Missing required environment variable(s)`** — `UP_TOKEN` or `SHEET_ID`
isn't set. On Vercel, check you added them to the right environment *and*
redeployed afterwards.

**500 with `CRON_SECRET is not set`** — add `CRON_SECRET` to your Vercel
environment variables and redeploy. The endpoint deliberately refuses to run
without it.

**401 when you curl the endpoint** — your `Authorization: Bearer …` header is
missing or doesn't match `CRON_SECRET`.

**`No python entrypoint found in default locations`** on the Vercel build —
`pyproject.toml` is missing or its `[tool.vercel] entrypoint` line was
changed. Vercel only auto-detects entrypoints named
`app`/`index`/`server`/`main`/`wsgi`/`asgi`, so this project points at
`api/sync.py` explicitly.

**The function times out** — lower `LOOKBACK_DAYS`, and do any large backfill
locally where there's no 60-second cap.

**The cron didn't fire exactly on time** — expected on Hobby; it can drift up
to an hour, and runs at most once a day.

**A `category` cell is blank** — normal for transfers between your own
accounts.

## Costs

Free on every service involved, at this usage:

- **Up API** — free, with generous rate limits for personal use.
- **Google Sheets API** — free; a daily sync is nowhere near the quotas.
- **Vercel Hobby** — free; this uses 1 of your 2 allowed cron jobs.

## Security notes

- `.env` and `service_account.json` are gitignored. Don't commit either, and
  don't paste them into issues.
- Credentials live in environment variables, so a public fork leaks nothing.
- `CRON_SECRET` gates `/api/sync`, and the handler fails closed (500) if it
  isn't configured, rather than leaving the endpoint open.
- The Google service account can only reach sheets you've explicitly shared
  with it.
- Revoke an Up token at https://api.up.com.au if you think it's been exposed.

## Contributing

Issues and pull requests are welcome. Two things worth knowing before you
open one:

- Dependencies are listed in **both** `requirements.txt` (used for local
  installs) and `pyproject.toml` (what Vercel installs from). Update both.
- `pyproject.toml` also carries the `[tool.vercel] entrypoint` that makes the
  Vercel build work — don't drop it.

## License

[Apache 2.0](LICENSE).

Not affiliated with or endorsed by Up Bank.
