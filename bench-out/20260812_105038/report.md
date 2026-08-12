# Benchmark ressources zk-SNARK

- Date : 2026-08-12T11:08:29
- Machine : Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.41, 16 cœurs, 15.2 GiB de RAM
- Backend de mesure : proc
- Prover principal : snarkjs
- rapidsnark : docker exec debian_rapidsnark (préfixe mnt/projet, prover mnt/projet/rapidsnark/package/bin/prover)
- Tailles mesurées : 1, 2, 4, 8, 16, 32, 64, 128
- Répétitions preuve/vérification : 2

## Moyennes par phase

| n | phase | durée (s) | CPU (s) | cœurs occupés | RSS max (MiB) | lecture (MiB) | écriture (MiB) |
|---|-------|-----------|---------|---------------|---------------|---------------|----------------|
| 1 | env | 59.209 | 41.281 | 0.70 | 287.6 | 53.3 | 0.0 |
| 1 | prove | 3.150 | 1.545 | 0.49 | 209.5 | 0.0 | 0.0 |
| 1 | verify | 2.372 | 1.361 | 0.57 | 176.1 | 0.0 | 0.0 |
| 2 | env | 59.417 | 51.948 | 0.87 | 299.6 | 0.0 | 0.0 |
| 2 | prove | 3.156 | 1.580 | 0.50 | 217.1 | 0.0 | 0.0 |
| 2 | verify | 2.354 | 1.345 | 0.57 | 175.0 | 0.0 | 0.0 |
| 4 | env | 63.513 | 75.778 | 1.19 | 311.3 | 0.0 | 0.0 |
| 4 | prove | 3.059 | 1.653 | 0.54 | 234.3 | 0.0 | 0.0 |
| 4 | verify | 2.384 | 1.424 | 0.60 | 201.3 | 0.0 | 0.0 |
| 8 | env | 68.666 | 122.252 | 1.78 | 367.6 | 0.0 | 0.0 |
| 8 | prove | 3.115 | 1.764 | 0.57 | 271.1 | 0.0 | 0.0 |
| 8 | verify | 2.324 | 1.308 | 0.56 | 157.1 | 0.0 | 0.0 |
| 16 | env | 77.882 | 221.742 | 2.85 | 463.6 | 0.0 | 0.0 |
| 16 | prove | 3.202 | 1.994 | 0.62 | 329.7 | 0.0 | 0.0 |
| 16 | verify | 2.375 | 1.316 | 0.55 | 203.3 | 0.0 | 0.0 |
| 32 | env | 101.352 | 441.347 | 4.35 | 602.9 | 0.0 | 0.0 |
| 32 | prove | 3.268 | 2.379 | 0.73 | 428.1 | 0.0 | 0.0 |
| 32 | verify | 2.390 | 1.411 | 0.59 | 198.2 | 0.0 | 0.0 |
| 64 | env | 148.590 | 918.034 | 6.18 | 831.0 | 0.0 | 0.0 |
| 64 | prove | 3.478 | 3.113 | 0.90 | 615.5 | 0.0 | 0.0 |
| 64 | verify | 2.442 | 1.464 | 0.60 | 196.9 | 0.0 | 0.0 |
| 128 | env | 231.612 | 1840.832 | 7.95 | 965.6 | 0.0 | 0.0 |
| 128 | prove | 3.693 | 4.126 | 1.12 | 781.2 | 0.0 | 0.0 |
| 128 | verify | 2.383 | 1.378 | 0.58 | 200.2 | 0.0 | 0.0 |

## Dimensionnement machine

À N=128 : RSS max du setup 966 MiB (7.9 cœurs occupés), de la preuve 781 MiB (1.1 cœurs), de la vérification 200 MiB.
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
