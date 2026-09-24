# État de l'art : ce qui a guidé la conception

Synthèse d'une revue de littérature faite en septembre 2026 (articles 2013-2026). Les nombres cités sont
ceux des résumés publiés ; ils motivent des choix, ils ne sont pas des promesses.

## 1. Quels signaux ont une preuve hors échantillon, nette de coûts ?

| Famille | Ce que dit la littérature | Usage dans Hermes |
|---|---|---|
| Momentum transversal | Facteurs marché, taille, momentum (Liu, Tsyvinski, Wu 2022) ; momentum jusqu'à 2-4 semaines puis retournement ; facteur de tendance crypto robuste aux coûts sur les grandes capitalisations (Fieberg et al. 2025). Réplication nette de coûts sur perpétuels Binance : écarts peu distinguables de zéro (2026). | Variables lentes (`ret_168`, `ret_336`, `trend_*`), jamais une stratégie seule. |
| Retournement court terme | Présent surtout dans les petites capitalisations illiquides ; c'est une prime de fourniture de liquidité, plus forte en période de stress (Farag et al. 2025). | Variables `iret_*` résiduelles, conditionnées par la volatilité et la dispersion. |
| Funding / base | Le carry peut dépasser 40 %/an et un carry élevé prédit des krachs (Schmeling, Schrimpf, Todorov) ; deux facteurs (log-base + prix-volume) expliquent la plupart des prédicteurs de perpétuels (Cao et al.) ; le trade de base décroît (Sharpe négatif en 2025 selon Borri et al.). | Cible **nette du funding** ; variables `funding_*`, `premium_*` ; le carry fait partie du rendement. Depuis fin 2023, Binance règle de plus en plus de contrats toutes les 4 h (57-69 % de l'univers en 2026) : `features.funding_per_8h` met le dernier taux sur base 8 h, marque ces contrats et donne à chacun son horloge de règlement. |
| Flux d'ordres agresseur | +1 σ de flux → +0,2 % le lendemain, +0,9 % sur la semaine (Anastasopoulos et al. 2026) ; les stratégies de microstructure pure ne survivent pas aux frais de détail. | Déséquilibre agrégé sur 1-72 h (`flow_*`), persistance, divergence flux/prix. |
| Intérêt ouvert | Pas de preuve robuste publiée ; données parfois mal déclarées. | Optionnel (`include_metrics`), en interaction seulement. |
| Avance du BTC | Prévisibilité croisée réelle mais surtout à l'échelle de minutes sur les grands noms. | Retours du marché et résiduels. |
| Heure du jour | Effets de quelques points de base (horloge de New York depuis les ETF). | Variables calendaires ; exécution hors des bords d'heure. |

**Mise en garde centrale** (Junior 2026) : sur 10 perpétuels Binance, un classement XGBoost obtient un IC
de rang +0,024 (t = 3,55) **et** un Sharpe net de −2,91. Un IC significatif avec une rotation non maîtrisée
perd de l'argent. D'où l'optimiseur à coûts et l'amortissement par la persistance du signal.

## 2. Modèles

- Sur la section transversale crypto, toutes les méthodes d'apprentissage ajoutent de la valeur, la
  complexité supplémentaire peu (Cakici et al. 2024) ; un petit nombre de caractéristiques porte la
  prévisibilité.
- Modèles de fondation (Chronos, TimesFM, Moirai) en zéro-shot : R² négatif, précision directionnelle
  ≈ 50 %, battus par LightGBM/CatBoost (Rahimikia et al. 2025) ; ils ne battent significativement la marche
  aléatoire que dans 2 tâches sur 10 (Noguer i Alonso 2026). **Non retenus comme prédicteurs.**
- « Vertu de la complexité » contestée : les prévisions se réduisent à du momentum synchronisé sur la
  volatilité (Nagel 2025). Une attention transversale aide sur de grands panels actions (Kelly et al. 2025) ;
  30-100 actifs sur quelques années est petit — d'où un réseau **optionnel**, en diversifieur.
- **Choix** : LightGBM (Huber sur cibles gauss-rangées, grosses feuilles, arrêt précoce sur l'IC), Ridge de
  référence, ensemble pondéré par l'IC de validation.

## 3. Cibles et pertes

Rendement futur total (prix **et** funding), résiduel d'un bêta glissant, divisé par la volatilité ex ante,
rangé en scores normaux à chaque instant ; mélange des horizons 4, 8 et 24 h (un ensemble d'horizons en une
seule cible). Sélection sur la performance **nette** et non sur l'erreur quadratique. Option
`residualize: style` : le résidu est en plus projeté, à chaque barre, hors de la taille/liquidité et de la
volatilité (les expositions qu'un livre neutre aux styles retire), comme les cibles « résiduelles de Barra »
des actions : le modèle apprend alors ce qu'un tel livre peut réellement détenir.

## 4. Validation

Purge d'au moins l'horizon de la cible et embargo (López de Prado 2018) ; CPCV disponible (meilleure que le
walk-forward sur données synthétiques selon Arian et al. 2024) ; Sharpe dégonflé (Bailey & López de Prado
2014), PBO (Bailey et al. 2017), historique minimal (2012), SPA de Hansen (2005). Chaque essai est compté.

Détails qui décident si la porte sait dire « non » (corrigés après revue adversariale) :

- **DSR** : les variantes de la grille de robustesse sont très corrélées ; elles comptent pour
  `N_eff = ρ + (1 − ρ) N` essais (ρ = corrélation moyenne de leurs P&L), multipliés par les configurations
  du registre ; la dispersion des Sharpe entre essais n'est jamais prise sous la variance d'échantillonnage
  d'un Sharpe quotidien sous le nul (`1/(T−1)`), sans quoi la déflation disparaît.
- **Test nul** : p-valeur de permutation exacte `(1 + #{nul ≥ observé}) / (n + 1)` (Phipson & Smyth 2010),
  dont le niveau est garanti quel que soit le nombre de répliques ; blocs de permutation d'une semaine
  calendaire quelle que soit la bougie.
- **Sharpe annualisé** : variance de long terme de Newey-West (noyau de Bartlett, largeur automatique) ;
  l'autocorrélation positive pénalise, la négative ne gonfle jamais le Sharpe.
- **t de l'IC** : calculé sur les IC moyens **journaliers** (les IC barre à barre sont très dépendants :
  cibles qui se chevauchent, scores persistants, régimes) avec Newey-West.
- **Stress de coûts** : le livre est construit avec les coûts estimés habituels mais chaque transaction paie
  le double (frais, spread, impact) — une exécution pire que prévu, pas un livre ré-optimisé.
- **Années positives** : les années de moins de 90 jours hors échantillon ne votent pas.
- **Latence** : la simulation exécute à la clôture de la bougie ; la porte exige en plus un Sharpe positif
  avec **une bougie entière** de retard, plus sévère que le retard réel (quelques secondes).
- **Stops au pire** : les stops catastrophe sont simulés comme posés sur l'exchange (exécutés au prix du stop,
  ou à l'ouverture en cas de gap) ; la porte exige aussi un Sharpe positif quand chaque stop déclenché est
  exécuté au **plus bas (long) ou au plus haut (short) de la bougie** — un stop-marché dans un krach éclair,
  carnet vide sous le déclencheur (10 octobre 2025).
- **Concentration** (diagnostic) : Sharpe et rendement sans les 5 meilleurs jours.

## 5. Portefeuille

Alpha = IC × σ × score (Grinold) ; portefeuille visé moyenne-variance à coûts, trading partiel vers la
cible (Gârleanu & Pedersen 2013) ; zones de non-trading (NBIM) ; covariance facteur + EWMA rétrécie
(Ledoit & Wolf) ; volatilité cible ; fraction de Kelly implicitement ≤ ½ ; réduction du risque en drawdown
(Grossman & Zhou) ; plafonds par nom en part du volume quotidien.

**Livre 1/N sur plusieurs réglages** (`portfolio.books`, RESULTS § 15-16). Plutôt que de choisir un horizon de
détention et une aversion aux coûts sur un backtest (le choix a posteriori perd l'essentiel de son avance dès
qu'on le fait sans connaître l'avenir), le capital peut être réparti à parts égales entre plusieurs réglages
(DeMiguel, Garlappi & Uppal 2009). Chaque sous-livre garde ses positions et est réoptimisé sur sa part à partir
de son propre signal (lissage, IC estimé et amortissement des coûts à son horizon) ; la surcouche de risque et
les stops agissent sur la somme, et seuls les ordres nets partent, comme dans les fonds multi-stratégies qui
compensent les ordres de leurs portefeuilles avant exécution. Le moteur en direct persiste l'état des
sous-livres et le réconcilie à chaque décision avec les positions réellement détenues.

## 6. Exécution

Les ordres taker subissent une sélection adverse liée à la latence (Albers et al. 2025) ; les ordres maker
se remplissent surtout quand on a tort (« dilemme du teneur de marché ») : mesurer son propre taux de
remplissage. Politique retenue : post-only au meilleur prix avec réalignement, bascule en IOC borné après
un délai, pas de trade si l'alpha est inférieur au coût attendu. Impact en racine carrée (préfacteur ≈ 0,5-1
en unités de volatilité quotidienne ; Donier & Bonart 2015).

## Références principales

- Liu, Tsyvinski, Wu (2022), *Common Risk Factors in Cryptocurrency*, Journal of Finance.
- Cakici, Shahzad, Będowska-Sójka, Zaremba (2024), *Machine learning and the cross-section of cryptocurrency returns*, IRFA.
- Fieberg, Liedtke, Poddig, Walker, Zaremba (2025), *A Trend Factor for the Cross Section of Cryptocurrency Returns*, JFQA.
- Schmeling, Schrimpf, Todorov, *Crypto Carry*, Management Science.
- Anastasopoulos, Gradojevic, Liu, Maynard, Tsiakas (2026), *Order flow and cryptocurrency returns*, JFM.
- Farag, Luo, Yarovaya, Zięba (2025), *Returns from liquidity provision in cryptocurrency markets*, JBF.
- Rahimikia, Ni, Wang (2025), *Re(Visiting) Time Series Foundation Models in Finance*, arXiv 2511.18578.
- Nagel (2025), *Seemingly Virtuous Complexity in Return Prediction*, NBER w34104.
- Kelly, Kuznetsov, Malamud, Xu (2025), *Artificial Intelligence Asset Pricing Models*, NBER w33351.
- DeMiguel, Garlappi, Uppal (2009), *Optimal Versus Naive Diversification: How Inefficient is the 1/N Portfolio
  Strategy?*, Review of Financial Studies.
- Bailey, Borwein, López de Prado, Zhu (2017), *The Probability of Backtest Overfitting*, Journal of Computational
  Finance.
- Gârleanu, Pedersen (2013), *Dynamic Trading with Predictable Returns and Transaction Costs*, JF.
- Jensen, Kelly, Malamud, Pedersen (2026), *Machine Learning and the Implementable Efficient Frontier*, RFS.
- Bailey, López de Prado (2014), *The Deflated Sharpe Ratio* ; Bailey et al. (2017), *The Probability of Backtest Overfitting*.
- Phipson, Smyth (2010), *Permutation P-values Should Never Be Zero*. Newey, West (1994), *Automatic Lag Selection in Covariance Matrix Estimation*.
- López de Prado (2018), *Advances in Financial Machine Learning*.
- Hansen (2005), *A Test for Superior Predictive Ability*, JBES.
- Ledoit, Wolf (2020), *Analytical nonlinear shrinkage of large-dimensional covariance matrices*, Ann. Stat.
- Donier, Bonart (2015), *A Million Metaorder Analysis of Market Impact on the Bitcoin*.
- Albers, Cucuringu, Howison, Shestopaloff (2025), *The Market Maker's Dilemma*, arXiv 2502.18625.
- Junior (2026), *Failure of Cross-Sectional Alpha Screening on Cryptocurrency Perpetual Futures*, SSRN.
