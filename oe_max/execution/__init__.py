"""
Running evolved code without trusting it.

An evolved program is code nobody wrote and nobody reviewed, produced by a
model that was rewarded for making a number go up. Upstream runs it in the
evaluator's own process, which means a candidate that opens a file, spawns
processes or allocates without bound does all of that inside the run.

Nothing here is a security boundary against a determined adversary, and it is
important not to claim otherwise — see `limits.py` for exactly what each
backend does and does not stop. What it *is*: a real reduction in the blast
radius of the mistakes evolutionary search actually makes, which are runaway
loops, unbounded allocation, and writing where it should not.
"""

from .limits import ResourceLimits, describe_backends
from .runner import ExecutionResult, SandboxedRunner, available_backends

__all__ = [
    "ExecutionResult", "ResourceLimits", "SandboxedRunner",
    "available_backends", "describe_backends",
]
