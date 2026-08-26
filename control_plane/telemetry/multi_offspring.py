"""
Ask for several alternatives per model request, and keep them all.

The economics are the whole argument. A request costs 84 seconds on the fastest
measured route and 292 on the primary; applying a diff and running the
evaluator costs milliseconds. Extracting two or three *distinct* candidates
from one response is therefore close to a linear throughput win, and throughput
is what the objective divides by.

The failure mode is equally specific, and it is not "it doesn't work": it is
three near-identical alternatives that all collapse to one AST hash. That is
throughput which is not real. G1 deduplication measures exactly that, and the
per-request `useful` count — siblings that survived the gate — is the number
worth reading, not the raw count.

How it fits without touching the engine
---------------------------------------

Upstream applies **every** diff block it finds to the parent, in sequence. So
asking for three alternatives in one response does not produce three children;
it produces one incoherent merge of all three. Multi-offspring therefore cannot
be done by prompting alone — the alternatives have to be separated before
upstream's parser sees them.

Three wrappers do that, in the three places the work happens:

  prompt   ask for N alternatives, each behind an explicit marker line
  parse    `extract_diffs`/`apply_diff` see only the first alternative, so the
           primary child is byte-for-byte what it would have been at N=1
  worker   the remaining alternatives become sibling programs — applied with
           upstream's own `apply_diff`, scored with the worker's own evaluator,
           and carried home on `Program.metadata`, the one channel that
           survives the process boundary

The main process then adds the siblings alongside the primary child, so they go
through the same MAP-Elites placement, novelty gate and telemetry as any other
candidate. Nothing about them is special once they land.

Off unless `OE_MAX_MULTI_OFFSPRING` is set, per the spec's instruction not to
enable it globally before it is benchmarked.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ENV_MULTI_OFFSPRING = "OE_MAX_MULTI_OFFSPRING"

# The marker the model is asked to put between alternatives. Deliberately not a
# SEARCH/REPLACE token: it has to survive upstream's diff regex untouched, and
# a marker that the parser recognised would be consumed before we saw it.
ALTERNATIVE_MARKER = "### ALTERNATIVE"
_MARKER_RE = re.compile(r"^\s*#{2,4}\s*ALTERNATIVE\b.*$", re.IGNORECASE | re.MULTILINE)

# Where siblings ride home from the worker.
SIBLINGS_KEY = "evolution_siblings"

# More than this and the response gets long enough that reasoning models
# truncate before finishing — the failure mode measured at 7,986 of 8,000
# tokens spent on hidden reasoning. Two or three is where the win is.
MAX_OFFSPRING = 5

# Alternatives 2..N of the current worker iteration, set while parsing.
_worker_alternatives: List[str] = []


def requested_offspring() -> int:
    """How many alternatives to ask for. 1 means the feature is off."""
    raw = os.environ.get(ENV_MULTI_OFFSPRING, "")
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, min(n, MAX_OFFSPRING))


def enabled() -> bool:
    return requested_offspring() > 1


# What a segment must contain to be an alternative rather than commentary.
_DIFF_HINT = "<<<<<<<"


def _has_marker(text: Optional[str]) -> bool:
    return bool(text) and bool(_MARKER_RE.search(text))


def reset() -> None:
    """
    Drop any alternatives left over from a previous iteration.

    Worker processes are reused, so without this an iteration whose response
    had no alternatives could inherit the last one's and attach siblings that
    belong to a different parent.
    """
    global _worker_alternatives
    _worker_alternatives = []


def split_alternatives(text: str) -> List[str]:
    """
    Split a response into its alternatives, in order.

    Two behaviours matter more than the splitting itself:

    * A response with no marker is one alternative. That is what a model
      ignoring the instruction produces, and it has to degrade to today's
      behaviour rather than to an error or an empty result.
    * The prose before the first marker ("Here are three approaches…") is a
      preamble, not an alternative. Returning it would make the *first*
      alternative the primary child's source — and since it contains no diff,
      the primary child would silently come back unchanged, which is the worst
      possible failure: a run that looks like it is working and evolves
      nothing.

      It is dropped only when it carries no diff blocks, so a model that writes
      its first alternative before the first marker keeps it.
    """
    if not text:
        return []
    if not _has_marker(text):
        return [text]
    parts = [p.strip() for p in _MARKER_RE.split(text)]
    parts = [p for p in parts if p]
    if parts and _DIFF_HINT not in parts[0]:
        parts = parts[1:]
    return parts or [text]


def prompt_instruction(n: int) -> str:
    return "\n".join([
        f"Produce {n} SEPARATE AND MATERIALLY DIFFERENT alternatives.",
        f"Begin each one with a line reading exactly '{ALTERNATIVE_MARKER} k'",
        "where k is 1, 2, 3 … and put that alternative's SEARCH/REPLACE blocks "
        "after it.",
        "Each alternative must stand alone as a complete change to the ORIGINAL "
        "program — do not write an alternative that depends on another having "
        "been applied first.",
        "Alternatives that differ only in naming, formatting or constants will "
        "be discarded as duplicates, so make them genuinely different "
        "approaches.",
    ])


# --------------------------------------------------------------------------
# hooks
# --------------------------------------------------------------------------

def install_prompt_hook(prompt: Dict[str, str]) -> Dict[str, str]:
    """
    Append the alternatives instruction to a built prompt.

    Called from the operator/prompt hook rather than installed separately, so a
    prompt is decorated exactly once no matter how many features are on.
    """
    n = requested_offspring()
    if n <= 1 or not isinstance(prompt, dict) or "system" not in prompt:
        return prompt
    out = dict(prompt)
    out["system"] = f"{out['system']}\n\n{prompt_instruction(n)}"
    return out


def install_parse_hooks() -> None:
    """
    Make upstream's parser see only the first alternative.

    This is what keeps the primary child identical to what it would have been
    with the feature off — the difference between an experiment with one
    variable and one with two.
    """
    if not enabled():
        return
    try:
        from openevolve.utils import code_utils
    except ImportError:
        return

    original_extract = getattr(code_utils, "extract_diffs", None)
    original_apply = getattr(code_utils, "apply_diff", None)
    if original_extract is None or original_apply is None:
        return
    if getattr(original_extract, "__evolution_instrumented__", False):
        return

    @functools.wraps(original_extract)
    def extract_wrapper(diff_text, *a, **kw):
        global _worker_alternatives
        parts = split_alternatives(diff_text)
        # Only a marked response updates the stash, and this is not a
        # micro-optimisation. Upstream's `apply_diff` calls `extract_diffs`
        # internally, so the wrapper re-enters with a single already-split
        # alternative — which has no marker. Assigning unconditionally let that
        # nested call overwrite the stash with an empty list, and the siblings
        # vanished between being parsed and being used. Found by tracing the
        # stash across one iteration; the symptom was simply "no siblings".
        if _has_marker(diff_text):
            _worker_alternatives = parts[1:]
        return original_extract(parts[0] if parts else diff_text, *a, **kw)

    @functools.wraps(original_apply)
    def apply_wrapper(code, diff_text, *a, **kw):
        parts = split_alternatives(diff_text)
        return original_apply(code, parts[0] if parts else diff_text, *a, **kw)

    extract_wrapper.__evolution_instrumented__ = True  # type: ignore[attr-defined]
    apply_wrapper.__evolution_instrumented__ = True    # type: ignore[attr-defined]
    code_utils.extract_diffs = extract_wrapper
    code_utils.apply_diff = apply_wrapper
    logger.info("OE-MAX multi-offspring enabled: %d alternatives per request",
                requested_offspring())


def take_alternatives() -> List[str]:
    """Consume this iteration's extra alternatives."""
    global _worker_alternatives
    alts, _worker_alternatives = _worker_alternatives, []
    return alts


def build_siblings(parent_code: str, parent_id: str, parent_metadata: Dict[str, Any],
                   iteration: Any, primary_code: Optional[str],
                   language: str = "python") -> List[Dict[str, Any]]:
    """
    Turn the extra alternatives into evaluated program dicts.

    Runs in the worker, where the evaluator already exists and the parent's code
    is in the snapshot. Every alternative that fails to apply, produces nothing,
    or reproduces the primary child exactly is dropped here rather than shipped
    home — a sibling identical to the primary is not throughput, and the cheapest
    place to notice is before it is pickled.
    """
    alternatives = take_alternatives()
    if not alternatives or not parent_code:
        return []

    try:
        from openevolve.process_parallel import _worker_config, _worker_evaluator
        from openevolve.utils.code_utils import apply_diff
    except ImportError:
        return []

    pattern = getattr(_worker_config, "diff_pattern", None) if _worker_config else None
    siblings: List[Dict[str, Any]] = []
    seen = {primary_code} if primary_code else set()

    for text in alternatives:
        try:
            code = apply_diff(parent_code, text, pattern) if pattern \
                else apply_diff(parent_code, text)
        except Exception as exc:
            logger.debug("sibling diff did not apply: %r", exc)
            continue
        if not code or code == parent_code or code in seen:
            continue
        max_len = getattr(_worker_config, "max_code_length", None) if _worker_config else None
        if max_len and len(code) > max_len:
            continue
        seen.add(code)

        sibling_id = str(uuid.uuid4())
        metrics = _evaluate(sibling_id, code)
        if metrics is None:
            continue
        siblings.append({
            "id": sibling_id,
            "code": code,
            "language": language,
            "parent_id": parent_id,
            "generation": (parent_metadata.get("generation") or 0) + 1,
            "metrics": metrics,
            "iteration_found": iteration,
            "metadata": {**parent_metadata, "changes": "alternative offspring",
                         "multi_offspring": True},
        })
    return siblings


def _evaluate(candidate_id: str, code: str) -> Optional[Dict[str, float]]:
    """Score a sibling with the worker's own evaluator, or drop it."""
    try:
        import asyncio

        from openevolve import process_parallel

        evaluator = getattr(process_parallel, "_worker_evaluator", None)
        if evaluator is None:
            return None
        return asyncio.run(evaluator.evaluate_program(code, candidate_id))
    except Exception as exc:
        # An alternative that will not evaluate is a failed mutation, not an
        # error: upstream discards its own unevaluable children the same way.
        logger.debug("sibling evaluation failed: %r", exc)
        return None
