"""Shell-Skripte, die pod-do auf den Pod kopiert. Alles liegt dort unter /opt/pod-do."""

ROOT = "/opt/pod-do"

# Umgebung fuer setup, boot und jede Aufgabe. Platzhalter werden in pod.py ersetzt.
ENV_SH = r"""# von pod-do erzeugt
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
export POD_DO_ROOT=/opt/pod-do
export POD_DO_ENV=/opt/pod-do/envs/@ENV@
export POD_DO_VOLUME=@VOLUME@
if [ -n "$POD_DO_VOLUME" ]; then export POD_DO_CACHE="$POD_DO_VOLUME/pod-do/cache"; else export POD_DO_CACHE=/opt/pod-do/cache; fi
export PATH="/opt/pod-do/bin:$PATH"
# Python von uv in die Umgebung, damit es im Archiv mitkommt und die venv danach noch stimmt
export UV_PYTHON_INSTALL_DIR="$POD_DO_ENV/.python"
export UV_CACHE_DIR=/opt/pod-do/cache/uv
export UV_LINK_MODE=copy
# HF_HOME bleibt lokal: Global Volumes koennen keine Dateien sperren, huggingface_hub braucht das
export HF_HOME="${HF_HOME:-/opt/pod-do/cache/huggingface}"
mkdir -p "$POD_DO_ENV" "$POD_DO_CACHE" "$UV_CACHE_DIR" 2>/dev/null || true
@VARS@
"""

# Richtet die Umgebung ein: Werkzeuge, boot, dann Archiv vom Volume holen oder setup laufen lassen.
BOOTSTRAP_SH = r"""#!/usr/bin/env bash
set -eo pipefail
source /opt/pod-do/env.sh
NAME=@ENV@
HASH=@HASH@
ENVS=/opt/pod-do/envs
SNAPDIR="${POD_DO_VOLUME:+$POD_DO_VOLUME/pod-do/envs}"
SNAP="${SNAPDIR:+$SNAPDIR/$NAME-$HASH.tar.zst}"
say() { printf '\n[pod-do] %s\n' "$*"; }

say "Werkzeuge"
need=()
for t in curl tar zstd; do command -v $t >/dev/null || need+=($t); done
if [ ${#need[@]} -gt 0 ]; then
  apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${need[@]}" >/dev/null
fi
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/opt/pod-do/bin INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null
fi

if [ -s /opt/pod-do/boot.sh ] && [ ! -f /opt/pod-do/.boot-ok ]; then
  say "boot"
  bash -eo pipefail /opt/pod-do/boot.sh
  touch /opt/pod-do/.boot-ok
fi

if [ -f "$ENVS/$NAME/.pod-do-ok" ] && [ "$(cat "$ENVS/$NAME/.pod-do-ok")" = "$HASH" ]; then
  say "Umgebung $NAME ist bereit"
  exit 0
fi

if [ -n "$SNAP" ] && [ -f "$SNAP.ok" ] && [ -f "$SNAP" ]; then
  say "Umgebung $NAME vom Volume holen ($(du -h "$SNAP" | cut -f1))"
  rm -rf "${ENVS:?}/$NAME" && mkdir -p "$ENVS"
  zstd -dc -T0 "$SNAP" | tar -C "$ENVS" -xf -
  echo "$HASH" > "$ENVS/$NAME/.pod-do-ok"
  say "Umgebung $NAME ist bereit"
  exit 0
fi

say "setup fuer $NAME (einmalig, danach liegt es auf dem Volume)"
rm -rf "${ENVS:?}/$NAME" && mkdir -p "$ENVS/$NAME"
source /opt/pod-do/env.sh
cd "$POD_DO_ENV"
bash -eo pipefail /opt/pod-do/setup.sh
echo "$HASH" > "$ENVS/$NAME/.pod-do-ok"

if [ -n "$SNAP" ]; then
  say "Umgebung auf dem Volume sichern"
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
else
  say "ohne Volume: setup laeuft auf jedem neuen Pod wieder"
fi
say "Umgebung $NAME ist bereit"
"""

# Startet ein Skript losgeloest von der SSH-Verbindung. Arg 1: Job-Ordner (mit run.sh).
START_SH = r"""#!/usr/bin/env bash
set -e
JOB="$1"
cd "$JOB"
rm -f exitcode
touch running
setsid nohup bash -c 'bash run.sh > log 2>&1; echo $? > exitcode; rm -f running' >/dev/null 2>&1 < /dev/null &
echo $! > pid
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

# Loescht den Pod, wenn er zu lange nichts tut oder zu lange laeuft. Arg 1: Minuten, Arg 2: Stunden.
WATCHDOG_SH = r"""#!/usr/bin/env bash
IDLE_MIN="${1:-20}"
MAX_H="${2:-6}"
LOG=/opt/pod-do/watchdog.log
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

echo "$(date -Is) Waechter: ${IDLE_MIN} min Leerlauf, hoechstens ${MAX_H} h" >> "$LOG"
while true; do
  sleep 60
  now=$(date +%s)
  if awk -v a="$now" -v b="$START" -v h="$MAX_H" 'BEGIN{exit !(h>0 && a-b >= h*3600)}'; then
    terminate "Hoechstdauer ${MAX_H} h erreicht"
  fi
  busy=0
  ls /opt/pod-do/jobs/*/running >/dev/null 2>&1 && busy=1
  pgrep -f 'sshd: .*@' >/dev/null 2>&1 && busy=1
  # pod-do touch: kurz "ich bin noch da"
  if [ -f /opt/pod-do/keepalive ] && [ $(( now - $(stat -c %Y /opt/pod-do/keepalive) )) -lt 120 ]; then busy=1; fi
  if [ $busy = 1 ]; then idle=0; else idle=$((idle + 1)); fi
  if [ "$idle" -ge "$IDLE_MIN" ]; then terminate "${IDLE_MIN} min Leerlauf"; fi
done
"""

START_WATCHDOG = r"""
mkdir -p /opt/pod-do
if [ -f /opt/pod-do/watchdog.pid ] && kill -0 "$(cat /opt/pod-do/watchdog.pid)" 2>/dev/null; then
  kill "$(cat /opt/pod-do/watchdog.pid)" 2>/dev/null || true
fi
setsid nohup bash /opt/pod-do/watchdog.sh @IDLE@ @MAX@ >/dev/null 2>&1 < /dev/null &
echo $! > /opt/pod-do/watchdog.pid
"""
