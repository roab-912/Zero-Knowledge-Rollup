# Benchmark ressources zk-SNARK

- Date : 2026-08-12T09:49:26
- Machine : Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.41, 16 cœurs, 15.2 GiB de RAM
- Backend de mesure : proc
- Prover principal : snarkjs
- rapidsnark : docker exec debian_rapidsnark (préfixe mnt/projet, prover mnt/projet/rapidsnark/package/bin/prover)
- Tailles mesurées : 1, 2, 4, 8, 16, 32, 64
- Répétitions preuve/vérification : 5

## Moyennes par phase

| n | phase | durée (s) | CPU (s) | cœurs occupés | RSS max (MiB) | lecture (MiB) | écriture (MiB) |
|---|-------|-----------|---------|---------------|---------------|---------------|----------------|
| 1 | env | 64.504 | 48.393 | 0.75 | 296.6 | 51.2 | 0.0 |
| 1 | prove | 3.242 | 1.741 | 0.54 | 208.4 | 0.0 | 0.0 |
| 1 | verify | 2.509 | 1.609 | 0.64 | 195.8 | 0.0 | 0.0 |
| 2 | env | 62.219 | 55.659 | 0.89 | 299.2 | 0.0 | 0.0 |
| 2 | prove | 3.253 | 1.730 | 0.53 | 217.7 | 0.0 | 0.0 |
| 2 | verify | 2.329 | 1.395 | 0.60 | 187.0 | 0.0 | 0.0 |
| 4 | env | 62.934 | 74.842 | 1.19 | 311.2 | 0.0 | 0.0 |
| 4 | prove | 3.115 | 1.684 | 0.54 | 234.7 | 0.0 | 0.0 |
| 4 | verify | 2.377 | 1.378 | 0.58 | 202.3 | 0.0 | 0.0 |
| 8 | env | 70.214 | 125.609 | 1.79 | 367.2 | 0.0 | 0.0 |
| 8 | prove | 3.160 | 1.846 | 0.58 | 272.1 | 0.0 | 0.0 |
| 8 | verify | 2.387 | 1.463 | 0.61 | 186.7 | 0.0 | 0.0 |
| 16 | env | 85.363 | 252.316 | 2.96 | 461.8 | 0.0 | 0.0 |
| 16 | prove | 3.457 | 2.139 | 0.62 | 329.5 | 0.0 | 0.0 |
| 16 | verify | 2.444 | 1.414 | 0.58 | 177.6 | 0.0 | 0.0 |
| 32 | env | 103.118 | 452.697 | 4.39 | 599.5 | 0.0 | 0.0 |
| 32 | prove | 3.352 | 2.368 | 0.71 | 428.6 | 0.0 | 0.0 |
| 32 | verify | 2.453 | 1.493 | 0.61 | 193.1 | 0.0 | 0.0 |
| 64 | env | 153.871 | 943.805 | 6.13 | 813.1 | 0.0 | 0.0 |
| 64 | prove | 4.318 | 3.485 | 0.81 | 620.1 | 0.0 | 0.0 |
| 64 | verify | 2.483 | 1.414 | 0.57 | 190.1 | 0.0 | 0.0 |

## Dimensionnement machine

À N=64 : RSS max du setup 813 MiB (6.1 cœurs occupés), de la preuve 620 MiB (0.8 cœurs), de la vérification 190 MiB.
Une fois le setup exécuté (one-off), un nœud dédié à la preuve peut donc être provisionné avec ~1.3× moins de RAM que la machine de setup.

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
- `tables/tab5_amortization.tex`
- `tables/tab6_provisioning.tex`
- `tables/tab7_io.tex`
- `tables/tab8_artifacts.tex`
