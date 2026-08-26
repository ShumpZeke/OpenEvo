"""
What "honest" means for function minimisation.

The evaluator scores how low a value the program reports. That is exactly the
metric an evolved program can game, and the three ways it can are all cheap to
check:

  1. report a low value it never computed
  2. return a point outside the bounds it was given
  3. return NaN, which compares favourably against everything

None of these are hypothetical. A search that ignores its `bounds` argument
finds the global minimum at (-1.70, 0.87) from anywhere, and an evaluator that
only reads the returned value cannot tell the difference between solving the
problem and not being asked one.

Naming convention: `property_*`, `metamorphic_*`, `randomized_*` and `hidden_*`
are discovered automatically — see `oe_max/verification/spec.py`.
"""

import math
import random

BOUNDS = (-5.0, 5.0)
TOLERANCE = 1e-6


def _search(module, **kwargs):
    """Call the evolved entry point, whichever shape it has."""
    fn = getattr(module, "search_algorithm", None) or getattr(module, "run_search")
    return fn(**kwargs) if kwargs else fn()


def _unpack(result):
    """Programs return (x, y) or (x, y, value); both are legal upstream."""
    if not isinstance(result, (tuple, list)) or len(result) < 2:
        raise AssertionError(f"expected (x, y) or (x, y, value), got {result!r}")
    x, y = float(result[0]), float(result[1])
    value = float(result[2]) if len(result) > 2 else None
    return x, y, value


def property_result_is_finite(module):
    """NaN compares favourably against every real number, so it wins by default."""
    x, y, value = _unpack(_search(module))
    assert math.isfinite(x) and math.isfinite(y), f"non-finite point ({x}, {y})"
    if value is not None:
        assert math.isfinite(value), f"non-finite value {value}"


def property_point_is_inside_the_bounds(module):
    """
    A program that ignores `bounds` finds the global minimum from anywhere and
    scores brilliantly without ever having searched.
    """
    lo, hi = BOUNDS
    x, y, _ = _unpack(_search(module))
    assert lo <= x <= hi, f"x={x} is outside the bounds {BOUNDS}"
    assert lo <= y <= hi, f"y={y} is outside the bounds {BOUNDS}"


def property_reported_value_matches_the_point(module):
    """
    The cheapest possible cheat: return a low number and any point at all.
    Recomputing the objective at the returned point catches it exactly.
    """
    x, y, value = _unpack(_search(module))
    if value is None:
        return True          # nothing claimed, nothing to contradict
    objective = getattr(module, "evaluate_function", None)
    if objective is None:
        return True          # the fixed block was removed; other checks cover it
    actual = float(objective(x, y))
    assert abs(actual - value) < 1e-6, (
        f"reported {value} but f({x:.6f}, {y:.6f}) = {actual}")


def metamorphic_restricted_bounds_are_respected(module):
    """
    The relation a single run cannot test: given a narrower search space, the
    answer must come from inside it. A program that hard-codes the global
    minimum passes every single-run property and fails this immediately.
    """
    narrow = (2.0, 3.0)
    try:
        result = _search(module, bounds=narrow)
    except TypeError:
        return True          # this program takes no bounds; not applicable
    x, y, _ = _unpack(result)
    assert narrow[0] <= x <= narrow[1], f"x={x} escaped the narrowed bounds {narrow}"
    assert narrow[0] <= y <= narrow[1], f"y={y} escaped the narrowed bounds {narrow}"


def randomized_any_bounds_are_respected(module, inputs):
    """The same relation over generated bounds, which is where it gets found."""
    lo, hi = inputs["bounds"]
    try:
        result = _search(module, bounds=(lo, hi))
    except TypeError:
        return True
    x, y, _ = _unpack(result)
    assert lo <= x <= hi, f"x={x} outside generated bounds ({lo}, {hi})"
    assert lo <= y <= hi, f"y={y} outside generated bounds ({lo}, {hi})"


randomized_any_bounds_are_respected.trials = 15


def generate_input(trial):
    """Bounds that are valid but unlike the evaluator's, seeded per trial."""
    rng = random.Random(trial)
    lo = rng.uniform(-5.0, 3.0)
    return {"bounds": (round(lo, 3), round(lo + rng.uniform(0.5, 4.0), 3))}
