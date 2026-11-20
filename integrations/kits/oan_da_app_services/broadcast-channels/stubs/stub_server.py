"""Stand-in for the two upstreams the add-on calls, for local and Dev integration.

    python3 stub_server.py --fixture fixture.example.json --port 18200

Serves, exactly as the kit's OpenAPI files describe:

* Beneficiary API (docs/beneficiary-api.openapi.yaml)
    GET  /api/v1/farmers?level=&code=&record_status=&page_size=&cursor=
    GET  /api/v1/farmers/{farmer_id}
* Bot Services broadcasts (docs/bot-services-broadcasts.openapi.yaml, spec §6.8.2)
    POST /broadcasts                         Idempotency-Key header
    GET  /broadcasts/{id}
    GET  /broadcasts/{id}/telegram-recipients?cursor=
* POST /token  a client-credentials token endpoint that accepts anything

The fixture lists farmers (with ``telegram: true`` when they are linked in the bot) and
agents (``da_id``, area codes, ``telegram``). A broadcast "finishes" ``--finish-after``
seconds after it is created; farmers with ``telegram_fails: true`` end as ``failed``.
Standard library only. Test data only: never point it at real phone numbers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

LEVELS = ("region", "zone", "woreda", "kebele")
MAX_PAGE = 1000


class State:
	def __init__(self, fixture: dict, finish_after: float):
		self.farmers: list[dict] = fixture.get("farmers", [])
		self.agents: list[dict] = fixture.get("agents", [])
		self.finish_after = finish_after
		self.broadcasts: dict[str, dict] = {}
		self.keys: dict[str, tuple[str, str]] = {}  # Idempotency-Key -> (body digest, id)
		self.lock = threading.Lock()


def _in_area(row: dict, level: str, code: str) -> bool:
	return row.get(f"{level}_code") == code


def _public(row: dict) -> dict:
	"""The beneficiary row shape (the stub's own flags are not part of it)."""
	return {k: v for k, v in row.items() if k not in ("telegram", "telegram_fails")}


class Handler(BaseHTTPRequestHandler):
	state: State

	def log_message(self, fmt, *args):  # keep the console quiet: no phone numbers in logs
		pass

	def _send(self, status: int, body: dict, content_type: str = "application/json") -> None:
		data = json.dumps(body).encode()
		self.send_response(status)
		self.send_header("Content-Type", content_type)
		self.send_header("Content-Length", str(len(data)))
		self.end_headers()
		self.wfile.write(data)

	def _problem(self, status: int, code: str, errors: list | None = None) -> None:
		body = {"type": "about:blank", "title": code.replace("_", " "), "status": status, "code": code}
		if errors:
			body["errors"] = errors
		self._send(status, body, "application/problem+json")

	# -- routing ----------------------------------------------------------------

	def do_GET(self):
		url = urlparse(self.path)
		q = {k: v[0] for k, v in parse_qs(url.query).items()}
		parts = [unquote(p) for p in url.path.strip("/").split("/")]
		if parts[:3] == ["api", "v1", "farmers"] and len(parts) == 3:
			return self._farmers(q)
		if parts[:3] == ["api", "v1", "farmers"] and len(parts) == 4:
			return self._farmer(parts[3])
		if parts[0] == "broadcasts" and len(parts) == 2:
			return self._broadcast(parts[1])
		if parts[0] == "broadcasts" and len(parts) == 3 and parts[2] == "telegram-recipients":
			return self._telegram_recipients(parts[1], q)
		self._problem(404, "not_found")

	def do_POST(self):
		url = urlparse(self.path)
		length = int(self.headers.get("Content-Length") or 0)
		raw = self.rfile.read(length) if length else b""
		if url.path == "/token":
			return self._send(
				200, {"access_token": uuid.uuid4().hex, "token_type": "Bearer", "expires_in": 300}
			)
		if url.path == "/broadcasts":
			return self._create(raw)
		self._problem(404, "not_found")

	# -- beneficiary API ----------------------------------------------------------

	def _farmers(self, q: dict) -> None:
		level, code = q.get("level"), q.get("code")
		errors = []
		if level not in LEVELS:
			errors.append({"field": "level", "code": "enum"})
		if not code:
			errors.append({"field": "code", "code": "required"})
		try:
			size = int(q.get("page_size") or 500)
			start = int(q.get("cursor") or 0)
		except ValueError:
			size, start = 0, 0
			errors.append({"field": "cursor", "code": "invalid_format"})
		if not 1 <= size <= MAX_PAGE:
			errors.append({"field": "page_size", "code": "out_of_range"})
		if errors:
			return self._problem(400, "validation_failed", errors)
		status = q.get("record_status", "ACTIVE")
		rows = [
			r
			for r in self.state.farmers
			if _in_area(r, level, code) and r.get("record_status", "ACTIVE") == status
		]
		chunk = rows[start : start + size]
		nxt = str(start + size) if start + size < len(rows) else None
		self._send(200, {"items": [_public(r) for r in chunk], "next_cursor": nxt})

	def _farmer(self, farmer_id: str) -> None:
		for r in self.state.farmers:
			if r["farmer_id"] == farmer_id:
				return self._send(200, _public(r))
		self._problem(404, "not_found")

	# -- Bot Services --------------------------------------------------------------

	def _create(self, raw: bytes) -> None:
		key = self.headers.get("Idempotency-Key")
		if not key:
			return self._problem(
				400, "validation_failed", [{"field": "header.Idempotency-Key", "code": "required"}]
			)
		try:
			body = json.loads(raw or b"{}")
		except ValueError:
			return self._problem(400, "invalid_json")
		missing = [f for f in ("recipients", "scope", "source", "title", "body") if f not in body]
		if missing or body.get("recipients") not in ("farmers", "das"):
			return self._problem(
				422, "validation_failed", [{"field": f, "code": "required"} for f in missing] or None
			)
		digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
		st = self.state
		with st.lock:
			if key in st.keys:
				known, bid = st.keys[key]
				if known != digest:
					return self._problem(409, "idempotency_conflict")
				return self._send(200, self._created(st.broadcasts[bid]))
			level, code = body["scope"].get("level"), body["scope"].get("code")
			if body["recipients"] == "farmers":
				pool = [f for f in st.farmers if _in_area(f, level, code) and f.get("telegram")]
				targets = [f["farmer_id"] for f in pool]
				failing = {f["farmer_id"] for f in pool if f.get("telegram_fails")}
				not_on = []
			else:
				pool = [
					a for a in st.agents if _in_area(a, level, code) and a["da_id"] != body.get("sender_ref")
				]
				targets = [a["da_id"] for a in pool if a.get("telegram")]
				failing = set()
				not_on = [a["da_id"] for a in pool if not a.get("telegram")]
			bid = "BC-" + uuid.uuid4().hex[:10]
			st.broadcasts[bid] = {
				"id": bid,
				"recipients_kind": body["recipients"],
				"targets": targets,
				"failing": failing,
				"not_on": not_on,
				"created": time.monotonic(),
			}
			st.keys[key] = (digest, bid)
			return self._send(202, self._created(st.broadcasts[bid]))

	@staticmethod
	def _created(b: dict) -> dict:
		return {"broadcast_id": b["id"], "recipients": len(b["targets"]), "not_on_telegram": b["not_on"]}

	def _finished(self, b: dict) -> bool:
		return time.monotonic() - b["created"] >= self.state.finish_after

	def _broadcast(self, bid: str) -> None:
		b = self.state.broadcasts.get(bid)
		if not b:
			return self._problem(404, "not_found")
		n = len(b["targets"])
		if self._finished(b):
			counts = {"accepted": 0, "sending": 0, "sent": n - len(b["failing"]), "failed": len(b["failing"])}
		else:
			counts = {"accepted": 0, "sending": n, "sent": 0, "failed": 0}
		counts |= {"expired": 0, "unknown": 0}
		state = "finished" if self._finished(b) else "running"
		self._send(200, {"broadcast_id": bid, "state": state, "counts": counts})

	def _telegram_recipients(self, bid: str, q: dict) -> None:
		b = self.state.broadcasts.get(bid)
		if not b or b["recipients_kind"] != "farmers":
			return self._problem(404, "not_found")
		sent = [f for f in b["targets"] if f not in b["failing"]] if self._finished(b) else []
		start = int(q.get("cursor") or 0)
		chunk = sent[start : start + 1000]
		nxt = str(start + 1000) if start + 1000 < len(sent) else None
		self._send(200, {"farmer_ids": chunk, "next_cursor": nxt})


def main() -> None:
	ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
	ap.add_argument("--fixture", required=True)
	ap.add_argument("--host", default="127.0.0.1")
	ap.add_argument("--port", type=int, default=18200)
	ap.add_argument("--finish-after", type=float, default=5.0, help="seconds until a broadcast finishes")
	args = ap.parse_args()
	with open(args.fixture, encoding="utf-8") as fh:
		Handler.state = State(json.load(fh), args.finish_after)
	server = ThreadingHTTPServer((args.host, args.port), Handler)
	print(f"stub listening on http://{args.host}:{args.port}", flush=True)
	server.serve_forever()


if __name__ == "__main__":
	main()
