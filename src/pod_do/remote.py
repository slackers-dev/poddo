"""SSH und Dateitransfer zum Pod. Nur ssh und tar, beides ist auf Mac und Pod vorhanden.

Alle Befehle an einen Pod laufen ueber eine einzige SSH-Verbindung (ControlMaster). Ein neuer
Handshake zu einem Pod in einem anderen Rechenzentrum kostet gut eine Sekunde, und ein `pod-do run`
braucht sonst zwanzig davon.
"""

from __future__ import annotations

import io
import shlex
import subprocess
import tarfile
import time
from pathlib import Path

from .api import Ssh
from .config import state_dir

# So lange haelt die gemeinsame Verbindung nach dem letzten Befehl. Kurz, damit der Waechter auf dem Pod
# (zaehlt SSH-Sitzungen als Arbeit) nicht lange aufgehalten wird.
CONTROL_PERSIST = "120"
# Unix-Sockets duerfen auf macOS hoechstens 104 Zeichen lang sein
MAX_SOCKET_PATH = 100


class RemoteError(Exception):
    pass


class Remote:
    def __init__(self, ssh: Ssh, key: Path | None, state: Path | None = None):
        self.ssh = ssh
        self.key = key
        self._state = state or state_dir()
        self._master_tried = False
        self._mux: Path | None = None
        if ssh.pod_id:
            sock = self._state / "ssh" / ssh.pod_id
            if len(str(sock)) <= MAX_SOCKET_PATH:
                self._mux = sock

    # -- Verbindung -------------------------------------------------------------------------------

    def _opts(self) -> list[str]:
        a = [
            "-p",
            str(self.ssh.port),
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=4",
            "-o",
            "ConnectTimeout=10",
        ]
        if self.ssh.pod_id:
            # Jeder Pod hat einen neuen Host-Schluessel. Beim ersten Kontakt merken (unter der Pod-ID, nicht
            # unter host:port, die RunPod wiederverwendet), danach muss er stimmen.
            known = self._state / "known_hosts"
            known.parent.mkdir(parents=True, exist_ok=True)
            a += [
                "-o",
                f"HostKeyAlias=pod-do-{self.ssh.pod_id}",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                f"UserKnownHostsFile={known}",
            ]
        else:
            a += ["-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null"]
        if self.key:
            a += ["-i", str(self.key), "-o", "IdentitiesOnly=yes"]
        if self._mux:
            a += ["-o", f"ControlPath={self._mux}"]
        return a

    def base_args(self, tty: bool = False) -> list[str]:
        """ssh-Aufruf, der die gemeinsame Verbindung nimmt, wenn es sie gibt, sonst direkt verbindet."""
        self._ensure_master()
        mux = ["-o", "ControlMaster=no"] if self._mux else []
        return ["ssh", *mux, *self._opts(), "-t" if tty else "-T", f"{self.ssh.user}@{self.ssh.host}"]

    def _ensure_master(self) -> None:
        """Gemeinsame Verbindung im Hintergrund starten (einmal). Geht das nicht, eben ohne."""
        if not self._mux or self._master_tried:
            return
        self._master_tried = True
        dest = f"{self.ssh.user}@{self.ssh.host}"
        self._mux.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self._mux.exists():
            alive = subprocess.run(
                ["ssh", *self._opts(), "-O", "check", dest],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
            )
            if alive.returncode == 0:
                return
            self._mux.unlink(missing_ok=True)  # Reste einer alten Verbindung
        # Alle Kanaele auf /dev/null: sonst haelt der Hintergrundprozess unsere Pipes offen
        try:
            subprocess.run(
                ["ssh", "-o", "ControlMaster=yes", "-o", f"ControlPersist={CONTROL_PERSIST}"]
                + self._opts()
                + ["-N", "-f", dest],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        """Gemeinsame Verbindung beenden (z. B. nach pod-do down)."""
        if self._mux and self._mux.exists():
            subprocess.run(
                ["ssh", *self._opts(), "-O", "exit", f"{self.ssh.user}@{self.ssh.host}"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
            )

    # -- Befehle ----------------------------------------------------------------------------------

    def run(
        self,
        script: str,
        *,
        check: bool = True,
        capture: bool = False,
        tty: bool = False,
        stdin=None,
        stdin_text: str | None = None,
    ) -> subprocess.CompletedProcess:
        """Fuehrt `script` mit bash -lc auf dem Pod aus. stdin_text geht als Eingabe mit (z. B. ein Token)."""
        cmd = self.base_args(tty=tty) + ["bash -lc " + shlex.quote(script)]
        if stdin_text is not None:
            r = subprocess.run(cmd, input=stdin_text, text=True, capture_output=capture)
        else:
            r = subprocess.run(cmd, text=capture, capture_output=capture, stdin=stdin)
        if check and r.returncode != 0:
            err = (r.stderr or "").strip() if capture else ""
            raise RemoteError(
                f"Befehl auf dem Pod fehlgeschlagen (Code {r.returncode}){': ' + err if err else ''}"
            )
        return r

    def ok(self) -> bool:
        """Antwortet der Pod per SSH? Ohne gemeinsame Verbindung, die entsteht erst danach."""
        mux = ["-o", "ControlMaster=no"] if self._mux else []
        cmd = ["ssh", *mux, *self._opts(), "-T", f"{self.ssh.user}@{self.ssh.host}", "true"]
        try:
            r = subprocess.run(cmd, stdin=subprocess.DEVNULL, capture_output=True, timeout=25)
        except subprocess.TimeoutExpired:
            return False
        return r.returncode == 0

    def interactive(self, cd: str | None = None) -> int:
        cmd = self.base_args(tty=True)
        if cd:
            cmd.append(f"cd {shlex.quote(cd)} && exec bash -l")
        return subprocess.call(cmd)

    # -- Dateien ----------------------------------------------------------------------------------

    def put_text(self, text: str, remote_path: str, mode: str = "644") -> None:
        self.put_files({remote_path: (text, int(mode, 8))})

    def put_files(self, files: dict[str, tuple[str, int]], then: str = "") -> None:
        """Mehrere Textdateien (absoluter Pfad -> (Inhalt, Modus)) in einem tar ueber eine Verbindung,
        danach optional `then` im selben Aufruf."""
        buf = io.BytesIO()
        now = time.time()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.GNU_FORMAT) as tar:
            for path, (text, mode) in files.items():
                data = text.encode()
                info = tarfile.TarInfo(path.lstrip("/"))
                info.size, info.mode, info.mtime = len(data), mode, now
                tar.addfile(info, io.BytesIO(data))
        script = "tar -C / -xf - --no-same-owner"
        if then:
            script += " && " + then
        r = subprocess.run(self.base_args() + ["bash -c " + shlex.quote(script)], input=buf.getvalue())
        if r.returncode != 0:
            raise RemoteError(f"Konnte {', '.join(files)} nicht schreiben")

    def upload(self, local: Path, remote: str) -> None:
        """Ordner: Inhalt nach `remote/`. Datei: nach `remote` (genau dieser Name)."""
        q = shlex.quote(remote)
        if local.is_dir():
            tar = subprocess.Popen(
                ["tar", "--no-xattrs", "--no-mac-metadata", "-C", str(local), "-cf", "-", "."],
                stdout=subprocess.PIPE,
                env={"COPYFILE_DISABLE": "1", "PATH": "/usr/bin:/bin"},  # ohne ._-Dateien von macOS
            )
            r = subprocess.run(
                self.base_args() + [f"mkdir -p {q} && tar -C {q} -xf - --no-same-owner"], stdin=tar.stdout
            )
            tar.wait()
            if r.returncode != 0 or tar.returncode != 0:
                raise RemoteError(f"Hochladen von {local} fehlgeschlagen")
        elif local.is_file():
            with local.open("rb") as f:
                r = subprocess.run(self.base_args() + [f'mkdir -p "$(dirname {q})" && cat > {q}'], stdin=f)
            if r.returncode != 0:
                raise RemoteError(f"Hochladen von {local} fehlgeschlagen")
        else:
            raise RemoteError(f"{local} gibt es nicht")

    def download(self, remote: str, local: Path) -> bool:
        """Ordner `remote`: Inhalt nach `local/`. Datei: nach `local/<name>`. False, wenn es fehlt."""
        q = shlex.quote(remote)
        kind = self.run(
            f"if [ -d {q} ]; then echo d; elif [ -f {q} ]; then echo f; else echo -; fi", capture=True
        ).stdout.strip()
        if kind == "-":
            return False
        local.mkdir(parents=True, exist_ok=True)
        if kind == "d":
            ssh = subprocess.Popen(self.base_args() + [f"tar -C {q} -cf - ."], stdout=subprocess.PIPE)
            r = subprocess.run(["tar", "-C", str(local), "-xf", "-"], stdin=ssh.stdout)
            ssh.wait()
            ok = r.returncode == 0 and ssh.returncode == 0
        else:
            with (local / Path(remote).name).open("wb") as f:
                ok = subprocess.run(self.base_args() + [f"cat {q}"], stdout=f).returncode == 0
        if not ok:
            raise RemoteError(f"Herunterladen von {remote} fehlgeschlagen")
        return True
