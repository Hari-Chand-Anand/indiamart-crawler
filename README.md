# IndiaMart BuyLeads / Direct Leads crawler — deployment guide

What this is: a script that logs into the HCA IndiaMart seller dashboard, pulls
today's BuyLeads (India-wide) and any new Direct-source contacts from Lead
Manager, and writes new rows into a Google Sheet — meant to run unattended on
a schedule (cron), on a free-tier cloud VM.

**Status: written from manual testing, not yet run end-to-end on a live VM.**
Treat the first several runs as a validation pass — read `crawler.log`
and spot-check the Sheet against the real IndiaMart pages before trusting
this fully unattended. The BuyLeads text parser has been unit-tested against
real captured page text; the Direct Leads (All Contacts table) parser has
not been run against the live, virtualized table yet, and is the part most
likely to need adjustment.

## What's intentionally left open

- **"Contact Buyer Now" / BuyLeads credit spend.** Revealing a buyer's phone/email
  on a fresh BuyLead costs 1 unit of your BuyLeads Balance and marks that lead as
  actioned. This script does **not** do that automatically (`REVEAL_CONTACT_DETAILS`
  in `.env` defaults to `false` and isn't wired to any action yet) — decide how many
  credits/day you're comfortable spending on this before turning it on.
- **Geography → rep routing.** The Sheet gets an empty "Rep Mapping" tab
  (State / Rep Name / Notes) automatically. Fill that in whenever you have the
  real mapping — the "Assigned Rep" column on the BuyLeads tab already has a
  lookup formula that will start populating itself as soon as you do.
- **"Call" and "Other" lead sources.** Only `Direct` is pulled from the All
  Contacts table right now, per what was asked for. Easy to add if you want them too.

---

## Step 1 — Save a login session (run on your own laptop, not the VM)

This avoids ever scripting IndiaMart's login form or OTP flow, and means the
unattended script never needs your password.

```bash
pip install playwright
playwright install chromium
python save_session.py
```

A real browser window opens. Log in exactly as you normally do. Once you can
see BuyLeads / Lead Manager, go back to the terminal and press Enter. This
creates `storage_state.json` — you'll copy this one file to the VM in Step 4.

**Re-run this any time `crawler.log` says the session looks logged out.**

## Step 2 — Google Sheets: service account setup

1. Go to [console.cloud.google.com](https://console.cloud.google.com/), create
   a project (or reuse one you already have).
2. APIs & Services → Library → enable **Google Sheets API**.
3. APIs & Services → Credentials → Create Credentials → **Service account**.
   Give it any name (e.g. `indiamart-crawler`). No special roles needed.
4. Open the service account → Keys → Add Key → **Create new key** → JSON.
   This downloads a `.json` file — this is `service_account.json`, treat it
   like a password.
5. Open the file, copy the `client_email` value (looks like
   `indiamart-crawler@your-project.iam.gserviceaccount.com`).
6. Open the Google Sheet you want the data in → Share → paste that email in
   → give it **Editor** access.
7. Copy the Sheet's ID out of its URL:
   `https://docs.google.com/spreadsheets/d/`**`THIS_PART`**`/edit`

## Step 3 — Oracle Cloud Always Free VM

You'll need to do the account signup yourself (identity/card verification is
required by Oracle even though the tier itself is free forever):

1. Sign up at [oracle.com/cloud/free](https://www.oracle.com/cloud/free/).
2. Console → Compute → Instances → **Create instance**.
   - Image: **Ubuntu 22.04**.
   - Shape: pick one under "Always Free eligible" — either the
     `VM.Standard.E2.1.Micro` (AMD, 2 of these included free) or an `Ampere A1`
     shape (ARM, up to 2 OCPU/12GB total free as of mid-2026 — Oracle quietly
     halved this from 4 OCPU/24GB in June 2026, so ignore older guides that
     still quote the bigger number). Either is plenty for this script.
   - Add your SSH public key (Oracle's console can generate one for you if
     you don't have one — download and keep the private key safe).
3. Once it's running, note its public IP. In the instance's **Virtual Cloud
   Network → Security Lists**, make sure port 22 (SSH) is open (it usually is
   by default for the default VCN).
4. Test access: `ssh ubuntu@<VM_PUBLIC_IP>`

## Step 4 — Deploy the crawler to the VM

From your laptop, copy this whole folder plus the two files you generated
(`storage_state.json` from Step 1, `service_account.json` from Step 2) to the VM:

```bash
scp -r indiamart-crawler ubuntu@<VM_PUBLIC_IP>:~/
scp storage_state.json ubuntu@<VM_PUBLIC_IP>:~/indiamart-crawler/
scp service_account.json ubuntu@<VM_PUBLIC_IP>:~/indiamart-crawler/
```

SSH into the VM and install everything:

```bash
ssh ubuntu@<VM_PUBLIC_IP>
cd indiamart-crawler

sudo apt update
sudo apt install -y python3-pip python3-venv
python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
playwright install --with-deps chromium
```

Set up your config:

```bash
cp .env.example .env
nano .env   # fill in GOOGLE_SHEET_ID at minimum; the other paths already match this layout
```

## Step 5 — Test it manually before scheduling anything

```bash
source venv/bin/activate
python3 crawler.py
```

Watch the output (also written to `crawler.log`). Then open the Google Sheet
and check the `BuyLeads` and `Direct Leads` tabs got created with rows that
match what's actually on seller.indiamart.com right now. Fix anything that
looks wrong in `parser.py` before moving on — this is the step that catches
parsing bugs before they run unattended for weeks.

## Step 6 — Schedule it

```bash
crontab -e
```

Add (runs every 20 minutes):

```
*/20 * * * * cd /home/ubuntu/indiamart-crawler && venv/bin/python3 crawler.py >> cron.log 2>&1
```

## Alternative deployment: GitHub Actions (no VM at all)

Skip Steps 3–6 entirely and use this instead if you'd rather not manage a
VM, or want to sidestep Oracle's idle-account/idle-instance reclaim risk
above. Same `crawler.py`, same parser, same Sheets output — the workflow
file (`.github/workflows/crawl.yml`, already included in this folder) just
runs it on GitHub's schedule instead of cron on a VM.

**The trade-off to know before you pick this:** free *scheduled* (cron)
workflow runs only work on a **public** GitHub repo on a free personal
account. Your secrets (login session, service account key, Sheet ID) stay
encrypted either way and are never visible to anyone — but the crawler's
*code* would be. For an internal competitive-intelligence tool like this
one, decide knowingly: either accept the code being public, pay for GitHub
Pro ($4/mo) to keep the repo private with scheduling, or use the VM path
above instead.

1. Create a new GitHub repo and push this folder to it (`git init`, `git add`,
   `git commit`, `git remote add origin ...`, `git push`). The `.gitignore`
   already in this folder keeps `storage_state.json`, `service_account.json`,
   and `.env` out of the commit — double check `git status` before your
   first push that none of those three show up as "to be committed".
2. On your laptop, run `save_session.py` once (Step 1 above) if you haven't
   already, to get `storage_state.json`.
3. In the repo on GitHub: **Settings → Secrets and variables → Actions →
   New repository secret**, and add three secrets:
   - `STORAGE_STATE_JSON` — paste the entire contents of `storage_state.json`
   - `SERVICE_ACCOUNT_JSON` — paste the entire contents of `service_account.json`
     (from Step 2 above)
   - `GOOGLE_SHEET_ID` — the Sheet ID from Step 2
4. Go to the **Actions** tab → you should see "IndiaMart crawler" listed →
   click it → **Run workflow** to trigger a manual test run before waiting
   for the schedule. Check the run's logs, and download the `crawler-log-*`
   artifact it uploads (same `crawler.log` content you'd see on a VM).
5. Once a manual run works, it will also fire automatically every 20
   minutes (`.github/workflows/crawl.yml`'s `schedule:` line) — no VM,
   no SSH, no crontab, nothing to keep alive yourself.

**Re-running Step 1:** when the saved session eventually expires (same as
the VM path — `crawler.log` / the workflow's failed runs will say
"Session looks logged out"), re-run `save_session.py` locally and update
the `STORAGE_STATE_JSON` secret with the new file's contents. Secrets don't
version themselves, so this is a manual step either way.

**GitHub's own idle rule:** scheduled workflows get auto-disabled after 60
days with no commits to the repo — not the same risk as Oracle's, but
still something to know about. A tiny commit (e.g. touching a comment)
every couple of months keeps it alive, or just keep developing the repo
normally.

## Live sheet: crawler + reps editing together

Once this is running unattended, the same Sheet gets written to by the
crawler *and* edited by hand by reps (call outcomes, notes) at the same
time. Here's how that's kept conflict-free, all already built into
`sheets_writer.py` and the Sheet structure — nothing extra to set up:

- **The crawler only ever appends new rows.** It never rewrites a row it
  already wrote (that's what the "Lead Key" dedup column is for). So
  whatever a rep types into an existing row is never touched again by a
  later run — there's no scenario where a sync overwrites a rep's notes.
- **Each tab is split into crawler-owned and rep-owned columns.** Everything
  through "Assigned Rep" is written by the crawler; the last three columns
  — **Call Status**, **Remarks**, **Next Action Date** — are left blank on
  append specifically for reps to fill in. The crawler never writes to
  those three columns after the initial (blank) append, so there's no
  column-level conflict either.
- **Call Status is a dropdown**, not free text (Not Called / Called –
  Interested / Called – Not Interested / Called – Follow Up / No Response /
  Deal Done) — set via a Google Sheets data validation rule, so the column
  stays usable for filtering and status reports instead of turning into
  inconsistent free-text notes. Edit the `CALL_STATUS_OPTIONS` list at the
  top of `sheets_writer.py` if you want different values.
- **Crawler-owned columns are warning-only protected.** If a rep accidentally
  clicks into a crawler-owned cell and starts typing, Sheets shows a "this
  cell is protected" warning before they can save the edit. It's
  deliberately a warning, not a hard lock — nobody gets stuck if they
  genuinely need to hand-correct something — but it stops most accidental
  overwrites. (Google Sheets UI: Data → Protected sheets and ranges, if you
  ever want to see or adjust it by hand.)

Two more things worth setting up by hand in the Sheet itself (not something
a script should do on your behalf, since they're per-person/per-team
preferences):

- **Filter Views, one per rep or region**, so a Punjab rep can look at only
  Punjab rows without affecting what anyone else sees or touching the
  underlying data. In Sheets: **Data → Filter views → Create new filter
  view**, filter the "State" (BuyLeads) or add a filter on "Assigned Rep"
  once Rep Mapping is filled in, then name it per rep (e.g. "Amit — Punjab
  view"). Unlike a plain Filter (the funnel icon), a Filter View is
  personal — it doesn't hide rows for other people looking at the same
  sheet, and it doesn't interfere with the crawler appending rows
  underneath it.
- **Notification rules** for "someone should know a new lead landed" without
  building a custom alert system: **Tools → Notification rules** → "Any
  changes are made" or "A user submits a form" doesn't quite fit here, but
  the closest built-in option is **"When a change is made"** scoped by
  checking it periodically, or simpler — have each rep open their Filter
  View once a shift. If you want a true push notification the moment a
  lead in someone's region lands, that needs a small Apps Script trigger
  (`onEdit`/`onChange` → email or a chat webhook) — not built here since it
  wasn't asked for, but straightforward to add on top of this structure
  later if reps want it.

## Monitoring / troubleshooting

- **`crawler.log`** (rotates automatically) is the first place to check. A
  line like `Session looks logged out` means: SSH in, and either the session
  genuinely expired (re-run `save_session.py` on your laptop and `scp` the
  new `storage_state.json` over) or IndiaMart changed something on the page
  and `looks_logged_in()` in `crawler.py` needs adjusting.
- If leads stop showing up in the Sheet but the log shows no errors, IndiaMart
  likely changed its page layout — check `parser.py`'s comments for where to
  look first, and re-run Step 5 to see the actual current page text via
  `page.locator("body").inner_text()`.
- Nothing here auto-alerts you on failure. If you want to know immediately
  when a run fails rather than noticing later, the simplest addition is a
  one-line webhook/email call in `crawler.py`'s `except` blocks — didn't want
  to wire up a notification channel without knowing which one you'd want.

- Oracle can suspend/reclaim an **account** (not just an idle VM) after ~30
  days with no activity in the OCI web console — that's separate from the VM
  itself running cron jobs over SSH, which doesn't count. Log into
  [cloud.oracle.com](https://cloud.oracle.com) every few weeks so the account
  doesn't get flagged as abandoned out from under the crawler.
- Separately, Oracle can also reclaim an individual **Always Free instance**
  if it looks idle: if CPU, network, and memory 95th-percentile all stay
  under ~20% for 7 straight days, it's eligible for reclamation. A
  lightweight scraper running every 20 minutes and idling the rest of the
  time can plausibly look like that to Oracle's metrics. If you go the
  Oracle route, keep an eye on this (there's a "prevent reclamation" toggle
  in the console for Always Free instances) — this is the main reason the
  GitHub Actions path below is worth considering instead of a VM.

## Security notes

- `storage_state.json`, `service_account.json`, and `.env` are all
  equivalent to credentials. Don't commit them to git, don't paste their
  contents anywhere (including to Claude). `.gitignore` below covers this if
  you ever put this folder under version control.
- The VM only needs port 22 open. Nothing here needs to be reachable from the
  internet as a server.

```
# .gitignore
.env
storage_state.json
service_account.json
crawler.log*
cron.log
venv/
```
