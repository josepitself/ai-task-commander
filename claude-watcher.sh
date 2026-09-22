#!/usr/bin/env bash
# Vigila <cua>/*.json i executa claude (Claude Code) per cada feina.
# Format del JSON: { "model": "claude-sonnet-5", "effort": "medium", "prompt": "..." }
# Resultat: <nom>.md (resposta), <nom>.err (stderr), <nom>.json.done (feina consumida)
# Ús: ./claude-watcher.sh [-f|--force] [-q|--queue DIR] [-w|--workdir DIR] [-h|--help]
# Requisits: bash 4+, python3 (per llegir el JSON) i la CLI claude al PATH amb sessió iniciada.
set -u

QUEUE="$HOME/Claude/HomeAssistant/claude-queue"
WORKDIR="$HOME/Claude/HomeAssistant"
FORCE=0

usage() {
  cat <<USAGE
Ús: $(basename "$0") [OPCIONS]

Vigila el directori de cua i executa claude per cada feina <nom>.json que hi aparegui.

Opcions:
  -f, --force         Arrenca encara que hi hagi feines pendents (*.json) a la cua i les processa.
  -q, --queue DIR     Directori de cua (per defecte: \$HOME/Claude/HomeAssistant/claude-queue).
  -w, --workdir DIR   Directori de treball de la CLI (per defecte: \$HOME/Claude/HomeAssistant).
  -h, --help          Mostra aquesta ajuda i surt.
USAGE
}

while [ $# -gt 0 ]; do
  case "$1" in
    -f|--force) FORCE=1 ;;
    -q|--queue) [ $# -ge 2 ] || { echo "Falta el directori per a $1" >&2; exit 2; }; QUEUE=$2; shift ;;
    --queue=*) QUEUE=${1#*=} ;;
    -w|--workdir) [ $# -ge 2 ] || { echo "Falta el directori per a $1" >&2; exit 2; }; WORKDIR=$2; shift ;;
    --workdir=*) WORKDIR=${1#*=} ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Opció desconeguda: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# Colors només si la sortida és un terminal.
if [ -t 1 ]; then RED=$'\e[31m'; GREEN=$'\e[32m'; YELLOW=$'\e[33m'; RESET=$'\e[0m'; else RED=; GREEN=; YELLOW=; RESET=; fi
ara() { date +%H:%M:%S; }

# --- Comprovacions prèvies -------------------------------------------------
command -v claude >/dev/null 2>&1 || { echo "No s'ha trobat la CLI claude al PATH." >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "Cal python3 per llegir els fitxers JSON de la cua." >&2; exit 1; }
[ -d "$WORKDIR" ] || { echo "El directori de treball $WORKDIR no existeix." >&2; exit 1; }

# --- Preparació del directori de cua ---------------------------------------
if [ -e "$QUEUE" ] && [ ! -d "$QUEUE" ]; then
  echo "La ruta $QUEUE existeix però és un fitxer, no un directori." >&2
  exit 1
fi
shopt -s nullglob
if [ ! -d "$QUEUE" ]; then
  mkdir -p "$QUEUE" || exit 1
  echo "Creat el directori de cua $QUEUE"
else
  # Només compten les feines pendents (*.json). L'històric (*.json.done, *.md, *.err) es conserva.
  pending=("$QUEUE"/*.json)
  done_jobs=("$QUEUE"/*.json.done)
  echo "Cua $QUEUE : ${#done_jobs[@]} feines fetes, ${#pending[@]} pendents"
  if [ ${#pending[@]} -gt 0 ]; then
    noms=$(printf '%s\n' "${pending[@]}" | xargs -n1 basename | paste -sd, - | sed 's/,/, /g')
    if [ $FORCE -eq 1 ]; then
      echo "${YELLOW}AVÍS: Hi ha ${#pending[@]} feines pendents ($noms). S'executaran ara per --force.${RESET}"
    else
      echo "${YELLOW}AVÍS: Hi ha ${#pending[@]} feines pendents ($noms). Revisa-les i executa l'script amb --force per processar-les, o esborra-les.${RESET}"
      exit 1
    fi
  fi
fi

# --- Lectura d'una feina ----------------------------------------------------
# Escriu model i effort a stdout (una línia cada un) i el prompt al fitxer indicat.
# Amb codi de sortida 1 escriu el motiu a stdout.
llegeix_feina() {
  python3 - "$1" "$2" <<'PY'
import json, sys
path, prompt_file = sys.argv[1], sys.argv[2]
try:
    with open(path, encoding="utf-8") as f:
        raw = f.read()
except OSError as e:
    print(f"no es pot llegir ({e})"); sys.exit(1)
if not raw.strip():
    print("el fitxer és buit"); sys.exit(1)
try:
    job = json.loads(raw)
except ValueError as e:
    print(f"no és JSON vàlid ({e})"); sys.exit(1)
if not isinstance(job, dict) or not all(job.get(k) for k in ("model", "effort", "prompt")):
    print("falten els camps model, effort o prompt"); sys.exit(1)
model, effort = str(job["model"]), str(job["effort"])
if "\n" in model or "\n" in effort:
    print("model o effort contenen salts de línia"); sys.exit(1)
with open(prompt_file, "w", encoding="utf-8") as f:
    f.write(str(job["prompt"]))
print(model); print(effort)
PY
}

# --- Bucle de vigilància ----------------------------------------------------
echo "Vigilant $QUEUE (Ctrl+C per aturar)"
promptfile=$(mktemp "${TMPDIR:-/tmp}/claude-watcher.XXXXXX") || exit 1
trap 'rm -f "$promptfile"' EXIT
# Feines invàlides ja avisades (ruta + data de modificació). Es tornen a provar si el fitxer canvia.
declare -A invalid=()
while true; do
  jobs_list=("$QUEUE"/*.json)
  if [ ${#jobs_list[@]} -gt 0 ]; then
    mapfile -t ordered < <(stat -c '%Y %n' "${jobs_list[@]}" | sort -n | cut -d' ' -f2-)
  else
    ordered=()
  fi
  for file in "${ordered[@]}"; do
    [ -f "$file" ] || continue
    base=$(basename "$file" .json)
    stamp="$file|$(stat -c %Y "$file")"
    [ -n "${invalid[$stamp]:-}" ] && continue

    # Un JSON invàlid (o a mig escriure) no atura el watcher: s'avisa un cop i s'ignora fins que canviï.
    if ! info=$(llegeix_feina "$file" "$promptfile"); then
      echo "${RED}[$(ara)] ERROR feina $base ignorada: $info${RESET}"
      invalid[$stamp]=1
      continue
    fi
    model=$(sed -n 1p <<<"$info")
    effort=$(sed -n 2p <<<"$info")

    echo "[$(ara)] Inici feina $base ($model/$effort)"
    t0=$(date +%s%N)
    # El prompt entra per stdin (fitxer temporal): no passa per la línia d'ordres.
    # claude no té --cd: s'executa en un subshell dins del directori de treball.
    # NO_COLOR evita codis ANSI al .err.
    (cd "$WORKDIR" && NO_COLOR=1 claude -p --model "$model" --effort "$effort" --output-format text) \
      <"$promptfile" >"$QUEUE/$base.md" 2>"$QUEUE/$base.err"
    rc=$?
    ms=$(( ($(date +%s%N) - t0) / 1000000 ))
    durada="$((ms / 1000)).$(( (ms % 1000) / 100 )) s"
    mv -f "$file" "$file.done"
    # El resultat es decideix pel codi de sortida de la CLI (0 = bé; claude torna 1 si falla).
    # Si la CLI acaba amb 0 però no ha escrit el .md, també és error.
    if [ $rc -eq 0 ] && [ -f "$QUEUE/$base.md" ]; then
      echo "${GREEN}[$(ara)] Fi feina $base: OK ($durada) -> $base.md${RESET}"
    else
      if [ $rc -ne 0 ]; then motiu="codi de sortida $rc"; else motiu="no s'ha escrit $base.md"; fi
      [ -s "$QUEUE/$base.err" ] && motiu="$motiu, revisa $base.err"
      echo "${RED}[$(ara)] Fi feina $base: ERROR ($durada, $motiu)${RESET}"
    fi
  done
  sleep 5
done
