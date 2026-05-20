from __future__ import annotations

import pandas as pd
from smartmoneyconcepts import smc


def liquidity_grab_filter(df: pd.DataFrame, direction: str, lookback: int = 30) -> bool:
    sample = df.tail(lookback + 2)
    if len(sample) < lookback + 2:
        return False
    prev = sample.iloc[-2]
    older = sample.iloc[:-2]
    if direction == "SHORT":
        return float(prev["high"]) > float(older["high"].max())
    return float(prev["low"]) < float(older["low"].min())


def volume_expansion_filter(df: pd.DataFrame, multiplier: float = 1.2) -> bool:
    if len(df) < 21:
        return False
    vol = df["volume"]
    baseline = vol.tail(21).iloc[:-1].mean()
    return float(vol.iloc[-1]) > float(baseline) * multiplier


def quality_score(
    has_liquidity_grab: bool,
    has_volume_expansion: bool,
    rr: float,
    htf_alignment: bool,
    in_ob_or_fvg: bool = False,
) -> int:
    score = 0
    if has_liquidity_grab:
        score += 20
    if has_volume_expansion:
        score += 20
    if rr >= 1.5:
        score += 20
    if rr >= 2.0:
        score += 10
    if htf_alignment:
        score += 20
    if in_ob_or_fvg:
        score += 10
    return min(score, 100)


def _intervals_overlap(a_lo: float, a_hi: float, b_lo: float, b_hi: float) -> bool:
    return not (a_hi < b_lo or a_lo > b_hi)


def ote_overlaps_ob_or_fvg(
    df: pd.DataFrame,
    ote_low: float,
    ote_high: float,
    swing_length: int = 10,
    tail_rows: int = 80,
) -> bool:
    """True если в хвосте данных есть OB или FVG, пересекающие OTE-зону."""
    if len(df) < swing_length * 3:
        return False
    z_lo, z_hi = min(ote_low, ote_high), max(ote_low, ote_high)
    try:
        sw = smc.swing_highs_lows(df, swing_length=swing_length)
        ob_df = smc.ob(df, sw)
        for _, row in ob_df.tail(tail_rows).iterrows():
            ob = row.get("OB")
            top = row.get("Top")
            bot = row.get("Bottom")
            if pd.notna(ob) and pd.notna(top) and pd.notna(bot):
                top_v, bot_v = float(top), float(bot)
                o_lo, o_hi = min(bot_v, top_v), max(bot_v, top_v)
                if _intervals_overlap(o_lo, o_hi, z_lo, z_hi):
                    return True
        fvg_df = smc.fvg(df)
        for _, row in fvg_df.tail(tail_rows).iterrows():
            fvg = row.get("FVG")
            top = row.get("Top")
            bot = row.get("Bottom")
            if pd.notna(fvg) and pd.notna(top) and pd.notna(bot):
                top_v, bot_v = float(top), float(bot)
                f_lo, f_hi = min(bot_v, top_v), max(bot_v, top_v)
                if _intervals_overlap(f_lo, f_hi, z_lo, z_hi):
                    return True
    except (KeyError, TypeError, ValueError):
        return False
    return False
