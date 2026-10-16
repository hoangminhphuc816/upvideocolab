"""So version Colab CLI/jkc đang cài với requirements-colab.txt."""
from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

PINS_FILE = Path(__file__).resolve().parent.parent / "requirements-colab.txt"


def parse_pins(text: str) -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if "==" not in line:
            continue
        name, _, ver = line.partition("==")
        pins[name.strip().lower()] = ver.strip()
    return pins


def check(installed: dict[str, str], pins: dict[str, str]) -> list[str]:
    problems = []
    for name in sorted(pins):
        want = pins[name]
        got = installed.get(name)
        if got is None:
            problems.append(f"{name}: chưa cài (cần {want})")
        elif got != want:
            problems.append(f"{name}: {got} != {want}")
    return problems


def installed_versions(keys) -> dict[str, str]:
    out = {}
    for name in keys:
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            pass
    return out


def main() -> int:
    pins = parse_pins(PINS_FILE.read_text(encoding="utf-8"))
    problems = check(installed_versions(pins), pins)
    if problems:
        print("Version Colab lệch với requirements-colab.txt:", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        print(
            "Cài lại: pip install -r requirements.txt -r requirements-colab.txt",
            file=sys.stderr,
        )
        return 1
    print("OK: " + ", ".join(f"{k}=={v}" for k, v in sorted(pins.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
