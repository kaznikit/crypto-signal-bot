"""Pine-style market structure: pivots + HH/LH/HL/LL + impulse legs + BOS/CHoCH.

Эта реализация — порт публичного индикатора **«Market Structure» by Leviathan**
(см. ``docs/pine_reference.md`` или исходник у пользователя), который выбран
эталоном после того, как SMC-based стек (``smartmoneyconcepts.smc``)
выдавал «прыгающие» импульсы и неправильные анкоры.

Ключевые отличия от прошлого стека:

* **Пивоты — это классические ``ta.pivothigh`` / ``ta.pivotlow``**: бар
  считается пивот-хаем, если его high ≥ всех highs в окне
  ``[i-swing_size, i+swing_size]``. Подтверждается через ``swing_size``
  баров после самого пивота.
* **Каждый пивот сразу классифицируется** как HH / LH (для HIGH-пивотов) и
  HL / LL (для LOW-пивотов) сравнением с предыдущим пивотом своего типа.
* **Импульс** = leg HL→HH (LONG) или LH→LL (SHORT). Это «expansion» после
  коррекции; точно та же логика, по которой Pine-индикатор рисует 0.5-линию
  только между HL и HH (или LH и LL). Развороты тренда (LL→HH, HH→LL) сами по
  себе импульсами не считаются — это уже область CHoCH-маркеров.
* **BOS/CHoCH** — единственный активный ``prevHigh`` (или ``prevLow``).
  Появление нового пивота активирует уровень; первое close-пересечение
  деактивирует и эмитит событие. Если предыдущее событие было в обратную
  сторону → ``CHOCH``, иначе → ``BOS``.

Эта модель **намного проще** прежнего стека (никакого walk-back, никакого
``extend_impulse_to_structural_extreme``, никакого ``find_extended_impulse_start``).
Анкоры импульсов всегда совпадают с теми, что глазами видит пользователь
на TV.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd


@dataclass(slots=True, frozen=True)
class Pivot:
    """Подтверждённый пивот с HH/LH/HL/LL-классификацией.

    ``idx`` — позиционный индекс бара пивота в исходном df.
    ``kind`` — ``"HIGH"`` или ``"LOW"``.
    ``price`` — цена пивота (high для HIGH, low для LOW).
    ``label`` — ``"HH"`` / ``"LH"`` / ``"HL"`` / ``"LL"`` — относительно
    предыдущего пивота того же типа.
    """

    idx: int
    kind: str  # "HIGH" | "LOW"
    price: float
    label: str  # "HH" | "LH" | "HL" | "LL"


@dataclass(slots=True, frozen=True)
class ImpulseLeg:
    """Импульс — leg HL→HH (LONG) или LH→LL (SHORT).

    ``start_idx``/``start_price`` — бар и цена «корректирующего» пивота
    (HL для LONG, LH для SHORT). Это invalidation для continuation-сетапа.
    ``end_idx``/``end_price`` — бар и цена «экспансионного» пивота (HH / LL).
    Это пик импульса; от него тянется fib-сетка.
    """

    direction: str  # "LONG" | "SHORT"
    start_idx: int
    start_price: float
    end_idx: int
    end_price: float

    @property
    def fib_half(self) -> float:
        """Цена 0.5-ретрейса (mid между start и end). Pine рисует
        ровно эту линию для каждого HL→HH / LH→LL leg'а."""
        return (self.start_price + self.end_price) / 2.0


@dataclass(slots=True, frozen=True)
class StructureBreak:
    """BOS или CHoCH: первое close-пересечение активного prevHigh/prevLow.

    ``swing_idx`` — пивот, чей уровень был пробит.
    ``broken_idx`` — бар, на котором пересечение состоялось.
    ``kind`` — ``"BOS"`` (продолжение тренда) или ``"CHOCH"`` (смена тренда:
    предыдущий пробой был в противоположную сторону).
    """

    direction: str  # "LONG" (high broken) | "SHORT" (low broken)
    kind: str  # "BOS" | "CHOCH"
    swing_idx: int
    swing_price: float
    broken_idx: int


def detect_pivots(
    df: pd.DataFrame,
    swing_size: int,
) -> list[Pivot]:
    """Классические пивоты (``ta.pivothigh`` / ``ta.pivotlow``) с HH/LH/HL/LL.

    Бар ``i`` — пивот-хай, если ``df.high.iloc[i]`` ≥ всех highs в окне
    ``[i - swing_size, i + swing_size]`` (включительно). Симметрично для лоу.

    Pine использует строгое ``>=`` для HH (``pivHi >= prevHigh``) — равные
    значения отнесены к HH, не к LH. Здесь то же самое.

    Возвращает пивоты в хронологическом порядке. Если на одном баре получается
    и HIGH, и LOW пивот (вырожденная inside-bar серия) — оба сохраняются.
    """
    if swing_size < 1:
        return []
    n = len(df)
    if n < 2 * swing_size + 1:
        return []

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    candidates: list[tuple[int, str, float]] = []
    for i in range(swing_size, n - swing_size):
        window_hi = highs[i - swing_size : i + swing_size + 1]
        h = float(highs[i])
        if math.isfinite(h) and h == window_hi.max():
            candidates.append((i, "HIGH", h))
        window_lo = lows[i - swing_size : i + swing_size + 1]
        low_v = float(lows[i])
        if math.isfinite(low_v) and low_v == window_lo.min():
            candidates.append((i, "LOW", low_v))

    # Сортировка по (idx, kind) — стабильно для случая когда на одном баре
    # одновременно HIGH и LOW; Pine обрабатывает их независимо в одном тике.
    candidates.sort(key=lambda x: (x[0], 0 if x[1] == "HIGH" else 1))

    prev_high: float | None = None
    prev_low: float | None = None
    pivots: list[Pivot] = []
    for idx, kind, price in candidates:
        if kind == "HIGH":
            label = "HH" if (prev_high is None or price >= prev_high) else "LH"
            prev_high = price
        else:
            label = "HL" if (prev_low is None or price >= prev_low) else "LL"
            prev_low = price
        pivots.append(Pivot(idx=idx, kind=kind, price=price, label=label))

    return pivots


def extract_impulse_legs(pivots: list[Pivot]) -> list[ImpulseLeg]:
    """Pine-эталонные импульсы: только HL→HH (LONG) и LH→LL (SHORT).

    Используется для эмиссии IMPULSE-маркеров на overlay'е (Pine рисует
    0.5-линию ровно для этих пар).  Для PREPARE-детекции используйте
    ``extract_all_pivot_legs``, который возвращает ВСЕ HIGH↔LOW переходы.
    """
    legs: list[ImpulseLeg] = []
    for i in range(1, len(pivots)):
        prev = pivots[i - 1]
        cur = pivots[i]
        if cur.label == "HH" and prev.label == "HL":
            legs.append(
                ImpulseLeg(
                    direction="LONG",
                    start_idx=prev.idx,
                    start_price=prev.price,
                    end_idx=cur.idx,
                    end_price=cur.price,
                )
            )
        elif cur.label == "LL" and prev.label == "LH":
            legs.append(
                ImpulseLeg(
                    direction="SHORT",
                    start_idx=prev.idx,
                    start_price=prev.price,
                    end_idx=cur.idx,
                    end_price=cur.price,
                )
            )
    return legs


def extract_all_pivot_legs(pivots: list[Pivot]) -> list[ImpulseLeg]:
    """Все ноги между последовательными пивотами противоположного типа.

    В отличие от ``extract_impulse_legs`` (только HL→HH и LH→LL), эта
    функция возвращает **все** HIGH↔LOW переходы:

    * LOW → HIGH (любые метки: HL→HH, HL→LH, LL→HH, LL→HL) → ``"LONG"``
    * HIGH → LOW (любые метки: HH→LL, HH→LH, LH→LL, LH→HL) → ``"SHORT"``

    Используется для детекции PREPARE: PREPARE приходит при первом касании
    0.5 **любой** ноги между последовательными пивотами — не только для
    «идеального» expansion-leg'а HL→HH, но и для коррекционных движений
    (HH→LH, LL→HL).

    Направление PREPARE = направление ноги:

    * LOW→HIGH (LONG): цена росла, ищем первый откат к 0.5 → PREPARE LONG.
    * HIGH→LOW (SHORT): цена падала, ищем первый отскок к 0.5 → PREPARE SHORT.
    """
    legs: list[ImpulseLeg] = []
    for i in range(1, len(pivots)):
        prev = pivots[i - 1]
        cur = pivots[i]
        if prev.kind == "LOW" and cur.kind == "HIGH":
            legs.append(
                ImpulseLeg(
                    direction="LONG",
                    start_idx=prev.idx,
                    start_price=prev.price,
                    end_idx=cur.idx,
                    end_price=cur.price,
                )
            )
        elif prev.kind == "HIGH" and cur.kind == "LOW":
            legs.append(
                ImpulseLeg(
                    direction="SHORT",
                    start_idx=prev.idx,
                    start_price=prev.price,
                    end_idx=cur.idx,
                    end_price=cur.price,
                )
            )
    return legs


def extract_structure_breaks(
    df: pd.DataFrame,
    swing_size: int,
    *,
    use_close: bool = True,
) -> list[StructureBreak]:
    """Pine-style BOS/CHoCH: один активный prevHigh / prevLow, деактивируется
    при первом close-пересечении.

    ``use_close=True`` (Pine ``'Candle Close'``) — break только если CLOSE
    свечи пересёк уровень. ``use_close=False`` (``'Wicks'``) — достаточно wick.

    CHoCH = пробой в направлении, обратном предыдущему пробою; первый пробой
    в серии всегда BOS.
    """
    if swing_size < 1:
        return []
    n = len(df)
    if n < 2 * swing_size + 1:
        return []

    highs = df["high"].to_numpy()
    lows = df["low"].to_numpy()
    closes = df["close"].to_numpy()
    src_hi = closes if use_close else highs
    src_lo = closes if use_close else lows

    prev_high: float | None = None
    prev_low: float | None = None
    prev_high_idx = -1
    prev_low_idx = -1
    high_active = False
    low_active = False
    prev_break_dir = 0  # 1=last break was UP, -1=DOWN, 0=none yet

    breaks: list[StructureBreak] = []
    for i in range(n):
        cand = i - swing_size
        if cand >= swing_size:
            window_hi = highs[cand - swing_size : cand + swing_size + 1]
            if math.isfinite(float(highs[cand])) and float(highs[cand]) == window_hi.max():
                prev_high = float(highs[cand])
                prev_high_idx = cand
                high_active = True
            window_lo = lows[cand - swing_size : cand + swing_size + 1]
            if math.isfinite(float(lows[cand])) and float(lows[cand]) == window_lo.min():
                prev_low = float(lows[cand])
                prev_low_idx = cand
                low_active = True

        if high_active and prev_high is not None and float(src_hi[i]) > prev_high:
            kind = "CHOCH" if prev_break_dir == -1 else "BOS"
            breaks.append(
                StructureBreak(
                    direction="LONG",
                    kind=kind,
                    swing_idx=prev_high_idx,
                    swing_price=prev_high,
                    broken_idx=i,
                )
            )
            high_active = False
            prev_break_dir = 1
        if low_active and prev_low is not None and float(src_lo[i]) < prev_low:
            kind = "CHOCH" if prev_break_dir == 1 else "BOS"
            breaks.append(
                StructureBreak(
                    direction="SHORT",
                    kind=kind,
                    swing_idx=prev_low_idx,
                    swing_price=prev_low,
                    broken_idx=i,
                )
            )
            low_active = False
            prev_break_dir = -1

    return breaks


def latest_impulse_leg(
    pivots: list[Pivot],
    *,
    direction: str | None = None,
) -> ImpulseLeg | None:
    """Последний impulse leg в указанном направлении (или вообще последний)."""
    legs = extract_impulse_legs(pivots)
    if direction is not None:
        legs = [leg for leg in legs if leg.direction == direction]
    return legs[-1] if legs else None


def structure_break_key(
    htf: str,
    br: StructureBreak,
    df: pd.DataFrame,
) -> tuple[str, int, int, str, str]:
    """Стабильный ключ события BOS/CHoCH для дедупа PREPARE."""
    return (
        htf,
        int(df.iloc[br.broken_idx]["open_time"]),
        int(df.iloc[br.swing_idx]["open_time"]),
        br.kind,
        br.direction,
    )


def latest_structure_break(
    breaks: list[StructureBreak],
    *,
    direction: str | None = None,
    kinds: tuple[str, ...] = ("BOS", "CHOCH"),
    max_bars_ago: int | None = None,
    last_idx: int | None = None,
) -> StructureBreak | None:
    """Последний BOS/CHoCH с фильтрами по направлению, виду и свежести.

    ``max_bars_ago`` — насколько далеко в прошлое от ``last_idx`` смотреть.
    Если ``last_idx`` не задан, используется ``broken_idx`` максимального
    события + ``max_bars_ago`` (т.е. фильтр не применяется к историческим
    запросам).
    """
    filtered = [
        b
        for b in breaks
        if b.kind in kinds and (direction is None or b.direction == direction)
    ]
    if not filtered:
        return None
    if max_bars_ago is not None and last_idx is not None:
        filtered = [b for b in filtered if (last_idx - b.broken_idx) <= max_bars_ago]
        if not filtered:
            return None
    return filtered[-1]


def continuation_anchor_break(
    breaks: list[StructureBreak],
    *,
    direction: str,
    last_idx: int,
    max_bars_ago: int,
    kinds: tuple[str, ...] = ("BOS", "CHOCH"),
) -> StructureBreak | None:
    """Якорь PREPARE-continuation: последний BOS/CHoCH в ``direction`` **после**
    последнего противоположного BOS/CHoCH.

    Если после LONG BOS был SHORT BOS/CHoCH (например, на LL), старый LONG
  пробой не может инициировать PREPARE — нужен новый LONG BOS/CHoCH. Это
    согласует логику с отображением STRUCTURE на графике (виден только свежий
    пробой, а не lookback на 120 баров назад).
    """
    opposite = "SHORT" if direction == "LONG" else "LONG"
    last_opposite = latest_structure_break(
        breaks,
        direction=opposite,
        kinds=kinds,
        max_bars_ago=max_bars_ago,
        last_idx=last_idx,
    )
    min_broken = last_opposite.broken_idx if last_opposite is not None else -1

    aligned = [
        b
        for b in breaks
        if b.kind in kinds
        and b.direction == direction
        and b.broken_idx > min_broken
        and (last_idx - b.broken_idx) <= max_bars_ago
    ]
    if not aligned:
        return None
    return aligned[-1]


def find_first_touch_idx(
    df: pd.DataFrame,
    *,
    direction: str,
    level: float,
    since_idx: int,
) -> int:
    """Индекс первого бара в ``(since_idx, last_idx]``, коснувшегося ``level``.

    LONG: ``low <= level``. SHORT: ``high >= level``. ``-1`` если касания нет
    или ``since_idx >= last_idx``.
    """
    if df is None or df.empty:
        return -1
    last_idx = int(df.index[-1])
    if since_idx >= last_idx:
        return -1
    sub = df.iloc[since_idx + 1 : last_idx + 1]
    if sub.empty:
        return -1
    if direction == "LONG":
        for idx in sub.index:
            if float(df.loc[idx, "low"]) <= level:
                return int(idx)
    elif direction == "SHORT":
        for idx in sub.index:
            if float(df.loc[idx, "high"]) >= level:
                return int(idx)
    return -1


def first_touch_of_level_since(
    df: pd.DataFrame,
    *,
    direction: str,
    level: float,
    since_idx: int,
) -> bool:
    """True, если **последний** бар df — первый после ``since_idx`` (исключая),
    который дотянулся до ``level``.

    Для LONG ретрейс идёт ВНИЗ: триггер = последний low ≤ level, и все
    предыдущие lows в окне ``(since_idx, last_idx - 1]`` строго > level.
    Для SHORT симметрично.

    Если ``since_idx >= last_idx`` (бар пика — текущий бар), возвращаем False
    (импульс ещё не завершён).
    """
    if df is None or df.empty:
        return False
    last_idx = int(df.index[-1])
    if since_idx >= last_idx:
        return False
    touch_idx = find_first_touch_idx(
        df, direction=direction, level=level, since_idx=since_idx
    )
    return touch_idx == last_idx


def prepare_emission_on_current_bar(
    df: pd.DataFrame,
    *,
    leg_end_idx: int,
    swing_size: int,
    touch_direction: str,
    level: float,
    since_idx: int | None = None,
) -> tuple[int, int] | None:
    """Бар эмиссии PREPARE = ``max(leg_end_idx + swing_size, touch_idx)``.

    Возвращает ``(emission_bar_idx, touch_idx)``, если эмиссия должна быть на
    **текущем** баре df; иначе ``None``. Учитывает касания 0.5 во время окна
    подтверждения пивота (``touch_idx < leg_end_idx + swing_size``).
    """
    last_pos = int(df.index[-1])
    since = leg_end_idx if since_idx is None else since_idx
    touch_idx = find_first_touch_idx(
        df, direction=touch_direction, level=level, since_idx=since
    )
    if touch_idx < 0:
        return None
    emission_bar = max(leg_end_idx + swing_size, touch_idx)
    if emission_bar != last_pos:
        return None
    return emission_bar, touch_idx


@dataclass(slots=True, frozen=True)
class LtfChoCh:
    """LTF структурный пробой для подтверждения ENTRY (CHoCH и/или BOS)."""

    direction: str  # "LONG" | "SHORT"
    level: float
    bars_ago: int
    kind: str = "CHOCH"  # "CHOCH" | "BOS"


def _bar_index_at_or_after(df: pd.DataFrame, open_time_ms: int) -> int:
    matched = df.index[df["open_time"] >= open_time_ms]
    if len(matched) == 0:
        return int(df.index[-1])
    return int(matched[0])


def detect_ltf_entry_confirm(
    df: pd.DataFrame,
    *,
    swing_size: int,
    max_bars_ago: int = 3,
    use_close: bool = True,
    kinds: tuple[str, ...] = ("CHOCH",),
    direction: str | None = None,
    since_open_ms: int | None = None,
    lookback_mode: str = "bars",
) -> LtfChoCh | None:
    """Последний BOS/CHoCH на LTF (опционально — в ``direction``).

    ``lookback_mode``:
    - ``bars`` — только пробои не старше ``max_bars_ago`` от текущего бара;
    - ``since_prepare`` — любой пробой после ``since_open_ms`` (в пределах TTL сетапа).
    """
    if not kinds:
        return None
    allowed = {k.upper() for k in kinds}
    breaks = extract_structure_breaks(df, swing_size=swing_size, use_close=use_close)
    if not breaks:
        return None
    last_pos = int(df.index[-1])
    since_idx = 0
    if lookback_mode == "since_prepare" and since_open_ms is not None:
        since_idx = _bar_index_at_or_after(df, since_open_ms)

    def _in_window(broken_idx: int) -> bool:
        recent = (last_pos - broken_idx) <= max_bars_ago
        if lookback_mode == "bars":
            return recent
        if lookback_mode == "since_prepare":
            return broken_idx >= since_idx
        # hybrid — после PREPARE или в скользящем окне N баров
        return broken_idx >= since_idx or recent

    candidates = [
        b
        for b in breaks
        if b.kind in allowed
        and _in_window(b.broken_idx)
        and (direction is None or b.direction == direction)
    ]
    if not candidates:
        return None
    last = candidates[-1]
    return LtfChoCh(
        direction=last.direction,
        level=last.swing_price,
        bars_ago=last_pos - last.broken_idx,
        kind=last.kind,
    )


def detect_ltf_choch(
    df: pd.DataFrame,
    *,
    swing_size: int,
    max_bars_ago: int = 3,
    use_close: bool = True,
) -> LtfChoCh | None:
    """Обратная совместимость: только CHoCH."""
    return detect_ltf_entry_confirm(
        df,
        swing_size=swing_size,
        max_bars_ago=max_bars_ago,
        use_close=use_close,
        kinds=("CHOCH",),
    )


def impulse_invalidated(
    df: pd.DataFrame,
    *,
    direction: str,
    start_price: float,
    after_idx: int,
) -> bool:
    """True, если в окне ``(after_idx, last_idx]`` цена пробила ``start_price``
    в направлении инвалидации импульса.

    Для LONG (HL→HH) инвалидация = ``low < start_price`` (=HL).
    Для SHORT (LH→LL) инвалидация = ``high > start_price`` (=LH).
    """
    if df is None or df.empty:
        return False
    last_idx = int(df.index[-1])
    if after_idx >= last_idx:
        return False
    sub = df.iloc[after_idx + 1 : last_idx + 1]
    if sub.empty:
        return False
    if direction == "LONG":
        return bool((sub["low"] < start_price).any())
    if direction == "SHORT":
        return bool((sub["high"] > start_price).any())
    return False
