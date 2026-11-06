"""Prometheus metrics, hand-rolled: counters, histograms and gauges read at
scrape time, rendered in the text exposition format (version 0.0.4).

No dependency: Hallway is one process, so there's nothing to aggregate, and
the whole format is a few lines. Label values are always from a small fixed
set (route templates, tool names, outcomes), never user input, so the series
count stays bounded.
"""

import math
from collections.abc import Callable, Iterable

Labels = tuple[tuple[str, str], ...]

# Seconds. Requests go to Switch and Mattermost; the guard must answer within 2 s.
LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0)


def _labels(labels: dict[str, str]) -> Labels:
    return tuple(sorted(labels.items()))


def _fmt_labels(labels: Labels, extra: Labels = ()) -> str:
    pairs = (*labels, *extra)
    if not pairs:
        return ""
    esc = (v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"') for _, v in pairs)
    return "{" + ",".join(f'{k}="{v}"' for (k, _), v in zip(pairs, esc, strict=True)) + "}"


def _fmt_value(v: float) -> str:
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    return repr(float(v)) if not float(v).is_integer() else str(int(v))


class Metric:
    kind = ""

    def __init__(self, name: str, help: str) -> None:
        self.name = name
        self.help = help

    def samples(self) -> Iterable[tuple[str, Labels, float]]:
        raise NotImplementedError

    def render(self) -> list[str]:
        out = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} {self.kind}"]
        out += [f"{name}{_fmt_labels(labels)} {_fmt_value(v)}" for name, labels, v in self.samples()]
        return out


class Counter(Metric):
    kind = "counter"

    def __init__(self, name: str, help: str) -> None:
        super().__init__(name, help)
        self.values: dict[Labels, float] = {}

    def inc(self, amount: float = 1.0, **labels: str) -> None:
        key = _labels(labels)
        self.values[key] = self.values.get(key, 0.0) + amount

    def get(self, **labels: str) -> float:
        return self.values.get(_labels(labels), 0.0)

    def samples(self) -> Iterable[tuple[str, Labels, float]]:
        for labels, v in sorted(self.values.items()):
            yield self.name, labels, v


class Histogram(Metric):
    kind = "histogram"

    def __init__(self, name: str, help: str, buckets: tuple[float, ...] = LATENCY_BUCKETS) -> None:
        super().__init__(name, help)
        self.buckets = buckets
        self.series: dict[Labels, tuple[list[int], list[float]]] = {}  # -> (bucket counts, [sum, count])

    def observe(self, value: float, **labels: str) -> None:
        counts, totals = self.series.setdefault(_labels(labels), ([0] * len(self.buckets), [0.0, 0.0]))
        for i, upper in enumerate(self.buckets):
            if value <= upper:
                counts[i] += 1
        totals[0] += value
        totals[1] += 1

    def count(self, **labels: str) -> int:
        s = self.series.get(_labels(labels))
        return int(s[1][1]) if s else 0

    def samples(self) -> Iterable[tuple[str, Labels, float]]:
        for labels, (counts, (total, n)) in sorted(self.series.items()):
            for upper, c in zip(self.buckets, counts, strict=True):
                yield f"{self.name}_bucket", (*labels, ("le", _fmt_value(upper))), c
            yield f"{self.name}_bucket", (*labels, ("le", "+Inf")), n
            yield f"{self.name}_sum", labels, total
            yield f"{self.name}_count", labels, n


class Collected(Metric):
    """A gauge or counter whose values are read when scraped (from the DB, the
    guard's counters, the presence sockets), so nothing has to keep it in sync."""

    def __init__(self, name: str, help: str, kind: str, read: Callable[[], Iterable[tuple[dict[str, str], float]]]):
        super().__init__(name, help)
        self.kind = kind
        self.read = read

    def samples(self) -> Iterable[tuple[str, Labels, float]]:
        for labels, v in self.read():
            yield self.name, _labels(labels), v


class Registry:
    def __init__(self) -> None:
        self.metrics: dict[str, Metric] = {}

    def add[M: Metric](self, metric: M) -> M:
        if metric.name in self.metrics:
            raise ValueError(f"metric {metric.name} registered twice")
        self.metrics[metric.name] = metric
        return metric

    def counter(self, name: str, help: str) -> Counter:
        return self.add(Counter(name, help))

    def histogram(self, name: str, help: str, buckets: tuple[float, ...] = LATENCY_BUCKETS) -> Histogram:
        return self.add(Histogram(name, help, buckets))

    def collected(
        self, name: str, help: str, read: Callable[[], Iterable[tuple[dict[str, str], float]]], kind: str = "gauge"
    ) -> Collected:
        return self.add(Collected(name, help, kind, read))

    def render(self) -> str:
        lines: list[str] = []
        for m in self.metrics.values():
            try:
                lines += m.render()
            except Exception as e:  # a broken collector must not take /metrics down with it
                lines.append(f"# {m.name} unavailable: {type(e).__name__}")
        return "\n".join(lines) + "\n"


REGISTRY = Registry()

# Updated where things happen; the scrape-time ones are registered in main.py.
http_requests = REGISTRY.counter("hallway_http_requests_total", "HTTP requests, by method, route template and status.")
http_latency = REGISTRY.histogram(
    "hallway_http_request_duration_seconds", "HTTP request latency in seconds, by method and route template."
)
signins = REGISTRY.counter(
    "hallway_signins_total",
    "Sign-in links opened: ok (an attendee), onboarding (a new email), expired, already_joined, "
    "signed_in_elsewhere (left unused: this device is signed in to another account).",
)
signin_links = REGISTRY.counter("hallway_signin_links_total", "Sign-in links issued, by sign-in mode.")
registrations = REGISTRY.counter("hallway_registrations_total", "New attendees, by mode (connect, live, daypass).")
mcp_calls = REGISTRY.counter("hallway_mcp_tool_calls_total", "MCP tool calls, by tool and outcome.")
mcp_latency = REGISTRY.histogram("hallway_mcp_tool_call_duration_seconds", "MCP tool call latency in seconds, by tool.")
rate_limited = REGISTRY.counter("hallway_rate_limited_total", "Requests refused by a rate limit, by limit name.")
guard_latency = REGISTRY.histogram(
    "hallway_guard_check_duration_seconds",
    "Privacy guard check latency in seconds. Switch delivers unchecked after 2 s.",
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 1.5, 2.0, 5.0),
)
guard_verdicts = REGISTRY.counter("hallway_guard_verdicts_total", "Privacy guard verdicts: ok, redacted, blocked.")
presence_drops = REGISTRY.counter(
    "hallway_presence_disconnects_total", "Stand-in WebSockets to Switch that closed and are being reconnected."
)
