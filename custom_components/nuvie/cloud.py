"""Talking to Nuvie Planner: pairing with an account, then fetching the plan and the 3D model.

Only aiohttp, no Home Assistant imports. The server side is apps/web/app/api/home-assistant/
in the Nuvie repository:

  POST /pair            start pairing → device_code, user_code, verification_uri_complete
  POST /pair/poll       → pending | approved (token, project) | denied | expired
  GET  /v1/plan         the plan zip (ETag; If-None-Match → 304)
  GET  /v1/model        the GLB (ETag), 202 while it's being built, 404 no_model, 403 plan
  DELETE /v1/connection forget this Home Assistant
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote

import aiohttp

API = "/api/home-assistant"
TIMEOUT = aiohttp.ClientTimeout(total=60)
MODEL_TIMEOUT = aiohttp.ClientTimeout(total=300)


class NuvieError(Exception):
    """Nuvie couldn't be reached or answered with an error (try again later)."""


class NuvieAuthError(NuvieError):
    """The connection was revoked, or its person can't open the project any more: pair again."""


class PairingDenied(NuvieError):
    """The person declined the pairing in Nuvie."""


class PairingExpired(NuvieError):
    """The pairing code expired before anyone approved it."""


@dataclass
class Pairing:
    device_code: str
    user_code: str
    url: str
    interval: float
    expires_in: float


@dataclass
class Paired:
    token: str
    project_id: str
    project_name: str


@dataclass
class Plan:
    """None data = not modified since `etag`."""

    data: bytes | None
    etag: str | None
    project_name: str | None


@dataclass
class Model:
    """status: "ok" (data + etag), "unchanged", "building" (retry_after), "none" (no model for this plan)."""

    status: str
    data: bytes | None = None
    etag: str | None = None
    retry_after: float = 0
    reason: str | None = None


class NuvieClient:
    def __init__(self, session: aiohttp.ClientSession, base_url: str, token: str | None = None, user_agent: str = "HomeAssistant-Nuvie") -> None:
        self._session = session
        self._base = base_url.rstrip("/") + API
        self._token = token
        self._ua = user_agent

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h = {"User-Agent": self._ua, **(extra or {})}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    async def _request(self, method: str, path: str, *, payload: Any = None, headers: dict[str, str] | None = None, timeout: aiohttp.ClientTimeout = TIMEOUT) -> tuple[aiohttp.ClientResponse, bytes]:
        try:
            async with self._session.request(method, self._base + path, json=payload, headers=self._headers(headers), timeout=timeout) as resp:
                body = await resp.read()
                return resp, body
        except (aiohttp.ClientError, TimeoutError) as err:
            raise NuvieError(f"Couldn't reach Nuvie: {err}") from err

    @staticmethod
    def _json(resp: aiohttp.ClientResponse, body: bytes) -> dict[str, Any]:
        try:
            data = json.loads(body or b"{}")
        except ValueError as err:
            raise NuvieError(f"Nuvie sent something unexpected ({resp.status})") from err
        if not isinstance(data, dict):
            raise NuvieError(f"Nuvie sent something unexpected ({resp.status})")
        return data

    def _fail(self, resp: aiohttp.ClientResponse, body: bytes) -> NuvieError:
        try:
            msg = str(self._json(resp, body).get("error") or f"Nuvie answered {resp.status}")
        except NuvieError:
            msg = f"Nuvie answered {resp.status}"
        if resp.status in (401, 403):
            return NuvieAuthError(msg)
        return NuvieError(msg)

    # ------------------------------------------------------------ pairing ----

    async def start_pairing(self, name: str, instance_id: str | None) -> Pairing:
        resp, body = await self._request("POST", "/pair", payload={"name": name, "instance_id": instance_id})
        if resp.status != 201:
            raise NuvieError(self._fail(resp, body).args[0])
        d = self._json(resp, body)
        try:
            return Pairing(str(d["device_code"]), str(d["user_code"]), str(d["verification_uri_complete"]), float(d.get("interval", 3)), float(d.get("expires_in", 600)))
        except (KeyError, TypeError, ValueError) as err:
            raise NuvieError("Nuvie sent an incomplete pairing") from err

    async def poll_pairing(self, device_code: str) -> Paired | None:
        """One poll: Paired once approved, None while pending."""
        resp, body = await self._request("POST", "/pair/poll", payload={"device_code": device_code})
        if resp.status == 429:
            return None
        if resp.status == 403:
            raise PairingDenied("The pairing was declined in Nuvie")
        if resp.status in (404, 410):
            raise PairingExpired("The pairing code expired")
        if resp.status != 200:
            raise NuvieError(self._fail(resp, body).args[0])
        d = self._json(resp, body)
        if d.get("status") != "approved":
            return None
        try:
            return Paired(str(d["token"]), str(d["project"]["id"]), str(d["project"].get("name") or "Nuvie plan"))
        except (KeyError, TypeError) as err:
            raise NuvieError("Nuvie sent an incomplete answer") from err

    # --------------------------------------------------------------- plan ----

    async def fetch_plan(self, etag: str | None = None) -> Plan:
        resp, body = await self._request("GET", "/v1/plan", headers={"If-None-Match": etag} if etag else None)
        name = resp.headers.get("x-nuvie-project")
        name = unquote(name) if name else None
        if resp.status == 304:
            return Plan(None, etag, name)
        if resp.status != 200:
            raise self._fail(resp, body)
        return Plan(body, resp.headers.get("ETag"), name)

    async def fetch_model(self, etag: str | None = None) -> Model:
        resp, body = await self._request("GET", "/v1/model", headers={"If-None-Match": etag} if etag else None, timeout=MODEL_TIMEOUT)
        if resp.status == 304:
            return Model("unchanged", etag=etag)
        if resp.status == 202:
            try:
                retry = float(resp.headers.get("Retry-After") or self._json(resp, body).get("retry_after") or 5)
            except (NuvieError, ValueError):
                retry = 5
            return Model("building", retry_after=min(max(retry, 2), 60))
        if resp.status == 200:
            return Model("ok", data=body, etag=resp.headers.get("ETag"))
        if resp.status == 401:
            raise self._fail(resp, body)
        if resp.status in (403, 404):
            d: dict[str, Any] = {}
            try:
                d = self._json(resp, body)
            except NuvieError:
                pass
            if d.get("code") in ("no_model", "plan"):
                return Model("none", reason=str(d.get("error") or ""))
            raise self._fail(resp, body)
        raise self._fail(resp, body)

    async def disconnect(self) -> None:
        resp, body = await self._request("DELETE", "/v1/connection")
        if resp.status not in (200, 401):
            raise self._fail(resp, body)
