"""Recherche quantitative (§17, §36, §39, §40, §56) : labels, jeux de données, splits, baselines,
entraînement, calibration, stacking, évaluation, registre d'expériences, ablation JEV, prédicteur.

Rien ici n'envoie d'ordre ni ne touche un exchange. Tout résultat obtenu sur des données synthétiques
porte la mention « données synthétiques — aucune preuve d'alpha ».

Modules et responsabilité unique de chacun :

- ``synthetic`` : générateur seedé d'événements de marché (aucun alpha planté par défaut) ;
- ``datasets`` : assemblage point-in-time features + labels, hash de jeu de données, niveau de qualité ;
- ``labels`` : cibles par horizon, censures explicites, disponibilité temporelle de chaque label ;
- ``splits`` : walk-forward imbriqué, purge/embargo, période finale gelée, provenance des transformateurs ;
- ``baselines`` : benchmarks et famille LightGBM verrouillée, sérialisables sans pickle ;
- ``calibration`` : Brier, log-loss, ECE, fiabilité, Platt/isotone, couverture d'intervalles ;
- ``evaluation`` : PnL net, bootstrap par blocs communs, contrôles négatifs, benchmarks §39.4 ;
- ``training`` : entraînement walk-forward, sélection hors test final, carte et artefact de modèle ;
- ``stacking`` : experts + méta-modèle sur prédictions OOF TEMPORELLES (jamais de fold aléatoire) ;
- ``experiment_registry`` : plan enregistré avant exécution, tous les essais, période finale gelée ;
- ``jev_ablation`` : variantes A/B/C/D et rapports ``kind="jev_ablation"`` ;
- ``predictor`` : charge un modèle enregistré et publie des ``Forecast``. Il ne décide rien.

Les sous-modules ne sont PAS importés ici : plusieurs tirent scikit-learn ou LightGBM, et ``okxq.cli``
ne doit pas payer ce coût pour afficher une aide. Importer explicitement
``okxq.research.<module>`` est la façon voulue de les utiliser.
"""

SYNTHETIC_NOTICE = "données synthétiques — aucune preuve d'alpha"

__all__ = ["SYNTHETIC_NOTICE"]
