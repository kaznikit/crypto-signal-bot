from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from bot.analyzer.improvements import (
    liquidity_grab_filter,
    ote_overlaps_ob_or_fvg,
    quality_score,
    volume_expansion_filter,
)
from bot.analyzer.setup_machine import SetupEvent
from bot.config import LiberalConfig, StrategyFeaturesConfig
from bot.market.pivots import (
    detect_pivots_htf,
    extract_structure_breaks_htf,
    latest_structure_break,
)
from bot.storage.models import Setup


@dataclass(frozen=True, slots=True)
class GateResult:
    ok: bool
    score: int
    reason: str


def continuation_htf_aligned_with_4h(
    df_4h: pd.DataFrame,
    continuation_direction: str,
    swing_size: int = 15,
) -> bool:
    """Импульс на 4H в ту же сторону, что и continuation-сетап на LTF.

    Pine-style: смотрим на последний BOS/CHoCH на 4H. Если он в том же
    направлении, что и continuation — alignment ok. Это проще чем мерить
    «направление последнего impulse leg» — Pine BOS уже даёт чистый сигнал
    «куда сейчас идёт структура».
    """
    pivots = detect_pivots_htf(df_4h, swing_size=swing_size, use_close=True, impulse_lock=True)
    if not pivots:
        return False
    breaks = extract_structure_breaks_htf(
        df_4h, swing_size=swing_size, use_close=True, impulse_lock=True
    )
    last_break = latest_structure_break(breaks)
    if last_break is None:
        return False
    return last_break.direction == continuation_direction


def evaluate_reversal_prepare_detailed(
    df: pd.DataFrame,
    choch_direction: str,
    setup: Setup,
    event: SetupEvent,
    features: StrategyFeaturesConfig,
) -> GateResult:
    if features.require_liquidity_grab_reversal and not liquidity_grab_filter(df, choch_direction):
        return GateResult(ok=False, score=0, reason="reversal_liquidity_grab_missing")

    ote_low = float(event.payload["ote_low"])
    ote_high = float(event.payload["ote_high"])
    need_overlap = features.require_ob_or_fvg_in_ote or features.quality_score_enabled
    overlap = (
        ote_overlaps_ob_or_fvg(
            df,
            ote_low=ote_low,
            ote_high=ote_high,
            swing_length=features.swing_length_ob_fvg,
        )
        if need_overlap
        else False
    )
    if features.require_ob_or_fvg_in_ote and not overlap:
        return GateResult(ok=False, score=0, reason="reversal_ote_no_ob_fvg_overlap")

    if not features.quality_score_enabled:
        return GateResult(ok=True, score=0, reason="passed_quality_disabled")

    has_liq = liquidity_grab_filter(df, choch_direction)
    has_vol = volume_expansion_filter(df) if features.volume_expansion_in_score else False
    score = quality_score(
        has_liquidity_grab=has_liq,
        has_volume_expansion=has_vol,
        rr=None,
        htf_alignment=None,
        in_ob_or_fvg=overlap,
    )
    event.payload.update(
        {
            "has_liquidity_grab": has_liq,
            "has_volume_expansion": has_vol,
            "htf_alignment": None,
            "in_ob_or_fvg": overlap,
            "quality_score": score,
        }
    )
    if features.quality_score_filter_enabled and score < features.min_quality_score:
        return GateResult(ok=False, score=score, reason="reversal_quality_score_below_threshold")
    return GateResult(ok=True, score=score, reason="passed")


def evaluate_reversal_prepare(
    df: pd.DataFrame,
    choch_direction: str,
    setup: Setup,
    event: SetupEvent,
    features: StrategyFeaturesConfig,
) -> tuple[bool, int]:
    result = evaluate_reversal_prepare_detailed(
        df=df,
        choch_direction=choch_direction,
        setup=setup,
        event=event,
        features=features,
    )
    return result.ok, result.score


def evaluate_continuation_prepare_detailed(
    df_htf: pd.DataFrame,
    setup: Setup,
    event: SetupEvent,
    features: StrategyFeaturesConfig,
    df_4h: pd.DataFrame | None,
) -> GateResult:
    direction = str(event.payload.get("direction", setup.direction))

    htf_alignment: bool | None = None
    if features.continuation_require_4h_alignment:
        if df_4h is None:
            return GateResult(ok=False, score=0, reason="continuation_4h_missing")
        htf_alignment = continuation_htf_aligned_with_4h(df_4h, direction)
        if htf_alignment is not True:
            return GateResult(ok=False, score=0, reason="continuation_4h_misaligned")

    ote_low = float(event.payload["ote_low"])
    ote_high = float(event.payload["ote_high"])
    need_overlap = features.require_ob_or_fvg_in_ote or features.quality_score_enabled
    overlap = (
        ote_overlaps_ob_or_fvg(
            df_htf,
            ote_low=ote_low,
            ote_high=ote_high,
            swing_length=features.swing_length_ob_fvg,
        )
        if need_overlap
        else False
    )
    if features.require_ob_or_fvg_in_ote and not overlap:
        return GateResult(ok=False, score=0, reason="continuation_ote_no_ob_fvg_overlap")

    if not features.quality_score_enabled:
        return GateResult(ok=True, score=0, reason="passed_quality_disabled")

    has_liq = liquidity_grab_filter(df_htf, direction)
    has_vol = volume_expansion_filter(df_htf) if features.volume_expansion_in_score else False
    score = quality_score(
        has_liquidity_grab=has_liq,
        has_volume_expansion=has_vol,
        rr=None,
        htf_alignment=htf_alignment,
        in_ob_or_fvg=overlap,
    )
    event.payload.update(
        {
            "has_liquidity_grab": has_liq,
            "has_volume_expansion": has_vol,
            "htf_alignment": htf_alignment,
            "in_ob_or_fvg": overlap,
            "quality_score": score,
        }
    )
    if features.quality_score_filter_enabled and score < features.min_quality_score:
        return GateResult(
            ok=False,
            score=score,
            reason="continuation_quality_score_below_threshold",
        )
    return GateResult(ok=True, score=score, reason="passed")


def evaluate_reversal_prepare_liberal(
    df: pd.DataFrame,
    choch_direction: str,
    setup: Setup,
    event: SetupEvent,
    features: StrategyFeaturesConfig,
    liberal: LiberalConfig,
) -> GateResult:
    relaxed = features.model_copy(
        update={
            "min_quality_score": liberal.min_quality_score,
            "require_ob_or_fvg_in_ote": False,
        }
    )
    return evaluate_reversal_prepare_detailed(
        df=df,
        choch_direction=choch_direction,
        setup=setup,
        event=event,
        features=relaxed,
    )


def evaluate_continuation_prepare_liberal(
    df_htf: pd.DataFrame,
    setup: Setup,
    event: SetupEvent,
    features: StrategyFeaturesConfig,
    df_4h: pd.DataFrame | None,
    liberal: LiberalConfig,
) -> GateResult:
    relaxed = features.model_copy(
        update={
            "min_quality_score": liberal.min_quality_score,
            "require_ob_or_fvg_in_ote": False,
        }
    )
    return evaluate_continuation_prepare_detailed(
        df_htf=df_htf,
        setup=setup,
        event=event,
        features=relaxed,
        df_4h=df_4h,
    )


def evaluate_continuation_prepare(
    df_htf: pd.DataFrame,
    setup: Setup,
    event: SetupEvent,
    features: StrategyFeaturesConfig,
    df_4h: pd.DataFrame | None,
) -> tuple[bool, int]:
    result = evaluate_continuation_prepare_detailed(
        df_htf=df_htf,
        setup=setup,
        event=event,
        features=features,
        df_4h=df_4h,
    )
    return result.ok, result.score
