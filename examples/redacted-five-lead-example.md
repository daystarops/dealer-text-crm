# Synthetic Five-Lead Review Example

All people, dates, vehicles, sources, events, and circumstances below are invented. They do not correspond to CRM records or real customers.

Review date: 2027-03-15  
Search period: All Active leads
Batch size: 5

## Ranked summary

### 1. Mina Q. | priority 88

- Vehicle: New 2027 Ford F-150 XLT
- Lead age: 9 days
- Source: Synthetic manufacturer website inquiry
- Buying signals: Requested towing specifications; supplied a fictional trade description; replied yesterday asking about Saturday availability
- Main obstacle: Needs to confirm trailer-weight requirements
- Appointment status: Tentative Saturday visit; not yet confirmed
- Last meaningful contact: Replied on 2027-03-14 asking whether the truck could be demonstrated with towing features
- Recommended action: Confirm the required towing configuration and offer two Saturday appointment times
- Suggested opening: “Hi Mina, I’m following up on the F-150 towing setup you asked about. I have the configuration details and wanted to help you choose the best time to see it.”

### 2. Devon R. | priority 79

- Vehicle: Used 2025 Ford Mustang Mach-E Premium
- Lead age: 17 days
- Source: Synthetic third-party listing
- Buying signals: Opened a manual email; asked about charging and warranty coverage; mentioned a vehicle to trade
- Main obstacle: Comparing home-charging costs with another EV
- Appointment status: No appointment
- Last meaningful contact: Asked a charging-cost question on 2027-03-11
- Recommended action: Answer the charging question and invite a short test drive
- Suggested opening: “Hi Devon, I have a clear answer to your Mach-E charging question and can also walk through the warranty coverage you were comparing.”

### 3. Priya L. | priority 68

- Vehicle: New 2027 Ford Explorer ST-Line
- Lead age: 28 days
- Source: Synthetic showroom inquiry
- Buying signals: Completed a showroom visit and asked for a feature comparison
- Main obstacle: Purchase timing depends on a fictional lease ending later in the spring
- Appointment status: Prior visit completed; no return appointment
- Last meaningful contact: Said on 2027-03-05 that she planned to revisit timing in two weeks
- Recommended action: Offer an updated comparison and a low-pressure timing review
- Suggested opening: “Hi Priya, I’m checking in at the time you mentioned and have the Explorer comparison ready if your timing has become clearer.”

### 4. Elliot V. | priority 52

- Vehicle: New 2027 Ford Maverick Lariat
- Lead age: 41 days
- Source: Synthetic social campaign
- Buying signals: Clicked an inventory link and previously asked about bed accessories
- Main obstacle: No response to two recent follow-ups
- Appointment status: No appointment; follow-up scheduled next week
- Last meaningful contact: Asked about accessories on 2027-02-18
- Recommended action: Send one concise value-based follow-up focused on the requested accessories
- Suggested opening: “Hi Elliot, I found the Maverick accessory information you asked for and wanted to see whether the truck is still on your list.”

### 5. Tessa N. | priority 24

- Vehicle: Used 2024 Ford Edge SEL
- Lead age: 63 days
- Source: Synthetic dealer website inquiry
- Buying signals: Initially asked for a price comparison
- Main obstacle: Later stated that she paused the purchase and has not re-engaged
- Appointment status: Prior tentative visit canceled; no active appointment
- Last meaningful contact: Stated on 2027-01-29 that she was pausing the search
- Recommended action: Low-priority permission-based check-in; close the loop if she remains out of market
- Suggested opening: “Hi Tessa, I’m making a brief check-in on your Edge search. Is it still paused, or would updated options be useful?”

## Example batch JSON shape

```json
{
  "as_of_date": "2027-03-15",
  "search_period": "All",
  "batch_size": 5,
  "leads": [
    {
      "lead_label": "Mina Q.",
      "vehicle": "New 2027 Ford F-150 XLT",
      "lead_age_days": 9,
      "lead_source": "Synthetic manufacturer website inquiry",
      "buying_signals": ["Asked about towing", "Provided a fictional trade", "Requested a Saturday visit"],
      "obstacle": "Needs to confirm trailer-weight requirements",
      "appointment_status": "Tentative Saturday visit; unconfirmed",
      "last_meaningful_contact": "Replied on 2027-03-14 about towing features",
      "recommended_action": "Confirm configuration and offer two appointment times",
      "priority_score": 88,
      "call_brief": {
        "opening_15_seconds": "Draft: Hi Mina, I have the F-150 towing details you requested and wanted to help you plan a quick look Saturday.",
        "reason_for_calling": "Follow up on the synthetic towing question and tentative visit.",
        "discovery_questions": ["What will you tow most often?", "Which features matter most?", "Would morning or afternoon be easier Saturday?"],
        "vehicle_or_payment_angle": "Confirm the appropriate configuration before discussing conditional pricing or payment options.",
        "objections": [
          {"objection": "I am still comparing trucks.", "response": "That makes sense; I can make the comparison easier by focusing on towing and the features you named."},
          {"objection": "I am not ready to commit.", "response": "No commitment is needed; a short demonstration can simply confirm whether the configuration fits."}
        ],
        "voicemail": "Draft: Hi Mina, I have the F-150 towing information you requested. Please call when convenient, and I can also reserve a short Saturday demonstration if helpful.",
        "follow_up_text": "Draft: Hi Mina, your F-150 towing details are ready. Would a short Saturday morning or afternoon demonstration be more convenient?",
        "appointment_close": "Draft: Would Saturday at 10:30 AM or 2:00 PM work better for a brief demonstration?"
      }
    }
  ]
}
```

