"""
sheets.py — Google Sheets integration for Bovonto Inventory Bot
Optimized for large sheets (18000+ rows) using:
  1. Server-side filter via Sheets API v4 (col A+B only first, then batch fetch matching rows)
  2. Session cache — one fetch per week selection, reused for all distributors
"""

import os
import gspread
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from dotenv import load_dotenv
from datetime import datetime

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.readonly",
]

SPREADSHEET_ID          = os.getenv("SPREADSHEET_ID")
SERVICE_ACCOUNT_FILE    = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "credentials.json")
MASTER_SHEET_NAME       = os.getenv("MASTER_SHEET_NAME",        "Master")
SALESMEN_SHEET_NAME     = os.getenv("SALESMEN_SHEET_NAME",      "Salesmen")
DISTRIBUTORS_SHEET_NAME = os.getenv("DISTRIBUTORS_SHEET_NAME",  "Distributors")
PRODUCTS_SHEET_NAME     = os.getenv("PRODUCTS_SHEET_NAME",      "Products")


# ──────────────────────────────────────────────
#  Auth helpers
# ──────────────────────────────────────────────

import json

def _get_creds() -> Credentials:
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        return Credentials.from_service_account_info(json.loads(creds_json), scopes=SCOPES)
    return Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)

def _get_gspread():
    return gspread.authorize(_get_creds())

def _get_sheets_service():
    """Raw Google Sheets API v4 client — for server-side filtering."""
    return build("sheets", "v4", credentials=_get_creds(), cache_discovery=False)

def _get_spreadsheet():
    return _get_gspread().open_by_key(SPREADSHEET_ID)


# ──────────────────────────────────────────────
#  Lookup helpers (small sheets — gspread fine)
# ──────────────────────────────────────────────

def get_salesmen() -> list[str]:
    ss = _get_spreadsheet()
    ws = ss.worksheet(SALESMEN_SHEET_NAME)
    values = ws.col_values(1)
    return [v.strip() for v in values if v.strip()]


def get_distributors_for_salesman(salesman: str) -> list[str]:
    ss   = _get_spreadsheet()
    ws   = ss.worksheet(DISTRIBUTORS_SHEET_NAME)
    rows = ws.get_all_values()
    return [
        row[0].strip()
        for row in rows
        if len(row) >= 3
        and row[2].strip().lower() == salesman.lower()
        and row[0].strip()
    ]


def get_current_month() -> str:
    return datetime.now().strftime("%b %Y")  # "Jun 2026"


# ──────────────────────────────────────────────
#  CORE — Server-side optimized fetch
# ──────────────────────────────────────────────

def fetch_month_week_rows(month: str, week: str) -> list[dict]:
    """
    Fast fetch for large sheets:
      Step 1 — Download col A+B only (tiny payload) to find matching row numbers
      Step 2 — Batch fetch ONLY those rows (A:J)
    Even at 50,000 rows, Step 1 is fast because 2 columns << 10 columns.
    """
    service = _get_sheets_service()

    # Step 1: col A + B only
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{MASTER_SHEET_NAME}!A:B",
        majorDimension="ROWS",
    ).execute()

    col_ab = result.get("values", [])

    target_rows = []
    for idx, row in enumerate(col_ab, start=1):
        if idx == 1:
            continue  # skip header
        if len(row) < 2:
            continue
        if (row[0].strip().lower() == month.lower() and
                row[1].strip().lower() == week.lower()):
            target_rows.append(idx)

    if not target_rows:
        return []

    # Step 2: batch fetch matching rows only
    ranges = _build_ranges(target_rows)
    range_start_rows = [int(r.split("!A")[1].split(":")[0]) for r in ranges]

    batch_result = service.spreadsheets().values().batchGet(
        spreadsheetId=SPREADSHEET_ID,
        ranges=ranges,
        majorDimension="ROWS",
    ).execute()

    # NOTE: Google's batchGet response valueRanges are guaranteed to be in
    # the SAME ORDER as the requested ranges list — so we zip by position,
    # not by re-parsing the echoed range string (which can be ambiguous
    # with sheet names containing special characters).
    rows_data = []
    seen_row_indices = set()
    value_ranges = batch_result.get("valueRanges", [])

    for range_idx, value_range in enumerate(value_ranges):
        values    = value_range.get("values", [])
        start_row = range_start_rows[range_idx]

        for i, row in enumerate(values):
            row_index = start_row + i
            if row_index in seen_row_indices:
                continue  # safety: never add the same sheet row twice
            seen_row_indices.add(row_index)

            if len(row) < 6:
                continue
            rows_data.append({
                "row_index":     row_index,
                "distributor":   row[3].strip() if len(row) > 3 else "",
                "salesman":      row[4].strip() if len(row) > 4 else "",
                "product":       row[5].strip() if len(row) > 5 else "",
                "category":      row[6].strip() if len(row) > 6 else "",
                "opening_stock": row[7].strip() if len(row) > 7 else "0",
                "receipt":       row[8].strip() if len(row) > 8 else "0",
                "closing_stock": row[9].strip() if len(row) > 9 else "",
            })

    return rows_data


def _build_ranges(row_numbers: list[int]) -> list[str]:
    """Group consecutive row numbers into A1 ranges to minimise API calls."""
    if not row_numbers:
        return []
    ranges = []
    start = end = row_numbers[0]
    for r in row_numbers[1:]:
        if r == end + 1:
            end = r
        else:
            ranges.append(f"{MASTER_SHEET_NAME}!A{start}:J{end}")
            start = end = r
    ranges.append(f"{MASTER_SHEET_NAME}!A{start}:J{end}")
    return ranges


def _parse_start_row(range_str: str) -> int:
    """'Master!A5:J10' → 5"""
    try:
        cell_part  = range_str.split("!")[1]
        start_cell = cell_part.split(":")[0]
        return int("".join(filter(str.isdigit, start_cell)))
    except Exception:
        return 1


# ──────────────────────────────────────────────
#  In-memory filter (zero API calls)
# ──────────────────────────────────────────────

def filter_rows_for_distributor(
    cached_rows: list[dict],
    salesman: str,
    distributor: str,
) -> list[dict]:
    """Filter cached rows for a specific distributor — no API call.
    Dedupes by row_index as a safety net against any upstream duplication."""
    matched = [
        r for r in cached_rows
        if r["salesman"].lower()     == salesman.lower()
        and r["distributor"].lower() == distributor.lower()
    ]
    seen = set()
    deduped = []
    for r in matched:
        if r["row_index"] in seen:
            continue
        seen.add(r["row_index"])
        deduped.append(r)
    return deduped


# ──────────────────────────────────────────────
#  Write closing stock
# ──────────────────────────────────────────────

def batch_write_closing_stocks(updates: list[dict]) -> None:
    """
    Single API round-trip to write all closing stocks.
    updates = [{"row_index": int, "closing_stock": str}, ...]
    """
    if not updates:
        return

    service = _get_sheets_service()

    value_ranges = [
        {
            "range":  f"{MASTER_SHEET_NAME}!J{u['row_index']}",
            "values": [[u["closing_stock"]]],
        }
        for u in updates
    ]

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "valueInputOption": "USER_ENTERED",
            "data": value_ranges,
        },
    ).execute()