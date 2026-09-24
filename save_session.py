"""
One-time, interactive helper. Run this ONCE on a machine with a screen (your laptop,
NOT the headless cloud VM) to log into the IndiaMart seller dashboard by hand and save
the resulting session to storage_state.json.

Why: this avoids ever having to script IndiaMart's login form (which may ask for an
OTP we can't automate) and avoids the crawler ever needing to know your password.
The unattended crawler on the VM just re-uses this saved session.

Usage:
    pip install playwright
    playwright install chromium
    python save_session.py

A real Chrome window will open. Log in exactly as you normally would (password,
OTP, whatever IndiaMart asks for). Once you can see the BuyLeads / Lead Manager
dashboard, come back to this terminal and press Enter. The session gets saved to
storage_state.json in this folder -- copy that one file to the VM (see README).

Re-run this whenever the crawler's logs say the session has expired.
"""

from playwright.sync_api import sync_playwright

OUTPUT_FILE = "storage_state.json"
START_URL = "https://seller.indiamart.com/"


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto(START_URL)

        print("\nA browser window has opened.")
        print("Log into the IndiaMart seller dashboard as you normally would.")
        print("Once you can see BuyLeads / Lead Manager in the sidebar, come back")
        print("here and press Enter.\n")
        input("Press Enter once you are logged in... ")

        context.storage_state(path=OUTPUT_FILE)
        browser.close()

    print(f"\nSaved session to {OUTPUT_FILE}.")
    print("Copy this file to the VM (e.g. with scp) and point STORAGE_STATE_PATH")
    print("at it in your .env file. Do not commit it to git or share it -- it is")
    print("equivalent to being logged into the seller account.")


if __name__ == "__main__":
    main()
