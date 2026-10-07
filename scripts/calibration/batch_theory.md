# Analyse de la théorie du batching

`batch_theory.py` analyse une campagne de calibration **gelée** et sa campagne
de validation indépendante. Il ne lance ni génération de circuits, ni preuve,
ni transaction L1. Il produit un nouveau dossier `bench-out/<date>_theory_<id>/`
avec **un seul rapport `result.json`** et quatre figures en PNG et PDF vectoriel.
Les résultats sources restent inchangés.

```powershell
python scripts/calibration/batch_theory.py --validation bench-out/20261007T093918_728693Z_validation_e4f15eaa/result.json --delta 10 --max-batch 512
```

Dépendances : celles de `batch_calibration.py`, plus NumPy, SciPy et Matplotlib.
Les chemins `/work/...` enregistrés dans Docker sont traduits vers la racine du
dépôt sur l'hôte. Après déplacement des campagnes, `--calibration` et `--model`
permettent de fournir leurs nouveaux emplacements. Les empreintes du modèle et
des données de calibration sont vérifiées, ainsi que la cohérence des campagnes.

## Sens de Δ et domaine admissible

Par défaut, **Δ = 10 secondes est un budget de traitement pour un batch**,
appliqué séparément au périmètre analysé. On maximise la capacité `N/T(N)` parmi
les tailles disponibles dont le temps moyen prédit ne dépasse pas Δ. La grille
provient du modèle gelé, filtrée par la présence des artefacts et `--max-batch`.
Les échecs de calibration excluent les tailles concernées de la recommandation.

La borne 512 représente ici le domaine expérimental de circuits construits ;
elle ne constitue pas une estimation de la limite physique de génération des
preuves. Les réussites de validation confirment la faisabilité sur les essais
effectués. Si toutes les tailles passent, le maximum admissible **sur cette
grille** est 512, sans conclusion sur les tailles supérieures.

`--delta-mode cadence` étudie plutôt `N/max(Δ,T(N))`, toujours avec la contrainte
de non-saturation `T(N) <= Δ`. Si tous les temps sont inférieurs à 10 s, le débit
cadencé est `N/10` et son élasticité vaut 1 : son optimum est donc la plus grande
taille disponible. Ce débit est calculé, **pas mesuré avec un ordonnanceur**.
La version observée utilise `N / moyenne(max(Δ,T_i))` pour conserver les variations
entre essais. Le scheduler réel du prototype peut avoir un autre comportement,
notamment lorsqu'un batch dépasse la période.

Pour une future campagne réellement mesurée jusqu'à 8192, l'analyse accepte
`--max-batch 8192` et utilise sa grille gelée ; elle n'extrapole pas la présente
campagne pour inventer des observations manquantes. Le collecteur actuel reste
configuré sur 1–512 ; sa grille devra être étendue avant la nouvelle campagne.
Chaque machine exige sa propre calibration et sa propre validation.

## Correspondance avec la théorie

Pour chaque prover et chaque périmètre, le modèle donne un temps effectif
`T(N) = W(N)/R`. Les mesures de temps n'identifient pas séparément W et R.
Les familles possibles sont affine, `α + βN + cN ln N` et `α + βN + cN^p`.
Le rapport calcule :

- le temps de service T, le coût moyen T/N et la capacité Λ = N/T ;
- les contributions fixe α/N et variable (T−α)/N, et la fraction fixe α/T ;
- γ = NT′/T et ξ = 1−γ, ainsi que l'élasticité du débit cadencé ;
- l'utilisation T/Δ et la marge 1−T/Δ du **budget temporel** ;
- le maximum admissible, l'optimum discret sur les puissances de deux et,
  lorsqu'il existe, le maximum continu de N/T ;
- la distribution bootstrap de la taille recommandée et les intervalles à 95 %.

Le maximum continu vaut `α/c` pour le modèle N ln N, et
`[α/((p−1)c)]^(1/p)` pour le modèle puissance lorsque les coefficients sont
positifs et que le point appartient à N > 1. Le choix discret compare réellement
les tailles admissibles : il ne se contente pas d'arrondir le maximum continu.
Un modèle affine croissant ne produit pas d'optimum intrinsèque fini ; si 512
est recommandé, la conclusion est « meilleur batch sur le domaine testé ».

Les phases mesurées sont séquentielles : leur temps total est une somme et non
un maximum. Leur part temporelle n'est **pas** l'indice B_x du modèle de ressources.
Les capacités physiques, budgets protocolaires, indices B_x/H_x et changements
de ressource limitante restent explicitement non identifiés. Les pics RSS
échantillonnés sont exportés comme diagnostics ; ils ne constituent pas des
budgets RAM ni des mesures exactes de pointe.

## Prédiction et expérimentation

Les modèles `proof_s`, `proof_generation_s` (témoin + preuve) et `total_s`
(préparation + témoin + preuve) sont ceux gelés **avant** la validation. Aucun
coefficient n'est réajusté sur la validation. Les répétitions de validation sont
nouvelles pour toutes les tailles ; `2,8,32,128,512` sont en plus des tailles
absentes de la calibration actuelle. Le point 512 est une extrapolation au-delà
de la plus grande taille de calibration, 256, évaluée sur de vraies mesures.

Le périmètre `local_verified_s` additionne `total_s + verification_s` **essai par
essai**. Il est modélisé uniquement à partir des données de calibration. Cette
extension est rétrospective : son modèle n'était pas gelé avant la collecte de
validation. Il faut une nouvelle validation prospective pour une affirmation
confirmatoire sur ce nouveau périmètre.

Ce coût local est une somme d'intervalles instrumentés. Il n'inclut ni DA, ni L1,
ni pool, ni attente, ni persistance complète du prototype ; il ne remplace donc
pas une mesure de bout en bout de `ZKRollup.compute_batch`. Les essais existants
exécutent les composants locaux et vérifient réellement les preuves. Une future
expérimentation du prototype complet devra mesurer les mêmes limites temporelles
que le modèle étendu, avec ses propres répétitions indépendantes et son état de
chaîne explicitement décrit.

La précision est rapportée par MAPE, erreur relative signée, erreur maximale,
RMSE et erreur logarithmique, pour toutes les tailles puis pour les tailles
réservées. Une MAPE de 5 % n'est pas une probabilité de réussite de 95 %.
La qualité du choix de batch se mesure aussi par le regret
`1 − débit_observé(N_prédit) / meilleur_débit_observé`, avec bootstrap.
Les échecs restent visibles ; aucun débit nul fictif ne leur est attribué.

Pour l'élasticité, les observations utilisent la sécante
`log2(Λ(2N)/Λ(N))`. On compare cette valeur à la **même sécante** du modèle,
et non directement à sa dérivée analytique. Les points sont placés au milieu
géométrique de [N,2N]. L'erreur est absolue, car une erreur relative devient
instable près de ξ = 0.

Les intervalles sont ponctuels à 95 %, conditionnels à la famille et à p choisis.
Ils n'intègrent ni l'incertitude de sélection du modèle, ni les biais du banc,
ni les variations entre machines. L'admissibilité prédite porte sur la moyenne ;
le rapport indique séparément les dépassements individuels observés.

## Figures

- `01_elasticity` : T, T/N, N/T, puis γ et ξ (deux axes liés), avec modèle,
  nouvelles observations et sécantes d'élasticité.
- `02_theory_vs_experiment` : débit prédit et observé, intervalles de mesure,
  erreurs par taille et MAPE sur tailles réservées.
- `03_local_processing` : mêmes grandeurs pour le coût local avec vérification.
- `04_phase_durations` : préparation, témoin, preuve et vérification, pour
  identifier l'étape qui prend le plus de temps sans l'assimiler à une ressource.

```powershell
python -m unittest discover -s scripts/calibration -p test_batch_theory.py -v
```
