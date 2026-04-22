import pathlib
import subprocess
import sys


def test_import_without_opentelemetry() -> None:
    code = """
import importlib
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path.cwd() / "src"))
sys.modules["opentelemetry"] = None

telemetry = importlib.import_module("fastactor.telemetry")
assert telemetry.is_enabled() is False

try:
    telemetry.instrument(object())
except RuntimeError as error:
    assert "fastactor[otel]" in str(error) or "OpenTelemetry is not installed" in str(error)
else:
    raise AssertionError("instrument unexpectedly succeeded")
"""
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=pathlib.Path(__file__).resolve().parents[2],
        check=False,
    )

    assert proc.returncode == 0, proc.stderr or proc.stdout
