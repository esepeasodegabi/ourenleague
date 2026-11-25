#!/usr/bin/env bash
# Regenera la web de la liga y publica el contenido estático en GitHub Pages.
# Conserva el directorio scripts/ del repo (código de extracción y de la liga).
# El token debe estar en GITHUB_OURENLEAGUE_TOKEN. Nunca se imprime ni se escribe a disco.
set -euo pipefail
# Nunca activar xtrace: el askpass o las cabeceras podrían filtrar el token.
set +x
export PS4='+ '

ROOT="$(cd "$(dirname "$0")" && pwd)"
CONFIG="${ROOT}/config.json"
SITE_SRC="${ROOT}/site"
PUBLISH_DIR="${ROOT}/publish_repo"
ASKPASS="${ROOT}/git-askpass.sh"
REPO="esepeasodegabi/ourenleague"
REMOTE_URL="https://github.com/${REPO}.git"
DEFAULT_START="2025-09-01"
TITLE=""
START=""
SKIP_BUILD=0
WAIT_PAGES=1

usage() {
  cat <<'USAGE'
Uso: ./publish.sh [--start YYYY-MM-DD] [--title "Título"] [--skip-build] [--no-wait]

  --start        Fecha de inicio de la liga (por defecto: config.json o 2025-09-01)
  --title        Título de la web (por defecto: el de config.json)
  --skip-build   Publica el site/ ya generado, sin volver a ejecutar liga.py
  --no-wait      No espera a que GitHub Pages termine el build
  --help         Esta ayuda

Requiere GITHUB_OURENLEAGUE_TOKEN en el entorno (no se guarda en archivos).
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --start)
      START="${2:-}"
      shift 2
      ;;
    --title)
      TITLE="${2:-}"
      shift 2
      ;;
    --skip-build)
      SKIP_BUILD=1
      shift
      ;;
    --no-wait)
      WAIT_PAGES=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      # Positional start date, for convenience: ./publish.sh 2025-09-01
      if [[ "$1" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]]; then
        START="$1"
        shift
      else
        echo "Argumento desconocido: $1" >&2
        usage >&2
        exit 2
      fi
      ;;
  esac
done

if [[ -z "${GITHUB_OURENLEAGUE_TOKEN:-}" ]]; then
  echo "Falta la variable de entorno GITHUB_OURENLEAGUE_TOKEN." >&2
  exit 1
fi

# Leer valores de config.json sin python extra si jq no está; usamos python.
eval "$(python3 - "$CONFIG" "$DEFAULT_START" <<'PY'
import json, shlex, sys
path, default_start = sys.argv[1], sys.argv[2]
start, title, repo, out = default_start, "Liga Brooklyn Ourense", "esepeasodegabi/ourenleague", "site"
try:
    data = json.loads(open(path, encoding="utf-8").read())
    start = data.get("start") or start
    title = data.get("title") or title
    repo = data.get("repo") or repo
    out = data.get("out") or out
except FileNotFoundError:
    pass
print("CFG_START=" + shlex.quote(str(start)))
print("CFG_TITLE=" + shlex.quote(str(title)))
print("CFG_REPO=" + shlex.quote(str(repo)))
print("CFG_OUT=" + shlex.quote(str(out)))
PY
)"

REPO="${CFG_REPO}"
REMOTE_URL="https://github.com/${REPO}.git"
SITE_SRC="${ROOT}/${CFG_OUT}"
[[ -n "$START" ]] || START="$CFG_START"
[[ -n "$TITLE" ]] || TITLE="$CFG_TITLE"

PYTHON="/workspace/h4c/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="$(command -v python3)"
fi

if [[ "$SKIP_BUILD" -eq 0 ]]; then
  echo "Generando la liga desde ${START}…"
  "$PYTHON" "${ROOT}/liga.py" --start "$START" --out "$SITE_SRC" --title "$TITLE"
  # Ocupación de sacos (ocupa/). Si falla, no bloquea la liga: se mantiene la versión anterior.
  echo "Generando la ocupación de sacos…"
  if ! "$PYTHON" "${ROOT}/ocupa.py" --out "${SITE_SRC}/ocupa.new"; then
    echo "Aviso: no pude regenerar ocupa/; se publica la versión anterior." >&2
    rm -rf "${SITE_SRC}/ocupa.new"
  else
    rm -rf "${SITE_SRC}/ocupa"
    mv "${SITE_SRC}/ocupa.new" "${SITE_SRC}/ocupa"
  fi
else
  echo "Omitiendo regeneración; se publica ${SITE_SRC}"
fi

if [[ ! -f "${SITE_SRC}/index.html" || ! -f "${SITE_SRC}/data.json" ]]; then
  echo "Faltan index.html o data.json en ${SITE_SRC}." >&2
  exit 1
fi

# Preparar el directorio de publicación (solo salida estática).
mkdir -p "$PUBLISH_DIR"
if [[ ! -d "${PUBLISH_DIR}/.git" ]]; then
  git init -b main "$PUBLISH_DIR" >/dev/null
  git -C "$PUBLISH_DIR" remote add origin "$REMOTE_URL"
else
  # Asegurar que el remote no tiene credenciales incrustadas.
  existing="$(git -C "$PUBLISH_DIR" remote get-url origin 2>/dev/null || true)"
  if [[ -z "$existing" ]]; then
    git -C "$PUBLISH_DIR" remote add origin "$REMOTE_URL"
  elif [[ "$existing" != "$REMOTE_URL" ]]; then
    if [[ "$existing" == *"@"* || "$existing" == *":"*github* && "$existing" == *"@"* ]]; then
      echo "El remote de origin contiene credenciales o no coincide; se reemplaza por la URL limpia." >&2
    fi
    # Si la URL actual incluye un token o usuario, forzar la URL pública.
    git -C "$PUBLISH_DIR" remote set-url origin "$REMOTE_URL"
  fi
fi

# No guardar credenciales en config ni en el helper.
git -C "$PUBLISH_DIR" config --local --unset-all http.extraHeader >/dev/null 2>&1 || true
git -C "$PUBLISH_DIR" config --local credential.helper ""
git -C "$PUBLISH_DIR" config --local user.name "Liga bot"
git -C "$PUBLISH_DIR" config --local user.email "liga-bot@users.noreply.github.com"

export GIT_ASKPASS="$ASKPASS"
export GIT_TERMINAL_PROMPT=0
export GIT_CONFIG_NOSYSTEM=1
# Evita que un helper global reescriba la URL con el token.
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0="credential.helper"
export GIT_CONFIG_VALUE_0=""

# Traer historial remoto si ya existe main (repo no vacío).
if [[ -n "$(git -C "$PUBLISH_DIR" ls-remote --heads origin main)" ]]; then
  git -C "$PUBLISH_DIR" fetch --depth=50 origin main
  git -C "$PUBLISH_DIR" checkout -B main origin/main
else
  git -C "$PUBLISH_DIR" checkout -B main
fi

# Vaciar el árbol de trabajo dejando .git, scripts/ y ocupa/, y copiar la salida estática.
# scripts/ versiona el extractor y la liga; no debe borrarse al regenerar la web.
# ocupa/ solo se sustituye si hay una versión nueva generada.
if [[ -e "${PUBLISH_DIR}/scripts" && ! -d "${PUBLISH_DIR}/scripts" ]]; then
  echo "scripts existe pero no es un directorio. Abortando." >&2
  exit 1
fi
find "$PUBLISH_DIR" -mindepth 1 -maxdepth 1 ! -name .git ! -name scripts ! -name ocupa -exec rm -rf {} +
if [[ -f "${SITE_SRC}/ocupa/index.html" && -f "${SITE_SRC}/ocupa/data.json" ]]; then
  rm -rf "${PUBLISH_DIR}/ocupa"
  mkdir -p "${PUBLISH_DIR}/ocupa"
  cp "${SITE_SRC}/ocupa/index.html" "${SITE_SRC}/ocupa/data.json" "${PUBLISH_DIR}/ocupa/"
fi
cp "${SITE_SRC}/index.html" "$PUBLISH_DIR/index.html"
cp "${SITE_SRC}/data.json" "$PUBLISH_DIR/data.json"
# Solo las fotos referenciadas en data.json (nada suelto de generaciones anteriores).
"$PYTHON" - "$SITE_SRC" "$PUBLISH_DIR" <<'COPYPHOTOS'
import json, shutil, sys
from pathlib import Path
src, dest = Path(sys.argv[1]), Path(sys.argv[2])
data = json.loads((src / "data.json").read_text(encoding="utf-8"))
people = data.get("rankings") or []
refs = []
for person in people:
    photo = person.get("photo")
    if photo and not str(photo).startswith("http"):
        refs.append(Path(photo).name)
img_src, img_dest = src / "img", dest / "img"
if refs:
    img_dest.mkdir(parents=True, exist_ok=True)
copied = 0
missing = []
for name in refs:
    s = img_src / name
    if not s.is_file():
        missing.append(name)
        continue
    shutil.copy2(s, img_dest / name)
    copied += 1
if missing:
    print("Faltan fotos referenciadas: " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
print(f"Fotos copiadas: {copied}")
COPYPHOTOS
# Evita que Jekyll ignore archivos o procese el sitio.
: > "$PUBLISH_DIR/.nojekyll"

"$PYTHON" "${ROOT}/scan_site.py" "$PUBLISH_DIR"
# Doble comprobación: el directorio publicado no debe contener nada más.
python3 - "$PUBLISH_DIR" <<'PY'
import sys
from pathlib import Path
root = Path(sys.argv[1])
allowed = {".git", "index.html", "data.json", "img", ".nojekyll", "scripts", "ocupa"}
extra = sorted(p.name for p in root.iterdir() if p.name not in allowed)
if extra:
    print("El directorio de publicación tiene archivos de más: " + ", ".join(extra), file=sys.stderr)
    sys.exit(1)
scripts = root / "scripts"
if scripts.exists() and not scripts.is_dir():
    print("scripts existe pero no es un directorio. Abortando.", file=sys.stderr)
    sys.exit(1)
# Asegurar que el remote sigue limpio
cfg = (root / ".git" / "config").read_text(encoding="utf-8")
if "github_pat" in cfg or "x-access-token:" in cfg or "@github.com" in cfg.replace("https://github.com", ""):
    print("El .git/config parece contener credenciales. Abortando.", file=sys.stderr)
    sys.exit(1)
ocupa = root / "ocupa"
if ocupa.exists():
    extra = sorted(p.name for p in ocupa.iterdir() if p.name not in {"index.html", "data.json"})
    if extra:
        print("ocupa/ tiene archivos de más: " + ", ".join(extra), file=sys.stderr)
        sys.exit(1)
print("Contenido a publicar: index.html, data.json, img/, ocupa/, .nojekyll (se conserva scripts/)")
PY

# Guardia: el email del portal no puede aparecer en nada de lo que se publica
# (web ni scripts/). Se lee de H4C_EMAIL o de extract.py y nunca se imprime.
"$PYTHON" - "$PUBLISH_DIR" <<'EMAILGUARD'
import os, re, sys
from pathlib import Path
root = Path(sys.argv[1])
needles = set()
env = (os.environ.get("H4C_EMAIL") or "").strip()
if env:
    needles.add(env.lower())
try:
    src = Path("/workspace/h4c/extract.py").read_text(encoding="utf-8")
    m = re.search(r'^DEFAULT_EMAIL\s*=\s*"([^"]+)"', src, re.M)
    if m and "@" in m.group(1):
        needles.add(m.group(1).strip().lower())
except OSError:
    pass
needles.add("@" + "brooklynfitboxing" + ".com")  # partido para no autodetectarse
hits = []
for path in root.rglob("*"):
    if ".git" in path.relative_to(root).parts or not path.is_file():
        continue
    try:
        text = path.read_bytes().decode("utf-8", "ignore").lower()
    except OSError:
        continue
    if any(n in text for n in needles):
        hits.append(str(path.relative_to(root)))
if hits:
    print("El email del portal aparece en archivos a publicar: " + ", ".join(sorted(hits)) + ". Abortando.", file=sys.stderr)
    sys.exit(1)
print("Guardia de email OK")
EMAILGUARD

git -C "$PUBLISH_DIR" add -A
if git -C "$PUBLISH_DIR" diff --cached --quiet; then
  echo "No hay cambios que publicar."
else
  stamp="$(TZ=Europe/Madrid date '+%Y-%m-%d %H:%M %Z')"
  msg="Actualización de la liga ${stamp} (inicio ${START})"
  git -C "$PUBLISH_DIR" commit -m "$msg"
  git -C "$PUBLISH_DIR" -c credential.helper= push origin main
  echo "Publicado: ${msg}"
fi

# Comprobar que el remote URL sigue sin credenciales tras el push.
final_url="$(git -C "$PUBLISH_DIR" remote get-url origin)"
if [[ "$final_url" != "$REMOTE_URL" ]]; then
  echo "Aviso: el remote cambió; se restaura la URL pública." >&2
  git -C "$PUBLISH_DIR" remote set-url origin "$REMOTE_URL"
fi

# Activar o actualizar GitHub Pages (main, /).
# El push ya se ha hecho; un fallo aquí no revierte el commit.
echo "Configurando GitHub Pages…"
PAGES_URL="https://${REPO%%/*}.github.io/${REPO#*/}/"
set +e
PAGES_OUT="$("$PYTHON" "${ROOT}/github_pages.py" enable "$REPO")"
PAGES_RC=$?
set -e
if [[ "$PAGES_RC" -eq 0 && -n "$PAGES_OUT" ]]; then
  PAGES_URL="$PAGES_OUT"
  echo "URL de Pages: ${PAGES_URL}"
else
  echo "El contenido ya está en main, pero no pude activar GitHub Pages (código ${PAGES_RC})." >&2
  echo "Actívalo una vez en: https://github.com/${REPO}/settings/pages" >&2
  echo "  Source: Deploy from a branch → main → / (root) → Save." >&2
  echo "O añade al token fine-grained (solo este repo) Pages: Read and write y Administration: Read and write, y vuelve a ejecutar:" >&2
  echo "  /workspace/liga/publish.sh --skip-build" >&2
  echo "URL prevista: ${PAGES_URL}" >&2
  exit 2
fi

if [[ "$WAIT_PAGES" -eq 1 ]]; then
  echo "Esperando al build de GitHub Pages…"
  "$PYTHON" "${ROOT}/github_pages.py" wait "$REPO" "$PAGES_URL"
fi

echo "Listo."
