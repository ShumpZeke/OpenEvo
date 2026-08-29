"""Seven documents state a test count. They must state the same one.

The counts drifted three times in a single day: a commit adds tests, one file
gets updated, six do not, and the repository quietly carries a stale claim --
which is the defect its own conventions call out ("a claim carries a number or
the word unverified", and a number that is wrong in the optimistic direction is
the same defect as one wrong in the pessimistic direction).

This does **not** check the counts against a real run. `pytest --collect-only`
is cheap, but the documented figures are *passed* counts and skips differ by
platform -- six upstream tests fail only on Windows, POSIX-limit tests skip only
off it -- so an exact automated comparison would be red on one machine and green
on another for no useful reason.

What it checks is the failure that actually happens: the files disagreeing with
each other, and a stated total that is not the sum of its parts.

Refreshing them is still a manual act after a real run. This makes forgetting
one of them loud.
"""

import re
from collections import defaultdict
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

DOCS = [
    "README.md",
    "CLAUDE.md",
    "docs/testing.md",
    "docs/project/status.md",
    "docs/project/handoff.md",
    "docs/project/coverage.md",
]

# label -> patterns that capture that suite's count in any of the shapes used.
PATTERNS = {
    "control plane": [
        r"\|\s*Control plane\s*\|[^|]*?\*\*(\d+) passed\*\*",
        r"\|\s*Control plane\s*\|[^|]*?\|[^|]*?\*\*(\d+) passed\*\*",
        r"(\d+) control plane",
    ],
    "oe-max": [
        r"\|\s*OE-MAX[^|]*\|[^|]*?\*\*(\d+) passed\*\*",
        r"\|\s*OE-MAX[^|]*\|[^|]*?\|[^|]*?\*\*(\d+) passed\*\*",
        r"(\d+) OE-MAX",
    ],
    "brainport": [
        r"\|\s*BrainPort[^|]*\|\s*\*\*(\d+) passed\*\*",
        r"\|\s*BrainPort[^|]*\|[^|]*?\*\*(\d+) passed\*\*",
        r"(\d+) BrainPort",
    ],
    # Only the composite "A upstream + B control plane + ..." form. A bare
    # "(\d+) upstream" also matches "6 upstream tests fail on Windows" and
    # coverage.md's 437, which is the *Linux* count -- 437 pass there, 431 on
    # Windows where six fail for platform reasons. Both are correct and
    # status.md says so, so they must not be read as disagreement.
    "upstream": [
        r"(\d+) upstream \+",
    ],
}


def _documented():
    """label -> {value: [files that say it]}"""
    seen = defaultdict(lambda: defaultdict(list))
    for name in DOCS:
        path = ROOT / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for label, patterns in PATTERNS.items():
            for pattern in patterns:
                for match in re.findall(pattern, text):
                    seen[label][int(match)].append(name)
    return seen


@pytest.mark.parametrize("label", sorted(PATTERNS))
def test_every_document_states_the_same_count(label):
    values = _documented().get(label)
    if not values:
        pytest.skip(f"no document states a {label} count")
    assert len(values) == 1, (
        "documents disagree about the {} test count: ".format(label)
        + "; ".join(
            "{} says {}".format(", ".join(sorted(set(files))), value)
            for value, files in sorted(values.items())
        )
    )


def test_the_stated_total_is_the_sum_of_its_parts():
    """`1,424 passing` and `= 1424 passing` must equal upstream + the three forks."""
    documented = _documented()
    parts = {}
    for label in ("upstream", "control plane", "oe-max", "brainport"):
        values = documented.get(label)
        if not values or len(values) != 1:
            pytest.skip("per-suite counts are not yet consistent; see the test above")
        parts[label] = next(iter(values))

    expected = sum(parts.values())

    totals = set()
    for name in DOCS + ["docs/project/build-log.md"]:
        path = ROOT / name
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in (r"\*\*([\d,]+) passing\.\*\*",
                        r"= (\d+) passing",
                        r"\+ ([\d,]+) passing tests",
                        r"Tests \d+ → (\d+)\."):
            for match in re.findall(pattern, text):
                totals.add(int(match.replace(",", "")))

    assert totals, "no document states a total"
    assert totals == {expected}, (
        "stated total(s) {} do not match {} + {} + {} + {} = {}".format(
            sorted(totals), parts["upstream"], parts["control plane"],
            parts["oe-max"], parts["brainport"], expected)
    )


def test_the_counts_are_actually_being_found():
    """A parametrised test over an empty match set passes silently.

    If a document is reformatted so the patterns stop matching, the checks above
    would go green while checking nothing.
    """
    documented = _documented()
    for label in PATTERNS:
        assert label in documented, f"no document matched a {label} count any more"
        files = {f for files in documented[label].values() for f in files}
        assert len(files) >= 2, (
            f"only {files} states a {label} count; the point of this test is "
            "cross-checking several"
        )
