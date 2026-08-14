# Benchmark ressources zk-SNARK

- Date : 2026-08-13T10:35:27
- Machine : Linux-5.15.146.1-microsoft-standard-WSL2-x86_64-with-glibc2.36, 8 cœurs, 7.7 GiB de RAM
- Backend de mesure : proc
- Prover principal : snarkjs
- rapidsnark : binaire natif : /usr/local/bin/prover
- Tailles mesurées : 1, 2, 4, 8, 16, 32, 64
- Répétitions preuve/vérification : 5

## Moyennes par phase

| n | phase | durée (s) | CPU (s) | cœurs occupés | RSS max (MiB) | lecture (MiB) | écriture (MiB) |
|---|-------|-----------|---------|---------------|---------------|---------------|----------------|
| 1 | env | 30.634 | 80.361 | 2.62 | 234.0 | 0.0 | 0.0 |
| 1 | prove | 0.892 | 2.001 | 2.24 | 190.4 | 0.0 | 0.0 |
| 1 | verify | 0.864 | 2.040 | 2.36 | 169.4 | 0.0 | 0.0 |
| 2 | env | 36.950 | 115.569 | 3.13 | 234.6 | 0.0 | 0.0 |
| 2 | prove | 0.965 | 2.120 | 2.20 | 196.0 | 0.0 | 0.0 |
| 2 | verify | 0.806 | 1.913 | 2.37 | 172.1 | 0.0 | 0.0 |
| 4 | env | 46.015 | 168.311 | 3.66 | 242.2 | 0.0 | 0.0 |
| 4 | prove | 0.976 | 2.214 | 2.27 | 212.9 | 0.0 | 0.0 |
| 4 | verify | 0.787 | 1.787 | 2.27 | 171.1 | 0.0 | 0.0 |
| 8 | env | 118.341 | 538.865 | 4.55 | 286.2 | 0.0 | 0.0 |
| 8 | prove | 1.008 | 2.443 | 2.42 | 242.7 | 0.0 | 0.0 |
| 8 | verify | 0.704 | 1.643 | 2.34 | 170.3 | 0.0 | 0.0 |
| 16 | env | 141.210 | 715.653 | 5.07 | 350.1 | 0.0 | 0.0 |
| 16 | prove | 1.410 | 3.442 | 2.44 | 297.8 | 0.0 | 0.0 |
| 16 | verify | 0.917 | 1.937 | 2.11 | 170.1 | 0.0 | 0.0 |
| 32 | env | 248.651 | 1380.316 | 5.55 | 464.4 | 0.0 | 0.0 |
| 32 | prove | 1.442 | 3.905 | 2.71 | 384.0 | 0.0 | 0.0 |
| 32 | verify | 0.788 | 1.778 | 2.26 | 171.1 | 0.0 | 0.0 |
| 64 | env | 410.447 | 2272.081 | 5.54 | 517.4 | 0.0 | 0.0 |
| 64 | prove | 1.565 | 4.514 | 2.88 | 460.6 | 0.0 | 0.0 |
| 64 | verify | 0.842 | 1.756 | 2.09 | 170.5 | 0.0 | 0.0 |

## Dimensionnement machine

À N=64 : RSS max du setup 517 MiB (5.5 cœurs occupés), de la preuve 461 MiB (2.9 cœurs), de la vérification 170 MiB.

## Figures

Chaque figure porte une affirmation unique, réutilisable comme légende :

- `figs/01_phase_cost_stacked.png` — End-to-end cost of one batch, split into its three phases: the one-off setup dominates the total, while the recurring phases stay small.
- `figs/01b_recurring_cost_stacked.png` — Recurring per-batch cost (proving + verification) with one column per prover: verification is identical, so the whole gap comes from proving.
- `figs/02_machine_sizing_per_phase.png` — Peak RAM and busy cores per phase, as box plots over the repetitions (min/max whiskers, median line, mean marker): the one-off setup sets the machine requirement; once it has been run, the recurring proving and verification phases fit on a smaller machine.
- `figs/03_setup_amortization.png` — Per-batch time T_setup/k + T_proof as the number of proved batches k grows: the one-off setup rapidly becomes a minority cost, which justifies excluding it from the per-batch critical path.

## Tables LaTeX

Prêtes à insérer dans l'article (adapter caption/label) :

- `tables/tab1_resources.tex`
- `tables/tab2_artifacts.tex`
