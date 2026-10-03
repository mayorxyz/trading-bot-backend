"""Live Bybit signal runner for a caller-supplied symbol or watchlist.

NOTHING IN THIS PROCESS TOUCHES THE DISK except live_state.db. Seed history is
fetched straight into in-memory DataFrames and WS bars are appended in memory;
no CSV is ever written or read here. data/*.csv belongs to the explicit
historical path (ingestion_bybit's CLI, POST /analyze) and is left untouched, so
a live run can never mutate the data a backtest would replay.

live_state.db is a short rolling hand-off buffer, not storage: it exists only
because the frontend cannot read this process's memory. Every tick writes a
snapshot and then purges anything past live_store.LIVE_RETENTION_HOURS.

Nothing about signal/entry/SL/TP computation is changed by that persistence â€”
the pipeline call is the same one as before; state collection reads the same
inputs a second time and stores what it finds.
"""

import argparse
import asyncio
import json
import os
import sys
import traceback

import numpy as np
import pandas as pd
import websockets

from tbb.marketdata.ingestion_bybit import fetch_klines
from tbb.pipeline import analyze_pair_with_bias
from tbb.formatting import format_signal_alert

from tbb.storage import live_store
from tbb import logsetup
from tbb.live import trade_manager
from tbb.backtesting.backtest import classify_skip, MAX_FILL_WAIT_BARS
from tbb.engines.consolidation import find_consolidation_zones
from tbb.analysis.primitives import ema_trend_filter
from tbb.analysis.imbalances import detect_imbalances, mark_tested_imbalances
from tbb.analysis.regime import detect_regime, resolve_topdown_bias
from tbb.indicators.support_resistance import find_sr_levels
from tbb.indicators.zigzag import get_zigzag_swings

STREAM_URL = "wss://stream.bybit.com/v5/public/spot"

# Bybit kline stream interval codes, keyed by our timeframe label. Built from a
# runtime symbol so the subscription topic always matches the running pair.
TF_STREAM_CODE = {"1D": "D", "4H": "240", "1H": "60", "15M": "15"}


def _symbol_list(spec):
    """Normalise a caller-supplied symbol or comma-separated watchlist."""
    if spec is None:
        return []
    if isinstance(spec, str):
        values = [part.strip() for part in spec.split(",")]
    else:
        values = [str(part).strip() for part in spec]
    symbols = [value.upper() for value in values if value]
    if not symbols:
        raise ValueError("no symbols supplied")
    return symbols


def _tf_topic_map(symbol):
    return {tf: f"kline.{code}.{symbol}" for tf, code in TF_STREAM_CODE.items()}

TRIGGER_TFS = {"1H", "15M"}  # analysis runs on close of these TFs
MAX_ROWS = 2000

# How much seed history to pull per timeframe, in days. Capped by MAX_ROWS
# anyway, so this only has to be enough to fill the window the pipeline uses.
SEED_DAYS = {"1D": 180, "4H": 60, "1H": 30, "15M": 7}

OHLCV_COLS = ["open", "high", "low", "close", "volume"]

# Timeframes whose bias feeds the top-down decision (mirrors run_analysis's bias_tfs).
BIAS_TFS = ("1D", "4H", "1H")

# Bars of tail history used by the state SNAPSHOT only â€” never by the signal path.
# detect_regime() looks at imbalances formed in its last 50 bars and only ever
# scans forward from them, so a bounded tail returns the identical regime as full
# history while avoiding detect_imbalances' O(n) scan over thousands of rows.
# Also the window zigzag/S-R/consolidation are snapshotted over, matching the 200
# bars analyze_pair() itself receives.
STATE_WINDOW_BARS = 200


def _normalise(df):
    """Coerce a fetched frame to the exact shape the pipeline expects."""
    if df is None or df.empty:
        return pd.DataFrame(columns=OHLCV_COLS,
                            index=pd.DatetimeIndex([], tz="UTC"))
    df = df.copy()
    for c in OHLCV_COLS:
        if c not in df.columns:
            df[c] = np.nan
    df = df[OHLCV_COLS]
    df.index = pd.to_datetime(df.index, utc=True)
    df = df.sort_index()
    df = df[~df.index.duplicated(keep="last")]
    if len(df) > MAX_ROWS:
        df = df.iloc[-MAX_ROWS:]
    return df


def seed_history(symbol):
    """
    Seed each timeframe from Bybit REST into memory. No CSV is written or read.

    fetch_klines returns a DataFrame and touches no files â€” fetch_and_save_all is
    the writing variant and is deliberately NOT used here (see module docstring).
    A per-timeframe failure degrades to an empty frame rather than killing the
    run; the pipeline already guards on insufficient history.
    """
    symbol = str(symbol).upper()
    topics = _tf_topic_map(symbol)
    df_by_tf = {}
    for tf in topics:
        try:
            df = fetch_klines(symbol, tf, days_back=SEED_DAYS[tf])
        except Exception as exc:
            print(f"[seed] {symbol} {tf} fetch failed ({type(exc).__name__}: {exc}); "
                  f"starting empty and filling from the stream")
            df = None
        df_by_tf[tf] = _normalise(df)
        print(f"[seed] {symbol} {tf}: {len(df_by_tf[tf])} bars in memory")
    return df_by_tf


def append_or_replace_row(df, candle):
    ts = pd.to_datetime(candle["start"], unit="ms", utc=True)
    row = {
        "open": float(candle["open"]),
        "high": float(candle["high"]),
        "low": float(candle["low"]),
        "close": float(candle["close"]),
        "volume": float(candle["volume"]),
    }
    replacement = pd.DataFrame([row], index=[ts])
    df = pd.concat([df, replacement], axis=0)
    df = df[~df.index.duplicated(keep="last")]
    df = df.sort_index()
    if len(df) > MAX_ROWS:
        df = df.iloc[-MAX_ROWS:]
    return df


def _tf_snapshot(df):
    """
    bias + regime for one timeframe. Read-only.

    bias is computed over the FULL frame, not a tail slice, because
    ema_trend_filter derives its flat/steep threshold from ema.std() over
    whatever it is handed â€” a global statistic. Slicing would shift the
    threshold and could report a bias the pipeline never actually used.

    regime is safe to bound (see STATE_WINDOW_BARS).
    """
    if df is None or len(df) < 3:
        return {"bias": None, "regime": None}
    window = df.tail(STATE_WINDOW_BARS)
    bias = ema_trend_filter(df)["bias"].iloc[-1]
    try:
        regime = detect_regime(window, detect_imbalances(window))
    except Exception:
        regime = None
    return {"bias": None if bias is None else str(bias),
            "regime": None if regime is None else str(regime)}


def _zones_and_levels(tf, df):
    """
    Active FVG/imbalance zones, consolidation zones and S/R levels for one
    timeframe, as price ranges with real timestamps.

    zigzag returns POSITIONAL swing indices into the window it was given, so
    consolidation start/end are mapped back through window.index to become
    timestamps the frontend can plot directly.
    """
    zones, levels = [], []
    if df is None or len(df) < 20:
        return zones, levels

    window = df.tail(STATE_WINDOW_BARS)

    imb = detect_imbalances(window)
    if not imb.empty:
        # tested flag is initialised False by detect_imbalances; this fills it in
        # so the frontend can distinguish still-active zones from spent ones.
        try:
            imb = mark_tested_imbalances(window, imb)
        except Exception:
            pass
        for _, r in imb.iterrows():
            zones.append({
                "timeframe": tf, "kind": "fvg", "direction": str(r["type"]),
                "price_low": float(r["zone_low"]), "price_high": float(r["zone_high"]),
                "start_ts": r["c1_idx"], "end_ts": r["c3_idx"],
                "tested": bool(r.get("tested", False)),
            })

    swings = get_zigzag_swings(window["high"], window["low"], window["close"])
    n = len(window.index)
    for z in find_consolidation_zones(swings):
        si, ei = int(z["start_index"]), int(z["end_index"])
        if not (0 <= si < n and 0 <= ei < n):
            continue
        zones.append({
            "timeframe": tf, "kind": "consolidation", "direction": None,
            "price_low": float(z["low"]), "price_high": float(z["high"]),
            "start_ts": window.index[si], "end_ts": window.index[ei],
            "tested": None,
        })

    for lv in find_sr_levels(swings):
        levels.append({
            "timeframe": tf, "price": float(lv["price"]),
            "touches": int(lv["touches"]), "level_type": str(lv["type"]),
        })

    return zones, levels


def collect_state(symbol, execution_tf, ts, df_by_tf, result):
    """
    Build the persistable snapshot for one analysis tick.

    `result` is the pipeline's own return value â€” the skip reason and signal are
    taken from it verbatim, never recomputed, so what is stored is exactly what
    the pipeline decided.
    """
    skipped = result.get("skipped") if isinstance(result, dict) else None

    tf_state = {tf: _tf_snapshot(df_by_tf.get(tf)) for tf in BIAS_TFS}

    # resolve_topdown_bias is a pure function of the per-TF biases, and
    # _tf_snapshot computes each bias the same way per_tf_bias does (last bar of
    # ema_trend_filter over the full frame). So this reproduces the pipeline's own
    # top-down result without re-running the expensive parts of resolve_bias.
    biases = {tf: st["bias"] for tf, st in tf_state.items() if st["bias"] is not None}
    topdown = resolve_topdown_bias(biases) if biases else None

    zones, levels = [], []
    for tf, df in df_by_tf.items():
        z, lv = _zones_and_levels(tf, df)
        zones.extend(z)
        levels.extend(lv)

    return {
        "ts": ts,
        "symbol": symbol,
        "execution_tf": execution_tf,
        "tf_state": tf_state,
        "topdown_bias": topdown["bias"] if topdown else None,
        "aligned_count": topdown["aligned_count"] if topdown else None,
        # Did the top-down bias gate itself pass? Distinct from "a signal fired" â€”
        # signal_json being non-null is what tells you that.
        "tradable": not (skipped or "").startswith("not tradable"),
        "direction": None if skipped else result.get("direction"),
        "skip_reason": classify_skip(skipped) if skipped else None,
        "skip_reason_raw": skipped,
        "signal": None if skipped else result,
        "zones": zones,
        "levels": levels,
    }


def resolve_open_live_trades(symbol, df_by_tf):
    """
    Advance every in-flight live trade through trade_manager.advance() and
    persist fills / TP1 flags / ratcheted stops / resolutions.

    The manager implements the engines' two-step plan (half off at TP1 ->
    breakeven -> chandelier trail); realized_rr on managed rows combines both
    legs so /live/stats reflects what was actually planned.
    """
    changed = []
    for t in live_store.pending_trades(symbol):
        df = df_by_tf.get(t["timeframe"])
        if df is None or df.empty:
            continue
        try:
            m = trade_manager.advance(t, df, max_fill_wait=MAX_FILL_WAIT_BARS)
        except Exception:
            traceback.print_exc()
            continue

        # Persist management state whenever it moved (idempotent between ticks).
        if (m.get("tp1_filled") != bool(t.get("tp1_filled"))) or \
                (m.get("stop_current") is not None and
                 m.get("stop_current") != t.get("stop_current")):
            live_store.update_trade_management(
                t["id"], tp1_filled=m.get("tp1_filled"),
                stop_current=m.get("stop_current"))

        if m["outcome"] == "pending":
            continue
        if m["outcome"] == "no_fill":
            live_store.mark_no_fill(t["id"])
            changed.append((t["id"], "no_fill"))
            continue

        if t["outcome"] == "pending" and m.get("fill_ts") is not None:
            live_store.mark_filled(t["id"], m["fill_ts"])

        if m["outcome"] in ("win", "loss"):
            info = live_store.resolve_trade_managed(
                t["id"], m["outcome"], m["resolved_ts"], m["exit_price"],
                m["realized_rr"], m["pnl"])
            msg = f"{m['outcome']} RR={info['realized_rr']:+.2f}"
            if m["events"]:
                msg += f" ({'; '.join(m['events'])})"
            changed.append((t["id"], msg))
    return changed


def run_analysis(df_by_tf, execution_tf, symbol):
    """Run pipeline using 1D/4H/1H for bias, execution_tf's data for entry/SL-TP."""
    symbol = str(symbol).upper()
    recent = df_by_tf[execution_tf].tail(200)
    opens = recent["open"].to_numpy(dtype=float)
    highs = recent["high"].to_numpy(dtype=float)
    lows = recent["low"].to_numpy(dtype=float)
    closes = recent["close"].to_numpy(dtype=float)
    volumes = recent["volume"].to_numpy(dtype=float)

    # bias always from 1D/4H/1H (top-down); execution TF supplies the OHLCV for entry calc
    bias_tfs = {"1D": df_by_tf["1D"], "4H": df_by_tf["4H"], "1H": df_by_tf["1H"]}

    result = analyze_pair_with_bias(
        pair=symbol,
        timeframe=execution_tf,
        df_by_tf=bias_tfs,
        opens=opens, highs=highs, lows=lows, closes=closes, volumes=volumes,
        db_path=None,  # live owns live_state.db only; signals.db is not the live ledger
    )
    if "skipped" in result:
        print(f"[{execution_tf}] SKIPPED:", result["skipped"])
    else:
        print(format_signal_alert(result))

    # ---- persistence (additive; never feeds back into the decision above) ----
    ts = df_by_tf[execution_tf].index[-1] if len(df_by_tf[execution_tf]) else None
    try:
        snapshot = collect_state(symbol, execution_tf, ts, df_by_tf, result)
        tick_id = live_store.record_tick(snapshot)

        if "skipped" not in result:
            trade_id = live_store.open_trade(
                tick_id=tick_id, symbol=symbol, timeframe=execution_tf,
                direction=result["direction"], opened_at=ts,
                entry_price=result["entry"], stop_price=result["sl"],
                take_profit=result["tp"], planned_rr=result.get("rr"),
                confluence=result.get("confluence_score"),
                notes=f"confidence={result.get('confidence')}",
            )
            print(f"[{execution_tf}] logged live trade id={trade_id} (tick {tick_id})")

        for tid, what in resolve_open_live_trades(symbol, df_by_tf):
            print(f"[{execution_tf}] live trade {tid} -> {what}")

        # Keep the hand-off buffer short. Runs per tick (minutes apart) and only
        # touches indexed columns, so the cost is negligible next to the pipeline.
        purged = live_store.purge_old()
        if purged.get("live_ticks"):
            print(f"[{execution_tf}] purged {purged} "
                  f"(> {live_store.LIVE_RETENTION_HOURS}h old)")
    except Exception:
        log = logsetup.get_logger("live_runner")
        log.exception("state persistence failed; live loop continues")
        print("State persistence failed but the live loop is still running.")

    return result


async def run_live_stream(df_by_tf, symbol):
    topic_map = _tf_topic_map(symbol)
    topic_to_tf = {v: k for k, v in topic_map.items()}
    while True:
        try:
            async with websockets.connect(
                STREAM_URL, ping_interval=20, ping_timeout=10
            ) as ws:
                subscribe_msg = {"op": "subscribe", "args": list(topic_map.values())}
                await ws.send(json.dumps(subscribe_msg))
                async for raw in ws:
                    data = json.loads(raw)
                    topic = data.get("topic")
                    if not topic:
                        continue
                    for candle in data.get("data", []):
                        if not candle.get("confirm"):
                            continue
                        tf = topic_to_tf.get(topic)
                        if tf is None:
                            continue
                        df_by_tf[tf] = append_or_replace_row(df_by_tf[tf], candle)

                        if tf in TRIGGER_TFS:
                            try:
                                run_analysis(df_by_tf, execution_tf=tf, symbol=symbol)
                            except Exception:
                                log = logsetup.get_logger("live_runner")
                                log.exception("analysis cycle failed; live loop continues")
                                print("Analysis cycle failed but live loop is still running.")
        except (websockets.exceptions.ConnectionClosed, OSError, asyncio.TimeoutError) as e:
            print(f"WS connection dropped ({e}). Reconnecting in 5s...")
            await asyncio.sleep(5)


def _cli_symbols(argv=None):
    parser = argparse.ArgumentParser(description="Run the live Bybit runner for one symbol or watchlist.")
    parser.add_argument("--symbol", dest="symbol", help="single symbol to track, e.g. BTCUSDT")
    parser.add_argument("--symbols", dest="symbols", help="comma-separated symbols, e.g. BTCUSDT,ETHUSDT")
    args = parser.parse_args(argv)

    explicit = _symbol_list(args.symbols) if args.symbols else []
    if args.symbol:
        explicit.extend(_symbol_list(args.symbol))
    if not explicit:
        env_symbol = os.environ.get("TBB_SYMBOL") or os.environ.get("LIVE_SYMBOL")
        if env_symbol:
            explicit.extend(_symbol_list(env_symbol))
    if not explicit:
        parser.error("a symbol or --symbols list is required; no default BTCUSDT fallback is used")
    return explicit


def main(argv=None):
    # Signal alerts contain emoji; a cp1252 console would raise mid-print and,
    # in the scanner, kill that symbol's persistence with it.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    symbols = _cli_symbols(argv)
    log = logsetup.get_logger("live_runner")
    print(f"live_runner: seed + stream are IN MEMORY ONLY (no data/*.csv writes). "
          f"live_state.db retention = {live_store.LIVE_RETENTION_HOURS}h. "
          f"symbols={', '.join(symbols)}")
    for idx, symbol in enumerate(symbols, 1):
        if len(symbols) > 1:
            print(f"[startup] starting watchlist symbol {idx}/{len(symbols)}: {symbol}")
        # Trim on startup too: the first tick can be up to one execution bar away.
        print(f"[startup] purge_old -> {live_store.purge_old()}")
        df_by_tf = seed_history(symbol)
        log.info("seeded %s, streaming started (retention=%sh)", symbol, live_store.LIVE_RETENTION_HOURS)
        asyncio.run(run_live_stream(df_by_tf, symbol))


if __name__ == "__main__":
    main()
