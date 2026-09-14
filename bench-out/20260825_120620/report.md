# Benchmark ressources zk-SNARK

- Date : 2026-08-25T16:45:12
- Machine : Linux-6.12.90+deb13.1-amd64-x86_64-with-glibc2.41, 32 cœurs, 128.0 GiB de RAM
- Backend de mesure : proc
- Prover principal : snarkjs
- rapidsnark : binaire natif : /home/r24barbi/rapidsnark/package/bin/prover
- Tailles mesurées : 1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192
- Répétitions preuve/vérification : 10

## Moyennes par phase

| n | phase | durée (s) | CPU (s) | cœurs occupés | RSS max (MiB) | lecture (MiB) | écriture (MiB) |
|---|-------|-----------|---------|---------------|---------------|---------------|----------------|
| 1 | env | 15.921 | 63.138 | 3.97 | 427.8 | 0.0 | 0.9 |
| 1 | prove | 0.646 | 2.403 | 3.72 | 340.0 | 0.0 | 0.0 |
| 1 | verify | 0.595 | 2.215 | 3.72 | 344.1 | 0.0 | 0.0 |
| 2 | env | 16.918 | 75.353 | 4.45 | 430.4 | 0.0 | 1.9 |
| 2 | prove | 0.663 | 2.482 | 3.74 | 339.3 | 0.0 | 0.0 |
| 2 | verify | 0.579 | 2.186 | 3.78 | 326.6 | 0.0 | 0.0 |
| 4 | env | 18.834 | 99.413 | 5.28 | 469.1 | 0.0 | 3.5 |
| 4 | prove | 0.668 | 2.521 | 3.78 | 346.1 | 0.0 | 0.0 |
| 4 | verify | 0.582 | 2.231 | 3.83 | 329.3 | 0.0 | 0.0 |
| 8 | env | 23.002 | 152.193 | 6.62 | 537.9 | 0.0 | 10.6 |
| 8 | prove | 0.691 | 2.684 | 3.88 | 401.8 | 0.0 | 0.0 |
| 8 | verify | 0.574 | 2.192 | 3.82 | 314.3 | 0.0 | 0.0 |
| 16 | env | 30.619 | 259.463 | 8.47 | 667.0 | 0.0 | 21.5 |
| 16 | prove | 0.734 | 2.891 | 3.94 | 470.1 | 0.0 | 0.0 |
| 16 | verify | 0.590 | 2.211 | 3.75 | 338.7 | 0.0 | 0.0 |
| 32 | env | 46.100 | 489.647 | 10.62 | 899.2 | 0.0 | 41.1 |
| 32 | prove | 0.795 | 3.255 | 4.09 | 544.5 | 0.0 | 0.0 |
| 32 | verify | 0.595 | 2.197 | 3.69 | 341.6 | 0.0 | 0.0 |
| 64 | env | 78.230 | 962.750 | 12.31 | 1301.1 | 0.0 | 88.3 |
| 64 | prove | 0.935 | 3.945 | 4.22 | 786.6 | 0.0 | 0.0 |
| 64 | verify | 0.602 | 2.210 | 3.67 | 341.7 | 0.0 | 0.0 |
| 128 | env | 135.621 | 1975.394 | 14.57 | 1503.5 | 0.0 | 196.6 |
| 128 | prove | 1.119 | 5.104 | 4.56 | 1199.2 | 0.0 | 0.0 |
| 128 | verify | 0.590 | 2.210 | 3.74 | 334.0 | 0.0 | 0.0 |
| 256 | env | 244.981 | 4083.977 | 16.67 | 1787.2 | 0.0 | 365.8 |
| 256 | prove | 1.501 | 7.258 | 4.83 | 1464.0 | 0.0 | 0.0 |
| 256 | verify | 0.594 | 2.210 | 3.72 | 340.8 | 0.0 | 0.0 |
| 512 | env | 472.804 | 8539.739 | 18.06 | 2676.4 | 0.0 | 743.8 |
| 512 | prove | 2.168 | 11.349 | 5.23 | 1833.2 | 0.0 | 0.0 |
| 512 | verify | 0.608 | 2.222 | 3.65 | 337.1 | 0.0 | 0.0 |
| 1024 | env | 946.704 | 17895.144 | 18.90 | 4504.2 | 0.0 | 1521.7 |
| 1024 | prove | 3.614 | 19.544 | 5.41 | 2933.5 | 0.0 | 0.0 |
| 1024 | verify | 0.596 | 2.234 | 3.75 | 335.9 | 0.0 | 0.0 |
| 2048 | env | 1894.634 | 37684.130 | 19.89 | 6799.8 | 0.0 | 3076.0 |
| 2048 | prove | 6.428 | 35.984 | 5.60 | 4299.9 | 0.0 | 0.0 |
| 2048 | verify | 0.606 | 2.231 | 3.68 | 334.0 | 0.0 | 0.0 |
| 4096 | env | 3736.521 | 81256.953 | 21.75 | 10313.6 | 0.0 | 6176.5 |
| 4096 | prove | 11.950 | 67.488 | 5.65 | 8070.1 | 0.0 | 0.0 |
| 4096 | verify | 0.592 | 2.188 | 3.69 | 340.1 | 0.0 | 0.0 |
| 8192 | env | 7604.499 | 172083.304 | 22.63 | 18306.8 | 0.0 | 12362.8 |
| 8192 | prove | 25.672 | 129.401 | 5.04 | 12223.4 | 0.0 | 0.0 |
| 8192 | verify | 0.580 | 2.218 | 3.82 | 322.1 | 0.0 | 0.0 |

## Dimensionnement machine

À N=8192 : RSS max du setup 17.88 GiB (22.6 cœurs occupés), de la preuve 11.94 GiB (7.1 cœurs), de la vérification 322 MiB.
Une fois le setup exécuté (one-off), un nœud dédié à la preuve peut donc être provisionné avec ~1.5× moins de RAM que la machine de setup.

## Figures

Chaque figure porte une affirmation unique, réutilisable comme légende :

- `figs/01_phase_cost_stacked.png` — End-to-end cost of one batch, split into its three phases: the one-off setup dominates the total, while the recurring phases stay small.
- `figs/01b_recurring_cost_stacked.png` — Recurring per-batch cost (proving + verification) with one column per prover: verification is identical, so the whole gap comes from proving.
- `figs/02_machine_sizing_per_phase.png` — Peak RAM and busy cores per phase, as box plots over the repetitions (min/max whiskers, median line, mean marker): the one-off setup sets the machine requirement; once it has been run, the recurring proving and verification phases fit on a smaller machine.
- `figs/03_setup_amortization.png` — Per-batch time T_setup/k + T_proof as the number of proved batches k grows: the one-off setup rapidly becomes a minority cost, which justifies excluding it from the per-batch critical path.
- `figs/05_phase_time_treemap.png` — Wall-clock time as area at the largest batch size, one cell per phase: setup, proving and verification are put in area ratio rather than in bar height, which keeps a two-orders-of-magnitude minority phase readable.
- `figs/06_setup_time_treemap.png` — Same decomposition with the one-off setup split into its three steps: the universal Powers of Tau, the circom compilation and the circuit-specific zkey, showing which part of the setup a change of circuit actually forces to be redone.

## Tables LaTeX

Prêtes à insérer dans l'article (adapter caption/label) :

- `tables/tab1_resources.tex`
- `tables/tab2_artifacts.tex`
