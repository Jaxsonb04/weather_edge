"""Forecaster-side CLISFO (SFO Daily Climate Report) settlement parser.

The Kalshi market settles on the NWS Daily Climate Report MAXIMUM for San
Francisco Airport, not on the max of hourly station observations. This module
fetches and parses that report so the forecaster can score and learn against the
same ground truth the trader settles on. It mirrors
``trading/sfo_kalshi_quant/settlement.py`` (the project deliberately duplicates
small cross-module utilities, like ``settlement_calendar``) and adds
temperature-section anchoring so a stray "MAXIMUM" elsewhere in the product
cannot be misread as the daily high.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class CliReport:
    report_date: date | None
    max_temperature_f: int | None
    raw_text: str
    is_preliminary: bool


def cli_product_url(site: str, issuedby: str) -> str:
    return (
        "https://forecast.weather.gov/product.php"
        f"?site={site}&product=CLI&issuedby={issuedby}&format=txt"
    )


def fetch_recent_cli_settlements(
    site: str, issuedby: str, *, timeout: int = 20, versions: int = 10
) -> dict[date, int]:
    """Settlement-high (F) by report date across recent CLI versions for any station.

    Versions are scanned newest-first and the first parse wins, so a corrected
    final report always shadows the preliminary one.
    """

    return {
        report_date: int(report.max_temperature_f)
        for report_date, report in fetch_recent_cli_reports(
            site, issuedby, timeout=timeout, versions=versions
        ).items()
        if report.max_temperature_f is not None
    }


def fetch_recent_cli_reports(
    site: str, issuedby: str, *, timeout: int = 20, versions: int = 10
) -> dict[date, CliReport]:
    """Newest confirmed-final report per date, or newest preliminary if alone."""

    reports: dict[date, CliReport] = {}
    for url in _recent_cli_urls(cli_product_url(site, issuedby), versions):
        try:
            report = _fetch_cli_report(url, timeout=timeout)
        except OSError:
            continue
        if report.report_date is None or report.max_temperature_f is None:
            continue
        current = reports.get(report.report_date)
        if current is None or (current.is_preliminary and not report.is_preliminary):
            reports[report.report_date] = report
    return reports


def _recent_cli_urls(base_url: str, versions: int) -> list[str]:
    if versions <= 1:
        return [base_url]
    return [base_url, *[f"{base_url}&version={v}" for v in range(2, versions + 1)]]


def parse_clisfo(text: str) -> tuple[date | None, int | None]:
    return _parse_report_date(text), _parse_max_temperature(text)


def parse_cli_report(text: str) -> CliReport:
    report_date, high = parse_clisfo(text)
    preliminary = bool(
        re.search(
            r"\b(?:VALID\b[^\n]*\b)?AS OF\s+\d{1,4}(?::\d{2})?\s*(?:AM|PM)?(?:[^\n]*LOCAL TIME)?",
            text,
            flags=re.IGNORECASE,
        )
    )
    return CliReport(report_date, high, text, preliminary)


def _fetch_cli_report(url: str, *, timeout: int) -> CliReport:
    request = Request(url, headers={"user-agent": "sfo-weatheredge-forecaster/0.1"})
    with urlopen(request, timeout=timeout) as response:
        text = response.read().decode("utf-8", errors="replace")
    return parse_cli_report(text)


def _parse_report_date(text: str) -> date | None:
    for pattern in (
        r"CLIMATE SUMMARY FOR\s+(\w+\s+\d{1,2}\s+\d{4})",
        r"SUMMARY FOR\s+(\w+\s+\d{1,2}\s+\d{4})",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        token = match.group(1).title()
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(token, fmt).date()
            except ValueError:
                pass
    return None


def _parse_max_temperature(text: str) -> int | None:
    # Anchor to the TEMPERATURE (F) section first so an unrelated "MAXIMUM"
    # elsewhere in the product cannot be mistaken for the daily high. The
    # canonical row is "MAXIMUM   67   12:29 PM ...".
    temp_header = re.search(r"TEMPERATURE\s*\(F\)", text, flags=re.IGNORECASE)
    if temp_header:
        window = text[temp_header.end(): temp_header.end() + 600]
        anchored = re.search(r"MAXIMUM\s+(-?\d{1,3})\b", window, flags=re.IGNORECASE)
        if anchored:
            return int(anchored.group(1))
    for pattern in (
        r"MAXIMUM\s+(-?\d{1,3})\b",
        r"MAX TEMP(?:ERATURE)?\s+(-?\d{1,3})\b",
        r"\bMAX\s+(-?\d{1,3})\b",
    ):
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None
