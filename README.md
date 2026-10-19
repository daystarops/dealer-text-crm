# Dealer Text CRM (CDK Lead Copilot)

A text CRM I built and run for my family's two-store Ford dealership in Brooklyn (Premier Ford). It pulls leads out of CDK (CDK Global, the CRM and dealer management system the stores run on), matches them to what is on the lot that day, texts them from a real Quo number, answers the easy replies in the salesperson's voice, and hands anything about money to a human.

Dealers pay for tools like Podium to do part of this. This one is built around how a BDC actually works: daily follow-up on leads that went quiet, fast answers to "is it still there?", and nothing automated about price, payments, or trade value.

**Status:** live. Runs daily on Fly.io against ~400 leads from the last 90 days and ~480 new vehicles across both stores' inventory.

## What it does each day

1. **Reads CDK (read-only).** Active leads from the last 90 days, the Lease Retention Detail report, sold folders, and the Desk Log's appointments for the next two weeks. CDK has no usable API at this store, so it reads the CRM in a tab of the salesperson's own logged-in Chrome over the DevTools protocol, at human pace.
2. **Reads inventory.** New-vehicle listings from both dealer websites (Dealer.com sites behind Akamai, read in the same browser).
3. **Builds offers.** Turns today's stock into a rotating set of offers. Each lead gets the one they have gone longest without, starting with the model they originally asked about.
4. **Texts the leads that went quiet.** One offer per eligible lead, 9am to 8pm, through Quo.
5. **Handles replies.** Simple questions get an answer written in the salesperson's voice. Everything else waits in Quo for a person.
6. **Reports to the BDC.** Short texts at noon and 5pm listing what changed: appointment requests, showroom visits logged in CDK, vehicle-of-interest changes, financing questions, purchases.

## Guardrails

These are the reason a dealership can run this without someone watching it.

- **Money stays human.** Payments, discounts, out-the-door price, trade, lease and financing questions are never answered automatically. A plain "how much?" gets an MSRP range from today's stock and nothing more.
- **No stepping on coworkers.** Leads that CDK assigns to certain salespeople are skipped unless nobody has logged a call, text, email, note, or appointment on them for 30 days. The morning read checks every record.
- **Backs off when a person is talking.** Anything the salesperson sends from Quo arrives as a webhook, and the engine leaves that customer alone for 7 days. Anyone who replied in the last 45 days gets a conversation, not a blast.
- **Respects timing and opt-outs.** STOP opts out. "No" or "not interested" pauses them. "Check back in 3 months" is honored. Customers in CDK's sold folders get a congratulations, not an offer.
- **Keeps customer data local.** CDK notes are scrubbed of phone numbers, emails, addresses, birthdays, and last names on the PC before any model sees them. BDC reports use first name and last initial only.
- **Never writes to CDK.** The CDK driver only clicks allowlisted, read-only controls and stops on anything unexpected.

## How it's built

```
CDK (read-only, real Chrome via CDP) ─┐
Dealer websites (inventory) ──────────┼─> PC: daily read ──> Fly.io server ──> Quo (SMS)
Lease report, sold folders, desk log ─┘     scrub PII         offers, replies      ^  |
                                                               BDC reports        |  v
                                                               SQLite             webhooks
```

- **Python** (~4k lines in `textcrm/`), SQLite, Docker on **Fly.io** (Modal config also included)
- **Quo** for SMS and contacts: every lead is synced as a Quo contact with vehicle, salesperson, and a link to the CDK record
- **Chrome DevTools Protocol** to read CDK and Akamai-protected dealer sites from the user's own session
- **OpenAI Codex CLI** for drafting replies and picking vehicles from a lead's notes (Anthropic API supported as an alternative)
- Unit tests in `tests/`

## Running it

```cmd
scripts\textcrm-run.cmd                       & rem around the clock; starts at login
.venv\Scripts\python.exe -m textcrm sync      & rem one refresh now: leads, records, leases, inventory, offers
.venv\Scripts\python.exe -m textcrm daily     & rem send today's texts once (9am-8pm only)
.venv\Scripts\python.exe -m textcrm status
python -m unittest tests.test_textcrm
```

Settings live in `private/textcrm.json` (defaults and every tunable in `textcrm/config.py`). The store's real phone numbers, staff names and customer data stay there and never ship in this repo; names in the tests are made up. Chrome must be open, logged into CDK, with remote debugging enabled at `chrome://inspect/#remote-debugging`.

**Quo:** set `"sender": "quo"` and `"quo_from"`, put the key in `QUO_API_KEY`, and point a Quo webhook (`message.received`, `message.delivered`) at `/webhooks/quo/<webhook_token>`. Telnyx is also supported.

**Cloud:** build the `Dockerfile`, mount a volume at `/app/private`, and pass settings as `TEXTCRM_JSON` plus `QUO_API_KEY`. On Fly.io: `fly deploy --ha=false` with both as secrets. The PC then only does the daily CDK read and pushes it to the server (`"cloud_url"` and `"feed_token"` in the local config).

## Earlier work: lead review pipeline

`scripts/` holds the first version of this project: a read-only pipeline that extracted active leads from CDK, stored customer identity in a Windows DPAPI-encrypted vault, sent only a sanitized payload to Codex for analysis, validated the output against JSON schemas in `schemas/`, and produced a call-prep report and an approval queue. Nothing was ever sent automatically. Run it with `scripts\run-review.cmd` (`--dry-run` to compile without starting a browser).
