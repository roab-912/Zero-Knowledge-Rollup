# Benchmark ressources zk-SNARK

- Date : 2026-11-04T15:16:27
- Machine : Linux-6.12.107+deb13-amd64-x86_64-with-glibc2.41, 32 cœurs, 128.0 GiB de RAM
- Backend de mesure : proc
- Prover principal : snarkjs
- rapidsnark : binaire natif : /home/r24barbi/Zero-Knowledge-Rollup/rapidsnark/package/bin/prover
- Tailles mesurées : 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192
- Répétitions preuve/vérification : 10

## Distributions RAM

Moyenne, médiane, P05/P25/P75/P95/P99 et écart-type temporels pondérés sont disponibles dans `steps.csv` et `memory_repetitions.csv`. Leurs résumés entre répétitions sont dans `phases.csv`, avec les effectifs valides. Les traces `raw/samples/` sont conservées. Les valeurs indisponibles sont vides ; `sparse` signale une seule observation positive. Le pic reste utilisé dans les figures de dimensionnement ci-dessous.

## Moyennes par phase

| n | phase | durée (s) | CPU (s) | cœurs occupés | RSS max (MiB) | lecture (MiB) | écriture (MiB) |
|---|-------|-----------|---------|---------------|---------------|---------------|----------------|
| 1 | prove | 0.829 | 2.668 | 3.22 | 365.0 | 0.0 | 0.0 |
| 1 | verify | 0.599 | 2.257 | 3.77 | 347.4 | 0.0 | 0.0 |
| 2 | prove | 0.840 | 2.701 | 3.22 | 374.2 | 0.0 | 0.0 |
| 2 | verify | 0.597 | 2.248 | 3.76 | 346.5 | 0.0 | 0.0 |
| 4 | prove | 0.863 | 2.799 | 3.24 | 391.8 | 0.0 | 0.0 |
| 4 | verify | 0.596 | 2.238 | 3.76 | 346.9 | 0.0 | 0.0 |
| 8 | prove | 0.872 | 2.930 | 3.36 | 427.8 | 0.0 | 0.0 |
| 8 | verify | 0.590 | 2.225 | 3.77 | 345.8 | 0.0 | 0.0 |
| 16 | prove | 0.904 | 3.101 | 3.43 | 489.7 | 0.0 | 0.0 |
| 16 | verify | 0.598 | 2.289 | 3.83 | 346.8 | 0.0 | 0.0 |
| 32 | prove | 0.970 | 3.527 | 3.64 | 605.1 | 0.0 | 0.0 |
| 32 | verify | 0.596 | 2.267 | 3.81 | 347.3 | 0.0 | 0.0 |
| 64 | prove | 1.076 | 4.181 | 3.89 | 817.0 | 0.0 | 0.2 |
| 64 | verify | 0.592 | 2.265 | 3.83 | 347.2 | 0.0 | 0.0 |
| 128 | prove | 1.283 | 5.417 | 4.22 | 1209.4 | 0.0 | 0.1 |
| 128 | verify | 0.596 | 2.250 | 3.78 | 346.6 | 0.0 | 0.0 |
| 256 | prove | 1.636 | 7.557 | 4.62 | 1473.9 | 0.0 | 0.3 |
| 256 | verify | 0.586 | 2.254 | 3.85 | 347.2 | 0.0 | 0.0 |
| 512 | prove | 2.319 | 11.516 | 4.97 | 1834.2 | 0.0 | 0.3 |
| 512 | verify | 0.590 | 2.253 | 3.82 | 347.1 | 0.0 | 0.0 |
| 1024 | prove | 3.620 | 19.423 | 5.37 | 2935.4 | 0.0 | 3.7 |
| 1024 | verify | 0.590 | 2.245 | 3.80 | 346.6 | 0.0 | 0.0 |
| 2048 | prove | 6.392 | 35.322 | 5.53 | 4297.6 | 0.0 | 8.7 |
| 2048 | verify | 0.590 | 2.271 | 3.85 | 346.7 | 0.0 | 0.0 |
| 4096 | prove | 12.221 | 65.112 | 5.33 | 8162.2 | 0.0 | 18.6 |
| 4096 | verify | 0.596 | 2.177 | 3.65 | 346.2 | 0.0 | 0.0 |
| 8192 | prove | 25.909 | 130.651 | 5.04 | 11280.2 | 0.0 | 39.6 |
| 8192 | verify | 0.585 | 2.231 | 3.81 | 347.4 | 0.0 | 0.0 |

## Tables LaTeX

Prêtes à insérer dans l'article (adapter caption/label) :

- `tables/tab1_resources.tex`
- `tables/tab2_artifacts.tex`
