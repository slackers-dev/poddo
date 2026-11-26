"""Einstellungen: global (~/.config/pod-do/config.toml, API-Schluessel) und pro Projekt (pod-do.toml)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROJECT_FILE = "pod-do.toml"
KEYCHAIN_SERVICE = "pod-do"
KEYCHAIN_ACCOUNT = "runpod-api-key"

# Kleine, guenstige Karten zuerst. Reicht fuer Inferenz und kleine Trainings.
DEFAULT_GPUS = [
    "NVIDIA RTX A4500",
    "NVIDIA RTX A4000",
    "NVIDIA RTX 2000 Ada Generation",
    "NVIDIA RTX 4000 Ada Generation",
    "NVIDIA RTX A5000",
    "NVIDIA A40",
    "NVIDIA GeForce RTX 4090",
]


class ConfigError(Exception):
    pass


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "pod-do"


@dataclass
class GlobalConfig:
    ssh_key: Path | None = None
    volume: str = "pod-do"  # Name des Global Volume, "" = ohne Volume
    cloud: str = "SECURE"
    gpus: list[str] = field(default_factory=lambda: list(DEFAULT_GPUS))
    idle_minutes: int = 20
    wait_minutes: float = 10  # so lange auf eine freie GPU warten
    max_hours: float = 6.0

    @classmethod
    def load(cls, path: Path | None = None) -> GlobalConfig:
        path = path or config_dir() / "config.toml"
        data: dict[str, Any] = {}
        if path.exists():
            data = tomllib.loads(path.read_text())
        cfg = cls()
        if "ssh_key" in data:
            cfg.ssh_key = Path(data["ssh_key"]).expanduser()
        for k in ("volume", "cloud", "gpus", "idle_minutes", "wait_minutes", "max_hours"):
            if k in data:
                setattr(cfg, k, data[k])
        if cfg.ssh_key is None:
            cfg.ssh_key = find_ssh_key()
        return cfg


def find_ssh_key() -> Path | None:
    ssh = Path.home() / ".ssh"
    for name in ("id_runpod", "id_ed25519", "id_rsa"):
        if (ssh / name).exists() and (ssh / f"{name}.pub").exists():
            return ssh / name
    return None


# -- API-Schluessel ---------------------------------------------------------------------------------


ENV_KEYS = ("RUNPOD_API_KEY", "POD_API_KEY")


def dotenv_key(start: Path | None = None) -> str:
    """Schluessel aus der naechsten .env (hier oder darueber), RUNPOD_API_KEY oder POD_API_KEY."""
    d = (start or Path.cwd()).resolve()
    for p in [d, *d.parents]:
        f = p / ".env"
        if not f.is_file():
            continue
        for line in f.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:].lstrip()
            name, sep, value = line.partition("=")
            if sep and name.strip() in ENV_KEYS:
                value = value.strip().strip("'\"")
                if value:
                    return value
    return ""


def api_key() -> str:
    """Umgebungsvariable, sonst .env, sonst macOS-Schluesselbund, sonst ~/.config/pod-do/api_key."""
    for name in ENV_KEYS:
        key = os.environ.get(name, "").strip()
        if key:
            return key
    key = dotenv_key()
    if key:
        return key
    if sys.platform == "darwin" and shutil.which("security"):
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True,
            text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
    f = config_dir() / "api_key"
    if f.exists():
        return f.read_text().strip()
    raise ConfigError("Kein RunPod-API-Schluessel. Einrichten mit: pod-do login")


def store_api_key_interactive() -> str:
    """Fragt den Schluessel verdeckt ab und legt ihn sicher ab. Gibt den Ort zurueck."""
    import getpass

    if sys.platform == "darwin" and shutil.which("security"):
        # -w als letztes Argument ohne Wert: security fragt selbst verdeckt nach, der Schluessel taucht
        # so nie in einer Prozessliste auf. -U ersetzt einen vorhandenen Eintrag.
        print("RunPod-API-Schluessel eingeben (zweimal, wird nicht angezeigt):")
        subprocess.run(
            ["security", "add-generic-password", "-U", "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT, "-w"],
            check=True,
        )
        return "macOS-Schluesselbund (Eintrag pod-do)"
    key = getpass.getpass("RunPod-API-Schluessel (wird nicht angezeigt): ").strip()
    if not key:
        raise ConfigError("leer, nichts gespeichert")
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    f = d / "api_key"
    f.write_text(key + "\n")
    f.chmod(0o600)
    return str(f)


# -- Projekt -----------------------------------------------------------------------------------------


@dataclass
class EnvSpec:
    name: str
    image: str
    setup: str = ""  # laeuft einmal, Ergebnis ($POD_DO_ENV) wird als Archiv auf dem Volume gesichert
    boot: str = ""  # laeuft bei jedem Pod-Start (z. B. apt-get), wird nicht gesichert
    gpus: list[str] = field(default_factory=list)
    min_vram: int = 0
    disk: int = 50
    cloud: str = ""
    cuda: str = ""  # niedrigste CUDA-Version des Hosts, z. B. "12.1"
    idle_minutes: int | None = None
    max_hours: float | None = None
    vars: dict[str, str] = field(default_factory=dict)


@dataclass
class TaskSpec:
    name: str
    env: str
    run: str
    args: list[str] = field(default_factory=list)  # "name" Pflicht, "name?" optional
    defaults: dict[str, str] = field(default_factory=dict)
    upload: dict[str, str] = field(default_factory=dict)  # Ziel im Job-Ordner = lokaler Pfad
    download: dict[str, str] = field(default_factory=dict)  # Pfad im Job-Ordner = lokaler Ordner
    before: str = ""  # lokal vor dem Hochladen
    after: str = ""  # lokal nach dem Herunterladen
    down: bool = False  # Pod nach der Aufgabe sofort loeschen


@dataclass
class Project:
    root: Path
    name: str
    envs: dict[str, EnvSpec]
    tasks: dict[str, TaskSpec]

    @classmethod
    def find(cls, start: Path | None = None) -> Project:
        d = (start or Path.cwd()).resolve()
        for p in [d, *d.parents]:
            if (p / PROJECT_FILE).exists():
                return cls.load(p / PROJECT_FILE)
        raise ConfigError(f"Kein {PROJECT_FILE} gefunden (hier oder darueber). Anlegen mit: pod-do init")

    @classmethod
    def load(cls, path: Path) -> Project:
        try:
            data = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as e:
            raise ConfigError(f"{path}: {e}") from e
        root = path.parent
        name = str(data.get("project") or root.name)
        envs = {}
        for n, e in (data.get("env") or {}).items():
            if "image" not in e:
                raise ConfigError(f"{path}: env.{n} braucht image")
            envs[n] = EnvSpec(
                name=n,
                image=e["image"],
                setup=_script(root, e, "setup"),
                boot=_script(root, e, "boot"),
                gpus=list(e.get("gpus", [])),
                min_vram=int(e.get("min_vram", 0)),
                disk=int(e.get("disk", 50)),
                cloud=str(e.get("cloud", "")),
                cuda=str(e.get("cuda", "")),
                idle_minutes=e.get("idle_minutes"),
                max_hours=e.get("max_hours"),
                vars={k: str(v) for k, v in (e.get("vars") or {}).items()},
            )
        tasks = {}
        for n, t in (data.get("task") or {}).items():
            env = t.get("env") or (next(iter(envs)) if len(envs) == 1 else None)
            if env not in envs:
                raise ConfigError(f"{path}: task.{n} braucht env (eines von {', '.join(envs) or '-'})")
            run = _script(root, t, "run")
            if not run:
                raise ConfigError(f"{path}: task.{n} braucht run oder run_file")
            tasks[n] = TaskSpec(
                name=n,
                env=env,
                run=run,
                args=list(t.get("args", [])),
                defaults={k: str(v) for k, v in (t.get("defaults") or {}).items()},
                upload={k: str(v) for k, v in (t.get("upload") or {}).items()},
                download={k: str(v) for k, v in (t.get("download") or {}).items()},
                before=str(t.get("before", "")),
                after=str(t.get("after", "")),
                down=bool(t.get("down", False)),
            )
        return cls(root=root, name=name, envs=envs, tasks=tasks)


def _script(root: Path, table: dict, key: str) -> str:
    """`key` als Text im toml oder `key_file` als Pfad relativ zum Projekt."""
    if table.get(key):
        return str(table[key])
    f = table.get(f"{key}_file")
    if f:
        p = (root / f).resolve()
        if not p.exists():
            raise ConfigError(f"{key}_file {f} fehlt")
        return p.read_text()
    return ""


TEMPLATE = """\
# pod-do: schwere Aufgaben auf einer RunPod-GPU rechnen. Doku: https://github.com/slackers-dev/pod-do

# Eine Umgebung = Docker-Image + einmaliges Setup. Was setup nach $POD_DO_ENV installiert, sichert
# pod-do als Archiv auf dem Volume. Beim naechsten Pod ist es in ein, zwei Minuten wieder da.
[env.main]
image = "runpod/pytorch:2.2.0-py3.10-cuda12.1.1-devel-ubuntu22.04"
min_vram = 8          # GB, filtert die GPU-Liste
# gpus = ["NVIDIA RTX A4500", "NVIDIA A40"]   # eigene Reihenfolge, sonst die globale
boot = "apt-get update -qq && apt-get install -y -qq ffmpeg"
setup = '''
cd "$POD_DO_ENV"
uv venv --python 3.10 .venv
uv pip install --python .venv/bin/python numpy
'''

# Eine Aufgabe: hochladen, rechnen, herunterladen. {name} sind Argumente von `pod-do run`.
[task.hello]
env = "main"
args = ["input", "name?"]
defaults = { name = "{input.stem}" }
upload = { "in" = "{input}" }                # Job-Ordner/in  <-  lokaler Pfad
run = '''
source "$POD_DO_ENV/.venv/bin/activate"
mkdir -p out
python -c "import numpy; print('numpy', numpy.__version__)" | tee out/info.txt
ls -la in >> out/info.txt
'''
download = { "out" = "results/{name}" }      # Job-Ordner/out  ->  lokaler Ordner
"""
