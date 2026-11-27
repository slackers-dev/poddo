"""Container-Registry (z. B. ghcr.io): Zugang und Pruefung, ob ein Image schon existiert."""

from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .config import ConfigError, dotenv_value

KEYCHAIN_SERVICE = "poddo-registry"
ENV_TOKEN = "PODDO_REGISTRY_TOKEN"
MANIFEST_TYPES = ", ".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


@dataclass
class ImageRef:
    host: str
    repo: str
    tag: str

    @classmethod
    def parse(cls, ref: str) -> ImageRef:
        name, _, tag = ref.rpartition(":") if ":" in ref.rsplit("/", 1)[-1] else (ref, "", "latest")
        host, _, repo = name.partition("/")
        if "." not in host and ":" not in host and host != "localhost":
            host, repo = "registry-1.docker.io", name if "/" in name else f"library/{name}"
        return cls(host, repo, tag or "latest")

    def __str__(self) -> str:
        return f"{self.host}/{self.repo}:{self.tag}"


def token(host: str) -> str:
    """Zugang zur Registry: PODDO_REGISTRY_TOKEN (Umgebung oder .env), sonst macOS-Schluesselbund."""
    for name in (ENV_TOKEN, "POD_DO_REGISTRY_TOKEN"):  # zweiter: alter Name vor der Umbenennung
        value = os.environ.get(name, "").strip() or dotenv_value(name)
        if value:
            return value
    if sys.platform == "darwin" and shutil.which("security"):
        for service in (KEYCHAIN_SERVICE, "pod-do-registry"):
            r = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-a", host, "-w"],
                capture_output=True,
                text=True,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
    raise ConfigError(f"Kein Zugang fuer {host}. Einrichten mit: poddo registry login {host} --user <name>")


def store_token_interactive(host: str) -> str:
    if sys.platform == "darwin" and shutil.which("security"):
        print(f"Token fuer {host} eingeben (zweimal, wird nicht angezeigt):")
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", host, "-w"], check=True
        )
        return f"macOS-Schluesselbund (Eintrag {KEYCHAIN_SERVICE}, Konto {host})"
    raise ConfigError(f"Ohne macOS-Schluesselbund: {ENV_TOKEN} in der Umgebung oder in .env setzen")


def _get(url: str, headers: dict[str, str], method: str = "GET") -> tuple[int, bytes, dict[str, str]]:
    req = urllib.request.Request(url, headers={"User-Agent": "poddo/0.1", **headers}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def _bearer(challenge: str, user: str, secret: str, scope: str) -> str | None:
    """Token-Verfahren der Registry (WWW-Authenticate: Bearer realm=...,service=...)."""
    if not challenge.lower().startswith("bearer "):
        return None
    params = dict(p.split("=", 1) for p in challenge[7:].split(",") if "=" in p)
    params = {k.strip(): v.strip().strip('"') for k, v in params.items()}
    query = {"service": params.get("service", ""), "scope": scope}
    basic = base64.b64encode(f"{user}:{secret}".encode()).decode()
    status, body, _ = _get(
        params["realm"] + "?" + urllib.parse.urlencode(query), {"Authorization": f"Basic {basic}"}
    )
    if status != 200:
        raise ConfigError(f"Registry lehnt den Zugang ab ({status}). Token und Benutzer pruefen.")
    data = json.loads(body)
    return data.get("token") or data.get("access_token")


def image_exists(ref: ImageRef, user: str, secret: str) -> bool:
    url = f"https://{ref.host}/v2/{ref.repo}/manifests/{ref.tag}"
    headers = {"Accept": MANIFEST_TYPES}
    status, _, h = _get(url, headers, "HEAD")
    if status == 401:
        challenge = h.get("WWW-Authenticate") or h.get("Www-Authenticate") or ""
        tok = _bearer(challenge, user, secret, f"repository:{ref.repo}:pull")
        if tok:
            status, _, _ = _get(url, {**headers, "Authorization": f"Bearer {tok}"}, "HEAD")
    if status == 401:
        raise ConfigError(f"Kein Lesezugriff auf {ref} ({status})")
    # 404, bei ghcr.io auch 403 fuer ein Paket, das es noch nicht gibt
    return status == 200
