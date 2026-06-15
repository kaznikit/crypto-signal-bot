from bot.entry_engine.structural import (
    EntryDecision,
    EntryEvaluationContext,
    EntryStrategy,
    StructuralEntryStrategy,
)


def build_entry_strategy(name: str) -> EntryStrategy:
    normalized = name.strip().lower()
    if normalized == StructuralEntryStrategy.name:
        return StructuralEntryStrategy()
    raise ValueError(f"Unknown entry strategy: {name}")


__all__ = [
    "EntryDecision",
    "EntryEvaluationContext",
    "EntryStrategy",
    "StructuralEntryStrategy",
    "build_entry_strategy",
]
