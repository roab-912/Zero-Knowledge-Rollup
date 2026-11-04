# Inventaire du workflow et des ressources du rollup

Périmètre : les chemins récurrents présents dans `Sequencer`, `TxPool`,
`Executor`, `Prover`, `DataAvailability`, `L1Client` et `ZKRollup`.
Ce périmètre permet de lister les opérations du rollup actuel ; il ne constitue
pas un inventaire universel des ressources de toutes les architectures de rollup.

Chaque nouvelle collecte produit `resource_inventory.csv`, également exporté
par `exp3.py`. Il liste les étapes, leur fonction source, leurs ressources
potentielles et leur statut `measured` / `not_measured`. **Le statut décrit le
chronométrage de l'étape, pas la disponibilité de tous ses compteurs physiques.**

## Calcul du nouvel état global

Dans cette implémentation, `State` conserve un dictionnaire de soldes.
L'exécution d'un batch applique ses transferts à une copie du dictionnaire.
`State.hash()` trie et sérialise l'état entier puis calcule son SHA-256.
Il ne s'agit pas d'une mise à jour d'arbre de Merkle.

Les quatre courbes suivantes sont séparées par le protocole local v2 :

| Mesure | Fonction appelée | Ce qu'elle contient |
| --- | --- | --- |
| `state_copy` | `State.copy()` | Copie des soldes globaux |
| `compute_batch` | `Executor.compute_batch()` | Validation, application des N transactions et logs actuels de l'Executor |
| `state_hash_before` | `State.hash()` | Empreinte complète de l'état précédent |
| `state_hash_after` | `State.hash()` | Empreinte complète du nouvel état |

Le nombre global de comptes M est distinct du nombre N de transactions.
Par défaut M=2N, comme dans l'ancien microbenchmark. Pour étudier N à état
constant, fixer M avec `--rollup-state-accounts M`, avec M >= 2*max(N).
Les comptes supplémentaires sont initialisés au même solde, mais ne participent
pas aux transferts. M est conservé pour chaque mesure dans `local_steps.csv`
et `stage_metrics.csv`. La création de l'état initial est hors chronométrage,
comme l'initialisation de la base SQLite et les imports Python.

## Étapes chronométrées

`--rollup-local` ajoute les quinze opérations locales suivantes aux trois
étapes SNARK. `--rollup-local-only` permet de les mesurer sans outils SNARK.

| Étape | Ressources principalement sollicitées | Mode |
| --- | --- | --- |
| Construction des transactions + hash | CPU, mémoire | local |
| Insertion des transactions dans la txpool | CPU, mémoire, disque SQLite/WAL | local |
| Sélection et marquage des transactions | CPU, mémoire, disque SQLite/WAL | local |
| Construction du batch | CPU, mémoire | local |
| Attribution du batch_id dans la txpool | CPU, disque | local |
| Copie de l'état global | CPU, mémoire | local |
| Calcul du nouvel état | CPU, mémoire, logs | local |
| Préparation/écriture des entrées du circuit | CPU, mémoire, disque | local |
| Hash de l'état précédent | CPU, mémoire | local |
| Hash du nouvel état | CPU, mémoire | local |
| Sérialisation du payload DA | CPU, mémoire | local |
| Compression zlib du payload DA | CPU, mémoire | local |
| Écriture du JSON DA | CPU, mémoire, disque | local |
| Écriture du payload blob | CPU, disque | local |
| Snapshot persistant | CPU, mémoire, disque | local |
| Calcul du witness | CPU, mémoire, disque | SNARK |
| Génération de preuve Groth16 | CPU, mémoire, disque | SNARK |
| Vérification locale | CPU, mémoire, disque | SNARK |

La base isolée contient exactement N transactions en attente, avec des
émetteurs uniques. Le benchmark mesure les méthodes réelles de `TxPool` sans
démarrer l'API du séquenceur. Il ne couvre pas la contention entre producteurs,
une grande file d'attente préexistante, les doublons d'émetteurs ou la remise en
attente après arrondi à une puissance de deux. La fermeture explicite des
connexions SQLite fait partie du comportement courant.

La construction des transactions et leur insertion représentent le travail
d'ingestion associé à N transactions ; elles ne sont pas exécutées à cet
endroit dans le traitement d'un batch déjà constitué. La somme des étapes
reste une estimation de travail séquentiel, pas une trace de latence réelle.
Les hash internes à la publication DA et au snapshot restent dans leurs
propres durées, comme les appels répétés du rollup actuel.

Les écritures utilisent le buffering et les options SQLite de l'implémentation.
Les fichiers DA et snapshot ne sont pas synchronisés par `fsync` : leurs
durées ne mesurent donc pas la fin d'une écriture durable sur le support.

## Autres opérations présentes, non instrumentées par ce microbenchmark

| Opération | Ressource ou quantité concernée | Emplacement |
| --- | --- | --- |
| Requête HTTP, parsing, validation API | CPU, réseau, latence API | `Sequencer.setup_routes` |
| Attente du batch, file d'attente et verrou | Politique de publication, attente | `ZKRollup._run_batch_loop`, `compute_batch` |
| Archivage de la preuve | CPU, disque | `Prover.__store_proof__` |
| Lecture du résultat public | CPU, disque | `Prover.__read_public_file__` |
| Export Solidity calldata + encodage ABI | CPU, mémoire, disque | `Prover.submit_on_chain` |
| Padding/encodage en blob | CPU, mémoire | `L1Client.encode_payload_to_blob` |
| Commitment KZG | CPU, mémoire | `L1Client.build_blob_artifacts` |
| Preuve KZG + versioned hash | CPU, mémoire | `L1Client.build_blob_artifacts` |
| Lecture du nonce, estimation de gas | Réseau RPC, latence | `L1Client.send_blob_call` |
| Signature et sérialisation de la transaction L1 | CPU, mémoire | `L1Client.send_blob_call` |
| Envoi transaction + blob | Réseau, CPU | `L1Client.send_blob_call` |
| Attente du receipt | Latence de propagation/inclusion | `L1Client.send_blob_call` |
| Vérification EVM et mise à jour L1 | Gas, capacité de bloc | `Rollup.submitBatch` |
| Traitement du receipt et décodage des événements | CPU, mémoire | `Prover.submit_on_chain` |
| Compression ZIP d'une époque | CPU, disque, coût périodique | `DataAvailability.__compress_epoch_batch__` |
| Commit des références d'état en mémoire | CPU, mémoire | `ZKRollup._compute_batch_locked` |
| Compilation / trusted setup / clés | CPU, mémoire, disque, coût initial | phase `env` du collecteur |

Le setup possède déjà ses mesures de commandes lorsque `--skip-setup` est
absent ; il est exclu des courbes de traitement récurrent. Le rollup actuel
attend un receipt ; une politique de finalité ou de confirmations supplémentaires
constitue une configuration supplémentaire à instrumenter.
Les scripts `measure_batch_cost.py` et `measure_settlement_latency.py`
fournissent déjà des mesures de gas ou d'inclusion, distinctes de ces timers.
Le benchmark local n'effectue aucune soumission de transaction L1.

## Ressources physiques, contraintes et délais

| Catégorie | Mesures utiles | Interprétation |
| --- | --- | --- |
| CPU | secondes-cœur, temps écoulé, affinité/allocation | Le CPU est partagé entre plusieurs étapes |
| Mémoire | pic RSS, occupation pendant une étape, budget | Stock d'occupation ; ne se transforme pas directement en temps de service |
| Stockage | octets/IOPS lus-écrits, durée, sync, budget disque | Les caches et le buffering influencent les mesures |
| Réseau / RPC | octets transférés, débit, latence, attente RPC | Débit et latence sont des quantités distinctes |
| Exécution L1 | gas consommé, budget gas disponible | Unités de travail et contrainte protocolaire, pas une durée CPU locale |
| Publication DA / blobs | octets compressés, nombre de blobs, budget de publication | Capacité de publication et contrainte protocolaire |
| Attente / inclusion / publication | intervalle de release, queueing, receipt | Délais et politique d'exploitation, pas des ressources physiques |

Les opérations locales exportent durée et temps CPU internes. Leur RSS et
leurs I/O par opération ne sont pas échantillonnés ; les compteurs du worker
englobent ses imports et fixtures et ne doivent pas leur être attribués.
Pour les commandes SNARK, les compteurs du collecteur restent disponibles
selon le système et le backend ; les mesures Docker de portée système ne
décrivent pas exclusivement le processus prouveur.

Il est possible d'observer le changement de l'étape ayant le plus grand temps.
Pour conclure à un changement de ressource physique limitante, il faut aussi
contrôler ses allocations/capacités et les ressources partagées. Le rollup
verrouille le traitement d'un batch et ne réalise pas le pipeline idéal
impliqué par N/max(T). Une latence d'inclusion seule ne donne pas un débit L1.

## Exemple à état global constant

```bash
python scripts/bench/measure_zk_resources.py --skip-setup --max-exponent 13 --prover both --rollup-local --rollup-state-accounts 16384 --repeat 10 --out-dir bench-out/exp3/full_workflow
python scripts/analysis/exp3.py --from bench-out/exp3/full_workflow --outdir bench-out/exp3/full_workflow/figures
```

`01_stage_times` affiche par défaut six grandes phases : préparation du batch,
computation de l'état, witness, proof, verify et DA/persistance. Les durées
des étapes de chaque groupe sont additionnées par répétition avant le calcul
des statistiques ; aucune étape n'est éliminée du total. `--view detailed`
permet de comparer toutes les étapes individuellement.
`stage_metrics.csv` garde les détails ; `grouped_metrics.csv` fournit les groupes.
`06_state_processing` détaille la copie, le calcul de l'état et ses empreintes.
`resource_inventory.csv` indique les mesures réellement présentes dans le run.
Les versions locales v1 et v2 sont reconnues, mais ne sont pas mélangées :
la v1 inclut la copie dans `compute_batch` et regroupe plusieurs opérations DA.
