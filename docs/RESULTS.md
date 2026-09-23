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
aux stress, mais pas démontré. La prochaine preuve ne peut venir que de données jamais vues : l'incubation en
papier sur OKX (voir `OPERATIONS.md`), sur décision de l'utilisateur ; le réel reste interdit par la porte.

Ce document est mis à jour avec chaque résultat, favorable ou non.
