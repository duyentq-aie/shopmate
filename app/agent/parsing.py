"""Rules that read a customer's follow-up message: emails, order numbers, phone numbers, yes or no.

No LLM reads a follow-up (docs/DECISIONS-phase6.md, A2). The rules are conservative on purpose: what they do not
understand is not an email, not an order number and, above all, not a yes (A6: no explicit yes, no ticket).
"""

from __future__ import annotations

import re
import unicodedata

from app.retrieval.folding import fold, tokenize

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\d@])(?:\+?84|0)(?:[\s.\-]?\d){9}(?!\d)")
MAX_ORDER_DIGITS = 7  # a longer run of digits is a phone number or an amount, never an order number
_DIGITS = r"([0-9]{1," + str(MAX_ORDER_DIGITS) + r"})(?![0-9]|[.,][0-9])"  # not the start of a longer number or of a grouped amount ("88.000")
_NOT_A_QUANTITY = r"(?!\s*(?:k\b|d\b|dong\b|vnd\b|%|ngay\b|cai\b|chiec\b|nghin\b|trieu\b))"  # "88k", "88đ", "88 ngày": an amount or a count
_ORDER_AFTER_WORD = re.compile(r"\b(?<!van )(?:don|order)(?:\s+hang)?(?:\s+so|\s+ma)?\s*[:#]?\s*" + _DIGITS + _NOT_A_QUANTITY)  # not "mã vận đơn 88"
# "mã 88", "mã số 88", "mã đặt hàng 88": the word "mã" alone is enough, but only the order words may stand between it and the number, so
# "mã giảm giá 88", "mã sản phẩm 88" and "mã size 38" are not read. Known limit: "áo mã 38" (a product code) reads as order 38; the slot is only a
# candidate for a lookup, which still needs the owner's email.
_ORDER_AFTER_MA = re.compile(r"\bma\b(?:\s+so)?(?:\s+(?:don(?:\s+hang)?|dat\s+hang|order))?(?:\s+so)?\s*[:#]?\s*" + _DIGITS + _NOT_A_QUANTITY)
_NOT_MA = re.compile(r"\b(?:mà|má|mả|mạ)\b")  # folded they are all "ma": a conjunction or a word of its own is not the word "mã"
_ORDER_AFTER_HASH = re.compile(r"#\s*" + _DIGITS)
_BARE_NUMBER = re.compile(r"^#?\s*([0-9]{1," + str(MAX_ORDER_DIGITS) + r"})$")

_YES_LEAD = {"co", "yes", "y", "ok", "oke", "okay", "vang", "duoc", "u", "uh", "dong", "xac"}
_YES_FILLER = {"tao", "di", "nhe", "nha", "a", "ah", "ak", "shop", "ban", "giup", "minh", "cho", "phieu", "ticket", "luon", "roi", "day",
               "do", "y", "nhan", "thanks", "cam", "on", "oi"}
_NO_TOKENS = {"khong", "ko", "k", "no", "thoi", "huy", "khoi"}
_NO_SPELLED = {"đừng"}  # with diacritics only: folded it is the same as "đúng" (correct), so the folded form is not trusted


def parse_emails(text: str) -> list[str]:
    """The distinct email addresses in the text, lowercased, in order of appearance."""
    found: list[str] = []
    for match in EMAIL.findall(text or ""):
        address = match.lower()
        if address not in found:
            found.append(address)
    return found


def mask_email(address: str) -> str:
    """khachhang02@example.com -> k***@example.com"""
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain}"


def mask_contacts(text: str) -> str:
    """The text with every email masked (k***@example.com) and every phone number replaced: for anything that quotes the customer."""
    text = EMAIL.sub(lambda match: mask_email(match.group(0)), text or "")
    return PHONE.sub("[số điện thoại]", text)


def _without_emails(text: str) -> str:
    return EMAIL.sub(" ", text or "")


def parse_phone(text: str) -> str | None:
    match = PHONE.search(_without_emails(text))
    return re.sub(r"\D", "", match.group(0)) if match else None


def parse_order_number(text: str, *, bare_ok: bool = False) -> str | None:
    """An order number the message says is one ("đơn 88", "#88", "mã đơn: 2057", "mã 88"). A bare number counts only when the
    agent has just asked for the order number (bare_ok)."""
    text = _without_emails(text or "")
    if bare_ok:
        bare = _BARE_NUMBER.match(text.strip())
        if bare:
            return bare.group(1)
    text = _NOT_MA.sub(" ", PHONE.sub(" ", unicodedata.normalize("NFC", text).lower()))  # NFC: "a" + a separate accent is the same letter
    folded = re.sub(r"\s+", " ", fold(text))
    for pattern in (_ORDER_AFTER_HASH, _ORDER_AFTER_WORD, _ORDER_AFTER_MA):
        match = pattern.search(folded)
        if match:
            return match.group(1)
    return None


# what may stand next to a no-word in a refusal that says nothing else: "không cần đâu shop", "thôi khỏi nhé", "không, cảm ơn bạn"
_DECLINE_FILLER = {"can", "dau", "nhe", "nha", "nhen", "a", "ah", "ak", "oi", "shop", "ban", "minh", "cam", "on", "roi", "day", "nua", "muon", "luon", "thanks",
                   "xin", "loi", "vay", "ha"}


def is_plain_decline(text: str) -> bool:
    """True only for a refusal that is NOTHING BUT a refusal ("không", "thôi", "không cần đâu", "không, cảm ơn"): at least one no-word, and every other word a
    courtesy. "không, cho mình xem áo khoác" is not one (it asks for something), nor is any message with a word this does not know: the router reads those.
    (`parse_yes_no` calls such messages "no" too, and also every message that merely contains a no-word, which is right for a ticket and too eager here.)"""
    raw = (text or "").lower()
    tokens = tokenize(raw)
    if not tokens:
        return False
    spoken_dung = "đừng" in set(re.findall(r"\w+", raw))  # folded it is "dung", the same as "đúng" (correct): only the spelled form counts
    if not (_NO_TOKENS & set(tokens) or spoken_dung):
        return False
    return all(t in _NO_TOKENS or t in _DECLINE_FILLER or (t == "dung" and spoken_dung) for t in tokens)


def parse_yes_no(text: str) -> str:
    """"yes", "no" or "unclear". Only a message made entirely of affirmations and courtesies is a yes; any no-word makes it a no,
    even next to a yes-word; everything else is unclear. Unclear is never treated as a yes."""
    raw = (text or "").lower()
    tokens = tokenize(raw)
    if not tokens:
        return "unclear"
    spelled = set(re.findall(r"\w+", raw))
    if _NO_TOKENS & set(tokens) or _NO_SPELLED & spelled:
        return "no"
    affirmation = tokens[0] in _YES_LEAD or tokens[:2] == ["tao", "di"]
    if affirmation and all(t in _YES_LEAD or t in _YES_FILLER for t in tokens):
        return "yes"
    return "unclear"
