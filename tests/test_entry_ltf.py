from bot.analyzer.entry_ltf import (
    DEFAULT_LTF_BY_HTF,
    finest_closed_ltf,
    invalidation_tf_for_setup,
    ltf_expected_for_htf,
)
from bot.config import EntryConfig


def test_ltf_expected_from_config() -> None:
    entry = EntryConfig(ltf_by_htf={"4H": "5M|1H"})
    assert ltf_expected_for_htf("4H", entry) == "5M|1H"


def test_ltf_expected_default_for_unknown_htf() -> None:
    entry = EntryConfig()
    assert ltf_expected_for_htf("4H", entry) == DEFAULT_LTF_BY_HTF["4H"]


def test_ltf_expected_uses_cascade_when_enabled() -> None:
    entry = EntryConfig(
        ltf_by_htf={"1H": "5M"},
        cascade_enabled=True,
        cascade_by_htf={"1H": "5M|1M"},
    )

    assert ltf_expected_for_htf("1H", entry) == "5M|1M"


def test_ltf_expected_advanced_ignores_cascade() -> None:
    entry = EntryConfig(
        mode="advanced",
        ltf_by_htf={"1H": "5M"},
        cascade_enabled=True,
        cascade_by_htf={"1H": "5M|1M"},
    )

    assert ltf_expected_for_htf("1H", entry) == "5M"


def test_ltf_expected_sweep_reclaim_ignores_cascade() -> None:
    entry = EntryConfig(
        mode="sweep_reclaim",
        ltf_by_htf={"1H": "5M"},
        cascade_enabled=True,
        cascade_by_htf={"1H": "5M|1M"},
    )

    assert ltf_expected_for_htf("1H", entry) == "5M"


def test_finest_closed_ltf_picks_5m_when_closed() -> None:
    assert (
        finest_closed_ltf(
            "5M|15M|1H",
            closed_tfs=["5M", "15M"],
            available={"5M", "15M", "1H"},
        )
        == "5M"
    )


def test_invalidation_defaults_to_setup_htf() -> None:
    entry = EntryConfig(ltf_by_htf={"4H": "5M"})
    assert (
        invalidation_tf_for_setup("4H", "5M", entry, {"4H", "5M", "15M"})
        == "4H"
    )


def test_invalidation_explicit_override() -> None:
    entry = EntryConfig(
        ltf_by_htf={"4H": "5M"},
        invalidation_ltf_by_htf={"4H": "1H"},
    )
    assert (
        invalidation_tf_for_setup("4H", "5M", entry, {"4H", "5M", "1H"})
        == "1H"
    )
