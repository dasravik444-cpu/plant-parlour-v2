# Setup guide

About 15 minutes, all in a browser. No coding needed.

## 0. First: replace the leaked keys (important)

The old system's files contained passwords and keys in plain text, and those
files have been shared with several AI tools. Treat them as public and replace them:

| What | Where to fix it |
|---|---|
| Google service-account key `lead-sync-bot@plant-parlour-automation` (key id `60ba43bf...`) | Google Cloud Console > IAM & Admin > Service Accounts > lead-sync-bot > **Keys**: *Add key > Create new key > JSON* (download it), then **delete** the old key `60ba43bf...` |
| Gmail app password of das.ravik001@gmail.com | Google Account > Security > App passwords: delete it (create a new one later for the outreach phase) |
| Instagram passwords (plantparlour3) | Instagram > Settings > Password |
| Google API key `AIzaSyAWKV...` and Apollo key | Google Cloud Console > APIs & Services > Credentials (delete/regenerate); Apollo settings |
| Meta page access token | Meta for Developers > your app > regenerate |

The new system never stores secrets in files in the repository.

## 1. Create the Google Sheet

1. Open <https://sheets.new> and name the sheet e.g. **Plant Parlour Leads v2**.
2. Click **Share** and add `lead-sync-bot@plant-parlour-automation.iam.gserviceaccount.com`
   as **Editor** (untick "Notify people").
3. Copy the sheet **ID** from the address bar. It's the long code between `/d/` and `/edit`:
   `https://docs.google.com/spreadsheets/d/`**`1AbC...xyz`**`/edit`

The system creates the tabs **Leads**, **Plan** and **Daily Report** by itself.
You may add your own columns to the right of *Status* and edit *Status* freely.
Don't insert columns in the middle of the Leads tab; the system will refuse to
write rather than overwrite anything.

## 2. Where it runs

The system runs on the owner's **Android tablet** (since 9 October 2026; GitHub disabled Actions on the
account). The tablet setup asks for the sheet address and the key file from step 0/1 and keeps them on the
tablet only: **[TABLET.md](TABLET.md)**.

GitHub keeps the code. Its workflows (daily lead generation, outreach, e-mail hunt, probe) are manual-only now and
are not needed for daily work; the GitHub secrets (`GOOGLE_SERVICE_ACCOUNT_JSON`, `PP_SHEET_ID`, `PP_STATE_KEY`, ...)
are only used if one of them is started by hand. Don't start them while the tablet runs the campaign.

## 3. What you see in the sheet

* **Leads**: one row per business with phones, WhatsApp, emails, Instagram,
  Facebook, LinkedIn, contact person (when the business's website names its
  owner/founder), website, Google Maps link, priority, USP and *Contact Sources*
  (where each detail was found).
* **Plan**: the 30 parts, their dates, status and leads found.
* **Daily Report**: one line per run (searches, new leads, contact coverage, warnings).

## 4. Running something now

On the tablet: `pp run daily` (today's lead generation), `pp run outreach`, `pp status`. See [TABLET.md](TABLET.md).

## 5. Changing the campaign

Edit `config/plant-parlour.toml` on GitHub (open the file > pencil icon > Commit). The tablet takes the change
at its next morning update, or at once with `pp update`:

* `daily_target` (`"auto"` by default = area total / days, about 1,000/day for Kolkata;
  or a fixed number like 150), `days`, `radius_km`, `center`, categories and their
  search words, the list of chains to skip, time budget.
* `role_email_candidates` (on by default): for a lead that has its own website but no
  published e-mail, the system adds `info@`/`contact@` as **unverified** candidates
  (only when the domain can receive mail). They appear in *Other Contacts (unverified)*,
  never in the *Emails* column - verify before using them. Set to `false` to switch off.
* Changing the **area or number of days** needs a re-plan: after the tablet has the new config (`pp update`),
  run `pp replan` on the tablet. Leads already found are kept. Adding or
  removing **categories** or search words does not: the next run adds the new
  searches to every part (including finished ones) and drops removed ones.
* `[compliance] mode`: `"open-data"` (default) uses only openly licensed data
  (Overture Maps, OpenStreetMap) and the businesses' own websites, within
  Google's and Meta's terms. `"hybrid"` keeps that but also searches the web and
  Instagram for more contacts. `"standard"` also scrapes Google Maps (ratings,
  a few more places) - more complete but against Google's terms; run it from the
  tablet, as Google throttles GitHub's data-centre IPs. Switch only if you accept
  the tradeoff.
* Adding/removing categories: the pilot now covers cafes, restaurants, banquet
  halls, event planners, interior designers, landscapers, nurseries/florists,
  salons & spas, gyms, clinics, corporate offices, hotels and coworking spaces.
  Turn `real_estate` or `education` on (set `enabled = true`) for even more.
* For a new city or client, copy the file (e.g. `config/client-b.toml`) and
  give it its own sheet. (One campaign per repository copy is simplest.) Set
  `aliases` (other names of the city) and `local_landline_prefixes` (the
  region's landline codes, `+913` for West Bengal) in `[area]`.

## 6. Outreach (e-mail + WhatsApp)

Contacting the leads automatically has its own one-time setup (a Gmail app password, asked by the tablet
setup). Automatic e-mails stay paused until you have read the test e-mails.
See **[OUTREACH.md](OUTREACH.md)**.

## 7. The tablet (where the system runs now)

Since 9 October 2026 the system runs on the owner's Android tablet, not on GitHub Actions (GitHub disabled
Actions on the account, and its free runners are meant for building and testing code). Step-by-step setup,
the `pp` command, updates and troubleshooting: **[TABLET.md](TABLET.md)**. GitHub keeps the code and runs the
tests on every push; its workflows are manual-only now.

## 8. Keeping an eye on it

* Daily: glance at the **Daily Report** tab. The tablet e-mails you when a job has a problem twice in a row
  or an update was undone; `pp status` on the tablet shows every job's last run and the next one.
* `Health` column: `ok` means a source answered normally; `paused` means it was temporarily blocked and the
  run used fallbacks or deferred the work.
* GitHub runs the **Tests** on every change to the code (when Actions works on the account); the tablet runs
  the same tests before it accepts any update.

## About the repository being public

The repository is public, so it never holds secrets or lead data: the passwords, the Google key and the
campaign memory live on the tablet only (in Termux's private storage), and the logs never print full phone
numbers or e-mails.

## Branches

The work lives on branch `ccr-3354cfa7-vsl4cn`, the repository's default branch. The tablet always follows
the default branch: if you later merge into `main` and make `main` the default (Settings > General), the
tablet switches to it at its next update.
