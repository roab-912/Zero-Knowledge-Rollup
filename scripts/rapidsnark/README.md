# Bench zk en conteneur (rapidsnark, circom, snarkjs)

Ce dossier fournit de quoi exécuter `scripts/measure_zk_resources.py` sur un
poste Windows équipé de WSL 2 et Docker Desktop, avec rapidsnark disponible.

## Pourquoi exécuter le script *dans* le conteneur

`measure_zk_resources.py` attribue CPU, RSS et I/O au processus mesuré en lisant
`/proc/<pid>`. Deux conséquences :

- lancé depuis Windows, il n'a ni `/proc` ni `resource.getrusage()` : seules les
  durées restent exploitables ;
- si seul rapidsnark est conteneurisé et appelé par `docker exec`, l'arbre de
  processus est invisible depuis l'hôte. Le script le sait et bascule sur
  `scope="system"` (compteurs de toute la machine), ce qui suffit pour un ordre
  de grandeur mais pas pour une mesure de RAM par phase.

En exécutant le script dans le conteneur, le prover est un processus enfant :
toutes les phases sont mesurées en `scope="process"`.

L'image contient donc rapidsnark, circom, snarkjs et le runtime Python
(numpy/matplotlib) du script. Le prover est installé dans le `PATH`
(`/usr/local/bin/prover`), ce qui suffit à `--rapidsnark-mode auto`.

## Utilisation

Depuis la racine du dépôt, sous PowerShell :

```powershell
# construit l'image (compilation de rapidsnark : plusieurs minutes la 1re fois),
# démarre le conteneur et lance le bench
.\scripts\rapidsnark\bench.ps1 --sizes 1,2,4,8 --skip-setup --repeat 5 --prover both --require-rapidsnark
```

Ou directement avec docker compose :

```powershell
docker compose -f scripts\rapidsnark\docker-compose.yml up -d --build
docker compose -f scripts\rapidsnark\docker-compose.yml exec bench `
    python3 scripts/measure_zk_resources.py --sizes 1,2,4,8 --skip-setup --repeat 5 `
        --prover both --require-rapidsnark
```

Le dépôt est monté sur `/work` : les sorties `bench-out/<horodatage>/` arrivent
directement sur l'hôte, et les circuits existants de `circuits/` sont réutilisés
par `--skip-setup`.

Arrêt : `docker compose -f scripts\rapidsnark\docker-compose.yml down`.

Les chemins passés au script sont ceux du conteneur (`/work/...`, `/bench/...`).
Sous Git Bash, MSYS réécrit les arguments qui ressemblent à des chemins Unix
(`/bench/x` devient `C:/Program Files/Git/bench/x`) : utiliser PowerShell, ou
préfixer par `MSYS_NO_PATHCONV=1`.

## Petits circuits : affiner l'échantillonnage

Sur `n=1..4`, rapidsnark produit la preuve en ~40 ms, soit moins que la période
d'échantillonnage par défaut (`--sample-interval 0.1`) : aucun échantillon ne
tombe pendant l'exécution et la colonne RSS ressort à 0. La durée et le CPU,
mesurés par `wall`/`rusage`, restent justes. Pour obtenir aussi la RAM sur ces
tailles :

```powershell
.\scripts\rapidsnark\bench.ps1 --sizes 1,2,4 --skip-setup --repeat 10 --prover both --sample-interval 0.005
```

## Mesures d'I/O et bind mount Windows

`../..` est un bind mount de `D:\...` : il passe par drvfs et ses débits disque
ne sont pas ceux du disque. Le CPU et la RAM ne sont pas affectés, mais pour un
run où les colonnes I/O comptent, travailler sur le volume Linux `/bench` :

```powershell
# cycle complet (setup compris) sur le système de fichiers du conteneur
docker compose -f scripts\rapidsnark\docker-compose.yml exec bench `
    python3 scripts/measure_zk_resources.py --sizes 1,2,4 --work-dir /bench/circuits `
        --repeat 5 --prover both --require-rapidsnark

# ou, avec --skip-setup, y recopier d'abord les circuits du dépôt
docker compose -f scripts\rapidsnark\docker-compose.yml exec bench `
    cp -r /work/circuits/. /bench/circuits/
```

## Versions épinglées

| Outil      | Version   | Build arg           |
|------------|-----------|---------------------|
| rapidsnark | `main`    | `RAPIDSNARK_REF`    |
| circom     | `v2.2.2`  | `CIRCOM_VERSION`    |
| snarkjs    | `0.7.5`   | `SNARKJS_VERSION`   |
| gmp        | `6.3.0`   | `GMP_VERSION`       |

snarkjs est épinglé sur la version qui a produit les circuits du dépôt :
changer de version changerait les temps mesurés sans que le bench le signale.

```powershell
docker compose -f scripts\rapidsnark\docker-compose.yml build --build-arg SNARKJS_VERSION=0.7.6
```

## Variante historique : `setup.sh`

`setup.sh` crée le conteneur `debian_rapidsnark` attendu par
`scripts/generate_proofs.py` et par `--rapidsnark-mode docker` — circuits montés
sur `/mnt/projet`, prover sur `mnt/projet/rapidsnark/package/bin/prover`. Il
s'utilise depuis une distribution WSL où le client docker est disponible
(intégration WSL activée dans Docker Desktop) :

```bash
bash scripts/rapidsnark/setup.sh
```

Cette variante reste utile pour `generate_proofs.py`, mais mesure la phase de
preuve en `scope="system"` : préférer le conteneur `zk_bench` ci-dessus pour les
chiffres de RAM et de CPU.
