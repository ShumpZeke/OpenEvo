"""
Seed Forge: a starting population instead of one program.

Upstream begins every run from a single seed, so the first generations are
spent discovering the shape of the space, and every island starts in the same
basin — which leaves the island structure with nothing to keep apart.

The forge produces variants without a model request. They are not better
programs and are not meant to be; they are cheap, valid, structurally distinct
starting points. The tests below are mostly about the "valid" part, because a
clever transformation that produces subtly broken programs is worse than no
forge at all: it would spend the first generation of every island on candidates
that cannot run.
"""

import ast

import pytest

from oe_max.evaluation.gates import DedupIndex
from oe_max.search.seed_forge import (
    DEFAULT_SCALES, EFFORT_KEYWORDS, ForgeReport, Variant, forge,
)

SEED = '''
def search(iterations=1000, bounds=(-5, 5)):
    best = None
    scale = 0.35
    for i in range(iterations):
        x = i * 2.5
        if best is None or x < best:
            best = x
    return best
'''


def _accepted_codes(report):
    return [v.code for v in report.accepted]


# -- the variants are valid -------------------------------------------------

def test_every_accepted_variant_parses():
    """The one property that must hold: a broken variant costs a generation."""
    for code in _accepted_codes(forge(SEED)):
        ast.parse(code)


def test_every_accepted_variant_keeps_the_required_function():
    report = forge(SEED, required_functions=["search"])
    assert report.accepted
    for code in _accepted_codes(report):
        assert "def search(" in code


def test_the_seed_itself_is_always_kept():
    """
    A forge that replaced the program the operator supplied would be making a
    decision nobody asked for.
    """
    report = forge(SEED)
    origins = [v.origin for v in report.accepted]
    assert origins[0] == "seed"


def test_variants_are_distinct_from_each_other():
    codes = _accepted_codes(forge(SEED))
    assert len(codes) == len(set(codes))


def test_a_seed_that_does_not_parse_is_reported_not_raised():
    report = forge("def broken(:\n    pass\n")
    assert not report.accepted
    assert "does not parse" in report.variants[0].rejected


# -- the transformations are dull on purpose -------------------------------

def test_structural_literals_are_left_alone():
    """
    0 and 1 are indices, increments and identity elements. Scaling them changes
    what a program *does* rather than how hard it tries.
    """
    seed = "def f():\n    total = 0\n    for i in range(10):\n        total += 1\n    return total\n"
    for code in _accepted_codes(forge(seed)):
        tree = ast.parse(code)
        constants = [n.value for n in ast.walk(tree)
                     if isinstance(n, ast.Constant) and isinstance(n.value, int)]
        assert 0 in constants and 1 in constants


def test_control_flow_is_never_touched():
    """
    Reordering statements or swapping operators produces plausible-looking
    programs that are wrong — exactly what an evaluator cannot tell you cheaply.
    """
    original = ast.dump(ast.parse(SEED))
    for code in _accepted_codes(forge(SEED))[1:]:
        variant = ast.parse(code)
        assert [type(n).__name__ for n in ast.walk(variant) if isinstance(
            n, (ast.For, ast.If, ast.Return, ast.Compare))] == \
            [type(n).__name__ for n in ast.walk(ast.parse(SEED)) if isinstance(
                n, (ast.For, ast.If, ast.Return, ast.Compare))]
    assert original  # the seed was parseable to begin with


def test_effort_keywords_are_varied_by_name():
    report = forge(SEED)
    effort = [v for v in report.accepted if v.origin == "scale_effort"]
    assert effort, "no effort dial was found in a seed that has one"
    assert any("iterations=2000" in v.code or "iterations=500" in v.code
               or "iterations=10000" in v.code for v in effort)


def test_a_default_is_matched_to_the_right_argument():
    """
    Defaults align with the *last* N positional arguments. Getting that wrong
    scales whichever argument happens to be first, silently.
    """
    seed = "def f(data, iterations=100, tolerance=0.5):\n    return data\n"
    report = forge(seed, scales=(2.0,))
    # Selected by origin, not by matching text: the literal scaler also
    # produces `iterations=200`, and matching on that would test the wrong arm.
    effort = [v for v in report.accepted if v.origin == "scale_effort"]
    assert effort, [v.to_dict() for v in report.accepted]
    assert "iterations=200" in effort[0].code
    # tolerance is not an effort keyword, so this arm must leave it alone.
    assert "tolerance=0.5" in effort[0].code


def test_a_scale_that_would_collapse_an_int_is_skipped():
    """Scaling 2 by 0.5 gives 1 — a structural value, and a different program."""
    seed = "def f():\n    return 2\n"
    for code in _accepted_codes(forge(seed, scales=(0.5,))):
        assert "return 1" not in code


# -- the gates apply --------------------------------------------------------

def test_a_variant_identical_to_the_seed_is_deduplicated():
    """
    Scaling a program with no numbers left to scale reproduces it exactly, and
    that has to be caught rather than added twice.
    """
    seed = "def f():\n    return 1\n"        # nothing scalable
    report = forge(seed)
    assert len(report.accepted) == 1


def test_a_shared_dedup_index_is_respected():
    """
    Forging two seeds in one run must not add the same program twice, or the
    initial population is not as diverse as it is reported to be.
    """
    index = DedupIndex()
    first = forge(SEED, index=index)
    second = forge(SEED, index=index)
    assert first.accepted
    assert not second.accepted, "the second forge duplicated the first"


def test_max_variants_bounds_the_population():
    report = forge(SEED, max_variants=3)
    assert len(report.accepted) <= 3


# -- reporting --------------------------------------------------------------

def test_rejections_are_counted_rather_than_swallowed():
    """
    A forge whose variants are nearly all duplicates is doing nothing, and the
    only way to notice is for it to say so.
    """
    seed = "def f():\n    return 1\n"
    payload = forge(seed).to_dict()
    assert payload["produced"] >= payload["accepted"]
    assert isinstance(payload["rejected_by"], dict)


def test_a_forge_that_added_nothing_says_so():
    report = ForgeReport("abc123", [Variant("x = 1", "seed", rejected="G0: bad")])
    assert "added nothing" in report.summary()


def test_the_default_scales_span_both_directions():
    """One-directional scaling only ever makes a program try harder."""
    assert min(DEFAULT_SCALES) < 1.0 < max(DEFAULT_SCALES)


def test_effort_keywords_cover_the_usual_names():
    assert "iterations" in EFFORT_KEYWORDS and "samples" in EFFORT_KEYWORDS
