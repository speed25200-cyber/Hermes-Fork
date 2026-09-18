"""Simulation d'exécution (§33) : latence, files d'attente, fills, exchange virtuel et moteur de replay.

Aucune logique de stratégie ne vit ici : le moteur appelle un ``DecisionCallback`` injecté et partage le
domaine (positions, ledger, coûts) avec les modes PAPER/SHADOW/DEMO.
"""
