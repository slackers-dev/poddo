"""Eine Aufgabe ausfuehren: Argumente aufloesen, hochladen, rechnen, herunterladen."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import pod_scripts as S
from .api import ApiError
from .config import ConfigError, Project, TaskSpec, state_dir
from .pod import ALIVE, LEGACY_RESULTS, Pods, results_archive, say
from .remote import Remote, RemoteError


def resolve_args(task: TaskSpec, given: list[str], cwd: Path) -> dict[str, str]:
    """Positionsargumente nach `args` benennen, Vorgaben ergaenzen. Pfade werden absolut."""
    names = [a.rstrip("?") for a in task.args]
    required = [a for a in task.args if not a.endswith("?")]
    if len(given) < len(required) or len(given) > len(names):
        usage = " ".join(f"[{a[:-1]}]" if a.endswith("?") else f"<{a}>" for a in task.args)
        raise ConfigError(f"poddo run {task.name} {usage}")
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


# -- Stand der Jobs ----------------------------------------------------------------------------------


@dataclass
class JobStatus:
    job: str
    state: str  # running | saving | done | missing (auf dem Pod) | gone (Pod geloescht) | lost
    code: int | None = None
    started: int | None = None
    finished: int | None = None
    fetched: bool = False
    progress: str = ""  # von poddo-progress: "<erledigt> <total> [text]"
    last: str = ""  # letzte Zeile im Log
    saved: bool = False  # Ergebnisse auf dem Global Volume gesichert
    now: int = 0  # Uhr des Pods


def _int(v: str) -> int | None:
    return int(v) if v.strip().lstrip("-").isdigit() else None


def parse_status(text: str) -> dict[str, JobStatus]:
    """Ausgabe von status.sh lesen."""
    now = 0
    out: dict[str, JobStatus] = {}
    for line in text.splitlines():
        if line.startswith("@now "):
            now = _int(line[5:]) or 0
            continue
        f = (line.split("\t") + [""] * 9)[:9]
        if not f[0]:
            continue
        out[f[0]] = JobStatus(
            job=f[0],
            state=f[1] or "missing",
            code=_int(f[2]),
            started=_int(f[3]),
            finished=_int(f[4]),
            fetched=f[5] == "1",
            progress=f[6].strip(),
            last=f[7].strip(),
            saved=f[8].strip() == "1",
        )
    for st in out.values():
        st.now = now
    return out


TQDM = re.compile(r"(\d{1,3})%\|")


def progress_of(st: JobStatus) -> tuple[float | None, str]:
    """Anteil 0..1 (oder None) und Text: aus poddo-progress, sonst aus einer tqdm-Zeile im Log."""
    parts = st.progress.split(" ", 2)
    if len(parts) >= 2 and parts[0].replace(".", "", 1).isdigit() and parts[1].replace(".", "", 1).isdigit():
        done, total = float(parts[0]), float(parts[1])
        label = parts[2] if len(parts) > 2 else ""
        label = f"{parts[0]}/{parts[1]}" + (f" {label}" if label else "")
        return (min(done / total, 1.0) if total > 0 else None), label
    m = TQDM.search(st.last)
    if m:
        return min(int(m.group(1)) / 100, 1.0), ""
    return None, ""


def bar(frac: float, width: int = 20) -> str:
    n = round(frac * width)
    return "[" + "#" * n + "-" * (width - n) + f"] {int(frac * 100):3d}%"


def duration(seconds: int) -> str:
    seconds = max(0, int(seconds))
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def describe(st: JobStatus, keep_minutes: int, volume: bool = False) -> str:
    """Eine Zeile fuer poddo jobs."""
    if st.fetched:
        if st.state != "done":
            return "abgeholt"
    if st.state == "lost" or (st.state == "gone" and not volume):
        return "weg: Pod geloescht, Ergebnisse NICHT abgeholt (verloren)"
    if st.state == "gone":
        return f"Pod geloescht, Ergebnisse auf dem Volume (wenn der Job fertig wurde): poddo attach {st.job}"
    if st.state == "missing":
        return "unbekannt (gibt es auf dem Pod nicht)"
    if st.state == "saving":
        return "fertig, sichert die Ergebnisse auf dem Volume"
    if st.state == "running":
        el = duration(st.now - st.started) if st.started and st.now else "?"
        frac, label = progress_of(st)
        text = f"laeuft {el}"
        if frac is not None:
            text += "  " + bar(frac)
        if label:
            text += "  " + label
        elif st.last and frac is None:
            text += "  | " + st.last[:70]
        return text
    took = duration(st.finished - st.started) if st.finished and st.started else "?"
    head = f"fertig nach {took}, Code {st.code}"
    if st.fetched:
        return head + ", abgeholt"
    if st.saved:
        return f"{head}, NICHT abgeholt, auf dem Volume gesichert: poddo attach {st.job}"
    left = ""
    if st.finished and st.now:
        mins = keep_minutes - (st.now - st.finished) // 60
        left = f" (Pod loescht sich in etwa {max(mins, 0)} min)"
    return f"{head}, NICHT abgeholt, nur auf dem Pod{left}: poddo attach {st.job}"


def results_warning(job: str, keep_minutes: int, volume: bool = False) -> None:
    """Gut sichtbar: wo die Ergebnisse liegen und wie man sie holt."""
    if volume:
        lines = [
            f"Job {job} laeuft auf dem Pod weiter.",
            "Am Ende sichert der Pod die Ergebnisse auf dem Global Volume: sie ueberleben den Pod.",
            f"Holen:  poddo attach {job}      Stand:  poddo jobs",
            "(Ist der Pod dann schon weg, startet attach kurz einen, etwa 1 Rappen.)",
        ]
    else:
        lines = [
            f"Job {job} laeuft auf dem Pod weiter. Die Ergebnisse liegen NUR dort.",
            f"Nach dem Ende wartet der Pod etwa {keep_minutes} min, dann loescht er sich samt Ergebnissen.",
            f"Holen:  poddo attach {job}      Stand:  poddo jobs",
        ]
    w = max(len(x) for x in lines)
    print("\n+" + "-" * (w + 2) + "+", file=sys.stderr)
    for x in lines:
        print(f"| {x:<{w}} |", file=sys.stderr)
    print("+" + "-" * (w + 2) + "+\n", file=sys.stderr, flush=True)


def notify_local(title: str, text: str) -> None:
    """Mitteilung auf dem Mac (Text als Argument, nicht im Skript: keine Sonderzeichen-Probleme)."""
    if sys.platform != "darwin":
        return
    script = ["-e", "on run argv", "-e", "display notification (item 2 of argv) with title (item 1 of argv)"]
    try:
        subprocess.run(["osascript", *script, "-e", "end run", title, text], capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass


class PodGone(ConfigError):
    pass


# -- Aufgaben ----------------------------------------------------------------------------------------


class Runner:
    def __init__(self, pods: Pods, project: Project):
        self.pods = pods
        self.project = project

    @property
    def keep_minutes(self) -> int:
        return max(int(self.pods.cfg.keep_results_minutes), int(self.pods.cfg.idle_minutes))

    @property
    def volume(self) -> bool:
        return bool(self.pods.cfg.volume)

    def run(
        self,
        task_name: str,
        args: list[str],
        gpu: list[str] | None = None,
        down: bool | None = None,
        wait_minutes: float | None = None,
        detach: bool = False,
    ) -> int:
        task = self.project.tasks.get(task_name)
        if not task:
            raise ConfigError(
                f"Aufgabe {task_name!r} fehlt in poddo.toml (da: {', '.join(self.project.tasks)})"
            )
        values = resolve_args(task, args, Path.cwd())
        local_hook(task.before, values, self.project.root, "before")

        pod, r = self.pods.ensure(task.env, gpu, wait_minutes)
        self.collect(r, pod["id"])
        job = f"{task.name}-{time.strftime('%Y%m%d-%H%M%S')}"
        jobdir = f"{S.ROOT}/jobs/{job}"
        self._save(
            job, {"job": job, "task": task.name, "values": values, "pod": pod["id"], "started": time.time()}
        )

        for remote_rel, local_tpl in task.upload.items():
            local = Path(fmt(local_tpl, values)).expanduser()
            if not local.is_absolute():
                local = self.project.root / local
            say(f"hochladen: {local} -> {remote_rel}")
            r.upload(local, f"{jobdir}/{remote_rel}")

        header = "\n".join(f"export {k}={shlex.quote(v)}" for k, v in env_vars(values).items())
        header += f"\nexport JOB={shlex.quote(jobdir)}"
        self.pods.start_job(r, job, task.run, header, save=list(task.download))
        say(f"rechne {job} (losgeloest: Ctrl-C oder Laptop zu stoppt den Job nicht)")
        if detach:
            results_warning(job, self.keep_minutes, self.volume)
            return 0
        t0 = time.time()
        try:
            code = self.pods.follow(r, job)
        except (KeyboardInterrupt, RemoteError):
            results_warning(job, self.keep_minutes, self.volume)
            raise
        say(f"fertig nach {int(time.time() - t0)} s, Code {code}")
        self.fetch(r, task, values, jobdir, job)
        notify_local("poddo", f"{job}: fertig, Code {code}, Ergebnisse geholt")
        if code == 0:
            local_hook(task.after, values, self.project.root, "after")
        if down if down is not None else task.down:
            self.pods.down(pod)
        else:
            say("Pod laeuft weiter (Waechter loescht ihn bei Leerlauf). Sofort weg: poddo down")
        return code

    def fetch(self, r: Remote, task: TaskSpec, values: dict[str, str], jobdir: str, job: str) -> None:
        for remote_rel, local_tpl in task.download.items():
            local = Path(fmt(local_tpl, values)).expanduser()
            if not local.is_absolute():
                local = self.project.root / local
            if r.download(f"{jobdir}/{remote_rel}", local):
                say(f"heruntergeladen: {remote_rel} -> {local}")
            else:
                say(f"{remote_rel} gibt es auf dem Pod nicht")
        # Ab jetzt darf der Waechter den Pod wieder nach der normalen Leerlaufzeit loeschen. Die Kopie auf
        # dem Volume braucht es nicht mehr (download wirft bei einem Fehler, hier ist alles angekommen).
        cleanup = f"touch {shlex.quote(jobdir)}/fetched"
        if self.volume:
            for arch in self._archives(job):
                cleanup += f"; rm -f {shlex.quote(arch)}.ok {shlex.quote(arch)}"
        r.run(cleanup, check=False)
        self._update(job, fetched=True)

    # -- Ergebnisse vom Volume ----------------------------------------------------------------------

    def _archives(self, job: str) -> list[str]:
        """Archiv auf dem Volume, dazu der Ort aus der Zeit vor der Umbenennung (pod-do)."""
        return [
            results_archive(self.project.name, job),
            results_archive(self.project.name, job, LEGACY_RESULTS),
        ]

    def _restore(self, r: Remote, job: str) -> int | None:
        """Gesicherte Ergebnisse vom Volume in den Job-Ordner auf diesem Pod. Exit-Code des Jobs, oder
        None, wenn auf dem Volume nichts liegt."""
        jobdir = shlex.quote(f"{S.ROOT}/jobs/{job}")
        for arch in self._archives(job):
            res = r.run(
                f"bash -c {shlex.quote(S.RESTORE_RESULTS_SH)} restore {shlex.quote(arch)} {jobdir}"
                f" && cat {jobdir}/exitcode",
                check=False,
                capture=True,
            )
            if res.returncode == 3:
                continue
            if res.returncode != 0:
                raise RemoteError(
                    f"Ergebnisse von {job} vom Volume holen fehlgeschlagen: {res.stderr.strip()}"
                )
            return _int(res.stdout.strip().splitlines()[-1] if res.stdout.strip() else "")
        return None

    def collect(self, r: Remote, current_pod: str) -> None:
        """Nicht abgeholte Ergebnisse frueherer Jobs (anderer Pods) vom Volume holen, nebenbei und gratis."""
        if not self.volume:
            return
        for st in self.unfetched()[-20:]:
            task = self.project.tasks.get(st["task"])
            if st["pod"] == current_pod or not task:
                continue
            code = self._restore(r, st["job"])
            if code is None:
                continue
            say(f"Ergebnisse von {st['job']} (Code {code}) lagen noch auf dem Volume, hole sie")
            self.fetch(r, task, st["values"], f"{S.ROOT}/jobs/{st['job']}", st["job"])
            notify_local("poddo", f"{st['job']}: Ergebnisse vom Volume geholt")

    def _from_volume(self, st: dict, task: TaskSpec) -> int:
        """Der Pod des Jobs ist weg: Ergebnisse ueber einen anderen Pod vom Volume holen."""
        alive = self.pods.alive()
        own = [p for p in alive if p.get("name", "").endswith(f"-{task.env}")]
        created = None
        if own or alive:
            pod = (own or alive)[0]
            say(f"Pod von {st['job']} ist weg, hole die Ergebnisse ueber den laufenden Pod {pod['id']}")
            r = self.pods.remote(pod)
        else:
            say(f"Pod von {st['job']} ist weg. Starte kurz einen Pod, um die Ergebnisse vom Volume zu holen")
            pod, r = self.pods.temporary(self.project.envs[task.env])
            created = pod
        try:
            code = self._restore(r, st["job"])
            if code is None:
                self._update(st["job"], gone=True, lost=True)
                raise ConfigError(
                    f"Auf dem Volume liegen keine Ergebnisse von {st['job']}. Der Pod wurde wohl geloescht, "
                    "bevor der Job fertig war (max_hours?). Die Ergebnisse sind weg."
                )
            self.fetch(r, task, st["values"], f"{S.ROOT}/jobs/{st['job']}", st["job"])
        finally:
            if created:
                self.pods.down(created)
        notify_local("poddo", f"{st['job']}: Ergebnisse vom Volume geholt")
        return code if code is not None else 1

    # -- Wieder anhaengen, Log, Stand ---------------------------------------------------------------

    def _pod_remote(self, st: dict) -> tuple[dict, Remote]:
        """Pod eines Jobs, oder PodGone, wenn es ihn nicht mehr gibt."""
        try:
            pod = self.pods.api.get_pod(st["pod"])
        except ApiError as e:
            if e.status != 404:
                raise
            pod = {"status": "TERMINATED"}
        if pod.get("status") not in ALIVE:
            self._update(st["job"], gone=True)
            raise PodGone(f"Den Pod von {st['job']} gibt es nicht mehr ({pod.get('status')}).")
        return pod, self.pods.remote(pod)

    def attach(self, job: str | None) -> int:
        st = self._load(job)
        task = self.project.tasks.get(st["task"])
        if not task:
            raise ConfigError(f"Aufgabe {st['task']!r} gibt es nicht mehr in poddo.toml")
        try:
            _, r = self._pod_remote(st)
        except PodGone as e:
            if st.get("fetched"):
                raise ConfigError(f"{e} Die Ergebnisse sind schon abgeholt.") from e
            if not self.volume:
                self._update(st["job"], lost=True)
                raise ConfigError(f"{e} Ohne Volume sind die Ergebnisse weg.") from e
            return self._from_volume(st, task)
        try:
            code = self.pods.follow(r, st["job"])
        except (KeyboardInterrupt, RemoteError):
            results_warning(st["job"], self.keep_minutes, self.volume)
            raise
        self.fetch(r, task, st["values"], f"{S.ROOT}/jobs/{st['job']}", st["job"])
        notify_local("poddo", f"{st['job']}: fertig, Code {code}, Ergebnisse geholt")
        return code

    def logs(self, job: str | None, follow: bool = False, lines: int = 40) -> int:
        st = self._load(job)
        try:
            _, r = self._pod_remote(st)
        except PodGone as e:
            hint = " Das Log liegt mit den Ergebnissen auf dem Volume: poddo attach." if self.volume else ""
            raise ConfigError(f"{e}{hint}") from e
        d = shlex.quote(f"{S.ROOT}/jobs/{st['job']}")
        n = int(lines)
        if follow:
            script = (
                f"cd {d} && touch log && (tail -n {n} -F log 2>/dev/null & T=$!; "
                "while [ -e running ]; do sleep 1; done; sleep 1; kill $T 2>/dev/null)"
            )
        else:
            script = f"tail -n {n} {d}/log"
        try:
            return r.run(script, check=False).returncode
        except KeyboardInterrupt:
            return 0

    def statuses(self, limit: int = 10) -> list[JobStatus]:
        """Die letzten Jobs dieses Projekts mit ihrem Stand, ein SSH-Aufruf pro Pod."""
        states = self._states()[-limit:]
        by_pod: dict[str, list[dict]] = {}
        for st in states:
            if not st.get("gone"):
                by_pod.setdefault(st["pod"], []).append(st)
        found: dict[str, JobStatus] = {}
        for pod_id, sts in by_pod.items():
            try:
                pod = self.pods.api.get_pod(pod_id)
            except ApiError as e:
                if e.status != 404:
                    raise
                pod = {"status": "TERMINATED"}
            if pod.get("status") not in ALIVE:
                for s in sts:
                    self._update(s["job"], gone=True)
                continue
            names = " ".join(shlex.quote(s["job"]) for s in sts)
            try:
                out = self.pods.remote(pod).run(f"bash {S.ROOT}/status.sh {names}", capture=True).stdout
            except (RemoteError, ConfigError):
                continue
            found.update(parse_status(out))
        result = []
        for st in states:
            js = found.get(st["job"]) or JobStatus(st["job"], "lost" if st.get("lost") else "gone")
            js.fetched = js.fetched or bool(st.get("fetched"))
            result.append(js)
        return result

    def jobs(self, watch: bool = False, limit: int = 10) -> int:
        while True:
            rows = self.statuses(limit)
            if watch:
                print("\033[H\033[J", end="")
            if not rows:
                print("Noch keine Jobs in diesem Projekt.")
            for js in reversed(rows):
                print(f"{js.job:32} {describe(js, self.keep_minutes, self.volume)}")
            if not watch:
                return 0
            print(f"\n{time.strftime('%H:%M:%S')}  aktualisiert alle 5 s, Ctrl-C beendet die Anzeige")
            try:
                time.sleep(5)
            except KeyboardInterrupt:
                return 0

    # -- Merken, was lief (fuer attach, jobs, logs) --------------------------------------------------

    def _dir(self) -> Path:
        return state_dir() / self.project.name

    def _state_file(self, job: str) -> Path:
        return self._dir() / f"{job}.json"

    def _save(self, job: str, data: dict) -> None:
        f = self._state_file(job)
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data))

    def _update(self, job: str, **changes) -> None:
        f = self._state_file(job)
        if f.exists():
            self._save(job, {**json.loads(f.read_text()), **changes})

    def _states(self) -> list[dict]:
        d = self._dir()
        states = []
        for f in d.glob("*.json") if d.exists() else []:
            st = json.loads(f.read_text())
            st.setdefault("started", f.stat().st_mtime)  # aeltere Eintraege ohne Startzeit
            states.append(st)
        return sorted(states, key=lambda st: st["started"])

    def _load(self, job: str | None) -> dict:
        states = self._states()
        if job:
            states = [s for s in states if s["job"] == job]
        if not states:
            raise ConfigError("Kein Job gefunden" + (f": {job}" if job else ""))
        return states[-1]

    def unfetched(self) -> list[dict]:
        """Jobs, deren Ergebnisse noch nicht geholt sind und noch zu holen gehen (nur lokal gemerkt).
        Mit Volume auch die, deren Pod schon weg ist: deren Ergebnisse liegen auf dem Volume."""
        return [
            s
            for s in self._states()
            if not s.get("fetched") and not s.get("lost") and (self.volume or not s.get("gone"))
        ]
