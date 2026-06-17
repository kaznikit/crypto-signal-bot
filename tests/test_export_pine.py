from bot.export_pine import _collect_arrays, _render_pine


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


def test_collect_arrays_invalidated_after_entry_becomes_stop_loss() -> None:
    arrays = _collect_arrays(
        [
            {
                "kind": "INVALIDATED",
                "symbol": "BTCUSDT",
                "htf": "5M",
                "setup_htf": "1H",
                "bar_open_ms": 1_000,
                "invalidation_price": 90.0,
                "mark_price": 95.0,
                "direction": "LONG",
                "after_entry": True,
            }
        ],
        tf="1H",
        since_ms=None,
        include_liberal=False,
        kinds={"STOP_LOSS"},
        symbol="BTCUSDT",
    )

    assert arrays["kinds"] == ["STOP_LOSS"]
    assert arrays["prices"] == [95.0]


def test_collect_arrays_pre_entry_invalidated_is_not_stop_loss() -> None:
    arrays = _collect_arrays(
        [
            {
                "kind": "INVALIDATED",
                "symbol": "BTCUSDT",
                "htf": "1H",
                "bar_open_ms": 1_000,
                "invalidation_price": 90.0,
                "direction": "LONG",
                "after_entry": False,
            }
        ],
        tf="1H",
        since_ms=None,
        include_liberal=False,
        kinds={"STOP_LOSS"},
        symbol="BTCUSDT",
    )

    assert arrays["kinds"] == []
    assert arrays["prices"] == []


def test_collect_arrays_take_profit_and_stop_loss_use_exit_price() -> None:
    arrays = _collect_arrays(
        [
            {
                "kind": "TAKE_PROFIT",
                "symbol": "BTCUSDT",
                "htf": "5M",
                "setup_htf": "1H",
                "bar_open_ms": 1_000,
                "exit_price": 110.0,
                "direction": "LONG",
            },
            {
                "kind": "STOP_LOSS",
                "symbol": "BTCUSDT",
                "htf": "5M",
                "setup_htf": "1H",
                "bar_open_ms": 2_000,
                "exit_price": 95.0,
                "direction": "LONG",
            },
        ],
        tf="1H",
        since_ms=None,
        include_liberal=False,
        kinds={"TAKE_PROFIT", "STOP_LOSS"},
        symbol="BTCUSDT",
    )

    assert arrays["kinds"] == ["TAKE_PROFIT", "STOP_LOSS"]
    assert arrays["prices"] == [110.0, 95.0]


def test_collect_arrays_prepare_includes_impulse_anchors() -> None:
    arrays = _collect_arrays(
        [
            {
                "kind": "PREPARE",
                "symbol": "BTCUSDT",
                "htf": "1H",
                "bar_open_ms": 3_000,
                "origin_price": 105.0,
                "direction": "LONG",
                "structure_broken_open_ms": 2_500,
                "impulse_leg_start_open_ms": 1_000,
                "impulse_leg_end_open_ms": 2_000,
                "impulse_start_price": 100.0,
                "impulse_end_price": 110.0,
            }
        ],
        tf="1H",
        since_ms=None,
        include_liberal=False,
        kinds={"PREPARE"},
        symbol="BTCUSDT",
    )

    assert arrays["kinds"] == ["PREPARE"]
    assert arrays["prices"] == [105.0]
    assert arrays["times2"] == [2_500]
    assert arrays["impulse_start_times"] == [1_000]
    assert arrays["impulse_end_times"] == [2_000]
    assert arrays["impulse_start_prices"] == [100.0]
    assert arrays["impulse_end_prices"] == [110.0]


def test_render_pine_uses_labels_without_lines(tmp_path) -> None:
    out = tmp_path / "overlay.pine"

    _render_pine(
        times=[1_000],
        times2=[0],
        prices=[100.0],
        kinds_out=["ENTRY"],
        subkinds=[""],
        dirs=["LONG"],
        ote_lo=[0.0],
        ote_hi=[0.0],
        sls=[None],
        tps=[None],
        impulse_start_times=[0],
        impulse_end_times=[0],
        impulse_start_prices=[None],
        impulse_end_prices=[None],
        out_path=out,
        max_markers=400,
    )

    rendered = out.read_text(encoding="utf-8")
    assert "label.new" in rendered
    assert "line.new" not in rendered


def test_render_pine_pushes_prepare_impulse_anchor_arrays(tmp_path) -> None:
    out = tmp_path / "prepare_overlay.pine"

    _render_pine(
        times=[3_000],
        times2=[2_500],
        prices=[105.0],
        kinds_out=["PREPARE"],
        subkinds=[""],
        dirs=["LONG"],
        ote_lo=[100.0],
        ote_hi=[110.0],
        sls=[None],
        tps=[None],
        impulse_start_times=[1_000],
        impulse_end_times=[2_000],
        impulse_start_prices=[100.0],
        impulse_end_prices=[110.0],
        out_path=out,
        max_markers=400,
    )

    rendered = out.read_text(encoding="utf-8")
    assert "array.push(sig_kind, \"PREPARE\")" in rendered
    assert "array.push(sig_imp_start_t, 1000)" in rendered
    assert "array.push(sig_imp_end_t, 2000)" in rendered
    assert "array.push(sig_imp_start_price, 100.0)" in rendered
    assert "array.push(sig_imp_end_price, 110.0)" in rendered
    assert "IMP START" in rendered
    assert "IMP END" in rendered


def test_render_pine_suppresses_impulse_start_when_same_bar_has_end(tmp_path) -> None:
    out = tmp_path / "prepare_overlay.pine"

    _render_pine(
        times=[3_000, 4_000],
        times2=[2_500, 3_500],
        prices=[105.0, 95.0],
        kinds_out=["PREPARE", "PREPARE"],
        subkinds=["", ""],
        dirs=["LONG", "SHORT"],
        ote_lo=[100.0, 90.0],
        ote_hi=[110.0, 100.0],
        sls=[None, None],
        tps=[None, None],
        impulse_start_times=[1_000, 2_000],
        impulse_end_times=[2_000, 3_000],
        impulse_start_prices=[100.0, 110.0],
        impulse_end_prices=[110.0, 90.0],
        out_path=out,
        max_markers=400,
    )

    rendered = out.read_text(encoding="utf-8")
    assert "array.push(sig_imp_end_t, 2000)" in rendered
    assert "array.push(sig_imp_start_t, 2000)" not in rendered
    assert "array.push(sig_imp_start_price, 110.0)" not in rendered
