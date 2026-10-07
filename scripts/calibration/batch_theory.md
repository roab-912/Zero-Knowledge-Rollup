# Analyse de la théorie du batching

`batch_theory.py` analyse une campagne de calibration **gelée** et sa campagne
de validation indépendante. Il ne lance ni génération de circuits, ni preuve,
ni transaction L1. Il produit un nouveau dossier `bench-out/<date>_theory_<id>/`
avec **un seul rapport `result.json`** et cinq figures en PNG et PDF vectoriel.
Les résultats sources restent inchangés.

```powershell
python scripts/calibration/batch_theory.py --validation bench-out/20261007T093918_728693Z_validation_e4f15eaa/result.json --delta 300 --max-batch 512
```

Dépendances : celles de `batch_calibration.py`, plus NumPy, SciPy et Matplotlib.
Les chemins `/work/...` enregistrés dans Docker sont traduits vers la racine du
dépôt sur l'hôte. Après déplacement des campagnes, `--calibration` et `--model`
permettent de fournir leurs nouveaux emplacements. Les empreintes du modèle et
des données de calibration sont vérifiées, ainsi que la cohérence des campagnes.

## Sens de Δ et domaine admissible

Par défaut, **Δ = 300 secondes est un budget de traitement pour un batch**,
appliqué séparément au périmètre analysé. On maximise la capacité `N/T(N)` parmi
les tailles disponibles dont le temps moyen prédit ne dépasse pas Δ. La grille
provient du modèle gelé, filtrée par la présence des artefacts et `--max-batch`.
Les échecs de calibration excluent les tailles concernées de la recommandation.

La borne demandée (8192 par défaut) représente le domaine expérimental de circuits construits ;
elle ne constitue pas une estimation de la limite physique de génération des
preuves. Les réussites de validation confirment la faisabilité sur les essais
effectués. Si toutes les tailles passent, le maximum admissible **sur cette
grille** est sa plus grande taille, sans conclusion sur les tailles supérieures.

`--delta-mode cadence` étudie plutôt `N/max(Δ,T(N))`, toujours avec la contrainte
de non-saturation `T(N) <= Δ`. Si tous les temps sont inférieurs à 300 s, le débit
cadencé est `N/300` et son élasticité vaut 1 : son optimum est donc la plus grande
taille disponible. Ce débit est calculé, **pas mesuré avec un ordonnanceur**.
La version observée utilise `N / moyenne(max(Δ,T_i))` pour conserver les variations
entre essais. Le scheduler réel du prototype peut avoir un autre comportement,
notamment lorsqu'un batch dépasse la période.

La grille par défaut couvre toutes les puissances de deux de 1 à 8192 en
calibration et en validation. Une ancienne campagne plus petite conserve sa
grille : aucune observation n'est inventée. Chaque machine exige sa propre
calibration et sa propre validation. Le lanceur `run_batch_study.py` réalise
l'ensemble de la chaîne (voir `batch_calibration.md`).

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
Un modèle affine croissant ne produit pas d'optimum intrinsèque fini ; si la borne supérieure
est recommandée, la conclusion est « meilleur batch sur le domaine testé ».

Les phases mesurées sont séquentielles : leur temps total est une somme et non
un maximum. Leur part temporelle n'est **pas** l'indice B_x du modèle de ressources.
Les capacités physiques, budgets protocolaires, indices B_x/H_x et changements
de ressource limitante restent explicitement non identifiés. Les pics RSS
échantillonnés sont exportés comme diagnostics ; ils ne constituent pas des
budgets RAM ni des mesures exactes de pointe.

## Prédiction et expérimentation

Les modèles `proof_s`, `proof_generation_s` (témoin + preuve), `total_s`
(préparation + témoin + preuve) et `local_verified_s` sont désormais gelés
**avant** la validation. Toutes les tailles participent à la calibration ;
la validation porte sur de nouvelles transactions et exécutions à ces mêmes
tailles, sans réajustement. La MAPE principale utilise donc toutes les tailles.
Les anciens rapports avec tailles réservées conservent également leurs métriques.

Par défaut, une seule mesure est prise par taille dans chaque campagne, sans
warmup. Sans répétitions, aucun intervalle de variabilité n'est disponible.
Pour renforcer ensuite l'étude du plateau : `--repeat 10 --focus-repeat 30`
avec le lanceur, qui applique ces nombres aux deux campagnes.

Le périmètre `local_verified_s` additionne `total_s + verification_s` essai par
essai. Pour les anciennes campagnes où ce modèle n'a pas été gelé, l'analyse
conserve une extension rétrospective clairement signalée, ajustée uniquement
sur les mesures de calibration.

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

La vérification est désormais analysée séparément (`verification_s`) et gelée
avant validation pour les nouvelles campagnes. Pour les anciennes campagnes,
son modèle est ajusté sur la calibration uniquement et signalé comme rétrospectif.
Le vérificateur est **snarkjs dans les deux cas** : une seule série verte regroupe
les durées des preuves rapidsnark et snarkjs. Pour chaque taille et répétition,
on prend la moyenne des deux durées ; une paire reste une seule observation.
Une paire incomplète ou en échec est exclue et signalée. Cela n'invente donc pas
de répétitions supplémentaires lorsque le protocole n'en demande qu'une.
Les diagnostics individuels sont conservés dans `provers`, la série commune
dans `verification`. Son modèle commun est gelé avant les nouvelles validations.

Dans `01_elasticity`, la courbe de vérification est verte et en tirets.
Dans `02_theory_vs_experiment`, la même série commune est verte dans
chacun des deux panneaux ; l'axe des débits devient logarithmique pour garder les
deux étapes lisibles. Aucun panneau inférieur n'est réintroduit.
`05_verification` présente T, T/N, N/T, γ et ξ pour cette unique série.
Le débit N/T compte les transactions couvertes par les preuves vérifiées :
ce n'est ni un nombre de preuves par seconde (1/T), ni le débit complet du rollup.

Le pourcentage du titre de chaque panneau 02 est la MAPE de **génération**
(témoin + preuve), calculée sur toutes les tailles de validation avec le même
poids par taille : `100 * moyenne(|débit_prédit / débit_observé - 1|)`.
Il ne concerne pas la courbe verte et ne représente pas une probabilité de justesse.

- `01_elasticity` : T, T/N, N/T, puis γ et ξ (deux axes liés), avec modèle,
  nouvelles observations et sécantes d'élasticité.
- `02_theory_vs_experiment` : débit prédit et observé, intervalles de mesure,
  MAPE sur toutes les tailles ; deux panneaux uniquement (un par prover). Les erreurs détaillées restent dans le JSON.
- `03_local_processing` : mêmes grandeurs pour le coût local avec vérification.
- `04_phase_durations` : préparation, témoin, preuve et vérification, pour
  identifier l'étape qui prend le plus de temps sans l'assimiler à une ressource.

```powershell
python -m unittest discover -s scripts/calibration -p test_batch_theory.py -v
```

## Comment trouver l'optimum ?

On maximise `Λ(N)=N/T(N)` sur la grille admissible. Sa dérivée vaut
`Λ'(N) = [T(N) - N T'(N)] / T(N)^2` : le candidat continu satisfait
`N T'(N)=T(N)`, donc `γ=1` et `ξ=0`. Un maximum intérieur exige le passage de
ξ de positif à négatif. On compare ensuite les débits sur toutes les tailles
discrètes disponibles, puis le batch choisi au meilleur débit de validation.

`optimum_kind` distingue un maximum intérieur prédit, une borne expérimentale,
un objectif limité par la deadline et un plateau exactement constant.
`optimal_batch_predicted_without_deadline` et
`optimal_batch_observed_without_deadline` rendent explicite l'effet du budget.
`predicted_sizes_within_2_percent_of_best` donne une zone de quasi-optimalité
**descriptive**, sans preuve d'équivalence statistique.

Les coefficients d'un modèle affine positif ne peuvent pas produire de baisse
du débit : il n'existe alors pas de maximum intérieur prédit. Les familles
superlinéaires restent candidates, sans être forcées. Mesurer toutes les tailles
permet au modèle de voir une éventuelle baisse à 8192 ; cela ne garantit ni sa
sélection, ni un optimum unique. Avec une mesure par taille, les résultats
restent exploratoires même si un optimum discret est calculable.
