"""
Several alternatives per model request.

The economics: a request costs 84 s on the fastest measured route and 292 s on
the primary, while applying a diff and running the evaluator costs
milliseconds. Two distinct candidates from one response is close to halving the
cost per candidate.

The failure this must not have is not "it doesn't work" — it is three
near-identical alternatives that all collapse to one AST hash, which is
throughput that is not real. The tests below pin the dedup, and the harder
invariant: with the feature on, the *primary* child must be byte-for-byte what
it would have been with the feature off. Otherwise the experiment has two
variables and measures neither.
"""

import types

import pytest

from control_plane.telemetry import multi_offspring as mo


SAMPLE = """Here are three ways.

### ALTERNATIVE 1
<<<<<<< SEARCH
x = 1
=======
x = 2
>>>>>>> REPLACE

### ALTERNATIVE 2
<<<<<<< SEARCH
x = 1
=======
x = 3
>>>>>>> REPLACE

### ALTERNATIVE 3
<<<<<<< SEARCH
x = 1
=======
x = 4
>>>>>>> REPLACE
"""


# -- configuration ----------------------------------------------------------

def test_it_is_off_by_default(monkeypatch):
    monkeypatch.delenv(mo.ENV_MULTI_OFFSPRING, raising=False)
    assert mo.requested_offspring() == 1
    assert mo.enabled() is False


@pytest.mark.parametrize("value,expected", [
    ("2", 2), ("3", 3), ("1", 1), ("0", 1), ("-4", 1),
    ("99", mo.MAX_OFFSPRING), ("", 1), ("lots", 1),
])
def test_the_count_is_clamped_to_something_sane(monkeypatch, value, expected):
    """
    A large N makes the response long enough that reasoning models truncate —
    the failure measured at 7,986 of an 8,000-token budget spent on hidden
    reasoning. Garbage input falls back to "off", never to a crash.
    """
    monkeypatch.setenv(mo.ENV_MULTI_OFFSPRING, value)
    assert mo.requested_offspring() == expected


# -- splitting --------------------------------------------------------------

def test_alternatives_are_separated():
    parts = mo.split_alternatives(SAMPLE)
    assert len(parts) == 3
    assert "x = 2" in parts[0] and "x = 3" in parts[1] and "x = 4" in parts[2]


def test_the_preamble_is_not_mistaken_for_the_first_alternative():
    """
    The worst possible failure lives here. If "Here are three ways." became
    alternative 1, it would be what upstream applies — and applying a diff-free
    string returns the parent unchanged, so the run would look healthy and
    evolve nothing.
    """
    assert "Here are three ways" not in mo.split_alternatives(SAMPLE)[0]


def test_a_first_alternative_written_before_the_marker_is_kept():
    """Some models label only the later ones. Dropping that is losing work."""
    text = ("<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE\n"
            "### ALTERNATIVE 2\n"
            "<<<<<<< SEARCH\nx = 1\n=======\nx = 3\n>>>>>>> REPLACE")
    parts = mo.split_alternatives(text)
    assert len(parts) == 2
    assert "x = 2" in parts[0] and "x = 3" in parts[1]


def test_a_response_without_markers_is_one_alternative():
    """A model that ignored the instruction must degrade to today's behaviour."""
    text = "<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE"
    assert mo.split_alternatives(text) == [text]


def test_marker_matching_tolerates_the_model_being_untidy():
    """Case, hash count and a trailing description all vary in practice."""
    text = ("##  alternative 1\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n"
            "#### ALTERNATIVE 2 - a different approach\n"
            "<<<<<<< SEARCH\na\n=======\nc\n>>>>>>> REPLACE")
    parts = mo.split_alternatives(text)
    assert len(parts) == 2
    assert "b" in parts[0] and "c" in parts[1]


def test_an_empty_response_yields_nothing():
    assert mo.split_alternatives("") == []
    assert mo.split_alternatives(None) == []


def test_the_marker_is_not_a_diff_token():
    """
    A marker upstream's parser recognised would be consumed before we saw it,
    and the split would silently return one alternative.
    """
    assert "<<<<<<<" not in mo.ALTERNATIVE_MARKER
    assert ">>>>>>>" not in mo.ALTERNATIVE_MARKER
    assert "=======" not in mo.ALTERNATIVE_MARKER


# -- prompting --------------------------------------------------------------

def test_the_prompt_asks_for_the_marker_it_will_split_on(monkeypatch):
    monkeypatch.setenv(mo.ENV_MULTI_OFFSPRING, "3")
    out = mo.install_prompt_hook({"system": "base", "user": "program"})
    assert mo.ALTERNATIVE_MARKER in out["system"]
    assert "3" in out["system"]
    assert out["user"] == "program", "the diff format contract must not be touched"


def test_the_prompt_warns_that_near_duplicates_are_discarded(monkeypatch):
    """Asking for three and getting three renamings is the failure to avoid."""
    monkeypatch.setenv(mo.ENV_MULTI_OFFSPRING, "3")
    system = mo.install_prompt_hook({"system": "base"})["system"]
    assert "duplicates" in system.lower()


def test_a_prompt_is_untouched_when_the_feature_is_off(monkeypatch):
    monkeypatch.delenv(mo.ENV_MULTI_OFFSPRING, raising=False)
    prompt = {"system": "base", "user": "program"}
    assert mo.install_prompt_hook(prompt) == prompt


# -- the primary child is unchanged ----------------------------------------

@pytest.fixture
def parse_hooks(monkeypatch):
    monkeypatch.setenv(mo.ENV_MULTI_OFFSPRING, "3")
    from openevolve.utils import code_utils

    original_extract = code_utils.extract_diffs
    original_apply = code_utils.apply_diff
    mo.install_parse_hooks()
    yield code_utils
    code_utils.extract_diffs = original_extract
    code_utils.apply_diff = original_apply
    mo.take_alternatives()


def test_the_primary_child_is_what_it_would_have_been_at_n_equals_one(parse_hooks):
    """
    The invariant that keeps the experiment honest. Upstream applies *every*
    diff block it finds, so without this the primary child would be an
    incoherent merge of all three alternatives.
    """
    code_utils = parse_hooks
    assert code_utils.apply_diff("x = 1", SAMPLE) == "x = 2"


def test_the_remaining_alternatives_are_kept_for_the_worker(parse_hooks):
    code_utils = parse_hooks
    code_utils.extract_diffs(SAMPLE)
    alternatives = mo.take_alternatives()
    assert len(alternatives) == 2
    assert "x = 3" in alternatives[0] and "x = 4" in alternatives[1]


def test_taking_the_alternatives_clears_them(parse_hooks):
    """A failed iteration must not inherit the previous one's alternatives."""
    code_utils = parse_hooks
    code_utils.extract_diffs(SAMPLE)
    assert mo.take_alternatives()
    assert mo.take_alternatives() == []


def test_a_single_alternative_response_behaves_exactly_as_before(parse_hooks):
    code_utils = parse_hooks
    text = "<<<<<<< SEARCH\nx = 1\n=======\nx = 9\n>>>>>>> REPLACE"
    assert code_utils.apply_diff("x = 1", text) == "x = 9"
    code_utils.extract_diffs(text)
    assert mo.take_alternatives() == []


def test_the_parse_hooks_are_not_installed_when_the_feature_is_off(monkeypatch):
    monkeypatch.delenv(mo.ENV_MULTI_OFFSPRING, raising=False)
    from openevolve.utils import code_utils

    before = code_utils.apply_diff
    mo.install_parse_hooks()
    assert code_utils.apply_diff is before


# -- building siblings ------------------------------------------------------

@pytest.fixture
def worker(monkeypatch):
    """A stand-in for the worker's evaluator and config."""
    from openevolve import process_parallel

    class Evaluator:
        def __init__(self):
            self.seen = []

        async def evaluate_program(self, code, cid):
            self.seen.append(code)
            return {"combined_score": 0.5 + 0.1 * len(self.seen)}

    evaluator = Evaluator()
    monkeypatch.setattr(process_parallel, "_worker_evaluator", evaluator,
                        raising=False)
    monkeypatch.setattr(process_parallel, "_worker_config",
                        types.SimpleNamespace(diff_pattern=None,
                                              max_code_length=20000),
                        raising=False)
    return evaluator


def _stash(alternatives):
    mo._worker_alternatives = list(alternatives)


def test_siblings_are_built_and_scored(worker, monkeypatch):
    monkeypatch.setenv(mo.ENV_MULTI_OFFSPRING, "3")
    _stash(["<<<<<<< SEARCH\nx = 1\n=======\nx = 3\n>>>>>>> REPLACE",
            "<<<<<<< SEARCH\nx = 1\n=======\nx = 4\n>>>>>>> REPLACE"])

    siblings = mo.build_siblings("x = 1", "parent", {"island": 2}, 7,
                                 primary_code="x = 2")
    assert [s["code"] for s in siblings] == ["x = 3", "x = 4"]
    assert all(s["parent_id"] == "parent" for s in siblings)
    assert all(s["metadata"]["island"] == 2 for s in siblings)
    assert all(s["metadata"]["multi_offspring"] for s in siblings)
    assert all(s["metrics"]["combined_score"] > 0 for s in siblings)
    assert len({s["id"] for s in siblings}) == 2


def test_a_sibling_identical_to_the_primary_is_dropped(worker, monkeypatch):
    """Not throughput. The cheapest place to notice is before it is pickled."""
    monkeypatch.setenv(mo.ENV_MULTI_OFFSPRING, "3")
    _stash(["<<<<<<< SEARCH\nx = 1\n=======\nx = 2\n>>>>>>> REPLACE"])

    assert mo.build_siblings("x = 1", "parent", {}, 1, primary_code="x = 2") == []


def test_two_identical_alternatives_yield_one_sibling(worker, monkeypatch):
    """Three renamings of the same change are one candidate, not three."""
    monkeypatch.setenv(mo.ENV_MULTI_OFFSPRING, "3")
    same = "<<<<<<< SEARCH\nx = 1\n=======\nx = 7\n>>>>>>> REPLACE"
    _stash([same, same])

    assert len(mo.build_siblings("x = 1", "p", {}, 1, primary_code="x = 2")) == 1


def test_an_alternative_that_does_not_apply_is_skipped(worker, monkeypatch):
    monkeypatch.setenv(mo.ENV_MULTI_OFFSPRING, "3")
    _stash(["<<<<<<< SEARCH\nnot in the program\n=======\nq = 1\n>>>>>>> REPLACE",
            "<<<<<<< SEARCH\nx = 1\n=======\nx = 5\n>>>>>>> REPLACE"])

    siblings = mo.build_siblings("x = 1", "p", {}, 1, primary_code="x = 2")
    assert [s["code"] for s in siblings] == ["x = 5"]


def test_an_unevaluable_sibling_is_dropped_not_stored(worker, monkeypatch):
    """A mutation that will not score is a failed mutation, not an error."""
    monkeypatch.setenv(mo.ENV_MULTI_OFFSPRING, "3")

    async def boom(code, cid):
        raise RuntimeError("evaluator blew up")

    worker.evaluate_program = boom
    _stash(["<<<<<<< SEARCH\nx = 1\n=======\nx = 3\n>>>>>>> REPLACE"])
    assert mo.build_siblings("x = 1", "p", {}, 1, primary_code="x = 2") == []


def test_an_oversized_sibling_is_rejected(worker, monkeypatch):
    """The same max_code_length the engine applies to its own child."""
    from openevolve import process_parallel

    monkeypatch.setenv(mo.ENV_MULTI_OFFSPRING, "3")
    monkeypatch.setattr(process_parallel, "_worker_config",
                        types.SimpleNamespace(diff_pattern=None, max_code_length=5))
    _stash(["<<<<<<< SEARCH\nx = 1\n=======\nx = 123456789\n>>>>>>> REPLACE"])

    assert mo.build_siblings("x = 1", "p", {}, 1, primary_code="x = 2") == []


def test_no_alternatives_means_no_work(worker, monkeypatch):
    monkeypatch.setenv(mo.ENV_MULTI_OFFSPRING, "3")
    _stash([])
    assert mo.build_siblings("x = 1", "p", {}, 1, primary_code="x = 2") == []


def test_apply_diff_calling_extract_diffs_does_not_lose_the_alternatives(parse_hooks):
    """
    The bug that made the first live run produce zero siblings, and produced no
    error of any kind. Upstream's `apply_diff` calls `extract_diffs` internally,
    so the wrapper re-enters with a single already-split alternative — which
    has no marker. Assigning the stash unconditionally let that nested call
    overwrite it with an empty list, and the siblings vanished between being
    parsed and being used.
    """
    code_utils = parse_hooks
    code_utils.extract_diffs(SAMPLE)
    code_utils.apply_diff("x = 1", SAMPLE)          # re-enters extract_diffs

    assert len(mo.take_alternatives()) == 2


def test_an_unmarked_response_does_not_clear_a_stash_it_did_not_set(parse_hooks):
    code_utils = parse_hooks
    code_utils.extract_diffs(SAMPLE)
    code_utils.extract_diffs("<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE")

    assert len(mo.take_alternatives()) == 2


def test_reset_drops_a_previous_iterations_alternatives(parse_hooks):
    """
    Worker processes are reused. Without an explicit reset, an iteration whose
    response had no alternatives could attach siblings belonging to a different
    parent.
    """
    code_utils = parse_hooks
    code_utils.extract_diffs(SAMPLE)
    mo.reset()
    assert mo.take_alternatives() == []
