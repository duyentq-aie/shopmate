"""Code guardrails on a generated answer (docs/DECISIONS-phase6.md, A11) and the deterministic answers that replace a rejected one.

The check is done in code AFTER generation, not asked of the prompt. Whatever a check cannot be sure of is a violation: a false alarm costs a
fixed template answer, a miss costs a wrong fact told to a customer.

- Citations. A product answer names at least one SKU of the evidence and no SKU that is not in it; a policy answer cites at least one
  (file, clause) of the evidence and writes no "Điều N" that is not in it. **An explicit "no information" policy answer cites nothing** (A21):
  it is the one policy answer that asserts nothing the evidence must back, so it needs no citation (every other rule still applies to it).
- Prices. Every price in the answer is a price of the evidence. **A price in the same sentence as a SKU must be the price of a SKU in that
  sentence** (owner's rule): "áo này 99k đúng không shop?" cannot be answered "đúng, SM-... giá 99k". A number the customer wrote may appear
  only as THEIR BUDGET (they put "dưới", "tầm", ... before it, and the answer does the same), never in a sentence about a product.
- Stock. "còn hàng" / "hết hàng" must agree with the evidence of the SKU of the sentence (or of every hit when the sentence names none).
- Order. The status words must be those of the order's real status and of no other; "đơn 88" must be the order number; no price at all.
- People. No email (a masked one is not an email to this check), no phone number, no street address; no copy of the instructions.

The fallback texts are built from the evidence only and must pass this very check (a test makes sure of it). **A fallback never asserts more
than the text it replaces** (A21): it keeps only the products the rejected text named and the clauses it cited, and when there are none it is the
fixed no-information reply, never a quote of something the model did not use.
"""

from __future__ import annotations

import re
from datetime import datetime

from app.agent import templates as T
from app.agent.parsing import EMAIL, PHONE
from app.retrieval.folding import fold

SKU = re.compile(r"\bSM-[A-Z]+-\d{4}\b")
_SENTENCES = re.compile(r"(?<=[.!?…])\s+|\n+")
_GROUPED = re.compile(r"(?<![0-9.,])([0-9]{1,3}(?:[.,][0-9]{3})+)(?![0-9])")  # 459.000, 1,200,000
_K = re.compile(r"(?<![0-9.,])([0-9]+(?:[.,][0-9]+)?)\s*k\b", re.I)  # 99k, 1,5k
_THOUSAND = re.compile(r"(?<![0-9.,])([0-9]+(?:[.,][0-9]+)?)\s*(?:nghìn|nghin|ngàn|ngan)\b", re.I)
_MILLION = re.compile(r"(?<![0-9.,])([0-9]+(?:[.,][0-9]+)?)\s*(?:triệu|trieu|tr)\b", re.I)
_PLAIN = re.compile(r"(?<![0-9.,])([0-9]{4,9})\s*(?:đ|₫|vnd|vnđ|đồng)(?!\w)", re.I)
_BUDGET_MARKERS = ("duoi", "tren", "tam", "khoang", "tu", "den", "toi da", "toi thieu", "khong qua", "ngan sach")  # folded, for the answer's sentence
_DIEU = re.compile(r"\bđiều\s+([0-9]+)", re.I)
_ORDER_NUMBER = re.compile(r"(?:#|\bđơn(?:\s+hàng)?(?:\s+số)?\s+)([0-9]{1,10})\b", re.I)
_ADDRESS = (re.compile(r"\b[0-9]{1,4}[A-Za-z]?(?:/[0-9]+)*\s+(?:đường|phố|ngõ|ngách|hẻm)\b", re.I),
            re.compile(r"\b(?:số nhà|địa chỉ)\s*:", re.I))
PROMPT_WINDOW = 50
MAX_QUOTED_CLAUSES = 2  # a policy fallback quotes at most this many of the clauses the rejected text cited
# folded; the model says, in so many words, that it has no information. "không có quy định" is NOT here: that is a claim about the rules, which needs a citation.
_DECLINE = re.compile(r"\b(?:(?:khong|chua)\s+(?:co|tim\s+thay|du)\s+(?:thong\s+tin|du\s+lieu)|(?:khong|chua)\s+de\s+cap)\b")

STATUS_LABELS = {  # the words the fixed answers use; the guard knows them too
    "UNCONFIRMED": "chưa được xác nhận", "UNFULFILLED": "đang được xử lý, chưa giao", "PARTIALLY_FULFILLED": "đã giao một phần",
    "FULFILLED": "đã giao", "CANCELED": "đã bị hủy"}
_STATUS_PHRASES = {  # folded; PARTIALLY_FULFILLED first, because its words contain those of FULFILLED ("đã giao một phần")
    "PARTIALLY_FULFILLED": ("giao mot phan", "giao mot so", "giao 1 phan"),
    "UNCONFIRMED": ("chua duoc xac nhan", "chua xac nhan", "cho xac nhan"),
    "UNFULFILLED": ("chua duoc giao", "chua giao", "dang duoc xu ly", "dang xu ly"),  # "đang chuẩn bị" is too common to name a status
    "CANCELED": ("da bi huy", "da huy", "bi huy"),
    "FULFILLED": ("da giao", "giao thanh cong", "da hoan tat", "da hoan thanh"),
}


# --- reading numbers ---------------------------------------------------------------------------------------------------------------


def _number(text: str) -> float:
    return float(text.replace(",", ".")) if re.fullmatch(r"[0-9]+[.,][0-9]{1,2}", text) else float(re.sub(r"[.,]", "", text))


def prices_in(text: str) -> list[int]:
    """Amounts in VND written in the text: 459.000đ, 459.000, 99k, 1,5k, 300 nghìn, 2 triệu, 1500000đ."""
    found = [int(re.sub(r"[.,]", "", m.group(1))) for m in _GROUPED.finditer(text)]
    found += [int(round(_number(m.group(1)) * 1000)) for pattern in (_K, _THOUSAND) for m in pattern.finditer(text)]
    found += [int(round(_number(m.group(1)) * 1_000_000)) for m in _MILLION.finditer(text)]
    found += [int(m.group(1)) for m in _PLAIN.finditer(text)]
    return found


_UNIT = r"(?:k|nghìn|nghin|ngàn|ngan|triệu|trieu|tr|đ|₫|vnd)"
_RANGE = re.compile(rf"([0-9]+(?:[.,][0-9]+)?)\s*[-~]\s*([0-9]+(?:[.,][0-9]+)?)\s*({_UNIT})\b", re.I)
_BUDGET_PHRASE = re.compile(
    rf"(?:dưới|duoi|trên|tren|tầm|tam|khoảng|khoang|từ|tu|đến|den|tối đa|toi da|tối thiểu|toi thieu|không quá|khong qua|ngân sách|ngan sach)"
    rf"\s*(?:là|la)?\s*([0-9][0-9.,]*\s*{_UNIT}?(?:\s*[-~]\s*[0-9][0-9.,]*\s*{_UNIT}?)?)", re.I)


def budget_numbers(question: str) -> set[int]:
    """The amounts of the customer's message that are a BUDGET: a budget word comes right before them ("dưới 600k", "tầm 300-500k", "từ 200k").
    An amount without such a word ("áo này 99k đúng không") is a claim about a product, not a budget."""
    question = _RANGE.sub(lambda m: f"{m.group(1)}{m.group(3)}-{m.group(2)}{m.group(3)}", question)  # 300-500k -> 300k-500k
    budget: set[int] = set()
    for match in _BUDGET_PHRASE.finditer(question):
        budget.update(prices_in(match.group(1)))
    return budget


def _has_budget_word(sentence: str) -> bool:
    folded = fold(sentence)
    return any(re.search(rf"\b{marker}\b", folded) for marker in _BUDGET_MARKERS)


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCES.split(text) if s.strip()]


# --- the checks -----------------------------------------------------------------------------------------------------------------------------


def _order_statuses_mentioned(text: str) -> set[str]:
    folded = fold(text)
    mentioned = set()
    for status, phrases in _STATUS_PHRASES.items():
        for phrase in phrases:
            if phrase in folded:
                mentioned.add(status)
                folded = folded.replace(phrase, " ")  # a phrase is counted once for the status it belongs to, not again for a shorter one
    return mentioned


def _stock_claims(sentence: str) -> tuple[bool, bool]:
    """(says in stock, says out of stock)"""
    folded = fold(sentence)
    out = bool(re.search(r"het hang|khong con hang|ngung ban|het sach", folded))
    folded = re.sub(r"het hang|khong con hang|ngung ban|het sach", " ", folded)
    return bool(re.search(r"con hang|san hang|co san", folded)), out


def check_answer(kind: str, text: str, evidence: dict, question: str, *, cited_skus=(), cited_clauses=(), instructions: str = "") -> list[str]:
    """What is wrong with a generated answer ([] if nothing). `kind`: products | policies | order. Each item is `rule: detail`."""
    problems: list[str] = []
    if not text or not text.strip():
        return ["empty: the answer has no text"]
    if EMAIL.search(text):
        problems.append("email: the answer contains an email address")
    if PHONE.search(text):
        problems.append("phone: the answer contains a phone number")
    if any(pattern.search(text) for pattern in _ADDRESS):
        problems.append("address: the answer contains a street address")
    if instructions and _leaks(text, instructions):
        problems.append("prompt_leak: the answer repeats the instructions")
    if kind == "products":
        problems += _check_products(text, evidence, question, cited_skus)
    elif kind == "policies":
        problems += _check_policies(text, evidence, question, cited_clauses)
    elif kind == "order":
        problems += _check_order(text, evidence)
    else:
        problems.append(f"kind: unknown kind {kind!r}")
    return problems


def _leaks(text: str, instructions: str) -> bool:
    normalised = " ".join(instructions.split())
    squeezed = " ".join(text.split())
    return any(normalised[i:i + PROMPT_WINDOW] in squeezed for i in range(0, max(1, len(normalised) - PROMPT_WINDOW), 10))


def _check_products(text: str, evidence: dict, question: str, cited_skus) -> list[str]:
    hits = {h["sku"]: h for h in evidence.get("hits", [])}
    problems = []
    in_text = set(SKU.findall(text))
    for sku in sorted(in_text - set(hits)):
        problems.append(f"unknown_sku: {sku} is not in the tool results")
    for sku in sorted(set(cited_skus) - set(hits)):
        problems.append(f"unknown_sku: cited {sku} is not in the tool results")
    if not in_text & set(hits):
        problems.append("no_sku_cited: the answer names none of the products found")
    allowed_prices = {h["price_vnd"] for h in hits.values()}
    budget = budget_numbers(question)
    for sentence in sentences(text):
        skus = [s for s in SKU.findall(sentence) if s in hits]
        stated = prices_in(sentence)
        for price in stated:
            if skus:  # the owner's rule: in a sentence about a SKU, the price is that SKU's price
                if price not in {hits[s]["price_vnd"] for s in skus}:
                    problems.append(f"price_mismatch: {price} is not the price of {', '.join(skus)}")
            elif price not in allowed_prices and not (price in budget and _has_budget_word(sentence)):
                problems.append(f"price_not_in_evidence: {price}" + (" (the customer's number, not as a budget)" if price in prices_in(question) else ""))
        says_in, says_out = _stock_claims(sentence)
        relevant = [hits[s] for s in skus] or list(hits.values())
        if says_in and not all(h["in_stock"] for h in relevant):
            problems.append("stock_mismatch: says in stock, the tool results say out of stock")
        if says_out and not all(not h["in_stock"] for h in relevant):
            problems.append("stock_mismatch: says out of stock, the tool results say in stock")
    return problems


def is_decline(text: str, cited_clauses=()) -> bool:
    """The model says outright that it has no information ("không có thông tin về ...", "chính sách không đề cập ..."), cites no clause and names
    no "Điều N". Such an answer asserts nothing that needs a citation. What it is NOT: an answer that cites a clause, and a claim about the rules
    ("không có quy định ...")."""
    if not text or cited_clauses or _DIEU.search(text):
        return False
    return bool(_DECLINE.search(fold(text)))


def _check_policies(text: str, evidence: dict, question: str, cited_clauses) -> list[str]:
    pairs = {(h["file"], h["clause"]) for h in evidence.get("hits", [])}
    problems = []
    cited = {(c["file"], c["clause"]) for c in cited_clauses}
    if not cited & pairs and not is_decline(text, cited_clauses):
        problems.append("no_clause_cited: the answer cites none of the clauses found")
    for pair in sorted(cited - pairs):
        problems.append(f"unknown_clause: cited {pair[0]} Điều {pair[1]} is not in the tool results")
    clauses = {c for _, c in pairs}
    for number in sorted({int(n) for n in _DIEU.findall(text)} - clauses):
        problems.append(f"unknown_clause: Điều {number} is not in the tool results")
    evidence_prices = {p for h in evidence.get("hits", []) for p in prices_in(h["text"])}
    budget = budget_numbers(question)
    for sentence in sentences(text):
        for price in prices_in(sentence):
            if price not in evidence_prices and not (price in budget and _has_budget_word(sentence)):
                problems.append(f"price_not_in_evidence: {price}")
    return problems


def _check_order(text: str, evidence: dict) -> list[str]:
    order, problems = evidence["order"], []
    mentioned = _order_statuses_mentioned(text)
    if mentioned != {order["status"]}:
        problems.append(f"order_status_mismatch: the answer says {sorted(mentioned) or 'no status'}, the order is {order['status']}")
    for number in {n for n in _ORDER_NUMBER.findall(text)} - {order["number"]}:
        problems.append(f"order_number_mismatch: {number} is not the order number")
    if prices_in(text):
        problems.append("price_not_in_evidence: the order results contain no amount")
    for sku in sorted(set(SKU.findall(text)) - {line["sku"] for line in order["lines"]}):
        problems.append(f"unknown_sku: {sku} is not in the order")
    return problems


# --- the fixed answers that replace a rejected one ----------------------------------------------------------------------------------------


def _price(vnd: int) -> str:
    return f"{vnd:,}".replace(",", ".") + "đ"


def _date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).strftime("%d/%m/%Y")
    except (TypeError, ValueError):
        return str(iso)[:10]


def _quote(hits: list[dict]) -> str:
    """The clauses quoted whole, each under its title, then one citation line naming all of them."""
    quoted = [f"Theo chính sách của cửa hàng ({hit['title']}):\n{hit['text'].strip()}" for hit in hits]
    return "\n\n".join(quoted) + "\n" + citation_footer([(hit["file"], hit["clause"]) for hit in hits])


def fallback_text(kind: str, evidence: dict) -> str:
    """The deterministic answer, built from the evidence alone. It never says more than the evidence says."""
    if kind == "products":
        lines = [f"- {h['name']} (SKU {h['sku']}): {_price(h['price_vnd'])}, {'còn hàng' if h['in_stock'] else 'hết hàng'}, "
                 f"kích cỡ {', '.join(h['sizes'])}" for h in evidence["hits"][:3]]
        return "Mình tìm thấy các sản phẩm sau theo dữ liệu cửa hàng:\n" + "\n".join(lines)
    if kind == "policies":
        return _quote(evidence["hits"][:1])
    order = evidence["order"]
    items = "; ".join(f"{line['name']} (SKU {line['sku']}), số lượng {line['quantity']} (đã chuyển đi {line['quantity_fulfilled']})" for line in order["lines"])
    label = STATUS_LABELS.get(order["status"], f"trạng thái {order['status']}")
    parts = [f"Đơn #{order['number']}: {label}.", f"Ngày tạo: {_date(order['created'])}.", f"Sản phẩm: {items}."]
    tracking = [f["tracking_number"] for f in order["fulfillments"] if f.get("tracking_number")]
    if tracking:
        parts.append("Mã vận đơn: " + ", ".join(tracking) + ".")
    return " ".join(parts)


def bounded_fallback(kind: str, text: str, evidence: dict, *, cited_skus=(), cited_clauses=()) -> str:
    """The fixed answer that replaces a rejected generated `text`, and says no more than that text did (A21).

    products: only the products the text named (a SKU in the text, or cited), in the order of the evidence; none -> the fixed no-information reply.
    policies: only the clauses the text cited, at most MAX_QUOTED_CLAUSES, quoted whole with their citation; none -> the fixed no-information reply.
    order:    the whole statement of the verified order: the text was an answer to "where is my order", and this is that answer made of facts only.
    `fallback_text` alone is the answer of the no-LLM mode, which has no text to stay within."""
    if kind == "products":
        named = set(SKU.findall(text or "")) | set(cited_skus)
        hits = [h for h in evidence["hits"] if h["sku"] in named]
        return fallback_text(kind, {**evidence, "hits": hits}) if hits else T.NO_INFORMATION
    if kind == "policies":
        cited = {(c["file"], c["clause"]) for c in cited_clauses}
        hits = [h for h in evidence["hits"] if (h["file"], h["clause"]) in cited][:MAX_QUOTED_CLAUSES]
        return _quote(hits) if hits else T.NO_POLICY_INFO
    return fallback_text(kind, evidence)


def citation_footer(pairs) -> str:
    return "Nguồn: " + "; ".join(f"{file}, Điều {clause}" for file, clause in pairs)
