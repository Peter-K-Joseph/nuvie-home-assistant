"""What syncing a Nuvie export into Home Assistant would change.

Pure (no Home Assistant imports) so it can be tested on its own: the caller
takes a snapshot of the floor / area / entity registries, asks `plan_sync`
what to do, then applies the plan (see __init__.py).

Rules:
- floors and areas are matched by name (ignoring case and spaces, as Home
  Assistant does); missing ones are created, existing ones are never renamed
  or deleted
- an existing area with no floor is put on its plan floor; one already on a
  floor is left there
- a linked entity is put in its room only when neither it nor its device has
  an area yet (a room someone picked by hand is never overwritten)
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .const import SCHEMA_PREFIX


def validate_manifest(data: Any) -> str | None:
    """None when `data` is a Nuvie export manifest, else what's wrong with it."""
    if not isinstance(data, dict):
        return "not a JSON object"
    schema = data.get("schema")
    if not isinstance(schema, str) or not schema.startswith(SCHEMA_PREFIX):
        return f"unknown schema {schema!r} (expected {SCHEMA_PREFIX})"
    for key in ("floors", "areas", "devices"):
        if not isinstance(data.get(key), list):
            return f"'{key}' is missing"
    for f in data["floors"]:
        if not isinstance(f, dict) or not f.get("floor_id") or not f.get("name"):
            return "a floor has no floor_id / name"
    for a in data["areas"]:
        if not isinstance(a, dict) or not a.get("area_id") or not a.get("name"):
            return "an area has no area_id / name"
    for d in data["devices"]:
        if not isinstance(d, dict) or not d.get("id"):
            return "a device has no id"
    return None


@dataclass
class EntityInfo:
    """What the entity registry knows about a linked entity."""

    area_id: str | None
    device_area_id: str | None = None


@dataclass
class SyncPlan:
    """The changes a sync makes. Keys are the export's floor_id / area_id."""

    new_floors: list[dict[str, Any]] = field(default_factory=list)
    new_areas: list[dict[str, Any]] = field(default_factory=list)
    # existing HA area id → plan floor key
    area_floors: dict[str, str] = field(default_factory=dict)
    # entity id → plan area key
    entity_areas: dict[str, str] = field(default_factory=dict)
    # plan key → existing HA id
    floor_ids: dict[str, str] = field(default_factory=dict)
    area_ids: dict[str, str] = field(default_factory=dict)
    # plan key → plan key of an earlier floor / area with the same name
    aliases: dict[str, str] = field(default_factory=dict)
    # linked in the plan but not in Home Assistant
    missing_entities: list[str] = field(default_factory=list)
    # in Home Assistant, but already in a room (left alone)
    kept_entities: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.new_floors or self.new_areas or self.area_floors or self.entity_areas)


def _key(name: str) -> str:
    # Home Assistant's own name matching (helpers/normalized_name_base_registry), so
    # "Bedroom 1" finds "bedroom1" and a create never clashes with an existing name
    return name.casefold().replace(" ", "")


def plan_sync(
    manifest: dict[str, Any],
    floors_by_name: dict[str, str],
    areas_by_name: dict[str, tuple[str, str | None]],
    entities: dict[str, EntityInfo | None],
    *,
    sync_areas: bool = True,
    assign_entities: bool = True,
) -> SyncPlan:
    """Work out what to create / assign.

    floors_by_name: floor name → HA floor id
    areas_by_name: area name → (HA area id, HA floor id or None)
    entities: entity id → its registry info; None = it exists but has no
        registry entry (a YAML entity without a unique id), so it can't be
        given an area. Entities left out don't exist.
    """
    plan = SyncPlan()
    floors = {_key(n): fid for n, fid in floors_by_name.items()}
    areas = {_key(n): v for n, v in areas_by_name.items()}

    new_floor: dict[str, str] = {}
    for f in manifest["floors"]:
        name = _key(f["name"])
        found = floors.get(name)
        if found:
            plan.floor_ids[f["floor_id"]] = found
        elif name in new_floor:
            plan.aliases[f["floor_id"]] = new_floor[name]
        elif sync_areas:
            new_floor[name] = f["floor_id"]
            plan.new_floors.append({"key": f["floor_id"], "name": f["name"], "level": f.get("level")})

    new_area: dict[str, str] = {}
    for a in manifest["areas"]:
        name = _key(a["name"])
        found = areas.get(name)
        if name in new_area:
            plan.aliases[a["area_id"]] = new_area[name]
        elif found:
            area_id, floor_id = found
            plan.area_ids[a["area_id"]] = area_id
            if sync_areas and floor_id is None and a.get("floor_id"):
                plan.area_floors[area_id] = plan.aliases.get(a["floor_id"], a["floor_id"])
        elif sync_areas:
            new_area[name] = a["area_id"]
            plan.new_areas.append({"key": a["area_id"], "name": a["name"], "floor_key": plan.aliases.get(a.get("floor_id"), a.get("floor_id"))})

    known_areas = set(plan.area_ids) | {a["key"] for a in plan.new_areas}
    for entity_id, area_id in linked_entities(manifest):
        if entity_id not in entities:
            plan.missing_entities.append(entity_id)
            continue
        info = entities[entity_id]
        if info is None:
            continue
        area_key = plan.aliases.get(area_id, area_id)
        if not assign_entities or not area_key or area_key not in known_areas:
            continue
        if info.area_id or info.device_area_id:
            plan.kept_entities.append(entity_id)
            continue
        plan.entity_areas[entity_id] = area_key

    return plan


def linked_entities(manifest: dict[str, Any]) -> list[tuple[str, str | None]]:
    """Every linked entity once, with its device's plan area: devices, then smart-switch buttons, then doorbell rings."""
    out: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for d in manifest["devices"]:
        ids = [d.get("entity_id")] + [c.get("entity_id") for c in d.get("controls") or []] + [d.get("ring_entity_id")]
        for entity_id in ids:
            if entity_id and entity_id not in seen:
                seen.add(entity_id)
                out.append((entity_id, d.get("area_id")))
    return out


ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")

# the link slot of a doorbell's ring entity (a switch button's slot is its gang id)
RING = "@ring"


def link_key(device_id: str, gang_id: str | None = None) -> str:
    """Key of a link made in Home Assistant: the device, one of its switch buttons, or its ring (RING)."""
    return f"{device_id}#{gang_id}" if gang_id else device_id


def apply_links(manifest: dict[str, Any], links: dict[str, str]) -> dict[str, Any]:
    """The manifest with links made in Home Assistant (the 3D card's edit mode) over the plan's.

    links: link key → entity id, or "" for "not linked". A device or button
    whose link came from here gets `linked_in: "home_assistant"`.
    """
    if not links:
        return manifest
    devices = []
    for d in manifest["devices"]:
        d = dict(d)
        key = link_key(d["id"])
        if key in links:
            d["entity_id"] = links[key] or None
            d["linked_in"] = "home_assistant"
        ring = link_key(d["id"], RING)
        if "ring_entity_id" in d and ring in links:
            d["ring_entity_id"] = links[ring] or None
            d["ring_linked_in"] = "home_assistant"
        if d.get("controls"):
            controls = []
            for c in d["controls"]:
                c = dict(c)
                key = link_key(d["id"], c.get("gang_id"))
                if c.get("gang_id") and key in links:
                    c["entity_id"] = links[key] or None
                    c["linked_in"] = "home_assistant"
                controls.append(c)
            d["controls"] = controls
        devices.append(d)
    return {**manifest, "devices": devices}


def summary(plan: SyncPlan, applied: bool = True) -> str:
    """One readable paragraph for a notification / the log."""
    did = "Created" if applied else "Would create"
    parts: list[str] = []
    if plan.new_floors:
        parts.append(f"{len(plan.new_floors)} floor(s)")
    if plan.new_areas:
        parts.append(f"{len(plan.new_areas)} area(s)")
    text = f"{did} {', '.join(parts)}." if parts else "Floors and areas were already set up."
    if plan.area_floors:
        text += f" Put {len(plan.area_floors)} existing area(s) on their floor."
    if plan.entity_areas:
        text += f" Placed {len(plan.entity_areas)} device(s) in their rooms."
    if plan.kept_entities:
        text += f" Left {len(plan.kept_entities)} device(s) in the rooms already chosen for them."
    if plan.missing_entities:
        shown = ", ".join(plan.missing_entities[:5])
        more = f" and {len(plan.missing_entities) - 5} more" if len(plan.missing_entities) > 5 else ""
        text += f" Not found in Home Assistant: {shown}{more} (check the ids in Nuvie)."
    return text
