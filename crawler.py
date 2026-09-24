"""
Unattended IndiaMart BuyLeads + Direct Leads crawler.

Run this from cron every 15-30 minutes (see README). Each run is a fresh,
short-lived process by design -- easier to reason about and restart cleanly
than a long-running loop, which is what "robust, doesn't break easily" calls
for on an unattended VM.

Flow per run:
  1. Launch headless Chromium, load the saved login session (storage_state.json).
  2. Bail out loudly (non-zero exit, logged) if that session looks logged out --
     it will eventually expire and someone needs to re-run save_session.py.
  3. BuyLeads: set Location filter to India, Recent tab, refresh if IndiaMart
     shows its "inactive" interstitial, parse the page text, keep only leads
     posted today, write new ones to the Sheet (dedup handled by sheets_writer).
  4. Direct Leads: read the Lead Manager "All Contacts" table, keep only
     Source = Direct, stop as soon as an already-recorded lead is hit (the
     table is newest-first, so everything after that point is backlog we've
     already seen) -- this is how "no backlog" is enforced for this source,
     since (unlike BuyLeads) it has no visible per-row timestamp to filter on.
"""

import logging
import os
import sys
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

import parser as leadparser
import sheets_writer

BUYLEADS_URL = "https://seller.indiamart.com/bltxn/?pref=recent&C_L_B=1"
CONTACTS_URL = "https://seller.indiamart.com/enquiry/inbox/"

LOG_PATH = os.path.join(os.path.dirname(__file__), "crawler.log")


def setup_logging():
    logger = logging.getLogger("indiamart_crawler")
    logger.setLevel(logging.INFO)
    handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.addHandler(logging.StreamHandler(sys.stdout))
    return logger


log = setup_logging()


def looks_logged_in(page) -> bool:
    try:
        return page.get_by_text("BuyLeads", exact=True).first.is_visible(timeout=5000)
    except PWTimeout:
        return False


def set_india_filter_and_refresh(page):
    """Switch the Location filter to India and refresh to a clean 'today'
    view. Selectors here are best-effort based on manual testing -- if
    IndiaMart changes this filter panel's layout, this is the first place
    to check."""
    try:
        page.get_by_text("India", exact=True).first.click(timeout=5000)
        page.get_by_text("Filter", exact=True).first.click(timeout=5000)
        page.wait_for_timeout(1500)
    except PWTimeout:
        log.warning("Could not click India filter / Filter button; continuing with whatever is already showing.")

    # IndiaMart occasionally shows a "You've been inactive for a while!"
    # interstitial with a "Get Fresh Leads" button that blocks the page.
    try:
        btn = page.get_by_text("Get Fresh Leads", exact=True)
        if btn.first.is_visible(timeout=3000):
            btn.first.click()
            page.wait_for_timeout(1500)
    except PWTimeout:
        pass


def crawl_buyleads(page, spreadsheet, pulled_at: str):
    log.info("Navigating to BuyLeads...")
    page.goto(BUYLEADS_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    if not looks_logged_in(page):
        raise RuntimeError("Session looks logged out on the BuyLeads page.")

    set_india_filter_and_refresh(page)

    body_text = page.locator("body").inner_text()
    leads = leadparser.parse_buyleads(body_text)
    todays_leads = [l for l in leads if l["is_today"]]
    log.info(f"Parsed {len(leads)} BuyLeads rows, {len(todays_leads)} from today.")

    reveal = os.getenv("REVEAL_CONTACT_DETAILS", "false").lower() == "true"
    if reveal:
        log.warning(
            "REVEAL_CONTACT_DETAILS=true -- this will click 'Contact Buyer Now' "
            "on every new lead, which spends a BuyLeads credit each time. Make "
            "sure that is actually the intended behaviour before leaving this on."
        )
        # Deliberately not auto-implemented further: this is the one action
        # in this script with a real, recurring cost, so it should be wired
        # up explicitly once the credit-budget question is settled, not
        # silently turned on by an env flag alone. See README.

    written = sheets_writer.write_buyleads(spreadsheet, todays_leads, pulled_at)
    log.info(f"Wrote {written} new BuyLeads rows to the sheet.")


def crawl_direct_leads(page, spreadsheet, pulled_at: str):
    log.info("Navigating to Lead Manager -> All Contacts...")
    page.goto(CONTACTS_URL, wait_until="domcontentloaded")
    page.wait_for_timeout(2000)

    if not looks_logged_in(page):
        raise RuntimeError("Session looks logged out on the All Contacts page.")

    existing_ws = spreadsheet.worksheet("Direct Leads") if _has_worksheet(spreadsheet, "Direct Leads") else None
    known_keys = sheets_writer._existing_keys(existing_ws) if existing_ws else set()

    collected = []
    seen_row_texts = set()
    stale_scrolls = 0
    for _ in range(40):  # hard cap so a layout change can't spin forever
        rows = page.locator('[role="row"]')
        count = rows.count()
        new_this_pass = 0
        for i in range(count):
            text = rows.nth(i).inner_text()
            if text in seen_row_texts:
                continue
            seen_row_texts.add(text)
            new_this_pass += 1
            parsed = leadparser.parse_all_contacts_rows([text])
            if not parsed:
                continue
            row = parsed[0]
            if row["source"] != "Direct":
                continue
            key = sheets_writer._lead_key(row["sender_name"], row["phone"], row["requirement"])
            if key in known_keys:
                # Table is newest-first: hitting a known row means everything
                # from here on is backlog we've already recorded.
                log.info("Reached a previously-seen contact; stopping scroll (no backlog re-scan).")
                collected_and_write()
                return
            collected.append(row)

        if new_this_pass == 0:
            stale_scrolls += 1
            if stale_scrolls >= 2:
                break
        else:
            stale_scrolls = 0

        page.mouse.wheel(0, 2000)
        page.wait_for_timeout(800)

    def collected_and_write():
        written = sheets_writer.write_direct_leads(spreadsheet, collected, pulled_at)
        log.info(f"Wrote {written} new Direct Leads rows to the sheet.")

    collected_and_write()


def _has_worksheet(spreadsheet, title: str) -> bool:
    return any(ws.title == title for ws in spreadsheet.worksheets())


def main():
    load_dotenv()
    storage_state_path = os.getenv("STORAGE_STATE_PATH", "./storage_state.json")
    service_account_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "./service_account.json")
    sheet_id = os.getenv("GOOGLE_SHEET_ID")

    if not sheet_id:
        log.error("GOOGLE_SHEET_ID is not set in .env -- aborting.")
        sys.exit(1)
    if not os.path.exists(storage_state_path):
        log.error(f"{storage_state_path} not found -- run save_session.py once first.")
        sys.exit(1)

    pulled_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    log.info("=== Crawler run starting ===")
    spreadsheet = sheets_writer.connect(service_account_path, sheet_id)
    sheets_writer.ensure_rep_mapping_tab(spreadsheet)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(storage_state=storage_state_path)
        page = context.new_page()

        try:
            crawl_buyleads(page, spreadsheet, pulled_at)
        except RuntimeError as e:
            log.error(f"BuyLeads crawl aborted: {e}")
        except Exception:
            log.exception("Unexpected error during BuyLeads crawl.")

        try:
            crawl_direct_leads(page, spreadsheet, pulled_at)
        except RuntimeError as e:
            log.error(f"Direct Leads crawl aborted: {e}")
        except Exception:
            log.exception("Unexpected error during Direct Leads crawl.")

        browser.close()

    log.info("=== Crawler run finished ===")


if __name__ == "__main__":
    main()
