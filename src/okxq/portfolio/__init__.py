"""Portefeuille (§21, §22, §24, §37, §51) : modèle de coûts, edges depuis les prévisions, covariance,
optimisation convexe, arrondi en contrats et génération des intentions d'ordre.

Le module propose ; il ne décide rien : chaque intention passe ensuite par le Risk Engine indépendant
(``okxq.risk``) qui seul produit une ``RiskDecision``.
"""
