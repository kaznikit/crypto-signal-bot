from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
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


class EntryConfig(BaseModel):
    """LTF-подтверждение ENTRY после PREPARE на HTF."""

    # HTF сетапа → LTF для ENTRY (pipe, приоритет слева: «5M|15M|1H» для 4H).
    ltf_by_htf: dict[str, str] = Field(
        default_factory=lambda: {
            "4H": "5M|15M|1H",
            "1H": "5M|15M",
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
    max_entries_per_setup: int = 2
    # Каскадный ENTRY: после PREPARE на HTF подтверждение идёт по цепочке TF,
    # например 1H -> 5M BOS/CHoCH -> 1M BOS/CHoCH.
    cascade_enabled: bool = False
    cascade_by_htf: dict[str, str] = Field(default_factory=lambda: {"1H": "5M|1M"})
    cascade_retrace_level: float = 0.5
    cascade_confirm_structure_kinds: list[str] = Field(
        default_factory=lambda: ["BOS", "CHOCH"]
    )


class FiltersConfig(BaseModel):
    min_atr_pct: float = 0.3
    min_rr: float = 1.5


class RiskConfig(BaseModel):
    sl_buffer_atr: float = 0.5
    tp_r_multiples: list[float] = Field(default_factory=lambda: [2.0, 3.0])


class TelegramConfig(BaseModel):
    chat_id_env: str = "TG_CHAT_ID"
    send_prepare_signals: bool = True


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


class StrategyFeaturesConfig(BaseModel):
    """Опциональные правила стратегии — включаются через config.yaml.

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
    strategy_features: StrategyFeaturesConfig = Field(default_factory=StrategyFeaturesConfig)

    def prepare_htfs(self) -> tuple[str, ...]:
        """HTF, на которых ищутся PREPARE (reversal — только 4H, continuation — все из списка)."""
        allowed = set(self.timeframes)
        return tuple(tf for tf in PREPARE_HTF_ORDER if tf in allowed)


class EnvConfig(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    tg_bot_token: str = Field(alias="TG_BOT_TOKEN")
    tg_chat_id: str = Field(alias="TG_CHAT_ID")
    tg_paper_chat_id: str | None = Field(default=None, alias="TG_PAPER_CHAT_ID")
    bybit_api_key: str | None = Field(default=None, alias="BYBIT_API_KEY")
    bybit_api_secret: str | None = Field(default=None, alias="BYBIT_API_SECRET")
    bot_env: str = Field(default="dev", alias="BOT_ENV")
    bot_log_level: str = Field(default="INFO", alias="BOT_LOG_LEVEL")
    bot_db_url: str = Field(default="sqlite:///./bot.db", alias="BOT_DB_URL")


def load_bot_config(config_path: Path) -> BotConfig:
    raw: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    return BotConfig.model_validate(raw)
