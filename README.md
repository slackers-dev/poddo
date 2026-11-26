# pod-do

Schwere Aufgaben vom Laptop auf eine GPU bei [RunPod](https://www.runpod.io) abgeben. Ein Befehl startet
einen Pod, lädt die Eingaben hoch, rechnet, holt das Ergebnis zurück. Ein Wächter auf dem Pod löscht ihn,
wenn er nichts mehr zu tun hat.

```sh
pod-do run lhm ~/Desktop/fotos-beniam      # Pod starten, Fotos hoch, rechnen, avatar.ply zurück
pod-do down                                # Pod sofort weg (sonst nach 20 min Leerlauf)
```

> **Stand 26.11.2026:** Der ganze Ablauf ist gegen einen lokalen Container mit SSH und eine nachgebaute API
> getestet (Einrichten, Archiv auf dem Volume, Wiederherstellen, Hochladen, Rechnen, Holen, Wächter). Gegen
> das echte RunPod noch nicht.

## Wie es funktioniert

- **Umgebung einmal einrichten, danach in Minuten da.** Jedes Projekt beschreibt seine Umgebung: Docker-Image
  und Setup-Skript. Beim ersten Mal läuft das Setup (Pakete, CUDA-Erweiterungen, Modelle) und landet in
  `$POD_DO_ENV`. pod-do packt den Ordner und legt ihn auf ein **Global Volume** von RunPod. Jeder spätere
  Pod holt ihn von dort, in jeder Region und auf jeder GPU.
- **Keine Bindung an ein Rechenzentrum.** Ein Network Volume hängt an einem Rechenzentrum. Ist dort keine
  GPU frei, kommt man nicht an seine Daten. Ein Global Volume geht überall (die ersten 100 GB sind gratis),
  darum kann pod-do einfach die erste freie GPU aus deiner Liste nehmen.
- **Jobs laufen losgelöst von der Verbindung.** Bricht die Verbindung ab oder drückst du Ctrl-C, rechnet der
  Pod weiter. `pod-do attach` hängt sich wieder an und holt das Ergebnis.
- **Wächter gegen vergessene Pods.** Der Pod löscht sich nach `idle_minutes` ohne Job und ohne SSH-Sitzung
  selbst, spätestens nach `max_hours`.
- **Nur Standardbibliothek.** Python ab 3.11, ssh und tar. Spricht die RunPod REST API v2.

## Einrichten (einmal pro Rechner)

```sh
uv tool install git+https://github.com/slackers-dev/pod-do   # oder: uv tool install -e <pfad zum Klon>
pod-do login            # API-Schlüssel (RunPod → Settings → API Keys) in den macOS-Schlüsselbund
pod-do volume --create  # Global Volume "pod-do" anlegen
pod-do gpus --min-vram 8
```

Der API-Schlüssel kommt aus `RUNPOD_API_KEY`, sonst aus dem Schlüsselbund (Eintrag `pod-do`), sonst aus
`~/.config/pod-do/api_key`. Er landet nie in einem Projekt.

SSH: pod-do nimmt `~/.ssh/id_runpod`, `~/.ssh/id_ed25519` oder `~/.ssh/id_rsa` (den ersten, den es gibt) und
gibt den öffentlichen Schlüssel dem Pod mit. In der RunPod-Konsole muss nichts eingetragen werden.

Optionale globale Einstellungen in `~/.config/pod-do/config.toml`:

```toml
ssh_key = "~/.ssh/id_runpod"
volume = "pod-do"           # Name des Global Volume, "" = ohne (dann läuft setup auf jedem Pod)
cloud = "SECURE"            # oder "COMMUNITY" (günstiger, nicht jede Maschine hat eine öffentliche IP)
gpus = ["NVIDIA RTX A4500", "NVIDIA RTX A4000", "NVIDIA A40"]   # Reihenfolge = Vorliebe
idle_minutes = 20
max_hours = 6
```

## In einem Projekt verwenden

`pod-do init` legt eine `pod-do.toml` mit Beispiel an ([`examples/pod-do.toml`](examples/pod-do.toml)).
Aufbau:

```toml
project = "splattwin"       # optional, sonst Ordnername. Pods heissen pod-do-<project>-<env>.

[env.lhm]                   # eine Umgebung = ein Pod-Typ
image = "runpod/pytorch:2.2.0-py3.10-cuda12.1.1-devel-ubuntu22.04"
min_vram = 8                # GB, filtert die GPU-Liste
cuda = "12.1"               # niedrigste CUDA-Version des Hosts (optional)
disk = 80                   # GB Container-Disk
gpus = ["NVIDIA RTX A4500", "NVIDIA A40"]   # optional, sonst die globale Liste
boot = "apt-get update -qq && apt-get install -y -qq ffmpeg libgl1"   # bei jedem Pod-Start
setup_file = "scripts/pod-do/setup-lhm.sh"  # einmal, Ergebnis in $POD_DO_ENV wird gesichert
vars = { PYTHONUNBUFFERED = "1" }           # Umgebungsvariablen auf dem Pod

[task.lhm]                  # eine Aufgabe
env = "lhm"
args = ["fotos", "name?"]   # Argumente von `pod-do run lhm <fotos> [name]`, ? = optional
defaults = { name = "{fotos.name}" }
before = "scripts/pod-do/heic-zu-jpg.sh"    # lokal, vor dem Hochladen (optional)
upload = { "images" = "{fotos}" }           # Job-Ordner/images  <-  lokaler Ordner oder Datei
run = '''
cd "$POD_DO_ENV/LHM-plusplus" && source .venv/bin/activate
python scripts/inference/to_gs_ply.py --images_dir "$JOB/images" --output "$JOB/out/avatar.ply"
'''
download = { "out" = "data/lhm/{name}" }    # Job-Ordner/out  ->  lokaler Ordner
down = false                # Pod nach der Aufgabe sofort löschen?
```

Statt `setup`, `boot` und `run` als Text geht jeweils `setup_file`, `boot_file`, `run_file` (Pfad relativ zum
Projekt).

**Hochladen:** Ein lokaler Ordner landet mit seinem Inhalt in `Job-Ordner/<ziel>/`, eine Datei genau als
`Job-Ordner/<ziel>`. **Herunterladen:** Ein Ordner auf dem Pod landet mit seinem Inhalt im lokalen Ordner,
eine Datei darin unter ihrem Namen.

**Platzhalter** `{name}` gibt es in `upload`, `download` und `defaults`. Für Pfade gehen auch `{name.name}`,
`{name.stem}`, `{name.suffix}`, `{name.parent}`. Lokale Pfade in den Argumenten werden absolut.

**Auf dem Pod** stehen in `run` (und in `before`/`after` lokal) die Argumente als Umgebungsvariablen in
Grossbuchstaben bereit (`$FOTOS`, `$NAME`). Dazu kommen:

| Variable | Inhalt |
|---|---|
| `$JOB` | Job-Ordner, auch das Arbeitsverzeichnis von `run` |
| `$POD_DO_ENV` | eingerichtete Umgebung (`/opt/pod-do/envs/<env>`) |
| `$POD_DO_CACHE` | Ordner auf dem Volume, überlebt alles. Ohne Dateisperren, also nicht für Download-Caches |
| `$HF_HOME` | lokal auf dem Pod (`/opt/pod-do/cache/huggingface`), wenn nicht anders gesetzt |

**Was ins Setup gehört:** alles, was lange dauert und sich selten ändert, auch Modelle. Es muss nach `$POD_DO_ENV`
installieren (das ist auch das Arbeitsverzeichnis). Eine venv mit `uv venv` dort anlegen; pod-do setzt
`UV_PYTHON_INSTALL_DIR`, damit auch Python selbst in `$POD_DO_ENV` liegt und das Archiv auf jedem Pod
funktioniert. `uv` ist auf dem Pod schon da. Systempakete (apt) gehören in `boot`, die landen nicht im Archiv.

**Neu einrichten:** Ändert sich `image` oder `setup`, richtet pod-do automatisch neu ein und ersetzt das
Archiv.

## Befehle

| Befehl | Was |
|---|---|
| `pod-do run <aufgabe> [args]` | Pod suchen oder starten, Umgebung herstellen, hochladen, rechnen, holen. `--down` löscht danach, `--gpu <id>` wählt die Karte |
| `pod-do up [env]` | nur Pod starten und bereitmachen |
| `pod-do attach [job]` | wieder an einen Job hängen (Standard: der letzte), dann Ergebnis holen |
| `pod-do ssh [env]` | Shell auf dem Pod |
| `pod-do status` | Guthaben, laufende pod-do-Pods, Volume |
| `pod-do down [env]` / `--all` | Pods dieses Projekts löschen, oder alle pod-do-Pods |
| `pod-do gpus [--min-vram N]` | GPUs mit Preis und Verfügbarkeit, die IDs für `gpus = [...]` |
| `pod-do check` | Konfiguration und GPU-Wahl prüfen, ohne etwas zu starten |
| `pod-do volume [--create]` | Global Volume anzeigen oder anlegen |

## Kosten

- GPU: nur solange der Pod lebt, sekundengenau. Kleine Karten (RTX A4500, A4000) kosten etwa $0.25/h.
- Container-Disk: Rappenbeträge, nur solange der Pod lebt.
- Global Volume: die ersten 100 GB gratis, danach $0.09/GB im Monat, dazu kleine Gebühren pro Zugriff.
  Wird das Guthaben 0, löscht RunPod das Volume nach 15 Tagen.

## Auf dem Pod

Alles von pod-do liegt unter `/opt/pod-do`: `envs/`, `jobs/<job>/` (run.sh, log, exitcode), `watchdog.log`.
Das Volume ist unter `/vol` eingehängt, die Archive liegen in `/vol/pod-do/envs/`.

## Grenzen

- Ein Pod pro Projekt und Umgebung, eine GPU pro Pod.
- Global Volumes kennen keine Dateirechte und kein atomares Umbenennen. Darum liegt die Umgebung als Archiv
  dort und wird auf die lokale Disk des Pods entpackt.
- Der Wächter löscht den Pod über die API mit dem Schlüssel, den RunPod dem Pod mitgibt. Geht das nicht,
  beendet er den Container. Dann kostet die GPU nichts mehr, der Pod steht aber noch als EXITED in der Liste.
  `pod-do status` zeigt das, `pod-do down` räumt auf.

## Entwickeln

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Lizenz: MIT
