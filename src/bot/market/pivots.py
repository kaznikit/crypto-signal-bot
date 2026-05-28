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
* **Импульс** = leg HL→HH (LONG) или LH→LL (SHORT), **подтверждённый** BOS/CHoCH
  той же стороны. ``end`` = последний pivot-экстремум до ``broken_idx``,
  ``start`` = ближайший противоположный pivot. Повторный BOS без касания
  ``fib_half`` сбрасывает предыдущую ногу (reset start/end).
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
    ``anchor_break_idx`` — бар BOS/CHoCH, подтвердивший ногу (break-driven).
    """

    direction: str  # "LONG" | "SHORT"
    start_idx: int
    start_price: float
    end_idx: int
    end_price: float
    anchor_break_idx: int | None = None

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

    # Нормализация «активной метки»: однотипные пивоты подряд (без пивота
    # противоположного типа между ними) считаем переносом текущей метки, а не
    # созданием новой. Это убирает цепочки HL→HL→HL / LH→LH→LH на плато и в
    # мелком шуме и оставляет один активный HIGH/LOW за фазу движения.
    collapsed: list[tuple[int, str, float]] = []
    for idx, kind, price in candidates:
        if not collapsed:
            collapsed.append((idx, kind, price))
            continue

        last_idx, last_kind, last_price = collapsed[-1]
        if kind != last_kind:
            collapsed.append((idx, kind, price))
            continue

        if kind == "HIGH":
            # Для HIGH переносим метку на более высокий экстремум, а при
            # равенстве — на более поздний бар.
            if price > last_price or (price == last_price and idx > last_idx):
                collapsed[-1] = (idx, kind, price)
        else:
            # Для LOW переносим метку на более низкий экстремум, а при
            # равенстве — на более поздний бар.
            if price < last_price or (price == last_price and idx > last_idx):
                collapsed[-1] = (idx, kind, price)

    prev_high: float | None = None
    prev_low: float | None = None
    pivots: list[Pivot] = []
    for idx, kind, price in collapsed:
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


def _last_pivot_before(
    pivots: list[Pivot],
    *,
    before_idx: int,
    min_idx: int = -1,
    kind: str | None = None,
    label: str | None = None,
) -> Pivot | None:
    for pivot in reversed(pivots):
        if pivot.idx >= before_idx:
            continue
        if min_idx >= 0 and pivot.idx < min_idx:
            continue
        if kind is not None and pivot.kind != kind:
            continue
        if label is not None and pivot.label != label:
            continue
        return pivot
    return None


def _first_pivot_after_breaking_threshold(
    pivots: list[Pivot],
    *,
    after_idx: int,
    kind: str,
    direction: str,
    threshold_price: float,
    before_idx: int | None = None,
) -> Pivot | None:
    """Первый pivot в окне ``(after_idx, before_idx)``, пробивший ``threshold_price``.

    Для ``direction == "SHORT"`` (``kind == "LOW"``) ищем первый LOW с
    ``price < threshold_price``. Для ``LONG`` (``kind == "HIGH"``) — первый
    HIGH с ``price > threshold_price``. ``before_idx`` (если задан) ограничивает
    поиск сверху, чтобы не «забегать вперёд» в pivot, принадлежащий следующему
    одноимённому break (он будет привязан к нему).
    """
    for pivot in pivots:
        if pivot.idx <= after_idx:
            continue
        if before_idx is not None and pivot.idx >= before_idx:
            break
        if pivot.kind != kind:
            continue
        if direction == "SHORT" and pivot.price < threshold_price:
            return pivot
        if direction == "LONG" and pivot.price > threshold_price:
            return pivot
    return None


def _last_pivot_before_breaking_threshold(
    pivots: list[Pivot],
    *,
    before_idx: int,
    min_idx: int,
    kind: str,
    direction: str,
    threshold_price: float,
    after_idx: int = -1,
) -> Pivot | None:
    """Последний pivot ``до`` ``before_idx``, пробивший ``threshold_price``.

    ``after_idx`` (если задан) дополнительно ограничивает поиск снизу: pivot'ы
    с ``idx <= after_idx`` не рассматриваются. Это нужно, чтобы end ноги текущего
    break не «крал» end-pivot уже подтверждённой предыдущей ноги того же
    направления.
    """
    for pivot in reversed(pivots):
        if pivot.idx >= before_idx:
            continue
        if min_idx >= 0 and pivot.idx < min_idx:
            continue
        if after_idx >= 0 and pivot.idx <= after_idx:
            continue
        if pivot.kind != kind:
            continue
        if direction == "SHORT" and pivot.price < threshold_price:
            return pivot
        if direction == "LONG" and pivot.price > threshold_price:
            return pivot
    return None


def _structural_start_pivot(
    pivots: list[Pivot],
    *,
    direction: str,
    before_idx: int,
    after_idx: int,
) -> Pivot | None:
    """Структурный start импульса в окне ``(after_idx, before_idx)``.

    Для SHORT — HIGH-пивот с МАКСИМАЛЬНОЙ ценой (структурный «потолок» движения).
    Для LONG  — LOW-пивот  с МИНИМАЛЬНОЙ ценой (структурное «дно» движения).

    Это согласует анкоры IMPULSE-ноги с тем, что трейдер визуально видит на
    графике: красная диагональ SHORT-импульса должна идти от пика (HH), а не
    от ближайшего LH; зелёная LONG-диагональ — от настоящего LL/HL дна, а не
    от последнего верхнего HL.

    При равенстве цены выбирается БОЛЕЕ РАННИЙ pivot — это исток движения, а
    не последнее повторное касание уровня.
    """
    if direction == "SHORT":
        candidates = [
            p
            for p in pivots
            if p.kind == "HIGH" and after_idx < p.idx < before_idx
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda p: (p.price, -p.idx))
    candidates = [
        p
        for p in pivots
        if p.kind == "LOW" and after_idx < p.idx < before_idx
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda p: (p.price, p.idx))


def _end_pivot_from_break_bar(
    df: pd.DataFrame,
    br: StructureBreak,
    *,
    after_end_idx: int,
) -> Pivot | None:
    """End BOS-ноги на баре пробоя, если wick уже сделал новый HH/LL.

    Pivot подтверждается через ``swing_size`` баров позже, но для continuation
    PREPARE нужна нога сразу (см. INJUSDT 1H 11.05.26 14:00 — HH на баре BOS).
    """
    if br.broken_idx <= after_end_idx or br.broken_idx >= len(df):
        return None
    if br.direction == "LONG":
        price = float(df.iloc[br.broken_idx]["high"])
        if price <= br.swing_price:
            return None
        return Pivot(idx=br.broken_idx, kind="HIGH", price=price, label="HH")
    price = float(df.iloc[br.broken_idx]["low"])
    if price >= br.swing_price:
        return None
    return Pivot(idx=br.broken_idx, kind="LOW", price=price, label="LL")


def _end_peak_from_df_after_break(
    df: pd.DataFrame,
    br: StructureBreak,
    *,
    before_idx: int | None,
    min_bar_idx: int,
) -> Pivot | None:
    """HH/LL после break по wick'ам df, пока pivot ещё не подтверждён.

    Нужно для CHOCH LONG, где пик (напр. INJ 1H 10.05.26 22:00) появляется
    через 1–3 бара после CHoCH, а pivot HIGH подтверждается только через
    ``swing_size`` баров (см. PREPARE 11.05.26 00:00 MSK).
    """
    if br.broken_idx >= len(df):
        return None
    last_pos = int(df.index[-1])
    hi = last_pos if before_idx is None else min(before_idx - 1, last_pos)
    lo = max(br.broken_idx, min_bar_idx)
    if hi < lo:
        return None
    sub = df.iloc[lo : hi + 1]
    if sub.empty:
        return None
    if br.direction == "LONG":
        price = float(sub["high"].max())
        if price <= br.swing_price:
            return None
        idx = int(sub["high"].idxmax())
        return Pivot(idx=idx, kind="HIGH", price=price, label="HH")
    price = float(sub["low"].min())
    if price >= br.swing_price:
        return None
    idx = int(sub["low"].idxmin())
    return Pivot(idx=idx, kind="LOW", price=price, label="LL")


def prepare_emission_bar_idx(
    *,
    leg_end_idx: int,
    anchor_break_idx: int | None,
    swing_size: int,
    touch_idx: int,
) -> int:
    """Индекс бара эмиссии PREPARE: первое касание 0.5 или после confirm пика.

    Для continuation-BOS пик часто на баре пробоя (``leg_end == anchor``):
    эмиссия на ``touch_idx``, без ожидания ``swing_size`` confirm pivot.

    Если пик в окне ``[leg_end, anchor + swing_size]`` (типичный CHOCH-импульс
    сразу после flip), эмитируем на первом касании 0.5 — иначе теряются
    INJ 1H 10.05.26 23:00 / 12.05.26 15:00. Иначе — классика
    ``max(leg_end + swing_size, touch_idx)`` (пик подтверждён позже).
    """
    if touch_idx < 0:
        return -1
    anchor = anchor_break_idx if anchor_break_idx is not None else -1
    if anchor >= 0:
        if leg_end_idx == anchor:
            return touch_idx
        if leg_end_idx >= anchor - swing_size and leg_end_idx <= anchor + swing_size:
            # CHOCH: пик рядом с flip, касание 0.5 — часто на следующем баре
            # (INJ 1H 11.05.26 00:00 MSK после wick 23:00).
            if touch_idx > leg_end_idx:
                return touch_idx + 1
            return touch_idx
    return max(leg_end_idx + swing_size, touch_idx)


def _build_leg_from_break(
    pivots: list[Pivot],
    br: StructureBreak,
    *,
    min_idx: int = -1,
    next_same_dir_break_idx: int | None = None,
    min_end_idx: int = -1,
    previous_opposite_broken_idx: int = -1,
    df: pd.DataFrame | None = None,
) -> ImpulseLeg | None:
    """Собрать импульсную ногу от BOS/CHoCH.

    End ноги — это **новый** экстремум, в сторону пробоя относительно
    ``br.swing_price`` (для LONG — HIGH с ``price > swing_price``; для SHORT —
    LOW с ``price < swing_price``). Если такой pivot ещё не подтверждён, нога
    не строится — fallback'ом служит сам ``swing pivot`` (последний экстремум
    до пробоя в том же направлении сегмента).

    Start — **структурный** экстремум-исток движения:
    * SHORT: самый высокий HIGH-пивот в (last_opposite_broken_idx, end.idx) —
      то есть пик последнего LONG-тренда, от которого медведи начали движение.
    * LONG: самый низкий LOW-пивот в (last_opposite_broken_idx, end.idx) — дно
      последнего SHORT-тренда, от которого быки начали движение.

    Если в этом окне ничего нет, fallback'ом служит последний коррекционный
    HL/LH в трендовом сегменте.
    """
    if br.direction == "LONG":
        end_kind, start_kind = "HIGH", "LOW"
        preferred_start_label = "HL"
    else:
        end_kind, start_kind = "LOW", "HIGH"
        preferred_start_label = "LH"

    # End ноги (HH/LL) не может быть старее свинга, который пробивается этим break.
    # Иначе при CHOCH/BOS возможно «прилипание» к старому экстремуму из предыдущего
    # сегмента и потеря актуальной ноги (например, BOS LONG ловит HH из давно
    # завершённого LONG-тренда, который выше пробитого LH-свинга, но семантически
    # принадлежит прошлой ноге — см. INJUSDT 1H 18-19.05.26).
    # Применяем ограничение только когда swing соответствует семантике направления
    # (LONG ⇒ HIGH, SHORT ⇒ LOW). Иначе синтетические тесты с «несвязанным»
    # swing-kind ломаются.
    swing_kind_matches_dir = any(
        p.idx == br.swing_idx and p.kind == end_kind for p in pivots
    )
    if br.kind == "CHOCH" or swing_kind_matches_dir:
        after_end_idx = max(min_end_idx, br.swing_idx - 1)
    else:
        after_end_idx = max(min_end_idx, -1)

    # 1) Подтверждённый ``end`` до ``broken_idx``.
    end_pivot = _last_pivot_before_breaking_threshold(
        pivots,
        before_idx=br.broken_idx,
        min_idx=min_idx,
        kind=end_kind,
        direction=br.direction,
        threshold_price=br.swing_price,
        after_idx=after_end_idx,
    )
    # CHOCH: не использовать пробитый swing (LH/HL на ``swing_idx``) как end.
    if (
        br.kind == "CHOCH"
        and end_pivot is not None
        and end_pivot.idx <= br.swing_idx
    ):
        end_pivot = None
    # 2) Если в сегменте нет ничего глубже свинга — берём НОВЫЙ pivot ПОСЛЕ
    #    ``broken_idx`` (continuation BOS, где new LL/HH формируется уже после
    #    самого пробоя и подтверждается с задержкой ``swing_size``). Окно
    #    ограничено сверху следующим break того же направления, иначе мы бы
    #    «забирали» pivot, принадлежащий уже следующей ноге.
    if end_pivot is None:
        # CHOCH: пик импульса часто после бара следующего BOS (``broken_idx`` cap
        # отрезал бы его — см. unit ``test_confirmed_short_impulse_resets``).
        post_break_before = (
            None if br.kind == "CHOCH" else next_same_dir_break_idx
        )
        end_pivot = _first_pivot_after_breaking_threshold(
            pivots,
            after_idx=max(br.broken_idx, after_end_idx),
            kind=end_kind,
            direction=br.direction,
            threshold_price=br.swing_price,
            before_idx=post_break_before,
        )
    # 3) Fallback на ``swing pivot`` (когда ни до, ни после нет более глубокого
    #    экстремума — например, флэт после break без явного нового пика).
    if end_pivot is None:
        end_pivot = _last_pivot_before(
            pivots, before_idx=br.broken_idx, min_idx=min_idx, kind=end_kind
        )
        if end_pivot is not None and after_end_idx >= 0 and end_pivot.idx <= after_end_idx:
            end_pivot = None
        if (
            br.kind == "CHOCH"
            and end_pivot is not None
            and end_pivot.idx <= br.swing_idx
        ):
            end_pivot = None
    if end_pivot is None and df is not None:
        end_pivot = _end_pivot_from_break_bar(df, br, after_end_idx=after_end_idx)
    if df is not None:
        if br.kind == "CHOCH":
            choch_end_invalid = (
                end_pivot is None
                or end_pivot.idx <= br.swing_idx
                or (
                    br.direction == "LONG"
                    and end_pivot.price <= br.swing_price
                )
                or (
                    br.direction == "SHORT"
                    and end_pivot.price >= br.swing_price
                )
            )
            df_peak = _end_peak_from_df_after_break(
                df,
                br,
                before_idx=next_same_dir_break_idx,
                min_bar_idx=max(min_end_idx, br.broken_idx),
            )
            if df_peak is not None:
                if choch_end_invalid:
                    end_pivot = df_peak
                elif (
                    br.direction == "LONG"
                    and df_peak.price > end_pivot.price
                ) or (
                    br.direction == "SHORT"
                    and df_peak.price < end_pivot.price
                ):
                    end_pivot = df_peak
        elif br.kind == "BOS" and end_pivot is not None and end_pivot.idx < br.broken_idx:
            break_bar = _end_pivot_from_break_bar(df, br, after_end_idx=after_end_idx)
            if break_bar is not None and (
                (br.direction == "LONG" and break_bar.price > end_pivot.price)
                or (br.direction == "SHORT" and break_bar.price < end_pivot.price)
            ):
                end_pivot = break_bar
    if end_pivot is None:
        return None

    # Структурный start (CHOCH = разворот тренда): самый «крайний» pivot в
    # сегменте от последнего противоположного break до end_pivot.idx. Красная
    # диагональ SHORT идёт от пика (HH), не от последнего корректирующего LH
    # (см. INJUSDT 1H 19.05-21.05.26 в баг-репорте). Для BOS (continuation в
    # том же тренде) логика прежняя — последний LOCAL HL/LH из сегмента, иначе
    # каждая нога в трендовом канале анкорилась бы на одно и то же дно/пик.
    # Без подтверждённого предыдущего противоположного break (флипа сегмента)
    # «структурный» extreme может оказаться из давно неактуального прошлого —
    # тогда тоже падаем на local pivot.
    start_pivot: Pivot | None = None
    if br.kind == "CHOCH" and previous_opposite_broken_idx >= 0:
        start_pivot = _structural_start_pivot(
            pivots,
            direction=br.direction,
            before_idx=end_pivot.idx,
            after_idx=previous_opposite_broken_idx - 1,
        )
    if start_pivot is None:
        start_pivot = _last_pivot_before(
            pivots,
            before_idx=end_pivot.idx,
            min_idx=min_idx,
            kind=start_kind,
            label=preferred_start_label,
        )
    if start_pivot is None and min_idx >= 0 and previous_opposite_broken_idx >= 0:
        # Если после предыдущего same-direction break ещё не сформировался
        # новый HL/LH, не «глушим» ногу полностью: берём ближайший подтверждённый
        # корректирующий pivot до ``end``. Это нужно для последовательных BOS,
        # где PREPARE должен строиться от актуального retrace, даже если новый
        # HL/LH подтвердится позже.
        start_pivot = _last_pivot_before(
            pivots,
            before_idx=end_pivot.idx,
            min_idx=-1,
            kind=start_kind,
            label=preferred_start_label,
        )
    if start_pivot is None and min_idx < 0:
        start_pivot = _last_pivot_before(
            pivots, before_idx=end_pivot.idx, min_idx=min_idx, kind=start_kind
        )
    if start_pivot is None:
        return None

    if br.direction == "LONG" and end_pivot.price <= start_pivot.price:
        return None
    if br.direction == "SHORT" and end_pivot.price >= start_pivot.price:
        return None

    return ImpulseLeg(
        direction=br.direction,
        start_idx=start_pivot.idx,
        start_price=start_pivot.price,
        end_idx=end_pivot.idx,
        end_price=end_pivot.price,
        anchor_break_idx=br.broken_idx,
    )


def _fib_half_touched_between(
    df: pd.DataFrame | None,
    leg: ImpulseLeg,
    *,
    after_idx: int,
    before_idx: int,
) -> bool:
    """True, если между ``after_idx`` и ``before_idx`` цена коснулась ``fib_half``."""
    if df is None or df.empty or after_idx >= before_idx:
        return False
    level = leg.fib_half
    sub = df.iloc[after_idx + 1 : before_idx + 1]
    if sub.empty:
        return False
    if leg.direction == "LONG":
        return bool((sub["low"] <= level).any())
    return bool((sub["high"] >= level).any())


def extract_impulse_legs_confirmed(
    pivots: list[Pivot],
    breaks: list[StructureBreak],
    *,
    swing_size: int,
    df: pd.DataFrame | None = None,
) -> list[ImpulseLeg]:
    """Break-driven импульсы: нога подтверждается BOS/CHoCH той же стороны.

    Для каждого BOS/CHoCH строится нога от последних релевантных пивотов до
    ``broken_idx``. Если до следующего пробоя в ту же сторону не было касания
    ``fib_half`` предыдущей ноги, старая нога сбрасывается (reset start/end).

    ``swing_size`` сохранён для обратной совместимости сигнатуры.
    """
    _ = swing_size
    if not breaks or not pivots:
        return []

    sorted_breaks = sorted(breaks, key=lambda b: (b.broken_idx, b.swing_idx))
    confirmed: list[ImpulseLeg] = []
    active_by_dir: dict[str, ImpulseLeg | None] = {"LONG": None, "SHORT": None}
    last_anchor_idx_by_dir: dict[str, int] = {"LONG": -1, "SHORT": -1}
    last_end_idx_by_dir: dict[str, int] = {"LONG": -1, "SHORT": -1}
    last_broken_idx_by_dir: dict[str, int] = {"LONG": -1, "SHORT": -1}
    seen: set[tuple[int, int, str, int]] = set()

    # Граница «после-broken_idx» поиска end для текущего break:
    # - после CHOCH swing следующего BOS — это end текущей ноги → cap = broken_idx;
    # - после BOS swing следующего BOS — уровень, который ещё не end → cap = swing_idx.
    next_same_dir_break_idx_by_pos: list[int | None] = [None] * len(sorted_breaks)
    for i, br in enumerate(sorted_breaks):
        if br.kind not in ("BOS", "CHOCH"):
            continue
        for j in range(i + 1, len(sorted_breaks)):
            nxt = sorted_breaks[j]
            if nxt.kind in ("BOS", "CHOCH") and nxt.direction == br.direction:
                if br.kind == "CHOCH":
                    next_same_dir_break_idx_by_pos[i] = nxt.broken_idx
                else:
                    next_same_dir_break_idx_by_pos[i] = nxt.swing_idx
                break

    for i, br in enumerate(sorted_breaks):
        if br.kind not in ("BOS", "CHOCH"):
            continue

        opposite = "SHORT" if br.direction == "LONG" else "LONG"
        min_idx = last_anchor_idx_by_dir.get(br.direction, -1)
        leg = _build_leg_from_break(
            pivots,
            br,
            min_idx=min_idx,
            next_same_dir_break_idx=next_same_dir_break_idx_by_pos[i],
            min_end_idx=last_end_idx_by_dir.get(br.direction, -1),
            previous_opposite_broken_idx=last_broken_idx_by_dir.get(opposite, -1),
            df=df,
        )
        if leg is None:
            continue

        prev_active = active_by_dir.get(br.direction)
        if prev_active is not None and prev_active.anchor_break_idx is not None:
            touched = _fib_half_touched_between(
                df,
                prev_active,
                after_idx=prev_active.end_idx,
                before_idx=br.broken_idx,
            )
            if not touched:
                confirmed = [
                    existing
                    for existing in confirmed
                    if existing.anchor_break_idx != prev_active.anchor_break_idx
                ]

        key = (leg.start_idx, leg.end_idx, leg.direction, leg.anchor_break_idx or -1)
        if key in seen:
            continue
        seen.add(key)

        confirmed.append(leg)
        active_by_dir[br.direction] = leg
        last_anchor_idx_by_dir[br.direction] = br.broken_idx
        last_end_idx_by_dir[br.direction] = leg.end_idx
        last_broken_idx_by_dir[br.direction] = br.broken_idx

    return confirmed


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
    prev_high_label: str | None = None
    prev_low_label: str | None = None
    last_high_pivot: float | None = None
    last_low_pivot: float | None = None
    prev_high_idx = -1
    prev_low_idx = -1
    high_active = False
    low_active = False
    prev_break_dir = 0  # 1=last break was UP, -1=DOWN, 0=none yet
    # «Замороженные» уровни последних пробоев внутри ТЕКУЩЕЙ серии направления:
    # в том же направлении новый swing должен СТРОГО превзойти пробитый уровень,
    # иначе плоские повторные пивоты на той же отметке дадут дубликат break.
    # При смене серии (противоположный break) ограничение на эту сторону
    # ослабляем: иначе CHOCH по LH/HL (ниже/выше прошлого экстремума) потеряется.
    last_broken_high: float | None = None
    last_broken_low: float | None = None

    breaks: list[StructureBreak] = []
    for i in range(n):
        cand = i - swing_size
        if cand >= swing_size:
            window_hi = highs[cand - swing_size : cand + swing_size + 1]
            if math.isfinite(float(highs[cand])) and float(highs[cand]) == window_hi.max():
                price = float(highs[cand])
                if (
                    last_broken_high is None
                    or prev_break_dir != 1
                    or price > last_broken_high
                ):
                    # В LONG-тренде активный (ещё непробитый) prev_high — это
                    # структурный HH, и его нельзя «переписывать» более низким
                    # внутренним HIGH-пивотом. Иначе BOS LONG срабатывает на
                    # промежуточном уровне, до того как цена реально пробьёт
                    # структурный максимум. Симметрично — для prev_low в SHORT.
                    same_dir_locked_high = (
                        prev_break_dir == 1
                        and high_active
                        and prev_high is not None
                        and price < prev_high
                    )
                    if not same_dir_locked_high:
                        prev_high_label = (
                            "HH" if (last_high_pivot is None or price >= last_high_pivot) else "LH"
                        )
                        last_high_pivot = price
                        prev_high = price
                        prev_high_idx = cand
                        high_active = True
            window_lo = lows[cand - swing_size : cand + swing_size + 1]
            if math.isfinite(float(lows[cand])) and float(lows[cand]) == window_lo.min():
                price = float(lows[cand])
                if (
                    last_broken_low is None
                    or prev_break_dir != -1
                    or price < last_broken_low
                ):
                    same_dir_locked_low = (
                        prev_break_dir == -1
                        and low_active
                        and prev_low is not None
                        and price > prev_low
                    )
                    if not same_dir_locked_low:
                        prev_low_label = (
                            "HL" if (last_low_pivot is None or price >= last_low_pivot) else "LL"
                        )
                        last_low_pivot = price
                        prev_low = price
                        prev_low_idx = cand
                        low_active = True

        if high_active and prev_high is not None and float(src_hi[i]) > prev_high:
            kind = "CHOCH" if prev_break_dir == -1 else "BOS"
            if prev_high_label == "LH":
                kind = "CHOCH"
            breaks.append(
                StructureBreak(
                    direction="LONG",
                    kind=kind,
                    swing_idx=prev_high_idx,
                    swing_price=prev_high,
                    broken_idx=i,
                )
            )
            last_broken_high = prev_high
            high_active = False
            prev_break_dir = 1
        if low_active and prev_low is not None and float(src_lo[i]) < prev_low:
            kind = "CHOCH" if prev_break_dir == 1 else "BOS"
            if prev_low_label == "HL":
                kind = "CHOCH"
            breaks.append(
                StructureBreak(
                    direction="SHORT",
                    kind=kind,
                    swing_idx=prev_low_idx,
                    swing_price=prev_low,
                    broken_idx=i,
                )
            )
            last_broken_low = prev_low
            low_active = False
            prev_break_dir = -1

    return breaks


def latest_impulse_leg(
    pivots: list[Pivot],
    *,
    direction: str | None = None,
    breaks: list[StructureBreak] | None = None,
    swing_size: int = 1,
    df: pd.DataFrame | None = None,
) -> ImpulseLeg | None:
    """Последний impulse leg в указанном направлении (или вообще последний).

    Если передан ``breaks``, берутся только ноги с подтверждающим BOS/CHoCH
    (``extract_impulse_legs_confirmed``).
    """
    if breaks is not None:
        legs = extract_impulse_legs_confirmed(
            pivots, breaks, swing_size=swing_size, df=df
        )
    else:
        legs = extract_impulse_legs(pivots)
    if direction is not None:
        legs = [leg for leg in legs if leg.direction == direction]
    return legs[-1] if legs else None


@dataclass(slots=True, frozen=True)
class ImpulseLockState:
    """Фаза ретрейса после подтверждения пика импульса (HH / LL).

    Пока цена между ``start_price`` и ``end_price`` без обновления границ,
    внутренние пивоты и BOS/CHoCH не эмитятся — только после пробоя HL
    (обновление минимума) или HH (обновление максимума) импульса.
    """

    leg: ImpulseLeg
    lock_from_idx: int
    broken_start_idx: int  # первый бар с пробоем start (−1 = ещё нет)
    broken_end_idx: int  # первый бар с пробоем end (−1 = ещё нет)


def first_impulse_boundary_break_idxs(
    df: pd.DataFrame,
    leg: ImpulseLeg,
    *,
    after_idx: int,
    use_close: bool = True,
) -> tuple[int, int]:
    """Индексы первого пробоя ``start_price`` и ``end_price`` после ``after_idx``.

    LONG: start = close/low < HL (как BOS при ``use_close``), end = close/high > HH.
    SHORT: start = close/high > LH, end = close/low < LL.
    Возвращает ``(-1, -1)``, если пробоев ещё не было.
    """
    last_idx = int(df.index[-1])
    if after_idx >= last_idx:
        return -1, -1
    broken_start = -1
    broken_end = -1
    for idx in df.index[after_idx + 1 : last_idx + 1]:
        i = int(idx)
        if leg.direction == "LONG":
            start_src = float(df.loc[i, "close"]) if use_close else float(df.loc[i, "low"])
            if broken_start < 0 and start_src < leg.start_price:
                broken_start = i
            end_src = float(df.loc[i, "close"]) if use_close else float(df.loc[i, "high"])
            if broken_end < 0 and end_src > leg.end_price:
                broken_end = i
        else:
            start_src = float(df.loc[i, "close"]) if use_close else float(df.loc[i, "high"])
            if broken_start < 0 and start_src > leg.start_price:
                broken_start = i
            end_src = float(df.loc[i, "close"]) if use_close else float(df.loc[i, "low"])
            if broken_end < 0 and end_src < leg.end_price:
                broken_end = i
    return broken_start, broken_end


def compute_impulse_lock_state(
    df: pd.DataFrame,
    pivots: list[Pivot],
    *,
    swing_size: int,
    use_close: bool = True,
    breaks: list[StructureBreak] | None = None,
) -> ImpulseLockState | None:
    """Состояние HTF-lock для последнего импульса, если идёт ретрейс без пробоя границ.

    ``None`` — lock не активен (импульс ещё строится, или обе границы уже обновлены).

    Передайте ``breaks`` (сырой ``extract_structure_breaks``), чтобы lock строился
    только на импульсах, подтверждённых BOS/CHoCH.
    """
    leg = latest_impulse_leg(pivots, breaks=breaks, swing_size=swing_size, df=df)
    if leg is None:
        return None
    confirm_idx = leg.anchor_break_idx if leg.anchor_break_idx is not None else leg.end_idx
    lock_from_idx = max(leg.end_idx + swing_size, confirm_idx + 1)
    last_idx = int(df.index[-1])
    if last_idx < lock_from_idx:
        return None
    broken_start, broken_end = first_impulse_boundary_break_idxs(
        df, leg, after_idx=leg.end_idx, use_close=use_close
    )
    if broken_start >= 0 and broken_end >= 0:
        return None
    if broken_start < 0 and broken_end < 0:
        return ImpulseLockState(
            leg=leg,
            lock_from_idx=lock_from_idx,
            broken_start_idx=-1,
            broken_end_idx=-1,
        )
    return ImpulseLockState(
        leg=leg,
        lock_from_idx=lock_from_idx,
        broken_start_idx=broken_start,
        broken_end_idx=broken_end,
    )


def impulse_leg_anchor_idxs(legs: list[ImpulseLeg]) -> set[int]:
    """Бары HL/LH и HH/LL всех импульсных ног — всегда показываем на overlay."""
    out: set[int] = set()
    for leg in legs:
        out.add(leg.start_idx)
        out.add(leg.end_idx)
    return out


def impulse_leg_for_retracement_pivot(
    pivot: Pivot,
    legs: list[ImpulseLeg],
) -> ImpulseLeg | None:
    """Импульс, чей ретрейс после HH/LL содержит этот пивот.

    Берём последнюю ногу с ``end_idx < pivot.idx`` в том же направлении,
    что и тренд (LOW-пивот → LONG-импульсы). Иначе откат после 1-го HH
    ошибочно сравнивают с HL уже 2-го импульса.
    """
    if pivot.kind == "LOW":
        direction = "LONG"
    elif pivot.kind == "HIGH":
        direction = "SHORT"
    else:
        return None
    candidates = [
        leg for leg in legs if leg.direction == direction and leg.end_idx < pivot.idx
    ]
    return candidates[-1] if candidates else None


def _reference_leg_for_pivot(
    pivot: Pivot,
    state: ImpulseLockState,
    impulse_legs: list[ImpulseLeg],
) -> ImpulseLeg:
    ref = impulse_leg_for_retracement_pivot(pivot, impulse_legs)
    return ref if ref is not None else state.leg


def pivot_label_for_htf_display(
    pivot: Pivot,
    state: ImpulseLockState | None,
    *,
    impulse_legs: list[ImpulseLeg] | None = None,
) -> str:
    """Метка на графике: low выше HL импульса не может быть LL."""
    if state is None:
        return pivot.label
    # Экстремум текущей SHORT-импульсной ноги должен отображаться как LL.
    # Это устраняет кейс, когда LL на конце short-ноги визуально становился HL.
    if pivot.idx == state.leg.end_idx:
        if state.leg.direction == "SHORT" and pivot.kind == "LOW":
            return "LL"
    # После подтверждённого пробоя end-уровня импульса (BOS по направлению ноги)
    # low/high противоположного типа не должны перекрашиваться «старой» ногой.
    # Иначе в SHORT-режиме low ниже LL-экстремума иногда остаётся HL.
    if state.leg.direction == "SHORT" and pivot.kind == "LOW" and pivot.idx > state.leg.end_idx:
        if (
            state.broken_end_idx >= 0
            and pivot.idx >= state.broken_end_idx
            and pivot.price <= state.leg.end_price
        ):
            return "LL"
        return pivot.label
    if state.leg.direction == "LONG" and pivot.kind == "HIGH" and pivot.idx > state.leg.end_idx:
        if (
            state.broken_end_idx >= 0
            and pivot.idx >= state.broken_end_idx
            and pivot.price >= state.leg.end_price
        ):
            return "HH"
        return pivot.label
    # После слома HL/LH — новая структура, не перекрашиваем low/high старым импульсом.
    if state.broken_start_idx >= 0:
        if state.leg.direction == "LONG" and pivot.kind == "LOW" and pivot.idx >= state.broken_start_idx:
            return pivot.label
        if state.leg.direction == "SHORT" and pivot.kind == "HIGH" and pivot.idx >= state.broken_start_idx:
            return pivot.label
    # В lock-фазе ретрейса (HL/LH импульса ещё не пробит) LOW/HIGH не могут
    # стать LL/HH — иначе на overlay появляется LL до CHOCH (INJUSDT 1H 15.05.26).
    if state.broken_start_idx < 0 and pivot.idx > state.leg.end_idx:
        if state.leg.direction == "LONG" and pivot.kind == "LOW":
            return "HL"
        if state.leg.direction == "SHORT" and pivot.kind == "HIGH":
            return "LH"
    legs = impulse_legs or []
    ref = impulse_leg_for_retracement_pivot(pivot, legs) or state.leg
    if ref.direction == "LONG" and pivot.kind == "LOW" and pivot.price >= ref.start_price:
        return "HL"
    if ref.direction == "SHORT" and pivot.kind == "HIGH" and pivot.price <= ref.start_price:
        return "LH"
    return pivot.label


def _pivot_allowed_under_impulse_lock(
    pivot: Pivot,
    state: ImpulseLockState,
    *,
    anchor_idxs: set[int],
    impulse_legs: list[ImpulseLeg],
) -> bool:
    if pivot.idx in anchor_idxs:
        return True
    if pivot.idx <= state.leg.end_idx:
        return True
    ref = _reference_leg_for_pivot(pivot, state, impulse_legs)
    if ref.direction == "LONG":
        if pivot.kind == "LOW":
            if pivot.price >= ref.start_price:
                return True
            return (
                state.broken_start_idx >= 0 and pivot.idx >= state.broken_start_idx
            )
        if pivot.kind == "HIGH":
            # После слома HL — коррекционные LH (high ниже HH).
            if (
                state.broken_start_idx >= 0
                and pivot.idx >= state.broken_start_idx
                and pivot.price < ref.end_price
            ):
                return True
            if state.broken_end_idx < 0 or pivot.idx < state.broken_end_idx:
                return False
            return pivot.price > ref.end_price
    else:
        if pivot.kind == "HIGH":
            if pivot.price <= ref.start_price:
                return True
            return (
                state.broken_start_idx >= 0 and pivot.idx >= state.broken_start_idx
            )
        if pivot.kind == "LOW":
            if (
                state.broken_start_idx >= 0
                and pivot.idx >= state.broken_start_idx
                and pivot.price > ref.end_price
            ):
                return True
            if state.broken_end_idx < 0 or pivot.idx < state.broken_end_idx:
                return False
            return pivot.price < ref.end_price
    return False


def _is_valid_retrace_for_leg(pivot: Pivot, leg: ImpulseLeg) -> bool:
    """True если pivot — валидный коррекционный pivot для ноги (между start и end)."""
    if leg.direction == "LONG" and pivot.kind == "LOW":
        return leg.start_price <= pivot.price < leg.end_price
    if leg.direction == "SHORT" and pivot.kind == "HIGH":
        return leg.end_price < pivot.price <= leg.start_price
    return False


def _deepest_retrace_idxs_per_leg(
    pivots: list[Pivot],
    legs: list[ImpulseLeg],
    *,
    anchors: set[int],
) -> set[int]:
    """Самый глубокий коррекционный pivot для каждой ноги (1 retrace-метка на импульс).

    Pivot привязывается к ноге через ``impulse_leg_for_retracement_pivot``
    (последняя нога того же направления, закончившаяся до бара pivot).
    Внутри группы выбирается deepest: min по цене для LONG, max для SHORT.
    """
    if not legs:
        return set()
    candidates_by_leg: dict[int, list[Pivot]] = {}
    for pivot in pivots:
        if pivot.idx in anchors:
            continue
        ref = impulse_leg_for_retracement_pivot(pivot, legs)
        if ref is None:
            continue
        if not _is_valid_retrace_for_leg(pivot, ref):
            continue
        candidates_by_leg.setdefault(ref.end_idx, []).append(pivot)

    leg_by_end: dict[int, ImpulseLeg] = {leg.end_idx: leg for leg in legs}
    deepest_idxs: set[int] = set()
    for end_idx, group in candidates_by_leg.items():
        leg = leg_by_end.get(end_idx)
        if leg is None or not group:
            continue
        if leg.direction == "LONG":
            deepest = min(group, key=lambda p: p.price)
        else:
            deepest = max(group, key=lambda p: p.price)
        deepest_idxs.add(deepest.idx)
    return deepest_idxs


def filter_pivots_by_impulse_lock(
    pivots: list[Pivot],
    state: ImpulseLockState | None,
    *,
    impulse_legs: list[ImpulseLeg] | None = None,
) -> list[Pivot]:
    """Оставить на overlay только anchors импульсов и по 1 deepest-retrace на ногу.

    Каждый подтверждённый импульс представлен ровно тремя метками: ``start``,
    ``end`` (anchors) и самый глубокий коррекционный pivot между его ``end_idx``
    и началом следующей одноимённой ноги (или до конца истории). Лишние
    промежуточные HL/LH между BOS/CHOCH отбрасываются. После инвалидации
    активного импульса (``broken_start_idx >= 0``) дополнительно показываем
    pivots, формирующие новый тренд (LH после слома HL и т.п.).
    """
    legs = impulse_legs if impulse_legs is not None else extract_impulse_legs(pivots)
    if not legs:
        return pivots
    anchors = impulse_leg_anchor_idxs(legs)
    deepest_idxs = _deepest_retrace_idxs_per_leg(pivots, legs, anchors=anchors)
    if state is None:
        first_anchor_idx = min(anchors) if anchors else -1
        out: list[Pivot] = []
        for pivot in pivots:
            if first_anchor_idx >= 0 and pivot.idx < first_anchor_idx:
                out.append(pivot)
                continue
            if pivot.idx in anchors or pivot.idx in deepest_idxs:
                out.append(pivot)
        return out

    out: list[Pivot] = []
    for pivot in pivots:
        if pivot.idx in anchors:
            out.append(pivot)
            continue
        if pivot.idx in deepest_idxs:
            out.append(pivot)
            continue
        if state.broken_start_idx >= 0 and pivot.idx >= state.broken_start_idx:
            if _pivot_allowed_under_impulse_lock(
                pivot, state, anchor_idxs=anchors, impulse_legs=legs
            ):
                out.append(pivot)
            continue
    return out


def _structure_break_allowed_under_impulse_lock(
    br: StructureBreak,
    state: ImpulseLockState,
) -> bool:
    if br.broken_idx <= state.leg.end_idx:
        return True
    if (
        state.leg.anchor_break_idx is not None
        and br.direction == state.leg.direction
        and br.broken_idx < state.leg.anchor_break_idx
    ):
        return False
    opposite = "SHORT" if state.leg.direction == "LONG" else "LONG"
    if state.broken_start_idx < 0 and state.broken_end_idx < 0:
        # Во время ретрейса после импульса разрешаем только явный CHOCH по
        # ближайшему коррекционному HL/LH (свинг после пика импульса), чтобы
        # не пропускать смену тренда до пробоя стартового HL/LH импульса.
        if br.direction == opposite and br.kind == "CHOCH" and br.swing_idx > state.leg.end_idx:
            if state.leg.direction == "LONG":
                return br.swing_price > state.leg.start_price
            return br.swing_price < state.leg.start_price
        return False
    if br.direction == opposite:
        return state.broken_start_idx >= 0 and br.broken_idx >= state.broken_start_idx
    if br.direction == state.leg.direction:
        return state.broken_end_idx >= 0 and br.broken_idx >= state.broken_end_idx
    return False


def filter_structure_breaks_by_impulse_lock(
    breaks: list[StructureBreak],
    state: ImpulseLockState | None,
) -> list[StructureBreak]:
    if state is None:
        return breaks
    return [b for b in breaks if _structure_break_allowed_under_impulse_lock(b, state)]


def impulse_invalidation_structure_break(
    state: ImpulseLockState | None,
) -> StructureBreak | None:
    """Синтетический CHoCH на баре пробоя HL/LH импульса."""
    if state is None or state.broken_start_idx < 0:
        return None
    leg = state.leg
    choch_dir = "SHORT" if leg.direction == "LONG" else "LONG"
    return StructureBreak(
        direction=choch_dir,
        kind="CHOCH",
        swing_idx=leg.start_idx,
        swing_price=leg.start_price,
        broken_idx=state.broken_start_idx,
    )


def apply_impulse_invalidation_choch(
    breaks: list[StructureBreak],
    state: ImpulseLockState | None,
) -> list[StructureBreak]:
    """Пробой HL/LH импульса = CHoCH на уровне ``start`` импульса, не BOS по внутреннему low."""
    if state is None or state.broken_start_idx < 0:
        return breaks
    leg = state.leg
    inv = state.broken_start_idx
    choch_dir = "SHORT" if leg.direction == "LONG" else "LONG"
    choch = impulse_invalidation_structure_break(state)
    assert choch is not None
    # Убираем все контр-пробои после пика импульса и до/на inv — иначе reclassify
    # может превратить synthetic CHoCH в BOS (если в списке уже есть SHORT/LONG
    # пробой на том же баре, но от внутреннего low/high, а не от HL/LH импульса).
    start_after_peak = leg.end_idx + 1
    kept = [
        b
        for b in breaks
        if not (
            b.direction == choch_dir
            and start_after_peak <= b.broken_idx <= inv
        )
    ]
    kept.append(choch)
    kept.sort(key=lambda b: (b.broken_idx, b.swing_idx))
    return kept


def reclassify_structure_break_kinds(
    breaks: list[StructureBreak],
) -> list[StructureBreak]:
    """Пересчёт BOS/CHoCH только по **видимым** пробоям (после impulse-lock).

    Скрытые внутренние пробои не должны сдвигать ``prev_break_dir``, иначе
    первый пробой HL после LONG-импульса ошибочно становится BOS SHORT.
    """
    prev_dir = 0
    out: list[StructureBreak] = []
    for br in breaks:
        if br.direction == "LONG":
            kind = "CHOCH" if prev_dir == -1 else "BOS"
            prev_dir = 1
        else:
            kind = "CHOCH" if prev_dir == 1 else "BOS"
            prev_dir = -1
        if kind == br.kind:
            out.append(br)
        else:
            out.append(
                StructureBreak(
                    direction=br.direction,
                    kind=kind,
                    swing_idx=br.swing_idx,
                    swing_price=br.swing_price,
                    broken_idx=br.broken_idx,
                )
            )
    return out


def reanchor_choch_to_structural_swing(
    breaks: list[StructureBreak],
    pivots: list[Pivot],
    df: pd.DataFrame,
    *,
    use_close: bool = True,
) -> list[StructureBreak]:
    """Отбрасывает CHOCH по internal HL/LH без пробоя структурного swing.

    Если swing CHOCH выше (SHORT) / ниже (LONG) структурного экстремума
    сегмента, flip засчитывается только при close ниже/выше структурного
    уровня. Сохранённые CHOCH не меняют swing — это важно для collapsed
    probe+confirm (см. INJUSDT 1H 15.05.26).
    """
    if not breaks or df.empty:
        return breaks
    src_hi = df["close"].to_numpy() if use_close else df["high"].to_numpy()
    src_lo = df["close"].to_numpy() if use_close else df["low"].to_numpy()
    out: list[StructureBreak] = []
    for br_pos, br in enumerate(breaks):
        if br.kind != "CHOCH":
            out.append(br)
            continue
        last_opposite_broken = -1
        for prev in reversed(breaks[:br_pos]):
            if prev.direction != br.direction:
                last_opposite_broken = prev.broken_idx
                break
        if br.direction == "SHORT":
            candidates = [
                p
                for p in pivots
                if p.kind == "LOW" and last_opposite_broken < p.idx < br.broken_idx
            ]
            if candidates:
                structural = min(candidates, key=lambda p: (p.price, p.idx))
                if (
                    structural.price < br.swing_price - 1e-12
                    and float(src_lo[br.broken_idx]) < br.swing_price
                    and float(src_lo[br.broken_idx]) >= structural.price
                ):
                    continue
        else:
            candidates = [
                p
                for p in pivots
                if p.kind == "HIGH" and last_opposite_broken < p.idx < br.broken_idx
            ]
            if candidates:
                structural = max(candidates, key=lambda p: (p.price, -p.idx))
                if (
                    structural.price > br.swing_price + 1e-12
                    and float(src_hi[br.broken_idx]) > br.swing_price
                    and float(src_hi[br.broken_idx]) <= structural.price
                ):
                    continue
        out.append(br)
    return out


def collapse_early_trend_flip_probe(
    breaks: list[StructureBreak],
    *,
    swing_size: int,
) -> list[StructureBreak]:
    """Схлопывает ранний CHOCH-пробой перед первым валидным flip-break.

    Паттерн:
    - ``CHOCH`` в новую сторону;
    - следом ``BOS`` в ту же сторону в пределах ``2 * swing_size`` баров;
    - swing второго пробоя сформирован уже после первого (``cur.broken_idx < nxt.swing_idx``).

    В таком случае первый пробой считаем преждевременным «probe» и удаляем.
    После удаления выполняется обычный ``reclassify_structure_break_kinds``.
    """
    if len(breaks) < 2:
        return breaks
    out: list[StructureBreak] = []
    i = 0
    max_gap = max(1, 2 * swing_size)
    while i < len(breaks):
        cur = breaks[i]
        if i + 1 < len(breaks):
            nxt = breaks[i + 1]
            if (
                cur.kind == "CHOCH"
                and nxt.kind == "BOS"
                and cur.direction == nxt.direction
                and 0 <= (nxt.broken_idx - cur.broken_idx) <= max_gap
                and cur.broken_idx < nxt.swing_idx
            ):
                # Объединяем пару в один flip на более позднем bar_open, но с
                # уровнем CHOCH от исходного HL/LH (probe-бар задаёт корректный
                # structure level, поздний BOS — момент подтверждения).
                out.append(
                    StructureBreak(
                        direction=nxt.direction,
                        kind="CHOCH",
                        swing_idx=cur.swing_idx,
                        swing_price=cur.swing_price,
                        broken_idx=nxt.broken_idx,
                    )
                )
                i += 2
                continue
        out.append(cur)
        i += 1
    return out


def first_correction_pivot_confirm_idx(
    pivots: list[Pivot],
    *,
    invalidation_idx: int,
    new_trend_direction: str,
    swing_size: int,
) -> int:
    """Бар подтверждения первого LH (после слома HL) или HL (после слома LH)."""
    want_kind = "HIGH" if new_trend_direction == "SHORT" else "LOW"
    for p in pivots:
        if p.idx <= invalidation_idx or p.kind != want_kind:
            continue
        return p.idx + swing_size
    return -1


def prepare_suppressed_after_trend_flip(
    *,
    df: pd.DataFrame,
    raw_pivots: list[Pivot],
    swing_size: int,
    use_close: bool,
    setup_direction: str,
    last_pos: int,
) -> bool:
    """Блок PREPARE в новую сторону до LH/HL и на баре подтверждения LH/HL.

    Решает: P SHORT на отскоке (reversal/continuation по 0.5 старого импульса)
    вместо метки LH на пике.
    """
    breaks_raw = extract_structure_breaks(
        df, swing_size=swing_size, use_close=use_close
    )
    state = compute_impulse_lock_state(
        df,
        raw_pivots,
        swing_size=swing_size,
        use_close=use_close,
        breaks=breaks_raw,
    )
    if state is None or state.broken_start_idx < 0:
        return False
    new_dir = "SHORT" if state.leg.direction == "LONG" else "LONG"
    if setup_direction != new_dir:
        return False
    confirm = first_correction_pivot_confirm_idx(
        raw_pivots,
        invalidation_idx=state.broken_start_idx,
        new_trend_direction=new_dir,
        swing_size=swing_size,
    )
    if confirm < 0:
        return True
    # На баре подтверждения первого LH/HL разрешаем PREPARE: если в этот же бар
    # случился первый валидный touch 0.5, сигнал не должен теряться.
    return last_pos < confirm


def latest_choch_break(
    breaks: list[StructureBreak],
    df: pd.DataFrame,
    pivots: list[Pivot],
    *,
    swing_size: int,
    use_close: bool,
    max_bars_ago: int | None,
    last_idx: int | None,
) -> StructureBreak | None:
    """Последний CHoCH, включая синтетический при пробое HL/LH импульса."""
    choch = latest_structure_break(
        breaks,
        kinds=("CHOCH",),
        max_bars_ago=max_bars_ago,
        last_idx=last_idx,
    )
    if choch is not None:
        return choch
    breaks_raw = extract_structure_breaks(
        df, swing_size=swing_size, use_close=use_close
    )
    state = compute_impulse_lock_state(
        df,
        pivots,
        swing_size=swing_size,
        use_close=use_close,
        breaks=breaks_raw,
    )
    inv = impulse_invalidation_structure_break(state)
    if inv is None:
        return None
    if max_bars_ago is not None and last_idx is not None:
        if (last_idx - inv.broken_idx) > max_bars_ago:
            return None
    return inv


def prepare_suppressed_during_impulse_lock(
    df: pd.DataFrame,
    raw_pivots: list[Pivot],
    *,
    swing_size: int,
    use_close: bool,
    setup_direction: str,
) -> bool:
    """Блокируем контртрендовый PREPARE в ретрейсе импульса (P SHORT до слома HL)."""
    breaks_raw = extract_structure_breaks(
        df, swing_size=swing_size, use_close=use_close
    )
    state = compute_impulse_lock_state(
        df,
        raw_pivots,
        swing_size=swing_size,
        use_close=use_close,
        breaks=breaks_raw,
    )
    if state is None:
        return False
    if state.broken_start_idx >= 0:
        return False
    if state.broken_end_idx >= 0:
        return False
    return setup_direction != state.leg.direction


def detect_pivots_htf(
    df: pd.DataFrame,
    swing_size: int,
    *,
    use_close: bool = True,
    impulse_lock: bool = True,
) -> list[Pivot]:
    """Пивоты с опциональным HTF impulse-lock (ретрейс без внутреннего шума).

    ``legs`` для anchors берём из ВИДИМЫХ breaks (включая synthetic CHOCH при
    инвалидации импульса) — иначе HH/LL-конец ноги, подтверждённой пробитием
    стартового HL/LH, теряется на overlay'е. Для самого ``state`` (lock-фаза)
    используем raw_breaks, чтобы synthetic CHOCH не зацикливался (он строится
    ИЗ state).
    """
    pivots = detect_pivots(df, swing_size=swing_size)
    if not impulse_lock:
        return pivots
    breaks_raw = extract_structure_breaks(
        df, swing_size=swing_size, use_close=use_close
    )
    visible_breaks = extract_structure_breaks_htf(
        df, swing_size=swing_size, use_close=use_close, impulse_lock=True
    )
    legs = extract_impulse_legs_confirmed(
        pivots, visible_breaks, swing_size=swing_size, df=df
    )
    state = compute_impulse_lock_state(
        df,
        pivots,
        swing_size=swing_size,
        use_close=use_close,
        breaks=breaks_raw,
    )
    return filter_pivots_by_impulse_lock(pivots, state, impulse_legs=legs)


def extract_structure_breaks_htf(
    df: pd.DataFrame,
    swing_size: int,
    *,
    use_close: bool = True,
    impulse_lock: bool = True,
) -> list[StructureBreak]:
    """BOS/CHoCH с опциональным HTF impulse-lock."""
    breaks = extract_structure_breaks(df, swing_size=swing_size, use_close=use_close)
    if not impulse_lock:
        return breaks

    def _postprocess_visible(
        visible_breaks: list[StructureBreak],
        state_local: ImpulseLockState | None,
    ) -> list[StructureBreak]:
        reclassified_local = reclassify_structure_break_kinds(visible_breaks)
        collapsed_local = collapse_early_trend_flip_probe(
            reclassified_local, swing_size=swing_size
        )
        if len(collapsed_local) != len(reclassified_local):
            reclassified_local = reclassify_structure_break_kinds(collapsed_local)
        else:
            reclassified_local = collapsed_local
        reclassified_local = reanchor_choch_to_structural_swing(
            reclassified_local, pivots, df, use_close=use_close
        )
        inv_local = impulse_invalidation_structure_break(state_local)
        if inv_local is None:
            return reclassified_local
        # Зафиксировать CHOCH именно от HL/LH ноги на баре инвалидации:
        # reclassify может ретроспективно перевесить его в BOS.
        out_local = [
            b
            for b in reclassified_local
            if not (
                b.direction == inv_local.direction
                and b.broken_idx == inv_local.broken_idx
            )
        ]
        out_local.append(inv_local)
        out_local.sort(key=lambda b: (b.broken_idx, b.swing_idx))
        return out_local

    pivots = detect_pivots(df, swing_size=swing_size)
    state = compute_impulse_lock_state(
        df,
        pivots,
        swing_size=swing_size,
        use_close=use_close,
        breaks=breaks,
    )
    if state is None:
        return _postprocess_visible(breaks, None)
    filtered = filter_structure_breaks_by_impulse_lock(breaks, state)
    with_invalidation = apply_impulse_invalidation_choch(filtered, state)
    # После фильтрации/подмешивания synthetic CHOCH пересчитываем и нормализуем
    # видимые BOS/CHOCH по порядку событий.
    return _postprocess_visible(with_invalidation, state)


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


def filter_causal_structure_breaks(
    breaks: list[StructureBreak],
    df: pd.DataFrame,
    *,
    swing_size: int,
    use_close: bool = True,
    impulse_lock: bool = True,
    max_bars_ago: int | None = None,
    last_idx: int | None = None,
) -> list[StructureBreak]:
    """Оставляет только пробои, которые были видимы на своём баре.

    Некоторые CHOCH/BOS в HTF impulse-lock могут появляться ретроспективно при
    пересчёте на более длинном хвосте. Такие события не должны якорить PREPARE.
    """
    if not breaks:
        return []
    if df is None or df.empty:
        return []

    candidate = breaks
    if max_bars_ago is not None and last_idx is not None:
        candidate = [b for b in candidate if (last_idx - b.broken_idx) <= max_bars_ago]
        if not candidate:
            return []

    # Сравниваем без поля ``kind``: повторный пересчёт impulse-lock на полном
    # хвосте может перевешивать BOS↔CHOCH (через ``reclassify_structure_break_kinds``),
    # но сам факт пробоя — момент закрытия свечи — был виден в реальном времени.
    # Иначе causal-filter на полностью валидном break теряет его из-за смены kind.
    visible_cache: dict[int, set[tuple[int, int, str]]] = {}
    out: list[StructureBreak] = []
    for br in candidate:
        broken_idx = int(br.broken_idx)
        if broken_idx < 0 or broken_idx >= len(df):
            continue
        visible = visible_cache.get(broken_idx)
        if visible is None:
            df_prefix = df.iloc[: broken_idx + 1]
            prefix_breaks = extract_structure_breaks_htf(
                df_prefix,
                swing_size=swing_size,
                use_close=use_close,
                impulse_lock=impulse_lock,
            )
            visible = {
                (int(b.broken_idx), int(b.swing_idx), b.direction)
                for b in prefix_breaks
            }
            visible_cache[broken_idx] = visible
        key = (int(br.broken_idx), int(br.swing_idx), br.direction)
        if key in visible:
            out.append(br)
    return out


def opposite_structure_break_since_open_ms(
    breaks: list[StructureBreak],
    df: pd.DataFrame,
    *,
    setup_direction: str,
    since_open_ms: int,
    kinds: tuple[str, ...] = ("BOS", "CHOCH"),
) -> StructureBreak | None:
    """Последний противоположный BOS/CHoCH после ``since_open_ms``.

    Используется для инвалидации ARMED-сетапа: если после PREPARE на его HTF
    пришла структура в противоположную сторону, вход в старом направлении
    запрещаем.
    """
    opposite = "SHORT" if setup_direction == "LONG" else "LONG"
    return structure_break_since_open_ms(
        breaks,
        df,
        since_open_ms=since_open_ms,
        direction=opposite,
        kinds=kinds,
        strict_after=False,
    )


def structure_break_since_open_ms(
    breaks: list[StructureBreak],
    df: pd.DataFrame,
    *,
    since_open_ms: int,
    direction: str | None = None,
    kinds: tuple[str, ...] = ("BOS", "CHOCH"),
    strict_after: bool = False,
) -> StructureBreak | None:
    """Последний структурный пробой после ``since_open_ms``.

    ``strict_after=False`` -> ``>= since_open_ms``.
    ``strict_after=True`` -> ``> since_open_ms``.

    Если ``direction`` задан, фильтрует по направлению.
    """
    for br in reversed(breaks):
        if br.kind not in kinds:
            continue
        if direction is not None and br.direction != direction:
            continue
        try:
            broken_open_ms = int(df.iloc[br.broken_idx]["open_time"])
        except (KeyError, IndexError):
            continue
        if strict_after:
            if broken_open_ms > since_open_ms:
                return br
            continue
        if broken_open_ms >= since_open_ms:
            return br
    return None


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
    anchor_break_idx: int | None = None,
) -> tuple[int, int] | None:
    """Бар эмиссии PREPARE на первом касании 0.5 (или после confirm пика).

    Возвращает ``(emission_bar_idx, touch_idx)``, если эмиссия должна быть на
    **текущем** баре df; иначе ``None``.
    """
    last_pos = int(df.index[-1])
    since = leg_end_idx if since_idx is None else since_idx
    touch_idx = find_first_touch_idx(
        df, direction=touch_direction, level=level, since_idx=since
    )
    if touch_idx < 0:
        return None
    emission_bar = prepare_emission_bar_idx(
        leg_end_idx=leg_end_idx,
        anchor_break_idx=anchor_break_idx,
        swing_size=swing_size,
        touch_idx=touch_idx,
    )
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
    # open_time бара пробоя на LTF (нужен для re-entry: требовать новый break).
    broken_open_ms: int | None = None
    # Opposite swing до пробоя (LONG -> последний LOW, SHORT -> последний HIGH).
    # Используется как reset-уровень перед повторным входом.
    reset_level: float | None = None


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
    pivots = detect_pivots(df, swing_size=swing_size)
    reset_level: float | None = None
    if last.direction == "LONG":
        lows = [p for p in pivots if p.kind == "LOW" and p.idx < last.broken_idx]
        if lows:
            reset_level = float(lows[-1].price)
    elif last.direction == "SHORT":
        highs = [p for p in pivots if p.kind == "HIGH" and p.idx < last.broken_idx]
        if highs:
            reset_level = float(highs[-1].price)
    broken_open_ms = int(df.iloc[last.broken_idx]["open_time"])
    return LtfChoCh(
        direction=last.direction,
        level=last.swing_price,
        bars_ago=last_pos - last.broken_idx,
        kind=last.kind,
        broken_open_ms=broken_open_ms,
        reset_level=reset_level,
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
