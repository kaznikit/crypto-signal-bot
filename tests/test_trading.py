from bot.trading import Position


def _position(**overrides: float) -> Position:
    values = {
        "setup_id": "setup-1",
        "symbol": "BTCUSDT",
        "setup_type": "CONTINUATION",
        "direction": "LONG",
        "tf": "5M",
        "stop_price": 90.0,
        "tp_price": 120.0,
    }
    values.update(overrides)
    return Position(**values)


def test_first_entry_stop_is_minus_point_six_r() -> None:
    position = _position()
    position.add_entry(
        entry_type="first_entry",
        entry_time=1,
        entry_price=100.0,
        risk_fraction=0.6,
    )

    result = position.close(exit_time=2, exit_price=90.0, exit_reason="sl")

    assert result.realized_r == -0.6


def test_reentry_keeps_total_worst_case_at_minus_one_r() -> None:
    position = _position()
    position.add_entry(
        entry_type="first_entry",
        entry_time=1,
        entry_price=100.0,
        risk_fraction=0.6,
    )
    position.add_entry(
        entry_type="reentry",
        entry_time=2,
        entry_price=95.0,
        risk_fraction=0.4,
    )

    result = position.close(exit_time=3, exit_price=90.0, exit_reason="sl")

    assert result.realized_r == -1.0


def test_position_tracks_costs_mae_and_mfe() -> None:
    position = _position(fee_rate=0.001, slippage_rate=0.001)
    position.add_entry(
        entry_type="first_entry",
        entry_time=1,
        entry_price=100.0,
        risk_fraction=1.0,
    )
    position.update_excursions(high=115.0, low=95.0)

    result = position.close(exit_time=2, exit_price=120.0, exit_reason="tp")

    assert result.realized_r < result.gross_r
    assert result.fees > 0
    assert result.slippage > 0
    assert result.mae_r < 0
    assert result.mfe_r > 0


def test_position_applies_configured_funding_cost() -> None:
    position = _position(funding_rate=0.001)
    position.add_entry(
        entry_type="first_entry",
        entry_time=0,
        entry_price=100.0,
        risk_fraction=1.0,
    )

    result = position.close(
        exit_time=8 * 60 * 60 * 1000,
        exit_price=120.0,
        exit_reason="tp",
    )

    assert result.funding > 0
    assert result.realized_r < result.gross_r
