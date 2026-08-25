import os, sys, tempfile, shutil
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)


@pytest.fixture
def workspace():
    d = tempfile.mkdtemp(prefix="evo_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(autouse=True)
def clean_bus():
    """Each test gets a fresh process-wide bus."""
    from control_plane.telemetry.bus import reset_bus
    reset_bus()
    yield
    reset_bus()
