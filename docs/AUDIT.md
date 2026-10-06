# Audit of the previous systems, and what v2 changes

Reviewed: the Antigravity pipeline (all 30 source files plus the two code dumps
and SYSTEM_ARCHITECTURE.md) and the ChatGPT "Agency Lead Lab Hybrid 0.7.0"
package (786 files; its 506 offline checks pass).

## Antigravity system: it ran, but produced unreliable data and broke easily

| # | Problem (file) | Effect | v2 |
|---|---|---|---|
| 1 | `fix_leads.py` asks an AI: *"If you don't know it with 100% certainty, invent a highly realistic Indian mobile number"* and writes the answer into the sheet | **Fake phone numbers in the sheets** | No AI in the data path; every value has a source URL; the test suite asserts that every stored value exists in the source data |
| 2 | `verifier_subagent.py` takes the first Google Places / Apollo result for a name, no location check | Another business's phone attached to a lead | Phones come from the business's own Maps listing or website; search matches need a name match and must not be in another city |
| 3 | `lead_generation_6am.py` dorking names leads `keyword.title()` ("Cafe") and takes emails from search snippets | Junk names, misattributed emails | Discovery only from Maps listings (real names, place ids); snippet contacts are kept apart as "unverified" |
| 4 | Scraper takes the first email/phone anywhere on a page (incl. "designed by" footers); phone regex ignores landlines | Web-agency contacts, missed landlines | Agency credits excluded; tel:/mailto:/JSON-LD first; text numbers need context ("call", "phone"...); libphonenumber validation and mobile/landline labels |
| 5 | DuckDuckGo as the lead source | Mostly "Top 10" blog pages; rate-limited, some days 0 leads | Google Maps search (structured business records) with fallbacks |
| 6 | `contact_verifier.py`: SMTP probing from a home IP; on any error the email is accepted as "CATCH_ALL" | Unreliable verification, IP reputation risk | No SMTP probing; DNS MX check only (an unknown result never drops an email) |
| 7 | `sitecustomize.py` globally monkey-patches networking: up to 15 retries × up to 120 s, nested with 5 socket retries | A single request could hang for 30+ minutes | One retry, short waits, timeouts everywhere, run deadline |
| 8 | `pipeline_orchestrator.py`: 60-120 min sleeps between stages; a failed "critical" stage skips the rest of the day | Android kills the long process, so the whole day is lost (the "one link breaks the chain" problem) | Durable task queue, no long sleeps, every failure isolated, resume after a crash |
| 9 | `verifier_subagent.py` rewrites the entire sheet (`ws.update(data)`); IDs are row counts | Concurrent edits lost; duplicate IDs | Upsert by key; your edits and Status are preserved; stable Lead IDs |
| 10 | `zone_memory.py` advances a day per invocation | Re-runs skip territory | Calendar-derived schedule |
| 11 | `jules_merger.py` auto-merges AI-written PRs into the running system and force-pushes rollbacks | Unreviewed code changes in production | Retired; CI tests on every push; no self-modifying code |
| 12 | `metacognitive_observer.py`, `linkedin_dispatcher.py` import modules that don't exist; `whatsapp_dispatcher.py` has a syntax error (line 210) | Dead code | Not carried over (outreach is a later phase) |
| 13 | Passwords, app passwords, API keys and tokens hard-coded in many files | Credentials exposed | Secrets only in environment/GitHub secrets; see SETUP.md step 0 |
| 14 | `dispatcher_subagent.py` sends every draft in the Gmail Drafts folder | Could send personal drafts | Outreach phase will use its own outbox (not built yet) |

**Kept from Antigravity:** the zone/category idea, Kolkata localities and
category keywords, contact-page follow-up, the familiar Google Sheet as the
output, and cron-style daily scheduling.

## ChatGPT Hybrid 0.7.0: careful, but found almost nothing

| Problem | Effect | v2 |
|---|---|---|
| Discovery from OpenStreetMap only (Google Maps path needed paid Firecrawl credits) | Thin coverage of Kolkata businesses; the bounded live trial found 1 candidate and 0 contacts | Google Maps search, free, verified live |
| A business without a website was "held for manual review" | No Instagram/Facebook fallback chain | Web search finds Instagram/Facebook/LinkedIn and websites |
| Daily caps of 120 web requests and 6 discovery requests; 3 zones of 4 km | Could not reach 100-200 leads/day or cover 100 km | Squares over the full 100 km circle; target-driven daily loop |
| No Google Sheets output | Manual CSV import | Direct Sheets upsert |
| Four overlapping code generations, nine START-HERE files | Hard to operate on a tablet | One package, one CLI, one config file |

**Kept from ChatGPT:** SQLite as the source of truth with transactional task
results, crash recovery, calendar-based scheduling, evidence per contact,
name/branch checks before trusting a contact, robots.txt handling, response
size caps, formula-safe CSV export, "never invent a URL or contact".
