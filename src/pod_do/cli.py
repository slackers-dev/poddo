"""pod-do: schwere Aufgaben vom Laptop auf eine RunPod-GPU abgeben."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, registry
from .api import ApiError, RunPod
from .config import (
    PROJECT_FILE,
    TEMPLATE,
    ConfigError,
    GlobalConfig,
    Project,
    api_key,
    config_dir,
    store_api_key_interactive,
)
from .pod import Pods, gpu_candidates, image_ref, say
from .remote import RemoteError
from .task import Runner


def _ctx(need_project: bool = True):
    cfg = GlobalConfig.load()
    api = RunPod(api_key())
    project = Project.find() if need_project else None
    return cfg, api, project


def _env_arg(project: Project, env: str | None) -> str:
    if env:
        return env
    if len(project.envs) == 1:
        return next(iter(project.envs))
    raise ConfigError(f"Welche Umgebung? {', '.join(project.envs)}")


def cmd_login(a) -> int:
    where = store_api_key_interactive()
    api = RunPod(api_key())
    bal = api.balance()
    print(f"Gespeichert in {where}. Guthaben: ${bal['balanceAmount']:.2f}")
    cfg = GlobalConfig.load()
    print(f"SSH-Schluessel: {cfg.ssh_key or 'keiner gefunden (ssh-keygen -t ed25519)'}")
    return 0


def cmd_init(a) -> int:
    p = Path.cwd() / PROJECT_FILE
    if p.exists():
        print(f"{p} gibt es schon")
        return 1
    p.write_text(TEMPLATE)
    print(f"{p} angelegt. Umgebung und Aufgabe anpassen, dann: pod-do run hello <datei>")
    return 0


def cmd_gpus(a) -> int:
    cfg, api, _ = _ctx(need_project=False)
    cloud = a.cloud or cfg.cloud
    gpus = api.gpus(cloud)
    key = "community" if cloud == "COMMUNITY" else "secure"
    rows = []
    for g in gpus:
        if g.get("manufacturer") != "NVIDIA" and not a.all:
            continue
        avail = g.get("availability") or "NONE"
        if avail == "NONE" and not a.all:
            continue
        price = (g.get("price") or {}).get(key) or 0
        if g.get("memory", 0) < a.min_vram or (price == 0 and not a.all):
            continue
        rows.append((price, g))
    rows.sort(key=lambda x: x[0])
    print(f"{'GPU-ID (fuer gpus = [...])':40} {'VRAM':>5} {'$/h':>6}  Verfuegbar  ({cloud})")
    for price, g in rows:
        mark = " *" if g["id"] in cfg.gpus else ""
        print(f"{g['id']:40} {g.get('memory', 0):>4}G {price:>6.2f}  {g.get('availability') or 'NONE'}{mark}")
    print("* = in deiner Standardliste")
    return 0


def cmd_up(a) -> int:
    cfg, api, project = _ctx()
    pods = Pods(api, cfg, project)
    pod, _ = pods.ensure(_env_arg(project, a.env), a.gpu, a.wait)
    print(pod["id"])
    return 0


def cmd_run(a) -> int:
    cfg, api, project = _ctx()
    runner = Runner(Pods(api, cfg, project), project)
    down = True if a.down else (False if a.keep else None)
    return runner.run(a.task, a.args, a.gpu, down, a.wait, detach=a.detach)


def cmd_attach(a) -> int:
    cfg, api, project = _ctx()
    return Runner(Pods(api, cfg, project), project).attach(a.job)


def cmd_jobs(a) -> int:
    cfg, api, project = _ctx()
    return Runner(Pods(api, cfg, project), project).jobs(watch=a.watch, limit=a.n)


def cmd_logs(a) -> int:
    cfg, api, project = _ctx()
    return Runner(Pods(api, cfg, project), project).logs(a.job, follow=a.follow, lines=a.n)


def cmd_ssh(a) -> int:
    cfg, api, project = _ctx()
    pods = Pods(api, cfg, project)
    env = _env_arg(project, a.env)
    pod = pods.find(env)
    if not pod:
        raise ConfigError(f"Kein Pod fuer {env}. Starten mit: pod-do up {env}")
    return pods.remote(pod).interactive(cd="/opt/pod-do")


def cmd_status(a) -> int:
    cfg, api, project = _ctx(need_project=False)
    bal = api.balance()
    print(f"Guthaben: ${bal['balanceAmount']:.2f}")
    all_pods = api.list_pods()
    mine = [p for p in all_pods if p.get("name", "").startswith("pod-do-")]
    others = len(all_pods) - len(mine)
    if not mine:
        print("Keine pod-do-Pods.")
    for p in mine:
        gpu = (p.get("gpu") or {}).get("id", "-")
        status, name, cost = p.get("status", "?"), p.get("name", ""), p.get("cost", 0)
        print(f"{p['id']:16} {status:12} {name:36} {gpu:32} ${cost:.2f}/h")
    if others:
        print(f"({others} weitere Pods, nicht von pod-do)")
    try:
        here = Project.find()
    except ConfigError:
        here = None
    if here:
        open_jobs = Runner(Pods(api, cfg, here), here).unfetched()
        for st in open_jobs:
            print(f"Ergebnisse NICHT abgeholt: {st['job']}  ->  pod-do attach {st['job']}")
        if open_jobs:
            print("(Details mit Fortschritt: pod-do jobs)")
    vol = Pods(api, cfg, project or Project(Path.cwd(), "-", {}, {})).volume_id() if cfg.volume else None
    print(
        f"Global Volume {cfg.volume!r}: {vol or 'fehlt (pod-do volume --create)'}"
        if cfg.volume
        else "Ohne Volume"
    )
    return 0


def cmd_down(a) -> int:
    cfg, api, project = _ctx(need_project=not a.all)
    if a.all:
        targets = [
            p
            for p in api.list_pods()
            if p.get("name", "").startswith("pod-do-") and p.get("status") != "TERMINATED"
        ]
    else:
        pods = Pods(api, cfg, project)
        targets = pods.mine() if not a.env else [p for p in [pods.find(a.env)] if p]
    if not targets:
        print("Nichts zu loeschen.")
        return 0
    for p in targets:
        api.terminate_pod(p["id"])
        say(f"Pod {p['id']} ({p.get('name')}) geloescht")
    return 0


def cmd_volume(a) -> int:
    cfg, api, _ = _ctx(need_project=False)
    if not cfg.volume:
        print('In der Konfig ist volume = "": ohne Volume.')
        return 0
    pods = Pods(api, cfg, Project(Path.cwd(), "-", {}, {}))
    vid = pods.volume_id(create=a.create)
    if vid:
        print(f"Global Volume {cfg.volume!r}: {vid} (auf dem Pod unter /vol)")
    else:
        print(f"Global Volume {cfg.volume!r} fehlt. Anlegen: pod-do volume --create (erste 100 GB gratis)")
    return 0


def cmd_check(a) -> int:
    """Konfig und GPU-Wahl pruefen, ohne etwas anzulegen."""
    cfg, api, project = _ctx()
    print(
        f"Projekt {project.name}: Umgebungen {', '.join(project.envs) or '-'}, "
        f"Aufgaben {', '.join(project.tasks) or '-'}"
    )
    for env in project.envs.values():
        cloud = env.cloud or cfg.cloud
        c = gpu_candidates(api, env.gpus or cfg.gpus, env.min_vram, cloud)
        first = f"{c[0].name} ${c[0].price:.2f}/h" if c else "KEINE frei"
        print(f"  {env.name}: {env.image}, ab {env.min_vram} GB -> zuerst {first} ({len(c)} Kandidaten)")
    print(f"SSH-Schluessel: {cfg.ssh_key}")
    return 0


def _set_config(key: str, value: str) -> None:
    """Eine Zeile `key = "value"` in ~/.config/pod-do/config.toml setzen."""
    f = config_dir() / "config.toml"
    f.parent.mkdir(parents=True, exist_ok=True)
    lines = f.read_text().splitlines() if f.exists() else []
    lines = [ln for ln in lines if not ln.split("=")[0].strip() == key]
    lines.append(f"{key} = {json.dumps(value)}")  # JSON-String ist auch ein gueltiger TOML-String
    f.write_text("\n".join(lines) + "\n")


def cmd_registry(a) -> int:
    cfg = GlobalConfig.load()
    if a.action == "login":
        if a.user:
            _set_config("registry_user", a.user)
        where = registry.store_token_interactive(a.host)
        print(f"Token gespeichert in {where}" + (f", Benutzer {a.user}" if a.user else ""))
        return 0
    # runpod: dieselben Zugangsdaten bei RunPod hinterlegen, damit Pods das private Image ziehen koennen
    user = a.user or cfg.registry_user
    if not user:
        raise ConfigError("Benutzer fehlt: --user <name> oder registry_user in der Konfig")
    api = RunPod(api_key())
    name = cfg.runpod_registry
    if any(r.get("name") == name for r in api.registries()):
        print(f"Registry-Zugangsdaten {name!r} gibt es bei RunPod schon.")
        return 0
    reg = api.create_registry(name, user, registry.token(a.host))
    print(f"Registry-Zugangsdaten {name!r} bei RunPod angelegt ({reg.get('id')})")
    return 0


def cmd_image(a) -> int:
    cfg, api, project = _ctx()
    env = _env_arg(project, a.env)
    target = image_ref(project.envs[env])
    if not target:
        raise ConfigError(f'env.{env} hat kein docker = "<registry>/<repo>" in pod-do.toml')
    pods = Pods(api, cfg, project)
    if not a.check:
        pods.ensure(env, a.gpu, a.wait)
    print(f"{target}: {'vorhanden' if pods.image_exists(target) else 'fehlt'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pod-do", description=__doc__)
    p.add_argument("--version", action="version", version=f"pod-do {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("login", help="RunPod-API-Schluessel sicher ablegen").set_defaults(fn=cmd_login)
    sub.add_parser("init", help=f"{PROJECT_FILE} mit Beispiel anlegen").set_defaults(fn=cmd_init)
    sub.add_parser("check", help="Konfig und GPU-Wahl pruefen, ohne etwas zu starten").set_defaults(
        fn=cmd_check
    )

    s = sub.add_parser("gpus", help="GPUs mit Preis und Verfuegbarkeit")
    s.add_argument("--min-vram", type=int, default=0)
    s.add_argument("--cloud", choices=["SECURE", "COMMUNITY"])
    s.add_argument("--all", action="store_true", help="auch ausverkaufte und nicht-NVIDIA")
    s.set_defaults(fn=cmd_gpus)

    s = sub.add_parser("up", help="Pod starten und Umgebung bereitmachen")
    s.add_argument("env", nargs="?")
    s.add_argument("--gpu", action="append", help="GPU-ID, mehrfach fuer Ausweichkarten")
    s.add_argument(
        "--wait", type=float, metavar="MIN", help="so lange auf eine freie GPU warten (Standard 10)"
    )
    s.set_defaults(fn=cmd_up)

    s = sub.add_parser("run", help="Aufgabe rechnen: hochladen, rechnen, Ergebnis holen")
    s.add_argument("task")
    s.add_argument("args", nargs="*")
    s.add_argument("--gpu", action="append", help="GPU-ID, mehrfach fuer Ausweichkarten")
    s.add_argument(
        "--wait", type=float, metavar="MIN", help="so lange auf eine freie GPU warten (Standard 10)"
    )
    s.add_argument(
        "--detach",
        action="store_true",
        help="nur starten, nicht warten (Ergebnisse spaeter mit pod-do attach holen)",
    )
    g = s.add_mutually_exclusive_group()
    g.add_argument("--down", action="store_true", help="Pod danach sofort loeschen")
    g.add_argument("--keep", action="store_true", help="Pod danach laufen lassen (Standard)")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("attach", help="wieder an einen laufenden Job haengen und Ergebnis holen")
    s.add_argument("job", nargs="?", help="Standard: der letzte")
    s.set_defaults(fn=cmd_attach)

    s = sub.add_parser("jobs", help="letzte Jobs: laeuft (mit Fortschritt), fertig, abgeholt?")
    s.add_argument("-w", "--watch", action="store_true", help="alle 5 s neu anzeigen")
    s.add_argument("-n", type=int, default=10, help="so viele Jobs (Standard 10)")
    s.set_defaults(fn=cmd_jobs)

    s = sub.add_parser("logs", help="Log eines Jobs (Standard: der letzte)")
    s.add_argument("job", nargs="?")
    s.add_argument("-f", "--follow", action="store_true", help="weiter mitlesen, bis der Job fertig ist")
    s.add_argument("-n", type=int, default=40, help="so viele Zeilen (Standard 40)")
    s.set_defaults(fn=cmd_logs)

    s = sub.add_parser("ssh", help="Shell auf dem Pod")
    s.add_argument("env", nargs="?")
    s.set_defaults(fn=cmd_ssh)

    sub.add_parser("status", help="Guthaben, laufende Pods, Volume").set_defaults(fn=cmd_status)

    s = sub.add_parser("down", help="Pods dieses Projekts loeschen")
    s.add_argument("env", nargs="?")
    s.add_argument("--all", action="store_true", help="alle pod-do-Pods, egal welches Projekt")
    s.set_defaults(fn=cmd_down)

    s = sub.add_parser("registry", help="Zugang zur Registry fuer Docker-Images")
    s.add_argument(
        "action", choices=["login", "runpod"], help="login: Token lokal ablegen, runpod: bei RunPod"
    )
    s.add_argument("host", nargs="?", default="ghcr.io")
    s.add_argument("--user", help="Benutzer der Registry (bei ghcr.io der GitHub-Name)")
    s.set_defaults(fn=cmd_registry)

    s = sub.add_parser("image", help="Umgebung als Docker-Image bauen und hochladen (env.docker)")
    s.add_argument("env", nargs="?")
    s.add_argument("--check", action="store_true", help="nur pruefen, ob es das Image schon gibt")
    s.add_argument("--gpu", action="append", help="GPU-ID, mehrfach fuer Ausweichkarten")
    s.add_argument("--wait", type=float, metavar="MIN", help="so lange auf eine freie GPU warten")
    s.set_defaults(fn=cmd_image)

    s = sub.add_parser("volume", help="Global Volume anzeigen oder anlegen")
    s.add_argument("--create", action="store_true")
    s.set_defaults(fn=cmd_volume)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except (ConfigError, ApiError, RemoteError) as e:
        print(f"pod-do: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
