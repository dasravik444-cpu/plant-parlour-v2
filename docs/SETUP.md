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

## 2. Add the secrets on GitHub

Repository **dasravik444-cpu/plant-parlour-v2** > **Settings** > **Secrets and variables** > **Actions** >
**New repository secret**:

| Name | Value |
|---|---|
| `GOOGLE_SERVICE_ACCOUNT_JSON` | the **entire content** of the new JSON key file (open it in a text editor, copy everything) |
| `PP_SHEET_ID` | the sheet ID from step 1 |
| `PP_STATE_KEY` | a long random password, e.g. six random words. **Save a copy in your password manager.** It encrypts the campaign memory; if it is lost, the campaign memory starts fresh (the leads already in the sheet are safe and are not duplicated). |
| `GOOGLE_PLACES_API_KEY` *(optional, standard mode)* | official Google Places API key, used only in standard mode if Google Maps blocks the free method. |
| `FOURSQUARE_API_KEY` *(optional)* | free Foursquare Places API key (<https://foursquare.com/developers>). When set, fills missing website/phone/social for leads. Optional - it adds little over the open data. |

## 3. Turn on the daily schedule

Same page, tab **Variables** > **New repository variable**: name `PP_ENABLED`, value `true`.

From now on GitHub runs the system **every day at 06:07 IST**, with follow-up
runs every 3 hours (09:07, 12:07, 15:07, 18:07, 21:07 IST) that finish the day's
part and the enrichment work (websites, contact pages) and stop within a minute
or two when nothing is left. When GitHub is busy it delays scheduled runs,
sometimes by hours, and occasionally drops one; whichever slot arrives first does
the day's work. If a run fails, GitHub emails the repository owner.

## 4. First run (recommended now)

**Actions** tab > **Daily lead generation** > **Run workflow** > keep the defaults > **Run**.
It takes up to about 80 minutes. Then open your sheet:

* **Leads**: one row per business with phones, WhatsApp, emails, Instagram,
  Facebook, LinkedIn, contact person (when the business's website names its
  owner/founder), website, Google Maps link, priority and *Contact Sources*
  (where each detail was found).
* **Plan**: the 30 parts, their dates, status and leads found.
* **Daily Report**: one line per run (searches, new leads, contact coverage, warnings).

The run's own page (Actions > the run > *Summary*) shows the same daily report.

## 5. Changing the campaign

Edit `config/plant-parlour.toml` (on GitHub: open the file > pencil icon):

* `daily_target` (`"auto"` by default = area total / days, about 1,000/day for Kolkata;
  or a fixed number like 150), `days`, `radius_km`, `center`, categories and their
  search words, the list of chains to skip, time budget.
* `role_email_candidates` (on by default): for a lead that has its own website but no
  published e-mail, the system adds `info@`/`contact@` as **unverified** candidates
  (only when the domain can receive mail). They appear in *Other Contacts (unverified)*,
  never in the *Emails* column - verify before using them. Set to `false` to switch off.
* Changing the **area or number of days** needs a re-plan: run the workflow
  manually with **replan = true**. Leads already found are kept. Adding or
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

Contacting the leads automatically is a separate workflow with its own one-time setup (a Gmail app
password and two GitHub variables). It starts in dry-run so you can read every e-mail first.
See **[OUTREACH.md](OUTREACH.md)**.

## 7. The tablet (optional backup)

The tablet can run the same system when GitHub is unavailable, or as your main
runner if you prefer. Use Ubuntu inside Termux (proot-distro) so the `duckdb`
package installs; it reads the Overture Maps open data.

In Termux (or Ubuntu inside Termux):

```bash
pkg install git          # (or: apt install git)
git clone https://github.com/dasravik444-cpu/plant-parlour-v2
cd plant-parlour-v2
bash scripts/tablet/install.sh
nano ~/.plant-parlour/secrets.env      # fill PP_SHEET_ID and GOOGLE_SERVICE_ACCOUNT_FILE
bash scripts/tablet/run_daily.sh --budget-minutes 10 --max-searches 5   # small test
```

Then schedule it with cron as shown at the end of the installer. It runs hourly
and stops within seconds once the day's work is done. Keep Termux awake
(`termux-wake-lock`, disable battery optimisation for Termux).

**Run it in one place at a time.** The tablet keeps its own campaign memory.
If both run, the sheet still never gets duplicate rows, but some searches are
done twice.

## 8. Keeping an eye on it

* Daily: glance at the **Daily Report** tab, or the email from GitHub if a run failed.
* `Health` column: `gmaps:ok` means Google Maps answered normally; `paused` means
  it was temporarily blocked and the run used fallbacks or deferred the work.
* Live diagnostics any time: Actions > **Live source probe** > Run workflow (checks Google Maps,
  websites, search engines, Instagram and OpenStreetMap from GitHub's servers).
* Actions > **State persistence self-test** proves the encrypted save/restore chain works.
* Actions > **Tests** runs automatically on every change (offline test suite).
* GitHub pauses schedules in public repositories after 60 days without a commit.
  The workflow refreshes itself every morning to prevent that (job *keepalive*).
  If runs ever stop anyway, open Actions > **Daily lead generation** and click
  **Enable workflow**.
* If the campaign memory can't be restored (for example after `PP_STATE_KEY`
  was changed), the run stops with a red cross and the saved memory is left
  untouched. Put the old key back, or delete the `pp-state` artifacts to start fresh.

## About the repository being public

This repository is public, which gives unlimited free GitHub Actions minutes.
The campaign memory is stored **encrypted** (with `PP_STATE_KEY`), and the
logs never print full phone numbers or emails. If you make the repository
private, everything still works, but the free plan then allows 2,000 minutes a
month, so lower `time_budget_minutes` to about 55.

## Branches

The work lives on branch `ccr-3354cfa7-vsl4cn`, which GitHub made the default
branch because it was the first one pushed. Schedules run from the default
branch. If you later merge it into `main`, set `main` as the default branch
under Settings > General.
