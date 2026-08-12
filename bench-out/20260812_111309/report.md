# Benchmark ressources zk-SNARK

- Date : 2026-08-12T11:36:46
- Machine : Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.41, 16 cœurs, 15.2 GiB de RAM
- Backend de mesure : proc
- Prover principal : snarkjs
- rapidsnark : docker exec debian_rapidsnark (préfixe mnt/projet, prover mnt/projet/rapidsnark/package/bin/prover)
- Tailles mesurées : 1, 2, 4, 8, 16, 32, 64, 128
- Répétitions preuve/vérification : 5

## Moyennes par phase

| n | phase | durée (s) | CPU (s) | cœurs occupés | RSS max (MiB) | lecture (MiB) | écriture (MiB) |
|---|-------|-----------|---------|---------------|---------------|---------------|----------------|
| 1 | env | 60.236 | 43.097 | 0.72 | 283.9 | 0.0 | 0.0 |
| 1 | prove | 3.221 | 1.618 | 0.50 | 208.6 | 0.0 | 0.0 |
| 1 | verify | 2.457 | 1.479 | 0.60 | 191.0 | 0.0 | 0.0 |
| 2 | env | 60.951 | 52.650 | 0.86 | 284.8 | 0.0 | 0.0 |
| 2 | prove | 3.174 | 1.594 | 0.50 | 217.7 | 0.0 | 0.0 |
| 2 | verify | 2.451 | 1.443 | 0.59 | 183.6 | 0.0 | 0.0 |
| 4 | env | 64.790 | 77.944 | 1.20 | 313.3 | 0.0 | 0.0 |
| 4 | prove | 3.253 | 1.718 | 0.53 | 234.4 | 0.0 | 0.0 |
| 4 | verify | 2.394 | 1.410 | 0.59 | 198.8 | 0.0 | 0.0 |
| 8 | env | 69.072 | 124.152 | 1.80 | 368.1 | 0.0 | 0.0 |
| 8 | prove | 3.209 | 1.840 | 0.57 | 271.5 | 0.0 | 0.0 |
| 8 | verify | 2.406 | 1.431 | 0.59 | 193.0 | 0.0 | 0.0 |
| 16 | env | 79.507 | 226.419 | 2.85 | 455.9 | 0.0 | 0.0 |
| 16 | prove | 3.256 | 2.008 | 0.62 | 329.4 | 0.0 | 0.0 |
| 16 | verify | 2.422 | 1.455 | 0.60 | 196.7 | 0.0 | 0.0 |
| 32 | env | 101.366 | 443.104 | 4.37 | 605.2 | 0.0 | 0.0 |
| 32 | prove | 3.364 | 2.436 | 0.72 | 427.8 | 0.0 | 0.0 |
| 32 | verify | 2.385 | 1.377 | 0.58 | 201.1 | 0.0 | 0.0 |
| 64 | env | 147.204 | 903.999 | 6.14 | 825.8 | 0.0 | 0.0 |
| 64 | prove | 3.478 | 3.044 | 0.88 | 619.1 | 0.0 | 0.0 |
| 64 | verify | 2.382 | 1.382 | 0.58 | 202.8 | 0.0 | 0.0 |
| 128 | env | 234.258 | 1867.043 | 7.97 | 962.7 | 0.0 | 0.0 |
| 128 | prove | 3.744 | 4.215 | 1.13 | 773.8 | 0.0 | 0.0 |
| 128 | verify | 2.442 | 1.425 | 0.58 | 179.4 | 0.0 | 0.0 |

## Dimensionnement machine

À N=128 : RSS max du setup 963 MiB (8.0 cœurs occupés), de la preuve 774 MiB (1.1 cœurs), de la vérification 179 MiB.
Une fois le setup exécuté (one-off), un nœud dédié à la preuve peut donc être provisionné avec ~1.2× moins de RAM que la machine de setup.

## Figures

Chaque figure porte une affirmation unique, réutilisable comme légende :

- `figs/01_phase_cost_stacked.png` — End-to-end cost of one batch, split into its three phases: the one-off setup dominates the total, while the recurring phases stay small.
- `figs/01b_recurring_cost_stacked.png` — Recurring per-batch cost (proving + verification) with one column per prover: verification is identical, so the whole gap comes from proving.
- `figs/02_machine_sizing_per_phase.png` — Peak RAM and busy cores per phase, as box plots over the repetitions (min/max whiskers, median line, mean marker): the one-off setup sets the machine requirement; once it has been run, the recurring proving and verification phases fit on a smaller machine.
- `figs/03_setup_amortization.png` — Per-batch time T_setup/k + T_proof as the number of proved batches k grows: the one-off setup rapidly becomes a minority cost, which justifies excluding it from the per-batch critical path.

## Tables LaTeX

Prêtes à insérer dans l'article (adapter caption/label) :

- `tables/tab1_resources.tex`
- `tables/tab2_provers.tex`
- `tables/tab3_lifecycle.tex`
- `tables/tab4_ratios.tex`
- `tables/tab6_provisioning.tex`
- `tables/tab7_io.tex`
- `tables/tab8_artifacts.tex`

## Étapes en échec

- n=128 `prove_rapidsnark` (code 1) — voir `logs/n128_prove_rapidsnark_r1.log`
