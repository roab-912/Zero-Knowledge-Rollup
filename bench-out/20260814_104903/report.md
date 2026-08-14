# Benchmark ressources zk-SNARK

- Date : 2026-08-14T11:12:11
- Machine : Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.41, 16 cœurs, 15.2 GiB de RAM
- Backend de mesure : proc
- Prover principal : snarkjs
- rapidsnark : docker exec debian_rapidsnark (préfixe mnt/projet, prover mnt/projet/rapidsnark/package/bin/prover)
- Tailles mesurées : 1, 2, 4, 8, 16, 32, 64, 128
- Répétitions preuve/vérification : 5

## Moyennes par phase

| n | phase | durée (s) | CPU (s) | cœurs occupés | RSS max (MiB) | lecture (MiB) | écriture (MiB) |
|---|-------|-----------|---------|---------------|---------------|---------------|----------------|
| 1 | env | 60.787 | 41.454 | 0.68 | 285.6 | 50.3 | 0.0 |
| 1 | prove | 3.078 | 1.510 | 0.49 | 208.4 | 0.0 | 0.0 |
| 1 | verify | 2.321 | 1.298 | 0.56 | 186.5 | 0.0 | 0.0 |
| 2 | env | 59.665 | 50.479 | 0.85 | 287.7 | 0.0 | 0.0 |
| 2 | prove | 3.089 | 1.576 | 0.51 | 217.5 | 0.0 | 0.0 |
| 2 | verify | 2.315 | 1.321 | 0.57 | 172.5 | 0.0 | 0.0 |
| 4 | env | 62.787 | 73.554 | 1.17 | 314.1 | 0.0 | 0.0 |
| 4 | prove | 3.217 | 1.739 | 0.54 | 234.5 | 0.0 | 0.0 |
| 4 | verify | 2.415 | 1.353 | 0.56 | 190.5 | 0.0 | 0.0 |
| 8 | env | 67.575 | 119.633 | 1.77 | 366.9 | 0.0 | 0.0 |
| 8 | prove | 3.120 | 1.687 | 0.54 | 271.5 | 0.0 | 0.0 |
| 8 | verify | 2.335 | 1.300 | 0.56 | 134.7 | 0.0 | 0.0 |
| 16 | env | 77.525 | 216.296 | 2.79 | 457.0 | 0.0 | 0.0 |
| 16 | prove | 3.173 | 1.892 | 0.60 | 328.8 | 0.0 | 0.0 |
| 16 | verify | 2.354 | 1.288 | 0.55 | 168.3 | 0.0 | 0.0 |
| 32 | env | 99.122 | 430.740 | 4.35 | 605.8 | 0.0 | 0.0 |
| 32 | prove | 3.321 | 2.280 | 0.69 | 428.8 | 0.0 | 0.0 |
| 32 | verify | 2.369 | 1.350 | 0.57 | 188.1 | 0.0 | 0.0 |
| 64 | env | 142.584 | 884.502 | 6.20 | 840.0 | 0.0 | 0.0 |
| 64 | prove | 3.406 | 2.903 | 0.85 | 617.5 | 0.0 | 0.0 |
| 64 | verify | 2.365 | 1.387 | 0.59 | 190.7 | 0.0 | 0.0 |
| 128 | env | 227.067 | 1830.117 | 8.06 | 976.1 | 0.0 | 0.0 |
| 128 | prove | 3.720 | 4.166 | 1.12 | 769.5 | 0.0 | 0.0 |
| 128 | verify | 2.414 | 1.409 | 0.58 | 192.4 | 0.0 | 0.0 |

## Dimensionnement machine

À N=128 : RSS max du setup 976 MiB (8.1 cœurs occupés), de la preuve 769 MiB (1.1 cœurs), de la vérification 192 MiB.
Une fois le setup exécuté (one-off), un nœud dédié à la preuve peut donc être provisionné avec ~1.3× moins de RAM que la machine de setup.

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

## Étapes en échec

- n=128 `prove_rapidsnark` (code 1) — voir `logs/n128_prove_rapidsnark_r1.log`
