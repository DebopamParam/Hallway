"""Hallway privacy guard.

Speaks the Switch Trust guardrails wire protocol (`POST /guardrails/check`), so
Switch itself calls it for every human message and every agent send before the
message is stored. Point Switch's Trust settings at the real Switch Trust
service instead and nothing else changes.

Detectors are deliberately simple and explainable:
- contact details (emails, phone numbers, UPI ids) and government / financial ids -> REDACTED
- any attendee's private contact values, verbatim -> REDACTED
- travel / calendar / home / hotel room details -> REDACTED
- questions fishing for private details -> ALERTED (stored, flagged in-room)

Everything is in-process and every pattern is compiled once at import: Switch
waits 2 s for an answer and then lets the message through unchecked, so a scan
must stay in the sub-millisecond range. tests/test_guard_corpus.py measures
precision, recall and speed on a labelled corpus.
"""
import re
from collections.abc import Iterable
from dataclasses import dataclass

# ------------------------------------------------------------------ phones
# Digits joined by at most one separator between groups.
_SEP = r"[ .\-]"
_NOT_BEFORE = r"(?<![\w+₹$€£@/.:=#-])"  # not inside a word, an amount, a URL query or a longer number
_NOT_AFTER = r"(?![\w@]|[.\-:]\d|-[A-Za-z])"

PHONE_PATTERNS = [
    # International: "+91 98111 22233", "+1 (415) 555-0134", "+44 20 7946 0958", "+91.98111.22233".
    re.compile(r"(?<![\w+])\(?\+\d{1,3}\)?(?:[ .\-]?\(?\d{1,5}\)?){1,5}(?![\w@])"),
    # Indian mobile, 10 digits from 6-9, with an optional 0 / 91 / (+91) prefix:
    # "9811122233", "98111 22233", "098111-22233", "91 98111 22233", "98 1112 2233", "981 112 2233".
    re.compile(_NOT_BEFORE.replace("/", "") + r"(?:(?:\(?\+?91\)?|0)" + _SEP + r"?)?"
               r"(?:[6-9]\d{4}" + _SEP + r"?\d{5}|[6-9]\d" + _SEP + r"\d{4}" + _SEP + r"\d{4}"
               r"|[6-9]\d{2}" + _SEP + r"\d{3}" + _SEP + r"\d{4}|[6-9]\d{3}" + _SEP + r"\d{3}" + _SEP + r"\d{3})"
               + _NOT_AFTER),
    # North American "415-555-0134" / "(415) 555 0134": separators required.
    re.compile(_NOT_BEFORE + r"\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4}" + _NOT_AFTER),
    # Indian landline with an STD code: "080-4123 4567", "011 2345 6789", "080-41234567".
    re.compile(_NOT_BEFORE + r"0\d{2,4}[ \-](?:\d{3,4}[ \-]?\d{4}|\d{6,8})" + _NOT_AFTER),
    # Anything number-shaped right after a phone word: "call 2345 6789", "phone: 2345678".
    re.compile(r"(?i)\b(?:call|ring|text|sms|whats\s?app|phone|mobile|cell|tel|landline|number\s*(?:is|:))"
               r"(?![a-z])[^\d\n]{0,12}?(?<![A-Za-z])(\(?\+?\d[\d ().\-]{5,16}\d)" + _NOT_AFTER),
]
_DATE_LIKE = re.compile(r"^(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}$|^\d{1,2}[-/.]\d{1,2}[-/.](?:19|20)\d{2}$")
_CURRENCY_BEFORE = re.compile(r"(?i)(?:₹|\$|€|£|rs\.?|inr|usd)\s*$")
# A bare run of digits followed by a count noun is a quantity, not a phone.
_QUANTITY_AFTER = re.compile(
    r"(?i)^\s*(?:rows|users|requests|transactions|txns|records|events|downloads|parameters|params|tokens|"
    r"rps|qps|calls|messages|views|installs|merchants|customers|people|attendees|times|bytes|[kmgt]?b\b)")


def _phone_spans(content: str) -> Iterable[tuple[int, int]]:
    for i, pattern in enumerate(PHONE_PATTERNS):
        for m in pattern.finditer(content):
            g = m.lastindex or 0
            start, end = m.span(g)
            text = content[start:end]
            digits = re.sub(r"\D", "", text)
            if not 7 <= len(digits) <= 15 or _DATE_LIKE.match(text.strip("() ")):
                continue
            if _CURRENCY_BEFORE.search(content[max(0, start - 5):start]):
                continue
            bare = text.isdigit()
            if bare and _QUANTITY_AFTER.match(content[end:end + 16]):
                continue
            if i == 0 and len(digits) < 8:  # "+1 2025" and the like
                continue
            yield start, end


# ------------------------------------------------------------------ ids
def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


_CARD = re.compile(r"(?<![\w./=])(?:\d{4}[ \-]?){3}\d{1,7}(?![\w.])")

# -------------------------------------------------------------- flights
# Airline codes whose two letters are also common words / acronyms in event
# talk ("AI 2026", "UK 2025", "AA 101"): a flight needs the number glued on
# ("AI505", "AI-505") or a travel word nearby.
_AMBIGUOUS_AIRLINES = {"AI", "UK", "AA", "UA", "BA", "DL", "LH", "SG", "TK", "AF"}
_FLIGHT = re.compile(
    r"\b(6E|AI|UK|SG|QP|IX|I5|G8|EK|QR|EY|BA|LH|SQ|UA|AA|DL|TK|AF|KL|CX|TG|MH|WY|GF|SV)([ \-]?)(\d{2,4})\b")
_STRONG_TRAVEL = (r"flight|flying|fly|flies|plane|board(?:ing)?|depart(?:s|ing|ure)?|lands?|landing|"
                  r"arriv(?:e|es|ing)|catch(?:ing)?|gate|pnr|seat|terminal|booked|check-?in")
_STRONG_CONTEXT = re.compile(r"(?i)\b(?:" + _STRONG_TRAVEL + r")\b")
# Enough for an unambiguous number ("on AI 863") but not for a year ("working on AI 2026").
_WEAK_CONTEXT = re.compile(r"(?i)\b(?:" + _STRONG_TRAVEL + r"|on|take[s]?|taking|leaves|leaving|tonight|tomorrow|"
                           r"\d{1,2}(?::\d{2})?\s?(?:am|pm))\b")
_YEAR = re.compile(r"^(?:19|20)\d{2}$")


def _flight_spans(content: str) -> Iterable[tuple[int, int]]:
    for m in _FLIGHT.finditer(content):
        code, sep, number = m.groups()
        if m.start() > 0 and content[m.start() - 1] == "#":  # a hashtag: "#AI2026"
            continue
        near = content[max(0, m.start() - 30):m.start()] + " " + content[m.end():m.end() + 30]
        if _YEAR.match(number) and not _STRONG_CONTEXT.search(near):
            continue
        if code in _AMBIGUOUS_AIRLINES and sep == " " and not _WEAK_CONTEXT.search(near):
            continue
        yield m.start(), m.end()


# ------------------------------------------------------------ itinerary
# Moving around the venue isn't a travel plan: "heading to Hall B".
_VENUE_WORDS = (
    "Hall|Room|Stage|Booth|Track|Lobby|Auditorium|Session|Expo|Workshop|Lab|Floor|Level|Registration|Lunch|"
    "Dinner|Breakfast|Coffee|Keynote|Panel|Talk|Demo|Hallway|Series|Postgres|Production|Prod|Staging|Desk|"
    "Cafe|Café|Canteen|Main|Track|Zone|Area|Block|Wing|Tent|Arena|Pavilion|Terrace|Garden")
_ITINERARY = re.compile(
    r"\b(?:(?:fly|flying|flies|flight|travel+ing|travel+s?|trip|leaving|leave|heading|headed|driving|drive|"
    r"train|bus|cab|returning|return|going\s+back|flying\s+back|heading\s+back)\s+(?:back\s+)?(?:to|for)"
    r"|(?:landing|land|lands|arriving|arrive|arrives)\s+(?:in|at))\s+"
    r"(?!(?:" + _VENUE_WORDS + r")\b)([A-Z][a-zA-Z]+)"
    r"(?:\s+(?:on|next|this|at|tomorrow|tonight)\b(?:\s+[A-Za-z0-9:]+)?)?",
    re.IGNORECASE)

# ------------------------------------------------------- home and hotel
# A city or country on its own is not private ("I live in Bangalore").
_PLACES = {
    "bangalore", "bengaluru", "mumbai", "bombay", "delhi", "new delhi", "chennai", "hyderabad", "pune", "kolkata",
    "ahmedabad", "jaipur", "kochi", "goa", "gurgaon", "gurugram", "noida", "mysuru", "mysore", "india", "the us",
    "usa", "us", "uk", "london", "singapore", "dubai", "berlin", "paris", "new york", "san francisco", "sf", "nyc",
    "seattle", "toronto", "tokyo", "sydney", "europe", "asia", "canada", "germany", "australia", "japan",
}
_HOME = re.compile(
    r"\b(?:[Ii]|[Ss]he|[Hh]e|[Tt]hey|[Ww]e|[A-Z][a-z]+)\s+(?:live|lives|living|stay|stays)\s+(?:at|in|on)\s+"
    r"(?=[A-Z0-9#])([^.;!?\n]{3,60})")
_ADDRESS_IS = re.compile(r"(?i)\b(?:my|his|her|their|our)\s+(?:home\s+|house\s+|flat\s+)?address\s+is\b[^;!?\n]{3,80}")
_STAYING = re.compile(
    r"\b(?:I'?m|I\s+am|[Ww]e'?re|[Ww]e\s+are|[Ss]he'?s|[Hh]e'?s|[Tt]hey'?re|[A-Z][a-z]+\s+is)\s+staying\s+(?:at|in)\s+"
    r"(?:the\s+)?(?=[A-Z])([^.;!?\n]{2,60})")
_HOTEL_ROOM = re.compile(
    r"(?i)\b(?:(?:my|his|her|their|our)\s+(?:hotel\s+)?room|hotel\s+room)\s*(?:no\.?|number|#)?\s*(?:is\s*)?#?\d{2,5}\b")


_PLACE_PREFIX = re.compile(r"(?i)(?:" + "|".join(sorted(map(re.escape, _PLACES), key=len, reverse=True)) + r")\b")


def _place_only(text: str) -> bool:
    """Just a city or country, then nothing more specific: "Bangalore these days",
    "Pune, came by train". A street, area or number after it is private."""
    m = _PLACE_PREFIX.match(text.strip())
    return bool(m) and not re.search(r"[A-Z0-9#]", text.strip()[m.end():])


# --------------------------------------------------- all redaction rules
PRIVATE_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("contact", "email", re.compile(r"(?<![\w.+-])(?!git@)[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63}){0,6}\.[A-Za-z]{2,24}\b")),
    ("contact", "email", re.compile(
        r"(?i)\b[\w.+-]{1,64}\s?(?:\[at\]|\(at\)|\{at\})\s?[\w-]{1,63}(?:\s?(?:\[dot\]|\(dot\)|\{dot\}|\.)\s?[\w-]{1,63}){1,6}")),
    # "asha at acme dot io": a plain "at" only counts with a spelled-out "dot".
    ("contact", "email", re.compile(r"(?i)\b[\w.+-]{1,64} at [\w-]{1,63}(?: dot [\w-]{1,63}){1,6}\b")),
    ("contact", "upi", re.compile(
        r"(?i)(?<![\w.-])[\w.-]{2,64}@(?:ok(?:axis|icici|sbi|hdfcbank)|ybl|ibl|axl|apl|yapl|paytm|pt(?:yes|axis|hdfc|sbi)|upi|"
        r"icici|sbi|hdfcbank|axisbank|axis|kotak|idfcbank|idfcfirst|freecharge|jupiteraxis|fbl|airtel|"
        r"barodampay|wa(?:axis|hdfcbank|sbi|icici)|indus|federal|rbl|aubank|kmbl|yesbank|unionbank|pnb)\b(?!\.\w)")),
    ("contact", "upi", re.compile(r"(?i)\b(?:upi|vpa|gpay|phonepe)(?:\s*id)?\s*(?:is|:|-)?\s*([\w.-]{2,64}@[a-z]{2,32})\b")),
    ("government_id", "aadhaar", re.compile(r"(?<![\w-])\d{4}([ \-])\d{4}\1\d{4}(?![\w-])")),
    ("government_id", "aadhaar", re.compile(r"(?i)\baadhaa?r\b[^\d\n]{0,15}(\d{12})\b")),
    ("government_id", "pan", re.compile(r"\b[A-Z]{3}[PCHFATBLJG][A-Z]\d{4}[A-Z]\b")),
    ("government_id", "pan", re.compile(r"(?i)\bpan\b[^\w\n]{0,3}(?:(?:no\.?|number|card|is)\s*[:#-]?\s*)*([a-z]{5}\d{4}[a-z])\b")),
    ("government_id", "passport", re.compile(r"(?i)\bpassport\b[^.\n]{0,20}?\b([A-Z]\d{7})\b")),
    ("financial", "bank_account", re.compile(
        r"(?i)\b(?:a/c|acct|account)\s*(?:no\.?|number|#)?\s*(?:is\s*)?[:#-]?\s*(\d{9,18})\b")),
    ("location", "home_address", _ADDRESS_IS),
    ("location", "hotel_room", _HOTEL_ROOM),
    ("calendar", "calendar_detail", re.compile(
        r"(?i)\b(?:my|his|her|their)\s+(?:calendar|schedule|inbox|email)\s+(?:says|shows|has)\b[^.;!?]{0,80}")),
    ("travel", "pnr", re.compile(r"\bPNR\b\s*(?:is|:|no\.?|#)?\s*([A-Z0-9]{6})\b")),
]

PROBE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("probe_contact", re.compile(
        r"(?i)\b(?:what(?:'s| is| are)|share|send|give|tell|forward|pass|get)\b[^?.!]{0,30}?"
        r"\b(?:your|his|her|their|(?-i:[A-Z][a-z]+)'s)\s+(?:personal\s+|private\s+|home\s+|work\s+|direct\s+)?"
        r"(?:phone|number(?!\s+(?:one|1|of)\b)|e-?mail|whats\s?app|mobile|cell|contact|address|telegram|signal)\b")),
    ("probe_travel", re.compile(
        r"(?i)\bwhere\s+(?:are|is|will|do|does)\s+(?:you|he|she|they|(?-i:[A-Z][a-z]+))\b[^?]{0,25}?"
        r"\b(?:fly|flying|travel|travell?ing|going next|headed|heading|staying|stay)\b")),
    ("probe_travel", re.compile(
        r"(?i)\b(?:which|what)\s+(?:hotel|flight|room)\b[^?]{0,30}?\b(?:you|he|she|they|her|his|their)\b")),
    ("probe_private", re.compile(
        r"(?i)\b(?:your|his|her|their|(?-i:[A-Z][a-z]+)'s)\s+(?:\w+\s+)?"
        r"(?:calendar|inbox|e-?mails|salary|ctc|compensation|home address)\b[^?]{0,40}\?")),
    ("probe_private", re.compile(
        r"(?i)\b(?:what|tell|share|send|show|read|forward|check)\b[^?.!]{0,30}?\b(?:your|his|her|their|(?-i:[A-Z][a-z]+)'s)\s+"
        r"(?:\w+\s+)?(?:inbox|e-?mails|salary|ctc|compensation|home address)\b")),
    ("probe_private", re.compile(r"(?i)\bwhere\s+(?:do|does)\s+\w+\s+(?:live|stay)\b")),
]


@dataclass
class Finding:
    category: str
    detector: str
    text: str
    severity: str = "high"


class PrivateMatcher:
    """Attendees' private contact values, compiled into one case-insensitive
    pattern. Build it once per change of the values (Hallway caches it)."""

    MIN_LEN = 5

    def __init__(self, values: Iterable[str]):
        self.values = sorted({v.strip() for v in values if v and len(v.strip()) >= self.MIN_LEN}, key=len, reverse=True)
        self.pattern = re.compile("|".join(re.escape(v) for v in self.values), re.IGNORECASE) if self.values else None

    def finditer(self, content: str) -> Iterable[tuple[int, int]]:
        if self.pattern is not None:
            for m in self.pattern.finditer(content):
                yield m.start(), m.end()


def _spans(content: str, private: PrivateMatcher) -> list[tuple[int, int, str, str]]:
    spans: list[tuple[int, int, str, str]] = []
    for s, e in private.finditer(content):
        spans.append((s, e, "contact", "attendee_private_value"))
    for s, e in _phone_spans(content):
        spans.append((s, e, "contact", "phone"))
    for m in _CARD.finditer(content):
        digits = re.sub(r"\D", "", m.group(0))
        if 13 <= len(digits) <= 19 and _luhn(digits):
            spans.append((m.start(), m.end(), "financial", "card"))
    for s, e in _flight_spans(content):
        spans.append((s, e, "travel", "flight"))
    for m in _ITINERARY.finditer(content):
        # IGNORECASE is for the verbs; the destination must really be capitalised.
        if m.group(1)[0].isupper():
            spans.append((m.start(), m.end(), "travel", "itinerary"))
    for pattern in (_HOME, _STAYING):
        for m in pattern.finditer(content):
            if not _place_only(m.group(1)):
                spans.append((m.start(), m.end(), "location", "home_address"))
    for category, detector, pattern in PRIVATE_PATTERNS:
        for m in pattern.finditer(content):
            s, e = m.span(m.lastindex or 0) if detector in _GROUP_ONLY else m.span()
            spans.append((s, e, category, detector))
    return spans


# Detectors whose finding is the captured value only, not the label before it.
_GROUP_ONLY = {"upi", "pan", "passport", "bank_account", "pnr"}


def scan(content: str, extra_private: "list[str] | PrivateMatcher | None" = None) -> tuple[str, list[Finding]]:
    """Returns (outcome, findings) where outcome is ok | redacted | alerted.

    Overlapping hits are merged into one span, so each finding's text is a
    distinct substring of `content` and replacing them in any order is safe."""
    private = extra_private if isinstance(extra_private, PrivateMatcher) else PrivateMatcher(extra_private or [])
    spans = _spans(content, private)
    if spans:
        spans.sort()
        merged: list[list] = []
        for start, end, cat, det in spans:
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
                if cat not in merged[-1][2]:
                    merged[-1][2].append(cat)
                merged[-1][3].append(det)
            else:
                merged.append([start, end, [cat], [det]])
        findings = []
        for s, e, c, d in merged:
            text = content[s:e].strip()
            if text:
                findings.append(Finding(c[0] if len(c) == 1 else "+".join(c), "+".join(dict.fromkeys(d)), text))
        if findings:
            return "redacted", findings
    for detector, pattern in PROBE_PATTERNS:
        m = pattern.search(content)
        if m:
            return "alerted", [Finding("privacy_probe", detector, m.group(0), "medium")]
    return "ok", []


def redact(content: str, findings: list[Finding]) -> str:
    # Longest first, so a finding inside another's text can't leave a fragment.
    for f in sorted(findings, key=lambda f: len(f.text), reverse=True):
        content = content.replace(f.text, "[redacted]")
    return content


WIRE = {
    "ok": "GUARDRAIL_RESULT_OUTCOME_OK",
    "redacted": "GUARDRAIL_RESULT_OUTCOME_REDACTED",
    "alerted": "GUARDRAIL_RESULT_OUTCOME_ALERTED",
    "blocked": "GUARDRAIL_RESULT_OUTCOME_BLOCKED",
}


def wire_response(outcome: str, findings: list[Finding]) -> dict:
    return {
        "outcome": WIRE[outcome],
        "policy_id": "hallway-privacy",
        "policy_name": "Hallway privacy",
        "findings": [
            {
                "category": f.category,
                "detector_name": f.detector,
                "severity": f.severity,
                "detected_string": f.text,
            }
            for f in findings
        ],
    }


def blocked_response(reason: str) -> dict:
    """What Switch gets when the guard could not check a message (an internal
    error, or the guard is over its rate limit). BLOCKED is the one outcome
    Switch refuses to deliver; any non-2xx answer, a timeout or an ERRORED
    outcome would let the message through unchecked (Switch fails open)."""
    return {
        "outcome": WIRE["blocked"],
        "policy_id": "hallway-privacy",
        "policy_name": "Hallway privacy",
        "findings": [{"category": "guard_unavailable", "detector_name": reason, "severity": "high",
                      "detected_string": ""}],
    }
