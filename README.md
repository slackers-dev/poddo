<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <img src="assets/logo.svg" alt="poddo" height="64">
  </picture>
</p>

<h1 align="center">poddo</h1>

<p align="center">
  <b>pod, do:</b> schwere Jobs vom Laptop auf eine GPU bei <a href="https://www.runpod.io">RunPod</a>.<br>
  Ein Befehl startet den Pod, rechnet, holt das Ergebnis und räumt hinter sich auf.
</p>

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-3776ab">
  <img alt="Nur Standardbibliothek" src="https://img.shields.io/badge/dependencies-none-2f6bff">
  <img alt="Lizenz MIT" src="https://img.shields.io/badge/license-MIT-151515">
</p>

```sh
poddo run train ./data --detach   # Pod starten, Daten hoch, trainieren, Laptop darf zu
poddo jobs                        # Stand und Fortschritt
poddo attach                      # Ergebnis holen, auch wenn der Pod schon weg ist
```

## Warum

Ein Modell trainieren, das auf dem Laptop drei Tage bräuchte? Mit poddo gibst du es an eine gemietete GPU ab
und arbeitest weiter.

- **Einrichten nur einmal.** Die Umgebung (Pakete, CUDA-Erweiterungen, Modelle) wird einmal gebaut und auf
  einem Volume gesichert. Jeder spätere Pod hat sie in Minuten, auf jeder GPU, in jeder Region.
- **Der Laptop darf zu.** Jobs laufen auf dem Pod weiter. Ergebnisse landen auf dem Volume und warten dort,
  bis du sie abholst.
- **Keine vergessenen Pods.** Ein Wächter löscht den Pod, sobald er nichts mehr zu tun hat. Du zahlst nur die
  Minuten, in denen er gearbeitet hat.
- **Klein.** Ein Python-Paket ohne Abhängigkeiten, dazu ssh und tar.

> **Auf RunPod getestet (26.11.2026).** Beispiel 3D-Avatar aus Fotos (LHM++): Setup einmal etwa 25 Minuten,
> Archiv 14 GB auf dem Volume. Danach braucht `poddo run` auf einem frischen Pod gut 5 Minuten bis zum
> Ergebnis (davon etwa 1 Minute rechnen) und kostet auf einer L4 rund 6 Rappen.

## Schnellstart

**Du brauchst:** ein [RunPod-Konto](https://www.runpod.io) mit etwas Guthaben, [uv](https://docs.astral.sh/uv/),
und einen SSH-Schlüssel (`ssh-keygen -t ed25519`, falls du noch keinen hast).

```sh
# 1. Installieren
uv tool install git+https://github.com/slackers-dev/pod-do

# 2. Einmal pro Rechner
poddo login              # API-Schlüssel (RunPod → Settings → API Keys), landet im macOS-Schlüsselbund
poddo volume --create    # Global Volume "poddo" anlegen (die ersten 100 GB sind gratis)
poddo gpus --min-vram 8  # welche GPUs gibt es, was kosten sie

# 3. In deinem Projekt
poddo init               # legt poddo.toml mit Beispiel an
poddo check              # Konfiguration prüfen, ohne etwas zu starten
poddo run <aufgabe>      # los
```

Fertig? `poddo status` zeigt, ob noch Pods laufen. `poddo down` löscht sie sofort; sonst macht es der Wächter
nach 20 Minuten Leerlauf.

## Wie es funktioniert

- **Umgebung einmal einrichten, danach in Minuten da.** Jedes Projekt beschreibt seine Umgebung: Docker-Image
  und Setup-Skript. Beim ersten Mal läuft das Setup und landet in `$PODDO_ENV`. poddo packt den Ordner und
  legt ihn auf ein **Global Volume** von RunPod. Jeder spätere Pod holt ihn von dort.
- **Keine Bindung an ein Rechenzentrum.** Ein Network Volume hängt an einem Rechenzentrum. Ist dort keine
  GPU frei, kommt man nicht an seine Daten. Ein Global Volume geht überall, darum kann poddo einfach die
  erste freie GPU aus deiner Liste nehmen.
- **Jobs laufen losgelöst von der Verbindung.** Bricht die Verbindung ab, drückst du Ctrl-C oder klappst du
  den Laptop zu, rechnet der Pod weiter. `poddo attach` hängt sich wieder an und holt das Ergebnis.
  `poddo run --detach` startet nur und kehrt sofort zurück.
- **Wächter gegen vergessene Pods.** Der Pod löscht sich nach `idle_minutes` ohne Job und ohne SSH-Sitzung
  selbst, spätestens nach `max_hours`. Kommt ein eben angelegter Pod nicht bis zum Wächter (kein SSH,
  Ctrl-C, Fehler), löscht poddo ihn sofort wieder. Das Anlegen wiederholt poddo bei Server- oder
  Netzfehlern nie, damit nicht unbemerkt ein zweiter Pod entsteht.
- **Eine SSH-Verbindung pro Pod.** Alle Befehle laufen über eine gemeinsame Verbindung (ControlMaster,
  hält 2 Minuten nach dem letzten Befehl), die Skripte gehen in einem einzigen tar hinüber. Der
  Host-Schlüssel wird beim ersten Kontakt pro Pod gemerkt (`~/.local/state/poddo/known_hosts`) und danach
  geprüft.
- **Optional als Docker-Image.** Die fertige Umgebung kann auch als Image in einer Registry liegen,
  gebaut direkt auf dem Pod (siehe unten).

## Einstellungen

Der API-Schlüssel kommt aus der Umgebungsvariable `RUNPOD_API_KEY` oder `POD_API_KEY`, sonst aus der
nächsten `.env` (im aktuellen Ordner oder darüber, gleiche Namen), sonst aus dem Schlüsselbund (Eintrag
`poddo`), sonst aus `~/.config/poddo/api_key`. Eine `.env` gehört in `.gitignore`.

SSH: poddo nimmt `~/.ssh/id_runpod`, `~/.ssh/id_ed25519` oder `~/.ssh/id_rsa` (den ersten, den es gibt) und
gibt den öffentlichen Schlüssel dem Pod mit. In der RunPod-Konsole muss nichts eingetragen werden.

Optionale globale Einstellungen in `~/.config/poddo/config.toml`:

```toml
ssh_key = "~/.ssh/id_runpod"
volume = "poddo"            # Name des Global Volume, "" = ohne (dann läuft setup auf jedem Pod)
cloud = "SECURE"            # oder "COMMUNITY" (günstiger, nicht jede Maschine hat eine öffentliche IP)
gpus = ["NVIDIA RTX A4500", "NVIDIA RTX A4000", "NVIDIA A40"]   # Reihenfolge = Vorliebe
idle_minutes = 20
wait_minutes = 10           # ist keine GPU frei: so lange alle 30 s nochmals versuchen
max_hours = 6
keep_results_minutes = 60   # Ergebnisse nicht abgeholt und nicht auf dem Volume: so lange wartet der Pod
keep_results_days = 30      # nie abgeholte Ergebnisse auf dem Volume nach so vielen Tagen löschen (0 = nie)
```

## In einem Projekt verwenden

`poddo init` legt eine `poddo.toml` mit Beispiel an ([`examples/poddo.toml`](examples/poddo.toml)).
Aufbau:

```toml
project = "splattwin"       # optional, sonst Ordnername. Pods heissen poddo-<project>-<env>.

[env.lhm]                   # eine Umgebung = ein Pod-Typ
image = "runpod/pytorch:2.2.0-py3.10-cuda12.1.1-devel-ubuntu22.04"
min_vram = 8                # GB, filtert die GPU-Liste
cuda = "12.1"               # niedrigste CUDA-Version des Hosts (optional)
disk = 80                   # GB Container-Disk
gpus = ["NVIDIA RTX A4500", "NVIDIA A40"]   # optional, sonst die globale Liste
boot = "apt-get update -qq && apt-get install -y -qq ffmpeg libgl1"   # bei jedem Pod-Start
setup_file = "scripts/poddo/setup-lhm.sh"  # einmal, Ergebnis in $PODDO_ENV wird gesichert
vars = { PYTHONUNBUFFERED = "1" }           # Umgebungsvariablen auf dem Pod

[task.lhm]                  # eine Aufgabe
env = "lhm"
args = ["fotos", "name?"]   # Argumente von `poddo run lhm <fotos> [name]`, ? = optional
defaults = { name = "{fotos.name}" }
before = "scripts/poddo/heic-zu-jpg.sh"    # lokal, vor dem Hochladen (optional)
upload = { "images" = "{fotos}" }           # Job-Ordner/images  <-  lokaler Ordner oder Datei
run = '''
cd "$PODDO_ENV/LHM-plusplus" && source .venv/bin/activate
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
| `$PODDO_ENV` | eingerichtete Umgebung (`/opt/poddo/envs/<env>`) |
| `$PODDO_CACHE` | Ordner auf dem Volume, überlebt alles. Ohne Dateisperren, also nicht für Download-Caches |
| `$HF_HOME`, `$TORCH_HOME` | in `$PODDO_ENV/.cache/`, damit Modelle aus dem Setup ins Archiv kommen |

**Was ins Setup gehört:** alles, was lange dauert und sich selten ändert, auch Modelle. Es muss nach `$PODDO_ENV`
installieren (das ist auch das Arbeitsverzeichnis). Eine venv mit `uv venv` dort anlegen; poddo setzt
`UV_PYTHON_INSTALL_DIR`, damit auch Python selbst in `$PODDO_ENV` liegt und das Archiv auf jedem Pod
funktioniert. `uv` ist auf dem Pod schon da und legt `.pyc`-Dateien gleich beim Installieren an
(`UV_COMPILE_BYTECODE`), damit der erste `import` auf jedem Pod schneller ist. Systempakete (apt) gehören in
`boot`, die landen nicht im Archiv. `boot` läuft im Hintergrund, während die Umgebung vom Volume kommt
(Log: `/opt/poddo/boot.log`), darf sich also nicht auf `$PODDO_ENV` verlassen.

Namen von Umgebungen und Aufgaben: nur Buchstaben, Ziffern, `_`, `.` und `-`.

**Neu einrichten:** Ändert sich `image` oder `setup`, richtet poddo automatisch neu ein und ersetzt das
Archiv. Achtung: Dazu zählt jede Änderung am Setup-Skript, auch an Kommentaren.

## Umgebung als Docker-Image (optional)

Statt als Archiv auf dem Volume kann die eingerichtete Umgebung als Docker-Image in einer Registry liegen
(z. B. ghcr.io). Dann startet jeder neue Pod direkt aus dem fertigen Image: kein Setup, kein Archiv, kein
Volume nötig, und das Image läuft auch anderswo.

```toml
[env.lhm]
image = "runpod/pytorch:2.2.0-py3.10-cuda12.1.1-devel-ubuntu22.04"   # Basis
docker = "ghcr.io/slackers-dev/splattwin-lhm"                       # fertige Umgebung, Tag = Setup-Hash
```

**So entsteht es:** Gibt es `<docker>:<setup-hash>` noch nicht, richtet poddo den Pod wie gewohnt ein
(Archiv vom Volume oder Setup). Danach packt der Pod `/opt/poddo/envs/<env>` in Layer von etwa 4 GB
(`pigz`) und hängt sie mit [crane](https://github.com/google/go-containerregistry) an das Basis-Image,
ohne Docker-Daemon (der fehlt in RunPod-Pods). Ändert sich `image` oder `setup`, gibt es einen neuen Tag.
Von Hand: `poddo image <env>`, nur prüfen: `poddo image <env> --check`.

**Einmal einrichten (Beispiel ghcr.io):**

1. Auf GitHub einen Token (classic) mit nur `write:packages` und `read:packages` anlegen.
2. `poddo registry login ghcr.io --user <github-name>`: Token in den Schlüsselbund, Benutzer in die Konfig.
   Alternativ `PODDO_REGISTRY_TOKEN` in der Umgebung oder in `.env`.
3. `poddo registry runpod`: dieselben Zugangsdaten bei RunPod hinterlegen (Name `poddo`, änderbar mit
   `runpod_registry`), damit Pods das private Image ziehen können. Braucht einen API-Schlüssel mit dem Recht
   «Secrets». Oder in der RunPod-Konsole unter *Settings → Container Registry Auth* von Hand.

Der Token geht beim Hochladen nur über SSH-stdin an den Pod, er steht nie in dessen Umgebung, und nach dem
Hochladen meldet sich crane wieder ab. Fehlt der
Zugang, arbeitet poddo mit einem Hinweis wie ohne `docker` weiter.

**Was nicht ins Image kommt:** Systempakete aus `boot` (die laufen weiter bei jedem Start) und alles
ausserhalb von `$PODDO_ENV`. Images mit Modellen, die nur für Forschung freigegeben sind, privat halten.

## Befehle

| Befehl | Was |
|---|---|
| `poddo run <aufgabe> [args]` | Pod suchen oder starten, Umgebung herstellen, hochladen, rechnen, holen. `--detach` startet nur, `--down` löscht danach, `--gpu <id>` wählt die Karte, `--wait <min>` wartet auf eine freie |
| `poddo up [env]` | nur Pod starten und bereitmachen |
| `poddo attach [job]` | wieder an einen Job hängen (Standard: der letzte), dann Ergebnis holen |
| `poddo jobs [--watch]` | letzte Jobs mit Stand und Fortschritt |
| `poddo logs [job] [-f]` | Log eines Jobs, mit `-f` mitlesen |
| `poddo ssh [env]` | Shell auf dem Pod |
| `poddo status` | Guthaben, laufende poddo-Pods, Volume |
| `poddo down [env]` / `--all` | Pods dieses Projekts löschen, oder alle poddo-Pods |
| `poddo gpus [--min-vram N]` | GPUs mit Preis und Verfügbarkeit, die IDs für `gpus = [...]` |
| `poddo check` | Konfiguration und GPU-Wahl prüfen, ohne etwas zu starten |
| `poddo volume [--create]` | Global Volume anzeigen oder anlegen |
| `poddo image [env] [--check]` | Umgebung als Docker-Image bauen und hochladen (`docker = ...`) |
| `poddo registry login\|runpod [host] [--user]` | Zugang zur Registry lokal oder bei RunPod hinterlegen |

## Ergebnisse abholen

Am Ende jedes Jobs sichert der Pod die Pfade aus `download` (dazu `log` und `exitcode`) auf dem **Global
Volume**, unter `/vol/poddo/results/<projekt>/<job>.tar.zst`. Erst danach gilt der Job als fertig. So
überleben die Ergebnisse den Pod, auch wenn der Laptop aus ist und der Wächter den Pod löscht.

- **`poddo attach [job]`** holt die Ergebnisse: vom Pod, wenn er noch läuft, sonst vom Volume. Dafür nimmt
  poddo einen laufenden Pod des Projekts oder startet kurz einen (ohne Umgebung herzustellen, etwa eine Minute,
  ungefähr 1 Rappen) und löscht ihn danach wieder.
- **`poddo run`** holt nebenbei alle Ergebnisse früherer Jobs, die noch auf dem Volume liegen, gratis, weil
  der Pod sowieso läuft.
- Nach dem Herunterladen löscht poddo die Kopie auf dem Volume. Nie abgeholte Kopien löscht der Pod nach
  `keep_results_days` (Standard 30) Tagen.
- Ist ein Job gesichert, löscht der Wächter den Pod nach der normalen Leerlaufzeit (`idle_minutes`). Ohne
  Volume oder wenn das Sichern nicht ging, wartet er `keep_results_minutes` (Standard 60), weil die Ergebnisse
  dann nur auf dem Pod liegen. Das Warten kostet GPU-Zeit.
- Verloren sind Ergebnisse nur, wenn der Job nicht fertig wurde, bevor der Pod gelöscht wurde (etwa nach
  `max_hours`), oder ohne Volume.
- Koppelst du dich ab (Ctrl-C, `--detach`, Verbindung weg), zeigt poddo gut sichtbar, wie du die Ergebnisse
  holst. `poddo status` listet alle Jobs, deren Ergebnisse noch fehlen.
- Auf dem Mac kommt zusätzlich eine Mitteilung, wenn poddo die Ergebnisse geholt hat.

## Stand und Fortschritt

```sh
poddo jobs            # letzte Jobs: läuft (mit Fortschrittsbalken), fertig, abgeholt oder verloren
poddo jobs --watch    # alle 5 s neu
poddo logs -f         # Log des letzten Jobs mitlesen, bis er fertig ist
```

Den Fortschritt liest poddo aus einer [tqdm](https://github.com/tqdm/tqdm)-Zeile im Log, ohne dass du etwas
tun musst. Genauer geht es aus dem eigenen Skript mit `poddo-progress <erledigt> <total> [text]`, z. B.
`poddo-progress 7 12 "Bild 7"`.

## Kosten

- GPU: nur solange der Pod lebt, sekundengenau. Kleine Karten (RTX A4500, A4000) kosten etwa $0.25/h.
- Container-Disk: Rappenbeträge, nur solange der Pod lebt.
- Global Volume: die ersten 100 GB gratis, danach $0.09/GB im Monat, dazu kleine Gebühren pro Zugriff.
  Wird das Guthaben 0, löscht RunPod das Volume nach 15 Tagen.

## Auf dem Pod

Alles von poddo liegt unter `/opt/poddo`: `envs/`, `jobs/<job>/` (run.sh, log, exitcode, started,
finished, progress, saved, fetched), `boot.log`,
`watchdog.log`.
Das Volume ist unter `/vol` eingehängt, die Archive der Umgebungen liegen in `/vol/poddo/envs/`, gesicherte
Ergebnisse in `/vol/poddo/results/`.

## Grenzen

- Ein Pod pro Projekt und Umgebung, eine GPU pro Pod.
- Global Volumes kennen keine Dateirechte und kein atomares Umbenennen. Darum liegt die Umgebung als Archiv
  dort und wird auf die lokale Disk des Pods entpackt.
- Der Wächter löscht den Pod über die API mit dem Schlüssel, den RunPod dem Pod mitgibt. Geht das nicht,
  beendet er den Container. Dann kostet die GPU nichts mehr, der Pod steht aber noch als EXITED in der Liste.
  `poddo status` zeigt das, `poddo down` räumt auf.

## Umstieg von pod-do

Früher hiess das Tool `pod-do`. Alte Namen gehen weiter: `pod-do.toml` im Projekt, das Volume `pod-do`,
laufende `pod-do-…`-Pods, Umgebungen und Ergebnisse darauf, `$POD_DO_ENV` in Skripten. Der Ordner
`~/.local/state/pod-do` wird beim ersten Aufruf umbenannt. Den alten Befehl entfernen:
`uv tool uninstall pod-do`.

## Entwickeln

```sh
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Lizenz: MIT
