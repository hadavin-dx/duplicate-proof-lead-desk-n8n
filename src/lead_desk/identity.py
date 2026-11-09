"""Email key, payload checks, difference log."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal, Mapping

from lead_desk.constants import COMPANY_MAX, EMAIL_MAX, MESSAGE_MAX, NAME_MAX, PHONE_MAX

# The apostrophe is a legal character of the part before the @ (RFC 5322 atext); it stays out of the domain.
# one plain address: no list, comment or quote characters (the sender kinds and the reminder address use it)
PLAIN_ADDRESS = re.compile(r'^[^\s<>\r\n\x00,;"()]+@[^\s<>\r\n\x00,;"()]+$')
_EMAIL_RE = re.compile(r"[^@\s<>\",;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+")
_CONTROL_OK_IN_MESSAGE = frozenset("\t\n\r")
_FIELD_LIMITS: tuple[tuple[str, int | None], ...] = (  # fixed order; email has no length rule here (its rule is below)
    ("name", NAME_MAX),
    ("email", None),
    ("phone", PHONE_MAX),
    ("company", COMPANY_MAX),
    ("message", MESSAGE_MAX),
)
_DIFF_FIELDS = ("name", "phone", "company", "message")


@dataclass
class Checked:  # result of identity.check_payload
    kind: Literal["ok", "review"]
    reason: str | None  # for review
    name: str | None
    email: str | None
    email_key: str | None
    phone: str | None
    company: str | None
    message: str | None  # normalised, None when empty


def normalise_email(s: str) -> str:
    """Strip and lower case; nothing else."""
    return s.strip().lower()


def encodable(s: str) -> bool:
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def has_control_char(s: str, allow: frozenset[str] = frozenset()) -> bool:
    """True for any control character (Unicode category Cc, which includes NUL) except those in `allow`. SQLite
    stops reading text at a NUL, so a NUL breaks its length checks: such text never reaches the database."""
    return any(c not in allow and unicodedata.category(c) == "Cc" for c in s)


_BIDI_CONTROLS = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069")


def has_invisible_format(s: str, message: bool = False) -> bool:
    """True for characters that show nothing or change how text reads: format characters (Cf: zero-width space,
    right-to-left override), line and paragraph separators (Zl, Zp). A message may keep the zero-width joiners that
    emoji and some scripts need, but not the line and paragraph separators or the direction overrides."""
    for c in s:
        cat = unicodedata.category(c)
        if cat in ("Zl", "Zp"):
            return True
        if cat == "Cf" and (not message or c in _BIDI_CONTROLS):
            return True
    return False


def _review(reason: str) -> Checked:
    return Checked("review", reason, None, None, None, None, None, None)


def _lower_keeps_ascii_apart(e: str) -> bool:
    """False when lower-casing turns a non-ASCII character into an ASCII one (the Kelvin sign U+212A becomes k, the
    dotted capital I U+0130 becomes i plus a mark): two different mailboxes would get one email key and be merged.
    Other non-ASCII letters (a capital E with an accent) lower-case to non-ASCII letters and stay separate."""
    return not any(not c.isascii() and any(x.isascii() for x in c.lower()) for c in e)


def _valid_email(e: str) -> bool:
    return (
        _lower_keeps_ascii_apart(e)
        and len(e) <= EMAIL_MAX
        and len(normalise_email(e)) <= EMAIL_MAX  # lower case can be longer (some letters become two characters)
        and _EMAIL_RE.fullmatch(e) is not None
        and not e.endswith(".")
        and not any(unicodedata.category(c).startswith("C") for c in e)
    )


def check_payload(obj: object) -> Checked:
    """Field types and lengths, completeness, email shape, in that order."""
    if not isinstance(obj, dict):
        return _review("bad_field:payload")
    vals: dict[str, str | None] = {}
    for field, limit in _FIELD_LIMITS:
        v = obj.get(field)
        if v is None:
            vals[field] = None
            continue
        if not isinstance(v, str):
            return _review(f"bad_field:{field}")
        if not encodable(v):  # a lone surrogate cannot be stored: a bad field, not a server error
            return _review(f"bad_field:{field}")
        if has_control_char(v, _CONTROL_OK_IN_MESSAGE if field == "message" else frozenset()):
            return _review(f"bad_field:{field}")  # checked before strip(): strip() removes some control characters
        if field != "email" and has_invisible_format(v, field == "message"):  # an email is checked as a whole below
            return _review(f"bad_field:{field}")
        t = v.strip()
        if limit is not None and len(t) > limit:
            return _review(f"bad_field:{field}")
        vals[field] = t or None
    missing = [f for f in ("name", "email") if vals[f] is None]
    if missing:
        return _review("incomplete:" + ",".join(missing))
    email = vals["email"]
    if email is None or not _valid_email(email):
        return _review("invalid_email")
    return Checked(
        "ok", None, vals["name"], email, normalise_email(email), vals["phone"], vals["company"], vals["message"]
    )


def _collapse(s: str | None) -> str:
    return re.sub(" +", " ", (s or "").strip())


def diff_fields(stored: Mapping[str, str | None], new: Mapping[str, str | None]) -> list[str]:
    """Names of name, phone, company, message where the new value differs, in that fixed order."""
    out = []
    for f in _DIFF_FIELDS:
        b = _collapse(new.get(f))
        if b and b != _collapse(stored.get(f)):
            out.append(f)
    return out
