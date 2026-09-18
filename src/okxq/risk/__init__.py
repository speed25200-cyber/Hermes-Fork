"""Risk Engine indépendant (§25, §26, §53, §54).

Le modèle propose, ce paquet DÉCIDE : limites multi-niveaux (``budgets``), scénarios de perte
(``scenarios``), décision liée au hash exact du payload (``engine``), recontrôle à l'envoi et
réservations d'exposition (``approvals``), kill switches persistants (``kill_switch``) et watchdog à
heartbeats conditionnés (``watchdog``). Aucun module ici ne parle à l'exchange ni ne voit une clé.
"""
