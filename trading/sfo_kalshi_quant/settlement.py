from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.request import Request, urlopen


CLISFO_URL = "https://forecast.weather.gov/product.php?site=MTR&product=CLI&issuedby=SFO&format=txt"
CLISFO_VERSION_URL = CLISFO_URL + "&version={version}"


def cli_product_url(site: str, issuedby: str) -> str:
    return (
        "https://forecast.weather.gov/product.php"
        f"?site={site}&product=CLI&issuedby={issuedby}&format=txt"
    )


def fetch_recent_cli_settlements(
    site: str,
    issuedby: str,
    *,
    timeout: int = 20,
    versions: int = 10,
) -> dict[date, int]:
    """Recent CLI settlement highs by report date for any station's product.

    Versions are scanned newest-first and the first parse wins, so a corrected
    final report always shadows the preliminary evening one.
    """

    base = cli_product_url(site, issuedby)
    urls = [base, *[f"{base}&version={v}" for v in range(2, versions + 1)]]
    settlements: dict[date, int] = {}
    for url in urls:
        report = _fetch_clisfo_url(url, timeout=timeout)
        if report.report_date is None or report.max_temperature_f is None:
            continue
        settlements.setdefault(report.report_date, report.max_temperature_f)
    return settlements


@dataclass(frozen=True)
class ClisfoReport:
    report_date: date | None
    max_temperature_f: int | None
    raw_text: str


def fetch_latest_clisfo(timeout: int = 20) -> ClisfoReport:
    request = Request(CLISFO_URL, headers={"user-agent": "sfo-kalshi-quant/0.1"})
    with urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
    return parse_clisfo(text)


def fetch_recent_clisfo_settlements(
    *,
    timeout: int = 20,
    versions: int = 10,
) -> dict[date, int]:
    """Fetch recent CLISFO versions and return settlement highs by report date."""

    settlements: dict[date, int] = {}
    for url in _recent_clisfo_urls(versions):
        report = _fetch_clisfo_url(url, timeout=timeout)
        if report.report_date is None or report.max_temperature_f is None:
            continue
        settlements.setdefault(report.report_date, report.max_temperature_f)
    return settlements


def parse_clisfo(text: str) -> ClisfoReport:
    """Parse a San Francisco Airport Daily Climate Report.

    NWS CLISFO text formats shift over time, so this parser looks for the
    canonical maximum-temperature rows while keeping the raw report for audit.
    """

    report_date = _parse_report_date(text)
    max_temperature = _parse_max_temperature(text)
    return ClisfoReport(report_date=report_date, max_temperature_f=max_temperature, raw_text=text)


def _fetch_clisfo_url(url: str, *, timeout: int) -> ClisfoReport:
    request = Request(url, headers={"user-agent": "sfo-kalshi-quant/0.1"})
    with urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
    return parse_clisfo(text)


def _recent_clisfo_urls(versions: int) -> list[str]:
    if versions <= 1:
        return [CLISFO_URL]
    return [CLISFO_URL, *[CLISFO_VERSION_URL.format(version=version) for version in range(2, versions + 1)]]


def _parse_report_date(text: str) -> date | None:
    patterns = [
        r"CLIMATE SUMMARY FOR\s+(\w+\s+\d{1,2}\s+\d{4})",
        r"SUMMARY FOR\s+(\w+\s+\d{1,2}\s+\d{4})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        token = match.group(1).title()
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                from datetime import datetime

                return datetime.strptime(token, fmt).date()
            except ValueError:
                pass
    return None


def _parse_max_temperature(text: str) -> int | None:
    patterns = [
        r"MAXIMUM\s+(\d{2,3})\b",
        r"MAX TEMP(?:ERATURE)?\s+(\d{2,3})\b",
        r"\bMAX\s+(\d{2,3})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None
