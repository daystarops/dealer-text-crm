# Review Active CDK Leads

## Inputs

- `batch_size`: number of leads to review; default `5`.
- `search_period`: expected scope; default `All` for all Active leads.
- `as_of_date`: the review date used to verify the end date and calculate lead age.
- Date boundaries are intentionally empty when the Active lead scope is `All`.

## Objective

Review the first `batch_size` unreviewed active CRM leads and rank the strongest follow-up opportunities without modifying CDK.

## Workflow

1. Connect to the currently attached Chrome session with `browser-use` and work only in the CDK Unify CRM tab.
2. Briefly narrate each browser action before performing it.
3. Navigate through **CRM > Prospects > Active** using element text and page structure.
4. Inspect the current period and dates. If already set to `search_period` with the expected dates, preserve the search and do not refresh it. Otherwise, change only the read-only search controls needed to establish the requested period; never type into CRM fields. Refresh only if necessary to apply the search.
5. Record the ordered result list without exposing sensitive columns. Use privacy-safe labels such as first name plus last initial.
6. For each lead, one at a time:
   - Open the customer's name and wait for the record to load.
   - Inspect vehicle of interest, lead source, created date, status, inquiry/comments, salesperson notes, communication history, last customer response, last salesperson contact, appointments, trade, non-sensitive financing/payment expectations, and scheduled or overdue tasks.
   - Do not open a credit application or reveal protected personal/financial data.
   - Return to the same search-results list and preserve its state.
   - Continue with the next unreviewed lead.
7. Stop immediately after `batch_size` leads. Do not inspect additional leads.
8. Produce structured output conforming to `schemas/lead-review.schema.json`, followed by a concise ranked summary.

## Per-lead assessment

Capture:

- Privacy-safe customer label
- Vehicle
- Lead age in days
- Lead source
- Last meaningful contact
- Buying signals
- Main obstacle
- Appointment status
- Recommended next action
- Suggested call/text approach in `call_brief`
- Priority score from 0 to 100

Prioritize explicit customer engagement, recent replies, showroom activity, trade details, concrete timing, appointments, opened manual communications, and specific product/payment questions. Reduce priority for repeated non-response, out-of-market statements, unreachable contact channels, unresolved prerequisites, or stale activity.

## Hard restrictions

Treat CDK as read-only. Never type into fields or click Save, Send, Submit, Complete, Merge, Reassign, Delete, Archive, change status, or open a credit application. Stop and ask for approval if any action could modify a record. Never output or persist full phone numbers, addresses, birth dates, government identifiers, driver's-license data, banking information, credit details, credentials, cookies, or tokens.

