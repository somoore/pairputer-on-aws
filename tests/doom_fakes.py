"""Shared fakes for Agent DOOM goal-contract tests.

These factories replace the ~170 inline ``class FakeAgentPb2`` / ``class
FakeController`` copies that used to live in ``test_agent_doom_goal_contract``.
The variants differ deliberately and those differences change the code path
under test, so the factories preserve each call site's exact choices:

* which ``ACTION_*`` constants EXIST (production reads them with
  ``getattr(agent_pb2, "ACTION_TURN_LEFT", None)`` fallbacks);
* whether ``RawTiccmd`` exists at all (production does
  ``getattr(agent_pb2, "RawTiccmd", None)`` and takes a non-raw path when it
  is absent);
* the shape of ``PlayerAction`` (a ``SimpleNamespace(**kwargs)`` passthrough,
  an explicit-signature variant that defaults ``action``/``amount``/
  ``duration_tics``/``raw``, or a real class with an ``__init__``).

The returned object is a real class, so tests that read
``FakeAgentPb2.ACTION_SHOOT`` in assertions keep working unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace

# Sentinel for "field not set" so builders can OMIT a key entirely (matching
# inline blocks that never define it) rather than setting it to None — the two
# differ under production's ``getattr(obj, "field", default)`` reads.
_OMIT = object()


def _simple_player_action(**kwargs):
    return SimpleNamespace(**kwargs)


def _explicit_player_action(**kwargs):
    return SimpleNamespace(
        action=kwargs.get("action", 0),
        amount=kwargs.get("amount", 0),
        duration_tics=kwargs.get("duration_tics", 1),
        raw=kwargs.get("raw"),
    )


class _ClassPlayerAction:
    def __init__(self, action=0, amount=0, duration_tics=1, raw=None):
        self.action = action
        self.amount = amount
        self.duration_tics = duration_tics
        self.raw = raw


def make_agent_pb2(*, raw=True, player_action="simple", **constants):
    """Build a fake ``agent_pb2`` module as a class.

    ``raw``            include a ``RawTiccmd`` staticmethod (``SimpleNamespace``
                       passthrough). Set ``False`` to exercise the non-raw
                       fallback path. May also be a callable to install a
                       specific ``RawTiccmd`` implementation.
    ``player_action``  ``"simple"`` (default) -> ``SimpleNamespace(**kwargs)``;
                       ``"explicit"`` -> defaults action/amount/duration_tics/raw;
                       ``"class"`` -> a class with an ``__init__``; or any
                       callable/class to install directly.
    ``**constants``    ``ACTION_*`` names to define as class attributes; only
                       the names passed exist on the result.
    """
    namespace: dict = dict(constants)

    if player_action is None:
        pass  # some tests use a constants-only module with no PlayerAction
    elif player_action == "simple":
        namespace["PlayerAction"] = staticmethod(_simple_player_action)
    elif player_action == "explicit":
        namespace["PlayerAction"] = staticmethod(_explicit_player_action)
    elif player_action == "class":
        namespace["PlayerAction"] = _ClassPlayerAction
    elif callable(player_action):
        namespace["PlayerAction"] = (
            staticmethod(player_action)
            if not isinstance(player_action, type)
            else player_action
        )
    else:
        raise ValueError(f"unknown player_action={player_action!r}")

    if raw is True:
        namespace["RawTiccmd"] = staticmethod(_simple_player_action)
    elif raw is False:
        pass
    elif callable(raw):
        namespace["RawTiccmd"] = (
            staticmethod(raw) if not isinstance(raw, type) else raw
        )
    else:
        raise ValueError(f"unknown raw={raw!r}")

    return type("FakeAgentPb2", (), namespace)


def make_enemy(
    *,
    id,
    x_fp,
    y_fp,
    type_id=3004,
    health=20,
    line_of_sight=True,
    distance_fp=_OMIT,
):
    """Build an enemy state block.

    Shape::

        SimpleNamespace(
            line_of_sight=...,
            object=SimpleNamespace(
                id=..., type_id=..., health=...,
                [distance_fp=...,]
                position=SimpleNamespace(x_fp=..., y_fp=...),
            ),
        )

    ``distance_fp`` is OMITTED entirely when left unset (its default
    ``_OMIT``), matching the inline blocks that never set it — production
    reads it via ``getattr`` defaults, so omission vs. ``None`` differs.
    """
    obj_kwargs = dict(id=id, type_id=type_id, health=health)
    if distance_fp is not _OMIT:
        obj_kwargs["distance_fp"] = distance_fp
    obj_kwargs["position"] = SimpleNamespace(x_fp=x_fp, y_fp=y_fp)
    return SimpleNamespace(
        line_of_sight=line_of_sight,
        object=SimpleNamespace(**obj_kwargs),
    )


def make_controller(mask_len, *, mask_value=True, heuristic_action_index=None):
    """Build a ``FakeController`` whose ``action_mask`` returns a fixed mask.

    ``mask_len``  length of the returned mask list.
    ``mask_value``  the value repeated across the mask (default ``True``).
    ``heuristic_action_index``  if given, install a
        ``heuristic_action_index`` method returning this value.
    """
    namespace: dict = {
        "action_mask": lambda self, _state: [mask_value] * mask_len,
    }
    if heuristic_action_index is not None:
        namespace["heuristic_action_index"] = (
            lambda self, _state, _v=heuristic_action_index: _v
        )
    return type("FakeController", (), namespace)
