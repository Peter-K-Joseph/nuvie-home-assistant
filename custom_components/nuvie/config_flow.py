"""Set up Nuvie Planner: pair with a Nuvie account (or use an unzipped export), pick what the sync does.

Pairing: Home Assistant asks Nuvie for a code and shows a link; the person opens it,
signs in to Nuvie, picks the project and approves. Meanwhile this flow polls Nuvie
and finishes on its own once it's approved.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_REAUTH, ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import instance_id

from . import data_dir, read_manifest
from .cloud import NuvieError, Paired, Pairing, PairingDenied, PairingExpired
from .const import (
    CONF_ASSIGN_ENTITIES,
    CONF_PROJECT_ID,
    CONF_SOURCE,
    CONF_SYNC_AREAS,
    CONF_TOKEN,
    CONF_URL,
    DEFAULT_URL,
    DOMAIN,
    MANIFEST_FILE,
    SOURCE_CLOUD,
    SOURCE_FILES,
)

DEFAULT_OPTIONS = {CONF_SYNC_AREAS: True, CONF_ASSIGN_ENTITIES: True}


def _schema(defaults: dict[str, Any]) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_SYNC_AREAS, default=defaults.get(CONF_SYNC_AREAS, True)): bool,
            vol.Required(CONF_ASSIGN_ENTITIES, default=defaults.get(CONF_ASSIGN_ENTITIES, True)): bool,
        }
    )


class NuvieConfigFlow(ConfigFlow, domain=DOMAIN):
    """One entry: a project paired from Nuvie, or the export in <config>/nuvie/."""

    VERSION = 1

    def __init__(self) -> None:
        self._url = DEFAULT_URL
        self._pairing: Pairing | None = None
        self._poll: asyncio.Task[Paired] | None = None
        self._paired: Paired | None = None
        self._error = "cannot_connect"

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=[SOURCE_CLOUD, SOURCE_FILES])

    # ------------------------------------------------------------- Nuvie ----

    async def async_step_cloud(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Pair with Nuvie. Advanced mode can point at another Nuvie server."""
        if user_input is None and self.show_advanced_options:
            return self.async_show_form(step_id="cloud", data_schema=vol.Schema({vol.Required(CONF_URL, default=self._url): str}))
        if user_input is not None:
            self._url = str(user_input.get(CONF_URL) or DEFAULT_URL).strip().rstrip("/")
            if not self._url.startswith(("https://", "http://")):
                return self.async_show_form(step_id="cloud", data_schema=vol.Schema({vol.Required(CONF_URL, default=self._url): str}), errors={"base": "bad_url"})
        return await self.async_step_pair()

    def _client(self, token: str | None = None):
        from .updater import client_for

        return client_for(self.hass, self._url, token)

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._pairing is None:
            try:
                self._pairing = await self._client().start_pairing(self.hass.config.location_name or "Home Assistant", await instance_id.async_get(self.hass))
            except NuvieError:
                return self.async_abort(reason="cannot_connect")
        if self._poll is None:
            self._poll = self.hass.async_create_task(self._wait_for_approval(self._pairing), eager_start=False)
        if not self._poll.done():
            return self.async_show_progress(
                step_id="pair",
                progress_action="pair",
                progress_task=self._poll,
                description_placeholders={"url": self._pairing.url, "code": self._pairing.user_code},
            )
        try:
            self._paired = self._poll.result()
        except PairingDenied:
            self._error = "denied"
        except PairingExpired:
            self._error = "expired"
        except NuvieError:
            self._error = "cannot_connect"
        finally:
            self._poll = None
            self._pairing = None
        return self.async_show_progress_done(next_step_id="paired" if self._paired else "pair_failed")

    async def _wait_for_approval(self, pairing: Pairing) -> Paired:
        """Poll until the person approves in Nuvie (or declines, or the code expires)."""
        client = self._client()
        deadline = time.monotonic() + pairing.expires_in
        errors = 0
        while time.monotonic() < deadline:
            await asyncio.sleep(pairing.interval)
            try:
                paired = await client.poll_pairing(pairing.device_code)
            except (PairingDenied, PairingExpired):
                raise
            except NuvieError:
                # a dropped connection now and then is fine; Nuvie being gone for a minute isn't
                errors += 1
                if errors * pairing.interval > 60:
                    raise
                continue
            errors = 0
            if paired is not None:
                return paired
        raise PairingExpired("The pairing code expired")

    @callback
    def async_remove(self) -> None:
        """The dialog was closed: stop polling Nuvie."""
        if self._poll is not None and not self._poll.done():
            self._poll.cancel()

    async def async_step_pair_failed(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_abort(reason=self._error)

    async def async_step_paired(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._paired is not None
        data = {CONF_SOURCE: SOURCE_CLOUD, CONF_URL: self._url, CONF_TOKEN: self._paired.token, CONF_PROJECT_ID: self._paired.project_id}
        if self.source == SOURCE_REAUTH:
            return self.async_update_reload_and_abort(self._get_reauth_entry(), title=self._paired.project_name, data=data)
        return self.async_create_entry(title=self._paired.project_name, data=data, options=dict(DEFAULT_OPTIONS))

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> ConfigFlowResult:
        """The connection was revoked in Nuvie (or the project is gone): pair again."""
        self._url = entry_data.get(CONF_URL, DEFAULT_URL)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        return await self.async_step_pair()

    # ------------------------------------------------------------- files ----

    async def async_step_files(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The export unzipped into <config>/nuvie/."""
        path = data_dir(self.hass) / MANIFEST_FILE
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                manifest = await self.hass.async_add_executor_job(read_manifest, path)
            except HomeAssistantError:
                errors["base"] = "no_export" if not await self.hass.async_add_executor_job(path.exists) else "bad_export"
            else:
                return self.async_create_entry(title=manifest["project"].get("name") or "Nuvie plan", data={CONF_SOURCE: SOURCE_FILES}, options=user_input)
        return self.async_show_form(
            step_id="files",
            data_schema=_schema(user_input or {}),
            errors=errors,
            description_placeholders={"path": str(path)},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return NuvieOptionsFlow()


class NuvieOptionsFlow(OptionsFlow):
    """Change what the sync does (saving re-runs it)."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        return self.async_show_form(step_id="init", data_schema=_schema(dict(self.config_entry.options)))
