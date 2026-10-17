"""OPM General Schedule pay tables, bundled one file per year.

Each ``paytables/gs-<year>.json`` holds the base GS table, every locality
area's percentage and the pay cap, copied from the sources listed in the
file. Locality rates are computed the way OPM computes them: base rate times
(1 + locality percent), rounded to the nearest dollar, then limited to
Executive Schedule level IV.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from functools import lru_cache
from importlib import resources
from typing import Any

STEPS = 10
MAX_GRADE = 15


@dataclass(frozen=True)
class Locality:
    code: str
    name: str
    percent: Decimal
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class PayTable:
    year: int
    pay_cap: int
    base: dict[int, tuple[int, ...]]
    localities: dict[str, Locality]
    sources: tuple[str, ...]

    def rate(self, grade: int, step: int, locality: str) -> int:
        """Annual locality rate for ``grade`` and ``step`` (1-10), capped."""
        if grade not in self.base:
            raise KeyError(f"GS-{grade}")
        area = self.localities[locality]
        exact = Decimal(self.base[grade][step - 1]) * (1 + area.percent / 100)
        return min(int(exact.quantize(Decimal(1), rounding=ROUND_HALF_UP)), self.pay_cap)

    def top_of_grade(self, grade: int, locality: str) -> int:
        """Step 10 of ``grade``; 0 below GS-1, so "above grade 0" means any pay."""
        return self.rate(grade, STEPS, locality) if grade >= 1 else 0

    def find_locality(self, value: str) -> str:
        """Locality code for an OPM code, area name, alias or unambiguous words from a name.

        Raises ``LookupError`` with the candidates when nothing or several areas match.
        """
        wanted = _normalize(value)
        if not wanted:
            raise LookupError("expected an OPM locality code like \"DCB\" or an area name")
        for code, area in self.localities.items():
            if wanted in {_normalize(code), _normalize(area.name), *map(_normalize, area.aliases)}:
                return code
        words = set(wanted.split())
        hits = [code for code, area in self.localities.items()
                if any(words <= set(_normalize(name).split()) for name in (area.name, *area.aliases))]
        if len(hits) == 1:
            return hits[0]
        if hits:
            raise LookupError("matches several areas: " + ", ".join(
                f"{code} ({self.localities[code].name})" for code in hits))
        raise LookupError("no OPM locality area matches; run `usajobs-monitor pay-table` for the list")


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).lower().replace(".", "")).strip()


def available_years() -> list[int]:
    folder = resources.files(__package__).joinpath("paytables")
    years = []
    for entry in folder.iterdir():
        match = re.fullmatch(r"gs-(\d{4})\.json", entry.name)
        if match:
            years.append(int(match.group(1)))
    return sorted(years)


@lru_cache(maxsize=None)
def load_table(year: int) -> PayTable:
    if year not in available_years():
        raise LookupError(f"no pay table bundled for {year}; available: "
                          + ", ".join(str(y) for y in available_years()))
    raw: dict[str, Any] = json.loads(
        resources.files(__package__).joinpath("paytables", f"gs-{year}.json").read_text(encoding="utf-8"))
    base = {int(grade): tuple(int(r) for r in rates) for grade, rates in raw["base"].items()}
    localities = {
        code: Locality(code=code, name=area["name"], percent=Decimal(area["percent"]),
                       aliases=tuple(area.get("aliases", ())))
        for code, area in raw["localities"].items()
    }
    return PayTable(year=int(raw["year"]), pay_cap=int(raw["pay_cap"]), base=base,
                    localities=localities, sources=tuple(raw.get("sources", ())))
