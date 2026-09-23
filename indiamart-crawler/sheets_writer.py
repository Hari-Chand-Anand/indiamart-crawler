"""
Google Sheets writer. Uses a service account (see README "Google Sheets
setup") rather than the Apps Script web-app pattern -- more moving parts to
set up once, but no redeploy-on-every-edit fragility and no execution quota
surprises, which is what "robust, doesn't break easily" calls for here.

Collaboration model (crawler + reps editing the same sheet):
  - The crawler ONLY ever appends brand-new rows. It never rewrites a row
    it already wrote, so a rep's edits on existing rows are never touched
    or overwritten by a later run.
  - Each tab is split into CRAWLER-owned columns (everything up through
    "Assigned Rep") and REP-owned columns ("Call Status", "Remarks",
    "Next Action Date"), left blank on write for reps to fill in.
  - Crawler-owned columns get a warning-only Protected Range, so a rep
    editing one of those cells sees "this cell is protected" before
    overwriting something the crawler will just rewrite again next sync
    anyway -- a nudge, not a hard lock, so nobody gets stuck if they
    genuinely need to hand-correct a bad parse.
  - "Call Status" gets a dropdown (data validation) instead of free text,
    so the column stays usable for filtering/reporting.
See README "Live sheet: crawler + reps editing together" for the full
explanation of why this shape avoids conflicts.
"""

import hashlib
import gspread
from google.oauth2.service_account import Credentials

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# --- Columns the CRAWLER owns (written once on append, never touched again) -
BUYLEADS_CRAWLER_HEADERS = [
    "Lead Key", "Pulled At", "Product / Requirement", "City", "State", "Posted",
    "Category", "Sub-Category", "Buyer Search Query", "Specifications",
    "Buyer Filled Details", "Order Value", "Requirement Type",
    "Buyer Member Since", "GST Verified", "Buyer Also Buys", "Buyer Also Sells",
    "Business Type", "Engagement: Requirements", "Engagement: Calls",
    "Engagement: Replies", "Contact Status", "Buyer Name", "Buyer Company",
    "Buyer Address", "Buyer Email", "Buyer Phone", "Assigned Rep",
]

DIRECT_CRAWLER_HEADERS = [
    "Lead Key", "Pulled At", "Sender Name", "Phone", "GST Verified",
    "Requirement", "Quantity", "Source", "Lead Status", "Last Message",
    "Assigned Rep",
]

# --- Columns the REPS own -- crawler leaves these blank on append and never
# writes to them again, so reps can fill them in with zero risk of a later
# run clobbering their input. -------------------------------------------------
REP_OWNED_HEADERS = ["Call Status", "Remarks", "Next Action Date"]

CALL_STATUS_OPTIONS = [
    "Not Called", "Called - Interested", "Called - Not Interested",
    "Called - Follow Up", "No Response", "Deal Done",
]

BUYLEADS_HEADERS = BUYLEADS_CRAWLER_HEADERS + REP_OWNED_HEADERS
DIRECT_HEADERS = DIRECT_CRAWLER_HEADERS + REP_OWNED_HEADERS

REP_MAPPING_HEADERS = ["State", "Rep Name", "Notes"]


def _lead_key(*parts: str) -> str:
    return hashlib.sha1("||".join(parts).encode("utf-8")).hexdigest()[:16]


def _col_letter(n: int) -> str:
    """1-indexed column number -> spreadsheet column letter(s), e.g. 1->A, 28->AB."""
    letters = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters = chr(65 + rem) + letters
    return letters


def connect(service_account_json_path: str, sheet_id: str) -> gspread.Spreadsheet:
    creds = Credentials.from_service_account_file(service_account_json_path, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id)


def _ensure_worksheet(spreadsheet: gspread.Spreadsheet, title: str, headers: list[str]):
    try:
        ws = spreadsheet.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=title, rows=1000, cols=len(headers) + 2)
        ws.append_row(headers)
        return ws
    existing_headers = ws.row_values(1)
    if not existing_headers:
        ws.append_row(headers)
    return ws


def ensure_rep_mapping_tab(spreadsheet: gspread.Spreadsheet):
    """Creates a 'Rep Mapping' tab (State -> Rep Name) if it doesn't exist
    yet, so geography routing can be added later without touching this
    script -- fill it in whenever you have the real mapping."""
    _ensure_worksheet(spreadsheet, "Rep Mapping", REP_MAPPING_HEADERS)


def _existing_keys(ws) -> set[str]:
    col = ws.col_values(1)  # "Lead Key" is always column A
    return set(col[1:])  # skip header


def _rep_formula(row_number: int, state_col_letter: str) -> str:
    # INDEX/MATCH against the Rep Mapping tab; blank until that tab is filled in.
    return (
        f'=IFERROR(INDEX(\'Rep Mapping\'!B:B, MATCH({state_col_letter}{row_number}, '
        f"'Rep Mapping'!A:A, 0)), \"\")"
    )


def _existing_protected_ranges(spreadsheet: gspread.Spreadsheet, sheet_gid: int) -> list[dict]:
    meta = spreadsheet.fetch_sheet_metadata(
        params={"fields": "sheets(properties(sheetId),protectedRanges)"}
    )
    for sheet in meta.get("sheets", []):
        if sheet["properties"]["sheetId"] == sheet_gid:
            return sheet.get("protectedRanges", [])
    return []


def _protect_crawler_columns(spreadsheet: gspread.Spreadsheet, ws, crawler_col_count: int):
    """Warning-only protection over the crawler-owned columns (A through
    'Assigned Rep'). Warning-only rather than a hard lock on purpose: a rep
    can still override it if they genuinely need to hand-fix a bad parse,
    they just get a "this cell is protected" heads-up first. Safe to call
    every run -- skips if a matching whole-column protected range already
    exists, so it doesn't pile up duplicate ranges over time.
    """
    gid = ws.id
    for pr in _existing_protected_ranges(spreadsheet, gid):
        rng = pr.get("range", {})
        if (rng.get("startColumnIndex", -1) == 0
                and rng.get("endColumnIndex") == crawler_col_count
                and "startRowIndex" not in rng):
            return  # already protected
    spreadsheet.batch_update({
        "requests": [{
            "addProtectedRange": {
                "protectedRange": {
                    "range": {
                        "sheetId": gid,
                        "startColumnIndex": 0,
                        "endColumnIndex": crawler_col_count,
                    },
                    "description": (
                        "Crawler-owned columns -- edits here get overwritten "
                        "or ignored on the next run. Use Call Status / "
                        "Remarks / Next Action Date instead."
                    ),
                    "warningOnly": True,
                }
            }
        }]
    })


def _apply_call_status_validation(spreadsheet: gspread.Spreadsheet, ws, col_index: int):
    """Dropdown on the Call Status column (rows 2-2000). Re-applying this
    every run is harmless -- setDataValidation replaces the existing rule
    for that range rather than stacking another one on top."""
    gid = ws.id
    spreadsheet.batch_update({
        "requests": [{
            "setDataValidation": {
                "range": {
                    "sheetId": gid,
                    "startRowIndex": 1,
                    "endRowIndex": 2000,
                    "startColumnIndex": col_index,
                    "endColumnIndex": col_index + 1,
                },
                "rule": {
                    "condition": {
                        "type": "ONE_OF_LIST",
                        "values": [{"userEnteredValue": v} for v in CALL_STATUS_OPTIONS],
                    },
                    "showCustomUi": True,
                    "strict": False,
                },
            }
        }]
    })


def ensure_collaboration_setup(spreadsheet: gspread.Spreadsheet, ws, crawler_headers: list[str],
                                all_headers: list[str]):
    """Locks crawler-owned columns (warning-only) and puts a dropdown on
    Call Status. Called once per tab per run; both operations are cheap
    no-ops after the first successful run (see docstrings above)."""
    _protect_crawler_columns(spreadsheet, ws, len(crawler_headers))
    _apply_call_status_validation(spreadsheet, ws, all_headers.index("Call Status"))


def write_buyleads(spreadsheet: gspread.Spreadsheet, leads: list[dict], pulled_at: str):
    ws = _ensure_worksheet(spreadsheet, "BuyLeads", BUYLEADS_HEADERS)
    ensure_collaboration_setup(spreadsheet, ws, BUYLEADS_CRAWLER_HEADERS, BUYLEADS_HEADERS)
    known = _existing_keys(ws)
    new_rows = []
    for lead in leads:
        key = _lead_key(lead["title"], lead["city"], lead["posted"], lead.get("buyer_phone", ""))
        if key in known:
            continue
        known.add(key)
        row = [
            key, pulled_at, lead["title"], lead["city"], lead["state"], lead["posted"],
            lead["category"], lead["sub_category"], lead["buyer_search_query"],
            lead["specifications"], lead["buyer_filled_details"], lead["order_value"],
            lead["requirement_type"], lead["member_since"], lead["gst_verified"],
            lead["buyer_also_buys"], lead["buyer_also_sells"], lead["business_type"],
            lead["engagement_requirements"], lead["engagement_calls"],
            lead["engagement_replies"], lead["contact_status"], lead["buyer_name"],
            lead["buyer_company"], lead["buyer_address"], lead["buyer_email"],
            lead["buyer_phone"], "",  # Assigned Rep formula added after append
            "", "", "",  # Call Status, Remarks, Next Action Date -- left for reps
        ]
        new_rows.append(row)
    if not new_rows:
        return 0
    start_row = len(ws.col_values(1)) + 1
    ws.append_rows(new_rows, value_input_option="USER_ENTERED")
    # State column -> lookup formula per new row into the "Assigned Rep" column.
    state_col_letter = _col_letter(BUYLEADS_HEADERS.index("State") + 1)
    rep_col_letter = _col_letter(BUYLEADS_HEADERS.index("Assigned Rep") + 1)
    formulas = [[_rep_formula(start_row + i, state_col_letter)] for i in range(len(new_rows))]
    ws.update(f"{rep_col_letter}{start_row}:{rep_col_letter}{start_row + len(new_rows) - 1}",
              formulas, value_input_option="USER_ENTERED")
    return len(new_rows)


def write_direct_leads(spreadsheet: gspread.Spreadsheet, contacts: list[dict], pulled_at: str):
    ws = _ensure_worksheet(spreadsheet, "Direct Leads", DIRECT_HEADERS)
    ensure_collaboration_setup(spreadsheet, ws, DIRECT_CRAWLER_HEADERS, DIRECT_HEADERS)
    known = _existing_keys(ws)
    new_rows = []
    for c in contacts:
        key = _lead_key(c["sender_name"], c["phone"], c["requirement"])
        if key in known:
            continue
        known.add(key)
        new_rows.append([
            key, pulled_at, c["sender_name"], c["phone"], c["gst_verified"],
            c["requirement"], c["quantity"], c["source"], c["lead_status"],
            c["last_message"], "",
            "", "", "",  # Call Status, Remarks, Next Action Date -- left for reps
        ])
    if not new_rows:
        return 0
    start_row = len(ws.col_values(1)) + 1
    ws.append_rows(new_rows, value_input_option="USER_ENTERED")
    # Direct Leads has no State column captured yet -- Assigned Rep stays
    # blank until that's added, so reps can still fill it in by hand.
    return len(new_rows)
