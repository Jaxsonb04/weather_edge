#!/usr/bin/env python3
"""Verify the one-manifest trading install after a production upgrade."""

from __future__ import annotations

import re
import sys
from importlib.metadata import distributions
from typing import Iterable, Protocol


EXPECTED_PROJECT = "weatheredge"
EXPECTED_SCRIPT = "sfo-kalshi"
EXPECTED_ENTRY = "sfo_kalshi_quant.cli:main"
RETIRED_PROJECT = "sfo-kalshi-quant"


class EntryPointLike(Protocol):
    group: str
    name: str
    value: str


class DistributionLike(Protocol):
    metadata: object
    entry_points: Iterable[EntryPointLike]


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def validate_install(installed: Iterable[DistributionLike]) -> None:
    owners = [
        dist
        for dist in installed
        if canonical(dist.metadata.get("Name", ""))
        in {EXPECTED_PROJECT, RETIRED_PROJECT}
    ]
    if len(owners) != 1:
        names = [canonical(dist.metadata.get("Name", "")) for dist in owners]
        raise ValueError(
            f"expected exactly one WeatherEdge distribution metadata record, found {names}"
        )

    owner = owners[0]
    owner_name = canonical(owner.metadata.get("Name", ""))
    if owner_name != EXPECTED_PROJECT:
        raise ValueError(f"expected {EXPECTED_PROJECT} distribution, found {owner_name}")

    scripts = [
        entry
        for entry in owner.entry_points
        if entry.group == "console_scripts" and entry.name == EXPECTED_SCRIPT
    ]
    if len(scripts) != 1:
        raise ValueError(
            f"expected exactly one {EXPECTED_SCRIPT} console entry, found {len(scripts)}"
        )
    if scripts[0].value != EXPECTED_ENTRY:
        raise ValueError(
            f"expected {EXPECTED_SCRIPT} -> {EXPECTED_ENTRY}, found {scripts[0].value}"
        )


def main() -> int:
    try:
        validate_install(list(distributions()))
    except ValueError as error:
        print(f"invalid WeatherEdge install: {error}", file=sys.stderr)
        return 1
    print("verified weatheredge as the sole project owner of sfo-kalshi")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
