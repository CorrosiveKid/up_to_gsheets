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

Run manually with `python sync.py`, or schedule it (cron / Task Scheduler).
"""

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
LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "90"))
SPLIT_BY_ACCOUNT = os.environ.get("SPLIT_BY_ACCOUNT", "false").strip().lower() in ("1", "true", "yes")
ACCOUNT_SCOPE = os.environ.get("ACCOUNT_SCOPE", "all").strip().lower()

UP_BASE = "https://api.up.com.au/api/v1"
COLUMNS = ["id", "date", "description", "amount", "status", "account", "category", "tags"]

REQUIRED_ENV = {"UP_TOKEN": UP_TOKEN, "SHEET_ID": SHEET_ID}

VALID_SCOPES = ("personal", "joint", "all")
# personal = "Up" + "Up Savers" (individually-owned accounts)
# joint    = "2Up" + "2Up Savers" (jointly-owned accounts)
SCOPE_TO_OWNERSHIP = {"personal": "INDIVIDUAL", "joint": "JOINT"}


def check_config():
    missing = [name for name, val in REQUIRED_ENV.items() if not val]
    if missing:
        sys.exit(f"Missing required environment variable(s): {', '.join(missing)}")
    if not os.path.exists(SERVICE_ACCOUNT_FILE):
        sys.exit(f"Google service account file not found: {SERVICE_ACCOUNT_FILE}")
    if ACCOUNT_SCOPE not in VALID_SCOPES:
        sys.exit(f"ACCOUNT_SCOPE must be one of {VALID_SCOPES}, got {ACCOUNT_SCOPE!r}")


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


def allowed_account_ids(accounts, scope):
    """Which account IDs are in scope for 'personal' / 'joint' / 'all'."""
    if scope == "all":
        return set(accounts.keys())
    wanted_ownership = SCOPE_TO_OWNERSHIP[scope]
    return {
        account_id
        for account_id, attrs in accounts.items()
        if attrs["ownershipType"] == wanted_ownership
    }


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


def sync_rows(ws, header, existing_index, rows, tab_name):
    cat_idx = header.index("category")
    tag_idx = header.index("tags")

    new_rows = []
    updates = []  # (row_number, row_values)

    for row in rows:
        row_values = [str(row[c]) for c in COLUMNS]
        existing = existing_index.get(row["id"])

        if existing is None:
            new_rows.append(row_values)
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
        ws.append_rows(new_rows, value_input_option="USER_ENTERED")

    return len(new_rows), len(updates)


# --- Main ----------------------------------------------------------------

def main():
    check_config()

    since_dt = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    since_iso = since_dt.strftime("%Y-%m-%dT00:00:00+00:00")

    print(f"Fetching Up transactions since {since_iso} ...")
    transactions = fetch_transactions(since_iso)
    rows = [transaction_to_row(tx) for tx in transactions]
    print(f"Fetched {len(rows)} transactions.")

    accounts = fetch_accounts()
    in_scope_ids = allowed_account_ids(accounts, ACCOUNT_SCOPE)
    rows = [row for row in rows if row["account"] in in_scope_ids]
    print(f"{len(rows)} transactions in scope (ACCOUNT_SCOPE={ACCOUNT_SCOPE}).")

    creds = Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=["https://www.googleapis.com/auth/spreadsheets"],
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)

    if SPLIT_BY_ACCOUNT:
        account_names = {acc_id: attrs["displayName"] for acc_id, attrs in accounts.items()}
        grouped = group_rows_by_account(rows)

        total_added = total_updated = 0
        for account_id, account_rows in grouped.items():
            tab_name = tab_name_for_account(account_id, account_names)
            ws = open_worksheet(sh, tab_name)
            header, existing_index = load_existing_rows(ws)
            added, updated = sync_rows(ws, header, existing_index, account_rows, tab_name)
            print(f"[{tab_name}] new: {added}, updated: {updated}")
            total_added += added
            total_updated += updated
        print(f"Done. Total new rows: {total_added}, updated rows: {total_updated}.")
    else:
        ws = open_worksheet(sh, SHEET_NAME)
        header, existing_index = load_existing_rows(ws)
        added, updated = sync_rows(ws, header, existing_index, rows, SHEET_NAME)
        print(f"Done. New rows: {added}, updated rows: {updated}.")


if __name__ == "__main__":
    main()
