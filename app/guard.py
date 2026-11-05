"""Hallway privacy guard.

Speaks the Switch Trust guardrails wire protocol (`POST /guardrails/check`), so
Switch itself calls it for every human message and every agent send before the
message is stored. Point Switch's Trust settings at the real Switch Trust
service instead and nothing else changes.

Detectors are deliberately simple and explainable:
- contact details (emails, phone numbers) and government ids -> REDACTED
- any attendee's private contact values, verbatim -> REDACTED
- travel / calendar / home details -> REDACTED
- questions fishing for private details -> ALERTED (stored, flagged in-room)
"""
import re
from dataclasses import dataclass

PRIVATE_PATTERNS: list[tuple[str, str, re.Pattern]] = [
    ("contact", "email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("contact", "phone", re.compile(r"(?<![\w])(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{2,5}\)?[\s-]?)?\d{3,5}[\s-]?\d{4,5}(?![\w])")),
    ("government_id", "aadhaar", re.compile(r"\b\d{4}\s\d{4}\s\d{4}\b")),
    ("government_id", "pan", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),
    ("government_id", "passport", re.compile(r"(?i)\bpassport (?:no\.?|number)?\s*[:#]?\s*[A-Z]\d{7}\b")),
    ("travel", "flight", re.compile(r"\b(?:AI|6E|UK|SG|QP|IX|EK|QR|BA|LH|SQ|UA|AA|DL)[\s-]?\d{2,4}\b")),
    ("travel", "itinerary", re.compile(
        r"(?i)\b(?:flying|fly|flight|travell?ing|trip|leaving|heading)\s+(?:to|for)\s+[A-Z][a-zA-Z]+"
        r"(?:\s+(?:on|next|this)\s+[A-Za-z0-9]+)?")),
    ("location", "home_address", re.compile(
        r"(?i)\b(?:i|she|he|they)\s+(?:live|lives|stay|stays)\s+(?:at|in|on)\s+[^.,;!?]{3,60}")),
    ("calendar", "calendar_detail", re.compile(
        r"(?i)\b(?:my|his|her|their)\s+(?:calendar|schedule|inbox|email)\s+(?:says|shows|has)\b[^.;!?]{0,80}")),
]

PROBE_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("probe_contact", re.compile(r"(?i)\b(?:what(?:'s| is)|share|send|give me)\b[^?]{0,40}\b(?:phone|number|email|whatsapp|address)\b")),
    ("probe_travel", re.compile(r"(?i)\bwhere\b[^?]{0,30}\b(?:fly|flying|travel|travell?ing|going next|trip)\b")),
    ("probe_private", re.compile(r"(?i)\b(?:calendar|inbox|emails?|salary|budget|home address|where (?:do|does) \w+ live)\b[^?]{0,40}\?")),
]

# A phone-shaped match that is really a year, a time or a small number is noise.
_NOT_PHONE = re.compile(r"^\d{1,6}$|^(?:19|20)\d{2}$")


@dataclass
class Finding:
    category: str
    detector: str
    text: str
    severity: str = "high"


def scan(content: str, extra_private: list[str] | None = None) -> tuple[str, list[Finding]]:
    """Returns (outcome, findings) where outcome is ok | redacted | alerted.

    Overlapping hits are merged into one span, so each finding's text is a
    distinct substring of `content` and replacing them in any order is safe."""
    spans: list[tuple[int, int, str, str]] = []
    lower = content.lower()
    for value in extra_private or []:
        if value and len(value) >= 5:
            i = lower.find(value.lower())
            if i >= 0:
                spans.append((i, i + len(value), "contact", "attendee_private_value"))
    for category, detector, pattern in PRIVATE_PATTERNS:
        for m in pattern.finditer(content):
            digits = re.sub(r"\D", "", m.group(0))
            if detector == "phone" and (_NOT_PHONE.match(digits) or len(digits) < 8):
                continue
            spans.append((m.start(), m.end(), category, detector))
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
        findings = [Finding(c[0] if len(c) == 1 else "+".join(c), "+".join(dict.fromkeys(d)), content[s:e].strip())
                    for s, e, c, d in merged]
        return "redacted", findings
    for detector, pattern in PROBE_PATTERNS:
        m = pattern.search(content)
        if m:
            return "alerted", [Finding("privacy_probe", detector, m.group(0), "medium")]
    return "ok", []


def redact(content: str, findings: list[Finding]) -> str:
    for f in findings:
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
