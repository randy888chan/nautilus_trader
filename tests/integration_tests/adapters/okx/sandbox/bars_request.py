#!/usr/bin/env python3
# -------------------------------------------------------------------------------------------------
#  Copyright (C) 2015-2025 Nautech Systems Pty Ltd. All rights reserved.
#  https://nautechsystems.io
#
#  Licensed under the GNU Lesser General Public License Version 3.0 (the "License");
#  You may not use this file except in compliance with the License.
#  You may obtain a copy of the License at https://www.gnu.org/licenses/lgpl-3.0.en.html
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
# -------------------------------------------------------------------------------------------------
"""
Sandbox script for testing OKX pagination fixes.

This script demonstrates the fixed pagination behavior that resolves the
"50-page safeguard tripped" error that previously occurred with Forward/Latest modes.

## 🧑‍🎓 Root Cause (Explain it like I'm 15)

## 🧑‍🎓 Root Cause (Explain it like I'm 15)

Picture an **API pagination loop** as a relay race:

| Problem Area | What Went Wrong | Impact |
| ------------ | --------------- | ------ |
| **Direction** | Used "before" when needing "after" | Same starting point every lap |
| **Position** | Timestamp +1µs rounded to milliseconds | Baton never moved forward |
| **Counting** | Empty laps counted as progress | Safety limit hit, hiding real issue |

**The Result**: 50 empty laps → safeguard triggered → real bug masked

## 🔧 The Fix

1. **Match the API's granularity** - Do math in milliseconds (OKX's unit)
2. **Use proper directional parameter** - `after_ms` for Forward, `before_ms` for Backward
3. **Validate before counting** - Only count productive pages

## 🧪 Test Results

With the fix applied, pagination now works correctly:
- ✅ 150+ bar requests succeed (multi-page pagination)
- ✅ Cursor advances monotonically: 1754401465783 → 1754401466345 → 1754401466899
- ✅ No more "50-page safeguard tripped" errors
- ✅ No artificial 100-bar ceiling

"""

from __future__ import annotations

import argparse
import asyncio

import pandas as pd

from nautilus_trader.adapters.okx.factories import get_cached_okx_http_client
from nautilus_trader.common.component import Logger
from nautilus_trader.common.component import init_logging
from nautilus_trader.common.enums import LogLevel
from nautilus_trader.core import nautilus_pyo3


MAX_PAGE_SIZE = 100
TZ_UTC = pd.Timestamp(0, tz="UTC").tz


async def paginate_bars(
    http_client,
    *,
    bar_type: nautilus_pyo3.BarType,
    start: pd.Timestamp | None,
    end: pd.Timestamp | None,
    total_limit: int | None,
    sleep_between: float = 0.05,
) -> list[nautilus_pyo3.Bar]:
    remaining = total_limit
    cursor = start
    collected: list = []
    while True:
        batch_limit = MAX_PAGE_SIZE if remaining is None else min(MAX_PAGE_SIZE, remaining)
        batch = await http_client.request_bars(
            bar_type=bar_type,
            start=cursor,
            end=end,
            limit=batch_limit,
        )
        if not batch:
            break
        collected.extend(batch)
        if remaining is not None:
            remaining -= len(batch)
            if remaining <= 0:
                break
        cursor = pd.Timestamp(batch[-1].ts_event, tz=TZ_UTC) + pd.Timedelta("1us")
        if end is not None and cursor >= end:
            break
        await asyncio.sleep(sleep_between)
    return collected


def assert_chronological(bars: list):
    """
    Assert that bars are in chronological order and detect specific issues.
    """
    if not bars:
        return

    ts = [b.ts_event for b in bars]
    series = pd.Series(ts)

    if not series.is_monotonic_increasing:
        # Find the first non-monotonic pair
        for i in range(1, len(ts)):
            if ts[i] <= ts[i - 1]:
                print(f"❌ Chronological violation at index {i}:")
                print(f"   Bar {i-1}: {pd.Timestamp(ts[i-1], tz='UTC')}")
                print(f"   Bar {i}:   {pd.Timestamp(ts[i], tz='UTC')}")
                print(f"   Difference: {ts[i] - ts[i-1]} ns")
                break

        # Check for duplicates
        duplicates = series.duplicated()
        if duplicates.any():
            dup_indices = duplicates[duplicates].index.tolist()
            print(f"⚠️  Found {len(dup_indices)} duplicate timestamps at indices: {dup_indices}")

        raise AssertionError("Bars are not time-ascending")

    # Check for reasonable time gaps (should be ~1 minute for 1-minute bars)
    if len(ts) > 1:
        gaps = pd.Series(ts).diff().dropna()
        expected_gap_ns = 60 * 1_000_000_000  # 1 minute in nanoseconds
        unusual_gaps = gaps[(gaps < expected_gap_ns * 0.8) | (gaps > expected_gap_ns * 1.5)]

        if len(unusual_gaps) > 0:
            print(f"⚠️  Found {len(unusual_gaps)} unusual time gaps (expected ~60s):")
            for idx, gap_ns in unusual_gaps.head(3).items():
                gap_seconds = gap_ns / 1_000_000_000
                print(f"   Gap at index {idx}: {gap_seconds:.1f}s")

    print(f"✅ Chronological check passed for {len(bars)} bars")


async def quick_tests(http_client, bar_type: nautilus_pyo3.BarType, logger: Logger):
    logger.info("\n=== QUICK TESTS ===")
    logger.info("[Quick-1] Fetch latest 5 bars")
    latest_bars = await http_client.request_bars(bar_type=bar_type, limit=5)
    logger.info(f"    ↳ received {len(latest_bars)} bar(s)")
    assert 0 < len(latest_bars) <= 5
    assert_chronological(latest_bars)
    start_time = pd.Timestamp.utcnow().tz_convert("UTC") - pd.Timedelta(hours=1)
    logger.info("[Quick-2] Fetch 10 bars from fixed start_time")
    logger.info(f"    ↳ start={start_time}")
    fixed_bars = await http_client.request_bars(
        bar_type=bar_type,
        start=start_time,
        limit=10,
    )
    logger.info(f"    ↳ received {len(fixed_bars)} bar(s)")
    assert 0 < len(fixed_bars) <= 10
    assert_chronological(fixed_bars)


async def limit_tests(http_client, bar_type: nautilus_pyo3.BarType, logger: Logger):
    logger.info("\n=== LIMIT BEHAVIOR TESTS ===")

    # Test various limits to check the 100-bar claim
    test_cases = [
        (50, "small limit"),
        (100, "exact OKX page size"),
        (150, "exceeds page size"),
        (200, "double page size"),
        (300, "triple page size"),
        (500, "large limit"),
    ]

    start_time = pd.Timestamp.utcnow().tz_convert("UTC") - pd.Timedelta(hours=8)

    for limit, description in test_cases:
        logger.info(f"[Limit-Test] Testing limit={limit} ({description})")
        bars = await http_client.request_bars(
            bar_type=bar_type,
            start=start_time,
            limit=limit,
        )
        logger.info(f"    ↳ requested {limit}, received {len(bars)} bar(s)")

        # Check if we actually get more than 100 bars
        if limit > 100 and len(bars) <= 100:
            logger.warning(
                f"    ⚠️  POTENTIAL ISSUE: Requested {limit} but only got {len(bars)} bars",
            )

        assert (
            len(bars) <= limit
        ), f"Received more bars ({len(bars)}) than requested limit ({limit})"
        assert_chronological(bars)

        # Log time span of received data
        if bars:
            first_ts = pd.Timestamp(bars[0].ts_event, tz="UTC")
            last_ts = pd.Timestamp(bars[-1].ts_event, tz="UTC")
            time_span = last_ts - first_ts
            logger.info(f"    ↳ time span: {time_span} (from {first_ts} to {last_ts})")


async def edge_case_tests(http_client, bar_type: nautilus_pyo3.BarType, logger: Logger):
    logger.info("\n=== EDGE-CASE TESTS ===")
    logger.info("[Edge-1] Pagination with total_limit=300")
    start = pd.Timestamp.utcnow().tz_convert("UTC") - pd.Timedelta(hours=6)
    paginated = await paginate_bars(
        http_client,
        bar_type=bar_type,
        start=start,
        end=None,
        total_limit=300,
    )
    logger.info(f"    ↳ received {len(paginated)} bar(s)")
    assert len(paginated) == 300
    assert_chronological(paginated)
    logger.info("[Edge-2] Future window should raise ValueError")
    future_start = pd.Timestamp.utcnow().tz_convert("UTC") + pd.Timedelta(days=1)
    future_end = future_start + pd.Timedelta(minutes=10)
    try:
        bars_future = await http_client.request_bars(
            bar_type=bar_type,
            start=future_start,
            end=future_end,
            limit=5,
        )
        # If we get here, the adapter allows future windows (older behavior)
        logger.info(f"    ↳ returned {len(bars_future)} bar(s)")
        assert not bars_future, "Future window should return empty list"
    except ValueError as exc:
        # New adapter behavior - raises ValueError for future windows
        assert "future" in str(exc).lower(), f"Unexpected ValueError: {exc}"
        logger.info(f"    ↳ correctly raised ValueError: {exc}")
    logger.info("[Edge-3] Pre-listing window should be empty")
    pre_start = pd.Timestamp("2015-01-01T00:00:00Z")
    pre_end = pre_start + pd.Timedelta(minutes=30)
    bars_pre = await http_client.request_bars(
        bar_type=bar_type,
        start=pre_start,
        end=pre_end,
        limit=50,
    )
    logger.info(f"    ↳ returned {len(bars_pre)} bar(s)")
    assert not bars_pre
    logger.info("[Edge-4] Reversed window must raise")
    try:
        await http_client.request_bars(
            bar_type=bar_type,
            start=pre_end,
            end=pre_start,
            limit=5,
        )
    except ValueError as exc:
        logger.info(f"    ↳ correctly raised ValueError: {exc}")
    else:
        raise AssertionError("start > end did NOT raise")
    logger.info("[Edge-5] Invalid instrument must raise")
    wrong_bar_type = nautilus_pyo3.BarType.from_str(
        "BTC-USD-SWAP.OKX-1-MINUTE-SETTLEMENT-EXTERNAL",
    )
    try:
        await http_client.request_bars(bar_type=wrong_bar_type, limit=1)
    except Exception as exc:
        logger.info(f"    ↳ correctly raised: {exc}")
    else:
        raise AssertionError("Invalid instrument did NOT raise")
    logger.info("✅  Edge-case suite completed successfully")


async def pagination_demo(http_client, bar_type: nautilus_pyo3.BarType, logger: Logger):
    """
    Demonstrate the pagination fix in action.
    """
    logger.info("\n=== PAGINATION FIX DEMONSTRATION ===")
    logger.info("This test shows the fixed pagination behavior that resolves")
    logger.info("the '50-page safeguard tripped' error from Forward/Latest modes.")

    logger.info("\n[Demo-1] Multi-page request (173 bars) - should succeed")
    start_time = pd.Timestamp.utcnow().tz_convert("UTC") - pd.Timedelta(hours=3)
    bars = await http_client.request_bars(
        bar_type=bar_type,
        start=start_time,
        limit=173,  # Requires 2+ pages (100 bars per page)
    )
    logger.info(f"    ↳ requested 173, received {len(bars)} bar(s)")
    logger.info(f"    ↳ pages required: {(len(bars) + 99) // 100}")

    # Note: If this assertion fails with "got 1" instead of 173, it indicates
    # the Rust pagination code is using wrong direction parameters (after vs before)
    # See: OKX quirk where 'after' means older data, 'before' means newer data
    assert (
        len(bars) == 173
    ), f"Expected 173 bars, got {len(bars)} - check Rust pagination direction parameters"
    assert_chronological(bars)

    logger.info("\n[Demo-2] Large multi-page request (300 bars) - should succeed")
    # Use 5-hour window to ensure we can actually get 300 one-minute bars
    # 5 hours = 300 minutes = 300 potential bars (matching our limit)
    start_time_large = pd.Timestamp.utcnow().tz_convert("UTC") - pd.Timedelta(hours=5)
    bars_large = await http_client.request_bars(
        bar_type=bar_type,
        start=start_time_large,
        limit=300,  # Requires 3 pages
    )
    logger.info(f"    ↳ requested 300, received {len(bars_large)} bar(s)")
    logger.info(f"    ↳ pages required: {(len(bars_large) + 99) // 100}")
    assert len(bars_large) == 300, f"Expected 300 bars, got {len(bars_large)}"
    assert_chronological(bars_large)

    logger.info("\n[Demo-3] Check for monotonic cursor advancement")
    # The debug output should show increasing cursor values like:
    # DEBUG: Forward/Latest mode - after_ms=1754401465783
    # DEBUG: Forward/Latest mode - after_ms=1754401466345  (+562ms)
    # DEBUG: Forward/Latest mode - after_ms=1754401466899  (+554ms)
    logger.info("    ↳ Check the debug output above for monotonic cursor progression")
    logger.info("    ↳ You should see after_ms values that strictly increase")

    logger.info("\n✅ Pagination fix demonstration completed successfully!")
    logger.info("   • Multi-page requests work without hitting safeguards")
    logger.info("   • No artificial 100-bar ceiling")
    logger.info("   • Cursors advance monotonically")


async def main(args: argparse.Namespace):
    nautilus_pyo3.init_tracing()
    _guard = init_logging(level_stdout=LogLevel.TRACE)
    logger = Logger("okx-sandbox")
    http_client = get_cached_okx_http_client()

    # Cache instruments for the specified instrument type
    instrument_type = nautilus_pyo3.OKXInstrumentType.SWAP
    logger.info(f"Requesting instruments for type: {instrument_type}")
    instruments = await http_client.request_instruments(instrument_type)
    logger.info(f"Retrieved {len(instruments)} instruments")

    for inst in instruments:
        http_client.add_instrument(inst)
        logger.debug(f"Cached instrument: {inst.id}")

    logger.info(f"Instrument cache populated with {len(instruments)} instruments")

    # Parse the bar type and verify the instrument is cached
    bar_type = nautilus_pyo3.BarType.from_str(args.bar_type)
    logger.info(f"Using bar type: {bar_type}")

    # Verify the instrument is in cache
    cached_symbols = http_client.get_cached_symbols()
    instrument_symbol = bar_type.instrument_id.symbol.value
    if instrument_symbol not in cached_symbols:
        logger.error(f"Instrument {instrument_symbol} not found in cache!")
        logger.info(f"Available cached symbols: {cached_symbols[:10]}...")  # Show first 10
        raise ValueError(f"Instrument {instrument_symbol} not available")
    else:
        logger.info(f"✅ Instrument {instrument_symbol} found in cache")

    # Run test suites based on arguments
    if args.pagination or not (args.quick or args.edge or args.limits or args.pagination):
        await pagination_demo(http_client, bar_type, logger)
    if args.limits or not (args.quick or args.edge or args.limits or args.pagination):
        await limit_tests(http_client, bar_type, logger)
    if args.quick or not (args.quick or args.edge or args.limits or args.pagination):
        await quick_tests(http_client, bar_type, logger)
    if args.edge or not (args.quick or args.edge or args.limits or args.pagination):
        await edge_case_tests(http_client, bar_type, logger)

    logger.info("\n🎉  All requested test suites passed!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--bar-type",
        default="BTC-USD-SWAP.OKX-1-MINUTE-LAST-EXTERNAL",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--quick", action="store_true", help="Run basic functionality tests")
    group.add_argument("--edge", action="store_true", help="Run edge case tests")
    group.add_argument("--limits", action="store_true", help="Test various limit scenarios")
    group.add_argument("--pagination", action="store_true", help="Demonstrate the pagination fix")
    args = parser.parse_args()
    asyncio.run(main(args))
