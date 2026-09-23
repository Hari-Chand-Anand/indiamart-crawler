"""
Text parsers for IndiaMart's seller pages.

Why text-parsing instead of CSS selectors: IndiaMart's class names are
auto-generated and change on every front-end deploy, which makes CSS/XPath
selectors brittle. The rendered TEXT layout of each lead card has stayed
consistent across the pulls done while building this, so we parse that
instead -- same approach used to manually verify this data before writing
this script. It's more resilient, but still UI-dependent: if IndiaMart
reshuffles the page layout, re-check the patterns below against a fresh
`body.inner_text()` dump.

IMPORTANT: this parser was written from a handful of manually-inspected
samples, not a formal spec of IndiaMart's page structure. Treat the first
few live runs as a validation pass -- spot check the sheet against the
actual BuyLeads/Lead Manager pages before trusting it unattended.
"""

import re
from datetime import datetime

TIME_AGO_RE = re.compile(r"^(\d+)\s*(min|mins|hr|hrs)\s*ago$", re.IGNORECASE)
ENGAGEMENT_RE = re.compile(
    r"Requirements:\s*(\d+)(?:\|Calls:\s*(\d+))?(?:\|Replies:\s*(\d+))?"
)
KNOWN_LABELS = {
    "Machine Function", "Operating Mode", "Blade Size", "Machine Type",
    "Motor Power", "Quantity", "Sewing Machine Type", "Machine Condition",
    "Usage/Application", "Buyer Filled Details", "Order Value", "Model Name",
    "Model Name/number", "Model", "Brand", "Condition", "Automation Grade",
    "Belt Width", "Bed Size", "Finance/Loan Requirement", "Configuration",
    "Operation Type", "Max Sewing Speed", "Number Of Threads", "Stitch Type",
    "Application", "Max Stitch Width", "Additional Requirements",
    "Presser Foot Lift", "Product Condition",
}


def is_today(time_ago_text: str) -> bool:
    """True for '5 mins ago' / '3 hrs ago' style strings; False for
    'Yesterday', '21 Sep', etc. This mirrors the "today only, no backlog"
    rule -- adjust here if that rule ever changes."""
    return bool(TIME_AGO_RE.match(time_ago_text.strip()))


def parse_buyleads(body_text: str) -> list[dict]:
    """Split a BuyLeads page's full body text into one dict per lead.

    Call this right after: setting Location filter to "India", the "Recent"
    tab, and (if the inactivity interstitial appears) clicking "Get Fresh
    Leads" -- see crawler.py.
    """
    # Drop everything before the first real lead block (nav/filter chrome).
    # Every lead block ends right before the literal marker "Contact Buyer Now"
    # appears in the rendered text, so split on that.
    chunks = body_text.split("Contact Buyer Now")
    leads = []
    for chunk in chunks[:-1]:  # last chunk is trailing footer/nav, discard
        lead = _parse_one_buylead_chunk(chunk)
        if lead is not None:
            leads.append(lead)
    return leads


def _parse_one_buylead_chunk(chunk: str) -> dict | None:
    lines = [l.strip() for l in chunk.split("\n") if l.strip()]
    if not lines:
        return None

    # Walk backwards from the end of the chunk to find the title/location/
    # time block, since "Buyer also viewed" recommendation blocks (variable
    # length) can precede it.
    # Find the last occurrence of a City, / State line followed by a time-ago line.
    time_idx = None
    for i in range(len(lines) - 1, 1, -1):
        if TIME_AGO_RE.match(lines[i]) or lines[i] in ("Yesterday",) or re.match(r"^\d{1,2} [A-Za-z]{3}$", lines[i]):
            time_idx = i
            break
    if time_idx is None or time_idx < 2:
        return None

    # Layout is always three lines right before the time-ago line:
    #   <Title>
    #   <City>,
    #   <State>
    #   <N mins/hrs ago | Yesterday | DD Mon>
    time_text = lines[time_idx]
    state_line = lines[time_idx - 1] if time_idx - 1 >= 0 else ""
    city_line = lines[time_idx - 2].rstrip(",") if time_idx - 2 >= 0 else ""
    title_idx = time_idx - 3
    title = lines[title_idx] if title_idx >= 0 else ""

    lead = {
        "title": title,
        "city": city_line,
        "state": state_line or "",
        "posted": time_text,
        "is_today": is_today(time_text),
    }

    # Category > Sub-category: the two lines right after the time line.
    rest = lines[time_idx + 1:]
    if len(rest) >= 3 and rest[1] == ">":
        lead["category"] = rest[0]
        lead["sub_category"] = rest[2]
        rest = rest[3:]
    else:
        lead["category"] = ""
        lead["sub_category"] = ""

    # "Buyer Searched for ..." free text line, if present.
    if rest and rest[0].lower().startswith("buyer searched for"):
        lead["buyer_search_query"] = rest[0][len("Buyer Searched for "):].rstrip(".")
        rest = rest[1:]
    else:
        lead["buyer_search_query"] = ""

    # Key/value spec pairs until "Probable Requirement Type" (inclusive) --
    # pattern is  Label \n : \n Value  (three lines per pair).
    specs = {}
    i = 0
    requirement_type = ""
    while i < len(rest) - 2:
        label, colon, value = rest[i], rest[i + 1], rest[i + 2]
        if colon != ":":
            i += 1
            continue
        if label == "Probable Requirement Type":
            requirement_type = value
            i += 3
            break
        if label == "Buyer Filled Details":
            lead["buyer_filled_details"] = value
        elif label == "Order Value":
            lead["order_value"] = value
        elif label in KNOWN_LABELS or label not in (
            "Member since", "Buys", "Sells", "Engagement", "Business type", "Available",
        ):
            specs[label] = value
        i += 3
    lead.setdefault("buyer_filled_details", "")
    lead.setdefault("order_value", "")
    lead["requirement_type"] = requirement_type
    lead["specifications"] = "; ".join(f"{k}: {v}" for k, v in specs.items())

    # Buyer profile block: Member since / GST Verified / Buys / Engagement /
    # Sells / Business type -- order and presence both vary.
    remainder = "\n".join(rest[i:])
    lead["gst_verified"] = "Yes" if "GST Verified" in remainder else "No"

    m = re.search(r"Member since\s*\n?\s*([^\n]+)", remainder)
    lead["member_since"] = m.group(1).strip() if m else ""

    m = re.search(r"Buys\s*\n:\s*\n([^\n]+)", remainder)
    lead["buyer_also_buys"] = m.group(1).strip() if m else ""

    m = re.search(r"Sells\s*\n:\s*\n([^\n]+)", remainder)
    lead["buyer_also_sells"] = m.group(1).strip() if m else ""

    m = re.search(r"Business type\s*\n:\s*\n([^\n]+)", remainder)
    lead["business_type"] = m.group(1).strip() if m else ""

    m = ENGAGEMENT_RE.search(remainder)
    lead["engagement_requirements"] = m.group(1) if m else ""
    lead["engagement_calls"] = m.group(2) if m and m.group(2) else ""
    lead["engagement_replies"] = m.group(3) if m and m.group(3) else ""

    lead["contact_status"] = "Not opened (would use 1 credit)"
    for f in ("buyer_name", "buyer_company", "buyer_address", "buyer_email", "buyer_phone"):
        lead[f] = ""

    # A basic sanity check -- if we didn't manage to find a title, this
    # chunk was probably nav/footer noise, not a real lead.
    if not lead["title"] or len(lead["title"]) > 120:
        return None
    return lead


def parse_all_contacts_rows(row_texts: list[str]) -> list[dict]:
    """Parse rows from the Lead Manager -> All Contacts table
    (https://seller.indiamart.com/enquiry/inbox/), one string per row as
    returned by `page.inner_text()` on each grid row. Expected shape per
    row (blank lines for absent fields):

        <Sender Name>[GST]
        <Phone>
        <Requirement>
        <Quantity>            (optional)
        <Source>              Buylead | Direct | Call | Other
        <Lead Status>
        <Last message text>
    """
    contacts = []
    for text in row_texts:
        lines = [l for l in text.split("\n") if l.strip()]
        if len(lines) < 5:
            continue
        name = lines[0].replace("GST", "").strip()
        has_gst = "GST" in lines[0]
        phone = lines[1] if re.match(r"^[\d,]+$", lines[1]) else ""
        source_idx = next((i for i, l in enumerate(lines) if l in
                            ("Buylead", "Direct", "Call", "Other")), None)
        if source_idx is None:
            continue
        requirement_block = lines[2:source_idx]
        status = lines[source_idx + 1] if len(lines) > source_idx + 1 else ""
        message = lines[source_idx + 2] if len(lines) > source_idx + 2 else ""
        contacts.append({
            "sender_name": name,
            "gst_verified": "Yes" if has_gst else "No",
            "phone": phone,
            "requirement": requirement_block[0] if requirement_block else "",
            "quantity": requirement_block[1] if len(requirement_block) > 1 else "",
            "source": lines[source_idx],
            "lead_status": status,
            "last_message": message,
        })
    return contacts
