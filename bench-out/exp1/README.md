# Exp 1 — dataset complet

Source : `D:\Documents\Code\GitHub\Zero-Knowledge-Rollup\bench-out\20260825_120620`.

Reproduction : `python scripts/analysis/exp1.py --from bench-out/20260825_120620`. Ajouter `--phases prove verify env` pour inclure le setup ponctuel. Les options de cette exécution sont conservées dans `analysis.json`.

Cette expérience décrit les mesures sur toutes les tailles disponibles. Elle ne calibre pas de modèle prédictif (Exp 2). Les phases prove restent séparées par prover. Une seule série verify regroupe, à chaque taille, les répétitions réussies du même vérificateur présentes dans steps.csv (20 par taille dans ce run). Les moyennes, écarts-types population (ddof=0) et maxima sont recalculés sur ces observations, puis les élasticités sur les moyennes regroupées. Les données brutes restent séparées et inchangées ; la fusion suppose des conditions comparables.

## Correspondance avec le papier

| Grandeur | Interprétation et unité |
|---|---|
| T_p(N) | Temps écoulé d'une phase p, en s ; candidat empirique à un temps de service, pas T_x d'une ressource isolée. |
| W_CPU,p(N) | Temps CPU cumulé, en secondes-cœur (travail consommé). Ce n'est pas un nombre d'opérations. |
| R_eff,p(N) | W_CPU,p / T_p, en secondes-cœur/s ; utilisation moyenne observée, pas capacité physique. |
| W_CPU,p/N | Travail CPU moyen par transaction, en secondes-cœur/tx. |
| Lambda_p(N) = N/T_p(N) | Débit équivalent de la phase isolée, pas débit mesuré du rollup en régime établi. |
| gamma_CPU, xi_CPU | d ln W_CPU / d ln N et 1 - gamma_CPU. |
| gamma_Lambda,p | d ln(N/T_p)/d ln N = xi_CPU + d ln R_eff/d ln N. |
| W_IO | Volumes lus/écrits observés, en octets ; capacités de stockage non mesurées. |
| M_p^peak(N) | Moyenne des pics RSS des répétitions ; occupation mémoire, ni travail cumulatif ni capacité en work/s. |

L'indice p désigne une phase expérimentale ; x reste réservé aux ressources du modèle. Les notations Lambda_p, gamma_Lambda,p, R_eff,p et M_p^peak explicitent la correspondance expérimentale. Dans le papier, R_x^phys constant implique d ln T_x / d ln N = gamma_x, puis d ln Lambda_x / d ln N = xi_x. Pour nos phases, gamma_Lambda,p = xi_CPU,p + d ln R_eff,p / d ln N : on distingue donc gamma_Lambda,p et xi_CPU,p. L'élasticité du temps reste exportée dans le CSV comme grandeur intermédiaire et apparaît en 03.C sur l'axe droit, associé à l'élasticité du débit sur l'axe gauche. Toutes les élasticités sont sans dimension.

Les dérivées utilisent des différences finies logarithmiques centrées, unilatérales aux extrémités. Elles décrivent les moyennes mesurées et peuvent amplifier le bruit. Les écarts-types et nombres de répétitions sont exportés ; aucune incertitude de dérivée n'est estimée. Un passage ponctuel de gamma par 1 ne démontre ni plateau ni optimum global.

## Figures et tables

- `01_times` : temps T_p, temps/transaction, débit équivalent Lambda_p et élasticité du débit gamma_Lambda,p.
- `02_work_resources` : travail CPU (A), travail/transaction (B), moyenne des pics mémoire par batch (C) et par transaction (D).
Le panneau 02.D divise la moyenne des pics RSS par N : il décrit une occupation mémoire normalisée par transaction, pas une mesure de mémoire propre à chaque transaction ni un travail cumulatif.
- `03_elasticities` : A amortissement CPU, B amortissement de l'occupation RAM, C élasticité du débit ; emplacement D vide. A et B : xi à gauche, gamma = 1 - xi à droite. C : gamma_Lambda,p à gauche, gamma_T,p = d ln T_p / d ln N = 1 - gamma_Lambda,p à droite. Les axes gauches croissent tous vers le haut : plus haut signifie un gain relatif plus grand, vert au-dessus de zéro et orange en dessous. Les axes droits sont inversés (0 à gauche = 1 à droite ; 1 à gauche = 0 à droite).
Pour la RAM, gamma_RAM,p = d ln M_p^peak / d ln N et xi_RAM,p = -d ln(M_p^peak/N) / d ln N. Cet amortissement décrit la diminution de l'occupation mémoire normalisée par transaction ; il n'est pas automatiquement égal à l'élasticité du débit.
- `04_amortization_vs_throughput` : comparaison de xi_CPU,p, xi_RAM,p et gamma_Lambda,p dans un panneau par prover et un pour verify ; mêmes échelles pour tous les panneaux. Le setup occupe le quatrième panneau si env est demandé.
La grille 2 × 2 et les proportions des panneaux sont conservées ; les panneaux restants ne sont pas étirés.
- `exp1.csv` : toutes les grandeurs par phase, prover et taille.
- `observed_optima.csv` : maximum du débit par phase parmi les tailles mesurées admissibles.
- `circuits.csv` : contraintes et tailles d'artefacts du manifeste (descripteurs, pas travail CPU ni volume DA publié).

Les fonds des panneaux d'élasticité reprennent la convention de legacy : vert lorsque le coût par transaction diminue (gamma < 1 ou xi > 0), orange lorsqu'il augmente. Pour gamma_Lambda,p, vert signifie débit croissant (gamma_Lambda,p > 0), orange débit décroissant. La ligne pointillée marque le seuil de gain marginal nul. R_eff, son élasticité et les volumes d'E/S restent disponibles dans exp1.csv, mais ne sont plus représentés dans les figures principales.

Dans la figure 04, la courbe bleue continue représente l'amortissement du travail CPU (1 - gamma_CPU,p), et la courbe rose discontinue l'élasticité du débit équivalent (d ln Lambda_p / d ln N). La courbe verte à tirets-points représente l'amortissement du pic mémoire (xi_RAM,p = 1 - gamma_RAM,p). Il ne s'agit pas de la moyenne temporelle de la mémoire et aucune égalité avec l'élasticité du débit n'est supposée. Le fond vert indique une élasticité positive : diminution du travail CPU/transaction pour xi, augmentation du débit pour gamma_Lambda,p ; l'orange indique l'inverse. L'écart eta - xi est la dérivée logarithmique de R_eff. Cette comparaison illustre les observations ; elle ne démontre pas indépendamment l'identité théorique. L'égalité du papier suppose une capacité de ressource constante et un débit associé à cette ressource, tandis que nos temps sont mesurés par phase.

La notation gamma_Lambda,p remplace eta_p pour identifier explicitement la grandeur dérivée : le débit Lambda_p. Elle ne doit pas être confondue avec gamma_CPU,p, l'élasticité du travail. Le CSV exporte gamma_throughput et conserve eta_throughput comme alias pour compatibilité.

## Constats sur ce run

- rapidsnark/prove : 14 tailles, N=1…8192 ; R_eff : 1.684 à 7.077 secondes-cœur/s.
- snarkjs/prove : 14 tailles, N=1…8192 ; R_eff : 3.718 à 5.647 secondes-cœur/s.
- verify : 14 tailles, N=1…8192 ; R_eff : 3.664 à 3.797 secondes-cœur/s.

Le manifeste annonce 32 CPU logiques et 128.0 GB de RAM. Le nombre de CPU est une information de configuration, pas une calibration de capacité soutenable. Les limites d'affinité, de cgroup, la fréquence et la contention ne sont pas établies ici.

## Optimum et contraintes

Sans --deadline, le critère est N/T_p. Avec --deadline Delta, on retient les tailles où T_p <= Delta et maximise N/Delta. --ram-budget filtre sur le maximum des pics RSS des répétitions. Ces critères concernent une phase isolée et ne prouvent pas la faisabilité du système complet. Le budget RAM n'est pas automatiquement la RAM totale de la machine. Aucune extrapolation, interpolation de franchissement ou borne d'amortissement artificielle n'est utilisée.

- rapidsnark/prove : N retenu = 8192, critère N/T_phase.
- snarkjs/prove : N retenu = 4096, critère N/T_phase.
- verify : N retenu = 8192, critère N/T_phase.

## Ce que le dataset ne permet pas d'identifier

Le modèle suppose R_x^phys constant dans chaque configuration. R_eff varie avec N : les élasticités CPU et temporelle ne sont donc pas interchangeables. W_CPU/R_eff=T_p est une identité descriptive, pas une validation indépendante de T_x=W_x/R_x^phys.

Les phases prove et verify consomment plusieurs ressources et peuvent partager la machine. Leur maximum ne donne pas automatiquement tau. On ne calcule donc pas B_x, H_x, les transitions de bottleneck, Lambda_svc du système ou N_max physique/protocolaire sans capacités calibrées, allocation des ressources, mesures concurrentes et limites K_x^prot. La vérification de ce run est locale : elle ne mesure pas le gas ou la latence L1. La phase env est un setup ponctuel et ne doit pas être ajoutée au coût récurrent de chaque batch sans une politique explicite d'amortissement.

W_x,0 et W_x,v ne sont pas séparément identifiables à partir des seuls totaux sans hypothèse de forme ou expérience dédiée. Un ajustement affine/superlinéaire et sa validation relèvent de l'Exp 2.

## Mesures à ajouter ultérieurement à measure_zk_resources.py

Les données actuelles suffisent pour les figures ci-dessus ; le collecteur n'a pas été modifié pour cette expérience.

- Enregistrer affinité CPU, quotas cgroup, nombre de threads, fréquence, RAM disponible et portée/source de chaque compteur (processus ou système).
- Calibrer séparément les capacités CPU et I/O sous allocation fixe ; si W désigne des opérations, mesurer instructions/cycles et leur capacité correspondante.
- Mesurer plusieurs batches concurrents : intervalle entre sorties, cadence de release, latence, contention et allocation par phase pour tester tau et Lambda_svc.
- Mesurer les volumes réellement publiés, le gas de vérification et les capacités/limites protocolaires avec leurs unités.
- Améliorer si nécessaire les compteurs E/S des processus courts : l'échantillonnage peut manquer une partie de l'activité ; zéro n'est pas une preuve d'absence d'E/S.

Les anciens résultats elasticity sont conservés sous `legacy/`, uniquement comme archive ; leurs conclusions ne sont pas celles d'Exp 1.
