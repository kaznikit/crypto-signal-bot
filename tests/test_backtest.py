from bot.backtest import run_placeholder_backtest


def test_backtest_result_non_empty() -> None:
    data = [
        {"close": 100.0},
        {"close": 101.0},
        {"close": 99.0},
        {"close": 103.0},
    ]
    result = run_placeholder_backtest(data)
    assert result.trades == 4
