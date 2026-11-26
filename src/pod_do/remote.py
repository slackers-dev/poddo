"""SSH und Dateitransfer zum Pod. Nur ssh und tar, beides ist auf Mac und Pod vorhanden."""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path

from .api import Ssh


class RemoteError(Exception):
    pass


class Remote:
    def __init__(self, ssh: Ssh, key: Path | None):
        self.ssh = ssh
        self.key = key

    def base_args(self, tty: bool = False) -> list[str]:
        a = [
            "ssh",
            "-p",
            str(self.ssh.port),
            # Jeder Pod hat einen neuen Host-Schluessel: nicht speichern, nicht fragen
            "-o",
            "StrictHostKeyChecking=no",
            "-o",
            "UserKnownHostsFile=/dev/null",
            "-o",
            "LogLevel=ERROR",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ConnectTimeout=10",
        ]
        if self.key:
            a += ["-i", str(self.key), "-o", "IdentitiesOnly=yes"]
        a.append("-t" if tty else "-T")
        a.append(f"{self.ssh.user}@{self.ssh.host}")
        return a

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
        try:
            r = subprocess.run(self.base_args() + ["true"], capture_output=True, timeout=25)
        except subprocess.TimeoutExpired:
            return False
        return r.returncode == 0

    def interactive(self, cd: str | None = None) -> int:
        cmd = self.base_args(tty=True)
        if cd:
            cmd.append(f"cd {shlex.quote(cd)} && exec bash -l")
        return subprocess.call(cmd)

    def put_text(self, text: str, remote_path: str, mode: str = "644") -> None:
        q = shlex.quote(remote_path)
        cmd = self.base_args() + [f'mkdir -p "$(dirname {q})" && cat > {q} && chmod {mode} {q}']
        r = subprocess.run(cmd, input=text.encode())
        if r.returncode != 0:
            raise RemoteError(f"Konnte {remote_path} nicht schreiben")

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
