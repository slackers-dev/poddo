"""Eine Aufgabe ausfuehren: Argumente aufloesen, hochladen, rechnen, herunterladen."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from . import pod_scripts as S
from .config import ConfigError, Project, TaskSpec
from .pod import Pods, say
from .remote import Remote


def state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "pod-do"


def resolve_args(task: TaskSpec, given: list[str], cwd: Path) -> dict[str, str]:
    """Positionsargumente nach `args` benennen, Vorgaben ergaenzen. Pfade werden absolut."""
    names = [a.rstrip("?") for a in task.args]
    required = [a for a in task.args if not a.endswith("?")]
    if len(given) < len(required) or len(given) > len(names):
        usage = " ".join(f"[{a[:-1]}]" if a.endswith("?") else f"<{a}>" for a in task.args)
        raise ConfigError(f"pod-do run {task.name} {usage}")
    values: dict[str, str] = {}
    for n, v in zip(names, given, strict=False):
        p = (cwd / v).expanduser()
        values[n] = str(p.resolve()) if p.exists() else v
    for n, expr in task.defaults.items():
        if n not in values:
            values[n] = fmt(expr, values)
    for n in names:
        values.setdefault(n, "")
    return values


def fmt(template: str, values: dict[str, str]) -> str:
    """{name} und {name.stem}, {name.name}, {name.parent}, {name.suffix} fuer Pfade."""
    paths = {k: Path(v) for k, v in values.items()}
    try:
        return template.format(**paths)
    except (KeyError, IndexError, AttributeError) as e:
        raise ConfigError(f"Platzhalter in {template!r} unbekannt: {e}") from e


def env_vars(values: dict[str, str]) -> dict[str, str]:
    return {re.sub(r"\W", "_", k).upper(): v for k, v in values.items()}


def local_hook(script: str, values: dict[str, str], root: Path, what: str) -> None:
    if not script:
        return
    say(f"{what} (lokal)")
    r = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", script], cwd=root, env={**os.environ, **env_vars(values)}
    )
    if r.returncode != 0:
        raise ConfigError(f"{what} fehlgeschlagen (Code {r.returncode})")


class Runner:
    def __init__(self, pods: Pods, project: Project):
        self.pods = pods
        self.project = project

    def run(
        self,
        task_name: str,
        args: list[str],
        gpu: list[str] | None = None,
        down: bool | None = None,
        wait_minutes: float | None = None,
    ) -> int:
        task = self.project.tasks.get(task_name)
        if not task:
            raise ConfigError(
                f"Aufgabe {task_name!r} fehlt in pod-do.toml (da: {', '.join(self.project.tasks)})"
            )
        values = resolve_args(task, args, Path.cwd())
        local_hook(task.before, values, self.project.root, "before")

        pod, r = self.pods.ensure(task.env, gpu, wait_minutes)
        job = f"{task.name}-{time.strftime('%Y%m%d-%H%M%S')}"
        jobdir = f"{S.ROOT}/jobs/{job}"
        self._save(job, task, values, pod)

        for remote_rel, local_tpl in task.upload.items():
            local = Path(fmt(local_tpl, values)).expanduser()
            if not local.is_absolute():
                local = self.project.root / local
            say(f"hochladen: {local} -> {remote_rel}")
            r.upload(local, f"{jobdir}/{remote_rel}")

        say(f"rechne {job}")
        header = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in env_vars(values).items())
        header += f"\nexport JOB={shlex.quote(jobdir)}"
        t0 = time.time()
        code = self.pods.run_job(r, job, task.run, header)
        say(f"fertig nach {int(time.time() - t0)} s, Code {code}")
        self.fetch(r, task, values, jobdir)
        if code == 0:
            local_hook(task.after, values, self.project.root, "after")
        if down if down is not None else task.down:
            self.pods.down(pod)
        else:
            say("Pod laeuft weiter (Waechter loescht ihn bei Leerlauf). Sofort weg: pod-do down")
        return code

    def fetch(self, r: Remote, task: TaskSpec, values: dict[str, str], jobdir: str) -> None:
        for remote_rel, local_tpl in task.download.items():
            local = Path(fmt(local_tpl, values)).expanduser()
            if not local.is_absolute():
                local = self.project.root / local
            if r.download(f"{jobdir}/{remote_rel}", local):
                say(f"heruntergeladen: {remote_rel} -> {local}")
            else:
                say(f"{remote_rel} gibt es auf dem Pod nicht")

    def attach(self, job: str | None) -> int:
        st = self._load(job)
        task = self.project.tasks[st["task"]]
        pod = self.pods.api.get_pod(st["pod"])
        r = self.pods.remote(pod)
        code = self.pods.follow(r, st["job"])
        self.fetch(r, task, st["values"], f"{S.ROOT}/jobs/{st['job']}")
        return code

    # -- Merken, was zuletzt lief (fuer attach) ---------------------------------------------------

    def _state_file(self, job: str) -> Path:
        return state_dir() / self.project.name / f"{job}.json"

    def _save(self, job: str, task: TaskSpec, values: dict[str, str], pod: dict) -> None:
        f = self._state_file(job)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps({"job": job, "task": task.name, "values": values, "pod": pod["id"]}))

    def _load(self, job: str | None) -> dict:
        d = state_dir() / self.project.name
        files = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime) if d.exists() else []
        if job:
            files = [f for f in files if f.stem == job]
        if not files:
            raise ConfigError("Kein Job gefunden" + (f": {job}" if job else ""))
        return json.loads(files[-1].read_text())
