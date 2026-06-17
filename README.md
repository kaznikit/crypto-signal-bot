# Crypto Signal Bot

Python-бот для сигналов по крипте на базе CHoCH + Fibonacci OTE (Bybit linear, Telegram).

## Quick start

1. `cd crypto-signal-bot`
2. `python3.11 -m venv .venv` (или 3.12, если так в вашем окружении)
3. `source .venv/bin/activate`
4. `pip install -e ".[dev]"`
5. `cp .env.example .env`
6. Заполните `TG_BOT_TOKEN`, `TG_PREPARE_CHAT_ID`, `TG_ENTRY_CHAT_ID`
   (для paper — `TG_PAPER_CHAT_ID`). Старый `TG_CHAT_ID` работает как fallback.
7. При необходимости поправьте файлы в `config/`.
8. `python -m bot`

Рабочий каталог должен быть `crypto-signal-bot`, чтобы находился каталог `config/`.
Единый `config.example.yaml` сохранён как legacy-пример и fallback.

### Конфигурация по модулям

| Файл | Назначение |
|------|------------|
| `config/runtime.yaml` | Биржа, символы, HTF и paper-mode |
| `config/setup.yaml` | Поиск сетапов: pivots, Fib, reversal/continuation и quality gates |
| `config/entry.yaml` | Simple/cascade/advanced/sweep/DCA режимы входа |
| `config/risk.yaml` | SL/TP и параметры риска |
| `config/research.yaml` | Replay и статистика |
| `config/notifications.yaml` | Маршрутизация Telegram |

`PREPARE`, pre-entry `INVALIDATED`, heartbeat и PREPARE-статистика идут в
`TG_PREPARE_CHAT_ID`; `ENTRY`, stop после входа и entry-статистика — в
`TG_ENTRY_CHAT_ID`. Liberal-only сигналы идут в `TG_PAPER_CHAT_ID`.

## Pivot-стек: импульсы и структура по Pine-индикатору

С версии после рефакторинга вся market-structure-логика (HH/LH/HL/LL,
IMPULSE-маркеры, BOS/CHoCH, инвалидация) живёт в одном модуле
[`bot/market/pivots.py`](src/bot/market/pivots.py). Это **порт публичного
Pine-индикатора `Market Structure` by Leviathan** — пользователь выбрал его
эталоном, потому что прежний SMC-based стек (`smartmoneyconcepts.smc`)
выдавал «прыгающие» импульсы с неправильными анкорами.

Ключевые сущности:

* **Pivot** — классический `ta.pivothigh` / `ta.pivotlow`: бар — пивот-хай,
  если его high ≥ всех highs в окне `[i-swing_size, i+swing_size]`.
  Подтверждается через `swing_size` баров после пивот-бара.
* **HH / LH / HL / LL** — Pine-классификация: каждый пивот сравнивается с
  предыдущим **того же типа**. `>=` для HH (равные ходят в HH, не в LH).
* **ImpulseLeg** — `HL→HH` (LONG) или `LH→LL` (SHORT). Других вариантов нет:
  развороты тренда (LL→HH, HH→LL) импульсами не считаются — это область
  CHoCH-маркеров. Pine рисует 0.5-линию ровно для этих пар, отсюда и
  определение.
* **StructureBreak** — Pine BOS/CHoCH: один активный `prevHigh` (или
  `prevLow`), деактивируется первым `close > prevHigh`. CHoCH = пробой в
  направлении, обратном предыдущему пробою; первый пробой в серии — всегда
  BOS.

Параметры — секция `pivots:` в `config/setup.yaml`:

| Ключ | Что значит |
|------|-----------|
| `swing_size_by_tf` | Словарь TF → swing_size. Pine-дефолт — 20 на всех; у нас 15 на 4H, 12 на 1H, 10 на 5M/15M (чтобы внутри тика хватало пивотов). |
| `bos_use_close` | Pine `'Candle Close'` (true, дефолт) vs `'Wicks'` (false). Pine рекомендует Close для устойчивости — wick-only пробои не считаются BOS. |
| `impulse_max_age_bars` | Сколько HTF-баров после пика импульса разрешено ждать первое касание 0.5. Без ограничения старые impulse legs давали бы триггер на любом проходящем баре спустя сотни баров. |

Всё, что было до этого — `extend_impulse_to_structural_extreme`,
`completion_retrace`, `find_extended_impulse_start`, `resolve_anchored_impulse`,
`build_anchored_impulse`, `extract_structure_events`, `detect_choch`,
`detect_last_impulse_smc`, `AnchoredImpulse` / legacy `ImpulseLeg` —
**удалено**. Старые поля в конфиге оставлены без чтения для обратной
совместимости.

## Включение опций стратегии

Все переключатели — в `config/setup.yaml`, секция **`strategy_features`**:

| Параметр | Назначение |
|----------|------------|
| `require_liquidity_grab_reversal` | Для **разворота**: не слать PREPARE, пока не выполнен sweep ликвидности перед CHoCH (`liquidity_grab_filter`). |
| `quality_score_enabled` | Считать score 0–100 и логировать реальные доступные признаки. |
| `quality_score_filter_enabled` | Применять score как фильтр; по умолчанию выключен до статистического подтверждения весов. |
| `min_quality_score` | Минимальный score только при включённом `quality_score_filter_enabled`. |
| `volume_expansion_in_score` | Учитывать в score всплеск объёма относительно SMA. |
| `continuation_require_4h_alignment` | Для **продолжения** на 1H/15M: последний BOS/CHoCH на **4H** в ту же сторону. Сейчас 4H подтягивается только если в этом же минутном тике закрылся и 4H — иначе `series` не содержит 4H и сетап будет отклонён; для продакшена имеет смысл добавить кэш последней 4H-серии между тиками. |
| `require_ob_or_fvg_in_ote` | Требовать пересечение OTE-зоны с **OB или FVG** (`smartmoneyconcepts`). Единственное место, где ещё используется `smc` (OB/FVG никак не связаны с импульсной логикой). |
| `swing_length_ob_fvg` | Параметр `swing_length` для расчёта OB/FVG в библиотеке. |

**Paper mode:** при `paper_mode.enabled: true` обычные сигналы всё равно
разделяются между PREPARE/ENTRY-чатами. Чтобы вернуть старую маршрутизацию всех
paper-сигналов в один `TG_PAPER_CHAT_ID`, включите
`telegram.route_paper_mode_to_paper_chat: true`.

### Liberal paper-mode (`paper_mode.liberal`)

Второй проход гейтов с более мягкими порогами: если строгий PREPARE отклонён, но `liberal.enabled: true` и задан `TG_PAPER_CHAT_ID`, бот отправляет PREPARE **только в paper-чат** с префиксом `[LIBERAL]`. Основной канал не засоряется.

Параметры: `min_atr_pct`, `min_rr`, `min_quality_score`, `max_bars_ago_4h` (окно CHoCH на 4H шире), `ltf_swing_length_override` (ещё мягче LTF CHoCH для ENTRY). Сетапы помечаются `is_liberal` в БД; ENTRY/INVALIDATED для них уходят только в paper.

После изменения флагов перезапустите процесс бота.

### Сравнение ENTRY-вариантов

`entry.comparison_modes` запускает в live-цикле несколько независимых логик
входа от одного PREPARE. В рабочем конфиге одновременно сравниваются:

```yaml
entry:
  comparison_modes: [simple, sweep_reclaim, advanced]
```

Каждый вариант хранит собственное состояние, может отправить свой ENTRY и
сопровождается по собственному стопу. Открытый вход одного варианта не блокирует
остальные варианты этой исследовательской группы. `simple` использует reset
уровень подтверждения с fallback на HTF invalidation, `sweep_reclaim` — sweep
extreme с ATR-буфером, `advanced` — retest/sweep extreme согласно
`advanced.stop_source`.

ENTRY-сообщения имеют заголовки вида `ENTRY [SIMPLE/BOS #1]`,
`ENTRY [SWEEP_RECLAIM/RECLAIM #1]` и `ENTRY [ADVANCED/RETEST #1]`, а также
отдельные строки `stop`, `stopSource` и `setupInvalidation`. Entry-статистика
оценивает каждый вариант по его `recommended_stop` и показывает сравнительный
winrate по вариантам. Если `comparison_modes` пуст, бот запускает только
`entry.mode`, как раньше.

### Cascade ENTRY (`entry.cascade_*`)

`entry.cascade_enabled: true` включает многоступенчатое подтверждение вместо
мгновенного ENTRY по одному LTF BOS/CHoCH. Для `1H` текущая цепочка:

```yaml
entry:
  cascade_enabled: true
  cascade_by_htf:
    "1H": "5M|1M"
  cascade_confirm_structure_kinds: [BOS, CHOCH]
```

Логика: PREPARE на 1H уже означает касание 0.5; дальше бот ждёт BOS/CHoCH на
5M, затем BOS/CHoCH на 1M строго после 5M-пробоя и сразу отправляет ENTRY.
Дополнительный откат на 5M/1M больше не требуется. Прогресс хранится в setup,
поэтому перезапуск процесса не сбрасывает пройденные стадии.

### Advanced ENTRY (`entry.mode: advanced`)

`entry.mode` переключает механику подтверждения:

- `simple` — прежний вход по LTF BOS/CHoCH;
- `advanced` — последовательность `sweep -> reclaim -> CHoCH -> retest`.
- `sweep_reclaim` — ранний вход сразу после sweep и возврата закрытия за
  снятый уровень, без обязательного CHoCH/retest.

Advanced-режим хранит прогресс в setup и переживает перезапуск процесса.
После `PREPARE` он ждёт снятие ближайшего LTF pivot-low/pivot-high, возврат
закрытия за снятый уровень, свежий CHoCH и его ретест. Рекомендованный короткий
стоп по умолчанию ставится за экстремум retest-свечи с ATR-буфером, а
sweep-extreme остаётся уровнем инвалидации FSM. Через `advanced.stop_source`
можно вернуть более широкий стоп за sweep-extreme. Целевой уровень берётся из
вершины HTF-импульса. Перед ENTRY проверяются максимальная ширина стопа и RR до
HTF-цели. `advanced` игнорирует cascade-цепочку.

В `sweep_reclaim` стоп ставится за sweep-extreme с ATR-буфером. Перед входом
проверяются displacement/volume reclaim-свечи, максимальная ширина стопа и RR
до HTF-цели. Режим даёт больше и более ранние сигналы, но подтверждение слабее,
чем у полного `advanced`. `advanced.require_directional_reclaim: true`
дополнительно требует бычье тело для LONG и медвежье для SHORT; по умолчанию
выключено, поскольку закрытие обратно за снятым уровнем уже является reclaim.

Основные настройки находятся в `entry.advanced`: окна стадий, displacement и
volume-фильтры, ATR-буфер стопа, максимальная ширина стопа и минимальный RR.
ENTRY-сообщение всегда содержит `recommendedStop`; `invalidate` остаётся
отдельным HTF-уровнем смерти всего setup.

Для post-entry статистики сделки `advanced` и `sweep_reclaim` считаются
успешными, если цена раньше обновила `target_price` ближайшего HTF-импульса, и
неуспешными, если раньше ушла за `recommendedStop`. Если оба уровня задеты
одной свечой, статистика консервативно считает первым стоп.

### Fib DCA ENTRY (`entry.mode: fib_dca`)

`fib_dca` использует PREPARE как основание для заранее ограниченной лестницы
лимитных входов и не ждёт LTF BOS/CHoCH. Уровни и доли позиции задаются в
`entry.fib_dca.levels`; сумма `weight_pct` должна быть равна 100.

```yaml
entry:
  mode: fib_dca
  fib_dca:
    monitoring_tf_by_htf:
      "4H": "5M"
      "1H": "5M"
    levels:
      - {fib: 0.500, weight_pct: 40}
      - {fib: 0.618, weight_pct: 30}
      - {fib: 0.705, weight_pct: 20}
      - {fib: 0.786, weight_pct: 10}
```

План рассчитывается от импульса PREPARE и фиксируется в setup, поэтому
перезапуск процесса или изменение конфига не меняет активную лестницу. Каждое
новое касание уровня отправляет отдельный ENTRY с `fib`, `weight`,
`filled` и `averageEntry`. Общий стоп остаётся на HTF-инвалидации, цель —
вершина HTF-импульса. Заполненные уровни в history replay считаются одной
агрегированной позицией; полный стоп полностью заполненной сетки равен `-1R`.

Независимо от режима ENTRY на одном символе одновременно допускается только
одна открытая позиция. Пока она не достигла TP или стопа, новые setup не могут
отправить ENTRY. Для `fib_dca` касания следующих уровней считаются доливками
той же позиции. При касании стопа отправляется `INVALIDATED`, который в
TradingView отображается крестиком `X`.

### PREPARE / Fib статистика (`prepare_stats`)

Отдельная периодическая статистика оценивает каждый PREPARE независимо от
выбранного режима ENTRY. Она показывает, достигнута ли вершина импульса раньше
инвалидации, какие Fib-уровни были затронуты и самый глубокий уровень до
результата. Под уровнем, от которого пошло движение, используется объективно
воспроизводимое правило: самый глубокий настроенный Fib, затронутый до цели.

Если `prepare_stats.fib_levels` пуст, используются уровни
`entry.fib_dca.levels`. При одновременном касании цели и инвалидации одной
свечой результат консервативно считается неуспешным.

## Тест стратегии на истории

Есть **walk-forward HTF probe** для ветки разворота на **4H**.
Он покрывает только верхнюю часть пайплайна `STRUCTURE -> PREPARE`:
1. Считаем число подтверждённых CHoCH-событий через `extract_structure_breaks_htf` (`impulse_lock=True`). Новое событие = выросло число.
2. Пропускаем только бары с достаточным ATR.
3. Строим PREPARE-кандидат через `detect_reversal_prepare` (Pine-импульс LH→LL + 0.5-mid).
4. Прогоняем кандидата через включённые `strategy_features` гейты.

Запуск (нужен интернет к Bybit API):

```bash
cd crypto-signal-bot
source .venv/bin/activate
python -m bot.history_backtest --symbol BTCUSDT --limit 1000 --swing-size 15
# либо после `pip install -e .`:
signal-bot-history --symbol ETHUSDT --limit 1500 --swing-size 15
```

Ключевые флаги:
- `--swing-size` — Pine pivot length (по обе стороны). 10–20 для 4H норм; меньше = чаще пивоты и BOS, но больше шума.
- `--max-bars-ago` — допустимое «опоздание» между закрытием бара и баром пробоя CHoCH (если не указано — берётся `reversal.choch_lookback_bars` из `config/setup.yaml`).

Вывод — четыре числа по стадиям:

```
1) STRUCTURE flips (new CHOCH on 4H)
2) STRUCTURE prefilter passed (ATR + fresh CHOCH window)
3) PREPARE candidates (detect_reversal_prepare)
4) PREPARE gate-passed (strategy_features)
```

Если числа подозрительно одинаковые (напр. всё по 1) — значит где-то отсев уронил все кандидаты на первом же шаге. Обращайте внимание на разницу между строками 1 и 2: большой разрыв = CHoCH детектится, но либо старый (`BrokenIndex` далеко в прошлом), либо ATR ниже порога.

Для стадий `runtime -> ENTRY -> TP/SL` используйте `history_replay`.

**Как развить проверку дальше:**

1. Сохранить выгрузку свечей в CSV и гонять офлайн без сети.
2. Для полной симуляции ENTRY/FSM/TP-SL запускать `python -m bot.history_replay --mode reversal`.
3. Сравнивать разные `swing_length`, `min_quality_score`, `min_atr_pct` на одном участке.

Юнит-тесты без сети:

```bash
pytest -q tests
```

## Оффлайн replay с ENTRY/TP/SL

Для более практичной проверки (не только PREPARE-воронка) есть walk-forward replay:

- строит PREPARE по тем же правилам, что и live-бот;
- пытается получить ENTRY на LTF;
- закрывает сделки по TP/SL (или по последней цене в конце выборки);
- считает `winrate`, `avgR`, `totalR`, `maxDD` и `profit factor`.

Запуск:

```bash
cd crypto-signal-bot
source .venv/bin/activate
python -m bot.history_replay --symbol BTCUSDT --mode both --limit 1000

# Сравнить A/B/C/D/E на одном запрошенном историческом горизонте
python -m bot.history_replay --symbol BTCUSDT --mode both --limit 1000 --variant all

# Повторить вариант C с другим минимальным RR
python -m bot.history_replay --symbol BTCUSDT --mode both --variant C --min-rr 2.5
# точечно как Pine export для 1H, с прогрессом:
python -m bot.history_replay --symbol HYPEUSDT --mode continuation --limit 1000 --focus-htf 1H --progress
# или после pip install -e .
signal-bot-replay --symbol ETHUSDT --mode reversal --limit 1000
```

Replay создаёт полноценную позицию только после ENTRY с рыночной целью,
stop и положительным RR. Варианты:

| Вариант | Что проверяет | Риск |
| --- | --- | --- |
| `A` | Первый свежий `CHOCH` после касания Fib `0.5`, один вход | `1R` |
| `B` | Первый свежий `CHOCH` или `BOS` после касания Fib `0.5`, один вход | `1R` |
| `C` | Вариант `A` + подтверждённый re-entry после reset swing и нового BOS | `0.6R + 0.4R`, максимум `1R` |
| `D` | Вариант `C` + MFI как фильтр входа | максимум `1R` |
| `E` | Слепой Fib DCA/каскад по уровням `0.5/0.618/0.705/0.786`; контрольная группа, не основной вход | `1R` по всей лестнице |

Во всех вариантах, кроме `D`, MFI только логируется в признаки сделки и не
влияет на решение. `E` нужен для сравнения с механической лестницей, а не как
основной кандидат стратегии.

Комиссия, проскальзывание, spread и консервативный порядок SL/TP на одной
свече задаются в `history_replay`. Итоговый отчёт показывает expectancy после
расходов, MAE/MFE, losing streak, holding time и разрезы по направлению,
Fib-глубине и рыночному режиму.

`--mode`: `reversal`, `continuation`, `both` (по умолчанию `both`).
`--max-expanded-bars-per-tf` переопределяет глубину младших TF. Для каскада
`1H -> 15M -> 5M -> 1M` месяц истории требует примерно `60000` свечей `1M`;
иначе финальные ENTRY на истории могут не появиться из-за нехватки `1M`-данных.

### Плотность сигналов (replay)

Помимо `symbols.count` и `strategy_features`: импульс continuation = последний leg HL→HH (LONG) или LH→LL (SHORT) в направлении свежего BOS/CHoCH (`pivots.swing_size_by_tf[htf]`), reversal — последний leg в направлении, **обратном** свежему CHoCH. PREPARE строится только на баре первого касания 0.5 импульса (`first_touch_of_level_since`); раньше continuation мог ARMиться вне зоны через фазу `WAIT_OTE`, теперь такого нет. Окно CHoCH на 4H согласовано с `reversal.choch_lookback_bars`; LTF-подтверждение ENTRY делается через `detect_ltf_entry_confirm` с настройками `entry.confirm_structure_kinds`, `entry.ltf_swing_length`, `paper_mode.liberal.ltf_swing_length_override`; параллельные сетапы дедуплицируются по `(symbol, type, htf, direction)`.

Метрики переснимать локально на свежей истории: после перехода на Pine-стек количество PREPARE/ENTRY изменилось — старые цифры (где PREPARE считался на каждом «прыжке» SMC-импульса) больше не воспроизводятся.

## Визуализация через TradingView

- Каждое сообщение в Telegram содержит строку `TV: https://www.tradingview.com/chart/?symbol=BYBIT:SYMBOL&interval=...` с правильным TF (PREPARE — HTF сетапа, ENTRY — TF подтверждения).
- На графике для наглядности включите публичный SMC-индикатор (например, *Smart Money Concepts* от LuxAlgo / RocketC).

### Дедупликация активных сетапов

С версии после фикса перенаселения сетапы дедуплицируются по ключу
`(symbol, type, htf, direction)`: если уже есть активный (`ARMED`) сетап того же
направления на том же HTF и приходит новый PREPARE по более свежей структуре,
старый сетап инвалидируется и заменяется новым. Это вырезает спам вида
«PREPARE на каждом 4H закрытии», но не блокирует обновление на свежую структуру.
Логика одинаковая в live-боте и в `history_replay`.

### PREPARE: BOS/CHoCH + первое касание 0.5

PREPARE-маркер **всегда** требует две вещи одновременно:

1. **Свежий структурный пробой** на HTF (Pine BOS/CHoCH через
   `extract_structure_breaks`):
   - **Reversal** — CHoCH в окне `reversal.choch_lookback_bars` (по
     умолчанию `30` 4H-баров; для liberal-чата используется
     `paper_mode.liberal.max_bars_ago_4h`, по умолчанию `50`).
   - **Continuation** — BOS или CHoCH в сторону тренда в окне
     `continuation.structure_max_bars_ago` HTF-баров (по умолчанию `30`).
2. **Первое касание 0.5 импульса** этого HTF (`first_touch_of_level_since`):

   - **Continuation**: импульс = последний `HL→HH` (LONG) или `LH→LL` (SHORT)
     в направлении BOS/CHoCH. Триггер-уровень = `(start_price + end_price) / 2`
     — это ровно та 0.5-линия, которую рисует Pine для каждого
     HL→HH / LH→LL leg'а.
   - **Reversal**: импульс = последний leg в **противоположном** направлении
     (CHoCH UP → реверсируем последний `LH→LL`; CHoCH DOWN → последний
     `HL→HH`). Триггер = midprice того же leg'а — ждём ретрейс ОТ
     CHoCH'нувшего пика к 0.5 реверсируемого импульса.

   `first_touch_of_level_since(direction, level, since_idx=peak_idx)` гарантирует,
   что бары между пиком импульса (исключая) и текущим (исключая) не заходили за
   уровень, а текущий — заходит. Это убирает повторный фейр PREPARE на каждом
   проходящем баре.

3. **Импульс ещё актуален**:

   - Не старше `pivots.impulse_max_age_bars` (по умолчанию `60`) от текущего бара.
   - Не структурно инвалидирован: для LONG (HL→HH) нет ни одного бара после пика,
     где `low < HL`; для SHORT (LH→LL) — нет `high > LH`. Проверка через
     `impulse_invalidated`.

**Invalidation для setup'а** различается между ветками:

* **Continuation** LONG: `invalidation = HL` (start_price). SL ниже entry —
  если цена ушла под HL, импульс умер.
* **Continuation** SHORT: `invalidation = LH` (start_price). SL выше entry.
* **Reversal** LONG (CHoCH UP): `invalidation = LL` реверсируемого SHORT-импульса
  (= `end_price`). SL ниже entry — если цена сделала новый low, реверс провалился.
* **Reversal** SHORT (CHoCH DOWN): `invalidation = HH` реверсируемого LONG-импульса
  (= `end_price`). SL выше entry.

Это фикс bug'а старой ветки: там reversal-invalidation брался как
`impulse_leg.start_price` (= HH для SHORT-импульса), из-за чего для
LONG-reversal SL ставился **выше** entry — не имело физического смысла.

PREPARE-сигнал срабатывает на баре первого касания. Дальше бот автоматически
сопровождает `ARMED`-сетап: проверяет структурную инвалидацию на HTF, ценовую
инвалидацию на `entry.invalidation_ltf_by_htf` (или HTF по умолчанию) и LTF
подтверждение (`structure_break`/`directional_close` по `entry.confirm_mode`).
При подтверждении отправляется `ENTRY`, при нарушении условий — `INVALIDATED`.
Лейбл `P LONG/SHORT` ставится ровно на баре касания на уровне 0.5 импульса.
OTE-прямоугольник в Pine-оверлее не рисуется (триггер — одиночный уровень).

`reversal.choch_lookback_bars` намеренно расцеплен с `reversal.ttl_bars_4h`:
первое — насколько далеко в прошлое искать структурный пробой (исторический
факт), второе — TTL уже созданного PREPARE до экспирации.

В payload PREPARE сохраняются: `structure_kind` (`BOS`/`CHOCH`),
`structure_swing_open_ms`, `structure_broken_open_ms`, `prepare_trigger_level`
(= `ote_low` = `ote_high` — «вырожденная» зона для совместимости со схемой БД),
`prepare_trigger_fib` (по умолчанию `0.5`), `impulse_start_price` /
`impulse_end_price`, `invalidation_price`.

`continuation.fib_low` управляет fib-уровнем для PREPARE-continuation
(по умолчанию `0.5` — Pine-эталон).

### IMPULSE / STRUCTURE / PIVOT в overlay'е

`_emit_fresh_pivot_events` в `history_replay.py` на каждом закрытом баре
эмитит три вида событий:

* **PIVOT** — HH/LH/HL/LL-метка. Пивот подтверждается через `swing_size`
  баров после самого пивота, поэтому метка ставится на `last_pos - swing_size`.
* **IMPULSE** — leg `HL→HH` (LONG) / `LH→LL` (SHORT), у которого экспансионный
  пивот подтвердился ровно на этом баре. Pine-оверлей рисует диагональ
  (start → end) + горизонтальную 0.5-линию (midprice), продлённую вперёд от
  пика на `IMPULSE fib forward bars` (input в Pine, дефолт 40).
* **STRUCTURE** — BOS / CHoCH (первое close-пересечение активного prevHigh /
  prevLow). Эмитится мгновенно, без задержки на `swing_size`.

**HTF impulse-lock** (``detect_pivots_htf`` / ``extract_structure_breaks_htf``):
после подтверждения пика импульса (HH для LONG, LL для SHORT) внутренние
пивоты и противоположные BOS/CHoCH **не показываются**, пока цена не обновит
минимум импульса (close < HL при ``bos_use_close``) или максимум (close > HH).
LOW-пивот **ниже** HL (настоящий LL) — только после ``close < HL``; low **выше**
HL (коррекция / минимумы импульса на жёлтой зоне) показываются как **HL**.
Якоря ``start``/``end`` каждой импульсной ноги всегда на графике. Это даёт
цепочку вида ``HL → BOS → HH → P LONG → E LONG → CHOCH SHORT → LL`` без
ложных HL/LH/LL и SHORT-структуры на ретрейсе к 0.5. LTF для ENTRY lock не
применяется.

После полного прогона `_filter_invalidated_impulses` удаляет диагонали тех
импульсов, у которых после пика цена пробила `start_price` — это убирает
«длинные зелёные линии от структурно перекрытых импульсов».

Pine-индикатор Leviathan'а сам по себе **не делает** такой пост-фильтрации
(он рисует все исторические HL→HH leg'и навсегда). У нас она оставлена ради
чистоты overlay'я; на сам алгоритм PREPARE она не влияет — там есть отдельная
проверка `impulse_invalidated`.

### Pine overlay (история через replay)

Самый быстрый способ проверить точки входа на TV — выгрузить их прямо из
walk-forward-симуляции на свежей истории Bybit, не дожидаясь, пока live-бот
накопит сделки в `bot.db`. По умолчанию Pine показывает только вход, тейк и
стоп лейблами у свечи:

```bash
cd crypto-signal-bot
source .venv/bin/activate
signal-bot-export-pine --symbol BTCUSDT --tf 4H --from-replay --mode both --limit 500 --out btc_4h.pine
# с liberal-сетапами на том же чарте:
signal-bot-export-pine --symbol BTCUSDT --tf 4H --from-replay --include-liberal --out btc_all.pine
# узкий период:
signal-bot-export-pine --symbol ETHUSDT --tf 1H --from-replay --mode continuation --since 2026-04-01 --out eth_1h.pine
# отдельная проверка PREPARE: бар касания 0.5 + начало/конец HTF-импульса:
signal-bot-export-pine --symbol ETHUSDT --tf 1H --from-replay --mode continuation --kinds PREPARE --out eth_1h_prepare.pine
```

Параметры `--from-replay`:
- `--mode {reversal,continuation,both}` — какие сетапы симулировать;
- `--limit N` — свечей на TF (≤1000, Bybit klines API);
- `--max-expanded-bars-per-tf N` — cap младших TF при авторасширении истории (`1H -> 1M` при `--limit 1000` требует до `60000`);
- `--variant {configured,A,B,C,D,E}` — какой вариант ENTRY выгрузить в Pine;
- `--min-rr N` — переопределить минимальный RR для проверки порогов `1.5/2.0/2.5/3.0`;
- `--kinds ENTRY,TAKE_PROFIT,STOP_LOSS` — какие события вывести; это дефолт. Для проверки PREPARE используйте `--kinds PREPARE`;
- `--max-markers 400` — режется до N **последних** лейблов (Pine v5 лимит ~500 на индикатор).

### Pine overlay из `bot.db`

Если бот уже наработал реальные сигналы — берём их прямо из SQLite (`bar_open_ms`, `ote_low/high`, `sl`/`tp`, `liberal`, `setup_htf` сохраняются в payload):

```bash
signal-bot-export-pine --symbol BTCUSDT --tf 4H --since 2025-01-01 --out btc_overlay.pine
signal-bot-export-pine --symbol BTCUSDT --tf 4H --include-liberal --out btc_all.pine
```

### Импорт в TradingView

1. Откройте график **`BYBIT:SYMBOL`** на том же TF, что задавали в `--tf` (для ENTRY совпадение по TF подтверждения важно — иначе timestamps не сядут на бары).
2. Pine Editor → New → вставьте содержимое `.pine` → Save → Add to chart.
3. На графике появится:
   - **PREPARE** — бирюзовый/оранжевый лейбл `PREPARE LONG/SHORT` на баре касания 0.5, плюс `IMP START` и `IMP END` на начале/конце HTF-импульса;
   - **ENTRY** — синий лейбл `ENTRY LONG/SHORT`;
   - **TAKE_PROFIT** — зелёный лейбл `TP LONG/SHORT`;
   - **STOP_LOSS** — красный лейбл `SL LONG/SHORT`.
   Если один timestamp одновременно попадает в начало и конец разных импульсов,
   overlay оставляет только `IMP END`, чтобы свеча не имела двух смыслов сразу.
4. Линии SL/TP, STRUCTURE, IMPULSE и PIVOT в этом шаблоне не рисуются,
   чтобы график оставался пригодным для ручной проверки выбранного типа сигнала.

Если нужен обратный канал TV → бот, делайте отдельный webhook-эндпоинт; в этом боте не реализовано.

## Запуск на VPS (пошагово)

Предполагается **Ubuntu 22.04**, пользователь с `sudo`.

1. **Системные пакеты:** `sudo apt update && sudo apt install -y python3.11 python3.11-venv git`
2. **Код на сервер:** скопируйте каталог `crypto-signal-bot` в `/home/tradingbot/app` (или склонируйте репозиторий и оставьте только эту папку как корень приложения).
3. **Виртуальное окружение:**

   ```bash
   cd /home/tradingbot/app
   python3.11 -m venv .venv
   source .venv/bin/activate
   pip install -U pip
   pip install -e .
   ```

4. **Конфиг и секреты:** проверьте каталог `config/`, создайте `.env`
   (`chmod 600 .env`) с `TG_BOT_TOKEN`, `TG_PREPARE_CHAT_ID`,
   `TG_ENTRY_CHAT_ID`, при paper — `TG_PAPER_CHAT_ID`, при желании
   `BYBIT_API_KEY` / `BYBIT_API_SECRET`.
5. **База:** по умолчанию `BOT_DB_URL=sqlite:///./bot.db` — файл появится в текущей директории при первом запуске.
6. **Systemd:** используйте [`deploy/install.sh`](deploy/install.sh) и [`deploy/tradingbot.service`](deploy/tradingbot.service) из этого репозитория; подробности в [`deploy/README.md`](deploy/README.md).
7. **Проверка:** `sudo systemctl status tradingbot`, логи: `sudo journalctl -u tradingbot -f`.
8. **Ротация логов journald:** `sudo journalctl --vacuum-time=14d`.

Unit в `tradingbot.service` должен указывать `WorkingDirectory` на каталог, где
лежат `config/` и `.env`, и `ExecStart=.../python -m bot`.

## Дисклеймер

Сигналы не являются финансовой рекомендацией. Тестируйте на paper и истории перед реальной торговлей.
