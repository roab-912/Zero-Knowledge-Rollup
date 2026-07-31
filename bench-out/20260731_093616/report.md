# Benchmark ressources zk-SNARK

- Date : 2026-07-31T09:38:52
- Machine : Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.41, 16 cœurs, 15.2 GiB de RAM
- Backend de mesure : proc
- Prover principal : snarkjs
- rapidsnark : docker exec debian_rapidsnark (préfixe mnt/projet, prover mnt/projet/rapidsnark/package/bin/prover)
- Tailles mesurées : 1, 2
- Répétitions preuve/vérification : 1

## Moyennes par phase

| n | phase | durée (s) | CPU (s) | cœurs occupés | RSS max (MiB) | lecture (MiB) | écriture (MiB) |
|---|-------|-----------|---------|---------------|---------------|---------------|----------------|
| 1 | env | 57.934 | 41.492 | 0.72 | 286.0 | 54.6 | 0.0 |
| 1 | prove | 3.279 | 1.725 | 0.53 | 208.2 | 0.0 | 0.0 |
| 1 | verify | 2.359 | 1.380 | 0.59 | 207.2 | 0.0 | 0.0 |
| 2 | env | 59.095 | 52.128 | 0.88 | 287.5 | 0.0 | 0.0 |
| 2 | prove | 3.063 | 1.615 | 0.53 | 219.1 | 0.0 | 0.0 |
| 2 | verify | 2.286 | 1.324 | 0.58 | 198.8 | 0.0 | 0.0 |

## Dimensionnement machine

À N=2 : RSS max du setup 288 MiB (0.9 cœurs occupés), de la preuve 219 MiB (0.5 cœurs), de la vérification 199 MiB.
Une fois le setup exécuté (one-off), un nœud dédié à la preuve peut donc être provisionné avec ~1.3× moins de RAM que la machine de setup.

## Figures

Chaque figure porte une affirmation unique, réutilisable comme légende :

- `figs/01_phase_cost_stacked.png` — End-to-end cost of one batch, split into its three phases: the one-off setup dominates the total, while the recurring phases stay small.
- `figs/01b_recurring_cost_stacked.png` — Recurring per-batch cost (proving + verification) with one column per prover: verification is identical, so the whole gap comes from proving.
- `figs/02_machine_sizing_per_phase.png` — Peak RAM and busy cores per phase, as box plots over the repetitions (min/max whiskers, median line, mean marker): the one-off setup sets the machine requirement; once it has been run, the recurring proving and verification phases fit on a smaller machine.
- `figs/03_setup_amortization.png` — Per-batch time T_setup/k + T_proof as the number of proved batches k grows: the one-off setup rapidly becomes a minority cost, which justifies excluding it from the per-batch critical path.

## Tables LaTeX

Prêtes à insérer dans l'article (adapter caption/label) :

- `tables/tab_amortization.tex`
- `tables/tab_artifacts.tex`
- `tables/tab_lifecycle.tex`
- `tables/tab_phase_ratios.tex`
- `tables/tab_pipeline_resources.tex`
- `tables/tab_prover_comparison.tex`
- `tables/tab_proving_io.tex`
- `tables/tab_provisioning.tex`
