"""Measure whether an evaluator gives the same program the same score twice.

Run it against both evaluators to see the difference the seeding makes:

    python benchmarks/tasks/fn_min_seeded/check_determinism.py
    python benchmarks/tasks/fn_min_seeded/check_determinism.py --upstream

Exits non-zero if the evaluator under test is not deterministic, so it can be
used as a gate. A spread of exactly 0.0 is the pass condition -- "small" is not
good enough, because the whole point is that score differences are attributable
to the program rather than to the draw.
"""

import argparse
import importlib.util
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent


def load_evaluator(path):
    spec = importlib.util.spec_from_file_location("evaluator_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def score_of(result):
    """Both evaluators return EvaluationResult; be tolerant of a bare dict too."""
    metrics = getattr(result, "metrics", result)
    return float(metrics.get("combined_score", 0.0))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--upstream",
        action="store_true",
        help="test examples/function_minimization/evaluator.py instead",
    )
    parser.add_argument("-n", type=int, default=5, help="evaluations to run (default 5)")
    parser.add_argument(
        "--program",
        default=None,
        help="program to score (default: this benchmark's seed program)",
    )
    args = parser.parse_args()

    sys.path.insert(0, str(ROOT))

    if args.upstream:
        evaluator_path = ROOT / "examples" / "function_minimization" / "evaluator.py"
        program = args.program or str(
            ROOT / "examples" / "function_minimization" / "initial_program.py"
        )
    else:
        evaluator_path = HERE / "evaluator.py"
        program = args.program or str(HERE / "initial_program.py")

    evaluator = load_evaluator(evaluator_path)

    print("evaluator : {}".format(evaluator_path.relative_to(ROOT)))
    print("program   : {}".format(Path(program).name))
    print()

    scores = []
    for i in range(args.n):
        score = score_of(evaluator.evaluate(program))
        scores.append(score)
        print("  run {}: {:.6f}".format(i + 1, score))

    spread = max(scores) - min(scores)
    print()
    print("  mean   {:.6f}".format(statistics.mean(scores)))
    print("  spread {:.6f}".format(spread))
    print()

    if spread == 0.0:
        print("DETERMINISTIC -- every run scored identically.")
        return 0

    print(
        "NOT DETERMINISTIC -- the same program scored {:.4f} apart.\n"
        "Any comparison of two programs differing by less than that is noise.".format(
            spread
        )
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
