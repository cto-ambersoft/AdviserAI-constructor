import subprocess
import sys


def _run(command: list[str]) -> int:
    return subprocess.run(command, check=False).returncode


def dev() -> None:
    sys.exit(_run(["uv", "run", "uvicorn", "app.main:app", "--reload"]))


def lint() -> None:
    sys.exit(_run(["uv", "run", "ruff", "check", "."]))


def format_code() -> None:
    sys.exit(_run(["uv", "run", "ruff", "format", "."]))


def typecheck() -> None:
    sys.exit(_run(["uv", "run", "mypy", "app"]))


def test() -> None:
    sys.exit(_run(["uv", "run", "pytest"]))
