import pandas as pd

from bot.analyzer.setup_machine import SetupEvent, build_setup
from bot.analyzer.strategy_gates import (
    evaluate_continuation_prepare_detailed,
    evaluate_reversal_prepare,
    evaluate_reversal_prepare_detailed,
)
from bot.config import StrategyFeaturesConfig
from bot.storage.models import SetupType


def _df(rows: int = 120) -> pd.DataFrame:
    data = []
    for i in range(rows):
        o = 100.0 + i * 0.01
        c = o + 0.02
        data.append(
            {
                "open_time": i,
                "open": o,
                "high": c + 0.1,
                "low": o - 0.1,
                "close": c,
                "volume": 1000.0 + i,
            }
        )
    return pd.DataFrame(data)


def _build_test_setup(setup_type: SetupType, htf: str = "4H"):
    return build_setup(
        setup_id="test-setup",
        symbol="TESTUSDT",
        setup_type=setup_type,
        direction="LONG",
        htf=htf,
        ltf_expected="1H",
        origin_price=101.0,
        ote_low=100.0,
        ote_high=100.5,
        invalidation_price=99.0,
        ttl_hours=24,
    )


def _build_test_event(setup_id: str) -> SetupEvent:
    return SetupEvent(
        kind="PREPARE",
        payload={
            "setup_id": setup_id,
            "direction": "LONG",
            "ote_low": 100.0,
            "ote_high": 100.5,
        },
    )


def test_evaluate_reversal_min_quality_blocks() -> None:
    df = _df()
    setup = _build_test_setup(SetupType.REVERSAL)
    event = _build_test_event(setup.id)
    features = StrategyFeaturesConfig(
        require_liquidity_grab_reversal=False,
        quality_score_enabled=True,
        quality_score_filter_enabled=True,
        min_quality_score=100,
        require_ob_or_fvg_in_ote=False,
    )
    ok, score = evaluate_reversal_prepare(
        df=df,
        choch_direction="LONG",
        setup=setup,
        event=event,
        features=features,
    )
    assert ok is False
    assert score < 100


def test_evaluate_reversal_detailed_reason_for_quality_block() -> None:
    df = _df()
    setup = _build_test_setup(SetupType.REVERSAL)
    event = _build_test_event(setup.id)
    features = StrategyFeaturesConfig(
        require_liquidity_grab_reversal=False,
        quality_score_enabled=True,
        quality_score_filter_enabled=True,
        min_quality_score=100,
        require_ob_or_fvg_in_ote=False,
    )
    result = evaluate_reversal_prepare_detailed(
        df=df,
        choch_direction="LONG",
        setup=setup,
        event=event,
        features=features,
    )
    assert result.ok is False
    assert result.reason == "reversal_quality_score_below_threshold"


def test_continuation_detailed_reason_for_missing_4h_alignment_source() -> None:
    df = _df()
    setup = _build_test_setup(SetupType.CONTINUATION, htf="1H")
    event = _build_test_event(setup.id)
    features = StrategyFeaturesConfig(
        continuation_require_4h_alignment=True,
        quality_score_enabled=False,
    )
    result = evaluate_continuation_prepare_detailed(
        df_htf=df,
        setup=setup,
        event=event,
        features=features,
        df_4h=None,
    )
    assert result.ok is False
    assert result.reason == "continuation_4h_missing"
