from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Callable, Sequence


@dataclass(frozen=True, slots=True)
class Experiment:
    hypothesis: str
    repetitions: int = 3
    experiment_id: str = ""


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    baseline: tuple[float, ...]
    candidate: tuple[float, ...]
    baseline_mean: float
    candidate_mean: float
    absolute_delta: float
    relative_delta: float
    baseline_variance: float
    candidate_variance: float
    standard_error: float
    accepted: bool
    evidence: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class InvalidExperimentError(ValueError):
    repetitions: int

    def __str__(self) -> str:
        return f"repetitions must be at least 2, received {self.repetitions}"


MetricRunner = Callable[[], float]


class ExperimentEngine:
    def run(
        self,
        experiment: Experiment,
        baseline: MetricRunner,
        candidate: MetricRunner,
        higher_is_better: bool = True,
    ) -> ExperimentResult:
        if experiment.repetitions < 2:
            raise InvalidExperimentError(experiment.repetitions)
        baseline_values = tuple(baseline() for _ in range(experiment.repetitions))
        candidate_values = tuple(candidate() for _ in range(experiment.repetitions))
        baseline_mean = statistics.fmean(baseline_values)
        candidate_mean = statistics.fmean(candidate_values)
        delta = candidate_mean - baseline_mean
        relative = delta / abs(baseline_mean) if baseline_mean else 0.0
        baseline_variance = statistics.variance(baseline_values)
        candidate_variance = statistics.variance(candidate_values)
        standard_error = (
            (baseline_variance / len(baseline_values))
            + (candidate_variance / len(candidate_values))
        ) ** 0.5
        accepted = delta > 0 if higher_is_better else delta < 0
        if standard_error and abs(delta) <= standard_error:
            accepted = False
        return ExperimentResult(
            baseline_values,
            candidate_values,
            baseline_mean,
            candidate_mean,
            delta,
            relative,
            baseline_variance,
            candidate_variance,
            standard_error,
            accepted,
            ("repeated_trials", "variance_checked"),
        )
