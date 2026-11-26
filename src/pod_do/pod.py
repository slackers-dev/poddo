"""Pods finden, anlegen, bereit machen und loeschen."""

from __future__ import annotations

import hashlib
import re
import shlex
import sys
import time
from dataclasses import dataclass

from . import pod_scripts as S
from .api import ApiError, RunPod, ssh_of
from .config import ConfigError, EnvSpec, GlobalConfig, Project
from .remote import Remote

VOLUME_PATH = "/vol"
ALIVE = ("PROVISIONING", "STARTING", "RUNNING")


def say(msg: str) -> None:
    print(f"[pod-do] {msg}", file=sys.stderr, flush=True)


def pod_name(project: str, env: str) -> str:
    return re.sub(r"[^a-zA-Z0-9-]+", "-", f"pod-do-{project}-{env}").strip("-").lower()


def env_hash(env: EnvSpec) -> str:
    # Version 1 im Hash: aendert sich das Archivformat, wird neu eingerichtet
    h = hashlib.sha256(f"v1\n{env.image}\n{env.setup}".encode())
    return h.hexdigest()[:12]


@dataclass
class Candidate:
    gpu: str
    name: str
    vram: int
    price: float
    availability: str


def gpu_candidates(api: RunPod, wanted: list[str], min_vram: int, cloud: str) -> list[Candidate]:
    """GPUs in der gewuenschten Reihenfolge, ohne ausverkaufte und ohne zu kleine."""
    catalog = {g["id"]: g for g in api.gpus(cloud)}
    out = []
    for gid in wanted:
        g = catalog.get(gid) or next((c for c in catalog.values() if c.get("name") == gid), None)
        if not g:
            say(f"GPU {gid!r} kennt RunPod nicht (pod-do gpus zeigt die Namen)")
            continue
        if g.get("memory", 0) < min_vram:
            continue
        avail = g.get("availability") or "NONE"
        if avail == "NONE":
            continue
        price = (g.get("price") or {}).get("community" if cloud == "COMMUNITY" else "secure") or 0
        out.append(Candidate(g["id"], g.get("name", g["id"]), g.get("memory", 0), price, avail))
    return out


class Pods:
    def __init__(self, api: RunPod, cfg: GlobalConfig, project: Project):
        self.api = api
        self.cfg = cfg
        self.project = project

    # -- Suchen -----------------------------------------------------------------------------------

    def find(self, env: str) -> dict | None:
        name = pod_name(self.project.name, env)
        for p in self.api.list_pods():
            if p.get("name") == name and p.get("status") in ALIVE:
                return p
        return None

    def mine(self) -> list[dict]:
        prefix = pod_name(self.project.name, "")
        return [p for p in self.api.list_pods() if p.get("name", "").startswith(prefix)]

    # -- Volume -----------------------------------------------------------------------------------

    def volume_id(self, create: bool = False) -> str | None:
        if not self.cfg.volume:
            return None
        for v in self.api.global_volumes():
            if v.get("name") == self.cfg.volume:
                return v["id"]
        if not create:
            return None
        v = self.api.create_global_volume(self.cfg.volume)
        say(f"Global Volume {self.cfg.volume!r} angelegt ({v['id']})")
        return v["id"]

    # -- Anlegen ----------------------------------------------------------------------------------

    def create(self, env: EnvSpec, gpu_override: list[str] | None = None) -> dict:
        cloud = env.cloud or self.cfg.cloud
        wanted = gpu_override or env.gpus or self.cfg.gpus
        cands = gpu_candidates(self.api, wanted, env.min_vram, cloud)
        if not cands:
            raise ConfigError(
                f"Keine der GPUs ist gerade frei ({', '.join(wanted)}). "
                "pod-do gpus zeigt, was es gibt; mit --gpu eine andere waehlen."
            )
        key = self.cfg.ssh_key
        if not key:
            raise ConfigError(
                "Kein SSH-Schluessel gefunden. ssh-keygen -t ed25519, oder ssh_key in der Konfig."
            )
        pub = key.with_name(key.name + ".pub").read_text().strip()
        vol = self.volume_id()
        if self.cfg.volume and not vol:
            say(
                f"Hinweis: Global Volume {self.cfg.volume!r} fehlt, setup laeuft dann jedes Mal. "
                "Anlegen mit: pod-do volume --create"
            )
        body: dict = {
            "name": pod_name(self.project.name, env.name),
            "image": env.image,
            "cloud": cloud,
            "disk": env.disk,
            "ports": ["22/tcp"],
            "startSsh": True,
            "env": {"PUBLIC_KEY": pub, **env.vars},
        }
        if vol:
            body["mounts"] = {"global": [{"volumeId": vol, "path": VOLUME_PATH}]}
        last: Exception | None = None
        for c in cands:
            gpu: dict = {"id": c.gpu, "count": 1}
            if env.cuda:
                gpu["minCudaVersion"] = env.cuda
            say(f"versuche {c.name} ({c.vram} GB, ${c.price:.2f}/h, Verfuegbarkeit {c.availability})")
            try:
                pod = self.api.create_pod({**body, "gpu": gpu})
                say(f"Pod {pod['id']} angelegt: {c.name}, ${pod.get('cost', c.price):.2f}/h")
                return pod
            except ApiError as e:
                last = e
                if e.status in (400, 403, 404):
                    say(f"  geht nicht: {e.detail or e.title}")
                    continue
                raise
        raise ConfigError(f"Keine GPU bekommen. Letzter Fehler: {last}")

    # -- Bereit machen ----------------------------------------------------------------------------

    def wait_ssh(self, pod: dict, timeout: float = 900) -> Remote:
        t0 = time.time()
        last = None
        while True:
            pod = self.api.get_pod(pod["id"])
            st = pod.get("status")
            if st != last:
                say(f"Status: {st}")
                last = st
            if st in ("EXITED", "ERROR", "TERMINATED"):
                raise ConfigError(f"Pod {pod['id']} ist {st}. Logs in der RunPod-Konsole.")
            ssh = ssh_of(pod)
            if st == "RUNNING" and ssh:
                r = Remote(ssh, self.cfg.ssh_key)
                if r.ok():
                    say(f"SSH bereit: {ssh.user}@{ssh.host}:{ssh.port}")
                    return r
            if time.time() - t0 > timeout:
                raise ConfigError(
                    f"Pod {pod['id']} nach {int(timeout / 60)} min noch nicht per SSH erreichbar"
                )
            time.sleep(5)

    def remote(self, pod: dict) -> Remote:
        ssh = ssh_of(pod) or ssh_of(self.api.get_pod(pod["id"]))
        if not ssh:
            raise ConfigError(f"Pod {pod['id']} hat (noch) keine SSH-Verbindung")
        return Remote(ssh, self.cfg.ssh_key)

    def prepare(self, r: Remote, env: EnvSpec) -> None:
        """Skripte hinkopieren, Waechter starten, Umgebung herstellen (Archiv oder setup)."""
        vol = VOLUME_PATH if self.cfg.volume else ""
        exports = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in env.vars.items())
        files = {
            f"{S.ROOT}/env.sh": S.ENV_SH.replace("@ENV@", env.name)
            .replace("@VOLUME@", vol)
            .replace("@VARS@", exports),
            f"{S.ROOT}/bootstrap.sh": S.BOOTSTRAP_SH.replace("@ENV@", env.name).replace(
                "@HASH@", env_hash(env)
            ),
            f"{S.ROOT}/start.sh": S.START_SH,
            f"{S.ROOT}/follow.sh": S.FOLLOW_SH,
            f"{S.ROOT}/watchdog.sh": S.WATCHDOG_SH,
            f"{S.ROOT}/setup.sh": env.setup,
            f"{S.ROOT}/boot.sh": env.boot,
        }
        # alles in einem Rutsch: ein tar ueber eine Verbindung waere schneller, so ist es einfacher
        for path, text in files.items():
            r.put_text(text, path, "755")
        idle = env.idle_minutes if env.idle_minutes is not None else self.cfg.idle_minutes
        max_h = env.max_hours if env.max_hours is not None else self.cfg.max_hours
        r.run(S.START_WATCHDOG.replace("@IDLE@", str(int(idle))).replace("@MAX@", str(max_h)))
        say(f"Waechter laeuft: loescht den Pod nach {idle} min Leerlauf, spaetestens nach {max_h} h")
        code = self.run_job(r, "_bootstrap", f"bash {S.ROOT}/bootstrap.sh")
        if code != 0:
            raise ConfigError(
                f"Einrichten fehlgeschlagen (Code {code}). Log: pod-do ssh, dann "
                f"cat {S.ROOT}/jobs/_bootstrap/log"
            )

    def run_job(self, r: Remote, job: str, script: str, header: str = "") -> int:
        """Startet `script` losgeloest vom SSH und zeigt das Log. Gibt den Exit-Code zurueck."""
        d = f"{S.ROOT}/jobs/{job}"
        r.put_text(
            f"set -eo pipefail\nsource {S.ROOT}/env.sh\n{header}\ncd {shlex.quote(d)}\n{script}\n",
            f"{d}/run.sh",
        )
        r.run(f"bash {S.ROOT}/start.sh {shlex.quote(d)}")
        return self.follow(r, job)

    def follow(self, r: Remote, job: str) -> int:
        d = f"{S.ROOT}/jobs/{job}"
        try:
            return r.run(f"bash {S.ROOT}/follow.sh {shlex.quote(d)}", check=False).returncode
        except KeyboardInterrupt:
            say(f"Abgekoppelt. Der Job {job} laeuft auf dem Pod weiter: pod-do attach {job}")
            raise

    # -- Alles zusammen ---------------------------------------------------------------------------

    def ensure(self, env_name: str, gpu_override: list[str] | None = None) -> tuple[dict, Remote]:
        if env_name not in self.project.envs:
            raise ConfigError(f"Umgebung {env_name!r} gibt es nicht in pod-do.toml")
        env = self.project.envs[env_name]
        pod = self.find(env_name)
        if pod:
            say(f"nehme laufenden Pod {pod['id']} ({pod.get('gpu', {}).get('id', '?')})")
        else:
            pod = self.create(env, gpu_override)
        r = self.wait_ssh(pod)
        self.prepare(r, env)
        return pod, r

    def down(self, pod: dict) -> None:
        self.api.terminate_pod(pod["id"])
        say(f"Pod {pod['id']} ({pod.get('name')}) geloescht")
