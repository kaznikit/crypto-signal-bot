from bot.export_pine import _collect_arrays


def test_collect_arrays_entry_uses_recommended_stop_and_htf_target() -> None:
    arrays = _collect_arrays(
        [
            {
                "kind": "ENTRY",
                "symbol": "HYPEUSDT",
                "htf": "5M",
                "setup_htf": "1H",
                "bar_open_ms": 1_000,
                "entry": 100.0,
                "recommended_stop": 99.0,
                "target_price": 105.0,
                "direction": "LONG",
            }
        ],
        tf="1H",
        since_ms=None,
        include_liberal=False,
        kinds={"ENTRY"},
        symbol="HYPEUSDT",
    )

    assert arrays["kinds"] == ["ENTRY"]
    assert arrays["sls"] == [99.0]
    assert arrays["tps"] == [105.0]
