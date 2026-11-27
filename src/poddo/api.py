"""Kleiner Client fuer die RunPod REST API v2 (https://api.runpod.io/v2), nur Standardbibliothek."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

BASE_URL = "https://api.runpod.io"
USER_AGENT = "poddo/0.1"


class ApiError(Exception):
    def __init__(self, status: int, title: str, detail: str, errors: list[str] | None = None):
        self.status = status
        self.title = title
        self.detail = detail
        self.errors = errors or []
        msg = f"RunPod API {status} {title}: {detail}"
        if self.errors:
            msg += " (" + "; ".join(self.errors) + ")"
        super().__init__(msg)


# Transport: (methode, url, header, body) -> (status, body). Austauschbar fuer Tests.
Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    """Status 0 = keine Antwort (Netz weg, Zeitueberschreitung)."""
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
        reason = getattr(e, "reason", e)
        return 0, json.dumps({"title": "Netzwerk", "detail": str(reason)}).encode()


@dataclass
class Ssh:
    host: str
    port: int
    user: str
    pod_id: str = ""  # fuer den Host-Schluessel: gemerkt pro Pod, nicht pro host:port


class RunPod:
    def __init__(self, api_key: str, transport: Transport = urllib_transport, base_url: str = BASE_URL):
        self._key = api_key
        self._transport = transport
        self._base = base_url.rstrip("/")

    # -- Grundlage --------------------------------------------------------------------------------

    def request(self, method: str, path: str, body: Any = None, query: dict[str, Any] | None = None) -> Any:
        url = self._base + path
        if query:
            q = {
                k: (",".join(v) if isinstance(v, list | tuple) else v)
                for k, v in query.items()
                if v is not None
            }
            url += "?" + urllib.parse.urlencode(q)
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        # GET und DELETE darf man wiederholen. POST nicht bei 5xx oder ohne Antwort: Vielleicht hat RunPod
        # den Pod schon angelegt, ein zweiter Versuch saehe dann einen zweiten Pod (ohne Waechter).
        # 429 heisst "nicht bearbeitet", das geht immer nochmals.
        idempotent = method in ("GET", "HEAD", "DELETE")
        for attempt in range(5):
            status, raw = self._transport(method, url, headers, data)
            transient = status == 429 or (idempotent and (status == 0 or status >= 500))
            if transient and attempt < 4:
                time.sleep(2 * (attempt + 1))
                continue
            break
        payload: Any = None
        if raw:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"detail": raw.decode(errors="replace")[:500]}
        if status >= 400 or status == 0:
            p = payload if isinstance(payload, dict) else {}
            raise ApiError(status, p.get("title", ""), p.get("detail", ""), p.get("errors"))
        return payload

    def _list(self, path: str, key: str, query: dict[str, Any] | None = None) -> list[dict]:
        items: list[dict] = []
        q = dict(query or {})
        while True:
            page = self.request("GET", path, query=q)
            items += page.get(key, [])
            pag = page.get("pagination") or {}
            if not pag.get("hasNextPage"):
                return items
            q["cursor"] = pag["nextCursor"]

    # -- Pods -------------------------------------------------------------------------------------

    def list_pods(self) -> list[dict]:
        return self._list("/v2/pods", "pods")

    def get_pod(self, pod_id: str) -> dict:
        return self.request("GET", f"/v2/pods/{pod_id}")

    def create_pod(self, body: dict) -> dict:
        return self.request("POST", "/v2/pods", body)

    def terminate_pod(self, pod_id: str) -> None:
        self.request("DELETE", f"/v2/pods/{pod_id}")

    def exec(self, pod_id: str, command: list[str], timeout: int = 20) -> dict:
        return self.request(
            "POST", f"/v2/pods/{pod_id}/exec", {"command": command, "timeoutSeconds": timeout}
        )

    # -- Katalog, Volumes, Konto --------------------------------------------------------------------

    def gpus(self, cloud: str | None = None) -> list[dict]:
        q: dict[str, Any] = {"include": "AVAILABILITY", "product": "POD"}
        if cloud in ("SECURE", "COMMUNITY"):
            q["cloud"] = cloud
        return self.request("GET", "/v2/catalog/gpus", query=q).get("gpus", [])

    def global_volumes(self) -> list[dict]:
        return self._list("/v2/global-volumes", "globalVolumes")

    def create_global_volume(self, name: str) -> dict:
        return self.request("POST", "/v2/global-volumes", {"name": name})

    def registries(self) -> list[dict]:
        return self._list("/v2/registries", "registries")

    def create_registry(self, name: str, username: str, password: str) -> dict:
        return self.request(
            "POST", "/v2/registries", {"name": name, "username": username, "password": password}
        )

    def balance(self) -> dict:
        return self.request("GET", "/v2/billing/balance")


def ssh_of(pod: dict) -> Ssh | None:
    """Direkte SSH-Verbindung (mit scp/rsync/tar), None solange der Pod sie noch nicht hat."""
    d = (pod.get("ssh") or {}).get("direct")
    if not d:
        return None
    user = d.get("username") or "root"
    return Ssh(host=d["host"], port=int(d["port"]), user=user, pod_id=pod.get("id", ""))
