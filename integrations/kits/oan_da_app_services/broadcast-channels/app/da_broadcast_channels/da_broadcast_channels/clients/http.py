"""Shared HTTP plumbing for the two upstreams: a Keycloak client-credentials token and
JSON calls with timeouts, an optional CA bundle and value-free errors.

Each upstream is configured under its own key in site_config (``beneficiary_api`` and
``bot_services`` inside ``da_broadcast_channels``), falling back to DA Broadcast Channel
Settings. Secrets live in site_config or Password fields, never in code or logs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import frappe
import requests

SETTINGS = "DA Broadcast Channel Settings"
_TOKEN_SKEW_SECONDS = 30


class UpstreamError(Exception):
	"""An upstream call failed. ``code`` is safe to log and show; it never holds data."""

	def __init__(self, upstream: str, code: str, status: int | None = None):
		super().__init__(f"{upstream}: {code}" + (f" (HTTP {status})" if status else ""))
		self.upstream, self.code, self.status = upstream, code, status


@dataclass(frozen=True)
class Upstream:
	name: str
	base_url: str
	token_url: str | None
	client_id: str | None
	client_secret: str | None
	ca_bundle: str | None
	timeout: float

	@property
	def verify(self) -> bool | str:
		return self.ca_bundle or True


def upstream(name: str) -> Upstream:
	"""Configuration for ``beneficiary_api`` or ``bot_services``."""
	conf = (frappe.conf.get("da_broadcast_channels") or {}).get(name) or {}
	settings = frappe.get_cached_doc(SETTINGS)
	prefix = "beneficiary" if name == "beneficiary_api" else "bot"

	def pick(key: str, secret: bool = False):
		if conf.get(key):
			return conf[key]
		field = f"{prefix}_{key}"
		if secret:
			return settings.get_password(field, raise_exception=False) if settings.get(field) else None
		return settings.get(field)

	base_url = (pick("base_url") or "").rstrip("/")
	if not base_url:
		raise UpstreamError(name, "not_configured")
	return Upstream(
		name=name,
		base_url=base_url,
		token_url=pick("token_url"),
		client_id=pick("client_id"),
		client_secret=pick("client_secret", secret=True),
		ca_bundle=pick("ca_bundle"),
		timeout=float(conf.get("timeout") or settings.get("http_timeout_seconds") or 20),
	)


def _token(up: Upstream) -> str | None:
	"""A cached client-credentials access token, or None when the upstream needs none."""
	if not up.token_url:
		return None
	key = f"da_broadcast_channels:token:{up.name}"
	cached = frappe.cache.get_value(key)
	if cached and cached.get("expires_at", 0) > time.time():
		return cached["access_token"]
	try:
		res = requests.post(
			up.token_url,
			data={"grant_type": "client_credentials"},
			auth=(up.client_id or "", up.client_secret or ""),
			timeout=up.timeout,
			verify=up.verify,
		)
	except requests.RequestException:
		raise UpstreamError(up.name, "token_unreachable")
	if res.status_code != 200:
		raise UpstreamError(up.name, "token_refused", res.status_code)
	body = res.json()
	ttl = max(int(body.get("expires_in") or 60) - _TOKEN_SKEW_SECONDS, 10)
	frappe.cache.set_value(
		key, {"access_token": body["access_token"], "expires_at": time.time() + ttl}, expires_in_sec=ttl
	)
	return body["access_token"]


def call(
	up: Upstream,
	method: str,
	path: str,
	*,
	params: dict | None = None,
	json: dict | None = None,
	headers: dict | None = None,
	ok: tuple[int, ...] = (200,),
) -> tuple[int, Any]:
	"""One JSON call; returns (status, body). Raises UpstreamError with a value-free code."""
	all_headers = {"Accept": "application/json", **(headers or {})}
	token = _token(up)
	if token:
		all_headers["Authorization"] = f"Bearer {token}"
	try:
		res = requests.request(
			method,
			up.base_url + path,
			params=params,
			json=json,
			headers=all_headers,
			timeout=up.timeout,
			verify=up.verify,
		)
	except requests.Timeout:
		raise UpstreamError(up.name, "timeout")
	except requests.RequestException:
		raise UpstreamError(up.name, "unreachable")
	if res.status_code not in ok:
		code = "http_error"
		try:  # problem+json code only: never the detail, which may echo values
			code = str(res.json().get("code") or code)[:64]
		except ValueError:
			pass
		raise UpstreamError(up.name, code, res.status_code)
	try:
		return res.status_code, res.json()
	except ValueError:
		raise UpstreamError(up.name, "invalid_json", res.status_code)
