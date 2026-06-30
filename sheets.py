"""
sheets.py — Google Sheets integration for Bovonto Inventory Bot
Optimized for large sheets (18000+ rows) using:
  1. Server-side filter via Sheets API v4 (col A+B only first, then batch fetch matching rows)
  2. Session cache — one fetch per week selection, reused for all distributors

v2: Closing stock write now also auto-carries the value into the SAME
    distributor+product row's Opening Stock for the NEXT week, in the
    same batch API call. No Apps Script onEdit dependency needed since
    this bot writes via the raw Sheets API (which doesn't fire onEdit).
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
    Fetch all rows matching month+week from Master sheet.

    Uses a single bulk read (gspread get_all_values) then filters in Python.
    This is simple and 100% reliable — no risk of batchGet range-matching
    bugs. For sheets up to ~50,000 rows this single read is still fast
    (a few seconds), and since we only do this ONCE per week-selection
    (cached afterwards for all distributors), it is not a bottleneck.
    """
    ss = _get_spreadsheet()
    ws = ss.worksheet(MASTER_SHEET_NAME)
    all_values = ws.get_all_values()

    month_norm = month.strip().lower()
    week_norm  = week.strip().lower()

    rows_data = []
    for idx, row in enumerate(all_values, start=1):
        if idx == 1:
            continue  # header
        if len(row) < 6:
            continue
        r_month = row[0].strip().lower()
        r_week  = row[1].strip().lower()
        if r_month != month_norm or r_week != week_norm:
            continue

        rows_data.append({
            "row_index":     idx,
            "distributor":   row[3].strip() if len(row) > 3 else "",
            "salesman":      row[4].strip() if len(row) > 4 else "",
            "product":       row[5].strip() if len(row) > 5 else "",
            "category":      row[6].strip() if len(row) > 6 else "",
            "opening_stock": row[7].strip() if len(row) > 7 else "0",
            "receipt":       row[8].strip() if len(row) > 8 else "0",
            "closing_stock": row[9].strip() if len(row) > 9 else "",
        })

    return rows_data


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
#  Week / Month carry-forward helpers
# ──────────────────────────────────────────────

_WEEK_ORDER  = ["1st Week", "2nd Week", "3rd Week", "4th Week"]
_MONTH_ORDER = ["Jan","Feb","Mar","Apr","May","Jun",
                "Jul","Aug","Sep","Oct","Nov","Dec"]


def _next_week_label(week: str):
    """1st Week → 2nd Week → 3rd Week → 4th Week → None (caller handles month rollover)."""
    week_norm = week.strip()
    try:
        idx = _WEEK_ORDER.index(week_norm)
    except ValueError:
        return None
    if idx == len(_WEEK_ORDER) - 1:
        return None
    return _WEEK_ORDER[idx + 1]


def _next_month_label(month: str) -> str:
    """'Jun 2026' → 'Jul 2026'. 'Dec 2026' → 'Jan 2027'."""
    parts = month.strip().split(" ")
    mon_name, year = parts[0], int(parts[1])
    idx = _MONTH_ORDER.index(mon_name)
    if idx == 11:
        return f"Jan {year + 1}"
    return f"{_MONTH_ORDER[idx + 1]} {year}"


def _resolve_carry_target(month: str, week: str):
    """
    Returns (target_month, target_week) for carry-forward.
    1st/2nd/3rd Week → same month, next week.
    4th Week         → NEXT month, 1st Week (month rollover).
    """
    next_week = _next_week_label(week)
    if next_week:
        return month, next_week
    return _next_month_label(month), "1st Week"


def _find_target_row_indexes(target_month: str, target_week: str, targets: set) -> dict:
    """
    Scan Master sheet once and find row_index for each (distributor, product) pair
    that falls under (target_month, target_week).
    targets = {(distributor_lower, product_lower), ...}
    Returns: {(distributor_lower, product_lower): row_index}
    """
    ss = _get_spreadsheet()
    ws = ss.worksheet(MASTER_SHEET_NAME)
    all_values = ws.get_all_values()

    month_norm = target_month.strip().lower()
    week_norm  = target_week.strip().lower()

    found = {}
    for idx, row in enumerate(all_values, start=1):
        if idx == 1:
            continue
        if len(row) < 6:
            continue
        if row[0].strip().lower() != month_norm:
            continue
        if row[1].strip().lower() != week_norm:
            continue

        dist_key = row[3].strip().lower() if len(row) > 3 else ""
        prod_key = row[5].strip().lower() if len(row) > 5 else ""
        key = (dist_key, prod_key)
        if key in targets:
            found[key] = idx

    return found


# ──────────────────────────────────────────────
#  Write closing stock (+ auto carry-forward to next week/month opening)
# ──────────────────────────────────────────────

def batch_write_closing_stocks(updates: list[dict], month: str = None, week: str = None) -> None:
    """
    Single API round-trip to write all closing stocks.
    updates = [{"row_index": int, "closing_stock": str, "distributor": str, "product": str}, ...]

    If month + week are given, also carry-forwards each closing value into the
    SAME distributor+product row's Opening Stock column (H):
      - 1st/2nd/3rd Week → same month, next week
      - 4th Week         → NEXT MONTH's 1st Week (month rollover)

    If the next month's Master rows don't exist yet (they're normally created
    by Apps Script's monthFirstCheck on the 1st), the 4th-Week carry simply
    finds no matching row and is skipped silently — no error is raised.
    """
    if not updates:
        return

    service = _get_sheets_service()

    # 1) Closing stock writes (column J)
    value_ranges = [
        {
            "range":  f"{MASTER_SHEET_NAME}!J{u['row_index']}",
            "values": [[u["closing_stock"]]],
        }
        for u in updates
    ]

    # 2) Carry-forward: target week/month Opening (column H)
    if month and week:
        target_month, target_week = _resolve_carry_target(month, week)
        targets = {
            (u["distributor"].strip().lower(), u["product"].strip().lower())
            for u in updates
            if u.get("distributor") and u.get("product")
        }
        if targets:
            row_map = _find_target_row_indexes(target_month, target_week, targets)
            for u in updates:
                key = (u["distributor"].strip().lower(), u["product"].strip().lower())
                target_row = row_map.get(key)
                if target_row:
                    value_ranges.append({
                        "range":  f"{MASTER_SHEET_NAME}!H{target_row}",
                        "values": [[u["closing_stock"]]],
                    })

    service.spreadsheets().values().batchUpdate(
        spreadsheetId=SPREADSHEET_ID,
        body={
            "valueInputOption": "USER_ENTERED",
            "data": value_ranges,
        },
    ).execute()