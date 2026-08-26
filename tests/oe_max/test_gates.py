"""
Cheap-gate tests.

Emphasis on the two ways these gates fail expensively: rejecting a valid
candidate (throws away real work) and admitting a duplicate (spends a ~130s
model call and a benchmark on a program that cannot improve the archive).
"""
import pytest
from oe_max.evaluation.gates import (
    DedupIndex, GateResult, ast_hash, exact_hash, g0_validity, g1_dedup,
    normalized_hash, structural_hash,
)

GOOD = '''
import numpy as np

def search_algorithm(iterations=1000, bounds=(-5, 5)):
    """Docstring."""
    best_x = np.random.uniform(*bounds)
    return best_x, 0.0, 0.0
'''


# ---------------------------------------------------------------- G0
def test_valid_program_passes():
    r = g0_validity(GOOD, required_functions=["search_algorithm"],
                    required_imports=["numpy"])
    assert r.passed and "search_algorithm" in r.detail["functions"]


def test_syntax_error_rejected_with_line_number():
    r = g0_validity("def broken(:\n    pass")
    assert not r.passed and "syntax error" in r.reason
    assert r.detail.get("lineno")


def test_empty_program_rejected():
    assert not g0_validity("").passed
    assert not g0_validity("   \n\n  ").passed


def test_missing_required_function_rejected():
    r = g0_validity(GOOD, required_functions=["does_not_exist"])
    assert not r.passed and "does_not_exist" in r.reason


def test_missing_required_import_rejected():
    r = g0_validity(GOOD, required_imports=["scipy"])
    assert not r.passed and "scipy" in r.reason


def test_forbidden_import_rejected():
    r = g0_validity("import socket\ndef f(): pass", forbidden_imports=["socket"])
    assert not r.passed and "socket" in r.reason


def test_length_limit_enforced():
    r = g0_validity("x = 1\n" * 1000, max_length=100)
    assert not r.passed and "too long" in r.reason


def test_async_functions_count_as_defined():
    r = g0_validity("async def solve():\n    return 1", required_functions=["solve"])
    assert r.passed


def test_relative_import_does_not_crash_the_gate():
    assert g0_validity("from . import sibling\ndef f(): pass").passed


# ---------------------------------------------------------------- hashes
def test_exact_hash_is_byte_sensitive():
    assert exact_hash("a = 1") == exact_hash("a = 1")
    assert exact_hash("a = 1") != exact_hash("a = 2")
    assert exact_hash("a = 1") != exact_hash("a = 1 ")


def test_normalized_hash_ignores_whitespace_and_blank_lines():
    assert normalized_hash("a = 1\n\n\nb = 2") == normalized_hash("a = 1\nb = 2")
    assert normalized_hash("a = 1   \n") == normalized_hash("a = 1\n")
    assert normalized_hash("a = 1\r\nb = 2") == normalized_hash("a = 1\nb = 2")


def test_ast_hash_ignores_comments_and_docstrings():
    a = 'def f():\n    """doc."""\n    # comment\n    return 1'
    b = "def f():\n    return 1"
    assert ast_hash(a) == ast_hash(b)


def test_ast_hash_detects_real_logic_change():
    assert ast_hash("def f(): return 1") != ast_hash("def f(): return 2")


def test_ast_hash_returns_none_for_unparseable_code():
    # None must be distinguishable from "a different structure", or a parse
    # failure would masquerade as novelty.
    assert ast_hash("def (:") is None


def test_structural_hash_ignores_local_renaming():
    a = "def f(n):\n    total = 0\n    for i in range(n):\n        total += i\n    return total"
    b = "def f(count):\n    acc = 0\n    for j in range(count):\n        acc += j\n    return acc"
    assert structural_hash(a) == structural_hash(b)


def test_structural_hash_still_separates_different_logic():
    a = "def f(n):\n    t = 0\n    for i in range(n):\n        t += i\n    return t"
    b = "def f(n):\n    t = 1\n    for i in range(n):\n        t *= i\n    return t"
    assert structural_hash(a) != structural_hash(b)


def test_structural_hash_preserves_function_identity():
    """Renaming the entry point is a real difference, not a cosmetic one."""
    assert structural_hash("def solve(): return 1") != structural_hash("def other(): return 1")


# ---------------------------------------------------------------- G1
def test_first_sighting_is_novel():
    idx = DedupIndex()
    assert g1_dedup(GOOD, idx).passed


def test_exact_duplicate_rejected():
    idx = DedupIndex()
    idx.add(GOOD, "c1")
    r = g1_dedup(GOOD, idx)
    assert not r.passed and r.detail["kind"] == "exact" and r.detail["of"] == "c1"


def test_whitespace_variant_rejected_as_normalized_duplicate():
    idx = DedupIndex()
    idx.add("a = 1\nb = 2", "c1")
    r = g1_dedup("a = 1\n\n\nb = 2  ", idx)
    assert not r.passed and r.detail["kind"] == "normalized"


def test_comment_variant_rejected_as_ast_duplicate():
    idx = DedupIndex()
    idx.add("def f():\n    return 1", "c1")
    r = g1_dedup('def f():\n    """new doc."""\n    # explanatory\n    return 1', idx)
    assert not r.passed and r.detail["kind"] == "ast"


def test_renamed_variant_only_rejected_when_structural_enabled():
    a = "def f(n):\n    t = 0\n    for i in range(n):\n        t += i\n    return t"
    b = "def f(m):\n    s = 0\n    for k in range(m):\n        s += k\n    return s"
    idx = DedupIndex()
    idx.add(a, "c1")
    assert g1_dedup(b, idx, use_structural=False).passed          # opt-in
    assert not g1_dedup(b, idx, use_structural=True).passed


def test_genuinely_novel_program_passes():
    idx = DedupIndex()
    idx.add(GOOD, "c1")
    novel = GOOD.replace("np.random.uniform(*bounds)", "np.random.normal(0, 1)")
    assert g1_dedup(novel, idx, use_structural=True).passed


def test_unparseable_code_is_not_treated_as_a_duplicate():
    idx = DedupIndex()
    idx.add("def f(): return 1", "c1")
    assert g1_dedup("def (:", idx).passed   # G0's job to reject, not G1's


def test_index_stats_track_duplicate_pressure():
    idx = DedupIndex()
    idx.add(GOOD, "c1")
    for _ in range(3):
        g1_dedup(GOOD, idx)
    s = idx.stats()
    assert s["duplicate_hits"]["exact"] == 3
    assert s["total_duplicates"] == 3
    assert s["unique_exact"] == 1
