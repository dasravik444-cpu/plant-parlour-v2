# Outreach: e-mail + WhatsApp

The outreach system reads the **Leads** tab of your Google Sheet and contacts the leads, at no cost:

* **E-mail (automatic).** Personal, plain-text e-mails from a free Gmail account, sent in Indian business
  hours with random gaps, best leads first, with one polite follow-up. Any reply stops the follow-up;
  "no" removes the lead for good. When someone replies you get an alert e-mail, and the reply appears
  in the **Replies** tab with the phone number: call them.
* **WhatsApp (one tap per message).** Every morning the **WhatsApp Queue** tab gets personalised messages
  for the leads e-mail can't reach (no e-mail address, or no answer to the e-mails): numbers the business
  publishes as WhatsApp first, then mobile numbers. 20 a day at first, rising by 10 after every 3 days on
  which you sent them, up to 50 (a new number must warm up, like a new Gmail). On your phone, tap
  *Open Chat*, WhatsApp opens with the message already typed, press send, then pick the *Result* (Sent /
  Not on WhatsApp / ...). A number that is not on WhatsApp is simply marked: it never comes back, and the
  lead's next mobile number (if it has one) is tried the next day. Leads who answer "yes" by e-mail are put
  at the top of the queue: they asked for it.

It runs on the tablet every hour from 10:11 to 18:11 IST, Monday to Saturday, sending until 19:00 (see
[TABLET.md](TABLET.md)).
`pp off outreach` pauses it, `pp on outreach` resumes it.

## Why it works this way

**E-mail volume (owner's target: 300-400 a day, set to 350).** A free Gmail account stops at about 500
e-mails a day (everything sent from it counts), and an account that jumps straight to hundreds of cold
e-mails gets blocked. So the daily limit (first e-mails and follow-ups together) **climbs by itself**:

* it starts at **40** and rises by **30 after every good sending day** up to **350** - about two weeks,
  Monday to Saturday. A good day used at least 80% of its limit with no warning from Gmail and few bounces;
* it **stays put** while more than 4% of e-mails bounce (7 days), after a day with many bounces, and for a
  few days after a warning;
* Gmail's "daily limit" error **pauses sending until tomorrow** and lowers the limit to 60%; the system then
  remembers what this account took and stays under it, trying one step higher once a week. A spam block
  from Gmail pauses 2 days and halves the limit;
* bounces: 10 or more in 24 hours that are at least 10% of what was sent pause sending until tomorrow; 6%
  or more in a week (after 100 e-mails) pauses 2 days and lowers the limit to 70%. Before a first e-mail,
  an address whose domain no longer has a mail server is skipped (it would only bounce);
* every pause e-mails you (an alert after a Gmail limit waits until Gmail sends again) and shows in
  `pp status`; nothing needs doing - it restarts by itself.

At 350 a day with one follow-up 4 days later, about **180 new businesses** get a first e-mail each day. The
leads supply plenty: ~1,000 new leads a day, ~56% with a real e-mail address. Each hourly run sends about a
ninth of the day's limit, 30-80 seconds apart, for at most 45 minutes. E-mails are plain text with **no
links, images or tracking** (links in cold e-mails hurt delivery). Only addresses the business published
itself (website or listing) are used; guessed info@/contact@ addresses stay off, because bounces damage the
account. `scripts/simulate_outreach.py` runs weeks of this against a pretend Gmail (normal data, bad data,
a stricter Gmail limit, a spam block) to check it.

**WhatsApp.** There is no free, allowed way to send WhatsApp messages automatically:

| Option | API key? | Cost (India, Oct 2026) | Automatic sending? |
|---|---|---|---|
| WhatsApp (normal app) | No | Free | No - automating it breaks WhatsApp's terms; numbers get banned |
| **WhatsApp Business app** (use this) | No | Free | No - but catalog, quick replies, labels, greeting/away auto-replies |
| WhatsApp Business Platform (Cloud API) | Yes (Meta Business, system-user token) | Marketing message **₹0.86 + 18% GST each**; replies to people who message you first: 1,000 a month free, then ₹0.115 | Yes - but only to people who agreed to be contacted |

Tools that send "free" bulk WhatsApp (including the old system's Baileys server) automate WhatsApp Web
against its terms; WhatsApp bans such numbers, and it is now testing a monthly cap on messages that get
no reply. Cold messages also need the person's agreement under WhatsApp's Business policy. So the system
does the safe thing: it sends e-mails automatically, turns interested e-mail replies into WhatsApp
conversations (they said yes), and prepares a daily queue, warmed up slowly, for the leads e-mail can't
reach. 500-600 cold WhatsApp messages a day from a new number would get it banned within days, whatever
the tool. What gets a number restricted is people blocking or reporting it, so the messages are personal
(business name, a line for their kind of business), carry no links, and offer an easy "reply STOP".

## One-time setup (about 15 minutes)

1. **A separate Gmail for outreach** (recommended, free), e.g. `plantparlour.kolkata@gmail.com`, so your
   main inbox is never at risk. Give it a profile photo (your logo) - recipients see it, it builds trust.
   Use it normally for a few days (send a few real e-mails) before going live.
2. **App password:** in that account turn on 2-Step Verification, then open
   <https://myaccount.google.com/apppasswords>, create one named "Outreach" and copy the 16 letters.
   (If you ever change the account's password, create a new app password.)
3. **On the tablet** the setup asks for these (change them any time with `pp settings`); they stay in
   `~/.plant-parlour/settings.env` on the tablet, never in the code:

   | Setting | Value |
   |---|---|
   | `OUTREACH_GMAIL_ADDRESS` | the outreach Gmail address |
   | `OUTREACH_GMAIL_APP_PASSWORD` | the 16 letters |
   | `OUTREACH_SENDER_PHONE` | your WhatsApp Business number, e.g. `+91 98XXX XXXXX` (shown under every e-mail) |
   | `OUTREACH_NOTIFY_EMAIL` *(optional)* | where reply alerts go, e.g. your main Gmail. Without it they go to the outreach Gmail |

   The repository is public, so don't put your number or e-mail in its files.
4. **Your name:** `config/plant-parlour.toml`, section `[outreach.sender]`: check `name` (shown as the sender).
5. **Dry-run first:** `pp off live` makes every hourly run fill **Email Preview** with the exact e-mails it
   would send (nothing is sent). Read a few. To change the wording, edit `[outreach.email]` (see below).
6. **Go live:** `pp on live` (the tablet's default). Automatic sending also needs
   `[outreach.email] enabled = true` in `config/plant-parlour.toml` (it is `false` until you give the go-ahead
   after the test e-mails; edit it on GitHub, the tablet takes it at the next update or with `pp update`).
   To stop at any time: `pp off live` (back to dry-run) or `pp off outreach` (everything off).
   **Test e-mails:** `pp test-email` shows the next two e-mails exactly and sends them only after your yes,
   even while automatic sending is paused (10:00-18:30, Monday to Saturday).
7. **WhatsApp Business on the new number:** install *WhatsApp Business* (not normal WhatsApp), and fill in the
   business profile (name Plant Parlour, logo, description, address, hours, website). Add your price list
   to the *Catalog*, create a quick reply `/price` with the price-list message, and turn on the *greeting*
   and *away* messages (Business tools). This is free and makes every chat look professional.

## Your daily routine (about 20-30 minutes)

1. **Reply alerts / Replies tab:** call everyone marked *Interested* the same day (a call converts far better
   than more e-mails), then send the price list on WhatsApp (the *WhatsApp Chat* link is ready).
2. **WhatsApp Queue tab** (on your phone): for each new row tap *Open Chat*, press send in WhatsApp, then set
   *Result*. If WhatsApp says the number is not on WhatsApp, choose *Not on WhatsApp*. Choose *Not interested*
   if they say so - they are never contacted again.
3. **Status column in Leads:** when you take over a lead, write your own status (e.g. *Customer*, *Called*,
   *Meeting fixed*). The automation then leaves that lead alone. Statuses the system writes: *Emailed*,
   *Emailed (follow-up 1/2)*, *Interested - call now*, *Replied - read it*, *Not interested (opted out)*,
   *Email bounced*, *Emailed - no reply*.
4. **Do Not Contact tab:** add any e-mail, phone number or whole domain (`@example.com`) to block it.

## The tabs

| Tab | What it shows |
|---|---|
| Outreach | Every e-mailed lead: stage, e-mails sent, next follow-up, their reply |
| Email Preview | Dry-run only: the e-mails that would go out today |
| Replies | Each reply with type, text, phone, next step, a WhatsApp link and a link to check their Meta ads |
| WhatsApp Queue | Messages to send with one tap; you fill *Result* |
| Do Not Contact | Opt-outs, bounces and your own blocks - never contacted again |
| Outreach Report | One line per run: limit, sent, replies, bounces, WhatsApp, notes |

## Personal touch: the USP line

The Leads tab has a **USP** column: the one concrete thing a business says about itself on its own website
("Iconic Park Street restaurant famous for its Chelo Kebab since 1975", "15+ years of experience and 500+
projects"). When a lead has one, the first e-mail quotes it - *I came across Peter Cat and liked this line on
your website: "..."* - and such leads are e-mailed first. When the website says nothing distinctive (or there
is no website) the cell stays empty and the e-mail says where we came across them instead. Nothing is made
up: the line is always the business's own words, from its own site.

## Changing the messages

Edit `config/plant-parlour.toml`, section `[outreach.email]` (`subjects`, `first`, `follow_ups`),
`[outreach.whatsapp]` (`message`, `opted_in_message`) or `[outreach.hooks]` (one line per category, e.g.
`cafe = "..."`). Placeholders: `{greeting}` `{business}` `{first_name}` `{audience}` `{place}` `{hook}`
`{sender_name}` `{sender_first}` `{sender_business}` `{sender_phone}` `{sender_city}`. A typo in a
placeholder is caught before anything is sent. Keep e-mails short, plain and without links.

The number of WhatsApp messages a day is set in `[outreach.whatsapp]`: `start_per_day`, `step`,
`step_every_days` and `max_per_day` (20, +10 every 3 sending days, up to 50). You can raise `max_per_day`
(up to 200), but cold messages from one number above about 50 a day are what gets numbers restricted.

## Safety switches

| If | The system |
|---|---|
| Gmail says "daily limit" | stops until tomorrow, lowers the limit to 60% and stays under that account's limit; e-mails you |
| Gmail blocks a message as spam | stops for 2 days, halves the limit; e-mails you |
| Many bounces (10+ and 10% in 24 hours, or 6% in a week) | pauses until tomorrow / for 2 days (lower limit); e-mails you |
| More than 4% bounce in a week | the daily limit stops rising |
| The address's domain has no mail server any more | skipped and put on Do Not Contact |
| The Gmail login fails | sends nothing, records a problem |
| One lead's address is bad or odd | skips that lead, carries on |
| A lead replies "no" / "stop" / "not interested" | removes e-mail and phone numbers for good |
| A number is not on WhatsApp | you mark it once; it is never queued again, and the lead's next mobile number is tried |
| WhatsApp shows a warning or restricts the number | lower `max_per_day` in `[outreach.whatsapp]` to 20 and send only to numbers published as WhatsApp (`include_mobiles = false`) for a while |
| The tablet is off or Termux was closed | the run happens when it is back; nothing is sent twice |
| The outreach memory is lost | leads whose Status says *Emailed* are never e-mailed again; the WhatsApp queue rebuilds from its tab |

## Rules followed

E-mails go only to addresses the businesses published, say who is writing and how to stop, and every
opt-out is honoured permanently. India's telemarketing rules (TRAI) cover calls and SMS, not e-mail; if
you call leads, call from your normal mobile, one by one (no auto-dialers or recorded calls), and respect
"don't call me". India's data-protection rules (DPDP) phase in fully by May 2027 and will tighten consent
for contacting individuals; keep outreach business-to-business and honour every removal request.

## Paid upgrades (only when revenue allows)

* **More e-mail volume:** a domain (~₹800/year) + Google Workspace inboxes (~₹140/inbox/month) to send
  40/day from each inbox, plus a bulk e-mail verifier (~$37 per 10,000 checks) to keep bounces near zero.
* **Automatic WhatsApp:** the official WhatsApp Business Platform (Cloud API), ~₹1.02 per marketing message
  including GST, for people who agreed to hear from you (e.g. everyone who replied "yes" by e-mail).
