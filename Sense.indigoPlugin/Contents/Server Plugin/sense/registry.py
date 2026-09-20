"""Decide what to do with Indigo devices given what Sense reports. Pure functions, no Indigo.

Identity is the Sense id, never the name: Sense re-learns appliances under new ids, keeps
the name, and the old id comes back tagged Revoked. The plan below frees the old device's
name (rename + disable) before creating the new one, so a re-learned appliance always gets a
fresh, live Indigo device and the old one stays around, disabled, for history.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sense.client import DiscoveredDevice

CORE_ID = "core"
REVOKED_SUFFIX = " (revoked {id})"
STALE_SUFFIX = " (stale {id})"


@dataclass
class IndigoView:
    """What the planner needs to know about one existing plugin device."""

    dev_id: int
    sense_id: str
    name: str
    enabled: bool
    power: float = 0.0
    is_on: bool | None = None  # None: the isOn state has never been written


@dataclass
class Create:
    sense_id: str
    name: str


@dataclass
class Rename:
    dev_id: int
    name: str


@dataclass
class UpdatePower:
    dev_id: int
    watts: float


@dataclass
class Retire:
    """Free the name and disable: Sense revoked this id (or it vanished while disabled)."""

    dev_id: int
    name: str


@dataclass
class Revive:
    """Sense reports the id again after we retired it: restore the name and enable."""

    dev_id: int
    name: str


@dataclass
class Delete:
    dev_id: int


@dataclass
class Plan:
    actions: list = field(default_factory=list)


def _retired_name(name: str, sense_id: str) -> str:
    return name + REVOKED_SUFFIX.format(id=sense_id)


def _is_retired_name(name: str, sense_id: str) -> bool:
    return name.endswith(REVOKED_SUFFIX.format(id=sense_id)) or name.endswith(
        STALE_SUFFIX.format(id=sense_id)
    )


def plan(
    discovered: list[DiscoveredDevice],
    realtime: dict[str, tuple[str, float]],
    existing: dict[str, IndigoView],
    include_solar: bool = False,
) -> list:
    """Return the ordered actions that bring the Indigo devices in line with Sense.

    `realtime` maps Sense id -> (name, watts) for appliances currently running; anything
    else is at 0 W. `existing` maps Sense id -> IndigoView for every plugin device, enabled
    or not (the Active Total device, id "core", is handled by the caller and ignored here).
    """
    actions: list = []
    seen: set[str] = set()

    # 1. Retire revoked ids first so their names are free for re-learned appliances.
    for d in discovered:
        if not d.revoked:
            continue
        seen.add(d.id)
        view = existing.get(d.id)
        if view is None:
            continue
        if view.enabled or not _is_retired_name(view.name, d.id):
            actions.append(Retire(view.dev_id, _retired_name(_base_name(view.name, d.id), d.id)))

    # 2. Merged appliances: Sense folded them into another one; the Indigo device goes away.
    for d in discovered:
        for merged_id in d.merged_ids:
            view = existing.get(merged_id)
            if view is not None:
                seen.add(merged_id)
                actions.append(Delete(view.dev_id))

    # 3. Live appliances: create, rename, revive, and push power.
    for d in discovered:
        if d.revoked or d.id == CORE_ID:
            continue
        if d.id == "solar" and not include_solar:
            continue
        seen.add(d.id)
        watts = float(realtime.get(d.id, ("", 0.0))[1])
        view = existing.get(d.id)
        if view is None:
            actions.append(Create(d.id, d.name))
            continue
        if not view.enabled and _is_retired_name(view.name, d.id):
            actions.append(Revive(view.dev_id, d.name))
        elif view.enabled and view.name != d.name:
            actions.append(Rename(view.dev_id, d.name))
        # Indigo shows whole watts; only a change at that resolution is worth a write - or an
        # isOn state that disagrees with the power (never written on a migrated device).
        if view.enabled and (
            int(round(float(view.power))) != int(round(watts)) or view.is_on != (watts > 0)
        ):
            actions.append(UpdatePower(view.dev_id, watts))

    # 4. Devices Sense no longer lists at all: leave enabled ones alone (an incomplete API
    #    answer must not disturb anything), but free the name of a disabled leftover so it
    #    cannot block a re-learned appliance with the same name.
    for sense_id, view in existing.items():
        if sense_id in seen or sense_id == CORE_ID:
            continue
        if not view.enabled and not _is_retired_name(view.name, sense_id):
            actions.append(
                Retire(
                    view.dev_id, _base_name(view.name, sense_id) + STALE_SUFFIX.format(id=sense_id)
                )
            )
    return actions


def _base_name(name: str, sense_id: str) -> str:
    for suffix in (REVOKED_SUFFIX, STALE_SUFFIX):
        s = suffix.format(id=sense_id)
        if name.endswith(s):
            return name[: -len(s)]
    return name
