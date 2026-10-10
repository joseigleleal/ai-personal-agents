#!/bin/bash
# Genera / actualiza la copia pública del proyecto (vitrina), sin datos personales.
# Toma el último commit del repo privado, quita el perfil real y hace un commit en la carpeta pública
# con el correo "noreply" de GitHub. La historia pública es independiente: nunca contiene el perfil real.
# Uso:  ./scripts/exportar_publico.sh ["mensaje del commit"]
#       Primera vez: crea la carpeta y el repo local. Para publicar, agregar el remoto y hacer push (ver README).
set -euo pipefail

PRIVATE="$(cd "$(dirname "$0")/.." && pwd)"
PUBLIC="${PUBLIC_DIR:-$PRIVATE/../personal-agents-public}"
AUTHOR_NAME="Jose Iglesias"
AUTHOR_EMAIL="42049894+joseigleleal@users.noreply.github.com"
MESSAGE="${1:-Sincroniza con la versión de desarrollo}"
# Archivos que nunca salen del repo privado
EXCLUDE=(config/profile.yaml main.py)

cd "$PRIVATE"
if [ -n "$(git status --porcelain)" ]; then
  echo "Hay cambios sin commit en el repo privado; se exporta solo el último commit (HEAD)."
fi

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
git archive HEAD | tar -x -C "$STAGE"
for f in "${EXCLUDE[@]}"; do rm -f "$STAGE/$f"; done

# Control de seguridad: no exportar si aparece algo que parezca un secreto o un dato personal
if grep -rIlE "sk-ant-[A-Za-z0-9]|gsk_[A-Za-z0-9]{10}|BEGIN [A-Z ]*PRIVATE KEY|@gmail\.com|ocid1\.(tenancy|user)\." "$STAGE"; then
  echo "ERROR: los archivos de arriba parecen tener secretos o datos personales. No se exporta."; exit 1
fi

mkdir -p "$PUBLIC"
if [ ! -d "$PUBLIC/.git" ]; then
  git -C "$PUBLIC" init -q -b main
fi
git -C "$PUBLIC" config user.name "$AUTHOR_NAME"
git -C "$PUBLIC" config user.email "$AUTHOR_EMAIL"

rsync -a --delete --exclude .git "$STAGE/" "$PUBLIC/"
git -C "$PUBLIC" add -A
if git -C "$PUBLIC" diff --cached --quiet; then
  echo "Sin cambios respecto de la copia pública."; exit 0
fi
git -C "$PUBLIC" commit -q -m "$MESSAGE"
echo "Commit creado en $PUBLIC:"
git -C "$PUBLIC" log -1 --format='  %h %an <%ae> %s'

if git -C "$PUBLIC" remote get-url origin >/dev/null 2>&1; then
  git -C "$PUBLIC" push -q origin main && echo "Subido a $(git -C "$PUBLIC" remote get-url origin)"
else
  echo "Sin remoto todavía. Para publicar:  gh repo create <nombre> --public --source \"$PUBLIC\" --push"
fi
