# Plant Parlour on the Android tablet

The whole system - new leads every morning, the e-mail hunt, USP lines, outreach e-mails, the WhatsApp queue and
reply alerts - runs on your tablet, by itself, and writes to the same Google Sheet as before. Nothing runs on GitHub
any more: GitHub only keeps the code, and the tablet fetches new versions from it every morning.

**What you need:** the tablet plugged in and on Wi-Fi, about 1.5 GB of free space, and 30-45 minutes once.
Have these ready:

| You need | Where it is |
|---|---|
| **PP_STATE_KEY** | the long password you made up when you set up GitHub (e.g. six words with dashes) - in your notes |
| Your Google Sheet's address | open the sheet in the browser, copy the address bar |
| Gmail App Password (16 letters) | the one you created for the outreach Gmail - or create a new one (step 3) |
| Your WhatsApp Business number | |

## Step 1 - Install the apps (5 minutes)

1. In the tablet's browser open **https://f-droid.org**, download F-Droid and install it (Android asks to allow
   installing apps from the browser: allow it).
2. In F-Droid search for and install these four (all free, all from F-Droid - don't mix with the Play Store versions):
   * **Termux** - the terminal the system runs in
   * **Termux:API** - lets Android check every 15 minutes that the system is running
   * **Termux:Boot** - starts the system after the tablet restarts
   * **Termux:Widget** *(optional)* - home-screen buttons
3. Open **Termux:Boot** and **Termux:API** once each (just open and close them).

## Step 2 - Copy the code (2 minutes)

Open **Termux**, wait until it shows a `$` prompt, then paste this line (long-press > Paste) and press Enter:

```
pkg update -y && pkg install -y git && git clone https://github.com/dasravik444-cpu/plant-parlour-v2 ~/plant-parlour-v2
```

If it asks a question, press Enter. It ends with "Resolving deltas: 100%" or similar.

## Step 3 - Put the Google key and the Gmail App Password ready (5 minutes)

**Google key file.** In the tablet's browser (signed in with the Google account that owns the project):
1. Open **https://console.cloud.google.com/iam-admin/serviceaccounts** and pick the project
   *plant-parlour-automation*.
2. Tap **lead-sync-bot@...** > **Keys** > **Add key** > **Create new key** > **JSON** > **Create**.
   A `.json` file downloads to *Downloads*. The setup finds it there, keeps it safely inside Termux and offers to
   delete it from Downloads.

**Gmail App Password.** If you didn't keep the 16 letters: sign in to the outreach Gmail in the browser, open
**https://myaccount.google.com/apppasswords**, create one called "Tablet" and copy the 16 letters.

## Step 4 - Download the campaign memory from GitHub (2 minutes)

This is what lets the tablet continue exactly where GitHub stopped (which areas are done, every lead's details,
the outreach records). In the tablet's browser, signed in to GitHub (use *Desktop site* in Chrome's menu if the
page looks cut short), open these two links and tap the download button on each:

* https://github.com/dasravik444-cpu/plant-parlour-v2/actions/runs/37828475923/artifacts/11575495220 - **pp-state.zip**
* https://github.com/dasravik444-cpu/plant-parlour-v2/actions/runs/37828530389/artifacts/11572820881 - **pp-outreach.zip**

Both land in *Downloads*. They are encrypted; only your PP_STATE_KEY opens them.

## Step 5 - Run the setup (20-30 minutes, mostly waiting)

In Termux paste:

```
bash ~/plant-parlour-v2/scripts/tablet/setup.sh
```

* Android asks whether Termux may use your files: tap **Allow**.
* It installs Ubuntu and Python inside Termux (about 250 MB), then asks you:
  1. the **Google Sheet** address,
  2. nothing for the **key file**: it finds it in Downloads by itself and asks whether to delete it there (yes),
  3. the **outreach Gmail** and its **App Password** (typing is hidden),
  4. your **WhatsApp number**, and an e-mail for reply alerts (press `-` for the outreach Gmail itself),
  5. your **PP_STATE_KEY** to open the memory files.
* It checks each one (`[OK]` or `[!!]` with the reason), runs all the system's tests on the tablet, and starts.

If anything stops halfway (Wi-Fi drop, a typo), just paste the same command again: finished steps are skipped.

## Step 6 - Three Android settings (3 minutes, once)

1. **Settings > Apps > Termux > Battery > Unrestricted** (on Samsung: *Battery > Unrestricted* and remove it from
   *Sleeping apps*). Do the same for **Termux:API** and **Termux:Boot**. Without this Android stops it at night.
2. Keep the **Termux notification** - never tap *Exit* in it (that stops everything).
3. *Recommended on Android 14 or newer:* **Settings > About tablet > Software information**, tap **Build number**
   7 times (developer options on), then **Settings > Developer options > Disable child process restrictions: on**.
   This stops Android from closing long runs. (On Android 12-13 skip it; the system restarts itself if closed.)

Keep the tablet **plugged in and on Wi-Fi**. The screen can be off.

## Step 7 - The 2 test e-mails

Between 10:00 and 18:30, Monday to Saturday, in Termux:

```
pp test-email
```

It shows both e-mails exactly as they will go out (to whom, subject, text) and sends them only when you type `y`.
Tell me what you think of them; automatic sending stays paused until you say go.

## Every day

Nothing to do on the tablet. Look at the Google Sheet as before (Leads, Daily Report, WhatsApp Queue, Replies).
Reply alerts arrive by e-mail. If a job has a problem twice in a row, or an update is undone, the tablet e-mails you.

| India time | What the tablet does |
|---|---|
| 05:15 | checks for a new version (tests it; keeps it only if everything passes) |
| 06:00 | the day's new leads, then the e-mail hunt and USP lines for them |
| 10:11 - 18:11, hourly, Mon-Sat | outreach: e-mails (when switched on), WhatsApp queue, replies |
| 11:30, 16:30, 21:30 | follow-ups: unfinished website reading, e-mail hunt, USP lines |
| 23:30 | backup of the campaign memory (last 7 days kept on the tablet) |

If the tablet was off at one of these times, the job runs as soon as it is back.

## The `pp` command

Type `pp` in Termux for a menu, or:

| Command | What it does |
|---|---|
| `pp status` | is it running, what ran (OK / problem), what runs next, today's numbers |
| `pp logs` | today's log (`pp logs 300` for more, `pp logs live` to watch it) |
| `pp update` | get the newest version now (tested first, undone if anything breaks) |
| `pp rollback` | go back to the version before the last update |
| `pp test-email` | the next 2 outreach e-mails: shown first, sent after your yes |
| `pp settings` | change the sheet, key, Gmail, App Password, WhatsApp number |
| `pp off outreach` / `pp on outreach` | pause / resume outreach (also `leads`, `live`, `updates`) |
| `pp stop` / `pp start` | stop everything / start again (a stop also lasts through a restart) |
| `pp run daily` | run today's lead generation now (`followup`, `outreach`, `backup` too) |
| `pp import` | load memory files from Downloads again |
| `pp check` | test the Google Sheet and Gmail connections |
| `pp replan` | only after changing the area or the number of days in the config (leads are kept) |
| `pp setup` | repair the installation (the setup again) |

With Termux:Widget, add the Termux widget to the home screen: buttons *PP Status*, *PP Update*, *PP Logs*,
*PP Test e-mails*, *PP Start*.

## Updates

* **Automatic, every morning at 05:15.** The tablet downloads the newest version from GitHub, runs all the tests,
  and keeps it only if nothing new fails. If something fails it goes straight back to the previous version, keeps
  running, and e-mails you. It does not try that broken version again; the next fixed one is taken as usual.
* **Now:** `pp update`. **Undo:** `pp rollback`. **Switch automatic updates off:** `pp off updates`.
* **Changing a setting in the campaign config** (`config/plant-parlour.toml`, e.g. the e-mail texts or
  `[outreach.email] enabled`): edit it on GitHub in the browser (pencil icon > Commit). The tablet takes it at the
  next update, or right away with `pp update`.
* Your passwords and keys are never in the code folder, so an update can't touch them.

## Good to know

* **Don't uninstall Termux** - that deletes everything in it, including the campaign memory. To keep a copy outside
  Termux: `cp -r ~/.plant-parlour/backups ~/storage/downloads/plant-parlour-backups` (the copy holds lead contacts:
  keep the tablet locked).
* Your passwords and the Google key live in `~/.plant-parlour` inside Termux, which other apps can't read.
* Nothing is scheduled on GitHub any more. Don't start the GitHub workflows by hand while the tablet runs
  (two systems e-mailing the same leads).
* Everything stays free: Termux, F-Droid, Ubuntu and all the parts are free and open source.

## If something is wrong

| You see | Do this |
|---|---|
| `pp: command not found` | `bash ~/plant-parlour-v2/scripts/tablet/setup.sh` |
| `pp status` says NOT RUNNING | `pp start`; check the battery setting (step 6) |
| `[!!] Google Sheet NOT reachable ... 403` | share the sheet with the key's e-mail (lead-sync-bot@...) as Editor |
| `[!!] Gmail login FAILED` | `pp settings` and enter a new App Password (16 letters) |
| "wrong PP_STATE_KEY" | check your notes for the exact password and run `pp import` |
| An e-mail "update was undone" | nothing to do; tell me, I'll fix it and the next update takes the fix |
