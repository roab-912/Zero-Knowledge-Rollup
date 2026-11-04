# Benchmark ressources zk-SNARK

- Date : 2026-11-04T16:54:21
- Machine : Windows-11-10.0.26200-SP0, 8 cœurs, 0.0 GiB de RAM
- Backend de mesure : none
- Prover principal : snarkjs
- rapidsnark : indisponible (désactivé)
- Tailles mesurées : 512, 8192
- Répétitions preuve/vérification : 3

## Distributions RAM

Moyenne, médiane, P05/P25/P75/P95/P99 et écart-type temporels pondérés sont disponibles dans `steps.csv` et `memory_repetitions.csv`. Leurs résumés entre répétitions sont dans `phases.csv`, avec les effectifs valides. Les traces `raw/samples/` sont conservées. Les valeurs indisponibles sont vides ; `sparse` signale une seule observation positive. Le pic reste utilisé dans les figures de dimensionnement ci-dessous.

## Moyennes par phase

| n | phase | durée (s) | CPU (s) | cœurs occupés | RSS max (MiB) | lecture (MiB) | écriture (MiB) |
|---|-------|-----------|---------|---------------|---------------|---------------|----------------|

## Tables LaTeX

Prêtes à insérer dans l'article (adapter caption/label) :

- `tables/tab2_artifacts.tex`
