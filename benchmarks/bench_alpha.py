"""
Alpha Evolve benchmark -- the real evolution loop driven by a *simulated* brain.

The loop, archive, cache, dedup and gates are the production ones. The model is
not: `SimulatedBrain` returns canned strings on a fixed 10/10/80 split of
invalid/duplicate/valid, so these numbers measure the machinery's bookkeeping
and cost nothing to reproduce.

They are NOT a measurement of evolution quality, and nothing here should be
quoted as one -- score and improvement counts are properties of the canned
responses. Every record written carries `brain` and `simulated` so a reader of
the JSON cannot mistake it for a live run.
"""
import asyncio, time, json, pathlib, random, sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from oe_max.brain.port import NullBrainPort
from oe_max.brain.evolution import EvolutionConfig, run_evolution
from oe_max.brain.types import BrainResponse

async def run_once(iterations=10, seed=42):
    # Simulate a real LLM that sometimes returns invalid, sometimes diff, sometimes full code
    class SimulatedBrain(NullBrainPort):
        def __init__(self):
            super().__init__()
            self.calls = 0
        async def generate(self, req):
            self.calls += 1
            # Simulate 10% invalid, 10% duplicate, 80% valid
            r = random.random()
            if r < 0.1:
                return BrainResponse(content="syntax error :\n", ok=True)
            if r < 0.2 and self.calls > 1:
                # Duplicate of previous
                return BrainResponse(content="def solve(x):\n    return x*2\n", ok=True)
            # Valid improvement
            return BrainResponse(content=f"def solve(x):\n    return x*{2 + self.calls}\n", ok=True)

    brain = SimulatedBrain()
    cfg = EvolutionConfig(iterations=iterations, seed=seed, initial_code="def solve(x):\n    return x\n")
    t0 = time.time()
    stats = await run_evolution(brain, cfg)
    wall = time.time() - t0
    return {
        "iterations": iterations,
        "seed": seed,
        "time_to_first_valid": wall / max(1, stats.valid) if stats.valid else wall,
        "time_to_first_improvement": wall / max(1, stats.improvements) if stats.improvements else wall,
        "model_calls_per_accepted": brain.calls / max(1, stats.valid),
        "duplicate_rate": stats.duplicates / max(1, stats.candidates + stats.duplicates),
        "invalid_rate": stats.invalid / max(1, stats.candidates + stats.invalid + stats.duplicates),
        "cache_hit_rate": stats.cache_hits / max(1, stats.cache_hits + stats.candidates),
        "eval_wall": stats.wall_s,
        "total_wall": wall,
        "archive_improvement": stats.improvements,
        "best_score": stats.best_score,
        "brain_calls": brain.calls,
        # Provenance travels with the numbers. A results file that does not say
        # what produced it gets read later as though a real model did.
        "brain": "SimulatedBrain(NullBrainPort)",
        "simulated": True,
    }

async def main():
    results = []
    for seed in [42, 123, 999]:
        r = await run_once(iterations=15, seed=seed)
        results.append(r)
        print(f"seed {seed}: {r}")
    # bool is an int subclass, so `simulated` would otherwise be averaged.
    avg = {k: sum(r[k] for r in results) / len(results)
           for k in results[0]
           if isinstance(results[0][k], (int, float)) and not isinstance(results[0][k], bool)}
    print("\nAverage:", json.dumps(avg, indent=2))
    payload = {
        "note": ("Simulated brain, not a live model. The evolution loop is real; "
                 "the responses are canned. Do not quote these as evolution quality."),
        "brain": "SimulatedBrain(NullBrainPort)",
        "simulated": True,
        "runs": results,
        "avg": avg,
    }
    out = pathlib.Path(__file__).resolve().parent / "alpha_results.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("Wrote benchmarks/alpha_results.json")

if __name__ == "__main__":
    asyncio.run(main())
