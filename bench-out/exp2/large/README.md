# Exp 2 — extrapolation du débit

Source : `D:\Documents\Code\GitHub\Zero-Knowledge-Rollup\bench-out\20260825_120620`. Plan de calibration : large, plafond N=4096. Validation : toutes les tailles non sélectionnées. Exposant fixé a priori : p=2.

```powershell
python scripts/analysis/exp2.py --train-max 4096 --design large --calibration-count 6 --power 2 --phases prove --tolerance 0.05 --from "D:\Documents\Code\GitHub\Zero-Knowledge-Rollup\bench-out\20260825_120620" --outdir "bench-out\exp2\large"
```

Tailles retenues : rapidsnark/prove : [1, 256, 512, 1024, 2048, 4096]; snarkjs/prove : [1, 256, 512, 1024, 2048, 4096]

Les tailles sont sélectionnées sans consulter leurs temps : prefix prend toutes les tailles jusqu'au plafond ; logspace répartit un budget de tailles sur l'échelle logarithmique ; large conserve la plus petite et les plus grandes tailles éligibles ; manual prend la liste fournie. Une taille exclue à l'intérieur du domaine de calibration teste l'interpolation ; hors de ce domaine, elle teste l'extrapolation. metrics.csv distingue les deux.

Les mesures sont chargées comme dans Exp 1 : même moyenne des temps par taille, même regroupement des répétitions verify. Le débit est celui d'une phase isolée, pas celui du rollup complet. CPU et RAM ne participent pas à cet ajustement.

## Modèles issus des formes du papier

- Affine : T(N)=t0+bN ; Lambda=N/T ; gamma_Lambda=t0/T.
- Affine avec terme superlinéaire : T(N)=t0+bN+aN^p ; gamma_Lambda=[t0-(p-1)aN^p]/T.

Les coefficients sont non négatifs. On ajuste log(T_prédit)-log(T_mesuré), avec un poids égal par taille, uniquement sur la calibration. Cela équivaut à minimiser les écarts logarithmiques du débit. Les erreurs de mesure ne servent pas de poids. Les contributions normalisées <1e-10 sont mises à zéro.

Ces formes reprennent W=W0+wN et W=W0+wN+aN^p du papier en les appliquant au temps de phase. Sous capacité constante, leurs coefficients seraient ceux du travail divisés par la capacité. Ici, ce sont des modèles empiriques de temps : ni W ni R_phys ne sont identifiés séparément.

Les deux modèles sont présentés sans sélection sur les tailles de validation. Modifier p ou la coupure après avoir vu la validation constitue une exploration, pas une nouvelle validation indépendante. L'affine sert de référence sans optimum fini intrinsèque ; le terme superlinéaire permet de tester une dégradation.

## Résultats hors calibration

Tolérance pratique choisie : 5 %. Ce seuil est configurable avec --tolerance et ne représente pas un niveau de confiance statistique.

| Phase | Modèle | Domaine | Tailles | Erreur débit MAPE (%) | Erreur maximale (%) | Tailles dans la tolérance (%) |
|---|---|---|---:|---:|---:|---:|
| rapidsnark/prove | affine | interpolation | 7 | 1.75 | 2.99 | 100.0 |
| rapidsnark/prove | affine | extrapolation | 1 | 6.91 | 6.91 | 0.0 |
| rapidsnark/prove | superlinear | interpolation | 7 | 1.75 | 2.99 | 100.0 |
| rapidsnark/prove | superlinear | extrapolation | 1 | 6.91 | 6.91 | 0.0 |
| snarkjs/prove | affine | interpolation | 7 | 4.18 | 9.96 | 71.4 |
| snarkjs/prove | affine | extrapolation | 1 | 6.33 | 6.33 | 0.0 |
| snarkjs/prove | superlinear | interpolation | 7 | 4.18 | 9.96 | 71.4 |
| snarkjs/prove | superlinear | extrapolation | 1 | 6.33 | 6.33 | 0.0 |

## Fidélité des gains et de l'élasticité

La comparaison principale de l'élasticité utilise désormais les mêmes intervalles [Ni, Nj] entre tailles adjacentes pour le modèle et les mesures :

- Facteur de gain : G = Lambda(Nj)/Lambda(Ni) ; G > 1 signifie un débit croissant.
- Élasticité moyenne sur l'intervalle : gamma_bar = ln(G)/ln(Nj/Ni). C'est une pente logarithmique, pas la dérivée ponctuelle à une extrémité.
- Erreur de gain (%) : 100 × (G_prédit/G_observé - 1). Le dénominateur est le facteur G, pas G-1 : la mesure reste définie sur un plateau.
- Erreur d'élasticité : |gamma_bar_prédit - gamma_bar_observé|, sans dimension. On évite un pourcentage relatif à une élasticité proche de zéro.

Ces deux erreurs décrivent le même écart : ln(G_prédit/G_observé) = (gamma_bar_prédit - gamma_bar_observé) × ln(Nj/Ni). Elles ne constituent pas deux validations indépendantes. Un biais multiplicatif constant du débit s'annule dans les gains : il faut aussi conserver les erreurs de débit ci-dessus.

mixed : un seul point de l'intervalle est hors calibration ; heldout : les deux le sont. Un intervalle est classé en extrapolation dès qu'une extrémité sort du domaine de calibration. Les intervalles voisins partagent des mesures, donc leurs erreurs ne sont pas indépendantes.

| Phase | Modèle | Points utilisés | Domaine | Intervalles | Erreur gain MAPE (%) | Erreur élasticité MAE |
|---|---|---|---|---:|---:|---:|
| rapidsnark/prove | affine | mixed | interpolation | 2 | 0.42 | 0.0061 |
| rapidsnark/prove | affine | mixed | extrapolation | 1 | 3.45 | 0.0507 |
| rapidsnark/prove | affine | heldout | interpolation | 6 | 0.82 | 0.0118 |
| rapidsnark/prove | superlinear | mixed | interpolation | 2 | 0.42 | 0.0061 |
| rapidsnark/prove | superlinear | mixed | extrapolation | 1 | 3.45 | 0.0507 |
| rapidsnark/prove | superlinear | heldout | interpolation | 6 | 0.82 | 0.0118 |
| snarkjs/prove | affine | mixed | interpolation | 2 | 1.61 | 0.0230 |
| snarkjs/prove | affine | mixed | extrapolation | 1 | 10.38 | 0.1424 |
| snarkjs/prove | affine | heldout | interpolation | 6 | 2.15 | 0.0306 |
| snarkjs/prove | superlinear | mixed | interpolation | 2 | 1.61 | 0.0230 |
| snarkjs/prove | superlinear | mixed | extrapolation | 1 | 10.38 | 0.1424 |
| snarkjs/prove | superlinear | heldout | interpolation | 6 | 2.15 | 0.0306 |

Pour comparer logspace et large, utiliser exactement les mêmes tailles ou intervalles réservés. Les moyennes globales ne portent pas nécessairement sur les mêmes points. Par exemple, avec les plans logspace et large plafonnés à 4096 sur le dataset initial, l'intervalle [4096, 8192] permet une comparaison commune : son point gauche est utilisé en calibration, son point droit en validation.

Pour rapidsnark/prove, snarkjs/prove, le coefficient superlinéaire ajusté est nul à la précision numérique retenue. Les deux modèles se réduisent à une forme affine. Lorsque leurs coefficients affines coïncident à une tolérance relative de 1e-6 (sans tolérance absolue), les figures 01, 02 et 03 affichent une seule courbe, intitulée « Affine (power term = 0) ». Les deux ajustements restent exportés dans les CSV et analysis.json. La calibration n'identifie pas de terme provoquant un retournement futur. Cela n'établit pas son absence aux grandes tailles.

L'élasticité de référence est estimée par différences finies logarithmiques sur le dataset complet, comme dans Exp 1. Aux extrémités, la différence est unilatérale. Autour de la coupure, une pente peut utiliser des points des deux domaines, mais ces pentes ne sont jamais utilisées pour ajuster le modèle. La référence n'est pas une vérité exacte : elle dépend du bruit et de la grille.

La figure 02 et les anciens champs gamma_MAE/gamma_RMSE conservent la comparaison de la dérivée analytique du modèle avec cette référence discrète. `gamma_same_estimator_MAE` applique aussi les différences finies aux prédictions pour comparer avec le même estimateur. Les erreurs d'élasticité sont absolues (sans dimension), car une erreur relative près de zéro serait trompeuse.

## Taille optimale

Si t0>0 et a>0 : N0=[t0/((p-1)a)]^(1/p). Sinon aucun optimum intérieur isolé n'est identifié par cette formule. Pour le modèle affine à t0>0, le débit est croissant. Si les termes fixe et superlinéaire sont nuls, le débit est constant et l'optimum n'est pas unique.

La comparaison porte sur le même ensemble discret de tailles testées, de 1 au maximum du dataset. Le maximum observé est choisi après calibration uniquement pour évaluer la prédiction. Le regret est la perte de débit observée au choix prédit, relativement au meilleur débit observé. Les optima continus et entiers intermédiaires sont des prédictions non validées par des circuits de ces tailles. Le domaine testé n'est pas N_max physique ou protocolaire ; aucune contrainte RAM, release ou gas n'est imposée.

Le critère principal pour la recommandation est le regret : 100 × [1 - Lambda_observée(N_recommandé)/max_N Lambda_observée(N)]. La recommandation satisfait la tolérance si ce regret ne la dépasse pas. Toutes les tailles conservant au moins (1-tolérance) du meilleur débit observé sont exportées dans observed_near_optimal_sizes. Cette évaluation est rétrospective sur toute la grille, qui inclut les tailles de calibration ; elle ne remplace pas la validation sur des points exclus ou sur un nouveau run.

| Phase | Modèle | N prédit (grille) | N observé (grille) | Regret (%) | Dans la tolérance |
|---|---|---:|---:|---:|---|
| rapidsnark/prove | affine | 8192 | 8192 | 0.00 | oui |
| rapidsnark/prove | superlinear | 8192 | 8192 | 0.00 | oui |
| snarkjs/prove | affine | 8192 | 4096 | 6.90 | non |
| snarkjs/prove | superlinear | 8192 | 4096 | 6.90 | non |

Un maximum à la borne supérieure ne démontre pas l'existence d'un optimum fini. Un N0 extrapolé loin de la calibration est particulièrement fragile : un très petit coefficient superlinéaire peut produire un optimum très lointain. Un bon ajustement local ne garantit pas une bonne extrapolation.

## Fichiers et portée

Les figures 01, 02 et 03 sont regroupées dans le dossier parent : [figures comparatives](../README.md). Colonnes : logspace/large ; lignes : valeurs/erreurs ; couleurs : rapidsnark/snarkjs.

- `predictions.csv` : valeurs mesurées/prédites et erreurs par taille.
- `metrics.csv` : scores distincts calibration/validation/interpolation/extrapolation.
- `interval_predictions.csv` : gains et élasticités comparés sur les mêmes intervalles.
- `interval_metrics.csv` : erreurs par rôle des extrémités et domaine de validation.
- `calibration_plan.csv` : tailles à mesurer et coût historique indicatif de collecte.
- `optima.csv` : optima et regret sur les tailles testées.
- `analysis.json` : paramètres ajustés, unités, configuration et empreintes des sources.

Cette première expérience quantifie une précision prédictive sur un run. Elle ne prouve pas la véracité universelle du modèle. Aucun intervalle de confiance n'est estimé ici. Pour tester la robustesse, varier la coupure (--train-max), conserver les sorties dans des dossiers distincts et, ensuite, valider sur des runs indépendants. Le bruit sur les élasticités et les différences faibles entre meilleurs débits demandent une analyse des répétitions. Avec une seule taille de validation, le score est une erreur ponctuelle, pas une estimation de précision sur une plage. Pour comparer des coupures, comparer aussi les erreurs aux mêmes tailles réservées à la validation.
