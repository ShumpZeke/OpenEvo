"""
GPU sampling: absence is a value, and [N/A] is not zero.

`EventType.RESOURCE_GPU` existed from the start and nothing emitted it, so a
project built for running expensive workloads reported CPU, RAM and disk and
said nothing about the accelerator.

The failure to guard against is not a crash. It is a plausible-looking zero on
a dashboard for a machine that has no GPU, or for a field the driver declined
to report — a number nothing measured, which this project treats as a bug.
"""

from __future__ import annotations

import subprocess

import pytest

from control_plane.telemetry import gpu


REAL_OUTPUT = (
    "0, NVIDIA A100-SXM4-40GB, 73, 12043, 40960, 61, 249.85\n"
    "1, NVIDIA GeForce RTX 4090, 0, 512, 24564, 35, [N/A]\n"
)


def test_parses_a_multi_gpu_host():
    gpus = gpu.parse(REAL_OUTPUT)

    assert [g.index for g in gpus] == [0, 1]
    assert gpus[0].name == "NVIDIA A100-SXM4-40GB"
    assert gpus[0].utilization_percent == 73.0
    assert gpus[0].memory_percent == 29.4
    assert gpus[0].power_w == 249.85


def test_a_field_the_driver_declines_to_report_is_none_not_zero():
    """
    Real drivers emit `[N/A]` and `[Not Supported]` — power draw on many laptop
    GPUs, for one. Coercing those to 0.0 puts an invented reading on the
    dashboard, and 0 W is a plausible enough number that nobody would question
    it.
    """
    gpus = gpu.parse(REAL_OUTPUT)

    assert gpus[1].power_w is None
    assert gpus[1].utilization_percent == 0.0, (
        "a genuine zero must survive; only the markers become None")


@pytest.mark.parametrize("marker", ["[N/A]", "[Not Supported]", "", "   ", "garbage"])
def test_every_unreportable_form_becomes_none(marker):
    assert gpu._num(marker) is None


def test_a_zero_reading_is_kept():
    assert gpu._num("0") == 0.0
    assert gpu._num("0.0") == 0.0


def test_malformed_lines_are_skipped_rather_than_crashing():
    """A driver upgrade that changes the output format must not take the
    system view down with it."""
    gpus = gpu.parse("not,a,gpu,line\n\n0, Real GPU, 10, 1, 2, 3, 4\n")

    assert len(gpus) == 1
    assert gpus[0].name == "Real GPU"


def test_no_driver_is_reported_as_absent_with_a_reason(monkeypatch):
    monkeypatch.setattr(gpu.shutil, "which", lambda _: None)

    r = gpu.probe()

    assert r.available is False
    assert "nvidia-smi" in r.reason
    assert r.gpus == []
    assert r.to_dict()["count"] == 0


def test_a_wedged_driver_is_distinguished_from_having_no_gpu(monkeypatch):
    """
    A machine without an accelerator and a machine with a broken one call for
    completely different responses, so they must not collapse into one message.
    """
    monkeypatch.setattr(gpu.shutil, "which", lambda _: "/usr/bin/nvidia-smi")

    def _timeout(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="nvidia-smi", timeout=2.0)

    monkeypatch.setattr(gpu.subprocess, "run", _timeout)

    r = gpu.probe()

    assert r.available is False
    assert "timed out" in r.reason


def test_probe_never_raises_even_when_the_binary_misbehaves(monkeypatch):
    """A system view that 500s because of a GPU query is worse than no metric."""
    monkeypatch.setattr(gpu.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(gpu.subprocess, "run",
                        lambda *a, **kw: (_ for _ in ()).throw(OSError("boom")))

    r = gpu.probe()

    assert r.available is False and "boom" in r.reason


def test_a_successful_probe_reports_the_gpus(monkeypatch):
    class _Done:
        returncode = 0
        stdout = REAL_OUTPUT
        stderr = ""

    monkeypatch.setattr(gpu.shutil, "which", lambda _: "/usr/bin/nvidia-smi")
    monkeypatch.setattr(gpu.subprocess, "run", lambda *a, **kw: _Done())

    r = gpu.probe()

    assert r.available is True
    assert r.to_dict()["count"] == 2
    assert r.to_dict()["gpus"][1]["power_w"] is None
