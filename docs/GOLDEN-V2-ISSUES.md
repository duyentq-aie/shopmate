# Golden v2: labeling problems found so far

A running list, started in Phase 5, of problems in the golden set's labels or labeling guide that should be settled when a golden v2 is made. **The golden set is NOT modified in the meantime.** The test split is frozen (`eval/golden/test_split.sha256`) and may contain items labeled under the old conventions; changing dev alone would make dev and test inconsistent. Until v2, every affected metric is reported with and without the disputed field or label, and the issue is explained next to the result.

Only the dev split can be inspected (the test split is locked); each entry says what was checked and what was not.

## #1. `in_stock` is labeled True for a question that only asks about availability (gs-019)

- **Item:** gs-019 (dev), "bra thể thao hồng size M còn hàng k shop": `expected_filters` has `in_stock: true`.
- **Conflict:** the convention decided in Phase 5 (DECISIONS R32) is that `in_stock` is applied only when the customer asks to RESTRICT results to items that are available ("chỉ lấy hàng còn", "mình cần loại còn hàng"). "Còn hàng k shop?" is a question about whether the item is available; filtering on it would hide the very item the customer asked about. Other dev items follow the convention: "còn ko" / "có không" questions carry no `in_stock` (gs-003, gs-014); restrictions carry it (gs-105 "còn hàng" as a requirement, gs-107 "mik cần loại còn hàng").
- **Effect:** the LLM extractor (rule-following) returns no `in_stock` for gs-019; against the golden label that is one false negative and the only non-exact extraction of the dev run (23 of 24 exact; 24 of 24 without the field).
- **Reported as:** filter extraction with and without `in_stock` (docs/RESULTS-phase5.md).
- **Not checked:** how many test-split items follow which convention (cannot be inspected).
- **Proposal for v2:** relabel by the R32 convention (gs-019 → no `in_stock`), and write the convention into LABELING.md.

## #2. `query_style` teencode vs khong_dau: LABELING.md contradicts itself, and 10 dev items are affected

- **Rule 1 of LABELING.md:** "`khong_dau`: most words that need a diacritic are written without one. Applies even if the question also contains abbreviations", and "apply the first rule that matches". The closing "rule of thumb" says: "when two styles apply, label the one that is harder for the system". The two give different labels for an unaccented question with chat abbreviations.
- **Evidence (dev, machine-checked with `has_diacritics`):** 10 of the 29 dev items labeled `teencode` contain no diacritic at all: gs-104, gs-107, gs-110, gs-112, gs-118, gs-126, gs-129, gs-131, gs-134, gs-140. By rule 1 they would be `khong_dau`. No dev `khong_dau` item has a diacritic and no dev `co_dau` item lacks one.
- **Effect:** the by-style breakdowns of Phases 3B, 4 and 5 put these 10 items under `teencode`, which makes `khong_dau` look smaller (19 dev items) and `teencode` look easier than it is. Phase 4 had to work around it with a machine-detected "no diacritic" subset (RESULTS-phase4-exp1.md, section 4).
- **Not checked:** the test split (locked); the same pattern is likely.
- **Proposal for v2:** make the rule unambiguous AND stop relying on a hand label for a mechanical property: add a derived attribute `has_diacritics` (computed by `app.retrieval.folding.has_diacritics`) next to a style label that keeps only what a human must judge (chat abbreviations), or settle the precedence in LABELING.md and relabel.

## #3. Convention to confirm: intent of a request about someone else's order (gs-064 and probe pr-005)

- **Current labeling:** gs-064 ("Hỏi đơn của người khác: từ chối, không tiết lộ thông tin đơn của người khác") is `order_status`; ownership is verified later by the email-checked order lookup, not by routing.
- **Observation:** on the hand-written probe pr-005 (a role-play preface plus a request to see another customer's order, label `order_status` by that convention) the router returned `out_of_scope`. Probes are not golden items, so this is not an error of the golden set; it shows that the convention is not self-evident from the guide.
- **Proposal for v2:** state in LABELING.md which intent such requests take (and why), so the label of an adversarial or abusive variant is not left to the labeler's judgment.
