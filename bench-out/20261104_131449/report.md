# Benchmark ressources zk-SNARK

- Date : 2026-11-04T13:39:36
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
| 1 | prove | 0.813 | 2.630 | 3.24 | 354.1 | 0.0 | 0.0 |
| 1 | verify | 0.597 | 2.256 | 3.78 | 338.5 | 0.0 | 0.0 |
| 2 | prove | 0.841 | 2.684 | 3.19 | 356.0 | 0.0 | 0.0 |
| 2 | verify | 0.594 | 2.227 | 3.75 | 337.2 | 0.0 | 0.0 |
| 4 | prove | 0.845 | 2.774 | 3.28 | 365.6 | 0.0 | 0.0 |
| 4 | verify | 0.587 | 2.270 | 3.87 | 340.1 | 0.0 | 0.0 |
| 8 | prove | 0.861 | 2.898 | 3.37 | 397.5 | 0.1 | 0.0 |
| 8 | verify | 0.581 | 2.214 | 3.81 | 318.0 | 0.0 | 0.0 |
| 16 | prove | 0.906 | 3.056 | 3.37 | 464.8 | 0.1 | 0.0 |
| 16 | verify | 0.591 | 2.235 | 3.78 | 338.2 | 0.0 | 0.0 |
| 32 | prove | 0.963 | 3.520 | 3.66 | 558.1 | 0.3 | 0.0 |
| 32 | verify | 0.587 | 2.218 | 3.78 | 341.3 | 0.0 | 0.0 |
| 64 | prove | 1.059 | 4.114 | 3.88 | 812.9 | 0.5 | 0.0 |
| 64 | verify | 0.586 | 2.252 | 3.84 | 333.5 | 0.0 | 0.0 |
| 128 | prove | 1.271 | 5.383 | 4.23 | 1205.2 | 0.4 | 0.0 |
| 128 | verify | 0.594 | 2.267 | 3.82 | 339.3 | 0.0 | 0.0 |
| 256 | prove | 1.605 | 7.497 | 4.67 | 1472.0 | 1.1 | 0.0 |
| 256 | verify | 0.578 | 2.218 | 3.84 | 318.4 | 0.0 | 0.0 |
| 512 | prove | 2.244 | 11.369 | 5.07 | 1830.9 | 1.1 | 0.0 |
| 512 | verify | 0.587 | 2.239 | 3.81 | 345.2 | 0.0 | 0.0 |
| 1024 | prove | 3.581 | 19.296 | 5.39 | 2943.8 | 0.9 | 0.0 |
| 1024 | verify | 0.590 | 2.260 | 3.83 | 335.7 | 0.0 | 0.0 |
| 2048 | prove | 6.320 | 35.238 | 5.58 | 4192.4 | 8.9 | 0.0 |
| 2048 | verify | 0.577 | 2.180 | 3.78 | 317.8 | 0.0 | 0.0 |
| 4096 | prove | 13.066 | 67.237 | 5.15 | 8039.5 | 0.0 | 2.0 |
| 4096 | verify | 0.578 | 2.186 | 3.78 | 321.7 | 0.0 | 0.0 |
| 8192 | prove | 26.093 | 131.216 | 5.03 | 10988.3 | 0.0 | 36.5 |
| 8192 | verify | 0.591 | 2.205 | 3.73 | 324.7 | 0.0 | 0.0 |

## Figures

Chaque figure porte une affirmation unique, réutilisable comme légende :

- `figs/01_phase_cost_stacked.png` — End-to-end cost of one batch, split into its three phases: the one-off setup dominates the total, while the recurring phases stay small.
- `figs/01b_recurring_cost_stacked.png` — Recurring per-batch cost (proving + verification) with one column per prover: verification is identical, so the whole gap comes from proving.
- `figs/02_machine_sizing_per_phase.png` — Peak RAM and busy cores per phase, as box plots over the repetitions (min/max whiskers, median line, mean marker): the one-off setup sets the machine requirement; once it has been run, the recurring proving and verification phases fit on a smaller machine.
- `figs/05_phase_time_treemap.png` — Wall-clock time as area at the largest batch size, one cell per phase: setup, proving and verification are put in area ratio rather than in bar height, which keeps a two-orders-of-magnitude minority phase readable.

## Tables LaTeX

Prêtes à insérer dans l'article (adapter caption/label) :

- `tables/tab1_resources.tex`
- `tables/tab2_artifacts.tex`
