# Phase 8 pre-registration

> **Status: started, not complete.** `docs/PHASE8-PREREG.md` did not exist when the catalogue audit below was added, so this file holds, for now, only the section
> *Disclosed incidents and known limits* and only its first entry. The reported metrics with their bootstrap settings, the two figures (all items / without the item with the
> hash `3b9975a6…`), the exact commands, the commit that is frozen, the quota estimate, the rerun policy, the commitment to publish every result and the other entries of the
> disclosure section (A0, D7.24, the `agent_draft` items of the golden set, the saturated router on dev, the `no_answer` scoring rule that does not measure the quality of an offer)
> are written in turn 1 of Phase 8. Nothing here is a result of the test split: **the test split has not been run and was not consulted for anything in this file.**

## Disclosed incidents and known limits

### The synthetic catalogue has products whose name contradicts their material

**What it is.** `data/seed/catalog.vi.json` is generated from templates (`scripts/generate_catalog.py`): a product name is `kind + style + colour`, and the material is drawn
separately, from the materials of the kind. A style word can therefore name a material the product does not have: "Áo khoác nam **jean** xanh rêu" is made of Polyester. The description
is built from the name and the material ("… làm từ chất liệu polyester"), so the contradiction is also inside the text that is indexed. The data is synthetic; this is a limit of how it
was made, not a measurement.

**How it was checked.** `uv run python -m scripts.audit_catalog_consistency` (tests: `tests/test_audit_catalog.py`). It only reads the catalogue file: it writes nothing and opens no index
or service. A whole word of the name that names a material is compared with the `material` attribute (jean and denim: Denim; da: Da bò, Da tổng hợp or Da lộn; linen; nỉ; dù; kaki, also
Kaki pha spandex; len, also Len pha cotton; and the other material words, none of which occurs in a name). Words are matched exactly, accents included (da is leather, dạ and đa are other words;
dù is the fabric, "du lịch" is travel; tím is purple, "cổ tim" is a neckline). The name is also checked against the `color` attribute, longest colour first ("xanh navy" before "xanh").
"thun" is not checked (it names a garment as well as a fabric), and neither are the descriptions.

**Result: 250 products read.**

| rule | names with the word | contradictions |
|---|---|---|
| jean / denim | 18 | 4 |
| nỉ (fleece) | 2 | 2 |
| kaki | 10 | 2 |

Products with at least one contradiction: 8 of 250
SKUs: SM-NAM-0014, SM-NAM-0021, SM-NAM-0028, SM-NAM-0041, SM-NAM-0042, SM-NAM-0048, SM-NU-0027, SM-NU-0046

| SKU | name | material |
|---|---|---|
| SM-NAM-0014 | Áo khoác nam jean xanh rêu | Polyester |
| SM-NAM-0021 | Áo khoác nam nỉ có mũ xám | Denim |
| SM-NAM-0028 | Áo khoác nam jean xanh navy | Dù |
| SM-NAM-0041 | Quần short nam kaki xanh rêu | Cotton |
| SM-NAM-0042 | Áo khoác nam nỉ có mũ xanh navy | Polyester |
| SM-NAM-0048 | Quần short nam kaki đen | Polyester |
| SM-NU-0027 | Áo khoác nữ jean be | Dù |
| SM-NU-0046 | Chân váy nữ jean trắng | Tweed |

- All eight are a **style** word of the name, none is in the kind of the garment: every "Quần jean nam / nữ" is Denim, every "Ví da" is leather, every "Áo len nữ" is wool or a wool blend.
- **Looked at and found consistent (0 contradictions):** "da" (7 names), "len" (7 names). **Not checkable, the word is in no name:** denim, linen, dù, lụa, voan, tweed, kate, canvas, lưới, thun lạnh,
  cotton, polyester, bamboo, acetate, kim loại, cao su. A zero in the second group means the rule has nothing to read, not that it was verified.
- **Colour:** 0 products whose name has another colour, 0 whose name lacks its own colour. This is by construction (the generator writes the colour attribute into the name), so it is not independent evidence.
- 8 of 250 is 3.2% of the catalogue. Two of the eight, SM-NAM-0014 and SM-NAM-0042, are the two jackets of the offer shown by the widget demo (D7.30, D7.36); the demo draws them with a plain weave
  because it follows the `material` attribute.

**What it means for the results.** A question that filters on material, or an answer that quotes it, can touch one of these eight products and then rests on a record that disagrees with itself.
Checked on **dev** only, through `load_split("dev")`: none of the eight is among the products selected by the expected filters of the 24 dev product items. The **test split was not consulted**
(it is locked), so whether its items are exposed is not known and cannot be known without opening it; at most 8 of the 250 products are involved. Both figures of Phase 8 are reported on the catalogue
as it is.

**Decision: the catalogue is not corrected before Phase 8.** Correcting a name or a material changes the indexed text, therefore the embeddings, the lexical fingerprint and
`data/index/manifest.json`, and with them the index that the dev results, the frozen commit and the hashes of the committed results are tied to. It is left to after Phase 8, as one change with a re-index
and a re-run on dev, and the audit script is the check that it worked (it must then print 0 of 250).
