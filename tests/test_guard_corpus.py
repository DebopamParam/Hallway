"""Precision and recall of the privacy guard on a labelled corpus (tests/guard_corpus.py).

Run `.venv/bin/python -m tests.test_guard_corpus` for the per-group table."""
import time
from collections import Counter

import pytest

from app import guard

from .guard_corpus import CORPUS, PRIVATE_VALUES, groups

GROUP_OF = {
    "phone": "phone", "email": "email", "upi": "upi",
    "aadhaar": "id", "pan": "id", "passport": "id", "card": "id", "bank_account": "id",
    "flight": "flight", "pnr": "flight",
    "itinerary": "itinerary",
    "home_address": "address", "hotel_room": "address",
    "calendar_detail": "calendar",
    "attendee_private_value": "private_value",
}
GROUPS = ["phone", "email", "upi", "id", "flight", "itinerary", "address", "calendar", "private_value", "probe"]

# Must-block groups that must never be missed, and the floor for the others.
FULL_RECALL = {"phone", "email", "upi", "id", "private_value"}
MIN_RECALL = 0.9
MIN_PRECISION = 0.9
MAX_FALSE_POSITIVES = 2  # must-pass messages the guard redacts or alerts on


def predicted(outcome: str, findings) -> set[str]:
    if outcome == "alerted":
        return {"probe"}
    return {GROUP_OF.get(d, d) for f in findings for d in f.detector.split("+")}


def evaluate(scan=guard.scan, redact=guard.redact) -> dict:
    tp, fp, fn = Counter(), Counter(), Counter()
    false_pos: list[tuple[str, set[str]]] = []
    missed: list[tuple[str, str]] = []
    leaks: list[str] = []
    for case in CORPUS:
        outcome, findings = scan(case.text, PRIVATE_VALUES)
        got, want = predicted(outcome, findings), groups(case)
        if not want and outcome != "ok":
            false_pos.append((case.text, got))
        for g in got:
            (tp if g in want else fp)[g] += 1
        for g in want - got:
            fn[g] += 1
            missed.append((case.text, g))
        if case.leak and outcome == "redacted" and case.leak.lower() in redact(case.text, findings).lower():
            leaks.append(case.text)
    table = {}
    for g in GROUPS:
        p = tp[g] / (tp[g] + fp[g]) if tp[g] + fp[g] else 1.0
        r = tp[g] / (tp[g] + fn[g]) if tp[g] + fn[g] else 1.0
        table[g] = {"precision": p, "recall": r, "tp": tp[g], "fp": fp[g], "fn": fn[g]}
    return {"table": table, "false_positives": false_pos, "missed": missed, "leaks": leaks}


def report(result: dict) -> str:
    lines = [f"{'group':<14}{'precision':>10}{'recall':>8}{'tp':>5}{'fp':>5}{'fn':>5}"]
    for g, m in result["table"].items():
        lines.append(f"{g:<14}{m['precision']:>10.2f}{m['recall']:>8.2f}{m['tp']:>5}{m['fp']:>5}{m['fn']:>5}")
    lines.append(f"must-pass messages flagged: {len(result['false_positives'])}")
    lines += [f"  FP {t!r} -> {sorted(g)}" for t, g in result["false_positives"]]
    lines += [f"  MISS {g}: {t!r}" for t, g in result["missed"]]
    lines += [f"  LEAK {t!r}" for t in result["leaks"]]
    return "\n".join(lines)


def test_corpus_is_big_and_balanced():
    blocks = [c for c in CORPUS if c.group]
    assert len(CORPUS) >= 150
    assert len(blocks) >= 60 and len(CORPUS) - len(blocks) >= 60
    assert len({c.text for c in CORPUS}) == len(CORPUS), "duplicate corpus entries"
    assert all(g in GROUPS for c in blocks for g in groups(c))


def test_precision_and_recall_per_group():
    result = evaluate()
    print("\n" + report(result))
    for g, m in result["table"].items():
        floor = 1.0 if g in FULL_RECALL else MIN_RECALL
        assert m["recall"] >= floor, f"{g} recall {m['recall']:.2f}\n{report(result)}"
        assert m["precision"] >= MIN_PRECISION, f"{g} precision {m['precision']:.2f}\n{report(result)}"
    assert len(result["false_positives"]) <= MAX_FALSE_POSITIVES, report(result)


def test_redaction_removes_the_sensitive_text():
    assert evaluate()["leaks"] == []


def test_scan_is_fast_on_the_corpus():
    """Switch waits 2 s, then lets the message through unchecked: stay far below."""
    texts = [c.text for c in CORPUS]
    matcher = guard.PrivateMatcher(PRIVATE_VALUES * 50)  # ~200 attendees' private values
    worst = 0.0
    start = time.perf_counter()
    for _ in range(5):
        for t in texts:
            t0 = time.perf_counter()
            guard.scan(t, matcher)
            worst = max(worst, time.perf_counter() - t0)
    mean = (time.perf_counter() - start) / (5 * len(texts))
    assert mean < 0.002 and worst < 0.05, (mean, worst)
    # A long paste stays fast too (no catastrophic backtracking).
    t0 = time.perf_counter()
    guard.scan(" ".join(texts) * 5, matcher)
    assert time.perf_counter() - t0 < 0.5


@pytest.mark.parametrize("unit", ["98-", "a.", "1.", "0-", "a@a.", "1 ", "+1 ", "a [at] ", "x.x.x.@", "flying back "])
def test_no_quadratic_blowup(unit):
    """Repeated near-matches once took 25 s on 40 KB (an unbounded email local
    part rescanned to the end from every position): keep every pattern linear."""
    text = unit * (16_000 // len(unit))  # about Mattermost's longest post
    t0 = time.perf_counter()
    guard.scan(text, guard.PrivateMatcher(PRIVATE_VALUES))
    assert time.perf_counter() - t0 < 0.5


if __name__ == "__main__":  # pragma: no cover
    print(report(evaluate()))
