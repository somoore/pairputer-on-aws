#!/usr/bin/env python3.11
"""Small in-memory door/use-line outcome tracker for Agent DOOM."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

# Vanilla Doom/Doom II key-door specials (doomwiki.org "Linedef type" table): DR/D1 key doors
# 26-28/32-34 plus the Doom II S1/SR blaze key-door switches 99/133-137.
# MUST STAY IN SYNC with the classification copies in wad_map.py and planner.py — the capsule
# deploys these modules as standalone files, so the sets are duplicated instead of imported.
KEY_DOOR_SPECIAL_COLORS = {
    26: "blue",
    27: "yellow",
    28: "red",
    32: "blue",
    33: "red",
    34: "yellow",
    99: "blue",
    133: "blue",
    134: "red",
    135: "red",
    136: "yellow",
    137: "yellow",
}
KEY_DOOR_SPECIALS = set(KEY_DOOR_SPECIAL_COLORS)
# Non-key door actions that OPEN passage (any activation type); close-only variants excluded.
NORMAL_DOOR_SPECIALS = {
    1, 2, 4, 16, 29, 31, 46, 61, 63, 76, 86, 90, 103, 105, 106, 108, 109,
    111, 112, 114, 115, 117, 118,
}
# S-type lift/platform specials behave like doors for memory purposes: USE lowers the blocking
# floor for a few seconds and the line can simply be re-USEd if it rose again. W-type walk-over
# lifts (10/88/120/121) are deliberately NOT here: USE does nothing on them, so endless retry
# semantics would loop forever; they keep normal abandonment behavior.
LIFT_USE_SPECIALS = {21, 62, 122, 123}
LIFT_WALK_SPECIALS = {10, 88, 120, 121}
RETRIGGERABLE_SPECIALS = NORMAL_DOOR_SPECIALS | LIFT_USE_SPECIALS
EXIT_SPECIALS = {11, 51, 52, 124, 197}


@dataclass
class DoorRecord:
    line_id: int
    attempts: int = 0
    failures: int = 0
    successes: int = 0
    special: int = 0
    tag: int = 0
    state: str = "unknown"
    last_status: str = "new"
    updated_at: int = 0
    stale_opens: int = 0
    force_follow_failures: int = 0


class DoorMemory:
    """Tracks repeated USE attempts so the planner stops hammering dead lines."""

    def __init__(self, *, max_failures: int = 3) -> None:
        self.max_failures = int(max_failures)
        self._records: dict[int, DoorRecord] = {}

    def is_blocked(self, line_id: int | None) -> bool:
        if line_id is None:
            return False
        record = self._records.get(int(line_id))
        return bool(record and record.state in {"blocked", "requires_key", "requires_switch"})

    def is_open(self, line_id: int | None) -> bool:
        if line_id is None:
            return False
        record = self._records.get(int(line_id))
        return bool(record and record.state in {"opening", "opened"})

    def is_opening(self, line_id: int | None) -> bool:
        if line_id is None:
            return False
        record = self._records.get(int(line_id))
        return bool(record and record.state == "opening")

    def is_confirmed_open(self, line_id: int | None) -> bool:
        if line_id is None:
            return False
        record = self._records.get(int(line_id))
        return bool(record and record.state == "opened")

    def can_retry(self, line_id: int | None) -> bool:
        if line_id is None:
            return False
        record = self._records.get(int(line_id))
        if record is None:
            return True
        if record.state in {"blocked", "requires_key", "requires_switch"}:
            return False
        return record.failures < self.max_failures

    def route_penalty_for(self, line_id: int | None) -> float:
        if line_id is None:
            return 0.0
        record = self._records.get(int(line_id))
        if record is None or record.state != "congested":
            return 0.0
        return float(min(3200, 900 * max(1, record.failures)))

    def state_for(self, line_id: int | None) -> str:
        if line_id is None:
            return "unknown"
        record = self._records.get(int(line_id))
        return "unknown" if record is None else record.state

    def attempts_for(self, line_id: int | None) -> int:
        if line_id is None:
            return 0
        record = self._records.get(int(line_id))
        return 0 if record is None else int(record.attempts)

    def failures_for(self, line_id: int | None) -> int:
        if line_id is None:
            return 0
        record = self._records.get(int(line_id))
        return 0 if record is None else int(record.failures)

    def force_follow_failures_for(self, line_id: int | None) -> int:
        if line_id is None:
            return 0
        record = self._records.get(int(line_id))
        return 0 if record is None else int(record.force_follow_failures)

    def live_line_suppressed(self, line_id: int | None) -> bool:
        if line_id is None:
            return False
        record = self._records.get(int(line_id))
        if record is None:
            return False
        return record.state == "congested" and record.force_follow_failures >= 1

    def required_key_colors(self) -> set[str]:
        """Return key colors currently blocking known progression lines."""
        colors: set[str] = set()
        for record in self._records.values():
            if record.state != "requires_key":
                continue
            color = KEY_DOOR_SPECIAL_COLORS.get(int(record.special))
            if color:
                colors.add(color)
        return colors

    def mark_key_acquired(self, color: str) -> int:
        """Repair stale key-door blockage after inventory proves a key was picked up."""
        normalized = str(color).strip().lower()
        if not normalized:
            return 0
        repaired = 0
        for record in self._records.values():
            if record.state != "requires_key":
                continue
            if KEY_DOOR_SPECIAL_COLORS.get(int(record.special)) != normalized:
                continue
            record.state = "closed"
            record.failures = 0
            record.force_follow_failures = 0
            record.stale_opens = 0
            record.last_status = f"{normalized}_key_acquired_retry"
            record.updated_at = int(time.time())
            repaired += 1
        return repaired

    def tag_is_open(self, tag: int | None) -> bool:
        if tag is None:
            return False
        tag_int = int(tag)
        if tag_int <= 0:
            return False
        return any(
            record.tag == tag_int and record.state in {"opening", "opened"}
            for record in self._records.values()
        )

    def last_status_for(self, line_id: int | None) -> str:
        if line_id is None:
            return ""
        record = self._records.get(int(line_id))
        return "" if record is None else str(record.last_status)

    def candidate_lines(self, *, include_blocked: bool = False) -> tuple[dict[str, Any], ...]:
        """Return compact remembered line facts for planner fallback routing."""
        rows: list[dict[str, Any]] = []
        for line_id, record in self._records.items():
            if not include_blocked and record.state in {"blocked", "requires_key", "requires_switch"}:
                continue
            rows.append(
                {
                    "line_id": int(line_id),
                    "state": str(record.state),
                    "special": int(record.special),
                    "tag": int(record.tag),
                    "attempts": int(record.attempts),
                    "failures": int(record.failures),
                    "successes": int(record.successes),
                    "last_status": str(record.last_status),
                    "force_follow_failures": int(record.force_follow_failures),
                }
            )
        return tuple(rows)

    def record_status(self, line_id: int | None, *, status: str) -> None:
        if line_id is None:
            return
        record = self._records.setdefault(int(line_id), DoorRecord(line_id=int(line_id)))
        record.last_status = str(status)
        record.updated_at = int(time.time())

    def record_route_contact(self, line_id: int | None) -> None:
        if line_id is None:
            return
        record = self._records.setdefault(int(line_id), DoorRecord(line_id=int(line_id)))
        record.failures = min(self.max_failures + 2, record.failures + 1)
        record.state = "congested"
        record.last_status = "route_contact_congested"
        record.updated_at = int(time.time())

    def record_route_abandoned(
        self, line_id: int | None, *, status: str = "repeated_route_no_cross", passable: bool = False
    ) -> None:
        if line_id is None:
            return
        record = self._records.setdefault(int(line_id), DoorRecord(line_id=int(line_id)))
        if record.special in KEY_DOOR_SPECIALS:
            # A locked door abandoned by routing means we lack the key: flag requires_key so the
            # key hunt swaps the objective, instead of dead-blocking the only path forever.
            record.failures = min(self.max_failures + 2, record.failures + 1)
            record.state = "requires_key"
        elif record.special in RETRIGGERABLE_SPECIALS:
            record.failures = min(self.max_failures + 2, record.failures + 1)
            record.state = "congested"
        elif passable:
            # ANY passable line is a crossable bridge regardless of special: repeated no-cross is
            # congestion (monster in the way, pinch geometry, lift timing), not proof of a wall.
            # Penalize, never hard-block — it may be the single bridge into the rest of the map
            # (E1M2 lines 288/289: passable special-88 walk-over lifts got hard-blocked and cut
            # the only route into the map's east half).
            record.failures = min(self.max_failures + 2, record.failures + 1)
            record.state = "congested"
        else:
            record.failures = max(self.max_failures, record.failures + 1)
            record.state = "blocked"
        record.last_status = str(status)
        record.updated_at = int(time.time())

    def record_force_follow_stalled(
        self,
        line_id: int | None,
        *,
        status: str = "force_follow_no_cross",
        special: int = 0,
        tag: int = 0,
    ) -> None:
        if line_id is None:
            return
        record = self._records.setdefault(int(line_id), DoorRecord(line_id=int(line_id)))
        if special:
            record.special = int(special)
        if tag:
            record.tag = int(tag)
        record.force_follow_failures += 1
        record.failures = min(self.max_failures + 2, record.failures + 1)
        record.last_status = str(status)
        if record.special in KEY_DOOR_SPECIALS:
            record.state = "requires_key"
        elif record.special in RETRIGGERABLE_SPECIALS:
            record.state = "congested"
        elif record.force_follow_failures >= self.max_failures:
            record.state = "blocked"
        elif record.state == "unknown":
            record.state = "closed"
        record.updated_at = int(time.time())

    def observe_line(self, line_id: int | None, *, special: int = 0, tag: int = 0, exit_line: bool = False) -> None:
        if line_id is None:
            return
        record = self._records.setdefault(int(line_id), DoorRecord(line_id=int(line_id)))
        record.special = int(special)
        record.tag = int(tag)
        if exit_line:
            record.state = "exit"
        elif record.state == "unknown":
            record.state = "closed" if special else "unknown"
        record.updated_at = int(time.time())

    def record_attempt(self, line_id: int | None, *, status: str = "attempt") -> None:
        if line_id is None:
            return
        record = self._records.setdefault(int(line_id), DoorRecord(line_id=int(line_id)))
        record.attempts += 1
        if record.special in RETRIGGERABLE_SPECIALS and "use" in str(status):
            record.state = "opening"
            record.stale_opens = 0
        elif record.tag > 0 and record.special > 0 and "use" in str(status):
            # Tagged trigger (S-type floor/stair/remote-door switch): assume it fired so the
            # matching tag gate opens for routing; stale-open repair walks it back if not.
            record.state = "opening"
            record.stale_opens = 0
        elif record.state == "unknown":
            record.state = "closed"
        record.last_status = status
        record.updated_at = int(time.time())

    def record_success(self, line_id: int | None) -> None:
        if line_id is None:
            return
        record = self._records.setdefault(int(line_id), DoorRecord(line_id=int(line_id)))
        record.successes += 1
        record.state = "opened"
        record.last_status = "opened"
        record.stale_opens = 0
        record.updated_at = int(time.time())

    def record_progress(self, line_id: int | None) -> None:
        if line_id is None:
            return
        record = self._records.setdefault(int(line_id), DoorRecord(line_id=int(line_id)))
        if record.special in LIFT_WALK_SPECIALS and record.tag > 0:
            if record.state in {"opening", "closed", "unknown"}:
                record.successes += 1
            record.state = "opening"
            record.last_status = "walk_lift_triggered"
            record.stale_opens = 0
            record.updated_at = int(time.time())
            return
        if record.state in {"opening", "closed", "unknown"}:
            record.state = "opened"
            record.successes += 1
            record.last_status = "progress_after_line"
        record.stale_opens = 0
        record.updated_at = int(time.time())

    def record_stale_open(self, line_id: int | None) -> None:
        if line_id is None:
            return
        record = self._records.setdefault(int(line_id), DoorRecord(line_id=int(line_id)))
        record.stale_opens += 1
        record.last_status = "stale_open_blocked"
        if record.special in KEY_DOOR_SPECIALS:
            record.failures += 1
            record.state = "requires_key"
        elif record.special in RETRIGGERABLE_SPECIALS:
            if record.stale_opens < max(2, self.max_failures):
                record.state = "opening"
            else:
                record.failures += 1
                record.state = "closed"
                record.failures = min(record.failures, max(0, self.max_failures - 1))
        elif record.stale_opens >= self.max_failures:
            record.failures += 1
            record.state = "blocked"
        else:
            record.failures += 1
            record.state = "closed"
        record.updated_at = int(time.time())

    def record_failure(self, line_id: int | None, *, status: str = "blocked") -> None:
        if line_id is None:
            return
        record = self._records.setdefault(int(line_id), DoorRecord(line_id=int(line_id)))
        record.last_status = status
        if record.state == "exit" or record.special in EXIT_SPECIALS:
            record.state = "exit"
            record.failures = min(record.failures + 1, max(0, self.max_failures - 1))
            record.updated_at = int(time.time())
            return
        if "no_progress" in status and record.special in RETRIGGERABLE_SPECIALS:
            record.state = "opening"
            record.last_status = "assumed_opening"
            record.updated_at = int(time.time())
            return
        if record.special in RETRIGGERABLE_SPECIALS and ("route_contact" in status or "blocked" in status):
            record.failures = min(record.failures + 1, max(0, self.max_failures - 1))
            record.state = "closed"
            record.updated_at = int(time.time())
            return
        record.failures += 1
        if "key" in status:
            record.state = "requires_key"
        elif record.special in KEY_DOOR_SPECIALS:
            record.state = "requires_key"
        elif "switch" in status:
            record.state = "requires_switch"
        elif record.failures >= self.max_failures and record.successes == 0:
            record.state = "blocked"
        elif record.state == "unknown":
            record.state = "closed"
        record.updated_at = int(time.time())

    def summary(self) -> dict[str, Any]:
        blocked = [line_id for line_id, record in self._records.items() if self.is_blocked(line_id)]
        return {
            "tracked": len(self._records),
            "blocked": blocked[:8],
            "congested": [line_id for line_id, record in self._records.items() if record.state == "congested"][:8],
            "opened": [line_id for line_id, record in self._records.items() if record.state == "opened"][:8],
            "opening": [line_id for line_id, record in self._records.items() if record.state == "opening"][:8],
            "key": [line_id for line_id, record in self._records.items() if record.state == "requires_key"][:8],
            "switch": [line_id for line_id, record in self._records.items() if record.state == "requires_switch"][:8],
            "exit": [line_id for line_id, record in self._records.items() if record.state == "exit"][:8],
            "attempts": sum(record.attempts for record in self._records.values()),
            "stale": sum(record.stale_opens for record in self._records.values()),
            "force_follow": sum(record.force_follow_failures for record in self._records.values()),
        }
