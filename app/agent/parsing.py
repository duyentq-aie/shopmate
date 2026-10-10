"""Rules that read a customer's follow-up message: emails, order numbers, phone numbers, yes or no.

No LLM reads a follow-up (docs/DECISIONS-phase6.md, A2). The rules are conservative on purpose: what they do not
understand is not an email, not an order number and, above all, not a yes (A6: no explicit yes, no ticket).
"""

from __future__ import annotations

import re

from app.retrieval.folding import fold, tokenize

EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<![\d@])(?:\+?84|0)(?:[\s.\-]?\d){9}(?!\d)")
MAX_ORDER_DIGITS = 7  # a longer run of digits is a phone number or an amount, never an order number
_DIGITS = r"([0-9]{1," + str(MAX_ORDER_DIGITS) + r"})(?![0-9])"
_ORDER_AFTER_WORD = re.compile(r"\b(?:don|order)(?:\s+hang)?(?:\s+so|\s+ma)?\s*[:#]?\s*" + _DIGITS
                               + r"(?!\s*(?:k\b|d\b|dong\b|vnd\b|%|ngay\b|cai\b|chiec\b|nghin\b|trieu\b))")
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
    """An order number the message says is one ("đơn 88", "#88", "mã đơn: 2057"). A bare number counts only when the
    agent has just asked for the order number (bare_ok)."""
    text = _without_emails(text or "")
    if bare_ok:
        bare = _BARE_NUMBER.match(text.strip())
        if bare:
            return bare.group(1)
    text = PHONE.sub(" ", text)
    folded = fold(text)
    for pattern in (_ORDER_AFTER_HASH, _ORDER_AFTER_WORD):
        match = pattern.search(folded)
        if match:
            return match.group(1)
    return None


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
