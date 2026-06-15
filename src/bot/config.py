from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExchangeConfig(BaseModel):
    name: str
    category: str
    domain: str = "bybit"
    tld: str = "com"


class SymbolsConfig(BaseModel):
    mode: str = "top_by_volume"
    count: int = 30
    quote: str = "USDT"


class ReversalConfig(BaseModel):
    """Параметры reversal-ветки. Используется pivot-стек (см. ``PivotsConfig``),
    поэтому ``swing_length_htf`` / ``impulse_lookback`` / ``min_impulse_atr`` /
    ``min_break_close_atr`` оставлены для обратной совместимости конфигов, но
    в детекции больше не участвуют.
    """

    ttl_bars_4h: int = 6
    # Сколько HTF-баров назад искать CHoCH, на котором держится сетап. Раздельно с
    # `ttl_bars_4h`: CHoCH — это исторический факт (он остаётся валидным, пока импульс
    # не сломан), а ttl_bars_4h управляет TTL уже созданного PREPARE.
    choch_lookback_bars: int = 30

    # === Legacy (игнорируется в pivot-стеке) ===
    swing_length_htf: int = 20
    impulse_lookback: int = 60
    min_impulse_atr: float = 0.0
    min_break_close_atr: float = 0.0


class ContinuationConfig(BaseModel):
    """Параметры continuation-ветки. Триггер = первое касание 0.5 у leg HL→HH
    (LONG) / LH→LL (SHORT) — см. ``bot.market.pivots``. ``fib_low`` оставлен
    конфигурируемым (по умолчанию 0.5, как в Pine-индикаторе).
    """

    fib_low: float = 0.5
    # Окно lookback для BOS/CHoCH в сторону импульса. Без свежего структурного
    # пробоя в сторону тренда PREPARE-continuation не строится.
    structure_max_bars_ago: int = 30

    # === Legacy (игнорируется в pivot-стеке) ===
    fib_high: float = 0.618
    impulse_lookback: int = 60
    impulse_swing_length: int = 10
    min_impulse_atr: float = 0.0


class PivotsConfig(BaseModel):
    """Pivot-параметры (Pine-style market structure).

    ``swing_size_by_tf`` — словарь TF → swing_size (=количество баров слева и
    справа в ``ta.pivothigh/pivotlow``). Меньшее значение = больше пивотов
    = больше шума; большее = реже структура, но чище.

    Дефолты подобраны под среднюю частоту структурных событий на каждом TF
    (Pine-индикатор по умолчанию имеет 20 для всех TF).

    ``bos_use_close`` — Pine ``'Candle Close'`` (True, дефолт) или ``'Wicks'``
    (False). Pine рекомендует Close для устойчивости.
    """

    swing_size_by_tf: dict[str, int] = Field(
        default_factory=lambda: {
            "1M": 5,
            "5M": 10,
            "15M": 10,
            "1H": 12,
            "4H": 15,
        }
    )
    bos_use_close: bool = True
    # На сколько HTF-баров после пика импульса разрешено ждать первое касание
    # 0.5. Если касание не случилось за это окно, PREPARE не строим
    # (импульс «устарел»). Без ограничения старые impulse legs давали бы
    # триггер на любом проходящем баре спустя сотни баров.
    impulse_max_age_bars: int = 60


class EntryAdvancedConfig(BaseModel):
    """Продвинутый ENTRY: sweep -> reclaim -> CHoCH -> retest."""

    sweep_lookback_bars: int = 48
    reclaim_max_bars: int = 3
    confirm_max_bars: int = 12
    confirm_structure_kinds: list[str] = Field(default_factory=lambda: ["CHOCH"])
    require_displacement: bool = True
    require_directional_reclaim: bool = False
    displacement_body_atr_min: float = 0.8
    require_volume_expansion: bool = False
    volume_multiplier: float = 1.3
    retest_max_bars: int = 12
    retest_tolerance_atr: float = 0.15
    stop_source: Literal["retest_extreme", "sweep_extreme"] = "retest_extreme"
    stop_buffer_atr: float = 0.15
    max_stop_atr: float = 1.5
    min_rr_to_htf_target: float = 3.0


class FibDcaLevelConfig(BaseModel):
    fib: float
    weight_pct: float


class FibDcaConfig(BaseModel):
    """Limit-entry ladder anchored to the PREPARE impulse."""

    monitoring_tf_by_htf: dict[str, str] = Field(
        default_factory=lambda: {"4H": "5M", "1H": "5M", "15M": "5M"}
    )
    levels: list[FibDcaLevelConfig] = Field(
        default_factory=lambda: [
            FibDcaLevelConfig(fib=0.5, weight_pct=40.0),
            FibDcaLevelConfig(fib=0.618, weight_pct=30.0),
            FibDcaLevelConfig(fib=0.705, weight_pct=20.0),
            FibDcaLevelConfig(fib=0.786, weight_pct=10.0),
        ]
    )
    stop_source: Literal["htf_invalidation"] = "htf_invalidation"
    target_source: Literal["impulse_end"] = "impulse_end"
    cancel_remaining_on_target: bool = True
    cancel_remaining_on_opposite_structure: bool = True

    @model_validator(mode="after")
    def validate_levels(self) -> FibDcaConfig:
        if not self.levels:
            raise ValueError("entry.fib_dca.levels must not be empty")
        fibs = [float(level.fib) for level in self.levels]
        if any(fib <= 0 or fib >= 1 for fib in fibs):
            raise ValueError("entry.fib_dca.levels fib values must be between 0 and 1")
        if fibs != sorted(fibs) or len(set(fibs)) != len(fibs):
            raise ValueError("entry.fib_dca.levels must be unique and sorted by fib")
        if any(float(level.weight_pct) <= 0 for level in self.levels):
            raise ValueError("entry.fib_dca.levels weight_pct must be positive")
        total = sum(float(level.weight_pct) for level in self.levels)
        if abs(total - 100.0) > 1e-6:
            raise ValueError("entry.fib_dca.levels weight_pct must sum to 100")
        return self


EntryMode = Literal["simple", "advanced", "sweep_reclaim", "fib_dca"]


class EntryConfig(BaseModel):
    """LTF-подтверждение ENTRY после PREPARE на HTF."""

    # simple — текущий ENTRY по LTF BOS/CHoCH; advanced — sweep/reclaim/CHoCH/retest.
    mode: EntryMode = "simple"
    # Empty: run only `mode`. Non-empty: create independent setup state for
    # every listed mode and emit all ENTRY variants from one PREPARE.
    comparison_modes: list[EntryMode] = Field(default_factory=list)
    # HTF сетапа → LTF для ENTRY. Pipe позволяет явно задать несколько TF.
    ltf_by_htf: dict[str, str] = Field(
        default_factory=lambda: {
            "4H": "5M",
            "1H": "5M",
            "15M": "5M",
        }
    )
    # TF для проверки invalidation (по умолчанию = HTF сетапа). Не путать с ltf_by_htf:
    # при ltf_by_htf "5M" инвалидация на 5M убивает сетап до ENTRY.
    invalidation_ltf_by_htf: dict[str, str] = Field(default_factory=dict)
    ltf_swing_length: dict[str, int] = Field(
        default_factory=lambda: {"1M": 4, "5M": 5, "15M": 8, "1H": 12, "4H": 20}
    )
    ltf_max_bars_ago: int = 24
    # Окно свежести LTF BOS/CHoCH по TF (перекрывает ltf_max_bars_ago для указанных TF).
    ltf_max_bars_ago_by_tf: dict[str, int] = Field(
        default_factory=lambda: {"1M": 120, "5M": 48, "15M": 36, "1H": 18}
    )
    # False: ENTRY без SL/TP в payload и без фильтра min_rr (только цена + CHoCH).
    compute_sl_tp: bool = True
    # False: не требовать close за уровнем LTF CHoCH (только факт CHoCH в сторону сделки).
    require_close_beyond_choch: bool = True
    # Какие LTF-пробои подтверждают ENTRY: CHOCH, BOS или оба (BOS заметно чаще).
    confirm_structure_kinds: list[str] = Field(default_factory=lambda: ["CHOCH", "BOS"])
    # bars | since_prepare | hybrid (после PREPARE или в последних N LTF-барах).
    structure_lookback: str = "since_prepare"
    # structure_break — BOS/CHoCH на LTF; directional_close — бычий/медвежий close без BOS.
    confirm_mode: str = "structure_break"
    # true — swing для ENTRY = pivots.swing_size_by_tf (как линии BOS на оверлее).
    ltf_swing_use_pivot_sizes: bool = False
    # Максимум ENTRY-сигналов на один setup, пока он остаётся валидным.
    max_entries_per_setup: int = 1
    # Каскадный ENTRY: после PREPARE на HTF подтверждение идёт по цепочке TF,
    # например 1H -> 5M BOS/CHoCH -> 1M BOS/CHoCH.
    cascade_enabled: bool = False
    cascade_by_htf: dict[str, str] = Field(default_factory=lambda: {"1H": "5M|1M"})
    cascade_retrace_level: float = 0.5
    cascade_confirm_structure_kinds: list[str] = Field(
        default_factory=lambda: ["BOS", "CHOCH"]
    )
    advanced: EntryAdvancedConfig = Field(default_factory=EntryAdvancedConfig)
    fib_dca: FibDcaConfig = Field(default_factory=FibDcaConfig)

    @model_validator(mode="after")
    def validate_comparison_modes(self) -> EntryConfig:
        if len(set(self.comparison_modes)) != len(self.comparison_modes):
            raise ValueError("entry.comparison_modes must be unique")
        return self

    def active_modes(self) -> tuple[str, ...]:
        modes = self.comparison_modes or [self.mode]
        return tuple(str(mode) for mode in modes)

    def comparison_enabled(self) -> bool:
        return len(self.active_modes()) > 1


class FiltersConfig(BaseModel):
    min_atr_pct: float = 0.3
    min_rr: float = 1.5


class RiskConfig(BaseModel):
    sl_buffer_atr: float = 0.5
    tp_r_multiples: list[float] = Field(default_factory=lambda: [2.0, 3.0])


class TelegramConfig(BaseModel):
    prepare_chat_id_env: str = "TG_PREPARE_CHAT_ID"
    entry_chat_id_env: str = "TG_ENTRY_CHAT_ID"
    fallback_chat_id_env: str = "TG_CHAT_ID"
    paper_chat_id_env: str = "TG_PAPER_CHAT_ID"
    send_prepare_signals: bool = True
    route_paper_mode_to_paper_chat: bool = False


class LiberalConfig(BaseModel):
    """Ослабленные пороги для paper-канала (валидация объёма сигналов)."""

    enabled: bool = False
    min_atr_pct: float = 0.15
    min_rr: float = 1.2
    min_quality_score: int = 0
    fib_low: float = 0.382
    fib_high: float = 0.786
    # Расширенное окно lookback CHoCH для liberal-режима (был 8, теперь 50 — чтобы
    # ретрейс к OTE успел дойти до зоны даже после длинного бокового движения).
    max_bars_ago_4h: int = 50
    ltf_swing_length_override: dict[str, int] = Field(
        default_factory=lambda: {"1M": 3, "5M": 5, "15M": 8, "1H": 10}
    )


class PaperModeConfig(BaseModel):
    enabled: bool = True
    paper_chat_id_env: str = "TG_PAPER_CHAT_ID"
    liberal: LiberalConfig = Field(default_factory=LiberalConfig)


class HistoryReplayConfig(BaseModel):
    # Upper bound для авторасширения младших TF в history replay/export.
    # Для каскада 1H -> 15M -> 5M -> 1M нужен глубокий 1M-ряд:
    # 1000 свечей 1H = до 60000 свечей 1M.
    max_expanded_bars_per_tf: int = 4_000


class EntryStatsConfig(BaseModel):
    enabled: bool = True
    check_interval_hours: int = 24
    max_candidates_per_run: int = 25


class PrepareStatsConfig(BaseModel):
    enabled: bool = True
    check_interval_hours: int = 24
    max_candidates_per_run: int = 25
    evaluation_tf_by_htf: dict[str, str] = Field(
        default_factory=lambda: {"4H": "5M", "1H": "5M", "15M": "1M"}
    )
    # Empty means use entry.fib_dca.levels.
    fib_levels: list[float] = Field(default_factory=list)
    target_source: Literal["impulse_end"] = "impulse_end"
    invalidation_source: Literal["setup_invalidation"] = "setup_invalidation"

    @model_validator(mode="after")
    def validate_fib_levels(self) -> PrepareStatsConfig:
        fibs = [float(fib) for fib in self.fib_levels]
        if any(fib <= 0 or fib >= 1 for fib in fibs):
            raise ValueError("prepare_stats.fib_levels values must be between 0 and 1")
        if fibs != sorted(fibs) or len(set(fibs)) != len(fibs):
            raise ValueError("prepare_stats.fib_levels must be unique and sorted")
        return self


class StrategyFeaturesConfig(BaseModel):
    """Опциональные правила стратегии — включаются через config/setup.yaml.

    ``extend_impulse_to_structural_extreme`` и ``completion_retrace`` оставлены
    для обратной совместимости конфигов, но в pivot-стеке не используются
    (импульсы анкорятся всегда от HL/LH-пивота — никакого walk-back).
    """

    require_liquidity_grab_reversal: bool = False
    quality_score_enabled: bool = True
    min_quality_score: int = 0
    volume_expansion_in_score: bool = True
    continuation_require_4h_alignment: bool = False
    require_ob_or_fvg_in_ote: bool = False
    swing_length_ob_fvg: int = 10

    # === Legacy (игнорируется в pivot-стеке) ===
    extend_impulse_to_structural_extreme: bool = True
    completion_retrace: float = 0.5


PREPARE_HTF_ORDER: tuple[str, ...] = ("4H", "1H", "15M")


class BotConfig(BaseModel):
    exchange: ExchangeConfig
    symbols: SymbolsConfig
    timeframes: list[str]
    reversal: ReversalConfig
    continuation: ContinuationConfig
    pivots: PivotsConfig = Field(default_factory=PivotsConfig)
    entry: EntryConfig = Field(default_factory=EntryConfig)
    filters: FiltersConfig
    risk: RiskConfig
    telegram: TelegramConfig
    paper_mode: PaperModeConfig
    history_replay: HistoryReplayConfig = Field(default_factory=HistoryReplayConfig)
    entry_stats: EntryStatsConfig = Field(default_factory=EntryStatsConfig)
    prepare_stats: PrepareStatsConfig = Field(default_factory=PrepareStatsConfig)
    strategy_features: StrategyFeaturesConfig = Field(default_factory=StrategyFeaturesConfig)

    def prepare_htfs(self) -> tuple[str, ...]:
        """HTF, на которых ищутся PREPARE (reversal — только 4H, continuation — все из списка)."""
        allowed = set(self.timeframes)
        return tuple(tf for tf in PREPARE_HTF_ORDER if tf in allowed)


class EnvConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    tg_bot_token: str = Field(alias="TG_BOT_TOKEN")
    tg_chat_id: str | None = Field(default=None, alias="TG_CHAT_ID")
    tg_prepare_chat_id: str | None = Field(default=None, alias="TG_PREPARE_CHAT_ID")
    tg_entry_chat_id: str | None = Field(default=None, alias="TG_ENTRY_CHAT_ID")
    tg_paper_chat_id: str | None = Field(default=None, alias="TG_PAPER_CHAT_ID")
    bybit_api_key: str | None = Field(default=None, alias="BYBIT_API_KEY")
    bybit_api_secret: str | None = Field(default=None, alias="BYBIT_API_SECRET")
    bot_env: str = Field(default="dev", alias="BOT_ENV")
    bot_log_level: str = Field(default="INFO", alias="BOT_LOG_LEVEL")
    bot_db_url: str = Field(default="sqlite:///./bot.db", alias="BOT_DB_URL")

    def telegram_chat_id(self, env_name: str) -> str | None:
        values = {
            "TG_CHAT_ID": self.tg_chat_id,
            "TG_PREPARE_CHAT_ID": self.tg_prepare_chat_id,
            "TG_ENTRY_CHAT_ID": self.tg_entry_chat_id,
            "TG_PAPER_CHAT_ID": self.tg_paper_chat_id,
        }
        return values.get(env_name)


CONFIG_FILENAMES: tuple[str, ...] = (
    "runtime.yaml",
    "setup.yaml",
    "entry.yaml",
    "risk.yaml",
    "research.yaml",
    "notifications.yaml",
)


def _deep_merge(base: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def find_bot_config_source(start: Path | None = None) -> Path:
    cwd = start or Path.cwd()
    for candidate in (cwd, Path(__file__).resolve().parents[2]):
        config_dir = candidate / "config"
        if config_dir.is_dir():
            return config_dir
        legacy = candidate / "config.yaml"
        if legacy.exists():
            return legacy
        example = candidate / "config.example.yaml"
        if example.exists():
            return example
    msg = "Не найден config/, config.yaml или config.example.yaml."
    raise SystemExit(msg)


def load_bot_config(config_path: Path) -> BotConfig:
    if config_path.is_dir():
        raw: dict[str, Any] = {}
        missing = [name for name in CONFIG_FILENAMES if not (config_path / name).exists()]
        if missing:
            msg = f"В {config_path} отсутствуют обязательные конфиги: {', '.join(missing)}"
            raise ValueError(msg)
        for filename in CONFIG_FILENAMES:
            part = yaml.safe_load((config_path / filename).read_text(encoding="utf-8")) or {}
            if not isinstance(part, dict):
                raise ValueError(f"{config_path / filename} должен содержать YAML mapping")
            _deep_merge(raw, part)
    else:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return BotConfig.model_validate(raw)
