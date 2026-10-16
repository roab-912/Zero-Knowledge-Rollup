# Exp 2 — comparaison logspace / large

```powershell
python scripts/analysis/exp2.py --combine-designs --train-max 4096 --calibration-count 6 --power 2 --tolerance 0.05 --phases prove --from "D:\Documents\Code\GitHub\Zero-Knowledge-Rollup\bench-out\20260825_120620" --outdir "bench-out\exp2"
```

Chaque figure carrée contient quatre panneaux (2 × 2) : logspace à gauche, large à droite ; valeurs observées et prédites en haut, erreurs signées en bas. Les axes partagent les mêmes échelles par ligne. Couleurs reprises d'Exp 1 : bleu (#0072B2) pour rapidsnark ; rose (#CC79A7) pour snarkjs.

- `01_throughput` : débit et erreur relative en pourcentage.
- `02_elasticity` : dérivée analytique prédite face aux différences finies observées ; en bas, leur différence sans dimension. Cette comparaison utilise deux estimateurs différents.
- `03_interval_elasticity` : élasticités sur les mêmes intervalles, puis différence prédite moins observée. Les points sont reliés à leur milieu géométrique, sans lissage.

Courbes pointillées : mesures ; trait plein : modèle affine ; tirets : modèle avec puissance s'il est distinct. Quand le modèle avec puissance se réduit au même affine, une seule courbe est affichée par prover, avec la légende « Affine (power term = 0) ».

Pour 01 et 02, rond plein : taille en calibration ; rond vide : taille exclue. Pour 03, rond plein : deux extrémités en calibration ; triangle vide : une extrémité exclue ; carré vide : deux extrémités exclues. Seuls les marqueurs présents sont affichés dans la légende. Le trait vertical mixte indique la plus grande taille de calibration. Vert/orange en haut : hausse/baisse du débit. En bas, zéro est l'erreur idéale ; les erreurs d'élasticité ne sont pas des pourcentages.

Les données, paramètres et scores restent dans [logspace](logspace/README.md) et [large](large/README.md). Les ajustements restent indépendants pour chaque plan et chaque prover. Aucun point exclu ne participe à l'ajustement. Les scores moyens des deux plans ne portent pas forcément sur les mêmes tailles de validation.
