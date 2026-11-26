"""Pods finden, anlegen, bereit machen und loeschen."""

from __future__ import annotations

import hashlib
import re
import shlex
import sys
import time
from dataclasses import dataclass

from . import pod_scripts as S
from . import registry
from .api import ApiError, RunPod, ssh_of
from .config import ConfigError, EnvSpec, GlobalConfig, Project
from .remote import Remote, RemoteError

VOLUME_PATH = "/vol"
RESULTS_PATH = f"{VOLUME_PATH}/pod-do/results"
ALIVE = ("PROVISIONING", "STARTING", "RUNNING")


def say(msg: str) -> None:
    print(f"[pod-do] {msg}", file=sys.stderr, flush=True)


def pod_name(project: str, env: str) -> str:
    return re.sub(r"[^a-zA-Z0-9-]+", "-", f"pod-do-{project}-{env}").strip("-").lower()


def results_archive(project: str, job: str) -> str:
    """Wo die Ergebnisse eines Jobs auf dem Volume liegen."""
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", project).strip("-") or "projekt"
    return f"{RESULTS_PATH}/{safe}/{job}.tar.zst"


def image_ref(env: EnvSpec) -> str | None:
    """Fertige Umgebung als Image: <docker>:<setup-hash>, oder None ohne env.docker."""
    return f"{env.docker}:{env_hash(env)}" if env.docker else None


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

    def create(
        self,
        env: EnvSpec,
        gpu_override: list[str] | None = None,
        wait_minutes: float = 0,
        image: str | None = None,
        registry_id: str | None = None,
    ) -> dict:
        """Erste freie GPU aus der Liste. Ist keine frei, alle 30 s nochmals, bis `wait_minutes` um sind.
        image: fertiges Image statt env.image (env.docker), registry_id: Zugangsdaten bei RunPod dafuer."""
        cloud = env.cloud or self.cfg.cloud
        wanted = gpu_override or env.gpus or self.cfg.gpus
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
            "image": image or env.image,
            "cloud": cloud,
            "disk": env.disk,
            "ports": ["22/tcp"],
            "startSsh": True,
            "env": {"PUBLIC_KEY": pub, **env.vars},
        }
        if vol:
            body["mounts"] = {"global": [{"volumeId": vol, "path": VOLUME_PATH}]}
        if registry_id:
            body["registry"] = registry_id
        deadline = time.time() + wait_minutes * 60
        while True:
            pod = self._try_create(body, env, wanted, cloud)
            if pod:
                return pod
            if time.time() >= deadline:
                hint = "" if wait_minutes else " Mit --wait <minuten> wartet pod-do auf eine freie Karte."
                raise ConfigError(
                    f"Keine der GPUs ist gerade frei ({', '.join(wanted)}).{hint} "
                    "pod-do gpus zeigt, was es gibt; --gpu waehlt andere."
                )
            left = int((deadline - time.time()) / 60)
            say(f"keine GPU frei, versuche in 30 s nochmals (noch etwa {left} min)")
            time.sleep(30)

    def _try_create(self, body: dict, env: EnvSpec, wanted: list[str], cloud: str) -> dict | None:
        for c in gpu_candidates(self.api, wanted, env.min_vram, cloud):
            gpu: dict = {"id": c.gpu, "count": 1}
            if env.cuda:
                gpu["minCudaVersion"] = env.cuda
            say(f"versuche {c.name} ({c.vram} GB, ${c.price:.2f}/h, Verfuegbarkeit {c.availability})")
            try:
                pod = self.api.create_pod({**body, "gpu": gpu})
                say(f"Pod {pod['id']} angelegt: {c.name}, ${pod.get('cost', c.price):.2f}/h")
                return pod
            except ApiError as e:
                # 400: Karte gerade weg, 403/404: kein Zugriff. Naechste versuchen. 402 (Guthaben) nicht.
                if e.status in (400, 403, 404):
                    say(f"  geht nicht: {e.detail or e.title}")
                    continue
                raise
        return None

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

    def install(self, r: Remote, env: EnvSpec) -> None:
        """Skripte hinkopieren und den Waechter starten, alles in einem SSH-Aufruf."""
        vol = VOLUME_PATH if self.cfg.volume else ""
        exports = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in env.vars.items())
        texts = {
            f"{S.ROOT}/env.sh": S.ENV_SH.replace("@ENV@", env.name)
            .replace("@VOLUME@", vol)
            .replace("@VARS@", exports),
            f"{S.ROOT}/bootstrap.sh": S.BOOTSTRAP_SH.replace("@ENV@", env.name)
            .replace("@HASH@", env_hash(env))
            .replace("@SNAPSHOT@", "0" if image_ref(env) and self.cfg.registry_user else "1"),
            f"{S.ROOT}/start.sh": S.START_SH,
            f"{S.ROOT}/follow.sh": S.FOLLOW_SH,
            f"{S.ROOT}/watchdog.sh": S.WATCHDOG_SH,
            f"{S.ROOT}/status.sh": S.STATUS_SH,
            f"{S.ROOT}/save.sh": S.SAVE_SH,
            f"{S.ROOT}/bin/pod-do-progress": S.PROGRESS_SH,
            f"{S.ROOT}/setup.sh": env.setup,
            f"{S.ROOT}/boot.sh": env.boot,
        }
        idle = env.idle_minutes if env.idle_minutes is not None else self.cfg.idle_minutes
        max_h = env.max_hours if env.max_hours is not None else self.cfg.max_hours
        keep = max(int(self.cfg.keep_results_minutes), int(idle))
        watchdog = (
            S.START_WATCHDOG.replace("@IDLE@", str(int(idle)))
            .replace("@MAX@", str(max_h))
            .replace("@KEEP@", str(keep))
        )
        r.put_files({path: (text, 0o755) for path, text in texts.items()}, then=watchdog)
        say(
            f"Waechter laeuft: loescht den Pod nach {idle} min Leerlauf ({keep} min, solange Ergebnisse "
            f"nicht abgeholt sind), spaetestens nach {max_h} h"
        )

    def start_job(
        self,
        r: Remote,
        job: str,
        script: str,
        header: str = "",
        save: list[str] | None = None,
    ) -> None:
        """Startet `script` losgeloest vom SSH.
        save: Pfade im Job-Ordner, die am Ende aufs Volume gesichert werden (nur mit Volume)."""
        d = f"{S.ROOT}/jobs/{job}"
        body = f"set -eo pipefail\nsource {S.ROOT}/env.sh\n{header}\ncd {shlex.quote(d)}\n{script}\n"
        files = {f"{d}/run.sh": (body, 0o644)}
        if save is not None and self.cfg.volume:
            dest = results_archive(self.project.name, job)
            days = int(self.cfg.keep_results_days)
            files[f"{d}/save.conf"] = (f"DEST={shlex.quote(dest)}\nDAYS={days}\n", 0o644)
            files[f"{d}/save.list"] = ("".join(f"{p}\n" for p in save), 0o644)
        r.put_files(files, then=f"bash {S.ROOT}/start.sh {shlex.quote(d)}")

    def run_job(self, r: Remote, job: str, script: str, header: str = "") -> int:
        """Startet `script` losgeloest vom SSH und zeigt das Log. Gibt den Exit-Code zurueck."""
        self.start_job(r, job, script, header)
        return self.follow(r, job)

    def follow(self, r: Remote, job: str) -> int:
        d = f"{S.ROOT}/jobs/{job}"
        try:
            code = r.run(f"bash {S.ROOT}/follow.sh {shlex.quote(d)}", check=False).returncode
        except KeyboardInterrupt:
            say(f"Abgekoppelt. Der Job {job} laeuft auf dem Pod weiter: pod-do attach {job}")
            raise
        if code == 255:
            # 255 kommt von ssh selbst (Verbindung weg, Laptop schlief), ausser der Job endete wirklich so
            try:
                real = r.run(f"cat {shlex.quote(d)}/exitcode", capture=True).stdout.strip()
            except Exception:
                real = ""
            if real != "255":
                raise RemoteError(
                    f"Verbindung zum Pod verloren. Der Job {job} laeuft weiter: pod-do attach {job}"
                )
        return code

    # -- Alles zusammen ---------------------------------------------------------------------------

    def ensure(
        self,
        env_name: str,
        gpu_override: list[str] | None = None,
        wait_minutes: float | None = None,
        push: bool = True,
    ) -> tuple[dict, Remote]:
        if env_name not in self.project.envs:
            raise ConfigError(f"Umgebung {env_name!r} gibt es nicht in pod-do.toml")
        env = self.project.envs[env_name]
        target = image_ref(env)
        if target:
            try:
                exists = self.image_exists(target)
            except ConfigError as e:
                say(f"Docker-Image uebersprungen: {e}")
                target, exists = None, False
        pod = self.find(env_name)
        created = False
        if pod:
            say(f"nehme laufenden Pod {pod['id']} ({pod.get('gpu', {}).get('id', '?')})")
        else:
            created = True
            wait = self.cfg.wait_minutes if wait_minutes is None else wait_minutes
            image, cred = None, None
            if target and exists:
                say(f"fertiges Image: {target}")
                image, cred = target, self.runpod_registry_id()
            elif target:
                say(f"Image {target} gibt es noch nicht: einrichten wie gewohnt, danach hochladen")
            pod = self.create(env, gpu_override, wait, image=image, registry_id=cred)
        try:
            r = self.wait_ssh(pod)
            self.install(r, env)
        except BaseException:
            # Ohne Waechter loescht sich ein Pod nie selbst. Haben wir ihn eben angelegt und kommen nicht
            # bis zum Waechter (kein SSH, Ctrl-C, Fehler), sofort wieder loeschen statt ihn laufen zu lassen.
            if created:
                say(f"Pod {pod['id']} wird wieder geloescht (kam nicht bis zum Waechter)")
                try:
                    self.api.terminate_pod(pod["id"])
                except Exception as e:
                    say(f"Loeschen ging nicht ({e}). Bitte von Hand: pod-do down")
            raise
        code = self.run_job(r, "_bootstrap", f"bash {S.ROOT}/bootstrap.sh")
        if code != 0:
            raise ConfigError(
                f"Einrichten fehlgeschlagen (Code {code}). Log: pod-do ssh, dann "
                f"cat {S.ROOT}/jobs/_bootstrap/log"
            )
        if target and push and not self.has_image_marker(r, env, target):
            self.push_image(r, env, target)
        return pod, r

    # -- Docker-Image der Umgebung ------------------------------------------------------------------

    def image_exists(self, target: str) -> bool:
        ref = registry.ImageRef.parse(target)
        if not self.cfg.registry_user:
            raise ConfigError(
                "registry_user fehlt in ~/.config/pod-do/config.toml (z. B. GitHub-Name bei ghcr.io)"
            )
        return registry.image_exists(ref, self.cfg.registry_user, registry.token(ref.host))

    def runpod_registry_id(self) -> str | None:
        """Zugangsdaten bei RunPod, mit denen der Pod das private Image zieht (pod-do registry runpod)."""
        try:
            regs = self.api.registries()
        except ApiError as e:
            if e.status == 403:
                say(
                    "Hinweis: API-Schluessel darf Registry-Zugangsdaten nicht lesen. Fuer private Images "
                    "das Recht 'Secrets' geben oder runpod_registry auf die ID setzen."
                )
                return self.cfg.runpod_registry or None
            raise
        for r in regs:
            if self.cfg.runpod_registry in (r.get("name"), r.get("id")):
                return r["id"]
        say(
            f"Hinweis: keine Registry-Zugangsdaten {self.cfg.runpod_registry!r} bei RunPod. Ein privates "
            "Image laesst sich dann nicht ziehen. Anlegen mit: pod-do registry runpod"
        )
        return None

    def has_image_marker(self, r: Remote, env: EnvSpec, target: str) -> bool:
        marker = f"{S.ROOT}/envs/{env.name}/.pod-do-image"
        out = r.run(f"cat {marker} 2>/dev/null || true", capture=True).stdout.strip()
        return out == target

    def push_image(self, r: Remote, env: EnvSpec, target: str) -> None:
        """Umgebung als Layer ans Basis-Image haengen und hochladen (crane, ohne Docker-Daemon)."""
        ref = registry.ImageRef.parse(target)
        secret = registry.token(ref.host)
        say("Werkzeuge fuer das Image (crane, pigz)")
        r.run(S.IMAGE_TOOLS_SH)
        # Token nur ueber stdin, landet nicht in der Umgebung des Pods
        user = shlex.quote(self.cfg.registry_user)
        login = f"crane auth login {shlex.quote(ref.host)} -u {user} --password-stdin"
        r.run(f"source {S.ROOT}/env.sh && {login}", stdin_text=secret)
        try:
            self._push(r, env, target)
        finally:
            # Zugang nicht auf dem Pod liegen lassen (~/.docker/config.json)
            r.run(f"source {S.ROOT}/env.sh && crane auth logout {shlex.quote(ref.host)}", check=False)

    def _push(self, r: Remote, env: EnvSpec, target: str) -> None:
        script = (
            S.PUSH_SH.replace("@ENV@", env.name)
            .replace("@BASE@", shlex.quote(env.image))
            .replace("@TARGET@", shlex.quote(target))
        )
        r.put_text(script, f"{S.ROOT}/push.sh", "755")
        code = self.run_job(r, f"_push-{time.strftime('%H%M%S')}", f"bash {S.ROOT}/push.sh")
        if code != 0:
            raise ConfigError(f"Image hochladen fehlgeschlagen (Code {code})")
        say(f"Image bereit: {target}. Neue Pods starten ab jetzt direkt daraus.")

    def alive(self) -> list[dict]:
        return [p for p in self.mine() if p.get("status") in ALIVE]

    def temporary(self, env: EnvSpec) -> tuple[dict, Remote]:
        """Kurzer Pod nur fuer Dateien auf dem Volume, ohne die Umgebung. Der Aufrufer loescht ihn."""
        pod = self.create(env, wait_minutes=self.cfg.wait_minutes)
        try:
            r = self.wait_ssh(pod)
            self.install(r, env)
        except BaseException:
            say(f"Pod {pod['id']} wird wieder geloescht")
            self.api.terminate_pod(pod["id"])
            raise
        return pod, r

    def down(self, pod: dict) -> None:
        self.api.terminate_pod(pod["id"])
        say(f"Pod {pod['id']} ({pod.get('name')}) geloescht")
