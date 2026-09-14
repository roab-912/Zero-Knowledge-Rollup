# Image de bench « tout-en-un » : rapidsnark, circom, snarkjs et le runtime
# Python de measure_zk_resources.py dans un même conteneur Linux.
#
# Pourquoi tout dans une seule image plutôt qu'un conteneur rapidsnark isolé :
# measure_zk_resources.py échantillonne /proc/<pid> pour attribuer CPU, RSS et
# I/O au processus mesuré. Depuis Windows, /proc n'existe pas ; et si seul
# rapidsnark est conteneurisé, `docker exec` masque l'arbre de processus au
# script, qui bascule alors sur scope="system" (compteurs machine, bruités).
# En exécutant le script *dans* le conteneur, le prover est un enfant direct :
# toutes les phases sont mesurées en scope="process".
#
# Deux chemins mènent au prover, volontairement :
#   /usr/local/bin/prover                        détecté seul par
#                                                --rapidsnark-mode native/auto
#   /mnt/projet/rapidsnark/package/bin/prover    convention historique de
#                                                generate_proofs.py et de
#                                                scripts/rapidsnark/setup.sh
FROM node:22-bookworm

# Dépendances de compilation de rapidsnark (README amont) + runtime Python du
# script de bench. numpy/matplotlib viennent d'apt : sous bookworm, pip refuse
# d'installer hors venv (PEP 668).
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
        python3 \
        python3-numpy \
        python3-matplotlib \
        procps \
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
    && rm -rf "$RAPIDSNARK_DIR/build_prover" "$RAPIDSNARK_DIR/depends/gmp/build" \
    # NATIVE_CANDIDATES du script cherche « prover » dans le PATH
    && ln -s "$RAPIDSNARK_DIR/package/bin/prover" /usr/local/bin/prover

# circom : binaire de release officiel (compiler la version Rust prendrait
# plusieurs minutes de plus sans rien changer aux mesures).
ARG CIRCOM_VERSION=v2.2.2
RUN curl -fL --retry 3 -o /usr/local/bin/circom \
        "https://github.com/iden3/circom/releases/download/${CIRCOM_VERSION}/circom-linux-amd64" \
    && chmod +x /usr/local/bin/circom \
    && circom --version

# snarkjs épinglé sur la version qui a produit les circuits du dépôt : changer
# de version changerait les temps mesurés sans que le bench le signale.
ARG SNARKJS_VERSION=0.7.5
RUN npm install -g "snarkjs@${SNARKJS_VERSION}" && snarkjs --version | head -1

# Node alloue par défaut un tas trop petit pour les grands circuits : snarkjs
# meurt en « JavaScript heap out of memory » avant que la mesure n'aboutisse.
ENV NODE_OPTIONS=--max-old-space-size=8192

# /work = racine du dépôt montée depuis l'hôte (cf. docker-compose.yml).
WORKDIR /work

CMD ["sleep", "infinity"]
