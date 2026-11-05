"""Nuvie Planner: the house you planned in Nuvie, in Home Assistant.

- Gets the plan from Nuvie: paired with a Nuvie account, it downloads the
  project and keeps it current (updater.py); or it reads an export unzipped
  into <config>/nuvie/ (nuvie_home.json + model.glb). Either way the files
  end up in <config>/nuvie/, and everything below reads them from there.
- Creates the plan's floors and rooms as Home Assistant floors and areas, and
  puts linked devices in their rooms (only ones that have no room yet).
- Serves the plan and the 3D model to the `custom:nuvie-home-card` dashboard
  card at /api/nuvie/manifest and /api/nuvie/model (signed-in users only),
  and loads the card's script on every dashboard.
- `nuvie.sync` runs the sync again (paired: after fetching the latest plan;
  unzipped: e.g. after copying in a new export). A new export is also synced
  on its own at the next start, and a paired one whenever Nuvie has a newer plan.
- Links made in the card's edit mode (admins only, POST /api/nuvie/links) are
  kept in Home Assistant's storage and win over the plan's: a newer export
  doesn't undo them.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from aiohttp import web

from homeassistant.components import persistent_notification
from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import HomeAssistantView, StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError, Unauthorized
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import floor_registry as fr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .const import (
    CARD_FILE,
    CARD_URL,
    CONF_ASSIGN_ENTITIES,
    CONF_SOURCE,
    CONF_SYNC_AREAS,
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_URL,
    DATA_DIR,
    LINKS_STORAGE_KEY,
    DOMAIN,
    MANIFEST_FILE,
    MODEL_FILE,
    SERVICE_SYNC,
    SOURCE_CLOUD,
    STORAGE_KEY,
    STORAGE_VERSION,
    UPDATE_MINUTES,
)
from .cloud import NuvieAuthError, NuvieError
from .sync import ENTITY_RE, RING, EntityInfo, SyncPlan, apply_links, link_key, linked_entities, plan_sync, summary, validate_manifest

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

NOTIFICATION_ID = "nuvie_sync"


def read_manifest(path: Path) -> dict[str, Any]:
    """Load and check nuvie_home.json (blocking: run in the executor)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as err:
        raise HomeAssistantError(f"{path} not found - unzip the Nuvie export into the config folder") from err
    except (OSError, ValueError) as err:
        raise HomeAssistantError(f"Couldn't read {path}: {err}") from err
    problem = validate_manifest(data)
    if problem:
        raise HomeAssistantError(f"{path} isn't a Nuvie export: {problem}")
    return data


def data_dir(hass: HomeAssistant) -> Path:
    return Path(hass.config.path(DATA_DIR))


class Links:
    """Links made in Home Assistant (link key → entity id, "" = not linked), kept in .storage."""

    def __init__(self, hass: HomeAssistant) -> None:
        self.store: Store[dict[str, str]] = Store(hass, STORAGE_VERSION, LINKS_STORAGE_KEY)
        self.data: dict[str, str] = {}

    async def async_load(self) -> None:
        self.data = await self.store.async_load() or {}

    async def async_set(self, key: str, entity_id: str | None) -> None:
        """entity_id None = back to the plan's link."""
        if entity_id is None:
            self.data.pop(key, None)
        else:
            self.data[key] = entity_id
        await self.store.async_save(self.data)


def links(hass: HomeAssistant) -> Links:
    return hass.data[DOMAIN]["links"]


async def async_load_manifest(hass: HomeAssistant) -> dict[str, Any]:
    """The export with the links made in Home Assistant applied."""
    manifest = await hass.async_add_executor_job(read_manifest, data_dir(hass) / MANIFEST_FILE)
    return apply_links(manifest, links(hass).data)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Card script, API views and the sync service (once, whatever the entries)."""
    integration = await async_get_integration(hass, DOMAIN)
    card = Path(__file__).parent / "frontend" / CARD_FILE
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, str(card), True)])
    # ?v= busts the browser cache when the integration is updated
    add_extra_js_url(hass, f"{CARD_URL}?v={integration.version}")
    hass.data[DOMAIN] = {"links": Links(hass), "updaters": {}}
    await links(hass).async_load()
    hass.http.register_view(NuvieManifestView())
    hass.http.register_view(NuvieModelView())
    hass.http.register_view(NuvieLinksView())

    async def handle_sync(call: ServiceCall) -> None:
        entries = hass.config_entries.async_entries(DOMAIN)
        if not entries:
            raise HomeAssistantError("Set up Nuvie Planner first (Settings → Devices & services)")
        updater = updaters(hass).get(entries[0].entry_id)
        if updater is not None:
            try:
                await updater.async_refresh()
            except NuvieAuthError as err:
                entries[0].async_start_reauth(hass)
                raise HomeAssistantError(f"Nuvie: {err}") from err
            except NuvieError as err:
                raise HomeAssistantError(f"Nuvie: {err}") from err
        await async_sync(hass, entries[0], force=True)

    hass.services.async_register(DOMAIN, SERVICE_SYNC, handle_sync)
    return True


def updaters(hass: HomeAssistant) -> dict[str, Any]:
    """entry id → CloudUpdater, for entries paired with Nuvie."""
    return hass.data[DOMAIN]["updaters"]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Sync the export into the registries when it's new (or the options changed)."""
    if entry.data.get(CONF_SOURCE) == SOURCE_CLOUD:
        await _setup_cloud(hass, entry)
    try:
        await hass.async_add_executor_job(read_manifest, data_dir(hass) / MANIFEST_FILE)
    except HomeAssistantError as err:
        raise ConfigEntryNotReady(str(err)) from err
    await async_sync(hass, entry)
    entry.async_on_unload(entry.add_update_listener(_options_changed))
    return True


async def _setup_cloud(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Fetch the latest plan from Nuvie now, then check for a newer one every few minutes."""
    from .updater import CloudUpdater

    integration = await async_get_integration(hass, DOMAIN)
    updater = CloudUpdater(hass, entry, str(integration.version or ""))
    await updater.async_load()
    try:
        await updater.async_refresh()
    except NuvieAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except NuvieError as err:
        # Nuvie unreachable: carry on with the copy we have, if there is one
        if not await hass.async_add_executor_job((data_dir(hass) / MANIFEST_FILE).is_file):
            raise ConfigEntryNotReady(str(err)) from err
        _LOGGER.warning("Nuvie: couldn't check for a newer plan, using the one from last time: %s", err)
    updaters(hass)[entry.entry_id] = updater

    @callback
    def _forget() -> None:
        updaters(hass).pop(entry.entry_id, None)
        updater.async_stop()

    entry.async_on_unload(_forget)

    async def _tick(_now: Any) -> None:
        try:
            if await updater.async_refresh():
                await async_sync(hass, entry, quiet=True)
        except NuvieAuthError as err:
            _LOGGER.warning("Nuvie: %s", err)
            entry.async_start_reauth(hass)
        except NuvieError as err:
            _LOGGER.debug("Nuvie: couldn't check for a newer plan: %s", err)

    entry.async_on_unload(async_track_time_interval(hass, _tick, timedelta(minutes=UPDATE_MINUTES), name="nuvie_update", cancel_on_shutdown=True))


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    return True


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Removed in Home Assistant: tell Nuvie to forget this connection (best effort)."""
    if entry.data.get(CONF_SOURCE) != SOURCE_CLOUD or not entry.data.get(CONF_TOKEN):
        return
    from .updater import client_for

    try:
        await client_for(hass, entry.data.get(CONF_URL, DEFAULT_URL), entry.data[CONF_TOKEN]).disconnect()
    except NuvieError as err:
        _LOGGER.info("Nuvie: couldn't tell Nuvie to disconnect (remove it under Settings → Home Assistant there): %s", err)


async def _options_changed(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await async_sync(hass, entry, force=True)


async def async_sync(hass: HomeAssistant, entry: ConfigEntry, *, force: bool = False, quiet: bool = False) -> SyncPlan | None:
    """Apply the export to the registries. Skips an export (and options) already synced unless `force`.

    `quiet` (a plan Nuvie sent on its own): no notification unless the sync changed something.
    """
    manifest = await async_load_manifest(hass)
    sync_areas = entry.options.get(CONF_SYNC_AREAS, True)
    assign = entry.options.get(CONF_ASSIGN_ENTITIES, True)

    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    stamp = hashlib.sha256(
        json.dumps([manifest.get("generatedAt"), manifest["floors"], manifest["areas"], manifest["devices"], sync_areas, assign], sort_keys=True).encode()
    ).hexdigest()
    saved = await store.async_load() or {}
    if not force and saved.get("stamp") == stamp:
        return None

    floors = fr.async_get(hass)
    areas = ar.async_get(hass)
    entities = er.async_get(hass)
    devices = dr.async_get(hass)

    known: dict[str, EntityInfo | None] = {}
    for entity_id, _area in linked_entities(manifest):
        reg = entities.async_get(entity_id)
        if reg is not None:
            device = devices.async_get(reg.device_id) if reg.device_id else None
            known[entity_id] = EntityInfo(reg.area_id, device.area_id if device else None)
        elif hass.states.get(entity_id) is not None:
            known[entity_id] = None  # exists, but has no registry entry to give an area to

    plan = plan_sync(
        manifest,
        {f.name: f.floor_id for f in floors.async_list_floors()},
        {a.name: (a.id, a.floor_id) for a in areas.async_list_areas()},
        known,
        sync_areas=sync_areas,
        assign_entities=assign,
    )

    floor_ids = dict(plan.floor_ids)
    for f in plan.new_floors:
        floor_ids[f["key"]] = floors.async_create(f["name"], level=f["level"]).floor_id
    area_ids = dict(plan.area_ids)
    for a in plan.new_areas:
        area_ids[a["key"]] = areas.async_create(a["name"], floor_id=floor_ids.get(a["floor_key"])).id
    for area_id, floor_key in plan.area_floors.items():
        if floor_key in floor_ids:
            areas.async_update(area_id, floor_id=floor_ids[floor_key])
    for entity_id, area_key in plan.entity_areas.items():
        entities.async_update_entity(entity_id, area_id=area_ids[area_key])

    text = summary(plan)
    _LOGGER.info("Nuvie sync (%s): %s", manifest["project"].get("name"), text)
    if not (quiet and plan.empty):
        persistent_notification.async_create(hass, text, title=f"Nuvie: {manifest['project'].get('name', 'plan')} synced", notification_id=NOTIFICATION_ID)
    await store.async_save({"stamp": stamp})
    return plan


class NuvieManifestView(HomeAssistantView):
    """The export's device map, for the dashboard card."""

    url = "/api/nuvie/manifest"
    name = "api:nuvie:manifest"
    requires_auth = True

    async def get(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        try:
            manifest = await async_load_manifest(hass)
        except HomeAssistantError as err:
            return self.json_message(str(err), 404)
        return self.json({**manifest, "can_edit_links": request["hass_user"].is_admin})


class NuvieModelView(HomeAssistantView):
    """The export's 3D model (GLB), for the dashboard card."""

    url = "/api/nuvie/model"
    name = "api:nuvie:model"
    requires_auth = True

    async def get(self, request: web.Request) -> web.StreamResponse:
        hass: HomeAssistant = request.app["hass"]
        path = data_dir(hass) / MODEL_FILE
        if not await hass.async_add_executor_job(path.is_file):
            return self.json_message("No 3D model yet (connected to Nuvie: it's still being built, or your plan has no 3D export; unzipped: tick 'Include the 3D model' when exporting)", 404)
        return web.FileResponse(path, headers={"Content-Type": "model/gltf-binary", "Cache-Control": "no-cache"})


class NuvieLinksView(HomeAssistantView):
    """Link a planned device (or one switch button) to an entity, from the card's edit mode. Admins only.

    POST {"device_id": "...", "gang_id": "..." | "@ring" | null, "entity_id": "light.x" | "" | null}
    gang_id is a switch button's id, or "@ring" for a doorbell's ring entity.
    "" = not linked, null = back to the plan's link. Answers with the device as the card sees it.
    """

    url = "/api/nuvie/links"
    name = "api:nuvie:links"
    requires_auth = True

    async def post(self, request: web.Request) -> web.Response:
        hass: HomeAssistant = request.app["hass"]
        if not request["hass_user"].is_admin:
            raise Unauthorized()
        try:
            body = await request.json()
        except ValueError:
            return self.json_message("Expected JSON", 400)
        device_id = body.get("device_id") if isinstance(body, dict) else None
        gang_id = body.get("gang_id") if isinstance(body, dict) else None
        entity_id = body.get("entity_id") if isinstance(body, dict) else None
        if not isinstance(device_id, str) or (gang_id is not None and not isinstance(gang_id, str)):
            return self.json_message("device_id (and gang_id) must be strings", 400)
        if entity_id is not None and not isinstance(entity_id, str):
            return self.json_message("entity_id must be a string or null", 400)
        if entity_id and not ENTITY_RE.match(entity_id):
            return self.json_message(f"{entity_id!r} isn't an entity id", 400)
        if entity_id and hass.states.get(entity_id) is None and er.async_get(hass).async_get(entity_id) is None:
            return self.json_message(f"{entity_id} isn't in Home Assistant", 400)
        try:
            manifest = await async_load_manifest(hass)
        except HomeAssistantError as err:
            return self.json_message(str(err), 404)
        device = next((d for d in manifest["devices"] if d["id"] == device_id), None)
        if device is None:
            return self.json_message(f"No device {device_id} in the plan", 404)
        if gang_id == RING:
            if "ring_entity_id" not in device:
                return self.json_message(f"{device_id} isn't a doorbell", 404)
        elif gang_id and not any(c.get("gang_id") == gang_id for c in device.get("controls") or []):
            return self.json_message(f"{device_id} has no button {gang_id}", 404)

        await links(hass).async_set(link_key(device_id, gang_id), entity_id)
        if entity_id:
            await _place(hass, manifest, entity_id, device.get("area_id"))
        manifest = await async_load_manifest(hass)
        return self.json(next(d for d in manifest["devices"] if d["id"] == device_id))


async def _place(hass: HomeAssistant, manifest: dict[str, Any], entity_id: str, area_key: str | None) -> None:
    """Put a newly linked entity in its plan room, when the options allow it and it has no room yet."""
    entries = hass.config_entries.async_entries(DOMAIN)
    if not entries or not entries[0].options.get(CONF_ASSIGN_ENTITIES, True) or not area_key:
        return
    plan_area = next((a for a in manifest["areas"] if a["area_id"] == area_key), None)
    area = ar.async_get(hass).async_get_area_by_name(plan_area["name"]) if plan_area else None
    reg = er.async_get(hass).async_get(entity_id)
    if area is None or reg is None or reg.area_id:
        return
    device = dr.async_get(hass).async_get(reg.device_id) if reg.device_id else None
    if device and device.area_id:
        return
    er.async_get(hass).async_update_entity(entity_id, area_id=area.id)
