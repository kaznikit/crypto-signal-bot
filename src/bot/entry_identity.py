from __future__ import annotations

from typing import Any


def entry_variant(payload: dict[str, Any]) -> str:
    return str(payload.get("entry_variant") or payload.get("entry_mode") or "simple").lower()


def entry_point_name(payload: dict[str, Any]) -> str:
    variant = entry_variant(payload)
    if variant == "fib_dca":
        fib = payload.get("fib")
        return f"FIB_DCA/FIB_{fib}" if fib is not None else "FIB_DCA"
    if variant == "advanced":
        return "ADVANCED/RETEST"
    if variant == "sweep_reclaim":
        return "SWEEP_RECLAIM/RECLAIM"

    confirm_kind = payload.get("confirm_kind")
    if confirm_kind:
        return f"{variant.upper()}/{str(confirm_kind).upper()}"
    return variant.upper()


def entry_point_label(payload: dict[str, Any]) -> str:
    name = entry_point_name(payload)
    entry_index = payload.get("entry_index")
    if entry_index is None:
        return name
    return f"{name} #{entry_index}"
