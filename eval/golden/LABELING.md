# Golden set labeling guide

## query_style — apply the first rule that matches
1. `khong_dau` — most words that need a Vietnamese diacritic are written without one.
   Applies even if the question also contains abbreviations.
2. `teencode` — written with diacritics, but contains at least one chat abbreviation:
   ko, k (meaning "không"), dc / đc, sp, j, bn, mn, nhìu, e (em), …
3. `co_dau` — proper diacritics and no chat abbreviations.

A few missing diacritics or typos do not make a question khong_dau; real typos are kept on purpose.

NOT abbreviations: loanwords (ship, freeship, size, sale, cute, COD), prices like 400k,
placeholders such as {order_number}, emails, SKUs.

Rule of thumb: when two styles apply, label the one that is harder for the system.

## intent
One intent per item. If a question mixes several, label the action the customer wants first.

## order_status items
An order is revealed only when the order number AND the owner's email both match
(v1 verifies by email only; a phone number alone → ask for the email).
Number only → the system must ask for verification. Mismatch or someone else's order → refuse.

## author / status
- `author: agent_draft` — items drafted by the agent (gs-001 … gs-064), even after human edits.
- `author: human` — items written from scratch by a human.
- `status: human_reviewed` — only after a human has read the item and corrected it if needed.