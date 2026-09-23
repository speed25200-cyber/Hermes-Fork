# Résultats mesurés

Tout ce qui suit est **hors échantillon** (walk-forward purgé : chaque prédiction vient d'un modèle
entraîné uniquement sur le passé) et **net de frais, spread, impact et funding** (OKX, 2 pb maker /
5 pb taker, 60 % d'exécution passive supposée). Données : archives Binance USDT-M, contrats délistés
compris, univers point-in-time des ~30 contrats les plus liquides (15 pour le 1 min), juin 2022 → août
2026 (le mois en cours est retiré tant que son funding n'est pas publié). Rapports complets dans
`reports/`, registre des essais dans `reports/trials/` : chaque configuration testée dégonfle le Sharpe
(DSR). Tableau régénérable par `hermes research compare reports/<dossiers>`.

> **Aucune configuration n'a franchi la porte de promotion à ce jour ; le système refuse donc de trader de
> l'argent réel.** Les meilleures (horizons 4-48 h) sont rentables sur l'ensemble de la période (+11 à
> +15 %/an nets, Sharpe jusqu'à 0,93) et, à détention 24 h, résistent à des coûts doublés ; mais le gain
> dépend du régime (2024) et la significativité après correction des essais multiples n'est pas atteinte :
> la porte les rejette, à juste titre.

## Tous les essais

| Essai | Bougie | Horizons | Hors échantillon | IC (t) | P&L brut/an | Coûts/an | CAGR net | Sharpe net | Drawdown | DSR | p nul | PBO | Sharpe coûts×2 | Sharpe +1 barre | Promu |
|---|---|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|:-:|
| `research_15m` | 15 min | 30 min-2 h | 2023-07 → 2026-08 | 0,053 (34,8) | 3,5 % | 12,4 % | −8,9 % | −1,75 | −25 % (arrêt) | 0,00 | 1,00 | 0,35 | −1,12 | −2,62 | ❌ |
| `research_30m` | 30 min | 1-4 h | 2023-07 → 2026-08 | 0,054 (25,6) | 5,3 % | 14,3 % | −8,8 % | −2,14 | −25 % (arrêt) | 0,00 | 1,00 | 0,13 | −1,25 | −1,55 | ❌ |
| `research_15m` + lissage 1 horizon, plancher 0,5 (E1) | 15 min | 30 min-2 h | 2023-07 → 2026-08 | 0,053 (34,6) | 13,1 % | 17,9 % | −3,4 % | −0,26 | −22 % | 0,00 | 0,52 | 0,13 | −2,26 | −0,72 | ❌ |
| `research_15m_long` | 15 min | 4 h-24 h | 2023-07 → 2026-08 | 0,063 (14,0) | 33,3 % | 20,5 % | **+11,1 %** | 0,71 | −20 % | 0,58 | 0,08 | 0,82 | −0,06 | 0,60 | ❌ |
| `research_30m_long` | 30 min | 4 h-24 h | 2023-07 → 2026-08 | 0,063 (14,0) | 35,7 % | 21,6 % | **+13,2 %** | 0,80 | −20 % | 0,66 | **0,04** | 0,70 | 0,10 | 0,61 | ❌ |
| `research_15m` + lissage 2 horizons, sans amortissement (E2) | 15 min | 30 min-2 h | 2023-07 → 2026-08 | 0,053 | −1,0 % | 1,1 % | −2,1 % | −0,23 | −14 % | 0,01 | 0,72 | 0,87 | −0,49 | 0,02 | ❌ |
| `research_15m_intrabar` (agrégats 1 min) | 15 min | 30 min-2 h | 2023-07 → 2026-08 | 0,054 (35,7) | 3,8 % | 12,7 % | −8,9 % | −1,71 | −25 % (arrêt) | 0,00 | 1,00 | 0,60 | −1,07 | −2,22 | ❌ |
| `research_1m_long` | 1 min | 1 h-8 h | 2025-07 → 2026-08 | 0,050 (9,1) | 5,3 % | 15,8 % | −9,1 % | −0,79 | −19 % | 0,01 | 0,72 | 0,79 | −1,66 | −0,84 | ❌ |
| `research_30m_xl` | 30 min | 8 h-48 h (détention 24 h, aversion 2) | 2023-07 → 2026-08 | 0,074 (9,3) | 22,7 % | 8,5 % | **+11,9 %** | 0,77 | −20 % | 0,58 | **0,04** | 0,48 | **0,45** | **0,71** | ❌ |
| `research_15m_xl` | 15 min | 8 h-48 h (détention 24 h, aversion 2) | 2023-07 → 2026-08 | 0,074 (9,4) | 23,5 % | 9,2 % | **+11,0 %** | 0,74 | −20 % | 0,53 | **0,04** | 0,60 | **0,33** | **0,65** | ❌ |
| `research_30m_xl_lb` (fenêtres 14-30 j) | 30 min | 8 h-48 h (détention 24 h) | 2023-07 → 2026-08 | 0,072 (9,0) | 24,8 % | ~7 % | **+15,1 %** | **0,93** | −18 % | 0,55 | **0,04** | 0,86 | **0,62** | **0,89** | ❌ |

IC : Spearman transversal à l'horizon de détention, t de Newey-West sur les IC journaliers. En gras : ce
qui franchit son seuil.

## 1. Ce qui marche : la prédiction

Le modèle classe correctement les rendements *relatifs* des contrats, de façon stable : IC de 0,05 à 0,07
selon l'horizon, positif **chaque année** depuis 2023 et dans les 29 plis de deux semaines du 1 min. C'est
élevé pour de la finance (les facteurs actions publiés tournent autour de 0,02-0,05). **L'IC croît avec
l'horizon** : 0,056 à 4 h, 0,063 à 8 h, 0,073 à 24 h (15 et 30 min). Variables dominantes : prime du
perpétuel (`cs_premium`), retournements résiduels de 30 min à 4 h (`iret_*`), heure de la journée, niveau
de volatilité.

## 2. Pourquoi les horizons courts perdent

À 30 min-2 h, l'écart de rendement attendu entre contrats (IC × volatilité résiduelle) est de quelques
points de base, du même ordre que le coût d'un aller-retour (~10 pb). Mesuré à 30 min : le livre tournait
~273 fois son capital par an, chaque unité échangée rapportait ~2 pb bruts et coûtait ~5 pb. Les deux
configurations courtes atteignent le drawdown dur en 2023 et s'arrêtent. Lisser le score (E1) divise les
pertes par cinq mais ne suffit pas.

## 3. Horizons longs : rentables sur la période, pas encore robustes

Gain attendu par position ∝ √horizon, transactions par unité de temps ∝ 1/horizon : à 4-24 h le P&L brut
(33-36 %/an) dépasse nettement les coûts (21-22 %/an). Détail par année du 30 min (15 min comparable) :

| Année | Rendement net | P&L brut | Rotation (× capital) | pb bruts / unité échangée | pb de coûts / unité | IC estimé |
|---|---:|---:|---:|---:|---:|---:|
| 2023 (5 mois) | −6,4 % | −6,8 % | 20 | — | ~5 | 0,013 |
| 2024 | **+76,5 %** | +76,5 % | 391 | 19,6 | 5,2 | 0,033 |
| 2025 | −0,5 % | +36,5 % | 664 | 5,5 | 5,1 | 0,053 |
| 2026 (8 mois) | −10,6 % | +4,2 % | 218 | 1,9 | 5,2 | 0,035 |

Lecture : en 2025 l'IC est au plus haut mais la rotation double et les coûts absorbent tout le brut ; en
2026 l'avantage par transaction tombe sous le coût. Fin 2023, l'IC estimé (causal, prudent) reste faible :
le livre trade à peine et garde des positions sans soutien. La porte rejette : années positives 25-50 %,
DSR 0,58-0,66, PBO 0,70-0,82 (la meilleure variante de la grille ne le reste pas hors échantillon), et le
15 min ne tient pas les coûts doublés.

Diagnostic « sans arrêt » (contrôles de drawdown désactivés, hors porte) du 30 min long : Sharpe 0,89,
CAGR +17 %, **2025 à +14 %** au lieu de −0,5 % — la réduction de risque après le drawdown de début 2025 a
coûté l'année ; 2026 reste négatif (−13 %). Le run a été exécuté deux fois : résultats identiques au
chiffre près (pipeline déterministe).

Détention 24 h (`research_30m_xl`) : la rotation tombe de 418 à 164 fois le capital par an, les coûts de
21,6 % à 8,5 %/an ; le livre **tient désormais des coûts doublés** (Sharpe 0,45) et la PBO passe de 0,70 à
0,48. L'IC continue de croître avec l'horizon (0,083 à 48 h). Mais le résultat dépend d'un trimestre
(T4 2024 : +34 %), 2026 est négatif (−7,6 %) alors que l'IC y reste positif (0,042), le livre paie du
funding en 2026 (biais net acheteur d'un livre bêta-neutre), et l'intervalle bootstrap du Sharpe
([−0,26 ; 1,92], PSR 0,93) ne permet pas de conclure. Refus justifié.

Les agrégats 1 min (variance réalisée, sauts, asymétrie, flux de fin de bougie) n'apportent rien de
mesurable à 15 min (IC 0,0535 contre 0,0532).

Variables à fenêtres longues (`research_30m_xl_lb`) : **Sharpe net 0,93** (seuil franchi), CAGR +15 %,
drawdown −18 %, coûts ×2 : 0,62, une bougie de latence : 0,89, PSR 0,96, intervalle bootstrap
[−0,08 ; 2,15]. Refusé quand même : années positives 2/4 (2023 −5 %, 2024 +69 %, 2025 +9 %, 2026 −11 %),
DSR 0,55 avec 13 essais effectifs, PBO 0,86. Les variables dominantes deviennent des caractéristiques lentes
— illiquidité, volatilité, taille, bêta — : une partie du P&L vient de **paris de style** (petites
capitalisations peu liquides, faible volatilité) dont le rendement dépend du régime. La décomposition par
jambe le confirme indirectement : chaque jambe suit surtout le marché (2024 haussier : achats +61 %, ventes
−8 % ; 2025 : l'inverse) et le livre garde un biais net acheteur de 0,1-0,2 qui paie le funding en 2026.
L'hypothèse « ventes à découvert squeezées » est réfutée pour 2026 (la jambe vendeuse y gagne).

## 4. En cours

- `research_30m_xl_lb_style` : même modèle, livre **neutre aux styles** (taille/liquidité et volatilité
  traitées comme un risque aussi cher que le marché) — pour ne garder que la prédiction idiosyncratique ;
- `research_30m_xl_lb` réévalué avec les **stops catastrophe désormais simulés** (comme placés sur OKX en
  live), pour comparer à code égal.

Ce document est mis à jour avec chaque résultat, favorable ou non.
