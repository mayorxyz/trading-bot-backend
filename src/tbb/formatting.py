"""
formatting.py â€” formats analyze_pair_with_bias() / POST /predict output
into a clean table for chat/CLI/dashboard display.
"""


def format_signal_alert(result: dict) -> str:
    """Compact alert block for chat/console â the shape a fired signal prints in."""
    if "skipped" in result:
        return f"SKIPPED Ã¢ {result['skipped']}"

    rr = result.get("rr")
    rr_text = f"{float(rr):.2f}" if rr is not None else "-"
    direction = str(result["direction"]).upper()

    return (
        "\U0001F6A8New Signal Alert\U0001F6A8\n"
        "\n"
        f"{result['pair']} ({direction})\n"
        "\n"
        f"Entry: {result['entry']}/Market order (enter at current price)\n"
        "\n"
        f"SL: {result['sl']}\n"
        "\n"
        f"TP: {result['tp']}\n"
        "\n"
        f"TF: {result['timeframe']} | R:R: {rr_text} | "
        f"Confluence: {result['confluence_score']}/100 | "
        f"Confidence: {result['confidence']}"
    )


def format_signal_table(result: dict) -> str:
    if "skipped" in result:
        return f"SKIPPED â€” {result['skipped']}"

    breakdown = result.get("confluence_breakdown", {})
    breakdown_lines = "\n".join(
        f"  {k:<22} {v:>5}" for k, v in breakdown.items()
    )

    return f"""
{result['pair']} Â· {result['timeframe']} Â· {result['direction']}
----------------------------------------
Entry        {result['entry']}
Stop Loss    {result['sl']}   ({result['sl_method']})
Take Profit  {result['tp']}   ({result['tp_method']})
R:R          {result['rr']}
----------------------------------------
Confluence   {result['confluence_score']}/100  [{result['confidence']}]
{breakdown_lines}
----------------------------------------
Swings: {result['swings_found']}  SR: {result['sr_levels_found']}  Zones: {result['consolidation_zones']}
Breakouts: SR={result['sr_breakouts']} Cons={result['consolidation_breakouts']}
Wick rejections: {result['wick_rejections_at_level']}  Vol spikes: {result['volume_spikes']}
""".strip()


def format_signal_dict(result: dict) -> dict:
    """Structured version for API/frontend consumption (not print)."""
    if "skipped" in result:
        return {"status": "skipped", "reason": result["skipped"]}

    return {
        "status": "signal",
        "pair": result["pair"],
        "timeframe": result["timeframe"],
        "direction": result["direction"],
        "trade": {
            "entry": result["entry"],
            "sl": result["sl"],
            "sl_method": result["sl_method"],
            "tp": result["tp"],
            "tp_method": result["tp_method"],
            "rr": result["rr"],
        },
        "confidence": {
            "score": result["confluence_score"],
            "label": result["confidence"],
            "breakdown": result["confluence_breakdown"],
        },
        "context": {
            "swings": result["swings_found"],
            "sr_levels": result["sr_levels_found"],
            "consolidation_zones": result["consolidation_zones"],
            "sr_breakouts": result["sr_breakouts"],
            "consolidation_breakouts": result["consolidation_breakouts"],
            "wick_rejections": result["wick_rejections_at_level"],
            "volume_spikes": result["volume_spikes"],
            "volume_divergences": result["volume_divergences"],
        },
    }
