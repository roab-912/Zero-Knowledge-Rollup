# Benchmark ressources zk-SNARK

- Date : 2026-07-30T11:54:12
- Machine : Linux-6.6.87.2-microsoft-standard-WSL2-x86_64-with-glibc2.41, 16 cœurs, 15.2 GiB de RAM
- Backend de mesure : proc
- Prover principal : snarkjs
- rapidsnark : docker exec debian_rapidsnark (préfixe mnt/projet, prover mnt/projet/rapidsnark/package/bin/prover)
- Tailles mesurées : 1, 2, 4, 8, 16, 32, 64
- Répétitions preuve/vérification : 2

## Moyennes par phase

| n | phase | durée (s) | CPU (s) | cœurs occupés | RSS max (MiB) | lecture (MiB) | écriture (MiB) |
|---|-------|-----------|---------|---------------|---------------|---------------|----------------|
| 1 | env | 58.963 | 41.725 | 0.71 | 286.4 | 5.8 | 0.0 |
| 1 | prove | 3.214 | 1.719 | 0.53 | 209.7 | 0.0 | 0.0 |
| 1 | verify | 2.355 | 1.375 | 0.58 | 195.0 | 0.0 | 0.0 |
| 2 | env | 59.944 | 51.443 | 0.86 | 278.5 | 0.0 | 0.0 |
| 2 | prove | 3.096 | 1.548 | 0.50 | 217.0 | 0.0 | 0.0 |
| 2 | verify | 2.338 | 1.336 | 0.57 | 188.4 | 0.0 | 0.0 |
| 4 | env | 61.271 | 72.479 | 1.18 | 314.2 | 0.0 | 0.0 |
| 4 | prove | 3.069 | 1.581 | 0.52 | 234.4 | 0.0 | 0.0 |
| 4 | verify | 2.319 | 1.393 | 0.60 | 172.5 | 0.0 | 0.0 |
| 8 | env | 66.490 | 119.945 | 1.80 | 399.7 | 0.0 | 0.0 |
| 8 | prove | 3.103 | 1.755 | 0.57 | 272.4 | 0.0 | 0.0 |
| 8 | verify | 2.299 | 1.292 | 0.56 | 172.1 | 0.0 | 0.0 |
| 16 | env | 76.750 | 219.903 | 2.87 | 465.3 | 0.0 | 0.0 |
| 16 | prove | 3.144 | 1.906 | 0.61 | 328.8 | 0.0 | 0.0 |
| 16 | verify | 2.336 | 1.345 | 0.58 | 185.4 | 0.0 | 0.0 |
| 32 | env | 97.747 | 435.876 | 4.46 | 596.9 | 0.0 | 0.0 |
| 32 | prove | 3.215 | 2.262 | 0.70 | 429.2 | 0.0 | 0.0 |
| 32 | verify | 2.354 | 1.359 | 0.58 | 178.2 | 0.0 | 0.0 |
| 64 | env | 140.633 | 878.849 | 6.25 | 809.0 | 0.0 | 0.0 |
| 64 | prove | 3.354 | 2.943 | 0.88 | 614.5 | 0.0 | 0.0 |
| 64 | verify | 2.332 | 1.306 | 0.56 | 196.5 | 0.0 | 0.0 |

## Figures

- `figs/01_duree_par_phase_empilee.png`
- `figs/02_cpu_par_phase_empilee.png`
- `figs/03_memoire_pic_par_phase.png`
- `figs/04_io_par_phase.png`
- `figs/05_duree_par_phase_log.png`
- `figs/06_repartition_relative.png`
- `figs/07_decomposition_environnement.png`
- `figs/08_passage_echelle.png`
- `figs/09_parallelisme_effectif.png`
- `figs/10_comparaison_provers.png`
- `figs/11_chronogramme_n64.png`
- `figs/12_taille_artefacts.png`
