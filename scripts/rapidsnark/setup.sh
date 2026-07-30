#!/usr/bin/env bash
# Crée (ou recrée) un conteneur persistant `debian_rapidsnark` exposant le
# prover rapidsnark, de sorte que les commandes déjà utilisées dans
# scripts/generate_proofs.py fonctionnent telles quelles :
#
#   docker exec debian_rapidsnark \
#       mnt/projet/rapidsnark/package/bin/prover \
#       mnt/projet/<n>/circuit_final.zkey mnt/projet/<n>/witness.wtns \
#       mnt/projet/<n>/proof.json mnt/projet/<n>/public.json
#
# Le dossier ./circuits de l'hôte est monté sur /mnt/projet dans le conteneur,
# et rapidsnark (compilé dans l'image) est exposé sur /mnt/projet/rapidsnark via
# un volume nommé : le dépôt de l'hôte n'est donc pas pollué par les binaires,
# alors que le chemin attendu reste valide dans le conteneur.
#
# Usage : bash scripts/rapidsnark/setup.sh [chemin/vers/circuits]
set -euo pipefail

CONTAINER="${CONTAINER:-debian_rapidsnark}"
IMAGE="${IMAGE:-rapidsnark:bench}"
VOLUME="${VOLUME:-rapidsnark_pkg}"
RAPIDSNARK_REF="${RAPIDSNARK_REF:-main}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd "$script_dir/../.." && pwd)"
circuits_dir="$(cd "${1:-$repo_root/circuits}" && pwd)"

echo "== Dossier des circuits : $circuits_dir"
echo "== Image                : $IMAGE"
echo "== Conteneur            : $CONTAINER"

if ! docker info >/dev/null 2>&1; then
    echo "[erreur] le démon docker ne répond pas." >&2
    echo "         Debian nu     : sudo systemctl start docker" >&2
    echo "         WSL + Desktop : démarrer Docker Desktop et activer" >&2
    echo "                         l'intégration WSL pour cette distro" >&2
    exit 1
fi

echo "== Construction de l'image (compilation de rapidsnark, quelques minutes)"
docker build \
    --build-arg "RAPIDSNARK_REF=$RAPIDSNARK_REF" \
    -t "$IMAGE" \
    -f "$script_dir/Dockerfile" \
    "$script_dir"

if [ -n "$(docker ps -aq --filter "name=^${CONTAINER}$")" ]; then
    echo "== Suppression du conteneur existant $CONTAINER"
    docker rm -f "$CONTAINER" >/dev/null
fi

# Volume recréé à chaque fois pour qu'il soit repeuplé depuis la nouvelle image
# (docker ne copie le contenu de l'image que dans un volume vide).
docker volume rm "$VOLUME" >/dev/null 2>&1 || true

echo "== Création du conteneur persistant"
docker run -d \
    --name "$CONTAINER" \
    --restart unless-stopped \
    -w / \
    -v "$circuits_dir:/mnt/projet" \
    -v "$VOLUME:/mnt/projet/rapidsnark" \
    "$IMAGE" \
    sleep infinity >/dev/null

echo "== Vérifications"
docker exec "$CONTAINER" test -x mnt/projet/rapidsnark/package/bin/prover \
    && echo "   prover présent : mnt/projet/rapidsnark/package/bin/prover"
docker exec "$CONTAINER" ls mnt/projet >/dev/null \
    && echo "   circuits visibles depuis le conteneur"
docker exec "$CONTAINER" mnt/projet/rapidsnark/package/bin/prover 2>&1 | head -3 || true

cat <<EOF

Conteneur prêt. Il redémarre automatiquement avec le démon docker
(--restart unless-stopped).

Bench :
  python3 scripts/measure_zk_resources.py --skip-setup --sizes 1,2,4,8 \\
      --repeat 5 --prover both --rapidsnark-mode docker \\
      --docker-container $CONTAINER

Appel direct, identique à generate_proofs.py :
  docker exec $CONTAINER mnt/projet/rapidsnark/package/bin/prover \\
      mnt/projet/1/circuit_final.zkey mnt/projet/1/witness.wtns \\
      mnt/projet/1/proof.json mnt/projet/1/public.json
EOF
