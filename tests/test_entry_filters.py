from bot.analyzer.filters import close_beyond_level, finalize_entry_levels


def test_finalize_entry_levels_skips_when_disabled() -> None:
    levels, reject = finalize_entry_levels(
        entry=100.0,
        direction="LONG",
        invalidation_price=90.0,
        compute_sl_tp=False,
        min_rr=10.0,
    )
    assert levels is None
    assert reject is None


def test_finalize_entry_levels_computes_and_checks_rr() -> None:
    levels, reject = finalize_entry_levels(
        entry=100.0,
        direction="LONG",
        invalidation_price=90.0,
        compute_sl_tp=True,
        min_rr=1.5,
    )
    assert reject is None
    assert levels is not None
    assert levels["sl"] == 90.0
    assert levels["tp"] == 120.0


def test_finalize_entry_levels_rejects_zero_risk() -> None:
    levels, reject = finalize_entry_levels(
        entry=100.0,
        direction="LONG",
        invalidation_price=100.0,
        compute_sl_tp=True,
        min_rr=1.0,
    )
    assert levels is None
    assert reject == "zero_risk"


def test_close_beyond_level_long() -> None:
    assert close_beyond_level(101.0, 100.0, "LONG") is True
    assert close_beyond_level(100.0, 100.0, "LONG") is False


def test_finalize_entry_levels_rejects_low_rr() -> None:
    levels, reject = finalize_entry_levels(
        entry=100.0,
        direction="LONG",
        invalidation_price=99.99,
        compute_sl_tp=True,
        min_rr=3.0,
    )
    assert levels is None
    assert reject == "rr_below_min"
