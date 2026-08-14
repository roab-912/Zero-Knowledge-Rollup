# Benchmark ressources zk-SNARK

- Date : 2026-08-13T14:50:48
- Machine : Linux-5.15.146.1-microsoft-standard-WSL2-x86_64-with-glibc2.36, 8 cœurs, 7.7 GiB de RAM
- Backend de mesure : proc
- Prover principal : snarkjs
- rapidsnark : binaire natif : /usr/local/bin/prover
- Tailles mesurées : 1, 2, 4, 8
- Répétitions preuve/vérification : 5

## Moyennes par phase

| n | phase | durée (s) | CPU (s) | cœurs occupés | RSS max (MiB) | lecture (MiB) | écriture (MiB) |
|---|-------|-----------|---------|---------------|---------------|---------------|----------------|
| 1 | env | 36.967 | 85.898 | 2.32 | 232.4 | 62.1 | 0.0 |
| 1 | prove | 1.343 | 3.040 | 2.26 | 187.4 | 0.0 | 0.0 |
| 1 | verify | 0.789 | 1.704 | 2.16 | 167.4 | 0.0 | 0.0 |
| 2 | env | 31.031 | 84.927 | 2.74 | 233.1 | 0.0 | 0.0 |
| 2 | prove | 1.116 | 2.257 | 2.02 | 195.8 | 0.0 | 0.0 |
| 2 | verify | 0.956 | 2.037 | 2.13 | 169.2 | 0.0 | 0.0 |
| 4 | env | 55.081 | 188.067 | 3.41 | 241.3 | 0.0 | 0.0 |
| 4 | prove | 1.262 | 2.436 | 1.93 | 210.7 | 0.0 | 0.0 |
| 4 | verify | 1.028 | 2.535 | 2.47 | 169.3 | 0.0 | 0.0 |
| 8 | env | 96.012 | 413.338 | 4.31 | 281.8 | 0.0 | 0.0 |
| 8 | prove | 1.111 | 2.777 | 2.50 | 242.0 | 0.0 | 0.0 |
| 8 | verify | 1.335 | 3.091 | 2.31 | 169.3 | 0.0 | 0.0 |

## Dimensionnement machine

À N=8 : RSS max du setup 282 MiB (4.3 cœurs occupés), de la preuve 242 MiB (2.5 cœurs), de la vérification 169 MiB.

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
