# Benchmark ressources zk-SNARK

- Date : 2026-08-12T10:29:11
- Machine : Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.41, 16 cœurs, 15.2 GiB de RAM
- Backend de mesure : proc
- Prover principal : snarkjs
- rapidsnark : docker exec debian_rapidsnark (préfixe mnt/projet, prover mnt/projet/rapidsnark/package/bin/prover)
- Tailles mesurées : 1, 2, 4, 8, 16, 32, 64
- Répétitions preuve/vérification : 1

## Moyennes par phase

| n | phase | durée (s) | CPU (s) | cœurs occupés | RSS max (MiB) | lecture (MiB) | écriture (MiB) |
|---|-------|-----------|---------|---------------|---------------|---------------|----------------|
| 1 | env | 58.704 | 41.041 | 0.70 | 286.3 | 0.0 | 0.0 |
| 1 | prove | 3.100 | 1.459 | 0.47 | 207.5 | 0.0 | 0.0 |
| 1 | verify | 2.350 | 1.297 | 0.55 | 182.0 | 0.0 | 0.0 |
| 2 | env | 60.153 | 51.377 | 0.85 | 287.7 | 0.0 | 0.0 |
| 2 | prove | 3.195 | 1.647 | 0.52 | 216.8 | 0.0 | 0.0 |
| 2 | verify | 2.338 | 1.315 | 0.56 | 136.8 | 0.0 | 0.0 |
| 4 | env | 63.121 | 72.851 | 1.15 | 346.6 | 0.0 | 0.0 |
| 4 | prove | 3.182 | 1.629 | 0.51 | 233.5 | 0.0 | 0.0 |
| 4 | verify | 2.363 | 1.361 | 0.58 | 206.5 | 0.0 | 0.0 |
| 8 | env | 68.126 | 120.652 | 1.77 | 367.6 | 0.0 | 0.0 |
| 8 | prove | 3.229 | 1.736 | 0.54 | 272.2 | 0.0 | 0.0 |
| 8 | verify | 2.386 | 1.396 | 0.59 | 201.0 | 0.0 | 0.0 |
| 16 | env | 78.659 | 220.329 | 2.80 | 462.7 | 0.0 | 0.0 |
| 16 | prove | 3.231 | 1.956 | 0.61 | 330.4 | 0.0 | 0.0 |
| 16 | verify | 2.402 | 1.345 | 0.56 | 197.0 | 0.0 | 0.0 |
| 32 | env | 100.402 | 432.436 | 4.31 | 617.4 | 0.0 | 0.0 |
| 32 | prove | 3.236 | 2.260 | 0.70 | 426.3 | 0.0 | 0.0 |
| 32 | verify | 2.375 | 1.326 | 0.56 | 203.5 | 0.0 | 0.0 |
| 64 | env | 146.591 | 895.397 | 6.11 | 825.2 | 0.0 | 0.0 |
| 64 | prove | 3.448 | 3.062 | 0.89 | 611.7 | 0.0 | 0.0 |
| 64 | verify | 2.368 | 1.288 | 0.54 | 204.6 | 0.0 | 0.0 |

## Dimensionnement machine

À N=64 : RSS max du setup 825 MiB (6.1 cœurs occupés), de la preuve 612 MiB (0.9 cœurs), de la vérification 205 MiB.
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
- `tables/tab6_provisioning.tex`
- `tables/tab7_io.tex`
- `tables/tab8_artifacts.tex`
