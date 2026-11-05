# Benchmark ressources zk-SNARK

- Date : 2026-11-05T08:04:24
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
| 1 | prove | 0.827 | 2.646 | 3.20 | 365.6 | 0.0 | 0.0 |
| 1 | verify | 0.581 | 2.244 | 3.86 | 346.3 | 0.0 | 0.0 |
| 2 | prove | 0.843 | 2.705 | 3.21 | 375.1 | 0.0 | 0.0 |
| 2 | verify | 0.593 | 2.244 | 3.78 | 346.5 | 0.0 | 0.0 |
| 4 | prove | 0.844 | 2.788 | 3.30 | 391.9 | 0.0 | 0.0 |
| 4 | verify | 0.588 | 2.246 | 3.82 | 346.1 | 0.0 | 0.0 |
| 8 | prove | 0.867 | 2.890 | 3.33 | 429.7 | 0.0 | 0.0 |
| 8 | verify | 0.592 | 2.276 | 3.85 | 347.6 | 0.0 | 0.0 |
| 16 | prove | 0.905 | 3.085 | 3.41 | 491.2 | 0.0 | 0.0 |
| 16 | verify | 0.602 | 2.255 | 3.74 | 347.1 | 0.0 | 0.0 |
| 32 | prove | 0.958 | 3.516 | 3.67 | 606.3 | 0.0 | 0.0 |
| 32 | verify | 0.596 | 2.248 | 3.77 | 347.1 | 0.0 | 0.0 |
| 64 | prove | 1.095 | 4.117 | 3.76 | 817.2 | 0.5 | 0.1 |
| 64 | verify | 0.594 | 2.268 | 3.82 | 347.3 | 0.0 | 0.0 |
| 128 | prove | 1.268 | 5.403 | 4.26 | 1212.5 | 1.1 | 0.1 |
| 128 | verify | 0.598 | 2.247 | 3.76 | 347.2 | 0.0 | 0.0 |
| 256 | prove | 1.651 | 7.520 | 4.55 | 1494.6 | 2.1 | 0.3 |
| 256 | verify | 0.592 | 2.251 | 3.80 | 347.9 | 0.0 | 0.0 |
| 512 | prove | 2.275 | 11.339 | 4.98 | 1865.5 | 4.2 | 0.8 |
| 512 | verify | 0.604 | 2.226 | 3.69 | 346.8 | 0.0 | 0.0 |
| 1024 | prove | 3.540 | 18.961 | 5.36 | 2932.2 | 8.4 | 4.1 |
| 1024 | verify | 0.601 | 2.218 | 3.69 | 347.6 | 0.0 | 0.0 |
| 2048 | prove | 6.372 | 35.318 | 5.54 | 4231.3 | 16.8 | 8.8 |
| 2048 | verify | 0.595 | 2.198 | 3.69 | 346.9 | 0.0 | 0.0 |
| 4096 | prove | 13.216 | 66.269 | 5.01 | 8106.3 | 33.6 | 19.0 |
| 4096 | verify | 0.588 | 2.202 | 3.74 | 347.3 | 0.0 | 0.0 |
| 8192 | prove | 28.134 | 131.301 | 4.67 | 12063.0 | 67.2 | 39.5 |
| 8192 | verify | 0.607 | 2.221 | 3.66 | 347.2 | 0.0 | 0.0 |

## Tables LaTeX

Prêtes à insérer dans l'article (adapter caption/label) :

- `tables/tab1_resources.tex`
- `tables/tab2_artifacts.tex`
