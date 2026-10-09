# Architecture

## Principles

1. **Evidence or nothing.** A contact is stored only if it was read literally
   from a public source. Each contact row keeps `source`, `source_url`,
   `confidence`, `evidence` and every corroborating source. There is no LLM in
   the data path.
2. **Durable, isolated tasks.** All work is rows in SQLite (`tasks`). A task's
   result and the follow-up tasks it creates are committed in one transaction.
   A failure is recorded on that task (with back-off) and never stops other tasks.
3. **One retry owner, bounded waits.** HTTP requests retry at most once with
   short waits; longer back-off belongs to the queue. Nothing can hang a run
   (every request has a timeout and a size cap; the run has a deadline).
4. **Degrade, don't die.** Circuit breakers pause a service that blocks us
   (Google Maps, Instagram, each search engine, each Overpass mirror). The
   provider chain falls back (Google Maps → Places API → OpenStreetMap); work
   that cannot be done now is deferred without burning retries.
5. **Calendar, not counters.** Which part is scheduled for a day is derived
   from the start date, so re-runs and missed days never skip territory.
6. **Always report.** Every run ends with Sheets sync and a report, even when
   cut short; important failures make the run exit non-zero, and GitHub emails you.
7. **Emails: observed, or clearly flagged.** The *Emails* column holds only addresses
   read from a public source. For a lead with its own website but no published e-mail,
   `info@`/`contact@` candidates (MX-checked) go in the *unverified* column, labelled as
   guessed - never presented as confirmed.
8. **No silent zeros.** An empty Google Maps answer before Maps has returned
   anything in the run is not trusted (it may be a format change or a soft
   block): the search is kept for later, and ten in a row stop discovery and
   turn the run red. Result records that no longer parse count as errors.

## Agents

| Agent | Module | Input → output |
|---|---|---|
| Planner | `planner.py`, `geo.py`, `providers/osm.py` | config → parts, search squares, `search` tasks |
| Discovery | `providers/overture.py` (open-data/hybrid, default), `osm.py`; `gmaps.py`, `places_api.py` (standard mode) | square + category → places |
| API enrichment | `providers/fsq.py` (optional, key-gated) | lead → official Foursquare phone/website/socials |
| Quality | `quality.py`, `runner._ingest` | places → kept / excluded (closed, chain, off-category, duplicate, outside area) |
| Website | `enrich/website.py`, `enrich/extract.py` | site → emails, phones, WhatsApp, social links, description |
| Social finder | `enrich/social.py`, `enrich/search.py` | business name → Instagram/Facebook/LinkedIn page, website |
| Instagram | `enrich/instagram.py` | handle → bio contacts (best effort) |
| Recorder | `sheets.py`, `report.py` | database → Leads / Plan / Daily Report tabs, CSV |
| Observer | `runner._summary`, `report.py` | run → JSON report, job summary, warnings, exit code |
| Orchestrator | `runner.py` | runs the loop with the time budget and the stopping rule |

## Daily loop

```
recover interrupted tasks -> ensure plan -> scheduled part for today
loop until deadline or stop signal:
    harvest finished enrichment results (DB writes happen only in the main thread)
    if new_leads_today < target  OR  searches of today's part (or earlier ones) remain:
        run the next search (plan order) -> ingest places -> queue enrichment
    keep up to 2 x workers enrichment tasks running in a thread pool
    every 20 minutes: copy new/changed lead rows to the sheet (checkpoint)
    stop when there is nothing left to do now
drain in-flight work (bounded), put unfinished tasks back in the queue
refresh part status -> Google Sheets upsert -> Plan tab -> Daily Report row -> JSON + summary
```

Enrichment chain per business:

```
Maps website?  yes -> crawl site (home + <=3 contact/about pages, robots.txt respected)
               no / listing site / social profile -> web search (also looks for the website)
after the site: no Instagram yet -> web search for Instagram/Facebook (and website)
Instagram found and no email -> Instagram profile (bio/business email), switched off on first login wall
B2B categories (designers, planners, venues, hotels, coworking) -> LinkedIn company page search
website found via search/Instagram -> crawl it too
```

A business becomes a **lead** when it has at least one phone, WhatsApp, email
or Instagram that is not low-confidence. Low-confidence findings (e.g. a number
seen only in a search snippet) go to the *Other Contacts (unverified)* column.

## Accuracy rules (from reviewing real rows)

* **Search results must be profile pages.** A link to a post, photo or video is
  never reduced to its account: in search results that account is often a food
  blogger or a guest, not the business.
* **Names match word by word.** Every distinctive word of the business name must
  appear (in the profile name or at word boundaries in the handle). Spelling
  variants may differ only in vowels or doubled letters (Bangali/Bengali,
  Dawat/Daawat), not in consonants (Arabiya is not Arabica). SEO tails in
  Maps names ("- Best Banquet Hall in Kolkata") are ignored.
* **Common words need local proof.** If the only matching word is a common
  English word or very short ("Natural", "Empire", "Zoi"), the profile must
  mention the city or locality; otherwise it is stored as unverified.
* **The business's own word wins.** If Google Maps or the business's website
  names an Instagram/Facebook account, a different account found by search is
  stored as unverified.
* **Hijacked domains are ignored.** Expired domains taken over by gambling
  sites are detected; the site is dropped as the website and a search looks for
  the real one.
* **The site must belong to the business.** If neither the business name
  (title, site name, structured data, domain) nor the phone from the listing
  appears on a site (e.g. a parent company's site), its contacts are unverified
  and its description is not used.
* **Foreign numbers** on an Indian business's website are unverified. Landlines
  from another region (Delhi +91 11...) are labelled as likely booking-platform
  lines; Indian mobile ranges are labelled *mobile*.
* **Plain-text e-mails on the business's own site** count when they carry the
  site's domain or the business's name (`leafcafe.kolkata@gmail.com` on
  leafcafe.in). Other addresses written there (a supplier's, the web
  designer's) stay unverified.

## E-mail hunt (leads still without an e-mail)

`python -m leadgen email-hunt` (part of every daily run, and the manual workflow
*E-mail hunt*) looks once more at each lead without a usable e-mail:

0. **Other listings with the same phone number** in the open data: with a
   similar name it is the same business listed again (e.g. its Facebook page
   and its own listing) and its e-mail/website is used; a different name (a
   shared switchboard number) only gives an unverified note.
1. **Its own website, deeper:** privacy/terms pages and the sitemap's contact
   pages; http/https and with/without www when it does not answer; robots.txt
   that cannot be read is retried (not reported as a refusal); e-mails kept in
   site-builder settings.
2. **A website the listing lacks:** the obvious domains for its name
   (`kanchanbakery.com/.in/.co.in`, with and without "kolkata") are checked
   with one DNS lookup each. A site is accepted only when it shows the
   business's own phone number (homepage or contact page), or its exact name
   with its PIN code. Parked and for-sale domains are rejected. Then it is read
   like step 1.

Nothing is guessed: every e-mail kept comes from a page of the business's own
site (the page is its source) or from the open data.

Measured on the pilot (8 Oct 2026, all 2,054 leads, checked by hand row by row):
52.7% -> 55.9% of leads with a usable e-mail. Of the leads still without one, 81%
have no website and no e-mail on their Facebook page (small restaurants, hotels,
salons, bakeries); a manual web search for samples of them found none either.
Directory pages, chain sites and shared phone numbers are handled as described in
the rules below (the first trials attributed chain hotels' and OYO properties'
addresses wrongly; those rules came from that review). Small businesses that have
no website and no e-mail on their Facebook page usually have no public e-mail
at all; for them the phone/WhatsApp is the route.

## Open-data mode (default)

`[compliance] mode = "open-data"` restricts the system to sources whose licences
allow storing and re-using the data, and to the businesses' own websites:

* **Overture Maps places** (CDLA-Permissive-2.0 / Apache-2.0; Meta business pages,
  Microsoft, Foursquare, AllThePlaces...). `providers/overture.py` lists the
  latest release on the public S3 bucket, reads only the campaign's bounding box
  and the configured category codes (`overture = [...]` per category; codes or
  `basic_category`, `*` patterns allowed) with DuckDB, and stores the rows in the
  `open_places` table. The extract is refreshed when a newer release exists and
  the last check is older than `open_data.refresh_days`. If a refresh fails, the
  stored extract is used; if there is none, discovery falls back to OpenStreetMap.
* Each search task (square + category) is answered from `open_places` (the square
  is widened by 2% because neighbouring squares can leave hairline gaps; repeats
  merge by place id). Secondary queries of a category return nothing.
* Phones, emails and social links from Overture are stored with source `overture`,
  the Facebook page (or overturemaps.org) as source URL and the record id and
  datasets as evidence. A Facebook page from Meta's dataset counts as high
  confidence. The brand field feeds the chain filter.
* Websites are crawled with `User-Agent: PlantParlourLeadBot/2.0 (+repository URL)`,
  no TLS/browser impersonation, and robots.txt is evaluated for that bot name.
* Search-engine lookups, Instagram profile reads and Google Maps are disabled.

Measured 2026-10-06 for 100 km around Kolkata: 209,282 places; 152,758 with a phone,
82,929 with an email, 84,091 with a website, 183,220 with a social link.

## Search squares and parts

* Business density from OpenStreetMap (shops, offices, cafes, hotels...)
  drives a quadtree: a square splits while it holds more than 60 mapped businesses (down to
  1 km) and always splits above 16 km. Without OpenStreetMap a centre-weighted
  estimate is used, so a plan is always produced.
* Squares are ordered along a Hilbert curve and cut into N groups of equal
  expected yield; groups are ordered centre-outwards and named after the main
  OpenStreetMap localities, later refined by the neighbourhood labels Google
  Maps returns.
* Secondary queries of a category ("rooftop restaurant") only run where the
  primary query ("restaurant") filled a page; Maps paging stops when results are
  mostly already known; sparse categories (coworking) skip almost-empty squares.

## State

`state/leadgen.sqlite`: `parts`, `cells`, `tasks`, `places`, `contacts`, `runs`,
`budget`, `events`, `meta`. On the tablet it lives in `~/.plant-parlour/state/` (Termux's private storage,
with a nightly copy kept for 7 days); `python -m leadgen tablet import` loads the encrypted copy GitHub saved
(same AES format as below). Until 9 Oct 2026, on GitHub Actions, it was restored before and saved
after each run as an AES-256 encrypted artifact (`scripts/ci/state.sh`, 90-day
retention, refreshed daily). The restore reads every artifact page and takes
the newest one of the branch; if listing, download, decryption or the integrity
check fails, the run stops and nothing is saved, so a good saved state is never
replaced by a fresh or broken one. Losing it is not a disaster: sheet rows are
matched by key, so a fresh database updates existing rows instead of
duplicating them (and adopts their Lead IDs).

## Data columns (Leads tab)

`Lead ID, Date Added, Business Name, Category, Area, Address, Phones, WhatsApp,
Emails, Instagram, Facebook, LinkedIn, Contact Person, Website, Google Maps, Rating,
Priority, Description, Contact Sources, Other Contacts (unverified), Plan Part, Last
Updated, Key, Status`. The system writes *Status* only for new rows and never
touches columns to its right. *Contact Person* holds an owner/founder name only when
the business's own website states it explicitly (structured-data `founder`,
"Founder: X", "X, Owner", "Founded by X"); it is never guessed. A sheet created
with an older column layout is upgraded in place (columns inserted, data kept).

## Live-verified behaviour (2026-10-06, GitHub Actions)

* Google Maps `tbm=map`: HTTP 200 with 20 valid records per page; paging via
  `!8i<offset>` returns new records; phones on 65-100% of listings depending on
  category; the record layout matched the parser (name [11], coords [9],
  place id [78], phone [178], website [7], address [39], area [14], rating/reviews [4]).
* Yahoo search answered consecutive queries; DuckDuckGo returned HTTP 202 after
  the first query; Bing/Mojeek/Brave/Startpage served challenges or 403/429.
* Instagram `web_profile_info`: HTTP 401; profile pages redirect to login.
* Overpass: 1 of 3 public mirrors answered (slowly).
* Google Sheets: tab creation, RAW writes (no formula injection), in-place
  updates, Status preservation and cleanup verified on temporary tabs.

## Running on the tablet (leadgen/tablet.py, scripts/tablet/)

Termux runs a small supervisor (`service.sh`) that holds Android's wake lock and keeps the scheduler
(`python -m leadgen tablet serve`) running inside Ubuntu 24.04 (proot-distro container `pp-ubuntu`, which sees the
Termux home as `/root`). The scheduler runs the GitHub workflows' steps at fixed India times, one job at a time
(a file lock also stops a job started by hand from overlapping one that is running): the daily run and its
follow-ups (`run`, `email-hunt`, `usp-refresh`), hourly `outreach` (Mon-Sat), a nightly backup and the morning
update. A slot missed while the tablet was off runs once when it is back. Termux:Boot starts it after a reboot and
an Android job (Termux:API) checks every 15 minutes that it is running; `pp stop` is respected by both.

Updates: `git fetch` of the repository's default branch, then `checkout`, `pip install` when the requirements
changed, and three checks - the code compiles, the config loads, and the whole test suite shows no failure that
was not already failing on this tablet (recorded at setup). Otherwise the previous version is checked out again,
the failed version is remembered and never retried, and the owner gets an e-mail. Files changed on the tablet are
never overwritten (`--discard-local` keeps them as a patch). The owner's settings, key and memory sit outside the
code folder.
