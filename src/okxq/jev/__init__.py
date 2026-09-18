"""JEV (TypeSafe AI) : composant sémantique ISOLÉ (§13, §14, §49, §50).

Ce sous-système ne voit jamais les clés OKX, les positions, le patrimoine ni l'identité du compte.
Il reçoit des documents publics (données non fiables), pose des questions étroites au modèle et rend
des évaluations datées. Il ne décide de rien : le Risk Engine et le gateway restent seuls maîtres.

Aucun appel réel au fournisseur n'est effectué par les tests ; ``okxq.jev.quality.FixtureJevEvaluator``
est un évaluateur FACTICE explicitement étiqueté comme tel.
"""
