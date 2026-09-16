# Up Bank → Google Sheets sync

Pulls your Up Bank transactions (including category and tags) into a
Google Sheet. Safe to run repeatedly — new transactions are appended,
and if a transaction already in the sheet gets recategorised or retagged
in Up, this script updates that row in place instead of duplicating it.

## 1. Get an Up personal access token

1. Go to https://api.up.com.au and log in with your Up account.
2. Generate a Personal Access Token. Copy it — you won't see it again.

## 2. Set up Google Sheets access (one-time)

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

## 3. Install and configure

```bash
cd up-to-sheets
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` and fill in `UP_TOKEN` and `SHEET_ID`. Leave the rest as
defaults unless you want a different tab name or lookback window.

## 4. Run it

```bash
python sync.py
```

First run creates the sheet tab with headers and populates it with the
last `LOOKBACK_DAYS` (default 90) of transactions. Every run after that
appends anything new and fixes up category/tags on rows that changed —
transactions older than the lookback window are left untouched, so the
sheet keeps growing without every run getting slower.

## 5. Schedule it

**macOS / Linux (cron):** run `crontab -e` and add a line to sync every
30 minutes:

```
*/30 * * * * cd /full/path/to/up-to-sheets && /usr/bin/python3 sync.py >> sync.log 2>&1
```

**Windows:** use Task Scheduler to run
`python C:\path\to\up-to-sheets\sync.py` on a repeating trigger, with
"Start in" set to the project folder (so it finds `.env` and
`service_account.json`).

## Notes / things worth knowing

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
  display name (e.g. "Up", "Rainy Day"). Set `SPLIT_BY_ACCOUNT=true` in
  `.env` to instead give each account its own tab, named the same way.
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
