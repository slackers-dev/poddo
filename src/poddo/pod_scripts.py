"""Shell-Skripte, die poddo auf den Pod kopiert. Alles liegt dort unter /opt/poddo."""

ROOT = "/opt/poddo"

# Bis zur Umbenennung hiess alles pod-do. Fertige Umgebungen (Archive, Docker-Images) enthalten noch
# absolute Pfade unter /opt/pod-do, alte Pods haben dort ihre Jobs. Beide Namen zeigen auf denselben Ordner.
LEGACY_LINK = (
    "if [ ! -e /opt/poddo ]; then"
    " if [ -d /opt/pod-do ] && [ ! -L /opt/pod-do ]; then ln -s /opt/pod-do /opt/poddo;"
    " else mkdir -p /opt/poddo; [ -e /opt/pod-do ] || ln -s /opt/poddo /opt/pod-do; fi; fi"
)

# Umgebung fuer setup, boot und jede Aufgabe. Platzhalter werden in pod.py ersetzt.
ENV_SH = r"""# von poddo erzeugt
# SSH-Sitzungen erben die ENV des Images nicht (PATH mit /usr/local/cuda/bin, CUDA_*, ...).
# Darum die Umgebung des Hauptprozesses uebernehmen, ohne sitzungsbezogene und geheime Werte.
if [ -r /proc/1/environ ]; then
  while IFS= read -r -d '' kv; do
    k="${kv%%=*}"
    case "$k" in
      HOME|PWD|OLDPWD|SHLVL|_|TERM|HOSTNAME|USER|LOGNAME|SHELL|PUBLIC_KEY|JUPYTER_PASSWORD|RUNPOD_API_KEY) continue ;;
    esac
    [[ "$k" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && export "$kv"
  done < /proc/1/environ
fi
export PODDO_ROOT=/opt/poddo
export PODDO_ENV=/opt/poddo/envs/@ENV@
export PODDO_VOLUME=@VOLUME@
if [ -n "$PODDO_VOLUME" ]; then export PODDO_CACHE="$PODDO_VOLUME/poddo/cache"; else export PODDO_CACHE=/opt/poddo/cache; fi
# alte Namen (pod-do), damit bestehende setup-Skripte und Aufgaben weiterlaufen
export POD_DO_ROOT="$PODDO_ROOT" POD_DO_ENV="$PODDO_ENV" POD_DO_VOLUME="$PODDO_VOLUME" POD_DO_CACHE="$PODDO_CACHE"
export PATH="/opt/poddo/bin:$PATH"
# Python von uv in die Umgebung, damit es im Archiv mitkommt und die venv danach noch stimmt
export UV_PYTHON_INSTALL_DIR="$PODDO_ENV/.python"
export UV_CACHE_DIR=/opt/poddo/cache/uv
export UV_LINK_MODE=copy
# .pyc gleich beim Installieren erzeugen: landen so im Archiv, der erste import auf jedem Pod ist schneller
export UV_COMPILE_BYTECODE=1
# Download-Caches in die Umgebung: so kommen Modelle, die erst beim ersten Lauf geladen werden, ins
# Archiv. Nicht aufs Volume: Global Volumes koennen keine Dateien sperren, huggingface_hub braucht das.
export HF_HOME="${HF_HOME:-$PODDO_ENV/.cache/huggingface}"
export TORCH_HOME="${TORCH_HOME:-$PODDO_ENV/.cache/torch}"
mkdir -p "$PODDO_ENV" "$PODDO_CACHE" "$UV_CACHE_DIR" 2>/dev/null || true
@VARS@
"""

# Richtet die Umgebung ein: Werkzeuge, boot, dann Archiv vom Volume holen oder setup laufen lassen.
BOOTSTRAP_SH = r"""#!/usr/bin/env bash
set -eo pipefail
source /opt/poddo/env.sh
NAME=@ENV@
HASH=@HASH@
ENVS=/opt/poddo/envs
SNAPDIR="${PODDO_VOLUME:+$PODDO_VOLUME/poddo/envs}"
SNAP="${SNAPDIR:+$SNAPDIR/$NAME-$HASH.tar.zst}"
say() { printf '\n[poddo] %s\n' "$*"; }
# Stand aus der Zeit vor der Umbenennung (pod-do): Archiv und Markierungen weiter nehmen
OLD="${PODDO_VOLUME:+$PODDO_VOLUME/pod-do/envs/$NAME-$HASH.tar.zst}"
if [ -n "$OLD" ] && [ ! -f "$SNAP.ok" ] && [ -f "$OLD.ok" ] && [ -f "$OLD" ]; then SNAP="$OLD"; fi
for m in ok image; do
  [ -f "$ENVS/$NAME/.pod-do-$m" ] && [ ! -f "$ENVS/$NAME/.poddo-$m" ] && cp "$ENVS/$NAME/.pod-do-$m" "$ENVS/$NAME/.poddo-$m"
done
snapshot() {
  [ -n "$SNAP" ] || return 0
  # Umgebung kommt als Docker-Image (env.docker): kein Archiv auf dem Volume noetig
  [ "@SNAPSHOT@" = 1 ] || return 0
  [ -f "$ENVS/$NAME/.poddo-image" ] && return 0
  [ -f "$SNAP.ok" ] && [ -f "$SNAP" ] && return 0
  say "Umgebung auf dem Volume sichern ($(du -sh "$ENVS/$NAME" | cut -f1), dauert etwas)"
  mkdir -p "$SNAPDIR"
  # Global Volumes kennen kein atomares Umbenennen: erst schreiben, dann .ok-Datei als Zeichen
  rm -f "$SNAP.ok"
  tar -C "$ENVS" -cf - "$NAME" | zstd -q -T0 -3 -o "$SNAP" -f
  echo ok > "$SNAP.ok"
  # alte Staende derselben Umgebung weg
  for f in "$SNAPDIR/$NAME"-*.tar.zst; do
    [ "$f" = "$SNAP" ] || rm -f "$f" "$f.ok"
  done
  say "gesichert: $SNAP ($(du -h "$SNAP" | cut -f1))"
}

say "Werkzeuge"
need=()
for t in curl tar zstd; do command -v $t >/dev/null || need+=($t); done
if [ ${#need[@]} -gt 0 ]; then
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}" >/dev/null
fi
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/opt/poddo/bin INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null
fi

# boot (apt-get & Co.) laeuft im Hintergrund, waehrend die Umgebung vom Volume kommt. Beides ist
# unabhaengig: boot landet nie in $PODDO_ENV. Vor setup und vor dem Ende wird auf boot gewartet.
BOOT_PID=""
if [ -s /opt/poddo/boot.sh ] && [ ! -f /opt/poddo/.boot-ok ]; then
  say "boot (im Hintergrund, Log: /opt/poddo/boot.log)"
  (bash -eo pipefail /opt/poddo/boot.sh > /opt/poddo/boot.log 2>&1 && touch /opt/poddo/.boot-ok) &
  BOOT_PID=$!
fi
wait_boot() {
  [ -n "$BOOT_PID" ] || return 0
  if ! wait "$BOOT_PID"; then
    tail -n 40 /opt/poddo/boot.log
    say "boot fehlgeschlagen (ganzes Log: /opt/poddo/boot.log)"
    exit 1
  fi
  BOOT_PID=""
  say "boot fertig"
}

if [ -f "$ENVS/$NAME/.poddo-ok" ] && [ "$(cat "$ENVS/$NAME/.poddo-ok")" = "$HASH" ]; then
  snapshot
  wait_boot
  say "Umgebung $NAME ist bereit"
  exit 0
fi

if [ -n "$SNAP" ] && [ -f "$SNAP.ok" ] && [ -f "$SNAP" ]; then
  say "Umgebung $NAME vom Volume holen ($(du -h "$SNAP" | cut -f1))"
  T0=$SECONDS
  rm -rf "${ENVS:?}/$NAME" && mkdir -p "$ENVS"
  zstd -dc -T0 "$SNAP" | tar -C "$ENVS" -xf -
  echo "$HASH" > "$ENVS/$NAME/.poddo-ok"
  say "geholt in $((SECONDS - T0)) s"
  wait_boot
  say "Umgebung $NAME ist bereit"
  exit 0
fi

wait_boot
say "setup fuer $NAME (einmalig, danach liegt es auf dem Volume)"
rm -rf "${ENVS:?}/$NAME" && mkdir -p "$ENVS/$NAME"
source /opt/poddo/env.sh
cd "$PODDO_ENV"
bash -eo pipefail /opt/poddo/setup.sh
echo "$HASH" > "$ENVS/$NAME/.poddo-ok"

if [ -n "$SNAP" ]; then
  snapshot
else
  say "ohne Volume: setup laeuft auf jedem neuen Pod wieder"
fi
say "Umgebung $NAME ist bereit"
"""

# Startet ein Skript losgeloest von der SSH-Verbindung. Arg 1: Job-Ordner (mit run.sh).
# Im Job-Ordner: started/finished (Unix-Zeit), exitcode, running solange er laeuft, fetched sobald poddo
# die Ergebnisse geholt hat, progress (von poddo-progress), log.
START_SH = r"""#!/usr/bin/env bash
set -e
JOB="$1"
cd "$JOB"
rm -f exitcode finished fetched saved progress
date +%s > started
touch running
# Erst sichern (save.sh), dann gilt der Job als fertig: so holt niemand halb gesicherte Ergebnisse ab
setsid nohup bash -c 'bash run.sh > log 2>&1; echo $? > exitcode; date +%s > finished
  [ -f save.conf ] && bash /opt/poddo/save.sh >> save.log 2>&1; rm -f running' >/dev/null 2>&1 < /dev/null &
echo $! > pid
"""

# Sichert die Ergebnisse eines fertigen Jobs auf dem Global Volume, damit sie den Pod ueberleben. Laeuft im
# Job-Ordner. save.conf (von poddo): DEST=<archiv>, DAYS=<alte Archive nach so vielen Tagen loeschen>.
# save.list: die Pfade aus `download`, eine Zeile pro Pfad. Dazu kommen immer log und exitcode.
SAVE_SH = r"""#!/usr/bin/env bash
set -o pipefail
source ./save.conf
VOL=/vol
# Ohne eingehaengtes Volume wuerde das Archiv auf der Pod-Disk landen und mit dem Pod verschwinden
if ! grep -qs " $VOL " /proc/mounts; then echo "kein Volume unter $VOL, nicht gesichert"; exit 0; fi
command -v zstd >/dev/null || { echo "zstd fehlt, nicht gesichert"; exit 0; }
paths=(log exitcode)
while IFS= read -r p; do [ -n "$p" ] && [ -e "$p" ] && paths+=("$p"); done < save.list
mkdir -p "$(dirname "$DEST")"
# Global Volumes kennen kein atomares Umbenennen: erst schreiben, dann .ok als Zeichen
rm -f "$DEST.ok"
if tar -cf - "${paths[@]}" | zstd -q -T0 -3 -o "$DEST" -f; then
  echo ok > "$DEST.ok"
  touch saved
  echo "gesichert: $DEST ($(du -h "$DEST" | cut -f1))"
else
  rm -f "$DEST"
  echo "sichern fehlgeschlagen"
fi
# Nie abgeholte Ergebnisse nach DAYS Tagen weg, damit das Volume nicht volllaeuft
if [ "${DAYS:-0}" -gt 0 ]; then
  find "$VOL/poddo/results" -type f -name '*.tar.zst*' -mtime "+$DAYS" -delete 2>/dev/null
fi
true
"""

# Holt gesicherte Ergebnisse vom Volume zurueck in den Job-Ordner. Arg 1: Archiv, Arg 2: Job-Ordner.
# Wird direkt mitgeschickt (bash -c), geht also auch auf Pods, die noch die alten Skripte haben.
RESTORE_RESULTS_SH = r"""#!/usr/bin/env bash
set -eo pipefail
SRC="$1"; JOB="$2"
[ -f "$SRC.ok" ] && [ -f "$SRC" ] || exit 3
command -v zstd >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq zstd >/dev/null; }
mkdir -p "$JOB"
zstd -dc "$SRC" | tar -C "$JOB" -xf -
touch "$JOB/saved"
"""

# Fortschritt aus dem eigenen Skript melden: poddo-progress <erledigt> <total> [text]
PROGRESS_SH = r"""#!/usr/bin/env bash
# poddo-progress <erledigt> <total> [text]: Fortschritt fuer poddo jobs und poddo attach
[ -n "$JOB" ] || { echo "poddo-progress: nur in einer Aufgabe von poddo" >&2; exit 0; }
printf '%s %s %s\n' "${1:-0}" "${2:-0}" "${*:3}" > "$JOB/progress.tmp" && mv -f "$JOB/progress.tmp" "$JOB/progress"
"""

# Stand der Jobs, eine Zeile pro Job, Felder mit Tab getrennt. Argumente: Jobnamen (sonst alle ausser _*).
# job, state (running|saving|done|missing), exitcode, started, finished, fetched (0/1), progress,
# letzte Logzeile, saved (0/1: Ergebnisse auf dem Volume).
# Zum Schluss "@now <Unix-Zeit>", damit die Uhr des Laptops keine Rolle spielt.
STATUS_SH = r"""#!/usr/bin/env bash
cd /opt/poddo/jobs 2>/dev/null || { echo "@now $(date +%s)"; exit 0; }
if [ $# -eq 0 ]; then set -- $(ls -1 | grep -v '^_'); fi
for j in "$@"; do
  if [ ! -d "$j" ]; then printf '%s\tmissing\t\t\t\t0\t\t\t0\n' "$j"; continue; fi
  if [ -e "$j/running" ] && [ -f "$j/exitcode" ]; then st=saving
  elif [ -e "$j/running" ]; then st=running
  elif [ -f "$j/exitcode" ]; then st=done
  else st=missing; fi
  started=$(cat "$j/started" 2>/dev/null || stat -c %Y "$j/run.sh" 2>/dev/null)
  last=$(tail -c 4000 "$j/log" 2>/dev/null | tr '\r' '\n' | sed 's/\x1b\[[0-9;]*[A-Za-z]//g' \
    | grep -v '^[[:space:]]*$' | tail -n 1 | tr '\t' ' ' | cut -c1-200)
  printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$j" "$st" "$(cat "$j/exitcode" 2>/dev/null)" "$started" \
    "$(cat "$j/finished" 2>/dev/null)" "$([ -e "$j/fetched" ] && echo 1 || echo 0)" \
    "$(head -c 200 "$j/progress" 2>/dev/null | tr '\t\n' '  ')" "$last" "$([ -e "$j/saved" ] && echo 1 || echo 0)"
done
echo "@now $(date +%s)"
"""

# Zeigt das Log eines Jobs, bis er fertig ist. Gibt dessen Exit-Code zurueck.
FOLLOW_SH = r"""#!/usr/bin/env bash
JOB="$1"
cd "$JOB" || exit 97
touch log
tail -n +1 -F log 2>/dev/null &
T=$!
while [ -e running ]; do sleep 1; done
sleep 1
kill $T 2>/dev/null
wait $T 2>/dev/null
[ -f exitcode ] && exit "$(cat exitcode)"
exit 98
"""

# Loescht den Pod, wenn er zu lange nichts tut oder zu lange laeuft. Arg 1: Minuten Leerlauf, Arg 2: Stunden
# hoechstens, Arg 3: Minuten Leerlauf, solange ein fertiger Job noch nicht abgeholte Ergebnisse hat.
WATCHDOG_SH = r"""#!/usr/bin/env bash
IDLE_MIN="${1:-20}"
MAX_H="${2:-6}"
KEEP_MIN="${3:-$IDLE_MIN}"
LOG=/opt/poddo/watchdog.log
# Variablen von RunPod stehen nur im Hauptprozess, nicht in SSH-Sitzungen
envof() { tr '\0' '\n' < /proc/1/environ 2>/dev/null | sed -n "s/^$1=//p" | head -n1; }
POD_ID="${RUNPOD_POD_ID:-$(envof RUNPOD_POD_ID)}"
KEY="${RUNPOD_API_KEY:-$(envof RUNPOD_API_KEY)}"
START=$(date +%s)
idle=0

terminate() {
  echo "$(date -Is) $1: Pod wird geloescht" >> "$LOG"
  if [ -n "$KEY" ] && [ -n "$POD_ID" ]; then
    curl -fsS -X DELETE -H "Authorization: Bearer $KEY" "https://api.runpod.io/v2/pods/$POD_ID" >> "$LOG" 2>&1 && exit 0
    curl -fsS -X POST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
      -d '{"action":"terminate"}' "https://api.runpod.io/v2/pods/$POD_ID/action" >> "$LOG" 2>&1 && exit 0
  fi
  if command -v runpodctl >/dev/null && [ -n "$POD_ID" ]; then
    runpodctl remove pod "$POD_ID" >> "$LOG" 2>&1 && exit 0
  fi
  # Letzter Ausweg: Container beenden. Dann kostet die GPU nichts mehr, der Pod bleibt als EXITED.
  echo "$(date -Is) API ging nicht, beende Container" >> "$LOG"
  kill -TERM 1
  exit 0
}

echo "$(date -Is) Waechter: ${IDLE_MIN} min Leerlauf (${KEEP_MIN} min mit nicht abgeholten Ergebnissen), hoechstens ${MAX_H} h" >> "$LOG"
while true; do
  sleep 60
  now=$(date +%s)
  if awk -v a="$now" -v b="$START" -v h="$MAX_H" 'BEGIN{exit !(h>0 && a-b >= h*3600)}'; then
    terminate "Hoechstdauer ${MAX_H} h erreicht"
  fi
  busy=0
  ls /opt/poddo/jobs/*/running >/dev/null 2>&1 && busy=1
  # offene SSH-Sitzung (ab OpenSSH 9.8 heisst der Prozess sshd-session)
  pgrep -f 'sshd(-session)?: .*@' >/dev/null 2>&1 && busy=1
  # poddo touch: kurz "ich bin noch da"
  if [ -f /opt/poddo/keepalive ] && [ $(( now - $(stat -c %Y /opt/poddo/keepalive) )) -lt 120 ]; then busy=1; fi
  if [ $busy = 1 ]; then idle=0; else idle=$((idle + 1)); fi
  # Fertige Jobs, deren Ergebnisse poddo noch nicht geholt hat: laenger warten (Ergebnisse waeren sonst weg)
  limit=$IDLE_MIN
  for d in /opt/poddo/jobs/*/; do
    case "$(basename "$d")" in _*) continue ;; esac
    # auf dem Volume gesichert (saved) ist so gut wie abgeholt
    if [ -f "$d/exitcode" ] && [ ! -e "$d/fetched" ] && [ ! -e "$d/saved" ]; then limit=$KEEP_MIN; break; fi
  done
  if [ "$idle" -ge "$limit" ]; then terminate "${limit} min Leerlauf"; fi
done
"""

START_WATCHDOG = r"""
mkdir -p /opt/poddo
if [ -f /opt/poddo/watchdog.pid ] && kill -0 "$(cat /opt/poddo/watchdog.pid)" 2>/dev/null; then
  kill "$(cat /opt/poddo/watchdog.pid)" 2>/dev/null || true
fi
setsid nohup bash /opt/poddo/watchdog.sh @IDLE@ @MAX@ @KEEP@ >/dev/null 2>&1 < /dev/null &
echo $! > /opt/poddo/watchdog.pid
"""

CRANE_URL = "https://github.com/google/go-containerregistry/releases/download/v0.22.1/go-containerregistry_Linux_@ARCH@.tar.gz"

# Werkzeuge fuer den Push: crane (Images ohne Docker-Daemon bauen) und pigz (paralleles gzip).
IMAGE_TOOLS_SH = r"""
set -eo pipefail
source /opt/poddo/env.sh
command -v pigz >/dev/null || { apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq pigz >/dev/null; }
case "$(uname -m)" in aarch64|arm64) ARCH=arm64 ;; *) ARCH=x86_64 ;; esac
command -v crane >/dev/null || curl -LsSf "$(echo @CRANE_URL@ | sed "s/@ARCH@/$ARCH/")" | tar -xz -C /opt/poddo/bin crane
crane version
""".replace("@CRANE_URL@", CRANE_URL)

# Packt /opt/poddo/envs/<env> in Layer von hoechstens etwa 4 GB und haengt sie an das Basis-Image.
PUSH_SH = r"""#!/usr/bin/env bash
set -eo pipefail
source /opt/poddo/env.sh
NAME=@ENV@
BASE=@BASE@
TARGET=@TARGET@
W=/opt/poddo/image-layers
say() { printf '\n[poddo] %s\n' "$*"; }
rm -rf "$W" && mkdir -p "$W"
MARKER="/opt/poddo/envs/$NAME/.poddo-image"
echo "$TARGET" > "$MARKER"
# Geht etwas schief, gilt das Image nicht als hochgeladen
trap 'rm -f "$MARKER"' ERR

say "Umgebung in Layer aufteilen ($(du -sh "/opt/poddo/envs/$NAME" | cut -f1))"
python3 - "$NAME" "$W" <<'EOF'
import os, sys
name, out = sys.argv[1], sys.argv[2]
root = f"/opt/poddo/envs/{name}"
LIMIT = 4 * 1024**3
dirs, files = [], []
for dp, dns, fns in os.walk(root):
    dns.sort()
    dirs.append(dp)
    for n in sorted(fns) + [d for d in dns if os.path.islink(os.path.join(dp, d))]:
        p = os.path.join(dp, n)
        files.append((p, os.lstat(p).st_size))
groups, cur, size = [list(dirs)], [], 0
for p, n in files:
    if cur and size + n > LIMIT:
        groups.append(cur)
        cur, size = [], 0
    cur.append(p)
    size += n
if cur:
    groups.append(cur)
for i, g in enumerate(groups):
    with open(f"{out}/layer{i:02d}.list", "w") as f:
        f.write("\n".join(p.lstrip("/") for p in g) + "\n")
print(f"{len(groups)} Layer")
EOF

say "Layer packen (pigz)"
args=()
for list in "$W"/layer*.list; do
  tar -C / --no-recursion -T "$list" -cf - | pigz -1 > "${list%.list}.tar.gz"
  args+=(--new_layer "${list%.list}.tar.gz")
  echo "$(basename "${list%.list}"): $(du -h "${list%.list}.tar.gz" | cut -f1)"
done

say "Image bauen und hochladen: $TARGET"
# PODDO_CRANE_FLAGS z. B. --insecure fuer eine lokale Test-Registry ohne HTTPS
crane append $PODDO_CRANE_FLAGS --base "$BASE" --new_tag "$TARGET" "${args[@]}"
rm -rf "$W"
say "fertig: $TARGET ($(crane digest $PODDO_CRANE_FLAGS "$TARGET"))"
"""
