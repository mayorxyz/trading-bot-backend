"""Multi-symbol scanner â€” walks every Bybit spot pair one at a time.

Each cycle: discover tradable {quote} pairs via REST, then for each symbol
(sequentially, however long each takes) seed 1D/4H/1H/15M history into memory
and run the same pipeline + persistence the live runner uses. When the last
symbol is done the scanner sleeps SCANNER_CYCLE_HOURS and starts over.

Unlike runner.py there is no websocket here: every cycle re-fetches fresh
history from REST, so state is rebuilt from closed candles each pass. The only
disk write remains live_state.db (ticks, trades) via live_store.
"""

import os
import sys
import time
import traceback

from tbb.marketdata.ingestion_bybit import fetch_instruments
from tbb.live import runner

# Quote currency filter â€” all Bybit spot includes BTC-quoted and USDC-quoted
# books that most users don't want mixed into results.
QUOTE_COIN = os.environ.get("SCANNER_QUOTE", "USDT").upper()

# Whole-cycle pause after the last symbol finishes.
CYCLE_PAUSE_SECONDS = float(os.environ.get("SCANNER_CYCLE_HOURS", 1)) * 3600

# Execution timeframe each symbol is analysed on (bias stays 1D/4H/1H).
EXECUTION_TF = os.environ.get("SCANNER_TF", "1H").upper()

# Courtesy gap between symbols, keeping REST well inside rate limits even when
# every fetch paginates.
SYMBOL_PAUSE_SECONDS = float(os.environ.get("SCANNER_SYMBOL_PAUSE", 0.5))

DISCOVERY_RETRY_SECONDS = 60


def get_symbols(quote_coin=QUOTE_COIN):
    """Tradable spot pairs quoted in `quote_coin`, alphabetically."""
    instruments = fetch_instruments(category="spot")
    symbols = sorted(
        i["symbol"] for i in instruments
        if i.get("status") == "Trading" and i.get("quote_coin") == quote_coin
    )
    if not symbols:
        raise RuntimeError(f"no Trading spot instruments quoted in {quote_coin}")
    return symbols


def scan_once(symbols, execution_tf=EXECUTION_TF):
    """
    One full pass over `symbols`. A symbol that fails (fetch error, bad data,
    pipeline crash) is logged and skipped; the scan never stops for one pair.
    Returns (analysed, failed) counts.
    """
    analysed = failed = 0
    for n, symbol in enumerate(symbols, 1):
        t0 = time.perf_counter()
        try:
            df_by_tf = runner.seed_history(symbol)
            runner.run_analysis(df_by_tf, execution_tf, symbol=symbol)
            analysed += 1
        except Exception:
            traceback.print_exc()
            failed += 1
            print(f"[scan] {symbol}: FAILED ({failed} so far); scan continues")
        took = time.perf_counter() - t0
        print(f"[scan] {n}/{len(symbols)} {symbol} finished in {took:.1f}s")
        if n < len(symbols):
            time.sleep(SYMBOL_PAUSE_SECONDS)
    return analysed, failed


def main():
    # Alert blocks contain emoji; make stdout survive a cp1252 console.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(f"scanner: quote={QUOTE_COIN}, tf={EXECUTION_TF}, "
          f"cycle pause={CYCLE_PAUSE_SECONDS / 3600:.2f}h, "
          f"symbol gap={SYMBOL_PAUSE_SECONDS}s. "
          f"In-memory data only; live_store retention="
          f"{runner.live_store.LIVE_RETENTION_HOURS}h.")
    while True:
        try:
            symbols = get_symbols()
        except Exception:
            # Discovery is one REST call; a blip here should cost a minute,
            # not an hour of dead air.
            traceback.print_exc()
            print(f"[scanner] symbol discovery failed; retrying in "
                  f"{DISCOVERY_RETRY_SECONDS}s")
            time.sleep(DISCOVERY_RETRY_SECONDS)
            continue

        cycle_t0 = time.perf_counter()
        print(f"[scanner] === cycle start: {len(symbols)} {QUOTE_COIN} pairs ===")
        analysed, failed = scan_once(symbols)
        elapsed_min = (time.perf_counter() - cycle_t0) / 60
        print(f"[scanner] === cycle done: {analysed} analysed, {failed} failed, "
              f"{elapsed_min:.1f} min elapsed; sleeping "
              f"{CYCLE_PAUSE_SECONDS / 3600:.2f}h ===")
        time.sleep(CYCLE_PAUSE_SECONDS)


if __name__ == "__main__":
    main()
