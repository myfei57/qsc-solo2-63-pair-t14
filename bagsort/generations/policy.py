"""The two policies for a request that presents an expired generation.

The service is **safety first**: the default policy rejects a stale reference
and hands back everything the caller needs to re-fetch the current allocation.
``adopt_current`` is an explicit opt-in that keeps the request alive by moving
it onto the current generation, which is only available for callers that
tolerate having their reference silently rewritten.

The availability ceiling of ``adopt_current`` is deliberately stated here
rather than discovered under load: it can salvage a stale reference only when
the flight is still live *and* still has an allocation (or schedule, by kind).
It cannot bring back a retired flight, cannot invent an allocation that never
existed, and it always changes what the request referred to -- so even a
salvaged request is answered with a notice naming the generation it was moved
onto. Requests above that ceiling stay a rejection under either policy.
"""

from __future__ import annotations

from typing import Any

REJECT = "reject"
ADOPT_CURRENT = "adopt_current"

POLICIES = (REJECT, ADOPT_CURRENT)

#: What adoption can and cannot do, surfaced verbatim on every stale answer so
#: an operator never mistakes "your request kept running" for "nothing changed".
AVAILABILITY_CEILING = {
    "policy": ADOPT_CURRENT,
    "salvages": (
        "a request whose flight is still live and whose reference kind still "
        "has a current generation; the request continues against that generation"
    ),
    "cannot_salvage": [
        "a retired or removed flight (there is no current generation to move to)",
        "a kind with no current allocation or schedule (adoption cannot create one)",
        "a request whose meaning is bound to the old generation's contents",
    ],
    "semantics": (
        "the request no longer refers to what the caller captured; it is "
        "answered with an adopted_notice naming both generations"
    ),
}


def describe(policy: str) -> dict[str, Any]:
    """Plain description of a policy, for console hints and catalog output."""

    if policy == REJECT:
        return {
            "policy": REJECT,
            "safety": "request is refused; no state changes under an old reference",
            "availability": "caller must re-fetch the current generation and retry",
        }
    if policy == ADOPT_CURRENT:
        return dict(AVAILABILITY_CEILING)
    raise ValueError(f"unknown stale policy {policy!r}")


def is_adoptive(policy: str) -> bool:
    return policy == ADOPT_CURRENT
