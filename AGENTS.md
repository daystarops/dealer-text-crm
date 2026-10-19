# CDK Lead Copilot Agent Instructions

## Purpose

Review active CDK CRM leads in controlled, configurable batches and produce privacy-safe follow-up recommendations. CDK is always read-only.

## Browser workflow

1. Use the `browser-use` skill to connect to the existing Chrome/CDK session. Do not start a separate profile when an attached session is available.
2. Work only in the CDK Unify CRM tab.
3. Navigate to **CRM > Prospects > Active**.
4. Inspect the current search period before changing anything. If Active leads already use the correct 90-day period, preserve it and do not reset or refresh it.
5. Verify the intended start and end dates. Refresh only when necessary to apply an approved read-only filter change.
6. Review lead names one at a time, beginning with the first unreviewed visible lead.
7. Open the customer's name and wait for the record to finish loading.
8. Review relevant entered data: vehicle, source, created date, status, inquiry/comments, salesperson notes, communication history, last customer response, last salesperson contact, appointments, trade, non-sensitive financing/payment expectations, and scheduled or overdue tasks.
9. Return to the same results list after each lead. Preserve the search state and continue with the next unreviewed lead.
10. Process only the configured batch size, then stop and return a ranked summary.

## Read-only guardrails

- Treat CDK as read-only.
- Never type into CRM fields.
- Never click or invoke Save, Send, Submit, Complete, Merge, Reassign, Delete, Archive, status changes, or a credit application.
- Stop and ask for explicit approval before any action that could modify a record.
- Prefer element text and page structure over fixed screen coordinates.
- Do not expose full phone numbers, home addresses, birth dates, Social Security numbers, driver's-license details, banking data, credit details, credentials, cookies, or tokens.
- Refer to customers with a privacy-safe label such as first name and last initial.

## Repository privacy

- Never commit customer data, screenshots containing customer data, browser profiles, cookies, credentials, exports, logs, recordings, or generated outputs.
- Examples must be fully synthetic and must not be derived by merely renaming real customers.
- Keep operational artifacts only in ignored directories.

