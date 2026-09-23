# Résultats mesurés

Tout ce qui suit est **hors échantillon** (walk-forward purgé : chaque prédiction vient d'un modèle
entraîné uniquement sur le passé) et **net de frais, spread, impact et funding** (OKX, 2 pb maker /
5 pb taker, 60 % d'exécution passive supposée). Données : archives Binance USDT-M, contrats délistés
compris, univers point-in-time des ~30 contrats les plus liquides, juin 2022 → août 2026 (le mois en cours
est retiré tant que son funding n'est pas publié). Rapports complets dans `reports/`, registre des essais
dans `reports/trials/` (chaque configuration testée dégonfle le Sharpe via le DSR).

> **Aucune configuration n'a franchi la porte de promotion à ce jour.** Le système refuse donc de trader
> de l'argent réel. Ce document le dit sans détour et explique pourquoi.

## 1. Ce qui marche : la prédiction

| Unité | Horizon | IC de Spearman hors échantillon | t (Newey-West, IC journaliers) | IC par année |
|---|---|---:|---:|---|
| 15 min | 30 min / 1 h / 2 h | 0,055 / 0,053 / 0,052 | 43 / 35 / 28 | 2023 : 0,033 · 2024 : 0,040 · 2025 : 0,034 · 2026 : 0,034 |
| 30 min | 1 h / 2 h / 4 h | 0,054 / 0,054 / 0,054 | 31 / 26 / 20 | 2023 : 0,025 · 2024 : 0,041 · 2025 : 0,035 · 2026 : 0,034 |
| 1 min | 15 min | 0,024 → 0,064 selon les 29 plis de deux semaines (août 2025 → août 2026) | — | positif dans chacun des 29 plis |

Le modèle classe correctement les rendements *relatifs* des contrats, de façon stable sur plus de trois ans
et dans chaque année. C'est un IC élevé pour de la finance (les facteurs actions publiés tournent autour de
0,02-0,05). Les variables dominantes sont la prime du perpétuel (`cs_premium`), les retournements
résiduels à 30 min-4 h (`iret_*`), l'heure de la journée et le niveau de volatilité.

## 2. Ce qui ne marche pas encore : le transformer en argent après coûts

| Config | Bougie | P&L brut/an | Coûts/an | CAGR net | Sharpe net | Drawdown | Verdict |
|---|---|---:|---:|---:|---:|---:|:-:|
| `research_15m` | 15 min | +3,5 % | 12,4 % | −8,9 % | −1,75 | −25 % (arrêt) | ❌ |
| `research_30m` | 30 min | +5,3 % | 14,3 % | −8,8 % | −2,14 | −25 % (arrêt) | ❌ |

Diagnostic chiffré (30 min) : le livre tourne environ **273 fois son capital par an** pour une exposition
brute moyenne faible ; chaque unité échangée rapporte ~2 pb bruts et coûte ~5,3 pb (3,2 pb de frais, 1 pb
de demi-spread, 1 pb d'impact). Les deux versions atteignent le drawdown dur (−25 %) en 2023 et s'arrêtent,
comme elles le feraient en réel. La porte les rejette sur tous les critères sauf la durée d'historique.

Pourquoi : à 30 min-2 h, l'écart de rendement attendu entre contrats (IC × volatilité résiduelle) est de
l'ordre de quelques points de base, du même ordre que le coût d'un aller-retour. Deux défauts de
construction aggravaient la rotation : le score brut du modèle change à chaque bougie (bruit), et
l'amortissement des coûts par la persistance du signal (jusqu'à ÷5) rendait les transactions trop bon
marché aux yeux de l'optimiseur.

## 3. Ce qui est en cours

L'IC ne décroît pas de 1 h à 4 h. Le gain attendu par position croît comme √horizon alors que le nombre de
transactions par unité de temps décroît comme 1/horizon : un horizon long est la voie naturelle vers un
gain par transaction supérieur au coût. Variantes pré-enregistrées (chacune comptée comme un essai) :

- `research_15m_long` et `research_30m_long` : mêmes bougies (décision toutes les 15 / 30 min), cibles à
  4 h / 8 h / 24 h, détention visée 8 h, score lissé (demi-vie d'un demi-horizon), amortissement des coûts
  plafonné (plancher 0,5) ;
- sur le walk-forward 15 min existant : lissage du score (1 et 2 horizons) et plancher d'amortissement
  (0,5 et 1).

Le 1 min a produit son walk-forward (IC positif dans les 29 plis) mais son évaluation a été interrompue
par manque de mémoire sur le runner ; l'évaluation a depuis été allégée (IC calculé en format large,
processus dimensionnés sur la taille réelle). À coûts égaux, un horizon de 15 min est encore plus
défavorable que 1 h : le 1 min n'a de chance qu'avec un horizon long lui aussi.

Ce document sera mis à jour avec chaque résultat, favorable ou non.
