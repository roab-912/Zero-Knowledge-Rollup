# Optimisation de l'ingestion SQLite

Le groupe **Batch preparation**, ses composants et les six courbes de l'Exp 3
sont conservés. Les données de `full_workflow` n'ont pas été réécrites.

## Changement mesuré

L'ancien `TxPool.add()` ouvrait/fermait une connexion pour chaque transaction
et exécutait INSERT puis UPDATE du hash en autocommit séparé.
Le nouveau pool conserve une connexion WAL protégée par un verrou réentrant.
INSERT et UPDATE sont validés ensemble par une transaction atomique. Les
paramètres WAL et `synchronous=NORMAL` sont conservés. Le chemin testé appelle
toujours `add(tx)` N fois, comme l'API et le benchmark, sans insertion en bloc
ni report des commits. Le pool se ferme explicitement via `close()` ou son
gestionnaire de contexte.

Les opérations SQL d'une instance sont sérialisées par le verrou ; plusieurs
instances accèdent au même fichier avec la coordination SQLite. Les tests
couvrent plusieurs producteurs, consommateurs et instances, ainsi que les
échecs de calcul/mise à jour du hash : aucun INSERT partiel n'est visible.

## Comparaison locale

Mesures faites sur cette machine Windows, trois répétitions par taille, avec
des bases neuves et le même workload avant/après. Création des transactions,
initialisation et contrôles sont hors du timer d'insertion. L'ordre des deux
implémentations alterne selon les répétitions.

| Transactions | Ancien : moyenne (s) | Nouveau : moyenne (s) | Nouveau avec fermeture (s) | Accélération de l'insertion |
| --- | ---: | ---: | ---: | ---: |
| 512 | 6,250 | 0,0588 | 0,0653 | ×106,3 |
| 8192 | 99,064 | 1,0213 | 1,0310 | ×97,0 |

Les douze échantillons sont dans `samples.csv` ; `comparison.json` contient
les écarts-types, versions et empreintes SHA256 des sources. Chaque échantillon
vérifie les comptes, les IDs et les hash après fermeture du writer.
`before_TxPool.py` conserve l'implémentation précédente.

Cette comparaison ne mesure ni HTTP, ni preuve, ni débit concurrent du rollup.
Elle n'est pas directement comparable aux 15,77 s du précédent serveur Linux.
Les données de l'ancien serveur doivent être recollectées avec le code modifié
pour déterminer le nouveau maximum entre Batch preparation et rapidsnark.
Les réglages de synchronisation sont identiques ; ce test n'évalue pas les
garanties en cas de panne électrique.

## Reproduction

```bash
python scripts/bench/compare_txpool_ingestion.py --baseline-source bench-out/exp3/txpool_optimization/before_TxPool.py --sizes 512,8192 --repeat 3 --outdir bench-out/exp3/txpool_optimization_recheck
```

Pour l'Exp 3 complète, relancer `measure_zk_resources.py` dans un nouveau
dossier, puis `exp3.py`. Une simple régénération des figures de l'ancien run
continue de représenter les anciennes durées mesurées.
