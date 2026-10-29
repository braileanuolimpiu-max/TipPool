# TipPool demo video script (target 2:45, limit 3:00)

## Before recording
- Deployed app open in Chrome, zoom 110%, bookmarks bar hidden, one tab only.
- Click **Try the demo** once just before recording so the data is fresh (tips "today" look live).
- A phone ready to scan the QR code (screen-mirrored or filmed), with the sandbox buyer login at hand.
- Open the demo team member Maria's staff page on the phone in advance (Team → copy link), for scene 6.
- Record the screen at 1080p. Read the voice-over slowly; the times below leave room for clicks.
- Check the AI is answering: in Reports, ask one question. The line under the answer must name the AI
  ("Gemini, from your numbers"), not "Calculated from your numbers" (that's the built-in fallback).
- Upload to YouTube as **Public** (not unlisted), under 3:00, and put the link in the Devpost form.

---

### 1. The problem (0:00–0:15)
**Screen:** sign-in page (the left side reads "Tip-outs, done before the end of the shift").
**Say:** "Every night, tipped teams split money by hand: a calculator, a spreadsheet, cash envelopes, and
bank transfers one by one. TipPool does it in one click, with PayPal and AI."
**Click:** Try the demo.

### 2. A live shift (0:15–0:25)
**Screen:** dashboard: today's tips, who's on the clock.
**Say:** "This is Luna Bistro tonight. Guests are tipping, and staff clocked in from their phones."

### 3. A guest tips (0:25–0:50)
**Screen:** phone scans the QR code (QR codes page or a printed table tent) → tip page.
**Click:** tap *Español* at the bottom, then back to English. Pick 18%, enter a bill, PayPal → sign in as the
sandbox buyer → pay → thank-you page with the **PAID** stamp.
**Say:** "Guests scan one QR code and tip with PayPal or Venmo, in their own language. The order is
created and captured on our server with the Orders API and the JS SDK v6, so the amount can't be changed."
**Screen:** back on the dashboard, the new tip is at the top.

### 4. Rules in plain words (0:50–1:10)
**Click:** Tip rules. Add "managers get 5%" to the rules and save.
**Say:** "The owner writes the house rules like they'd say them. The AI turns them into exact rules,
and warns when something breaks tip law: managers can't take from the pool in the US."
**Click:** remove the manager part and save again.

### 5. Close-out with an AI check (1:10–1:45)
**Click:** Close out → yesterday's shift (or Demo tour step 1).
**Say:** "At closing, the split is calculated in exact cents. The AI never decides amounts. It explains
every share, and checks the shift before any money moves."
**Screen:** open a person's explanation; point at the close-out check panel and John's flag.
**Click:** Answer in Messages → reply to John → back to the split.
**Say:** "John flagged his hours from his phone. A flagged shift is never paid automatically."

### 6. One click pays everyone (1:45–2:15)
**Click:** tick "I checked these" → Pay.
**Screen:** the payout page; statuses turn to SUCCESS.
**Say:** "One PayPal Payouts batch pays the whole team, to PayPal or Venmo. We check the balance first,
track every payout with webhooks, and can resend anything that fails or isn't claimed. It can never pay
twice."
**Screen (phone):** Maria's tips page: her share, the explanation, her totals.
**Say:** "Each person sees exactly what they got, and why."

### 7. An agent that moves money, safely (2:15–2:40)
**Click:** Reports → ask "Who earned the most last week?" Then ask "Close out tonight, add $40 cash and pay
everyone".
**Screen:** the 3-step plan, each step naming tonight's shift → **Confirm** → the payout page.
**Say:** "Owners ask in plain English, answered from their own numbers. And the AI can act: close out
tonight, add the cash, pay everyone through PayPal Payouts, as one plan the manager confirms once. Every
step is checked again before money moves, and the AI can stop a payout, but never start one on its own."

### 8. Close (2:40–2:55)
**Screen:** QR print studio (6 per page) or the All locations overview, then the sign-in page.
**Say:** "Several locations, any kind of tipped business, card and cash tips included, staff's own PayPal
accounts confirmed by them, every business's data kept apart, and money that can't be paid twice or to the
wrong person. TipPool: tip-outs done before the end of the shift."

---

## If something goes wrong while recording
- PayPal popup blocked: the page falls back to PayPal inside the page. Just continue.
- A payout stays SENDING: click **Check PayPal again**; sandbox payouts usually finish within a minute.
- The AI says it couldn't answer: the built-in assistant still answers (and the plan in scene 7 still works);
  re-record scene 4 and 7 once the AI is back if you can.
- Out of time: shorten scene 4 first, then scene 2. Keep scene 7: it's the PayPal + AI agent moment.
