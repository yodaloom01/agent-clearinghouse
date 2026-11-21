"""
Verification engine — turns a service's promise into an automatic yes/no.

A service lists itself with a `spec`: a list of checks the delivered result must
pass. On delivery the engine runs every check and returns (passed, report).
Because the buyer agreed to this spec when they hired the service, a pass is an
objective settlement — the buyer cannot later claim good work was bad.

Supported checks (each is a dict with a "type"):
  {"type": "not_empty"}
  {"type": "min_length", "value": 10}            # characters
  {"type": "max_length", "value": 500}
  {"type": "min_sentences", "value": 3}
  {"type": "max_sentences", "value": 5}
  {"type": "contains", "value": "foo"}
  {"type": "contains_all", "values": ["a", "b"]}
  {"type": "equals", "value": "YES"}
  {"type": "one_of", "values": ["YES", "NO"]}
  {"type": "regex", "pattern": "^\\+?\\d[\\d -]{7,}$"}
  {"type": "json_valid"}
  {"type": "json_has_keys", "values": ["name", "price"]}

Hardening notes:
  - result and pattern lengths are capped before any regex to blunt catastrophic
    backtracking (ReDoS). A fully adversarial regex is still theoretically possible;
    the residual fix is a sandboxed regex engine with a hard wall-clock timeout.
"""

import json
import re

_MAX_RESULT_SCAN = 20_000
_MAX_PATTERN_LEN = 300
_SENTENCE_SPLIT = re.compile(r"[.!?]+")


def _sentences(text: str) -> int:
    return len([s for s in _SENTENCE_SPLIT.split(text) if s.strip()])


def _check(result: str, c: dict):
    """Return (ok, detail) for a single check."""
    t = c.get("type")

    if t == "not_empty":
        return (bool(result and result.strip()), "must not be empty")

    if t == "min_length":
        return (len(result) >= c["value"], f"length >= {c['value']} (got {len(result)})")

    if t == "max_length":
        return (len(result) <= c["value"], f"length <= {c['value']} (got {len(result)})")

    if t == "min_sentences":
        n = _sentences(result)
        return (n >= c["value"], f"sentences >= {c['value']} (got {n})")

    if t == "max_sentences":
        n = _sentences(result)
        return (n <= c["value"], f"sentences <= {c['value']} (got {n})")

    if t == "contains":
        return (c["value"] in result, f"must contain {c['value']!r}")

    if t == "contains_all":
        missing = [v for v in c["values"] if v not in result]
        return (not missing, f"must contain all; missing {missing}")

    if t == "equals":
        return (result.strip() == c["value"], f"must equal {c['value']!r}")

    if t == "one_of":
        return (result.strip() in c["values"], f"must be one of {c['values']}")

    if t == "regex":
        pattern = c.get("pattern", "")
        if len(pattern) > _MAX_PATTERN_LEN:
            return (False, "pattern rejected: too long")
        try:
            rx = re.compile(pattern)
        except re.error as e:
            return (False, f"invalid regex: {e}")
        return (bool(rx.search(result[:_MAX_RESULT_SCAN])), f"must match /{pattern}/")

    if t == "json_valid":
        try:
            json.loads(result)
            return (True, "valid JSON")
        except Exception as e:
            return (False, f"must be valid JSON: {e}")

    if t == "json_has_keys":
        try:
            obj = json.loads(result)
        except Exception as e:
            return (False, f"must be valid JSON: {e}")
        if not isinstance(obj, dict):
            return (False, "JSON must be an object")
        missing = [k for k in c["values"] if k not in obj]
        return (not missing, f"must have keys; missing {missing}")

    return (False, f"unknown check type: {t}")


def validate_spec(spec) -> str | None:
    """Return an error string if the spec is malformed, else None."""
    if spec is None:
        return None
    if not isinstance(spec, list):
        return "spec must be a list of checks"
    known = {
        "not_empty", "min_length", "max_length", "min_sentences", "max_sentences",
        "contains", "contains_all", "equals", "one_of", "regex",
        "json_valid", "json_has_keys",
    }
    for c in spec:
        if not isinstance(c, dict) or "type" not in c:
            return "each check must be an object with a 'type'"
        if c["type"] not in known:
            return f"unknown check type: {c['type']}"
        if c["type"] == "regex" and len(c.get("pattern", "")) > _MAX_PATTERN_LEN:
            return "regex pattern too long"
    return None


def run_checks(result: str, spec):
    """
    Run all checks. Returns (passed: bool, report: list[dict]).
    An empty/None spec means there is nothing to auto-check -> passed False with a
    note, so the caller routes it to the manual path instead.
    """
    if not spec:
        return (False, [{"ok": False, "detail": "no spec; manual review required"}])
    report = []
    all_ok = True
    for c in spec:
        ok, detail = _check(result or "", c)
        report.append({"type": c.get("type"), "ok": ok, "detail": detail})
        all_ok = all_ok and ok
    return (all_ok, report)
