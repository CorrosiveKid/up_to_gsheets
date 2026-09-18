#!/usr/bin/env python3
"""
Up Bank -> Google Sheets sync.

- Fetches transactions from the Up API (personal access token).
- Upserts them into a Google Sheet: new transactions are appended,
  transactions already in the sheet get their category/tags updated
  in place if they've changed.
- Only compares/updates transactions within a rolling lookback window
  (LOOKBACK_DAYS) to keep each run fast regardless of how much history
  is in the sheet.

Run manually with `python sync.py`, or schedule it (cron / Task Scheduler /
Vercel Cron — see api/sync.py and README.md).
"""

import base64
import binascii
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests
import gspread
from google.oauth2.service_account import Credentials

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # python-dotenv is optional; env vars can be set another way

# --- Config (from environment) ---------------------------------------------

UP_TOKEN = os.environ.get("UP_TOKEN")
SHEET_ID = os.environ.get("SHEET_ID")
SHEET_NAME = os.environ.get("SHEET_NAME", "Transactions")
SERVICE_ACCOUNT_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "service_account.json")
# Alternative to the file above, for environments with no writable/committed
# filesystem (Vercel et al): the whole service account key as a single env
# var, either raw JSON or base64-encoded JSON.
SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "90"))
SPLIT_BY_ACCOUNT = os.environ.get("SPLIT_BY_ACCOUNT", "false").strip().lower() in ("1", "true", "yes")
ACCOUNT_SCOPE = os.environ.get("ACCOUNT_SCOPE", "all").strip().lower()
SPENDING_ONLY = os.environ.get("SPENDING_ONLY", "false").strip().lower() in ("1", "true", "yes")
SORT_ORDER = os.environ.get("SORT_ORDER", "asc").strip().lower()

UP_BASE = "https://api.up.com.au/api/v1"

# The "account" column is only useful in combined mode (multiple accounts
# sharing one tab) — when SPLIT_BY_ACCOUNT is on, each tab is already a
# single account, so the column would just repeat the tab name on every row.
BASE_COLUMNS = ["id", "date", "description", "amount", "status"]
TAIL_COLUMNS = ["category", "tags"]
COLUMNS = BASE_COLUMNS + TAIL_COLUMNS if SPLIT_BY_ACCOUNT else BASE_COLUMNS + ["account"] + TAIL_COLUMNS

REQUIRED_ENV = {"UP_TOKEN": UP_TOKEN, "SHEET_ID": SHEET_ID}

VALID_SCOPES = ("personal", "joint", "all")
# personal = "Up" + "Up Savers" (individually-owned accounts)
# joint    = "2Up" + "2Up Savers" (jointly-owned accounts)
SCOPE_TO_OWNERSHIP = {"personal": "INDIVIDUAL", "joint": "JOINT"}

VALID_SORT_ORDERS = ("asc", "desc")


class ConfigError(Exception):
    """Raised when the environment isn't configured well enough to run.

    Raised rather than sys.exit()'d so the same code path works both as a
    CLI (where main() turns it into an exit status) and inside a serverless
    handler (where it becomes an HTTP error response).
    """


def check_config():
    missing = [name for name, val in REQUIRED_ENV.items() if not val]
    if missing:
        raise ConfigError(f"Missing required environment variable(s): {', '.join(missing)}")
    if not SERVICE_ACCOUNT_JSON and not os.path.exists(SERVICE_ACCOUNT_FILE):
        raise ConfigError(
            f"No Google credentials: set GOOGLE_SERVICE_ACCOUNT_JSON, or provide the "
            f"key file at {SERVICE_ACCOUNT_FILE}"
        )
    if ACCOUNT_SCOPE not in VALID_SCOPES:
        raise ConfigError(f"ACCOUNT_SCOPE must be one of {VALID_SCOPES}, got {ACCOUNT_SCOPE!r}")
    if SORT_ORDER not in VALID_SORT_ORDERS:
        raise ConfigError(f"SORT_ORDER must be one of {VALID_SORT_ORDERS}, got {SORT_ORDER!r}")


def load_credentials():
    """Service account credentials from the env var if set, else the key file.

    The env var takes precedence so a deployed environment doesn't
    accidentally pick up a stale key file that happened to get bundled.
    """
    scopes = ["https://www.googleapis.com/auth/spreadsheets"]

    if SERVICE_ACCOUNT_JSON:
        return Credentials.from_service_account_info(
            _parse_service_account_json(SERVICE_ACCOUNT_JSON), scopes=scopes
        )

    return Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=scopes)


def _parse_service_account_json(raw):
    """Accept the key as raw JSON or as base64-encoded JSON.

    Base64 is the practical option for dashboards and .env files, where a
    multi-line JSON blob with embedded "\\n" in the private key is easy to
    mangle on copy/paste.
    """
    raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    try:
        decoded = base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ConfigError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is neither valid JSON nor valid base64"
        ) from exc

    try:
        return json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            "GOOGLE_SERVICE_ACCOUNT_JSON decoded from base64 but isn't valid JSON"
        ) from exc


# --- Up API ------------------------------------------------------------------

def fetch_transactions(since_iso):
    """Fetch all transactions since a given RFC3339 timestamp, following pagination."""
    headers = {"Authorization": f"Bearer {UP_TOKEN}"}
    url = f"{UP_BASE}/transactions"
    params = {"filter[since]": since_iso, "page[size]": 100}

    transactions = []
    while url:
        resp = requests.get(url, headers=headers, params=params)
        resp.raise_for_status()
        payload = resp.json()
        transactions.extend(payload["data"])
        url = payload.get("links", {}).get("next")
        params = None  # the "next" link already includes the query string
    return transactions


def fetch_accounts():
    """Return {account_id: {"displayName": ..., "ownershipType": ..., "accountType": ...}}
    for all of the user's Up accounts."""
    headers = {"Authorization": f"Bearer {UP_TOKEN}"}
    url = f"{UP_BASE}/accounts"

    accounts = {}
    while url:
        resp = requests.get(url, headers=headers)
        resp.raise_for_status()
        payload = resp.json()
        for acc in payload["data"]:
            attrs = acc["attributes"]
            accounts[acc["id"]] = {
                "displayName": attrs.get("displayName", acc["id"]),
                "ownershipType": attrs.get("ownershipType"),
                "accountType": attrs.get("accountType"),
            }
        url = payload.get("links", {}).get("next")
    return accounts


def allowed_account_ids(accounts, scope, spending_only=False):
    """Which account IDs are in scope, combining ownership (personal/joint/all)
    with the optional spending-accounts-only restriction."""
    if scope == "all":
        ids = set(accounts.keys())
    else:
        wanted_ownership = SCOPE_TO_OWNERSHIP[scope]
        ids = {
            account_id
            for account_id, attrs in accounts.items()
            if attrs["ownershipType"] == wanted_ownership
        }

    if spending_only:
        ids = {
            account_id
            for account_id in ids
            if accounts[account_id]["accountType"] == "TRANSACTIONAL"
        }

    return ids


def transaction_to_row(tx):
    attrs = tx["attributes"]
    rel = tx.get("relationships", {})

    category = (rel.get("category") or {}).get("data")
    category_id = category["id"] if category else ""

    tags_data = (rel.get("tags") or {}).get("data", [])
    tags = ",".join(t["id"] for t in tags_data)

    account = (rel.get("account") or {}).get("data") or {}

    return {
        "id": tx["id"],
        "date": attrs["createdAt"][:10],
        "description": attrs["description"],
        "amount": attrs["amount"]["value"],
        "status": attrs["status"],
        "account": account.get("id", ""),
        "category": category_id,
        "tags": tags,
        # Full timestamp, kept only for sorting — not a sheet column (not in
        # COLUMNS), so it never gets written out. Using the full timestamp
        # rather than the truncated "date" avoids same-day transactions
        # ending up in an arbitrary order relative to each other.
        "_created_at": attrs["createdAt"],
    }


# --- Google Sheets -----------------------------------------------------------

def col_letter(n):
    """1-indexed column number -> spreadsheet column letter (1 -> A, 27 -> AA)."""
    letters = ""
    while n > 0:
        n, r = divmod(n - 1, 26)
        letters = chr(65 + r) + letters
    return letters


def open_worksheet(sh, tab_name):
    try:
        ws = sh.worksheet(tab_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=tab_name, rows=1000, cols=len(COLUMNS))
        ws.append_row(COLUMNS)
    return ws


def group_rows_by_account(rows):
    grouped = {}
    for row in rows:
        grouped.setdefault(row["account"], []).append(row)
    return grouped


_INVALID_TAB_CHARS = set("[]*?/\\:")


def tab_name_for_account(account_id, account_names):
    """Sheet tab names can't contain [ ] * ? / \\ : and are capped at 100 chars."""
    raw = account_names.get(account_id, account_id)
    cleaned = "".join(c for c in raw if c not in _INVALID_TAB_CHARS).strip()
    return (cleaned or account_id)[:100]


def load_existing_rows(ws):
    """Return (header, {transaction_id: (row_number, row_values)})."""
    values = ws.get_all_values()
    if not values:
        ws.append_row(COLUMNS)
        return COLUMNS, {}

    header = values[0]
    id_col = header.index("id") if "id" in header else 0

    index = {}
    for i, row in enumerate(values[1:], start=2):  # row 1 is the header
        if len(row) > id_col and row[id_col]:
            index[row[id_col]] = (i, row)
    return header, index


def sync_rows(ws, header, existing_index, rows, tab_name, account_names):
    cat_idx = header.index("category")
    tag_idx = header.index("tags")

    new_rows = []  # (sort_key, row_values)
    updates = []  # (row_number, row_values)

    for row in rows:
        row_values = []
        for c in COLUMNS:
            if c == "account":
                row_values.append(str(account_names.get(row["account"], row["account"])))
            else:
                row_values.append(str(row[c]))

        existing = existing_index.get(row["id"])

        if existing is None:
            new_rows.append((row["_created_at"], row_values))
            continue

        rownum, old_row = existing
        old_category = old_row[cat_idx] if len(old_row) > cat_idx else ""
        old_tags = old_row[tag_idx] if len(old_row) > tag_idx else ""

        if old_category != row["category"] or old_tags != row["tags"]:
            updates.append((rownum, row_values))

    if updates:
        end_col = col_letter(len(COLUMNS))
        batch_data = [
            {"range": f"{tab_name}!A{rownum}:{end_col}{rownum}", "values": [row_values]}
            for rownum, row_values in updates
        ]
        ws.spreadsheet.values_batch_update({"valueInputOption": "USER_ENTERED", "data": batch_data})

    if new_rows:
        # Sort so a multi-transaction batch is internally consistent, not
        # just correctly placed relative to what's already in the sheet.
        new_rows.sort(key=lambda item: item[0], reverse=(SORT_ORDER == "desc"))
        sorted_values = [row_values for _, row_values in new_rows]

        if SORT_ORDER == "desc":
            # Newest-first sheet: new rows go above existing data, right
            # after the header row.
            ws.insert_rows(sorted_values, row=2, value_input_option="USER_ENTERED")
        else:
            # Oldest-first sheet (default): new rows go after everything
            # that's already there.
            ws.append_rows(sorted_values, value_input_option="USER_ENTERED")

    return len(new_rows), len(updates)


# --- Main ----------------------------------------------------------------

def run_sync(log=print):
    """Do one full sync and return a summary dict.

    Returns rather than prints its result so callers that aren't a terminal
    (the Vercel cron handler) can turn it into a response body. `log` still
    gets the human-readable progress lines — on Vercel those land in the
    function logs.
    """
    check_config()

    since_dt = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    since_iso = since_dt.strftime("%Y-%m-%dT00:00:00+00:00")

    log(f"Fetching Up transactions since {since_iso} ...")
    transactions = fetch_transactions(since_iso)
    rows = [transaction_to_row(tx) for tx in transactions]
    log(f"Fetched {len(rows)} transactions.")
    fetched = len(rows)

    accounts = fetch_accounts()
    in_scope_ids = allowed_account_ids(accounts, ACCOUNT_SCOPE, SPENDING_ONLY)
    rows = [row for row in rows if row["account"] in in_scope_ids]
    log(f"{len(rows)} transactions in scope (ACCOUNT_SCOPE={ACCOUNT_SCOPE}, SPENDING_ONLY={SPENDING_ONLY}).")
    account_names = {acc_id: attrs["displayName"] for acc_id, attrs in accounts.items()}

    gc = gspread.authorize(load_credentials())
    sh = gc.open_by_key(SHEET_ID)

    summary = {
        "fetched": fetched,
        "in_scope": len(rows),
        "since": since_iso,
        "split_by_account": SPLIT_BY_ACCOUNT,
        "tabs": {},
    }

    if SPLIT_BY_ACCOUNT:
        grouped = group_rows_by_account(rows)

        total_added = total_updated = 0
        for account_id, account_rows in grouped.items():
            tab_name = tab_name_for_account(account_id, account_names)
            ws = open_worksheet(sh, tab_name)
            header, existing_index = load_existing_rows(ws)
            added, updated = sync_rows(ws, header, existing_index, account_rows, tab_name, account_names)
            log(f"[{tab_name}] new: {added}, updated: {updated}")
            summary["tabs"][tab_name] = {"added": added, "updated": updated}
            total_added += added
            total_updated += updated
        log(f"Done. Total new rows: {total_added}, updated rows: {total_updated}.")
        summary["added"] = total_added
        summary["updated"] = total_updated
    else:
        ws = open_worksheet(sh, SHEET_NAME)
        header, existing_index = load_existing_rows(ws)
        added, updated = sync_rows(ws, header, existing_index, rows, SHEET_NAME, account_names)
        log(f"Done. New rows: {added}, updated rows: {updated}.")
        summary["tabs"][SHEET_NAME] = {"added": added, "updated": updated}
        summary["added"] = added
        summary["updated"] = updated

    return summary


def main():
    try:
        run_sync()
    except ConfigError as exc:
        sys.exit(str(exc))


if __name__ == "__main__":
    main()
