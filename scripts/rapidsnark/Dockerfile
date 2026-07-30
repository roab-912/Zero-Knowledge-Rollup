# Image de bench rapidsnark, utilisable via `docker exec` sur un conteneur
# persistant nommé `debian_rapidsnark` (cf. scripts/rapidsnark/setup.sh).
#
# rapidsnark est compilé dans l'image, à l'emplacement attendu par
# generate_proofs.py et measure_zk_resources.py :
#     mnt/projet/rapidsnark/package/bin/prover
# (chemin relatif au WORKDIR `/`, comme dans les commandes existantes).
FROM debian:bookworm

# Dépendances de compilation listées par le README de rapidsnark.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        libgmp-dev \
        libsodium-dev \
        nasm \
        curl \
        m4 \
        git \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG RAPIDSNARK_REPO=https://github.com/iden3/rapidsnark.git
ARG RAPIDSNARK_REF=main
ARG RAPIDSNARK_DIR=/mnt/projet/rapidsnark

RUN git clone "$RAPIDSNARK_REPO" "$RAPIDSNARK_DIR" \
    && cd "$RAPIDSNARK_DIR" \
    && git checkout "$RAPIDSNARK_REF" \
    && git submodule update --init --recursive

# Pré-téléchargement de gmp dans depends/, où build_gmp.sh l'attend : le script
# saute son propre téléchargement si l'archive est déjà là. Nécessaire parce que
# sa boucle de miroirs considère une page d'erreur HTTP comme un succès (curl
# sans -f) puis abandonne sur l'échec du checksum. Ici curl -f + vérification
# explicite, avec repli sur les autres miroirs.
ARG GMP_VERSION=6.3.0
ARG GMP_SHA256=a3c2b80201b89e68616f4ad30bc66aee4927c3ce50e33929ca819d5c43538898
RUN set -eu; \
    cd "$RAPIDSNARK_DIR/depends"; \
    archive="gmp-${GMP_VERSION}.tar.xz"; \
    for url in \
        "https://ftp.gnu.org/gnu/gmp/${archive}" \
        "https://gmplib.org/download/gmp/${archive}" \
        "https://ftpmirror.gnu.org/gmp/${archive}" ; do \
        echo "→ $url"; \
        if curl -fL --retry 3 --retry-delay 2 --connect-timeout 15 -o "$archive" "$url" \
           && echo "${GMP_SHA256}  ${archive}" | sha256sum -c -; then \
            echo "gmp récupéré et vérifié depuis $url"; break; \
        fi; \
        rm -f "$archive"; \
    done; \
    test -f "$archive"

RUN cd "$RAPIDSNARK_DIR" \
    && ./build_gmp.sh host \
    && make host \
    && test -x "$RAPIDSNARK_DIR/package/bin/prover" \
    # les objets intermédiaires ne servent plus, seul package/ est utilisé
    && rm -rf "$RAPIDSNARK_DIR/build_prover" "$RAPIDSNARK_DIR/depends/gmp/build"

# WORKDIR `/` : c'est ce qui rend valides les chemins relatifs
# `mnt/projet/...` des commandes docker exec existantes.
WORKDIR /

CMD ["sleep", "infinity"]
