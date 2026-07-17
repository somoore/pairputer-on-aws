#!/usr/bin/env python3.11
"""Deterministic DOOM enemy threat classification."""

from __future__ import annotations

from typing import Any

# DOOM thing type ids. These are stable WAD "doomednum" values, not runtime ids.
HITSCAN_TYPE_IDS = {
    7,     # Spider mastermind
    9,     # Shotgun guy
    65,    # Heavy weapon dude / chaingunner
    84,    # Wolfenstein SS
    3004,  # Zombieman
}

PROJECTILE_TYPE_IDS = {
    16,    # Cyberdemon
    64,    # Arch-vile
    66,    # Revenant
    67,    # Mancubus
    68,    # Arachnotron
    69,    # Hell knight
    71,    # Pain elemental
    3001,  # Imp
    3003,  # Baron of Hell
    3005,  # Cacodemon
}

MELEE_RUSH_TYPE_IDS = {
    58,    # Spectre
    3002,  # Demon
    3006,  # Lost soul
}
MELEE_TYPE_IDS = MELEE_RUSH_TYPE_IDS


def object_type_id(value: Any) -> int:
    obj = getattr(value, "object", value)
    try:
        return int(getattr(obj, "type_id", 0) or 0)
    except (TypeError, ValueError):
        return 0


def classify_enemy(value: Any) -> str:
    type_id = object_type_id(value)
    if type_id in HITSCAN_TYPE_IDS:
        return "hitscan"
    if type_id in PROJECTILE_TYPE_IDS:
        return "projectile"
    if type_id in MELEE_RUSH_TYPE_IDS:
        return "melee_rush"
    return "unknown"


def is_hitscan_enemy(value: Any) -> bool:
    return classify_enemy(value) == "hitscan"


def is_melee_rush_enemy(value: Any) -> bool:
    return classify_enemy(value) == "melee_rush"
