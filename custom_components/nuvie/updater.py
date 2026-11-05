"""Keeping a paired Home Assistant's copy of the plan current.

Every few minutes (and on `nuvie.sync`) it asks Nuvie for the plan with the
ETag of the copy it has; Nuvie answers 304 until the project is saved with
changes. A new plan is written into <config>/nuvie/ and <config>/www/nuvie/,
the same place a manual export goes, so everything else reads it as before.
The 3D model is fetched after a new plan; Nuvie may still be building it, so
that's retried in the background until it's ready.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .bundle import BundleError, read_bundle, write_atomic, write_bundle
from .cloud import NuvieClient, NuvieError
from .const import CLOUD_STORAGE_KEY, CONF_PROJECT_ID, CONF_TOKEN, CONF_URL, DATA_DIR, DEFAULT_URL, MODEL_FILE, STORAGE_VERSION

_LOGGER = logging.getLogger(__name__)

# a model still building is asked for again, up to this many times (~ half an hour at most)
MAX_MODEL_TRIES = 60


def client_for(hass: HomeAssistant, url: str, token: str | None = None, version: str = "") -> NuvieClient:
    return NuvieClient(async_get_clientsession(hass), url, token, user_agent=f"HomeAssistant/{HA_VERSION} Nuvie/{version}".rstrip("/"))


class CloudUpdater:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, version: str = "") -> None:
        self.hass = hass
        self.entry = entry
        self.client = client_for(hass, entry.data.get(CONF_URL, DEFAULT_URL), entry.data[CONF_TOKEN], version)
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, CLOUD_STORAGE_KEY)
        self.state: dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._model_tries = 0
        self._retry_unsub: Callable[[], None] | None = None

    @property
    def model_path(self) -> Path:
        return Path(self.hass.config.path(DATA_DIR)) / MODEL_FILE

    async def async_load(self) -> None:
        state = await self._store.async_load() or {}
        # a new pairing (another project, or the same one again): start from scratch
        if state.get("project_id") != self.entry.data.get(CONF_PROJECT_ID):
            state = {"project_id": self.entry.data.get(CONF_PROJECT_ID)}
        self.state = state

    async def async_refresh(self) -> bool:
        """Fetch the plan if Nuvie has a newer one (and then its model). True when the plan changed.

        Raises NuvieAuthError when the connection is gone, NuvieError when Nuvie can't be reached.
        """
        async with self._lock:
            model_there = await self.hass.async_add_executor_job(self.model_path.is_file)
            plan = await self.client.fetch_plan(self.state.get("plan_etag"))
            changed = plan.data is not None
            if plan.data is not None:
                try:
                    files = await self.hass.async_add_executor_job(read_bundle, plan.data)
                except BundleError as err:
                    raise NuvieError(f"Nuvie sent a plan that couldn't be read: {err}") from err
                await self.hass.async_add_executor_job(write_bundle, Path(self.hass.config.path()), files)
                self.state["plan_etag"] = plan.etag
                _LOGGER.info("Nuvie: got the latest plan of %s", plan.project_name or self.entry.title)
            if plan.project_name and plan.project_name != self.entry.title:
                self.hass.config_entries.async_update_entry(self.entry, title=plan.project_name)
            await self._store.async_save(self.state)
        if changed or not model_there or self.state.get("model_pending"):
            self._model_tries = 0
            await self.async_fetch_model()
        return changed

    async def async_fetch_model(self) -> None:
        """Fetch the 3D model; while Nuvie is building it, try again later. Never raises."""
        self._cancel_retry()
        try:
            async with self._lock:
                there = await self.hass.async_add_executor_job(self.model_path.is_file)
                model = await self.client.fetch_model(self.state.get("model_etag") if there else None)
                if model.status == "ok" and model.data:
                    await self.hass.async_add_executor_job(write_atomic, self.model_path, model.data)
                    self.state["model_etag"] = model.etag
                    _LOGGER.info("Nuvie: got the 3D model (%.1f MB)", len(model.data) / 1e6)
                elif model.status == "none":
                    _LOGGER.info("Nuvie: no 3D model for this plan (%s); the card shows the rooms flat", model.reason or "not available")
                self.state["model_pending"] = model.status == "building"
                await self._store.async_save(self.state)
        except NuvieError as err:
            _LOGGER.warning("Nuvie: couldn't fetch the 3D model, will try again: %s", err)
            self.state["model_pending"] = True
            self._schedule_retry(60)
            return
        if model.status == "building":
            self._schedule_retry(model.retry_after)

    def _schedule_retry(self, delay: float) -> None:
        self._model_tries += 1
        if self._model_tries > MAX_MODEL_TRIES:
            _LOGGER.warning("Nuvie: the 3D model still isn't ready; trying again at the next update")
            return

        @callback
        def _retry(_now: Any) -> None:
            self._retry_unsub = None
            self.entry.async_create_background_task(self.hass, self.async_fetch_model(), "nuvie_model")

        self._retry_unsub = async_call_later(self.hass, delay, _retry)

    def _cancel_retry(self) -> None:
        if self._retry_unsub:
            self._retry_unsub()
            self._retry_unsub = None

    def async_stop(self) -> None:
        self._cancel_retry()
