"""Features point-in-time (§7–§11, §35).

Une seule implémentation de référence des formules (``okxq.features.registry.FeatureEngine``) sert à la
recherche (assemblage batch en polars, ``okxq.features.batch``) et au direct (état incrémental,
``okxq.features.incremental``). Toute feature porte un masque OK/MISSING/STALE/INVALID ; une valeur
absente n'est jamais remplacée par zéro.
"""
