"""
Verification: deciding whether an improvement is real.

The evaluator answers "what did this score?". That is not the same question as
"is this score honest", and evolutionary search is unusually good at finding
the gap between them — a candidate that returns a hard-coded answer, ignores
the constraint it was given, or reports a value it never computed will happily
outscore one that solves the problem.

`stages` runs the checks, `spec` is how a task declares what "honest" means for
it, `suspicion` decides which candidates are worth the extra cost, and
`counterexamples` remembers what broke so a later mutation can be asked to fix
it.
"""

from .counterexamples import Counterexample, CounterexampleStore
from .spec import Check, CheckResult, VerificationSpec, load_spec
from .stages import V1Verifier, VerificationReport
from .suspicion import SuspicionDetector, SuspicionVerdict

__all__ = [
    "Check", "CheckResult", "Counterexample", "CounterexampleStore",
    "SuspicionDetector", "SuspicionVerdict", "V1Verifier", "VerificationReport",
    "VerificationSpec", "load_spec",
]
