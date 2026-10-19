# Generate a Call Brief

Using one privacy-safe lead review that conforms to `schemas/lead-review.schema.json`, create a concise, truthful follow-up brief. Do not invent inventory, incentives, approval terms, payment figures, or customer statements.

Return:

1. **15-second opening**: identify the dealership context, the vehicle, and a customer-specific reason for the call.
2. **Reason for calling**: one sentence grounded in the recorded inquiry or most meaningful contact.
3. **Three discovery questions**: open-ended questions about timing, vehicle fit, trade, visit readiness, or budget expectations without soliciting protected financial data.
4. **Strongest vehicle/payment angle**: the best supported value proposition; use conditional language for availability, incentives, trade values, payments, or financing.
5. **Two likely objections and responses**: empathetic, concise, and based on the recorded obstacle.
6. **Voicemail**: approximately 20 seconds, with no sensitive details.
7. **Short follow-up text**: under 320 characters, conversational, and requiring human approval before sending.
8. **Desired appointment close**: ask for a specific next step with two time choices, without claiming that an appointment exists.

Use only the privacy-safe `lead_label`. Mark every generated message as a draft. Never send or enter it into CDK.

