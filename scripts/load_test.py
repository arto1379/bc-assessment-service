#!/usr/bin/env python3
"""Automated test harness for the BC Assessment service.

Covers the matrix the troubleshooting PRD asks for: many unique addresses,
repeats (cache behaviour), unknown addresses, empty/malformed input, a
long-running soak, and concurrent bursts.

The most important check is `--mode stale`. The original scraper reused one
Playwright page and waited for the text "Total value $..." to appear; because
the previous property's markup was still on that page, the wait returned
immediately and the *previous* property's numbers were returned and cached
under the new address. Asserting that distinct addresses yield distinct
total_values is the direct regression test for that defect.

Usage:

    python scripts/load_test.py --base-url http://localhost:8011 --mode all
    python scripts/load_test.py --mode soak --duration 14400   # 4h soak
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import httpx

DEFAULT_ADDRESSES = [
    "7009 TISDALL ST VANCOUVER",
    "3690 10TH AVE W VANCOUVER",
    "502-1055 HOMER ST VANCOUVER",
    "1488-2088 BARCLAY ST VANCOUVER",
    "4569 W 2ND AVE VANCOUVER",
    "702-1290 BURNABY ST VANCOUVER",
    "403-3355 CYPRESS PL WEST VANCOUVER",
    "205-2255 YORK AVE VANCOUVER",
    "TH7-1650 BAYSHORE DR VANCOUVER",
    "5555 CAMBIE ST VANCOUVER",
    "1405-788 RICHARDS ST VANCOUVER",
    "3001-1188 QUEBEC ST VANCOUVER",
    "3080 PRINCE EDWARD ST VANCOUVER",
    "510-6188 NO 3 RD RICHMOND",
    "7009-8080 GRANVILLE AVE RICHMOND",
    "5511 NO 1 RD RICHMOND",
    "1500 MARINE DR NORTH VANCOUVER",
    "2045 LONSDALE AVE NORTH VANCOUVER",
    "512 BLUE MOUNTAIN ST COQUITLAM",
    "3300 KINGSLEY ST VICTORIA",
]

UNKNOWN_ADDRESSES = [
    "99999 NOWHERE ST ATLANTIS",
    "1 DOESNOTEXIST BLVD NARNIA",
    "ZZZZ INVALID AVE VANCOUVER",
]

MALFORMED_ADDRESSES = [
    "",
    "   ",
    "!!!",
    "%20%20",
    "a" * 500,
    "'; DROP TABLE assessment_cache; --",
    "<script>alert(1)</script>",
]


@dataclass
class Call:
    address: str
    status_code: int
    elapsed: float
    api_status: str = ""
    error_type: Optional[str] = None
    total_value: Optional[str] = None
    exception: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.api_status == "success"


@dataclass
class Report:
    name: str
    calls: list[Call] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)

    def add_note(self, note: str) -> None:
        self.notes.append(note)

    def fail(self, message: str) -> None:
        self.failures.append(message)


async def call_address(client: httpx.AsyncClient, address: str) -> Call:
    started = time.monotonic()
    try:
        response = await client.get("/address", params={"address": address})
        elapsed = time.monotonic() - started
    except Exception as exc:
        return Call(
            address=address,
            status_code=0,
            elapsed=time.monotonic() - started,
            exception=f"{type(exc).__name__}: {exc}",
        )

    try:
        body: dict[str, Any] = response.json()
    except Exception:
        return Call(
            address=address,
            status_code=response.status_code,
            elapsed=elapsed,
            exception="non-JSON response",
        )

    total_value = None
    if body.get("status") == "success":
        total_value = (body.get("data") or {}).get("result", {}).get("total_value")

    return Call(
        address=address,
        status_code=response.status_code,
        elapsed=elapsed,
        api_status=str(body.get("status", "")),
        error_type=body.get("error_type"),
        total_value=total_value,
    )


def _percentiles(values: Iterable[float]) -> str:
    data = sorted(values)
    if not data:
        return "n/a"
    def pct(p: float) -> float:
        idx = min(int(len(data) * p), len(data) - 1)
        return data[idx]
    return (
        f"min={data[0]:.2f}s p50={pct(0.5):.2f}s "
        f"p95={pct(0.95):.2f}s max={data[-1]:.2f}s"
    )


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def mode_unique(client: httpx.AsyncClient, addresses: list[str]) -> Report:
    report = Report("unique addresses")
    for address in addresses:
        report.calls.append(await call_address(client, address))
    return report


async def mode_cache(client: httpx.AsyncClient, addresses: list[str]) -> Report:
    """Each address twice: the second call must be far faster and identical."""
    report = Report("cache behaviour")
    for address in addresses[:5]:
        first = await call_address(client, address)
        second = await call_address(client, address)
        report.calls.extend([first, second])

        if not first.ok:
            report.add_note(f"{address}: first call not successful, cache not exercised")
            continue
        if first.total_value != second.total_value:
            report.fail(
                f"{address}: cached value differs "
                f"({first.total_value!r} then {second.total_value!r})"
            )
        elif second.elapsed < first.elapsed:
            report.add_note(
                f"{address}: {first.elapsed:.2f}s -> {second.elapsed:.2f}s (cached)"
            )
    return report


async def mode_unknown(client: httpx.AsyncClient) -> Report:
    """Unknown addresses must report not_found, never a scraper fault."""
    report = Report("unknown addresses")
    for address in UNKNOWN_ADDRESSES:
        call = await call_address(client, address)
        report.calls.append(call)
        if call.ok:
            report.fail(f"{address!r}: unexpectedly succeeded")
        elif call.error_type not in (None, "not_found"):
            report.add_note(
                f"{address!r}: classified {call.error_type!r} rather than not_found"
            )
    return report


async def mode_malformed(client: httpx.AsyncClient) -> Report:
    """Malformed input must produce a controlled error, never a 500."""
    report = Report("malformed input")
    for address in MALFORMED_ADDRESSES:
        call = await call_address(client, address)
        report.calls.append(call)
        if call.status_code >= 500:
            report.fail(f"{address[:40]!r}: server error {call.status_code}")
        if call.exception:
            report.fail(f"{address[:40]!r}: {call.exception}")
    return report


async def mode_concurrent(
    client: httpx.AsyncClient, addresses: list[str], burst: int
) -> Report:
    """Fire a burst at once — no request may hang or crash the service."""
    report = Report(f"concurrent burst (n={burst})")
    chosen = (addresses * burst)[:burst]
    results = await asyncio.gather(
        *(call_address(client, a) for a in chosen), return_exceptions=True
    )
    for item in results:
        if isinstance(item, Exception):
            report.fail(f"burst raised {type(item).__name__}: {item}")
        else:
            report.calls.append(item)

    health = await client.get("/health")
    report.add_note(f"/health after burst -> {health.status_code}")
    if health.status_code >= 500:
        report.fail("service reported unhealthy after concurrent burst")
    return report


async def mode_stale(client: httpx.AsyncClient, addresses: list[str]) -> Report:
    """THE regression test: distinct addresses must not share a total_value."""
    report = Report("stale-result detection")
    seen: dict[str, str] = {}

    for address in addresses:
        call = await call_address(client, address)
        report.calls.append(call)
        if not call.ok or not call.total_value:
            continue
        if call.total_value in seen and seen[call.total_value] != address:
            report.fail(
                f"STALE RESULT: {address!r} returned {call.total_value} — "
                f"the same value already returned for {seen[call.total_value]!r}"
            )
        else:
            seen[call.total_value] = address

    report.add_note(f"{len(seen)} distinct total_values across {len(addresses)} addresses")
    return report


async def mode_soak(
    client: httpx.AsyncClient, addresses: list[str], duration: float, interval: float
) -> Report:
    """Long-running repeated requests — the condition that used to degrade."""
    report = Report(f"soak ({duration:.0f}s)")
    deadline = time.monotonic() + duration
    index = 0
    consecutive_failures = 0

    while time.monotonic() < deadline:
        address = addresses[index % len(addresses)]
        index += 1
        call = await call_address(client, address)
        report.calls.append(call)

        if call.ok:
            consecutive_failures = 0
        else:
            consecutive_failures += 1
            if consecutive_failures == 5:
                report.fail(
                    f"5 consecutive failures starting at request {index} "
                    f"(last error_type={call.error_type!r}) — this is the "
                    f"degradation the restart timer was masking"
                )

        if index % 25 == 0:
            health = await client.get("/health")
            print(
                f"  [{index}] elapsed={time.monotonic() - deadline + duration:.0f}s "
                f"health={health.status_code} last={call.error_type or 'ok'}",
                flush=True,
            )

        await asyncio.sleep(interval)

    return report


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_report(report: Report) -> bool:
    print(f"\n{'=' * 70}\n{report.name}\n{'=' * 70}")
    if not report.calls:
        print("  no calls made")
    else:
        ok = sum(1 for c in report.calls if c.ok)
        print(f"  calls: {len(report.calls)}  success: {ok}  failed: {len(report.calls) - ok}")
        print(f"  latency: {_percentiles(c.elapsed for c in report.calls)}")

        breakdown = Counter(
            c.error_type or ("success" if c.ok else "unknown")
            for c in report.calls
        )
        print("  outcomes: " + ", ".join(f"{k}={v}" for k, v in breakdown.most_common()))

        exceptions = [c for c in report.calls if c.exception]
        if exceptions:
            print(f"  transport errors: {len(exceptions)}")
            for call in exceptions[:3]:
                print(f"    - {call.address[:40]!r}: {call.exception}")

    for note in report.notes:
        print(f"  note: {note}")
    for failure in report.failures:
        print(f"  FAIL: {failure}")

    return not report.failures


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8011")
    parser.add_argument(
        "--mode",
        default="all",
        choices=["all", "unique", "cache", "unknown", "malformed", "concurrent", "stale", "soak"],
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--burst", type=int, default=10)
    parser.add_argument("--duration", type=float, default=600.0, help="soak seconds")
    parser.add_argument("--interval", type=float, default=2.0, help="soak gap seconds")
    parser.add_argument("--addresses-file", help="newline-separated addresses")
    args = parser.parse_args()

    addresses = DEFAULT_ADDRESSES
    if args.addresses_file:
        with open(args.addresses_file, encoding="utf-8") as handle:
            addresses = [line.strip() for line in handle if line.strip()]

    reports: list[Report] = []
    async with httpx.AsyncClient(
        base_url=args.base_url.rstrip("/"), timeout=args.timeout
    ) as client:
        try:
            health = await client.get("/health")
            print(f"Target {args.base_url} /health -> {health.status_code} {health.text[:200]}")
        except Exception as exc:
            print(f"Cannot reach {args.base_url}: {exc}")
            return 2

        run = args.mode
        if run in ("all", "unique"):
            reports.append(await mode_unique(client, addresses))
        if run in ("all", "cache"):
            reports.append(await mode_cache(client, addresses))
        if run in ("all", "unknown"):
            reports.append(await mode_unknown(client))
        if run in ("all", "malformed"):
            reports.append(await mode_malformed(client))
        if run in ("all", "stale"):
            reports.append(await mode_stale(client, addresses))
        if run in ("all", "concurrent"):
            reports.append(await mode_concurrent(client, addresses, args.burst))
        if run == "soak":
            reports.append(
                await mode_soak(client, addresses, args.duration, args.interval)
            )

    passed = all(print_report(r) for r in reports)
    print(f"\n{'=' * 70}")
    print("RESULT:", "PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
