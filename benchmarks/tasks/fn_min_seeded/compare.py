"""Compare two programs seed by seed under the deterministic evaluator.

    python benchmarks/tasks/fn_min_seeded/compare.py A.py B.py

``combined_score`` is one number built from a mean over ten trials, and a mean
hides its own shape. This prints the ten trials behind it so a difference can be
read for what it is: "better everywhere" and "better on nine draws and
catastrophic on the tenth" produce similar aggregates and mean opposite things.

That distinction is not academic here. The first use of this tool found that a
local-refinement variant lands closer to the global minimum on 9 of 10 seeds --
by roughly 5x -- yet scores *lower* than the seed program, because on the tenth
it converges into the wrong basin at distance 3.99 and the mean carries it.
"""

import argparse
import importlib.util
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def _evaluator():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("seeded_evaluator", HERE / "evaluator.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def per_seed(ev, path):
    """Distance to the global minimum and function value, for each fixed seed."""
    rows = []
    for seed in ev.SEEDS:
        try:
            program = ev._load(path, seed)
            with ev.pinned_randomness(seed):
                x, y, value = ev._unpack(program.run_search())
            rows.append((float(np.hypot(x - ev.GLOBAL_MIN_X, y - ev.GLOBAL_MIN_Y)), value))
        except Exception as exc:
            print("  seed {} failed: {}: {}".format(seed, type(exc).__name__, exc))
            rows.append((float("nan"), float("nan")))
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("a", help="baseline program")
    parser.add_argument("b", help="candidate program")
    args = parser.parse_args()

    ev = _evaluator()
    a, b = per_seed(ev, args.a), per_seed(ev, args.b)

    print("A = {}".format(Path(args.a).name))
    print("B = {}".format(Path(args.b).name))
    print()
    print(
        "{:>6} {:>9} {:>9} {:>8} {:>10} {:>10}".format(
            "seed", "A dist", "B dist", "closer", "A value", "B value"
        )
    )
    print("-" * 60)

    wins = 0
    for seed, (da, va), (db, vb) in zip(ev.SEEDS, a, b):
        closer = "B" if db < da else "A"
        wins += db < da
        print(
            "{:>6} {:9.4f} {:9.4f} {:>8} {:10.4f} {:10.4f}".format(
                seed, da, db, closer, va, vb
            )
        )

    mean_a = float(np.mean([d for d, _ in a]))
    mean_b = float(np.mean([d for d, _ in b]))
    worst_a = float(np.max([d for d, _ in a]))
    worst_b = float(np.max([d for d, _ in b]))

    print("-" * 60)
    print("{:>6} {:9.4f} {:9.4f}".format("mean", mean_a, mean_b))
    print("{:>6} {:9.4f} {:9.4f}".format("worst", worst_a, worst_b))
    print()
    print("B is closer on {} of {} seeds.".format(wins, len(ev.SEEDS)))

    score_a = ev.evaluate(args.a).metrics["combined_score"]
    score_b = ev.evaluate(args.b).metrics["combined_score"]
    print("A combined_score: {:.6f}".format(score_a))
    print("B combined_score: {:.6f}".format(score_b))

    # The case worth calling out explicitly, because the aggregate says the
    # opposite of the per-seed record and the aggregate is what evolution reads.
    if wins > len(ev.SEEDS) / 2 and score_b < score_a:
        print()
        print(
            "NOTE: B wins the majority of seeds but scores lower. combined_score\n"
            "      averages distances, so B's worst seed ({:.2f}) outweighs its wins.\n"
            "      Evolution optimises the score, so it will reject B.".format(worst_b)
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
