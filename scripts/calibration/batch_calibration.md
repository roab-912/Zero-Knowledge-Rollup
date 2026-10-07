# Mesure, calibration et validation du batching

Les fichiers de calibration sont regroupés dans `scripts/calibration/` :
`batch_calibration.py`, `test_batch_calibration.py` et ce guide.
`batch_calibration.py` est un outil indépendant : `elasticity.py` et le runtime
du rollup restent inchangés. Les modèles sont des **temps effectifs**, en secondes,
sur une configuration fixe. Les durées ne permettent pas d'identifier séparément
un travail physique W et une capacité R. Les contraintes décrivent le circuit ;
le RSS décrit une occupation mémoire, pas un travail CPU ou un volume d'E/S.

## Commandes

Chaque nouveau lancement de `calibration`, `fit` ou `validation` crée par défaut
un sous-dossier dans le `bench-out/` **à la racine du dépôt**, même si la commande
est lancée depuis un autre répertoire. Le nom inclut l'horodatage UTC, la commande
et un identifiant unique, par exemple `20261006T150000_123456Z_calibration_a1b2c3d4`.
Le chemin est affiché dès le démarrage. Les lancements successifs ne s'écrasent pas.
Chaque dossier contient un **unique fichier de résultats : `result.json`**.
Le manifeste, les mesures brutes, le bilan et les résultats d'analyse sont des
sections de ce fichier, qui est actualisé après chaque essai par remplacement
atomique. `fit` et `validation` utilisent eux aussi ce format.

```sh
python scripts/calibration/batch_calibration.py calibration
python scripts/calibration/batch_calibration.py fit --campaign bench-out/NOM_DU_DOSSIER_CALIBRATION
python scripts/calibration/batch_calibration.py validation --model bench-out/NOM_DU_DOSSIER_FIT/result.json
```

Remplacer les noms de dossiers par ceux affichés aux étapes précédentes.
`--out` permet de choisir un nom explicite sous `bench-out/` ; les chemins
relatifs de cette option sont résolus depuis la racine du dépôt. Un dossier
existant est refusé pour un nouveau lancement ; `--resume --out ...` reprend
une campagne dans son dossier initial. `--resume` sans `--out` est refusé.

Depuis la racine du dépôt, avec Python, NumPy, SciPy, Node/snarkjs et le binaire
natif rapidsnark `prover` dans le PATH, exemple avec des noms explicites
(choisir de nouveaux noms s'ils existent déjà) :

```sh
python scripts/calibration/batch_calibration.py calibration --out bench-out/batch-calibration
python scripts/calibration/batch_calibration.py fit --campaign bench-out/batch-calibration --out bench-out/batch-fit
python scripts/calibration/batch_calibration.py validation --model bench-out/batch-fit/result.json --out bench-out/batch-validation
```

Ces commandes lancent respectivement :

- 10 répétitions et 1 warmup par prover/taille sur `1,4,16,64,256` ;
- sélection, ajustement et gel des modèles et prédictions sur les 10 tailles ;
- 10 nouvelles répétitions et 1 warmup sur `1,2,4,...,512`, avec une autre graine.

Les tailles `2,8,32,128,512` sont réservées à la validation. Aucun résultat
exploratoire sur la forme des courbes de rapidsnark/snarkjs n'est imposé au modèle.
La campagne complète n'est jamais lancée par l'import du module ou par les tests.

Options de collecte : `--provers snarkjs` ou `--provers rapidsnark snarkjs`,
`--sizes 1,4`, `--repeat 10`, `--warmups 1`, `--timeout 600` (par sous-processus),
`--seed 123`, `--sample-interval 0.05`, `--circuits-dir circuits`,
`--snarkjs CHEMIN_EXECUTABLE`, `--rapidsnark CHEMIN_PROVER`.
Les chemins d'exécutables ne sont pas des chaînes de commandes shell. Sous
Windows, le shim npm snarkjs est résolu en `node .../build/cli.cjs`.

Pour reprendre une collecte interrompue, relancer avec les mêmes paramètres,
`--out bench-out/NOM_DU_DOSSIER_EXISTANT` et `--resume`.
Les essais terminés, y compris les échecs, sont conservés ; les essais
interrompus sont rejoués dans de nouveaux dossiers techniques. Les warmups sont
refaits à la reprise et enregistrés dans la section `resume_warmups` du même fichier.
Une campagne gelée ne peut plus être complétée.
Un nouvel environnement ou une nouvelle configuration nécessite une autre campagne.

Pour un premier contrôle réduit :

```sh
python scripts/calibration/batch_calibration.py calibration --out bench-out/batch-small --sizes 1,4 --setup-sizes 1,4 --provers snarkjs --repeat 2 --warmups 0 --timeout 30
python -m unittest discover -s scripts/calibration -p test_batch_calibration.py -v
```

Deux tailles ne suffisent volontairement pas à sélectionner les trois familles :
`fit` exige au moins quatre tailles réussies par série, sinon il écrit
`insufficient_data` sans inventer de recommandation.

### Artefacts et conteneur

Chaque taille doit contenir `circuit_js/circuit.wasm`, `circuit_final.zkey` et
`verification_key.json`, issus d'une compilation/setup cohérents. Les fichiers
`.zkey` ne sont pas versionnés dans ce dépôt. Avant la calibration, les
environnements manquants ou incomplets sont maintenant générés automatiquement
par `generate_missing_environments` du script `scripts/bench/generate_environments.py`.
Cette fonction réutilise les commandes de setup instrumentées de
`measure_zk_resources.py` : Powers of Tau, compilation, clé Groth16, vérifications
de la clé, export des clés de vérification et du contrat.

Par défaut, les 10 tailles de 1 à 512 sont préparées avant les mesures : cela
permet de figer également les artefacts des tailles réservées à la validation.
Pour un essai réduit, limiter **aussi** `--setup-sizes`, comme ci-dessus.
Les options sont `--circom CHEMIN`, `--setup-sizes 1,4`,
`--setup-timeout 21600` (6 heures par commande) et `--no-setup` pour désactiver
la génération. Le timeout des preuves `--timeout` reste distinct.

Les circuits déjà complets sont conservés. Un circuit incomplet est reconstruit
dans `bench-out/<lancement>/setup/` en préservant sa source locale si elle existe.
Après succès du setup, ses artefacts cohérents sont installés dans `--circuits-dir` ;
l'ancien dossier incomplet est archivé sous `.before-setup-<taille>-<identifiant>`.
Les intermédiaires et journaux de setup restent dans le dossier du lancement.
Le temps de préparation et les ressources de chaque commande sont enregistrés
dans la section `setup` du seul `result.json`, hors des temps par batch.

Une erreur de setup arrête la collecte avec `status: setup_failed`. La reprise
réutilise les tailles entièrement générées et recommence la taille incomplète.
La validation ne régénère jamais une clé après le gel : si un artefact figé est
absent ou modifié, il faut restaurer l'original ou effectuer une nouvelle calibration.
Avec `--no-setup`, les essais sans artefacts sont enregistrés `unavailable` ;
un prover absent donne aussi `unavailable`, jamais un TPS nul.
Le setup reprend les contributions expérimentales du dépôt ; il ne constitue pas
une cérémonie de confiance pour un déploiement en production.

Le script existant permet de préparer des artefacts et de mesurer le setup à part,
par exemple pour les petites tailles dans **un nouveau répertoire** :

```sh
python scripts/bench/measure_zk_resources.py --sizes 1,2,4 --work-dir bench-out/batch-artifacts --out-dir bench-out/batch-setup --repeat 1 --prover snarkjs --no-plots --count-constraints
```

Passer ensuite `--circuits-dir bench-out/batch-artifacts` à la calibration et à la
validation. Ce script existant mesure aussi preuve/vérification ; ses résultats
ne sont pas importés dans la nouvelle calibration. Pour une étude complète,
préparer les 10 tailles avant le gel ; les artefacts sont contrôlés par SHA-256.

Sur Windows, exécuter **tout le benchmark dans le conteneur** fourni par le dépôt
permet de mesurer les deux provers sur le même OS et leurs processus enfants via
`/proc`. Avec le conteneur `zk_bench` déjà installé et le dépôt monté sur `/work` :

```powershell
docker start zk_bench
docker exec -w /work zk_bench python3 scripts/calibration/batch_calibration.py calibration
```

Pour les étapes suivantes, utiliser également `docker exec -w /work zk_bench`
avec les commandes `python3 ... fit` puis `python3 ... validation` et les chemins
affichés. La génération manquante et toutes les preuves se déroulent ainsi dans
le même environnement.

Pour créer un nouveau conteneur, exemple PowerShell sans dépendre des chemins du
Compose historique :

```powershell
docker build -t zk-bench:rapidsnark scripts/bench/rapidsnark
docker run --rm -it --hostname zk-calibration --mount "type=bind,source=$($PWD.Path),target=/work" --workdir /work zk-bench:rapidsnark bash
```

Dans ce shell, installer la dépendance d'analyse absente de l'image actuelle :

```sh
apt-get update
apt-get install -y python3-scipy
```

Exécuter ensuite les trois commandes initiales avec `python3`. Le prover de
l'image est `/usr/local/bin/prover`. Conserver le même environnement et les mêmes
variables de parallélisme jusqu'à la validation. Les temps dépendent du système
de fichiers : un bind mount Windows fait partie de la configuration mesurée.
L'outil n'encapsule pas un seul prover dans `docker exec`, dont les compteurs
enfants seraient invisibles depuis l'hôte. Sur Windows natif, `psutil` est
facultatif pour les ressources ; sinon les valeurs manquantes sont explicites.

## Périmètre temporel

### Exécution native sur un serveur Linux

Rapidsnark peut être installé hors du `PATH`. Fournir son chemin à la
calibration **et** à la validation :

```sh
python3 scripts/calibration/batch_calibration.py calibration --no-setup --rapidsnark /home/r24barbi/rapidsnark/package/bin/prover
python3 scripts/calibration/batch_calibration.py validation --model bench-out/NOM_DU_DOSSIER_FIT/result.json --no-setup --rapidsnark /home/r24barbi/rapidsnark/package/bin/prover
```

L'étape `fit` entre ces commandes reste identique. Pour appliquer le même chemin
à toute la chaîne (y compris le lanceur Python pour la grille jusqu'à 8192) :

```sh
export RAPIDSNARK_BIN=/home/r24barbi/rapidsnark/package/bin/prover
```

La priorité est : chemin `--rapidsnark` explicite, `RAPIDSNARK_BIN`, `prover`
dans le `PATH`, puis les installations `rapidsnark/package/bin/prover` et
`rapidsnark/build/prover` à côté du projet, dans le domicile de l'utilisateur
et dans le projet. Un chemin explicitement configuré mais invalide n'est pas
remplacé silencieusement. Sous Linux, le fichier doit être exécutable.
Le chemin retenu est affiché et enregistré avec son empreinte dans le manifeste.

Les commandes natives restent : `snarkjs wtns calculate`, puis le binaire
rapidsnark avec `zkey witness proof public`, ou `snarkjs groth16 prove`, puis
`snarkjs groth16 verify`. Pour snarkjs, la séparation témoin/preuve permet la
mesure de chaque étape au lieu d'une mesure agrégée `groth16 fullprove`.

Une campagne ayant enregistré `rapidsnark: unavailable` doit être relancée
dans un nouveau dossier après correction : `--resume` conserve les essais
déjà enregistrés et interdit de changer l'environnement d'une campagne.

Dans `scripts/bench/generate_proofs.py`, les TPS historiques sont
`N / mean(timings)` : snarkjs utilise `groth16 fullprove` (witness + preuve),
rapidsnark utilise `wtns calculate` puis le prover via Docker. Préparation,
vérification, disponibilité des données et règlement L1 sont exclus.
`measure_zk_resources.py` conserve ce sens pour sa phase `prove`.
`classes/Prover.py` chronomètre également witness + preuve. Ce ne sont pas des
TPS de bout en bout du rollup.

`scripts/bench/optimize_batch_partition.py` sépare déjà `wtns calculate`,
`groth16 prove` pour snarkjs et l'appel du binaire pour rapidsnark. Sa commande
`groth16 verify` est commune aux deux provers, comme dans la calibration.
Ce script étudie le partitionnement et conserve son emplacement dans `scripts/bench/`.
Le générateur et l'échantillonneur de `measure_zk_resources.py`, ainsi que
`Batch`, `State` et `Executor`, restent importés depuis leurs emplacements existants.

Le nouveau mode lance un processus witness puis un processus prover **pour
chaque batch**, y compris pour snarkjs (`groth16 prove` au lieu de `fullprove`).
Il constitue donc une nouvelle série expérimentale, qui ne doit pas être
mélangée aux anciennes mesures de `fullprove` ou à un service persistant.

| Champ | Périmètre, en secondes |
|---|---|
| `preparation_s` | Générateur existant, transactions, Batch, état neuf, Executor, écriture input |
| `witness_s` | Processus snarkjs `wtns calculate`, lectures et écriture `.wtns` incluses |
| `proof_s` | Processus prover, chargement zkey/witness, preuve, écriture des sorties |
| `proof_generation_s` | Somme séquentielle witness + preuve |
| `total_s` | Intervalle monotone contigu préparation → fin de preuve, orchestration incluse |
| `verification_s` | Vérification locale snarkjs séparée, exclue de `total_s` |
| `artifact_load_s` | Manquant : les CLI ne fournissent pas de chronomètre de chargement isolé |
| `rollup_end_to_end_s` | Manquant : ni pool, DA, L1, attente ni règlement dans cette expérience |

Les chargements répétés sont bien compris dans les étapes concernées. On
n'ajoute pas un temps de chargement fictif. Compilation et setup sont hors coût
récurrent. Les mesures utilisent `time.perf_counter`. Le surcoût d'instrumentation
figure dans `total_s` ; celui-ci peut dépasser la somme des durées instrumentées.
Les hashes et la vérification sont calculés après le périmètre principal.

`ZKRollup.compute_batch` possède un verrou empêchant le chevauchement des batches.
Il n'existe donc pas ici de pipeline multi-batch dont on pourrait déduire le
débit par un maximum des temps d'étapes. Le débit rapporté est celui du périmètre
local séquentiel, pas celui d'un service soumis à son ordonnanceur et au L1.

Les warmups réchauffent éventuellement les caches OS, jamais un JIT persistant.
L'inventaire SHA-256 lit également les artefacts avant la collecte : même avec
`--warmups 0`, cette expérience ne prétend pas mesurer un cache disque froid.
L'ordre des warmups puis des essais mesurés est randomisé et enregistré. Un seul
essai est lancé à la fois ; éviter les autres benchmarks sur la même machine.
Les transactions utilisent des couples source/destination disjoints, comme le
générateur existant, avec des montants entiers déterministes de 1 à 1000. L'état
initial est reconstruit. Les deux provers ont les mêmes graines, les mêmes
artefacts et des hashes input/witness identiques pour une répétition donnée.

## Modèles et incertitude

Les trois familles, identiques pour les deux provers, sont `alpha + beta*N`,
`alpha + beta*N + c*N*ln(N)` et `alpha + beta*N + c*N^p`, avec coefficients
non négatifs et `p ∈ {1.25,1.5,1.75,2,2.5,3}`. `alpha` est un temps fixe estimé ;
`alpha/N` est sa contribution amortie par transaction.

L'ajustement minimise les résidus temporels relatifs sur les moyennes par taille.
La sélection minimise l'erreur logarithmique absolue en validation croisée
**leave-one-size-out**, toutes les répétitions de la taille étant exclues ensemble.
À moins de `max(0.02, 10 % du meilleur score)` du meilleur score, priorité à affine,
puis N ln N, puis puissance. Le choix de p fait partie de cette sélection sur la
calibration ; les scores CV servent à sélectionner, pas à estimer sans biais
l'erreur finale, qui provient de la campagne indépendante.

Les prédictions contiennent `N/T(N)`, `1-N*T'(N)/T(N)`, `alpha/N` et le statut
d'extrapolation. La recommandation maximise les TPS sur la grille disposant des
artefacts, excluant les tailles en échec lors de la calibration. Les tailles
encore non mesurées restent des recommandations **prédites**, à valider.
`fit --admissible-sizes 1,4,16,64` permet de restreindre cette grille ;
`--max-time 12` impose une limite de temps prédit, séparément pour chaque périmètre.
Il n'y a pas de conversion automatique RSS → travail ni de plafond mémoire extrapolé.

Un optimum continu exige un passage de xi positif à négatif. Les sorties
distinguent maximum intérieur prédit, borne expérimentale, plateau incertain et
données insuffisantes. 512 est seulement la borne de l'expérience.

Par défaut, 500 bootstraps rééchantillonnent les répétitions **dans chaque taille**.
Les intervalles de mesure sur les TPS sont distincts des intervalles de paramètres
et de prédictions issus de réajustements de calibration. Ces derniers sont
conditionnels à la famille et au p retenus ; ils ne couvrent pas le choix du modèle,
les dérives matérielles ni une mauvaise spécification. Avec une seule répétition,
l'incertitude correspondante est manquante, jamais un intervalle de précision nulle.
Le script signale les coefficients instables, les matrices mal conditionnées et
les résidus dépassant `max(10 % du temps moyen, 3 erreurs standards)`.

La zone optimale prédite comprend les tailles à moins de 2 % du meilleur débit
ou dont les intervalles ponctuels se chevauchent avec celui du meilleur. La zone
observée utilise le chevauchement des intervalles de mesure. Ce sont des zones
descriptives, pas des tests simultanés d'équivalence statistique.

La validation calcule `nombre_total_tx / somme_durées`, les erreurs relatives de
TPS, le meilleur batch observé, la perte de la recommandation, et les sécantes
`ln(TPS(2N)/TPS(N))/ln(2)` pour mesures **et** prédictions. Elle ne réajuste rien.
Les pertes et élasticités observées ont aussi des intervalles bootstrap.
Les TPS sont conditionnés au succès ; un rapport partiel signale les tailles
absentes ou en échec. Une recommandation non mesurée donne une perte manquante.

## Fichier de résultats et limites

`bench-out/<lancement>/result.json` utilise le schéma 2 et contient les sections
suivantes selon l'étape :

| Section | Contenu |
|---|---|
| `manifest` | Campagne, configuration, plan randomisé, graines, environnement et artefacts ; provenance et méthode pour `fit` |
| `trials` | Mesures brutes de chaque essai terminé, y compris les warmups initiaux |
| `resume_warmups` | Warmups supplémentaires lors des reprises |
| `setup` | Étapes, commandes, ressources, journaux et temps total de génération des environnements, hors mesures par batch |
| `summary` | Bilan des statuts, nombre d'essais terminés/prévus et date de mise à jour |
| `model` | Modèles figés, coefficients, diagnostics, prédictions et recommandations (`fit` et `validation`) |
| `validation` | Comparaison indépendante entre nouvelles mesures et prédictions |
| `frozen` | Dans la calibration source : chemin du fichier de résultats de `fit`, son SHA-256 et l'empreinte des données de calibration |
| `status` | `setting_up` pendant la préparation, `setup_failed` si elle échoue, puis `running` et `complete` pour les mesures ; les échecs individuels restent dans `trials` |

Les statuts d'essai sont `success`, `invalid_proof`, `timeout`, `out_of_memory`,
`error` et `unavailable`. Un SIGKILL sans diagnostic explicite d'OOM reste une
erreur non attribuée. Le domaine interne Groth16 est lu dans le header zkey,
sans l'inférer du nombre de contraintes ; ses sauts peuvent être comparés aux résidus.

Le fichier est sauvegardé après chaque essai, hors du périmètre chronométré. La
reprise conserve toutes les entrées existantes et ajoute celles qui manquent.
Si l'écriture d'une nouvelle version échoue, la précédente version complète reste
lisible. La mise à jour réécrit le document entier : le coût de sauvegarde augmente
avec la taille de la campagne, sans entrer dans les temps par batch.

Le gel ajoute une section `frozen` au fichier de la calibration, sans changer ses
mesures. L'empreinte des données porte sur `manifest`, `trials` et `resume_warmups` ;
les métadonnées de gel et le bilan sont exclus de cette empreinte. Le fichier de
résultats de `fit` est ensuite vérifié par SHA-256 avant toute validation.
`--model` désigne donc `bench-out/<lancement_fit>/result.json`.

Les fichiers nécessaires aux outils cryptographiques (`input.json`, witness,
preuve, signaux publics) et les journaux restent dans `attempts/` pour audit :
ce sont les artefacts techniques, distincts du rapport unique `result.json`.
Le fichier `.lock` protège l'accès à une campagne.

Les anciennes sorties multifichiers sont conservées telles quelles. Leurs modèles
figés restent lisibles pour contrôle d'intégrité ; leur reprise de collecte n'est
pas prise en charge par ce nouveau format. Créer une nouvelle campagne pour les
mesures au format unifié ; les contrôles d'environnement restent applicables.

L'échantillonneur existant couvre les processus enfants visibles avec `/proc`
ou `psutil`. CPU et volumes E/S sont les derniers compteurs vus par PID ; RSS est
le maximum échantillonné de la somme des RSS. Les processus courts, derniers
incréments et pics entre échantillons peuvent être manqués ; la mémoire partagée
peut être comptée plusieurs fois. Les valeurs indisponibles sont `null` avec
raison et méthode. Le CPU Python de préparation est mesuré séparément, sans
prétendre mesurer son RSS/E/S par phase. Conserver le même intervalle de mesure.

Les entrées/sorties et witnesses sont conservés pour audit : prévoir l'espace
disque nécessaire. Le gel est logique avec contrôle de hashes, pas une protection
contre un utilisateur modifiant volontairement tous les fichiers.
# Analyse de la théorie et figures

Après une validation terminée, utiliser [batch_theory.py](batch_theory.py),
documenté dans [batch_theory.md](batch_theory.md), pour calculer les élasticités,
la taille optimale sous un budget Δ = 10 s, les erreurs théorie–mesures et
générer les figures PNG/PDF. Cette analyse produit son propre `result.json`
dans un nouveau sous-dossier de `bench-out` et conserve les campagnes sources.

