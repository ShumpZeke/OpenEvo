"""
GPU sampling.

`EventType.RESOURCE_GPU` has existed since the event model was written and
nothing ever emitted it, so the Control Center reported CPU, RAM and disk and
was silent about the accelerator — on a project whose whole purpose is running
expensive workloads.

Two decisions worth stating, because both could reasonably have gone the other
way:

**`nvidia-smi`, not a Python binding.** `pynvml` and friends are another
dependency, another wheel that must match the driver, and a failure mode where
`import` succeeds and every call raises. `nvidia-smi` ships with the driver, so
if there is an NVIDIA GPU it is there, and if it is not there the answer to
"what GPUs are present?" is already "we cannot tell".

**Absence is a value, not an error.** A machine with no GPU is the normal case,
not a fault, and it must be distinguishable from a machine where sampling
failed. `probe()` therefore always returns a result carrying `available` and a
`reason`, and never raises. Rendering a GPU-less host as `0%` utilised would be
a fabricated number, which this project forbids.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Kept short. This is sampled on a request path, and a hung nvidia-smi — which
# happens on a wedged driver — must not hold the whole system view hostage.
_TIMEOUT_S = 2.0

_QUERY = (
    "index,name,utilization.gpu,memory.used,memory.total,"
    "temperature.gpu,power.draw"
)


@dataclass
class GpuSample:
    index: int
    name: str
    utilization_percent: Optional[float] = None
    memory_used_mb: Optional[float] = None
    memory_total_mb: Optional[float] = None
    temperature_c: Optional[float] = None
    power_w: Optional[float] = None

    @property
    def memory_percent(self) -> Optional[float]:
        if not self.memory_total_mb:
            return None
        return round(100.0 * (self.memory_used_mb or 0.0) / self.memory_total_mb, 1)

    def to_dict(self) -> Dict[str, Any]:
        d = dict(self.__dict__)
        d["memory_percent"] = self.memory_percent
        return d


@dataclass
class GpuProbe:
    available: bool
    reason: str = ""
    gpus: List[GpuSample] = field(default_factory=list)
    sampled_at: float = field(default_factory=time.time)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "count": len(self.gpus),
            "gpus": [g.to_dict() for g in self.gpus],
            "sampled_at": self.sampled_at,
        }


def _num(raw: str) -> Optional[float]:
    """
    Parse one field, treating nvidia-smi's own not-applicable markers as None.

    Real drivers emit `[N/A]` and `[Not Supported]` for fields a particular
    card does not report — power draw on many laptop GPUs, for one. Coercing
    those to 0.0 would put a fabricated number on the dashboard, which is
    precisely the case this project treats as a bug.
    """
    raw = (raw or "").strip()
    if not raw or raw.startswith("[") or raw.lower() in {"n/a", "not supported"}:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def parse(output: str) -> List[GpuSample]:
    """Parse `nvidia-smi --format=csv,noheader,nounits` output."""
    samples: List[GpuSample] = []
    for line in (output or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:
            index = int(parts[0])
        except ValueError:
            continue
        samples.append(GpuSample(
            index=index,
            name=parts[1],
            utilization_percent=_num(parts[2]) if len(parts) > 2 else None,
            memory_used_mb=_num(parts[3]) if len(parts) > 3 else None,
            memory_total_mb=_num(parts[4]) if len(parts) > 4 else None,
            temperature_c=_num(parts[5]) if len(parts) > 5 else None,
            power_w=_num(parts[6]) if len(parts) > 6 else None,
        ))
    return samples


def probe() -> GpuProbe:
    """
    Sample the GPUs, or say why not. Never raises.

    A system view that 500s because a machine has no GPU would be a worse
    failure than the missing metric.
    """
    binary = shutil.which("nvidia-smi")
    if not binary:
        return GpuProbe(False, "nvidia-smi not found — no NVIDIA driver on this host")

    try:
        proc = subprocess.run(
            [binary, f"--query-gpu={_QUERY}", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # Usually a wedged driver. Worth distinguishing from "no GPU": one is
        # a machine without an accelerator, the other is a machine with a
        # broken one, and they call for completely different responses.
        return GpuProbe(False, f"nvidia-smi timed out after {_TIMEOUT_S:.0f}s")
    except OSError as exc:
        return GpuProbe(False, f"nvidia-smi could not be run: {exc}"[:200])

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return GpuProbe(False, f"nvidia-smi exited {proc.returncode}: "
                               f"{detail[0] if detail else 'no output'}"[:200])

    gpus = parse(proc.stdout)
    if not gpus:
        return GpuProbe(False, "nvidia-smi reported no GPUs")
    return GpuProbe(True, "", gpus)
