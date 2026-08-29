"""A real evaluator that does not import `openevolve`.

The seed-hook tests are about the hook -- does it forge variants, score them,
spread them across islands, report what it added, decline to re-seed a resumed
run. None of that is about the evaluator, but every variant is scored in a child
process, and a child that imports `openevolve` pays 2.9s before doing any work:
`openevolve/__init__` reaches controller -> evaluator -> llm.openai -> openai,
so an evaluator that only wants `EvaluationResult` drags in an API client.

The engine accepts a plain metrics dict as well as an `EvaluationResult` -- the
difference is that a dict carries no artifacts, which these tests do not use. So
this returns a dict, imports nothing but numpy, and the child costs ~0.3s
instead of ~4.6s.

It still runs the candidate. A stub returning a constant would make
`test_the_variants_are_spread_across_islands` pass without the variants ever
executing, which is not the same test.
"""

import importlib.util

import numpy as np

GLOBAL_MIN_X = -1.704
GLOBAL_MIN_Y = 0.678
GLOBAL_MIN_VALUE = -1.519

TRIALS = 3


def evaluate(program_path):
    """Score a program. Returns a plain dict; no openevolve import."""
    spec = importlib.util.spec_from_file_location("candidate", program_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, "run_search"):
        return {"combined_score": 0.0, "error": "missing run_search"}

    values, distances = [], []
    for seed in range(TRIALS):
        np.random.seed(seed)
        try:
            result = module.run_search()
        except Exception:
            continue
        if not isinstance(result, tuple) or len(result) not in (2, 3):
            continue
        x, y = float(result[0]), float(result[1])
        value = (
            float(result[2]) if len(result) == 3
            else float(np.sin(x) * np.cos(y) + np.sin(x * y) + (x**2 + y**2) / 20)
        )
        if not all(np.isfinite(v) for v in (x, y, value)):
            continue
        values.append(value)
        distances.append(float(np.hypot(x - GLOBAL_MIN_X, y - GLOBAL_MIN_Y)))

    if not values:
        return {"combined_score": 0.0, "error": "all trials failed"}

    value_score = 1.0 / (1.0 + abs(float(np.mean(values)) - GLOBAL_MIN_VALUE))
    distance_score = 1.0 / (1.0 + float(np.mean(distances)))
    reliability = len(values) / TRIALS
    return {
        "value_score": float(value_score),
        "distance_score": float(distance_score),
        "reliability_score": float(reliability),
        "combined_score": float(
            0.5 * value_score + 0.3 * distance_score + 0.2 * reliability
        ),
    }
