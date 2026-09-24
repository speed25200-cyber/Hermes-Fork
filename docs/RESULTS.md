# Résultats mesurés

Tout ce qui suit est **hors échantillon** (walk-forward purgé : chaque prédiction vient d'un modèle
entraîné uniquement sur le passé) et **net de frais, spread, impact et funding** (OKX, 2 pb maker /
5 pb taker, 60 % d'exécution passive supposée). Données : archives Binance USDT-M, contrats délistés
compris, univers point-in-time des ~30 contrats les plus liquides (15 pour le 1 min), juin 2022 → août
2026 (le mois en cours est retiré tant que son funding n'est pas publié). Rapports complets dans
`reports/`, registre des essais dans `reports/trials/` : chaque configuration testée dégonfle le Sharpe
(DSR). Tableau régénérable par `hermes research compare reports/<dossiers>`.

> **Aucune configuration n'a franchi la porte de promotion à ce jour ; le système refuse donc de trader de
> l'argent réel.** Les premières versions rentables (horizons 4-48 h, +11 à +15 %/an) dépendaient d'un
> régime (2024) et, pour plus de la moitié, de paris de style (petites capitalisations peu volatiles).
> Entraîné sur une **cible nette des styles** avec un livre neutre (`research_30m_xl_lb_sres`), le modèle
> gagne +10 %/an avec un drawdown de −13 %, **3 années sur 4 positives**, du funding encaissé, et reste
> positif à coûts doublés. Un audit adversarial a ensuite corrigé trois biais (univers pris sur Binance alors
> qu'on exécute sur OKX, stops trop serrés, exécution au dernier prix). **Exécutable sur OKX, le meilleur
> candidat fait Sharpe 1,10, +12,6 %/an, drawdown −11 %** (intervalle à 90 % du Sharpe [0,20 ; 2,07]), reste
> positif à coûts doublés (0,64) et avec chaque stop exécuté au pire (0,39) : **7 critères sur 9**. Refusé :
> Sharpe dégonflé 0,46 (33 essais effectifs, 3 ans d'historique) et PBO 0,39 ; 2026 est négatif (−6,8 %).

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
| `research_30m_xl_lb` (fenêtres 14-30 j), avant stops simulés | 30 min | 8 h-48 h (détention 24 h) | 2023-07 → 2026-08 | 0,072 (9,0) | 24,8 % | 7,8 % | **+15,1 %** | **0,93** | −18 % | 0,55 | **0,04** | 0,86 | **0,62** | **0,89** | ❌ |
| `research_30m_xl_lb`, stops catastrophe simulés | 30 min | 8 h-48 h (détention 24 h) | 2023-07 → 2026-08 | 0,072 (9,0) | 24,8 % | 8,4 % | **+14,6 %** | **0,91** | −18 % | 0,49 | **0,04** | 0,65 | **0,59** | **0,88** | ❌ |
| `research_30m_xl_lb_style` (livre neutre aux styles) | 30 min | 8 h-48 h (détention 24 h) | 2023-07 → 2026-08 | 0,072 (9,0) | 10,7 % | 8,2 % | +3,2 % | 0,34 | −21 % | 0,11 | 0,08 | **0,11** | −0,35 | **0,31** | ❌ |
| `research_30m_xl_lb_sres` (cible nette des styles, livre neutre) | 30 min | 8 h-48 h (détention 24 h) | 2023-07 → 2026-08 | 0,045* (8,7) | 10,6 % | 5,0 % | **+10,4 %** | **0,84** | **−13 %** | 0,34 | **0,04** | 0,45 | **0,20** | **0,56** | ❌ |
| `research_30m_xl_lb_sres_u50` (idem, 50 contrats) | 30 min | 8 h-48 h (détention 24 h) | 2023-07 → 2026-08 | 0,052* (11,3) | 17,2 % | 9,5 % | **+11,2 %** | **0,83** | **−14 %** | 0,22 | **0,04** | **0,05** | −0,08 | **0,82** | ❌ |
| `research_30m_xl_lb_sres_u50_h16` (idem, détention 8 h) — **biaisé, voir § 7** | 30 min | 8 h-48 h (détention 8 h) | 2023-07 → 2026-08 | 0,049* (16,3) | 25,6 % | 7,5 % | (+22,9 %) | (1,50) | (−10 %) | 0,73 | 0,04 | 0,05 | 0,85 | 1,41 | ❌ |
| **`research_30m_xl_lb_sres`, corrigé** (univers OKX, VWAP, stops 8 σ) | 30 min | 8 h-48 h (détention 24 h) | 2023-07 → 2026-08 | 0,045* (8,3) | 15,6 % | 6,0 % | **+12,6 %** | **1,10** | **−11 %** | 0,46 | **0,04** | 0,38 | **0,64** | **1,08** | ❌ |
| `research_30m_xl_lb_sres_u50_h16`, corrigé | 30 min | 8 h-48 h (détention 8 h) | 2023-07 → 2026-08 | 0,051* (16,5) | 18,8 % | 8,5 % | **+12,7 %** | **1,09** | −16 % | 0,44 | 0,08 | **0,08** | **0,55** | **1,02** | ❌ |
| `research_30m_xl_lb_sres_gate` (garde de régime : taille ×0,5 si BTC < −15 % de son plus haut 90 j) | 30 min | 8 h-48 h (détention 24 h) | 2023-07 → 2026-08 | 0,045* (8,3) | 13,7 % | 5,0 % | **+10,6 %** | **1,00** | **−10,5 %** | 0,38 | **0,04** | **0,26** | **0,50** | **0,98** | ❌ |
| `research_30m_xl_lb_sres_v2` (funding 8 h, 300 arbres, poids égaux) | 30 min | 8 h-48 h (détention 24 h) | 2023-07 → 2026-08 | 0,047* (9,1) | 12,0 % | 6,3 % | +8,4 % | **0,82** | **−12 %** | 0,25 | **0,04** | **0,10** | −0,01 | **0,75** | ❌ |
| `research_30m_xl_lb_sres_pos` (positionnement des gros comptes, § 11) | 30 min | 8 h-48 h (détention 24 h) | 2023-07 → 2026-08 | 0,048* (9,2) | 13,0 % | 6,4 % | +8,3 % | 0,79 | −15 % | 0,25 | **0,04** | **0,04** | **0,16** | **0,64** | ❌ |

IC : Spearman transversal à l'horizon de détention, t de Newey-West sur les IC journaliers. En gras : ce
qui franchit son seuil (drawdown en gras : meilleur que −15 %). * IC mesuré contre la cible nette des
styles : plus difficile à prédire, il ne se compare pas aux IC contre la cible bêta-résiduelle.

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

Stops catastrophe simulés (`research_30m_xl_lb`, même code que le réel : stop à k σ journaliers posé à
l'ouverture, exécuté au stop ou à l'ouverture en cas de gap, frais taker) : Sharpe 0,91 au lieu de 0,93, CAGR
+14,6 %, coûts +0,6 %/an ; PBO 0,65 au lieu de 0,86 (la grille se classe plus régulièrement). Par année :
2023 −4,5 %, 2024 +65 %, 2025 +9,5 %, 2026 −11,9 %. Toujours refusé (DSR 0,49 avec 16 essais, années
positives 2/4).

## 4. Les paris de style expliquent l'essentiel du P&L

`research_30m_xl_lb_style` garde le même modèle mais rend le livre **neutre aux styles** (taille/liquidité
et volatilité traitées comme un risque aussi cher que le marché). Le P&L brut tombe de 24,8 % à 10,7 %/an
pour des coûts inchangés (8,2 %/an) : Sharpe 0,34, CAGR +3,2 %, coûts doublés négatifs. Par différence, la
part « style » du livre non neutre a rapporté environ −6 % en 2023, +40 % en 2024, +23 % en 2025 et −13 % en
2026 ; la part idiosyncratique +1,5 %, +25 %, −14 % et +1 %. Les deux dépendent du régime ; aucune ne
passe seule la porte. Le style n'est pas illégitime (taille et momentum sont des facteurs documentés des
cryptomonnaies, Liu, Tsyvinski & Wu 2022), mais c'est une prime de risque cyclique, pas une prédiction.

Point notable : en 2025, l'IC est au plus haut (0,073) alors que le livre neutre perd −13,7 % (rotation 286,
coûts 14 %). L'IC mesure surtout un classement lent, dominé par les caractéristiques de style ; une fois
celles-ci retirées du livre, ce qui reste à trader est faible et coûteux.

## 5. Cible nette des styles : le P&L devient régulier

`research_30m_xl_lb_sres` entraîne le modèle sur le rendement résiduel **projeté hors de la taille/liquidité
et de la volatilité** à chaque barre (après écrêtage des sauts : voir la revue ci-dessous), et le livre est
neutre aux mêmes styles. Le modèle apprend donc exactement ce que le livre a le droit de détenir.

| Année | `xl_lb` (paris de style) | `xl_lb_style` (livre neutre) | `xl_lb_sres` (cible et livre neutres) | `xl_lb_sres_u50` (50 contrats) |
|---|---:|---:|---:|---:|
| 2023 (5 mois) | −4,5 % | +1,5 % | +4,1 % | +2,0 % |
| 2024 | +65,3 % | +24,8 % | +14,1 % | +15,7 % |
| 2025 | +9,5 % | −13,7 % | +16,9 % | +20,9 % |
| 2026 (8 mois) | −11,9 % | +0,7 % | −2,2 % | −2,7 % |
| Sharpe / drawdown | 0,91 / −18 % | 0,34 / −21 % | 0,84 / −13 % | 0,83 / −14 % |

Lecture :
- à P&L brut égal (10,6 %/an contre 10,7 %), la cible nette des styles **divise la rotation par 1,7** (96
  contre 160) : le modèle ne perd plus de transactions à suivre des primes que le livre neutralise, et les
  coûts tombent à 5 %/an ; le livre encaisse davantage de funding (+4,9 %/an contre +1,1 %) ;
- le gain est réparti (3 années positives, 2026 à peine négatif) et le livre reste positif à coûts doublés
  (Sharpe 0,20) et avec une barre de latence (0,56) ; sans les contrôles de drawdown, Sharpe 1,03 et
  +14 %/an (2025 : +28 %) ;
- sur 50 contrats, l'IC et son t montent (0,052, t 11,3 : loi fondamentale), le P&L brut aussi (17 %/an),
  mais la rotation et les coûts (9,5 %/an) montent davantage : coûts doublés négatifs. En revanche la grille
  de construction y est très stable (PBO 0,05) et désigne une détention de 8 h (Sharpe 1,50 à aversion 2) ;
- 2026 reste le point faible : IC réalisé nul (−0,005 sur 30 contrats, +0,005 sur 50) ;
- refus : DSR 0,34 et 0,22 (29 et 32 essais effectifs, intervalle bootstrap du Sharpe [−0,04 ; 1,80]) ; PBO
  0,45 sur 30 contrats.

Revue adversariale de cette cible (avant les runs retenus ici) : la projection était d'abord ajustée sur des
résidus non écrêtés — un seul saut (XRP le jour du jugement SEC, +45 σ) faisait des cibles de tous les autres
un pari de volatilité. Corrigé (écrêtage avant projection, test), et les runs lancés avec l'erreur ont été
annulés avant d'entrer au registre.

## 6. Détention 8 h sur 50 contrats : 7 critères sur 8… avant audit

`research_30m_xl_lb_sres_u50_h16` (détention 8 h choisie a posteriori dans la grille de `sres_u50`, PBO
0,05, compté au registre) affichait Sharpe 1,50, +22,9 %/an, drawdown −9,8 %, Sharpe 0,85 à coûts doublés,
1,41 avec une bougie de latence, intervalle bootstrap [0,75 ; 2,65], SPA p = 0,007 ; seul le DSR (0,73 avec
37 essais effectifs) manquait. Ce chiffre est **biaisé à la hausse** ; il n'est pas retenu.

## 7. Audit adversarial du meilleur candidat

Trois auditeurs indépendants ont cherché ce qui rend le résultat trop beau ; chaque constat a été soumis à un
vérificateur chargé de le réfuter (reproduction exacte du run à partir du walk-forward).

1. **Univers non exécutable (confirmé, majeur).** La recherche classait tout Binance ; le moteur réel ne
   trade que ce qu'OKX liste. Reconstitué jour par jour depuis les archives d'OKX, 14 % des jours-membres
   portaient sur des contrats qu'OKX ne listait pas à la date : 10 % de l'exposition, **un tiers du P&L
   de prix**, surtout des ventes à découvert de petites capitalisations « à la mode » propres à Binance.
   Restreint à OKX à la date, le même livre fait **Sharpe 1,12, +15,6 %/an, drawdown −12,5 %** (DSR ≈ 0,40).
   Corrigé : univers pris parmi les contrats OKX listés la veille (`data.universe.venue: okx`), catalogue
   OKX limité aux cryptos (BB et ON y sont BlackBerry et ON Semiconductor).
2. **Stops trop serrés (confirmé).** À 4 volatilités quotidiennes, 683 stops en 3 ans (un jour sur trois) ;
   exécutés au plus bas de la bougie, le Sharpe tombe à −0,04. Mais **sans aucun stop le livre fait mieux**
   (Sharpe 1,60, drawdown −9,7 % ; 0,93 contre 0,84 sur 30 contrats) : le P&L ne vient pas des stops.
   Corrigé : stops catastrophe à 8 volatilités (protection d'un moteur arrêté), et la porte exige désormais
   un Sharpe positif avec chaque stop exécuté au pire.
3. **Exécution au prix de clôture de la décision (confirmé, mineur).** Le 10 octobre 2025 (krach), le livre
   était exécuté au dernier prix de la bougie, alors que les prix rebondissaient dans les minutes suivantes :
   −0,5 à −1,7 point de CAGR sur la période. Corrigé : exécution au VWAP du premier quart d'heure suivant.
4. Rejetés après vérification : seuil de drawdown « au bord », bêtas sous-estimés, spread au plancher.

Le funding du backtest reste celui de Binance (OKX paie le sien) : écart de second ordre, non corrigé.

## 8. Candidats corrigés : ce qui est réellement exécutable

Mêmes modèles, avec les trois corrections (univers pris parmi les contrats qu'OKX listait la veille, exécution
au VWAP du quart d'heure suivant la décision, stops catastrophe à 8 volatilités) :

| | `sres` corrigé (30 contrats, 24 h) | `sres_u50_h16` corrigé (50 contrats, 8 h) |
|---|---:|---:|
| Sharpe net / intervalle à 90 % | **1,10** / [0,20 ; 2,07] | 1,09 / [0,14 ; 2,12] |
| CAGR / drawdown max | +12,6 % / −11,0 % | +12,7 % / −16,1 % |
| P&L brut / coûts / funding (par an) | 15,6 % / 6,0 % / +2,7 % | 18,8 % / 8,5 % / +2,3 % |
| 2023 (5 mois) / 2024 / 2025 / 2026 (8 mois) | +0,6 / +12,4 / +36,7 / −6,8 % | −2,5 / +20,2 / +31,5 / −6,2 % |
| Coûts ×2 / une bougie de latence / stops au pire | 0,64 / 1,08 / 0,39 | 0,55 / 1,02 / 0,33 |
| Sans les 5 meilleurs jours | Sharpe 0,77, +7,7 %/an | Sharpe 0,78, +8,1 %/an |
| Stops déclenchés par an | 43 (683 en 3 ans auparavant) | 63 |
| p nul / PBO / DSR | **0,04** / 0,38 / 0,46 | 0,08 / **0,08** / 0,44 |
| Critères franchis | **7 sur 9** | 6 sur 9 |

Lecture :
- l'estimation de l'audit se confirme : un Sharpe autour de 1,1 et 12-13 %/an une fois le biais retiré ;
- sur 30 contrats, la correction **améliore** le résultat (Sharpe 0,84 → 1,10) : les petites capitalisations
  propres à Binance étaient coûteuses à 24 h de détention ; l'avantage des 50 contrats disparaît (sa grille
  reste la plus stable, PBO 0,08) ;
- le livre ne dépend plus des stops (sans stops : Sharpe 1,00) ni de la chance d'exécution (stops au pire :
  0,39) ; il reste positif à coûts doublés ;
- **faiblesses** : 2025 porte l'essentiel du gain ; 2026 est négatif et l'IC réalisé y est nul ou négatif
  (−0,022 sur 30 contrats) ; le Sharpe dégonflé (0,46) dit qu'avec ~33 essais effectifs, trois ans ne
  suffisent pas à exclure la chance ; la PBO (0,38) dépasse le seuil.

**Conclusion honnête** : `research_30m_xl_lb_sres` est le meilleur candidat exécutable, cohérent et robuste
aux stress, mais pas démontré. La prochaine preuve ne peut venir que de données jamais vues ; le réel reste
interdit par la porte.

## 9. Incubation en papier (depuis le 23 septembre 2026)

Sur décision de l'utilisateur, `research_30m_xl_lb_sres` tourne en **papier** sur le VPS depuis le 23 septembre
2026 à 20 h UTC : décisions réelles toutes les 30 minutes sur le flux Binance, univers limité aux swaps
crypto listés par OKX, exécution simulée, capital fictif de 10 000 USDT. Modèle `f2227b88c6bd`, entraîné sur
un runner GitHub jusqu'au 29 août 2026 (le VPS, 7 Go, exécute sans entraîner), non promu. Ce qui dira si
le résultat de recherche tient : l'IC réalisé (≈ 0,045 attendu), la courbe d'équité face au cône attendu
(+12,6 %/an, volatilité ≈ 11 %), l'écart d'exécution face aux coûts modélisés — tous suivis par le tableau
de bord. Il faut plusieurs mois pour conclure : à Sharpe 1, un an de papier ne sépare encore un vrai
avantage du hasard qu'avec environ 84 % de confiance.

**Premier constat (23 septembre, 20 h 30 UTC) : le livre est à plat, et c'est voulu.** La taille des positions
est proportionnelle à l'IC estimé causalement ; or l'IC réalisé des derniers plis est nul ou négatif (plis de
mai et juillet 2026 : −0,054 et −0,006) et la borne prudente de validation vaut 0. Le backtest de recherche
finissait lui-même sans position fin août 2026. Le moteur décide donc toutes les 30 minutes, mesure l'IC réalisé
de ses scores une fois l'horizon de 24 h écoulé, et ne prendra des positions que si cet IC redevient positif.
L'incubation mesure dès maintenant la question qui compte : **le classement du modèle tient-il sur des
données jamais vues ?** Un IC réalisé durablement nul signerait la fin de cet avantage.

## 10. Pourquoi 2026 perd, et ce que l'incubation doit trancher

Diagnostic des scores walk-forward (jumeau à 30 contrats, cible nette des styles, erreurs types Newey-West) :

| Période | IC de rang, score brut | IC de rang, score lissé | IC de Pearson du livre (ce qui dimensionne) |
|---|---:|---:|---:|
| 2023 S2 – 2024 | 0,039 ± 0,009 | 0,036 | 0,020 |
| 2025 | 0,058 ± 0,010 | 0,042 | 0,035 |
| 2026, janvier → 15 mai | **0,049 ± 0,011** | **0,009** | −0,007 |
| 2026, 16 mai → août | 0,023 ± 0,010 | 0,016 | −0,004 |

**Le classement brut a presque tenu** (2026 : 0,036 ± 0,008 contre 0,047 ± 0,007 avant, un écart d'environ une
erreur type) ; **ce qui a cassé, c'est la partie lente du score**, celle que le livre trade (score lissé sur
24 barres, détenu 24 h). Un pli de 60 jours a une erreur type de 0,02-0,03 : un chiffre isolé comme −0,054 ne
se sur-interprète pas. Explications, de la plus à la moins probable :

- **E1 — janvier à mi-mai : la partie lente a cassé, pas le classement.** L'IC du score vieux de 48 barres
  passe de +0,029 à −0,010 ; le momentum 14-30 jours et les sommes de funding sur 30 jours (≈ 50 % du gain du
  GBM) changent de signe ; les variables rapides tiennent. Contre : la frontière de mi-mai a été choisie après
  avoir vu les données, et le retournement du momentum 30 jours n'est qu'à t ≈ −2.
- **E2 — mi-mai à août : le bloc rapide s'est retourné.** Le retour à la moyenne 8 h-3 j devient continuation
  (t −6,1), le flux aussi (t −4,0), dans les deux univers, en même temps que l'autocorrélation transversale
  des rendements journaliers devient positive. Contre : un seul épisode de 3,5 mois, sans indicateur avancé
  validé.
- **E3 — régime de baisse, cause commune possible de E1.** Avant 2026, l'IC était plus faible quand BTC
  clôturait à plus de 15 % sous son plus haut de 90 jours (4 épisodes sur 5) ou quand le marché alt baissait
  sur 30 jours (7 sur 8), même à IC récent égal ; 2026 est le plus long épisode de ce type. Contre : variable
  retenue parmi ~30 examinées sur les mêmes données, et 2026 ne peut pas la valider (80-96 % des jours dans
  cet état).
- **E4 — déclin structurel / encombrement.** A priori fort (carry crypto négatif en 2025, anomalies publiées
  qui perdent ~58 %), mais l'IC brut de 2026 n'est pas significativement plus bas : indiscernable de E2/E3 sur
  8 mois.

Écartés par mesure : **ré-entraîner plus souvent ou sur une fenêtre plus courte ne sauve pas 2026** (test
causal : fenêtre croissante 0,016, demi-vie 365 j 0,015, glissante 365 j 0,007, 180 j 0,002 ; tous t < 1,7) —
les configurations `sres_rec` et `sres_roll` ont donc été retirées sans être lancées. Interdits, car décidés sur
2026 : retirer ou inverser des variables selon leur IC de 2026, raccourcir l'horizon ou le lissage (le livre
à 8 h perd aussi −6,2 % en 2026), filtrer sur la dispersion ou la corrélation (signe historique faux en 2026).

**Contrôles pré-enregistrés de l'incubation** (fixés le 23 septembre 2026, avant toute donnée en direct ;
journalisés par le moteur, jamais utilisés pour dimensionner, affichés par le tableau de bord) : IC de rang du
score brut (`ic_raw`), du score vieux d'un horizon (`ic_lag`), et l'état du marché (`btc_dd90`, `mkt_ret30`,
`xs_ac1`). Chaque explication prédit un profil différent :

| Si… | alors… |
|---|---|
| l'IC remonte maintenant que BTC est sorti de sa baisse (depuis le 21 août 2026) | E3 : le creux de 2026 était un régime |
| l'IC brut reste positif mais `ic_lag` reste ≤ 0 | E1 : seule la partie lente est perdue |
| tout reste près de zéro | E4 : l'avantage est perdu |

**Variantes de recherche retenues (une par essai, dans l'ordre)** : V1, une garde de régime (taille divisée
par deux quand BTC a clôturé la veille à plus de 15 % sous son plus haut de 90 jours ; paramètres figés, jamais
réajustés ; `research_30m_xl_lb_sres_gate`) — une estimation linéaire sur le livre officiel annonçait Sharpe
1,14 → 1,31 et 2026 −6,8 % → −3,5 %, à lire comme une borne haute puisque la variable a été choisie après
coup ; V2, de l'hygiène d'ajustement (ensemble à poids égaux, nombre d'arbres fixé d'avance, correction des
contrats à funding toutes les 4 h) ; V3, un membre « momentum de facteurs » causal, a priori faible.

## 11. Une information nouvelle : le positionnement des gros comptes

Les variantes V1-V3 réutilisent la même information. Test local d'une source jamais utilisée : les archives
Binance « metrics » (intérêt ouvert, ratios long/short des gros comptes et de tous les comptes, instantanés de
5 minutes) sur les jours de présence dans l'univers à 30 contrats (61 473 fichiers). **Règle fixée avant tout
résultat** : IC de rang *partiel* contre la cible nette des styles à 24 h, c'est-à-dire en plus du score
walk-forward du modèle ; sélection sur 2023-08 → 2025-12 seulement (|t| ≥ 3 et même signe en 2024 et en 2025) ;
2026 gardé comme contrôle (même signe et au moins la moitié de l'effet) ; un essai n'est dépensé que si une
variable franchit les deux.

| Variable (17 testées) | IC partiel 2023-25 (t) | 2024 | 2025 | 2026 (t) | Sélection | Contrôle 2026 |
|---|---:|---:|---:|---:|:-:|:-:|
| Ratio des positions des gros comptes, écart à 30 j | **−0,021 (−3,5)** | −0,029 | −0,017 | **−0,023 (−2,3)** | ✅ | ✅ |
| Même ratio, variation sur 1 j | −0,017 (−3,6) | −0,006 | −0,031 | +0,001 (0,1) | ✅ | ❌ |
| Intérêt ouvert, variation 1 j / 3 j | −0,011 (−2,4) / −0,011 (−1,9) | −0,014 / −0,013 | −0,008 / −0,004 | +0,017 / +0,024 | ❌ | — |
| Rotation (volume / intérêt ouvert), écart à 30 j | +0,011 (1,9) | +0,017 | +0,011 | +0,019 | ❌ | — |
| Ratio de tous les comptes, écart à 30 j | −0,004 (−0,8) | −0,002 | −0,005 | +0,007 | ❌ | — |

Lecture : quand les gros comptes d'un contrat sont inhabituellement acheteurs, il fait moins bien ensuite (hors
bêta et styles), **et cela a tenu en 2026**, là où les variations d'intérêt ouvert se sont retournées comme la
partie lente du modèle. Avec 17 variables testées, un t de −3,5 garde une p corrigée (Bonferroni) d'environ 0,01.
Les deux variables sélectionnées sur 2023-2025 entrent dans le modèle (lues une bougie en retard, z-score sur
28 jours pour être reproductibles avec les 30 jours que sert l'API en direct), ainsi que le rang transversal
de l'écart (la forme exacte testée ci-dessus) : essai `research_30m_xl_lb_sres_pos`.

## 12. Essais du 23 septembre : ni V1, ni V2, ni le positionnement ne font mieux

| | `sres` corrigé (papier) | V1 : garde de régime | V2 : funding 8 h, 300 arbres, poids égaux |
|---|---:|---:|---:|
| IC (t) | 0,045 (8,3) | 0,045 (8,3) | 0,047 (9,1) |
| Sharpe net / CAGR | **1,10** / **+12,6 %** | 1,00 / +10,6 % | 0,82 / +8,4 % |
| Drawdown max | −11,0 % | **−10,5 %** | −12,3 % |
| 2023 / 2024 / 2025 / 2026 | +0,6 / +12,4 / +36,7 / −6,8 % | −1,5 / +15,2 / +24,4 / **−3,2 %** | +1,3 / +9,9 / +25,9 / −8,3 % |
| DSR / PBO / coûts ×2 / stops au pire | 0,46 / 0,38 / 0,64 / 0,39 | 0,38 / **0,26** / 0,50 / 0,28 | 0,25 / **0,10** / −0,01 / — |
| Critères franchis | 7 sur 9 | 7 sur 9 (PBO oui, années positives non) | 6 sur 9 |

**V1** fait ce que le diagnostic annonçait sur 2026 (perte divisée par deux, estimation préalable −3,5 %) et
rend le choix de réglage plus stable, mais coûte davantage dans les années porteuses (2025 : +24 % au lieu de
+37 %) et fait passer 2023 sous zéro : le Sharpe baisse. La variable ayant été choisie après examen des
données, ce résultat est de plus une borne haute. **Pas d'amélioration nette : le modèle en papier reste
`sres`**, sans garde ; la garde reste disponible (`portfolio.regime_gate_*`).

L'hygiène d'ajustement relève à peine l'IC et rend le choix de réglage plus stable (PBO 0,10), mais le livre
gagne moins et 2026 reste négatif (IC réalisé −0,023) : l'hypothèse « du bruit dans l'ajustement explique
2026 » est écartée. Le passage de `to_funding` aux variables par contrat retire aussi cette variable du
modèle de direction du marché (un quatrième changement, sans effet sur la porte).

**Positionnement des gros comptes (`sres_pos`, 24 septembre) : pas mieux non plus.** La variable retenue au § 11
est bien utilisée par le modèle (7ᵉ au gain moyen du GBM) et l'IC à 24 h monte à peine (0,048, t 9,2, contre
0,045), mais le livre gagne moins : Sharpe 0,79 (intervalle à 90 % [−0,11 ; 1,86]), +8,3 %/an, drawdown −14,9 %,
années 2023 / 2024 / 2025 / 2026 à −1,2 / +7,9 / +26,3 / −5,1 %. 2026 perd un peu moins (−5,1 % contre −6,8 %),
l'IC réalisé de 2026 reste négatif (−0,017) ; le choix de réglage est très stable (PBO 0,04) mais les stress sont
plus fragiles (coûts ×2 : 0,16 ; stops au pire : 0,04). **6 critères sur 9 : le modèle en papier reste `sres`.**
Les écarts entre `sres`, V1, V2 et `sres_pos` (Sharpe 0,79 à 1,10) sont du même ordre que l'incertitude d'un seul
backtest : aucune de ces trois variantes ne se distingue du modèle de référence.

## 13. Pré-enregistrement : l'activité au comptant face aux perpétuels (24 septembre 2026, 00 h 45 UTC)

Deuxième source d'information jamais utilisée : les bougies **au comptant** de Binance (archives publiques,
30 minutes), comparées à celles du perpétuel. Idée : un contrat dont l'activité se fait surtout à effet de levier
(perpétuel) plutôt qu'au comptant, ou dont les acheteurs agressifs sont sur le perpétuel plutôt qu'au comptant,
porte un excès de spéculation qui se retourne. **Règles fixées avant tout résultat, identiques au § 11** : IC de
rang *partiel* contre la cible nette des styles à 24 h, en plus du score walk-forward du modèle (jumeau à 30
contrats) ; sélection sur 2023-08 → 2025-12 seulement (|t| de Newey-West ≥ 3 et même signe en 2024 et en 2025) ;
contrôle sur 2026 (même signe et au moins la moitié de l'effet) ; un essai n'est dépensé que si une variable
franchit les deux. Variables calculées seulement où le comptant existe (couverture publiée), lues **une bougie en
retard**, huit en tout :

| Variable | Définition |
|---|---|
| `perp_share_7d` | log(volume perpétuel / volume comptant), sommes sur 7 jours |
| `perp_share_z` | même rapport sur 1 jour, z-score sur 30 jours |
| `perp_share_chg_7d` | variation sur 7 jours du rapport sur 1 jour |
| `spot_taker_1d` | part des achats agressifs au comptant sur 1 jour, moins ½ |
| `spot_taker_z` | même part, z-score sur 30 jours |
| `taker_gap_1d` | part des achats agressifs au comptant moins celle du perpétuel, 1 jour |
| `spot_vol_growth` | log(volume comptant moyen 7 jours / volume comptant moyen 30 jours) |
| `spot_perp_ret_gap_1d` | rendement 1 jour au comptant moins rendement 1 jour du perpétuel (a priori redondant avec la prime) |

Correspondance des noms : le même symbole au comptant s'il existe, sinon sans le préfixe « 1000 » / « 1000000 » /
« 1M » (1000PEPEUSDT → PEPEUSDT), LUNA2USDT → LUNAUSDT. Avec les 17 variables du § 11, 25 variables ont
désormais été criblées par cette règle : un t de 3 garde une p corrigée (Bonferroni) d'environ 0,07.

**Résultat (24 septembre, 01 h UTC) : aucune variable ne franchit la sélection, aucun essai n'est dépensé.**
Comptant trouvé et vérifié (prix à ±3 % du perpétuel) pour 213 des 276 contrats, soit 89 % des bougies de
l'univers ; alignement contrôlé (corrélation des rendements 30 min de 0,94-0,96 au même instant, ≈ 0 à une bougie
d'écart). IC de rang du score du modèle sur les mêmes bougies : 0,047 (sélection), 0,038 en 2026.

| Variable | IC partiel 2023-25 (t) | 2024 | 2025 | 2026 (t) | Sélection |
|---|---:|---:|---:|---:|:-:|
| `perp_share_7d` | −0,003 (−0,4) | −0,015 | +0,006 | +0,012 (1,2) | ❌ |
| `perp_share_z` | +0,005 (0,9) | +0,001 | +0,018 | +0,014 (1,0) | ❌ |
| `perp_share_chg_7d` | +0,008 (1,4) | +0,001 | +0,016 | +0,002 (0,1) | ❌ |
| `spot_taker_1d` | −0,005 (−0,8) | −0,005 | −0,003 | +0,001 (0,1) | ❌ |
| `spot_taker_z` | −0,006 (−1,0) | −0,005 | +0,001 | +0,019 (1,6) | ❌ |
| `taker_gap_1d` | −0,002 (−0,3) | +0,001 | +0,001 | +0,003 (0,2) | ❌ |
| `spot_vol_growth` | +0,013 (2,1) | +0,006 | +0,017 | −0,008 (−0,7) | ❌ |
| `spot_perp_ret_gap_1d` | −0,003 (−1,5) | −0,006 | −0,002 | +0,002 (0,3) | ❌ |

Lecture : au-delà de ce que le modèle sait déjà, la répartition de l'activité entre comptant et perpétuel ne
prédit rien de mesurable à 24 h. La seule piste proche du seuil (croissance du volume au comptant, t 2,1) change
de signe en 2026. L'hypothèse de l'excès de levier est écartée pour cet horizon.

## 14. Pré-enregistrement : la profondeur du carnet d'ordres (24 septembre 2026, 01 h UTC)

Troisième source jamais utilisée : les archives Binance « bookDepth » (instantanés toutes les ~30 s de la valeur
cumulée des ordres à ±1 %, ±2 %… ±5 % du prix, disponibles depuis janvier 2023). Idée : un carnet durablement
plus garni à l'achat qu'à la vente (ou l'inverse), ou une liquidité qui se retire, annonce la suite au-delà de ce
que le modèle voit déjà dans les prix et les volumes. **Mêmes règles qu'aux § 11 et § 13** (IC de rang partiel à
24 h en plus du score du modèle, sélection 2023-08 → 2025-12 à |t| ≥ 3 avec même signe en 2024 et 2025, contrôle
2026 à même signe et au moins la moitié de l'effet, un essai seulement si une variable franchit les deux). Pour
chaque bougie de 30 min : le dernier instantané avant la clôture ; variables lues une bougie en retard ; six en
tout (31 variables criblées au total, t de 3 ≈ p corrigée 0,08) :

| Variable | Définition |
|---|---|
| `depth_imb_1pct` | log(ordres d'achat à −1 % / ordres de vente à +1 %), moyenne sur 1 jour |
| `depth_imb_5pct` | même rapport à ±5 %, moyenne sur 1 jour |
| `depth_imb_1pct_z` | `depth_imb_1pct`, z-score sur 30 jours |
| `depth_imb_chg_1d` | `depth_imb_1pct` moins sa valeur de la veille |
| `depth_adv_2pct` | log(profondeur totale à ±2 %, moyenne 1 jour / volume échangé sur 1 jour) |
| `depth_chg_7d` | variation sur 7 jours du log de la profondeur totale à ±2 % (moyenne 1 jour) |

**Résultat (24 septembre, 03 h UTC) : aucune variable ne franchit la sélection, aucun essai n'est dépensé.**
55 000 archives quotidiennes téléchargées sans échec (270 contrats), profondeur présente sur 99,6 % des bougies
de l'univers ; même score de référence qu'au § 13 (IC 0,047 en sélection, 0,038 en 2026).

| Variable | IC partiel 2023-25 (t) | 2024 | 2025 | 2026 (t) | Sélection |
|---|---:|---:|---:|---:|:-:|
| `depth_imb_1pct` | −0,004 (−0,7) | −0,016 | +0,002 | +0,010 (0,8) | ❌ |
| `depth_imb_5pct` | −0,014 (−2,7) | −0,010 | −0,027 | −0,014 (−1,3) | ❌ (t < 3) |
| `depth_imb_1pct_z` | −0,005 (−0,9) | +0,001 | −0,018 | −0,004 (−0,4) | ❌ |
| `depth_imb_chg_1d` | −0,007 (−1,4) | +0,001 | −0,017 | −0,004 (−0,4) | ❌ |
| `depth_adv_2pct` | +0,011 (2,5) | +0,009 | +0,020 | +0,008 (1,1) | ❌ (t < 3) |
| `depth_chg_7d` | +0,007 (1,2) | +0,005 | +0,010 | +0,025 (3,5) | ❌ |

Lecture : le déséquilibre du carnet près du prix (±1 %) n'annonce rien à 24 h, ce qui est attendu (son effet
connu dure des secondes à des minutes). Deux variables gardent le même signe sur toutes les périodes sans
atteindre le seuil : un carnet plus garni à la vente qu'à l'achat à ±5 % précède une meilleure suite (t −2,7), un
carnet profond par rapport au volume aussi (t 2,5). Elles ne sont **pas** retenues : la règle a été fixée avant
le résultat. `depth_chg_7d` n'est significative qu'en 2026, la période de contrôle : l'utiliser serait choisir sur
2026. Au total, sur trois sources nouvelles (positionnement, comptant, carnet ; 31 variables), une seule variable a
franchi la règle, et l'essai qu'elle a déclenché n'a pas fait mieux (§ 12).

## 15. Pré-enregistrement : choisir le réglage du livre sur le passé seulement (24 septembre 2026, 01 h 20 UTC)

Dans les quatre derniers essais sur 30 contrats (`sres` corrigé, V1, V2, `sres_pos`), la grille de construction
place toujours en tête la détention 8 h avec une faible aversion aux coûts (Sharpe 1,55 à 1,70) ; le réglage
officiel, détention 24 h et aversion 2 (0,79 à 1,10), fixé avant tout résultat, est parmi les moins bons de ses
voisins. **Changer le réglage après avoir vu ce tableau serait du sur-ajustement** (le § 6 l'a montré). La réponse
propre est de laisser le système choisir lui-même, mois après mois, **avec le seul passé** : c'est ce que mesure la
PBO, mais sans en tirer un livre. Règle fixée ici, avant tout calcul :

- le premier jour de chaque mois, le réglage de la grille (3 horizons × 3 aversions, inchangée) dont le Sharpe
  quotidien est le meilleur sur les 365 jours **précédents** est tradé tout le mois ;
- tant que la grille a moins de 180 jours d'historique hors échantillon, le réglage configuré est tradé ;
- chaque changement de réglage paie 0,1 % du capital (reconstruction complète d'un livre brut de 0,5 à 10 pb) ;
- mesure : Sharpe, CAGR, drawdown et années de la série obtenue, **diagnostic à côté de la porte**, avec le même
  livre de référence (`research_30m_xl_lb_sres`, relancé tel quel pour enregistrer les rendements quotidiens de
  la grille). L'idée de ce test vient d'avoir vu la grille : il compte comme un essai de plus.

Décision fixée d'avance : si la sélection bat le réglage fixe sur le Sharpe **et** sur 2026, elle devient une
variante candidate (moteur à adapter, puis papier) ; sinon le réglage fixe reste, et l'écart de la grille est
attribué au hasard du choix.

**Résultat (24 septembre, 03 h UTC) : la sélection ne franchit pas la décision fixée d'avance ; le réglage fixe
reste.** Run `35942639721` : le livre de référence est reproduit à l'identique (Sharpe 1,096, mêmes années, 7 critères
sur 9, DSR 0,42 avec un essai de plus au registre), ce qui vérifie au passage la reproductibilité du pipeline.

| | Réglage fixe (24 h, aversion 2) | Sélection sur le passé | Meilleure ligne de la grille (a posteriori) |
|---|---:|---:|---:|
| Sharpe / CAGR | 1,10 / +12,6 % | **1,18** / +15,2 % | 1,62 / +21,7 % |
| Drawdown max | **−11,0 %** | −13,7 % | −12,9 % |
| 2023 / 2024 / 2025 / 2026 | +0,6 / +12,4 / +36,7 / **−6,8 %** | +0,6 / +9,2 / +56,1 / −9,7 % | — |

La sélection a passé 71 % des mois sur la détention 8 h, avec 8 changements de réglage. Elle gagne un peu en Sharpe,
entièrement grâce à 2025, et perd davantage en 2026 : **Sharpe mieux, 2026 moins bien, donc refusée** selon la
règle. L'essentiel de l'écart de la grille (1,62 contre 1,10) disparaît dès qu'on choisit sans connaître l'avenir :
c'est la mesure directe de ce que coûterait un choix fait après coup. Le code reste (diagnostic à chaque rapport,
rendements de la grille enregistrés dans `grid_daily.csv`).

Ce document est mis à jour avec chaque résultat, favorable ou non.
