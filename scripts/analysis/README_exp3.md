# Expérience 3 : durées, étapes dominantes et croisements

Le collecteur reste `scripts/bench/measure_zk_resources.py`. Par défaut, il
exécute maintenant deux commandes distinctes pour snarkjs : `wtns calculate`
pour le witness puis `groth16 prove` pour la preuve seule.

- `--split-prove` conserve explicitement ce comportement par défaut. Cela lance
  deux processus et change le protocole par rapport à `fullprove` de l'Exp 1.
- `--fullprove` permet de reproduire l'ancien protocole combiné, sans
  `--rollup-local`. Ces mesures ne permettent pas de séparer les deux durées.
- `--rollup-local` mesure quinze opérations locales : création des transactions,
  insertion/sélection/affectation SQLite, construction du batch, copie de l'état,
  calcul du nouvel état, entrées du circuit, hash avant/après, sérialisation et
  compression DA, écritures JSON et blob, puis snapshot. Ces durées utilisent
  les classes du rollup (`TxPool`, `Executor`, `State`, `Transaction`, `Batch`, `DataAvailability`) et le même
  sérialiseur d'entrées que `Prover`.

Le worker ne démarre ni API, ni séquenceur, ni client L1. Il écrit uniquement
dans le dossier de résultats. Les fixtures ont N transferts indépendants,
2N comptes par défaut, un montant de 1 et les mêmes soldes que le circuit synthétique.
`--rollup-state-accounts M` fixe la taille de l'état global, avec M >= 2*max(N).
Le collecteur vérifie la concordance des entrées avant de lancer les preuves.
La création de l'état initial et les imports sont hors chronométrage local.
Les logs de l'Executor sont inclus, conformément à son comportement actuel.
Les identifiants/timestamps des transactions sont fixes pour rendre la
compression reproductible ; ce workload ne reproduit pas une charge Ethereum.

## Collecter une nouvelle campagne complète

Sur la machine de benchmark avec les circuits et les outils installés :

```bash
python scripts/bench/measure_zk_resources.py --max-exponent 13 --skip-setup --circuits-dir circuits --prover both --rapidsnark-mode docker --require-rapidsnark --repeat 10 --rollup-local --sample-interval 0.01 --out-dir bench-out/exp3/measurements
python scripts/analysis/exp3.py --from bench-out/exp3/measurements --outdir bench-out/exp3/figures
```

Pour un binaire natif, remplacer la configuration Docker par
`--rapidsnark-mode native --rapidsnark-bin /chemin/vers/prover`.
Retirer `--skip-setup` pour générer de nouveaux circuits.
Les commandes conviennent aussi à PowerShell en gardant chaque commande sur
une ligne. Sous Windows, installer `psutil` pour les compteurs du worker externe ;
sinon ses compteurs indisponibles ne servent pas à l'analyse. Les horloges CPU
locales peuvent être trop grossières pour les petites opérations : une valeur
nulle ne signifie pas que l'opération ne consomme pas de CPU.

Les opérations locales utilisent des horloges internes haute résolution pour
la durée et le temps CPU, après imports/initialisation. `local_steps.csv` les
contient et `raw/rollup_local/` conserve leurs JSON et fichiers. `steps.csv`
garde les mesures de commandes (avec démarrage du processus/conteneur), ainsi
que les mesures globales du worker. Les compteurs RAM/I/O du worker incluent
son initialisation et ne sont pas attribués à ses opérations internes.

## Vue regroupée : six courbes par prouveur

Par défaut, `exp3.py` regroupe les étapes locales et garde witness, proof et
verify séparés. Les deux prouveurs restent dans des panneaux distincts.

| Courbe | Étapes incluses |
| --- | --- |
| Préparation du batch | Création des transactions, insertion/sélection SQLite, construction et affectation du batch |
| Computation de l'état | Copie, exécution des transactions, hash avant/après, préparation des entrées du circuit |
| Witness | Calcul du témoin seul |
| Proof | Génération de preuve seule |
| Verify | Vérification locale |
| DA + persistance | Sérialisation, compression, écritures JSON/blob et snapshot de l'état |

Chaque étape appartient à un seul groupe. Les durées, le travail CPU et les
I/O sont additionnés **par répétition**, puis les moyennes et écarts-types
sont calculés. Le RSS utilise le maximum des étapes séquentielles.
La somme des temps est conservée. Le maximum et les transitions sont calculés
entre les groupes ; chaque groupe est supposé exécuté séquentiellement.
Le groupe DA inclut les coûts locaux de stockage, pas la publication L1.
Une ancienne mesure `fullprove` reste combinée et n'invente pas de witness.

`Batch preparation` conserve son nom et inclut l'ingestion dans la txpool.
Le pool réutilise maintenant une connexion WAL, avec une transaction atomique
par `add()` (insertion + hash final) et `synchronous=NORMAL`. Les appels restent
individuels dans le benchmark et dans l'API. Une nouvelle collecte est nécessaire
pour mesurer cette optimisation ; relancer uniquement `exp3.py` conserve les
durées de l'ancienne implémentation.

Il suffit de relancer l'analyse sur une collecte détaillée existante :

```bash
python scripts/analysis/exp3.py --from bench-out/exp3/full_workflow --outdir bench-out/exp3/full_workflow/figures
```

Les figures principales affichent au plus six phases, plus l'enveloppe du
maximum sur la figure des durées. `stage_metrics.csv` conserve les étapes
détaillées ; `grouped_metrics.csv` contient les agrégats. `dominance.csv` et
`transitions.csv` correspondent à la vue choisie, enregistrée dans le manifeste.
Les diagnostics preuve/vérification et état global gardent leurs quelques
courbes spécialisées.

Pour retrouver toutes les courbes individuelles :

```bash
python scripts/analysis/exp3.py --from bench-out/exp3/full_workflow --outdir bench-out/exp3/full_workflow/figures_detailed --view detailed
```

## Exploiter le dataset existant

```bash
python scripts/analysis/exp3.py --from bench-out/20260825_120620 --outdir bench-out/exp3/legacy
```

Les PNG/PDF sont déjà générés dans `bench-out/exp3/legacy/` :

- `01_stage_times` : durées moyennes, écart-type population et maximum des groupes (ou étapes avec `--view detailed`) ;
- `02_stage_dominance` : durées normalisées par le maximum, avec étapes dominantes ;
- `03_derived_rates` : comparaison des estimations séquentielle et pipeline idéal ;
- `04_resources_*` : CPU, RAM et I/O disponibles, sans les convertir arbitrairement en durées ;
- `05_proving_vs_verify` : courbes `proof`, `witness` et `verify` ; la courbe
  `proof` contient uniquement `groth16 prove`. Un ancien dataset `fullprove`
  reste combiné et explicitement nommé `proof (fullprove)`.
- `06_state_processing` : copie de l'état global, calcul du nouvel état, hash
  de l'état précédent et du nouvel état.
- `resource_inventory.csv` : inventaire des étapes, fonctions, ressources
  sollicitées et statut de collecte.

L'[inventaire détaillé](RESOURCE_INVENTORY.md) précise les ressources physiques,
les contraintes L1 et les opérations encore non instrumentées.

Le style reprend `exp1/First Result` : couleurs bleu rapidsnark, rose snarkjs,
vert pointillé pour la vérification, marqueurs de taille 3, bandes d'écart-type
à 12 %, grille discrète, titres de panneaux à gauche et légendes communes
sous les figures. Les valeurs par défaut sont une largeur de 8,5 pouces,
une police de 8 points et 300 dpi. Les options `--fig-width`, `--fig-font-size`,
`--fig-dpi` et `--fig-format png pdf svg` permettent d'adapter l'export.

`stage_metrics.csv`, `dominance.csv`, `transitions.csv`,
`proving_comparison.csv`, `proving_transitions.csv` et `manifest.json`
permettent de réutiliser les valeurs. Une répétition de preuve échouée ou
incomplète est entièrement écartée. Le calcul des écarts-types de la phase
complète somme d'abord les étapes par répétition, comme dans l'Exp 1.
Les vérifications restent associées à leur backend ; elles ne sont pas poolées.

Le dataset historique donne pour rapidsnark :

| Comparaison avec la vérification | Intervalle du croisement observé |
| --- | --- |
| Témoin + preuve, comme la phase de l'Exp 1 | 512 à 1024 |
| Génération de preuve seule | 1024 à 2048 |

Les croisements entre deux courbes sont distingués des changements de l'étape
dominante : un croisement sous une troisième courbe n'est pas un changement
du maximum. Les égalités sans inversion ne sont pas des transitions.
Les intervalles ne constituent ni une estimation de l'entier exact du
croisement, ni un intervalle de confiance. L'ancien `fullprove` snarkjs reste
combiné ; ses sous-durées ne peuvent pas être reconstituées.

## Vérifier uniquement les opérations locales

```bash
python scripts/bench/measure_zk_resources.py --rollup-local-only --sizes 1,2,8,512 --repeat 3 --sleep-between 0 --no-plots --out-dir bench-out/exp3/local-check
python scripts/analysis/exp3.py --from bench-out/exp3/local-check --outdir bench-out/exp3/local-check-figures
```

Ce contrôle fonctionne sans outils SNARK. Il ne doit pas être fusionné avec
les anciennes mesures faites sur une autre machine pour en déduire un goulot.
Utiliser la campagne complète pour comparer les opérations sur la même machine.

## Interprétation dans le papier

Ces figures identifient des **étapes dominantes en temps écoulé**. Une étape
peut partager le CPU ou le disque avec une autre. Elles n'identifient pas à
elles seules la ressource physique limitante x du modèle.
`N/max(T_étape)` suppose des étapes indépendantes pouvant se chevaucher ;
`N/somme(T_étape)` est une estimation séquentielle sur ces microbenchmarks.
Le rollup actuel verrouille toute la méthode `compute_batch`, y compris
l'attente de settlement : il ne réalise pas ce pipeline idéal.
Le budget mémoire est une contrainte d'occupation, pas une durée de service.

Pour inclure une politique de publication, utiliser par exemple
`--release-period-s 12`. C'est une hypothèse explicite de l'analyse.
La vérification mesurée est locale via snarkjs. Le benchmark ne mesure pas
le gas, le temps de vérification EVM, le réseau, la construction KZG ou la
finalité. Les scripts `measure_batch_cost.py` et
`measure_settlement_latency.py` couvrent déjà certains coûts et délais L1.
La latence d'inclusion ne peut pas être assimilée à une capacité de service.
Pour valider les goulots physiques du modèle, il faut des capacités/allocation
explicites et une instrumentation de l'exécution réelle, avec mesure du
chevauchement et des ressources partagées.

## Validation des scripts

```bash
python -m unittest discover -s scripts/analysis -p 'test_exp*.py'
python -m unittest discover -s scripts/bench -p 'test_*.py'
```
