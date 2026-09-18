/* ============================================================
   Les langues de l'interface — français, anglais, albanais.

   Un seul dictionnaire, trois colonnes, et une règle : TOUT texte que
   la page montre vient d'ici. C'est aussi la passe de « bon
   français » : chaque chaîne est écrite une fois, correctement —
   apostrophes typographiques, espaces insécables devant les signes
   doubles — au lieu d'être corrigée à dix endroits.

   Ce qui n'est PAS traduit, et c'est un choix : le journal du moteur.
   Ses lignes sont la voix technique du serveur — des données, pas de
   l'interface — et les traduire fabriquerait deux vérités.

   La langue est un réglage d'affichage : elle vit dans le navigateur
   (localStorage), pas sur le serveur.
   ============================================================ */
"use strict";

const Langues = (() => {

  const D = {
    /* ===== pages ajoutées par la plateforme quantitative ===== */
    "nav.decisions":     { fr: "Décisions", en: "Decisions", sq: "Vendimet" },
    "nav.recherche":     { fr: "Recherche", en: "Research", sq: "Kërkimi" },
    "nav.jev":           { fr: "JEV", en: "JEV", sq: "JEV" },
    "nav.risque":        { fr: "Risque", en: "Risk", sq: "Rreziku" },
    "bandeau.synth.titre": { fr: "Données synthétiques.", en: "Synthetic data.", sq: "Të dhëna sintetike." },
    "bandeau.synth.texte": {
      fr: "Cet écran affiche un jeu de test : aucun chiffre ne provient d’un marché réel.",
      en: "This screen shows a test dataset: no figure comes from a real market.",
      sq: "Ky ekran shfaq një grup testimi: asnjë numër nuk vjen nga një treg real." },
    "indispo":           { fr: "Non disponible", en: "Not available", sq: "E padisponueshme" },
    "dec.titre":         { fr: "Décisions à la minute", en: "Minute-by-minute decisions", sq: "Vendimet minutë pas minute" },
    "dec.rejets":        { fr: "Candidats rejetés et leurs raisons", en: "Rejected candidates and their reasons", sq: "Kandidatët e refuzuar dhe arsyet" },
    "dec.vide":          { fr: "Aucune décision enregistrée pour l’instant.", en: "No decision recorded yet.", sq: "Asnjë vendim i regjistruar." },
    "dec.cutoff":        { fr: "Instant de coupe", en: "Cutoff", sq: "Momenti i prerjes" },
    "dec.issue":         { fr: "Issue", en: "Outcome", sq: "Rezultati" },
    "dec.raisons":       { fr: "Raisons", en: "Reasons", sq: "Arsyet" },
    "dec.modele":        { fr: "Modèle", en: "Model", sq: "Modeli" },
    "dec.univers":       { fr: "Univers", en: "Universe", sq: "Universi" },
    "dec.brut":          { fr: "Avantage brut", en: "Gross edge", sq: "Avantazhi bruto" },
    "dec.net":           { fr: "Avantage net", en: "Net edge", sq: "Avantazhi neto" },
    "dec.couts":         { fr: "Coûts", en: "Costs", sq: "Kostot" },
    "dec.incertitude":   { fr: "Incertitude", en: "Uncertainty", sq: "Pasiguria" },
    "dec.duree":         { fr: "Durée", en: "Duration", sq: "Kohëzgjatja" },
    "dec.provenance":    { fr: "Provenance", en: "Provenance", sq: "Prejardhja" },
    "rech.avertissement.titre": { fr: "Résultats de recherche, pas de promesses.", en: "Research results, not promises.", sq: "Rezultate kërkimore, jo premtime." },
    "rech.avertissement.texte": {
      fr: "Une expérience n’est une preuve que si sa période est indépendante et ses coûts inclus. Les périodes consultées perdent leur statut indépendant.",
      en: "An experiment proves something only if its period is independent and its costs are included. A period once consulted loses its independent status.",
      sq: "Një eksperiment provon diçka vetëm nëse periudha është e pavarur dhe kostot përfshihen. Një periudhë e konsultuar humbet pavarësinë." },
    "rech.experiences":  { fr: "Expériences enregistrées", en: "Recorded experiments", sq: "Eksperimentet e regjistruara" },
    "rech.modeles":      { fr: "Versions de modèles et statuts de promotion", en: "Model versions and promotion statuses", sq: "Versionet e modeleve dhe statuset" },
    "rech.jev":          { fr: "Apport de JEV : A, B, C, D", en: "JEV contribution: A, B, C, D", sq: "Kontributi i JEV: A, B, C, D" },
    "rech.vide":         { fr: "Aucune expérience enregistrée.", en: "No experiment recorded.", sq: "Asnjë eksperiment i regjistruar." },
    "rech.plan":         { fr: "Plan", en: "Plan", sq: "Plani" },
    "rech.periode":      { fr: "Période", en: "Period", sq: "Periudha" },
    "rech.statut":       { fr: "Statut", en: "Status", sq: "Statusi" },
    "rech.essais":       { fr: "Essais", en: "Trials", sq: "Provat" },
    "rech.independant":  { fr: "Période indépendante", en: "Independent period", sq: "Periudhë e pavarur" },
    "rech.consultee":    { fr: "consultée : statut perdu", en: "consulted: status lost", sq: "e konsultuar: statusi humbi" },
    "jev.sources":       { fr: "Sources et fraîcheur", en: "Sources and freshness", sq: "Burimet dhe freskia" },
    "jev.evenements":    { fr: "Événements évalués", en: "Evaluated events", sq: "Eventet e vlerësuara" },
    "jev.vide":          { fr: "Aucun événement évalué.", en: "No evaluated event.", sq: "Asnjë event i vlerësuar." },
    "jev.mode":          { fr: "Influence", en: "Influence", sq: "Influenca" },
    "jev.disponible":    { fr: "Disponibilité", en: "Availability", sq: "Disponueshmëria" },
    "jev.age":           { fr: "Âge", en: "Age", sq: "Mosha" },
    "jev.latence":       { fr: "Latence", en: "Latency", sq: "Latenca" },
    "jev.cache":         { fr: "Cache", en: "Cache", sq: "Cache" },
    "jev.erreurs":       { fr: "Erreurs", en: "Errors", sq: "Gabimet" },
    "jev.budget":        { fr: "Budget du jour", en: "Daily budget", sq: "Buxheti i ditës" },
    "jev.type":          { fr: "Type d’événement", en: "Event kind", sq: "Tipi i eventit" },
    "jev.mapping":       { fr: "Correspondance actif", en: "Asset mapping", sq: "Përputhja e aktivit" },
    "jev.version":       { fr: "Version", en: "Version", sq: "Versioni" },
    "jev.statut":        { fr: "Statut sémantique", en: "Semantic status", sq: "Statusi semantik" },
    "risq.etat":         { fr: "État de risque", en: "Risk state", sq: "Gjendja e rrezikut" },
    "risq.commandes":    { fr: "Commandes autorisées", en: "Authorised commands", sq: "Komandat e lejuara" },
    "risq.incidents":    { fr: "Incidents de risque", en: "Risk incidents", sq: "Incidentet e rrezikut" },
    "risq.qualite":      { fr: "Qualité des données", en: "Data quality", sq: "Cilësia e të dhënave" },
    "risq.pause":        { fr: "Suspendre les entrées", en: "Suspend entries", sq: "Pezullo hyrjet" },
    "risq.annuler":      { fr: "Annuler les ordres d’entrée", en: "Cancel entry orders", sq: "Anulo urdhrat e hyrjes" },
    "risq.flatten":      { fr: "Demander une sortie", en: "Request flatten", sq: "Kërko daljen" },
    "risq.reprise":      { fr: "Demander la reprise", en: "Request resume", sq: "Kërko riparimin" },
    "risq.note":         {
      fr: "Une demande acceptée n’est pas une action effectuée : le runtime l’exécute sous préconditions, et le suivi montre son résultat observé, résidus compris.",
      en: "An accepted request is not a completed action: the runtime executes it under preconditions, and the tracker shows the observed result, residuals included.",
      sq: "Një kërkesë e pranuar nuk është veprim i kryer: runtime-i e ekzekuton nën parakushte dhe ndjekja shfaq rezultatin e vërejtur." },
    "risq.halt":         { fr: "Niveau d’arrêt", en: "Halt level", sq: "Niveli i ndalimit" },
    "risq.perte":        { fr: "Perte du jour", en: "Daily loss", sq: "Humbja e ditës" },
    "risq.hwm":          { fr: "Sommet d’équité", en: "High-water mark", sq: "Kulmi i kapitalit" },
    "risq.limites":      { fr: "Version des limites", en: "Limits version", sq: "Versioni i limiteve" },
    "risq.vide":         { fr: "Aucun incident enregistré.", en: "No incident recorded.", sq: "Asnjë incident." },
    "risq.confirm.titre": { fr: "Confirmer cette commande", en: "Confirm this command", sq: "Konfirmo komandën" },
    "risq.confirm.compte": { fr: "Compte", en: "Account", sq: "Llogaria" },
    "risq.confirm.mode": { fr: "Mode", en: "Mode", sq: "Mënyra" },
    "risq.confirm.oui":  { fr: "Confirmer", en: "Confirm", sq: "Konfirmo" },
    "risq.confirm.non":  { fr: "Annuler", en: "Cancel", sq: "Anulo" },
    "risq.demande":      { fr: "Demande enregistrée", en: "Request recorded", sq: "Kërkesa u regjistrua" },
    "risq.refus":        { fr: "Refusé", en: "Refused", sq: "E refuzuar" },
    "tete.refus":        { fr: "accès refusé", en: "access denied", sq: "akses i refuzuar" },

    /* ===== l'en-tête ===== */
    "nav.marche":        { fr: "Marché", en: "Market", sq: "Tregu" },
    "nav.labo":          { fr: "Laboratoire", en: "Laboratory", sq: "Laboratori" },
    "tete.liaison":      { fr: "liaison…", en: "connecting…", sq: "duke u lidhur…" },
    "tete.relie":        { fr: "relié", en: "connected", sq: "i lidhur" },
    "tete.horsligne":    { fr: "hors ligne", en: "offline", sq: "jashtë linje" },
    "tete.marche":       { fr: "moteur en marche", en: "engine running", sq: "motori në punë" },
    "tete.arret":        { fr: "moteur à l’arrêt", en: "engine stopped", sq: "motori i ndalur" },
    "tete.pilotage":     { fr: "pilotage", en: "full control", sq: "drejtim i plotë" },
    "tete.lecture":      { fr: "lecture seule", en: "read-only", sq: "vetëm lexim" },
    "tete.mode":         { fr: "mode…", en: "mode…", sq: "mënyra…" },
    "tete.demarrer":     { fr: "Démarrer", en: "Start", sq: "Nis" },
    "tete.arreter":      { fr: "Arrêter", en: "Stop", sq: "Ndal" },
    "tete.indispo":      { fr: "Indisponible en lecture seule", en: "Unavailable in read-only mode", sq: "E padisponueshme në vetëm lexim" },
    "tete.theme":        { fr: "Clair / sombre", en: "Light / dark", sq: "E çelët / e errët" },

    /* ===== le héros ===== */
    "hero.equite":       { fr: "Équité", en: "Equity", sq: "Kapitali" },
    "hero.attente":      { fr: "en attente du premier relevé", en: "waiting for the first reading", sq: "në pritje të leximit të parë" },
    "hero.aujourdhui":   { fr: "aujourd’hui · {n} trade{s}", en: "today · {n} trade{s}", sq: "sot · {n} tregti" },

    /* ===== les tuiles ===== */
    "tuile.marge":       { fr: "Marge engagée", en: "Margin in use", sq: "Marzhi i angazhuar" },
    "tuile.notionnel":   { fr: "notionnel {v}", en: "notional {v}", sq: "nocionali {v}" },
    "tuile.pnl":         { fr: "PnL latent", en: "Unrealized PnL", sq: "PnL i parealizuar" },
    "tuile.pnl.sous":    { fr: "sur les positions ouvertes", en: "across open positions", sq: "në pozicionet e hapura" },
    "tuile.positions":   { fr: "Positions", en: "Positions", sq: "Pozicionet" },
    "tuile.dispo":       { fr: "disponible {v}", en: "available {v}", sq: "në dispozicion {v}" },
    "tuile.gain":        { fr: "Taux de gain", en: "Win rate", sq: "Përqindja e fitoreve" },
    "tuile.gain.sous":   { fr: "sur 24 heures", en: "over 24 hours", sq: "në 24 orë" },

    /* ===== les positions ===== */
    "pos.titre":         { fr: "Positions", en: "Positions", sq: "Pozicionet" },
    "pos.aucune":        { fr: "aucune", en: "none", sq: "asnjë" },
    "pos.ouvertes":      { fr: "{n} ouverte{s}", en: "{n} open", sq: "{n} të hapura" },
    "pos.vide.titre":    { fr: "Aucune position ouverte", en: "No open positions", sq: "Asnjë pozicion i hapur" },
    "pos.vide.texte":    { fr: "Les positions apparaissent ici dès qu’une stratégie en ouvre une, avec leur stop et leur take-profit.",
                           en: "Positions appear here as soon as a strategy opens one, with their stop and take-profit.",
                           sq: "Pozicionet shfaqen këtu sapo një strategji hap një të tillë, me stopin dhe take-profitin e tyre." },
    "pos.vuetableau":    { fr: "Vue tableau", en: "Table view", sq: "Pamje tabelare" },
    "pos.vuecartes":     { fr: "Vue cartes", en: "Card view", sq: "Pamje me karta" },
    "pos.taille":        { fr: "Taille", en: "Size", sq: "Madhësia" },
    "pos.marge":         { fr: "Marge", en: "Margin", sq: "Marzhi" },
    "pos.notionnel":     { fr: "Notionnel", en: "Notional", sq: "Nocionali" },
    "pos.tenue":         { fr: "Tenue", en: "Held", sq: "Mbajtur" },
    "pos.delamarge":     { fr: "{v} de la marge", en: "{v} of margin", sq: "{v} e marzhit" },
    "pos.sens":          { fr: "Sens", en: "Side", sq: "Kahu" },
    "pos.levier":        { fr: "Levier", en: "Leverage", sq: "Leva" },
    "pos.entree":        { fr: "Entrée", en: "Entry", sq: "Hyrja" },
    "pos.prix":          { fr: "Prix", en: "Price", sq: "Çmimi" },
    "pos.stop":          { fr: "Stop", en: "Stop", sq: "Stopi" },
    "pos.mode":          { fr: "Mode", en: "Mode", sq: "Mënyra" },
    "pos.instrument":    { fr: "Instrument", en: "Instrument", sq: "Instrumenti" },
    "pos.ouvrirgraphe":  { fr: "Ouvrir le graphique", en: "Open the chart", sq: "Hap grafikun" },
    "pos.trail.pose":    { fr: "posé", en: "armed", sq: "i vendosur" },
    "pos.trail.titre":   { fr: "Un ordre de suivi est posé côté exchange", en: "A trailing order is armed on the exchange", sq: "Një urdhër ndjekës është vendosur në bursë" },
    "pos.seuil.titre":   { fr: "Stop remonté au point mort : la position ne peut plus perdre", en: "Stop raised to break-even: the position can no longer lose", sq: "Stopi u ngrit në pikën e barazimit: pozicioni s’mund të humbasë më" },
    "pos.trail.chip":    { fr: "Le stop suit le prix et ne redescend jamais", en: "The stop follows the price and never moves back", sq: "Stopi ndjek çmimin dhe s’kthehet kurrë mbrapa" },
    "leg.stop":          { fr: "stop", en: "stop", sq: "stopi" },
    "leg.stopverrou":    { fr: "stop (gain verrouillé)", en: "stop (profit locked)", sq: "stopi (fitim i kyçur)" },
    "leg.entree":        { fr: "entrée", en: "entry", sq: "hyrja" },
    "leg.prix":          { fr: "prix", en: "price", sq: "çmimi" },
    "leg.tp":            { fr: "take-profit", en: "take-profit", sq: "take-profit" },
    "regle.depart":      { fr: "départ {v}", en: "from {v}", sq: "nga {v}" },

    /* ===== les clés OKX ===== */
    "cles.titre":        { fr: "Aucune clé OKX sur le serveur.", en: "No OKX keys on the server.", sq: "Asnjë çelës OKX në server." },
    "cles.texte":        { fr: "Hermes-Astra reçoit les prix et calcule ses signaux, mais il ne peut ni ouvrir ni fermer une position, et le compte affiché reste à zéro — ce n’est pas une perte, c’est un compte qu’il ne peut pas lire.",
                           en: "Hermes-Astra receives prices and computes its signals, but it can neither open nor close a position, and the displayed account stays at zero — that is not a loss, it is an account it cannot read.",
                           sq: "Hermes-Astra merr çmimet dhe llogarit sinjalet e veta, por s’mund të hapë e as të mbyllë pozicione, dhe llogaria e shfaqur mbetet zero — s’është humbje, është një llogari që ai s’mund ta lexojë." },
    "cles.placeholder":  { fr: "Collez ici les lignes de votre .env — ou le fichier entier.\n\nOKX_API_KEY=...\nOKX_API_SECRET=...\nOKX_API_PASSPHRASE=...",
                           en: "Paste your .env lines here — or the whole file.\n\nOKX_API_KEY=...\nOKX_API_SECRET=...\nOKX_API_PASSPHRASE=...",
                           sq: "Ngjitni këtu rreshtat e .env tuaj — ose skedarin e plotë.\n\nOKX_API_KEY=...\nOKX_API_SECRET=...\nOKX_API_PASSPHRASE=..." },
    "cles.poser":        { fr: "Poser les clés", en: "Set the keys", sq: "Vendos çelësat" },
    "cles.note":         { fr: "Collez le fichier entier si c’est plus simple : Hermes-Astra n’y prend que les trois lignes OKX et ignore tout le reste. Les clés sont testées auprès d’OKX avant d’être écrites, et rien n’est enregistré si OKX les refuse. Ce formulaire ne sert qu’à l’amorçage : une fois des clés en place, il disparaît et ne peut plus les remplacer.",
                           en: "Paste the whole file if that is easier: Hermes-Astra only reads the three OKX lines and ignores everything else. The keys are tested against OKX before being written, and nothing is saved if OKX rejects them. This form is for bootstrapping only: once keys are in place it disappears and can no longer replace them.",
                           sq: "Ngjitni skedarin e plotë nëse është më e thjeshtë: Hermes-Astra merr vetëm tre rreshtat OKX dhe shpërfill gjithçka tjetër. Çelësat provohen te OKX para se të shkruhen, dhe asgjë nuk ruhet nëse OKX i refuzon. Ky formular shërben vetëm për nisje: sapo çelësat të jenë vendosur, ai zhduket dhe s’mund t’i zëvendësojë më." },
    "cles.vide":         { fr: "Collez d’abord les lignes de votre .env.", en: "Paste your .env lines first.", sq: "Ngjitni fillimisht rreshtat e .env tuaj." },
    "cles.test":         { fr: "Test auprès d’OKX…", en: "Testing against OKX…", sq: "Duke provuar te OKX…" },
    "cles.ok":           { fr: "Clés posées et prises en compte : le moteur peut trader.", en: "Keys set and live: the engine can trade.", sq: "Çelësat u vendosën dhe janë aktivë: motori mund të tregtojë." },
    "cles.incomplet":    { fr: "Introuvable dans ce qui a été collé : {v}", en: "Not found in what was pasted: {v}", sq: "Nuk u gjet në atë që u ngjit: {v}" },
    "cles.deja":         { fr: "Des clés sont déjà en place : ce formulaire ne peut pas les remplacer.", en: "Keys are already in place: this form cannot replace them.", sq: "Çelësat janë tashmë të vendosur: ky formular s’mund t’i zëvendësojë." },
    "cles.refus":        { fr: "OKX a refusé ces clés — rien n’a été écrit. {v}", en: "OKX rejected these keys — nothing was written. {v}", sq: "OKX i refuzoi këta çelësa — asgjë nuk u shkrua. {v}" },

    /* ===== lecture seule ===== */
    "lecture.titre":     { fr: "Lecture seule.", en: "Read-only.", sq: "Vetëm lexim." },
    "lecture.texte":     { fr: "Cette page montre tout mais ne commande rien. Le droit d’agir vient du rôle porté par votre clé d’accès, pas de l’adresse d’écoute : un lecteur voit tout et ne peut rien demander. Pour piloter, ouvrez l’interface avec une clé de rôle <code>operator</code>. Même alors, une commande n’est qu’une demande auditée : le runtime l’exécute sous préconditions.",
                           en: "This page shows everything but commands nothing. The right to act comes from the role carried by your access key, not from the listening address: a reader sees everything and can request nothing. To take control, open the interface with an <code>operator</code> role key. Even then, a command is only an audited request: the runtime executes it under preconditions.",
                           sq: "Kjo faqe tregon gjithçka por s’komandon asgjë. E drejta e veprimit vjen nga roli i çelësit tuaj të aksesit, jo nga adresa e dëgjimit: lexuesi shikon gjithçka dhe nuk mund të kërkojë asgjë. Për të drejtuar, hapni ndërfaqen me një çelës me rol <code>operator</code>. Edhe atîherë, komanda është vetëm kërkesë e auditueme: runtime-i e ekzekuton nën parakushte." },

    /* ===== courbe, santé, journal ===== */
    "courbe.titre":      { fr: "Courbe d’équité", en: "Equity curve", sq: "Kurba e kapitalit" },
    "courbe.points":     { fr: "{n} points", en: "{n} points", sq: "{n} pika" },
    "sante.titre":       { fr: "Santé", en: "Health", sq: "Gjendja" },
    "journal.titre":     { fr: "Journal", en: "Log", sq: "Ditari" },
    "journal.vider":     { fr: "Vider", en: "Clear", sq: "Pastro" },

    /* ===== le graphique ===== */
    "gr.chargement":     { fr: "Chargement des chandelles…", en: "Loading candles…", sq: "Duke ngarkuar qirinjtë…" },
    "gr.echec":          { fr: "Les chandelles ne sont pas arrivées{v}.", en: "The candles did not arrive{v}.", sq: "Qirinjtë nuk mbërritën{v}." },
    "gr.injoignable":    { fr: "Le serveur n’a pas répondu.", en: "The server did not respond.", sq: "Serveri nuk u përgjigj." },
    "gr.fermer":         { fr: "Fermer le graphique", en: "Close the chart", sq: "Mbyll grafikun" },
    "gr.unite":          { fr: "Unité de temps", en: "Timeframe", sq: "Njësia kohore" },
    "gr.aria":           { fr: "Graphique de la position", en: "Position chart", sq: "Grafiku i pozicionit" },
    "gr.o":              { fr: "O", en: "O", sq: "H" },
    "gr.h":              { fr: "H", en: "H", sq: "L" },
    "gr.b":              { fr: "B", en: "L", sq: "U" },
    "gr.c":              { fr: "C", en: "C", sq: "M" },
    "gr.vol":            { fr: "VOL", en: "VOL", sq: "VOL" },
    "gr.entree.tag":     { fr: "ENTRÉE", en: "ENTRY", sq: "HYRJA" },
    "gr.seuil.tag":      { fr: "SEUIL", en: "B/E", sq: "PRAGU" },
    "gr.niv.entree":     { fr: "Entrée", en: "Entry", sq: "Hyrja" },
    "gr.niv.tp":         { fr: "Take-profit", en: "Take-profit", sq: "Take-profit" },
    "gr.niv.stop":       { fr: "Stop", en: "Stop", sq: "Stopi" },
    "gr.niv.stopverrou": { fr: "Stop (gain verrouillé)", en: "Stop (profit locked)", sq: "Stopi (fitim i kyçur)" },
    "gr.niv.trail":      { fr: "Stop suiveur", en: "Trailing stop", sq: "Stop ndjekës" },
    "gr.niv.liq":        { fr: "Liquidation", en: "Liquidation", sq: "Likuidimi" },
    "gr.niv.prix":       { fr: "Prix", en: "Price", sq: "Çmimi" },
    "gr.niv.tenue":      { fr: "Tenue", en: "Held", sq: "Mbajtur" },
    "gr.fermee":         { fr: "position fermée", en: "position closed", sq: "pozicion i mbyllur" },
    "gr.plusouverte":    { fr: "Cette position n’est plus ouverte — le graphique reste consultable.", en: "This position is no longer open — the chart remains available.", sq: "Ky pozicion s’është më i hapur — grafiku mbetet i disponueshëm." },

    /* ===== le laboratoire ===== */
    "labo.titre":        { fr: "Laboratoire autonome", en: "Autonomous laboratory", sq: "Laboratori autonom" },
    "labo.sous":         { fr: "Toutes les trente minutes, le chercheur rejoue le concours entier : treize signaux, quatre familles de sorties, trois durées — jugés sur deux fenêtres de sélection disjointes, puis validés sur sept jours jamais regardés par le choix. Pas de perle = pas de trade.",
                           en: "Every thirty minutes, the researcher replays the whole contest: thirteen signals, four exit families, three durations — judged on two disjoint selection windows, then validated on seven days the choice never saw. No pearl = no trade.",
                           sq: "Çdo tridhjetë minuta, kërkuesi e riluan të gjithë konkursin: trembëdhjetë sinjale, katër familje daljesh, tri kohëzgjatje — të gjykuara në dy dritare të ndara përzgjedhjeje, pastaj të vlerësuara në shtatë ditë që zgjedhja s’i ka parë kurrë. S’ka perlë = s’ka tregti." },
    "labo.perles.sur":   { fr: "perle{s} sur {n} candidats", en: "pearl{s} out of {n} candidates", sq: "perla nga {n} kandidatë" },
    "labo.attente":      { fr: "en attente du premier verdict", en: "waiting for the first verdict", sq: "në pritje të verdiktit të parë" },
    "labo.derniere":     { fr: "Dernière passe", en: "Last run", sq: "Kalimi i fundit" },
    "labo.prochaine":    { fr: "Prochaine", en: "Next", sq: "Tjetri" },
    "labo.imminente":    { fr: "imminente", en: "imminent", sq: "i afërt" },
    "labo.dans":         { fr: "dans {v}", en: "in {v}", sq: "pas {v}" },
    "labo.fenetres":     { fr: "Fenêtres", en: "Windows", sq: "Dritaret" },
    "labo.fenetres.val": { fr: "{j} j · validation {v} j", en: "{j} d · validation {v} d", sq: "{j} d · vlerësim {v} d" },
    "labo.joue":         { fr: "Le moteur joue", en: "The engine plays", sq: "Motori luan" },
    "labo.verdict":      { fr: "ce verdict", en: "this verdict", sq: "këtë verdikt" },
    "labo.repli":        { fr: "le repli du 31/08", en: "the 31/08 fallback", sq: "rezervën e 31/08" },
    "labo.encours":      { fr: "Recherche en cours", en: "Search running", sq: "Kërkimi në punë" },
    "labo.candidats":    { fr: "candidats…", en: "candidates…", sq: "kandidatët…" },
    "labo.lancer":       { fr: "Lancer une recherche", en: "Run a search", sq: "Nis një kërkim" },
    "labo.enrecherche":  { fr: "Recherche en cours…", en: "Search in progress…", sq: "Kërkimi në vazhdim…" },
    "labo.constellation":{ fr: "La constellation", en: "The constellation", sq: "Plejada" },
    "labo.posees":       { fr: "{n} posée{s}", en: "{n} placed", sq: "{n} të vendosura" },
    "labo.carte.aria":   { fr: "Constellation : winrate de sélection contre winrate de validation", en: "Constellation: selection win rate versus validation win rate", sq: "Plejada: përqindja e fitoreve në përzgjedhje kundrejt vlerësimit" },
    "labo.carte.vide":   { fr: "La constellation se dessinera au premier verdict du chercheur.<br>Chaque perle y sera posée par ses deux winrates — sélection et validation —<br>et le sur-ajustement se verra à l’œil : loin sous la diagonale, une stratégie a promis plus qu’elle n’a tenu.",
                           en: "The constellation will draw itself at the researcher’s first verdict.<br>Each pearl will be placed by its two win rates — selection and validation —<br>and overfitting will be visible at a glance: far below the diagonal, a strategy promised more than it delivered.",
                           sq: "Plejada do të vizatohet me verdiktin e parë të kërkuesit.<br>Çdo perlë do të vendoset nga dy përqindjet e saj të fitoreve — përzgjedhja dhe vlerësimi —<br>dhe mbipërshtatja do të duket me sy: larg nën diagonale, një strategji premtoi më shumë se ç’mbajti." },
    "labo.axe.x":        { fr: "winrate de sélection (%)", en: "selection win rate (%)", sq: "fitore në përzgjedhje (%)" },
    "labo.axe.y":        { fr: "winrate de validation (%)", en: "validation win rate (%)", sq: "fitore në vlerësim (%)" },
    "labo.leg.perles":   { fr: "perles retenues", en: "retained pearls", sq: "perlat e mbajtura" },
    "labo.leg.ecartes":  { fr: "vainqueurs écartés", en: "rejected winners", sq: "fituesit e skartuar" },
    "labo.diagonale":    { fr: "au-dessus : a confirmé mieux qu’annoncé", en: "above: confirmed better than promised", sq: "sipër: konfirmoi më mirë se ç’premtoi" },
    "labo.point.titre":  { fr: "{nom} — sélection {x} %, validation {y} %, net {net} marges", en: "{nom} — selection {x}%, validation {y}%, net {net} margins", sq: "{nom} — përzgjedhje {x}%, vlerësim {y}%, neto {net} marzhe" },
    "labo.retenues":     { fr: "Les perles retenues", en: "Pearls retained", sq: "Perlat e mbajtura" },
    "labo.sorties":      { fr: "TP +{tp} % marge · trail dès +{act} % · ≤ {h} h", en: "TP +{tp}% margin · trail from +{act}% · ≤ {h}h", sq: "TP +{tp}% marzh · trail nga +{act}% · ≤ {h} orë" },
    "labo.selection":    { fr: "Sélection", en: "Selection", sq: "Përzgjedhja" },
    "labo.validation":   { fr: "Validation", en: "Validation", sq: "Vlerësimi" },
    "labo.trades":       { fr: "{n} trades", en: "{n} trades", sq: "{n} tregti" },
    "labo.net":          { fr: "net {a} marges en sélection · {b} en validation", en: "net {a} margins in selection · {b} in validation", sq: "neto {a} marzhe në përzgjedhje · {b} në vlerësim" },
    "labo.vide.verdict": { fr: "Le chercheur n’a pas encore rendu son premier verdict — il concourt en ce moment, et cette page se remplira seule.", en: "The researcher has not delivered its first verdict yet — it is competing right now, and this page will fill itself.", sq: "Kërkuesi s’e ka dhënë ende verdiktin e parë — po konkurron tani, dhe kjo faqe do të mbushet vetë." },
    "labo.vide.repli":   { fr: "En attendant, le moteur joue le roster de repli du 31/08 :", en: "Meanwhile, the engine plays the 31/08 fallback roster:", sq: "Ndërkohë, motori luan listën rezervë të 31/08:" },
    "labo.ecartees":     { fr: "Écartées — le verdict honnête", en: "Rejected — the honest verdict", sq: "Të skartuarat — verdikti i ndershëm" },
    "labo.rien.ecarte":  { fr: "Rien d’écarté sur la dernière passe.", en: "Nothing rejected on the last run.", sq: "Asgjë e skartuar në kalimin e fundit." },
    "labo.concourantes": { fr: "{n} concourante{s}", en: "{n} contender{s}", sq: "{n} konkurrente" },
    "refus.validation":  { fr: "le vainqueur ({v}) échoue en validation", en: "the winner ({v}) fails validation", sq: "fituesi ({v}) dështon në vlerësim" },
    "refus.aucune":      { fr: "aucune concourante positive dans A et B", en: "no contender positive in both A and B", sq: "asnjë konkurrente pozitive në A dhe B" },
    "refus.courte":      { fr: "histoire trop courte", en: "history too short", sq: "histori tepër e shkurtër" },
    "refus.collecte":    { fr: "échec de collecte", en: "data collection failed", sq: "mbledhja e të dhënave dështoi" },

    /* ===== tuiles (suite), santé, journal, courbe ===== */
    "tuile.trades":      { fr: "{n} trades au total", en: "{n} trades overall", sq: "{n} tregti gjithsej" },
    "sante.attente":     { fr: "En attente du premier battement.", en: "Waiting for the first heartbeat.", sq: "Në pritje të rrahjes së parë." },
    "sante.vert":        { fr: "{ok} / {n} au vert", en: "{ok} / {n} green", sq: "{ok} / {n} në rregull" },
    "sante.wsPublic":    { fr: "Flux public", en: "Public feed", sq: "Rrjedha publike" },
    "sante.wsPrivate":   { fr: "Flux privé", en: "Private feed", sq: "Rrjedha private" },
    "sante.rest":        { fr: "API REST", en: "REST API", sq: "API REST" },
    "sante.dataFlow":    { fr: "Données", en: "Data", sq: "Të dhënat" },
    "sante.strategy":    { fr: "Stratégie", en: "Strategy", sq: "Strategjia" },
    "sante.aiEngine":    { fr: "Moteur", en: "Engine", sq: "Motori" },
    "sante.orders":      { fr: "Ordres", en: "Orders", sq: "Urdhrat" },
    "sante.stops":       { fr: "Protections", en: "Protections", sq: "Mbrojtjet" },
    "sante.portfolio":   { fr: "Portefeuille", en: "Portfolio", sq: "Portofoli" },
    "journal.vide":      { fr: "Rien pour l’instant. Le journal se remplit dès que le moteur agit.",
                           en: "Nothing yet. The log fills up as soon as the engine acts.",
                           sq: "Asgjë ende. Ditari mbushet sapo motori vepron." },
    "courbe.vide.titre": { fr: "Pas encore d’historique", en: "No history yet", sq: "Ende pa historik" },
    "courbe.vide.texte": { fr: "La courbe apparaît dès que le moteur a relevé quelques points.",
                           en: "The curve appears as soon as the engine has recorded a few points.",
                           sq: "Kurba shfaqet sapo motori të ketë regjistruar disa pika." },
    "courbe.plate.titre":{ fr: "Équité constante à {v} $", en: "Equity flat at {v} $", sq: "Kapital konstant në {v} $" },
    "courbe.plate.texte":{ fr: "{n} points relevés, tous identiques. La courbe apparaîtra dès que l’équité bougera.",
                           en: "{n} points recorded, all identical. The curve will appear as soon as equity moves.",
                           sq: "{n} pika të regjistruara, të gjitha njësoj. Kurba do të shfaqet sapo kapitali të lëvizë." },
    "courbe.plats":      { fr: "{n} points, plats", en: "{n} points, flat", sq: "{n} pika, të sheshta" },
    "courbe.haut":       { fr: "Plus haut", en: "High", sq: "Më e larta" },
    "courbe.bas":        { fr: "Plus bas", en: "Low", sq: "Më e ulëta" },
    "courbe.ampli":      { fr: "Amplitude", en: "Range", sq: "Amplituda" },
    "courbe.depuis":     { fr: "Depuis le début de la fenêtre", en: "Since the window start", sq: "Që nga fillimi i dritares" },

    /* ===== le détail dépliable du laboratoire ===== */
    "labo.methode":      { fr: "Comment le juge décide", en: "How the judge decides", sq: "Si vendos gjykatësi" },
    "labo.m1.titre":     { fr: "Concourir", en: "Compete", sq: "Konkurro" },
    "labo.m1.texte":     { fr: "156 combinaisons — treize signaux × quatre familles de sorties × trois durées — sont rejouées sur l’histoire, avec les formules exactes que le moteur trade. Pour concourir, il faut être positive dans DEUX sous-fenêtres disjointes, A puis B : le hasard doit réussir deux fois avant même d’entrer au classement.",
                           en: "156 combinations — thirteen signals × four exit families × three durations — are replayed over the history, with the exact formulas the engine trades. To compete, one must be profitable in TWO disjoint sub-windows, A then B: luck has to succeed twice before even entering the ranking.",
                           sq: "156 kombinime — trembëdhjetë sinjale × katër familje daljesh × tri kohëzgjatje — riluhen mbi historinë, me formulat e sakta që tregton motori. Për të konkurruar, duhet të jesh fitimprurëse në DY nëndritare të ndara, A pastaj B: fati duhet t’ia dalë dy herë para se të hyjë në renditje." },
    "labo.m2.titre":     { fr: "Couronner", en: "Crown", sq: "Kurorëzo" },
    "labo.m2.texte":     { fr: "Parmi les concourantes, le meilleur winrate l’emporte — le gain net départage les ex æquo. Un seul vainqueur par crypto.",
                           en: "Among the contenders, the best win rate prevails — net gain breaks ties. One winner per crypto.",
                           sq: "Mes konkurrenteve, fiton përqindja më e mirë e fitoreve — fitimi neto ndan barazimet. Një fitues i vetëm për kriptomonedhë." },
    "labo.m3.titre":     { fr: "Valider", en: "Validate", sq: "Vlerëso" },
    "labo.m3.texte":     { fr: "Le vainqueur — et lui seul — passe l’examen des sept derniers jours, que le choix n’a jamais regardés. S’il échoue, pas de repêchage du deuxième : redescendre la liste jusqu’à ce que ça passe reviendrait à tricher à l’examen. Pas de perle = pas de trade.",
                           en: "The winner — and only the winner — takes the exam of the last seven days, which the choice never looked at. If it fails, no second chance for the runner-up: walking down the list until something passes would be cheating on the exam. No pearl = no trade.",
                           sq: "Fituesi — dhe vetëm ai — jep provimin e shtatë ditëve të fundit, të cilat zgjedhja s’i ka parë kurrë. Nëse dështon, s’ka shans të dytë për vendin e dytë: të zbresësh listën derisa diçka të kalojë do të ishte kopjim në provim. S’ka perlë = s’ka tregti." },
    "labo.fen.a":        { fr: "Fenêtre A", en: "Window A", sq: "Dritarja A" },
    "labo.fen.b":        { fr: "Fenêtre B", en: "Window B", sq: "Dritarja B" },
    "labo.fen.jamais":   { fr: "jamais regardée par le choix", en: "never seen by the choice", sq: "e paparë kurrë nga zgjedhja" },
    "labo.deplier":      { fr: "Déplier le détail", en: "Expand details", sq: "Shpalos hollësitë" },
    "labo.histo":        { fr: "Les passes précédentes", en: "Past runs", sq: "Kalimet e mëparshme" },
    "labo.histo.point":  { fr: "{p} : {n} perle{s} en {d}", en: "{p}: {n} pearl{s} in {d}", sq: "{p}: {n} perla për {d}" },
    "labo.perle.explique": { fr: "Positive dans les deux sous-fenêtres, couronnée au winrate, puis confirmée sur les sept jours jamais vus. Les barres doivent se ressembler : c’est la signature d’un motif réel plutôt que d’un coup de chance.",
                           en: "Profitable in both sub-windows, crowned on win rate, then confirmed over the seven unseen days. The bars should look alike: that is the signature of a real pattern rather than a lucky streak.",
                           sq: "Fitimprurëse në të dyja nëndritaret, e kurorëzuar me përqindjen e fitoreve, pastaj e konfirmuar në shtatë ditët e papara. Shtyllat duhet të ngjajnë: kjo është firma e një motivi të vërtetë dhe jo e një rastësie." },
    "labo.finalistes":   { fr: "Le podium du concours", en: "The contest podium", sq: "Podiumi i konkursit" },
    "labo.vainqueur":    { fr: "Le vainqueur du concours", en: "The contest winner", sq: "Fituesi i konkursit" },
    "labo.presque":      { fr: "La plus proche du but", en: "Closest to the bar", sq: "Më e afërta me pragun" },
    "labo.recale":       { fr: "recalé : {v}", en: "failed: {v}", sq: "u rrëzua: {v}" },
    "refus.exp.valid":   { fr: "Gagner sur le passé qu’on a regardé est facile — c’est du par-cœur, pas de la compétence. Le vainqueur passe donc un examen final : les {v} derniers jours, que le choix n’a jamais vus. Il y a échoué — ce qu’il avait « appris » était du bruit, pas un motif. Écarté, sans repêchage : mieux vaut zéro trade qu’un trade fondé sur une illusion.",
                           en: "Winning on the past you looked at is easy — that is memorising, not skill. So the winner takes a final exam: the last {v} days, which the choice never saw. It failed there — what it had “learned” was noise, not a pattern. Rejected, no second chance: better zero trades than a trade built on an illusion.",
                           sq: "Të fitosh mbi të kaluarën që ke parë është e lehtë — është mësim përmendsh, jo aftësi. Prandaj fituesi jep një provim përfundimtar: {v} ditët e fundit, të cilat zgjedhja s’i ka parë kurrë. Aty dështoi — ajo që kishte «mësuar» ishte zhurmë, jo motiv. U skartua, pa shans të dytë: më mirë zero tregti sesa një tregti e ngritur mbi iluzion." },
    "tv.titre":          { fr: "La piste transversale", en: "The cross-sectional lead", sq: "Gjurma tërthore" },
    "tv.explique":       { fr: "Le moteur juge chaque crypto seule. Ce banc en juge trente ensemble : lesquelles feront mieux que les autres ? Le mouvement commun à toute la crypto s’annule entre le côté long et le côté court, et il ne reste que l’écart relatif.", en: "The engine judges each coin alone. This bench judges thirty together: which will do better than the rest? The move common to all of crypto cancels between the long and short sides, leaving only the relative gap.", sq: "Motori gjykon çdo kriptomonedhë veç. Ky stend gjykon tridhjetë bashkë: cilat do të bëjnë më mirë se të tjerat? Lëvizja e përbashkët e gjithë kriptos anulohet mes anës së gjatë dhe të shkurtër, dhe mbetet vetëm hendeku relativ." },
    "tv.preenr":         { fr: "Hypothèse pré-enregistrée le 2 septembre 2026 : <b>{s}</b>, rebalancement toutes les <b>{h} h</b>, cinq longs et cinq courts. Aucun autre signal, aucun autre horizon. Elle sera jugée sur les données à venir, bonnes ou mauvaises.", en: "Hypothesis pre-registered on 2 September 2026: <b>{s}</b>, rebalanced every <b>{h} h</b>, five long and five short. No other signal, no other horizon. It will be judged on data yet to come, good or bad.", sq: "Hipotezë e para-regjistruar më 2 shtator 2026: <b>{s}</b>, ribalancim çdo <b>{h} orë</b>, pesë të gjata dhe pesë të shkurtra. Asnjë sinjal tjetër, asnjë horizont tjetër. Do të gjykohet mbi të dhënat që vijnë, të mira a të këqija." },
    "tv.famille":        { fr: "La meilleure cellule bat {p} % de ce que le hasard produit quand on le laisse chercher aussi librement que nous. Il en faut 95 pour conclure.", en: "The best cell beats {p}% of what chance produces when allowed to search as freely as we did. 95 is needed to conclude.", sq: "Qeliza më e mirë mund {p} % të asaj që prodhon rastësia kur e lëmë të kërkojë po aq lirshëm sa ne. Duhen 95 për të përfunduar." },
    "tv.famille.ok":     { fr: "Le seuil est atteint : la famille bat le hasard.", en: "The threshold is met: the family beats chance.", sq: "Pragu u arrit: familja mund rastësinë." },
    "tv.famille.non":    { fr: "Sous le seuil : rien n’est démontré, et rien n’est branché.", en: "Below the threshold: nothing is demonstrated, and nothing is wired in.", sq: "Nën prag: asgjë nuk është vërtetuar dhe asgjë nuk është lidhur." },
    "tv.periode":        { fr: "{n} instruments, du {a} au {b}", en: "{n} instruments, from {a} to {b}", sq: "{n} instrumente, nga {a} deri {b}" },
    "tv.col.signal":     { fr: "signal", en: "signal", sq: "sinjal" },
    "tv.col.h":          { fr: "h", en: "h", sq: "orë" },
    "tv.col.net":        { fr: "net", en: "net", sq: "neto" },
    "tv.col.brut":       { fr: "brut", en: "gross", sq: "bruto" },
    "tv.col.t":          { fr: "t", en: "t", sq: "t" },
    "tv.col.creux":      { fr: "creux", en: "drawdown", sq: "rënie" },
    "tv.col.nul":        { fr: "hasard", en: "chance", sq: "rastësi" },

    /* Le releve hors echantillon. Le mot important de ce bloc n'est pas
       « net » ni « t » : c'est « bruit ». Tout le reste de la page
       montre des chiffres qu'on peut lire ; celui-ci montre un chiffre
       qu'il est encore interdit de lire, et doit le dire assez fort. */
    "ho.titre":          { fr: "Le relevé hors échantillon", en: "The out-of-sample record", sq: "Regjistrimi jashtë kampionit" },
    "ho.quoi":           { fr: "Classer {n} perpétuels par taux de financement, être long les {k} plus bas et court les {k} plus hauts, rebalancer toutes les {h} heures.", en: "Rank {n} perpetuals by funding rate, go long the {k} lowest and short the {k} highest, rebalance every {h} hours.", sq: "Renditni {n} perpetualë sipas normës së financimit, blini {k} më të ulëtat dhe shisni {k} më të lartat, ribalanconi çdo {h} orë." },
    "ho.compte":         { fr: "{n} périodes de 72 heures accumulées sur les {r} qu’il faut", en: "{n} of the {r} required 72-hour periods accumulated", sq: "{n} nga {r} periudhat 72-orëshe të nevojshme janë grumbulluar" },
    "ho.avant":          { fr: "Rien de lisible avant le {d} environ.", en: "Nothing readable before roughly {d}.", sq: "Asgjë e lexueshme para rreth {d}." },
    "ho.bruit":          { fr: "Le chiffre existe déjà et il est montré nulle part, parce qu’il est du bruit — y compris s’il est beau. C’est écrit d’avance pour que personne ne le lise comme un verdict.", en: "The number already exists and is shown nowhere, because it is noise — however good it looks. This is written in advance so that no one reads it as a verdict.", sq: "Numri ekziston tashmë dhe nuk shfaqet askund, sepse është zhurmë — sado i bukur të duket. Kjo shkruhet paraprakisht që askush të mos e lexojë si verdikt." },
    "ho.note":           { fr: "Un t vaut sharpe × √n. Avec un sharpe par période de {s}, atteindre t = 2 demande {r} périodes. Aucune quantité de calcul ne raccourcit cela : c’est la taille de l’effet qui l’impose.", en: "A t equals sharpe × √n. With a per-period sharpe of {s}, reaching t = 2 requires {r} periods. No amount of computation shortens this: the size of the effect sets it.", sq: "Një t barazon sharpe × √n. Me një sharpe për periudhë prej {s}, arritja e t = 2 kërkon {r} periudha. Asnjë sasi llogaritjeje nuk e shkurton këtë: e përcakton madhësia e efektit." },
    "ho.tient":          { fr: "L’hypothèse tient hors échantillon : t = {t}, net {n}.", en: "The hypothesis holds out of sample: t = {t}, net {n}.", sq: "Hipoteza qëndron jashtë kampionit: t = {t}, neto {n}." },
    "ho.refute":         { fr: "L’hypothèse ne tient pas hors échantillon : t = {t}, net {n}. C’est un résultat, pas un échec.", en: "The hypothesis does not hold out of sample: t = {t}, net {n}. That is a result, not a failure.", sq: "Hipoteza nuk qëndron jashtë kampionit: t = {t}, neto {n}. Ky është një rezultat, jo dështim." },
    "ho.jamais":         { fr: "L’avantage par période est trop petit devant son bruit : aucune durée raisonnable ne le trancherait. Ce n’est pas un défaut de mesure, c’est la réponse — une stratégie pareille n’est pas vérifiable, donc pas jouable.", en: "The per-period edge is too small against its noise: no reasonable span would settle it. That is not a measurement flaw, it is the answer — a strategy like that cannot be verified, so it cannot be played.", sq: "Përparësia për periudhë është shumë e vogël përballë zhurmës së saj: asnjë kohëzgjatje e arsyeshme nuk do ta zgjidhte. Ky nuk është defekt matjeje, është përgjigjja — një strategji e tillë nuk verifikohet, pra nuk luhet." },
    "refus.hasard":      { fr: "battue par le hasard", en: "beaten by chance", sq: "e mundur nga rastësia" },
    "refus.exp.hasard":  { fr: "Cette combinaison a passé les trois seuils, puis a échoué à la seule épreuve qui compte vraiment : faire mieux que la MÊME recherche menée sur le MÊME instrument privé de sa mémoire. On mélange l’ordre des journées — mêmes rendements, mêmes queues, mêmes heures agitées, mais plus rien qui relie un jour au suivant — et l’on relance la recherche. Ce qu’elle trouve alors est ce que le hasard rapporte. Une perle doit battre ce nombre-là, sinon elle n’est que la meilleure de cent cinquante-six tentatives sur les données qui l’ont désignée.",
                           en: "This combination cleared the three thresholds, then failed the only test that really counts: beating the SAME search run on the SAME instrument stripped of its memory. We shuffle the order of the days — same returns, same tails, same busy hours, but nothing linking one day to the next — and run the search again. What it finds then is what chance pays. A pearl has to beat that number, otherwise it is merely the best of a hundred and fifty-six attempts on the very data that picked it.",
                           sq: "Ky kombinim kaloi tre pragjet, pastaj dështoi në provën e vetme që ka vërtet rëndësi: të mundë TË NJËJTËN kërkim mbi TË NJËJTIN instrument pa kujtesën e tij. Përzihet radha e ditëve — të njëjtat kthime, të njëjtat bishta, të njëjtat orë të ngarkuara, por asgjë që lidh një ditë me tjetrën — dhe kërkimi rinis. Ajo që gjen atëherë është sa paguan rastësia. Një perlë duhet ta mundë atë numër, përndryshe është thjesht më e mira nga njëqind e pesëdhjetë e gjashtë përpjekje mbi vetë të dhënat që e zgjodhën." },
    "labo.hasard.titre": { fr: "L’épreuve du hasard", en: "The chance test", sq: "Prova e rastësisë" },
    "labo.hasard.pct":   { fr: "{p}e percentile du hasard", en: "{p}th percentile of chance", sq: "përqindja {p} e rastësisë" },
    "labo.hasard.taux":  { fr: "{t} % des répliques mélangées livrent aussi une perle — sur des données où il n’y a rien à trouver.", en: "{t}% of shuffled replicas also yield a pearl — on data where there is nothing to find.", sq: "{t} % e replikave të përziera japin gjithashtu një perlë — mbi të dhëna ku s’ka asgjë për të gjetur." },
    "labo.hasard.median":{ fr: "gain médian du hasard : {m} de marge par trade", en: "median chance gain: {m} of margin per trade", sq: "fitimi mesatar i rastësisë: {m} e marzhit për tregti" },
    "labo.hasard.exige": { fr: "il faut le {p}e percentile", en: "the {p}th percentile is required", sq: "kërkohet përqindja {p}" },
    "refus.exp.aucune":  { fr: "Sur les 156 combinaisons essayées, aucune n’a été positive dans les deux sous-fenêtres de sélection à la fois. Le hasard réussit parfois une fois ; deux fois de suite, c’est déjà un filtre.",
                           en: "Of the 156 combinations tried, none was profitable in both selection sub-windows at once. Luck sometimes succeeds once; twice in a row is already a filter.",
                           sq: "Nga 156 kombinimet e provuara, asnjëra s’qe fitimprurëse në të dyja nëndritaret e përzgjedhjes njëherësh. Fati ia del ndonjëherë një herë; dy herë radhazi është tashmë një filtër." },
    "refus.exp.courte":  { fr: "L’instrument n’a pas assez d’histoire en chandelles de cinq minutes pour être jugé honnêtement. Il sera rejugé quand l’histoire aura poussé.",
                           en: "The instrument does not have enough five-minute history to be judged honestly. It will be judged again once the history has grown.",
                           sq: "Instrumenti s’ka mjaftueshëm histori me qirinj pesëminutësh për t’u gjykuar ndershëm. Do të rigjykohet kur historia të jetë rritur." },
    "refus.exp.collecte":{ fr: "OKX n’a pas livré l’histoire complète pendant cette passe. L’instrument sera rejugé à la prochaine, dans moins de trente minutes.",
                           en: "OKX did not deliver the full history during this run. The instrument will be judged again on the next one, in under thirty minutes.",
                           sq: "OKX nuk e dorëzoi historinë e plotë gjatë këtij kalimi. Instrumenti do të rigjykohet në të ardhshmin, për më pak se tridhjetë minuta." },
    "porte.trades":      { fr: "trop peu de trades", en: "too few trades", sq: "tepër pak tregti" },
    "porte.negA":        { fr: "négative dans la fenêtre A", en: "loses money in window A", sq: "humbet në dritaren A" },
    "porte.negB":        { fr: "négative dans la fenêtre B", en: "loses money in window B", sq: "humbet në dritaren B" },
    "porte.wr":          { fr: "winrate sous le seuil", en: "win rate below the bar", sq: "përqindje fitoresh nën prag" },
    "porte.moyenne":     { fr: "gain moyen par trade trop faible", en: "average gain per trade too small", sq: "fitim mesatar për tregti tepër i vogël" },
    "porte.negatif":     { fr: "gain net négatif en validation", en: "negative net gain in validation", sq: "fitim neto negativ në vlerësim" },

    /* ===== la santé, en détail ===== */
    "sante.d.wsPublic":  { fr: "Le flux temps réel des prix publics d’OKX.", en: "OKX’s real-time public price feed.", sq: "Rrjedha publike e çmimeve të OKX në kohë reale." },
    "sante.d.wsPrivate": { fr: "Le flux privé du compte : positions, ordres, soldes.", en: "The private account feed: positions, orders, balances.", sq: "Rrjedha private e llogarisë: pozicione, urdhra, gjendje." },
    "sante.d.rest":      { fr: "Les requêtes signées vers OKX : ordres, protections, relevés.", en: "Signed requests to OKX: orders, protections, statements.", sq: "Kërkesat e nënshkruara drejt OKX: urdhra, mbrojtje, pasqyra." },
    "sante.d.dataFlow":  { fr: "La collecte des chandelles qui nourrit les signaux.", en: "The candle collection that feeds the signals.", sq: "Mbledhja e qirinjve që ushqen sinjalet." },
    "sante.d.strategy":  { fr: "Le roster de perles et l’évaluation des signaux.", en: "The pearl roster and signal evaluation.", sq: "Lista e perlave dhe vlerësimi i sinjaleve." },
    "sante.d.aiEngine":  { fr: "La boucle qui décide d’entrer, de tenir ou de sortir.", en: "The loop that decides to enter, hold or exit.", sq: "Cikli që vendos të hyjë, të mbajë a të dalë." },
    "sante.d.orders":    { fr: "La pose et le suivi des ordres réels.", en: "Placement and tracking of live orders.", sq: "Vendosja dhe ndjekja e urdhrave realë." },
    "sante.d.stops":     { fr: "La garde des stops et des take-profits : rien ne reste nu.", en: "The guard of stops and take-profits: nothing stays naked.", sq: "Roja e stopeve dhe e take-profiteve: asgjë s’mbetet zbuluar." },
    "sante.d.portfolio": { fr: "La lecture du compte : équité, marges, positions.", en: "Reading the account: equity, margins, positions.", sq: "Leximi i llogarisë: kapital, marzhe, pozicione." },

    /* ===== les tuiles, en détail ===== */
    "tuile.d.parpos":    { fr: "Par position", en: "Per position", sq: "Sipas pozicionit" },
    "tuile.d.volume":    { fr: "Volume du jour", en: "Today’s volume", sq: "Vëllimi i ditës" },
    "tuile.d.pnljour":   { fr: "PnL du jour", en: "Today’s PnL", sq: "PnL i ditës" },
    "tuile.d.trades24":  { fr: "Trades sur 24 h", en: "Trades in 24h", sq: "Tregti në 24 orë" },
    "tuile.d.dispo":     { fr: "Disponible", en: "Available", sq: "Në dispozicion" },

    /* ===== l'historique des positions ===== */
    "hist.titre":        { fr: "Historique des positions", en: "Position history", sq: "Historiku i pozicioneve" },
    "hist.vide":         { fr: "Aucune position clôturée pour l’instant. Les positions fermées apparaissent ici avec leur résultat réel, frais compris — la vérité d’OKX, pas la nôtre.",
                           en: "No closed positions yet. Closed positions appear here with their real result, fees included — OKX’s truth, not ours.",
                           sq: "Ende asnjë pozicion i mbyllur. Pozicionet e mbyllura shfaqen këtu me rezultatin e tyre real, me tarifat përfshirë — e vërteta e OKX, jo e jona." },
    "hist.ilya":         { fr: "il y a {v}", en: "{v} ago", sq: "para {v}" },
    "hist.fermees":      { fr: "{n} clôturée{s}", en: "{n} closed", sq: "{n} të mbyllura" },
    "hist.gagnees":      { fr: "{g} gagnée{s} sur {n}", en: "{g} won out of {n}", sq: "{g} të fituara nga {n}" },
    "tuile.gain.total":  { fr: "historique : {v} % · {n} trades", en: "all-time: {v}% · {n} trades", sq: "historiku: {v} % · {n} tregti" },
    "tuile.gain.vide24": { fr: "aucune clôture sur 24 h — total affiché", en: "no closes in 24h — all-time shown", sq: "asnjë mbyllje në 24 orë — totali i shfaqur" },
    "tuile.d.wr24":      { fr: "Gagnées sur 24 h", en: "Won in 24h", sq: "Fituar në 24 orë" },
    "tuile.d.wrtotal":   { fr: "Gagnées, tout l’historique", en: "Won, all-time", sq: "Fituar, gjithë historiku" },

    /* ===== le guet : ce que le moteur attend ===== */
    "guet.explique":     { fr: "Une perle retenue est guettée en continu : toutes les vingt secondes, le moteur relit sa bougie de cinq minutes et ouvre la position à l’instant où le signal se déclenche — si une place et du solde libre le permettent. En attendant, elle est à l’affût ; rien d’autre n’est requis.",
                           en: "A retained pearl is watched continuously: every twenty seconds the engine re-reads its five-minute candle and opens the position the instant the signal fires — provided a slot and free balance allow it. Until then it lies in wait; nothing else is required.",
                           sq: "Një perlë e mbajtur vëzhgohet pandërprerë: çdo njëzet sekonda motori rilexon qiririn e saj pesëminutësh dhe hap pozicionin në çastin kur sinjali shkrepet — nëse një vend dhe gjendja e lirë e lejojnë. Deri atëherë ajo rri në pritë; asgjë tjetër s’kërkohet." },
    "guet.affut":        { fr: "À l’affût", en: "On watch", sq: "Në pritë" },
    "guet.enposition":   { fr: "En position", en: "In position", sq: "Në pozicion" },
    "guet.bloquee":      { fr: "bloquée : {v}", en: "blocked: {v}", sq: "e bllokuar: {v}" },
    "guet.bougie":       { fr: "bougie close il y a {v}", en: "candle closed {v} ago", sq: "qiri i mbyllur para {v}" },
    "guet.signal":       { fr: "dernier signal il y a {v}", en: "last signal {v} ago", sq: "sinjali i fundit para {v}" },
    "guet.jamais":       { fr: "aucun signal depuis le guet", en: "no signal since the watch began", sq: "asnjë sinjal që nga fillimi i vëzhgimit" },
    "labo.sens":         { fr: "{l} longs à {wl} % · {s} shorts à {ws} %", en: "{l} longs at {wl}% · {s} shorts at {ws}%", sq: "{l} long me {wl} % · {s} short me {ws} %" },
    "labo.capital":      { fr: "{o}/{p} places · {m} $ par trade", en: "{o}/{p} slots · ${m} per trade", sq: "{o}/{p} vende · {m} $ për tregti" },
    "guet.moteuroff":    { fr: "Moteur à l’arrêt — aucune position ne s’ouvrira", en: "Engine stopped — no position will open", sq: "Motori i ndalur — asnjë pozicion s’do të hapet" },
    "garde.moteur":      { fr: "moteur à l’arrêt", en: "engine stopped", sq: "motori i ndalur" },
    "garde.place":       { fr: "toutes les places sont prises", en: "all slots taken", sq: "të gjitha vendet janë zënë" },
    "garde.budget":      { fr: "budget de marge plein", en: "margin budget full", sq: "buxheti i marzhit plot" },
    "garde.solde":       { fr: "solde libre insuffisant", en: "not enough free balance", sq: "gjendje e lirë e pamjaftueshme" },
    "garde.equite":      { fr: "équité sous le plancher", en: "equity below the floor", sq: "kapitali nën dysheme" },
    "garde.levier":      { fr: "levier requis indisponible", en: "required leverage unavailable", sq: "leva e kërkuar s’ofrohet" },
    "garde.flux":        { fr: "flux de prix gelé", en: "price feed frozen", sq: "rrjedha e çmimeve e ngrirë" },
    "garde.repit":       { fr: "répit après un ordre", en: "cooling down after an order", sq: "pushim pas një urdhri" },

    /* ===== durées ===== */
    "t.jours":           { fr: "{j} j {h} h", en: "{j}d {h}h", sq: "{j} d {h} o" },
    "t.j":               { fr: "{j} j", en: "{j}d", sq: "{j} d" },
    "t.h":               { fr: "{h} h", en: "{h}h", sq: "{h} o" },
    "t.heures":          { fr: "{h} h {m} min", en: "{h}h {m}m", sq: "{h} o {m} min" },
    "t.minutes":         { fr: "{m} min", en: "{m}m", sq: "{m} min" },
    "t.secondes":        { fr: "{s} s", en: "{s}s", sq: "{s} s" },
  };

  const LOCALES = { fr: "fr-FR", en: "en-GB", sq: "sq-AL" };
  const DISPONIBLES = ["fr", "en", "sq"];

  let langue = "fr";
  try {
    const v = localStorage.getItem("hermes-langue");
    if (DISPONIBLES.includes(v)) langue = v;
  } catch {}

  const abonnes = new Set();

  function t(cle, vars) {
    const e = D[cle];
    let s = (e && (e[langue] || e.fr)) || cle;
    if (vars) {
      for (const k in vars) s = s.split("{" + k + "}").join(String(vars[k]));
      // Le pluriel le plus simple qui soit : {s} devient « s » au-delà
      // de un. Suffisant pour les trois langues telles qu’écrites ici.
      if ("n" in vars && !("s" in vars)) s = s.split("{s}").join(Number(vars.n) > 1 ? "s" : "");
    }
    return s;
  }

  /* Les textes ÉCRITS dans la page (data-l, data-l-ph pour un
     placeholder, data-l-title, data-l-aria). Le HTML garde le français
     en dur : c’est ce qu’on voit pendant le chargement du script, et
     c’est la langue par défaut. */
  function appliquer() {
    document.documentElement.lang = langue;
    for (const el of document.querySelectorAll("[data-l]")) el.innerHTML = t(el.dataset.l);
    for (const el of document.querySelectorAll("[data-l-ph]")) el.placeholder = t(el.dataset.lPh);
    for (const el of document.querySelectorAll("[data-l-title]")) el.title = t(el.dataset.lTitle);
    for (const el of document.querySelectorAll("[data-l-aria]")) el.setAttribute("aria-label", t(el.dataset.lAria));
    for (const b of document.querySelectorAll("#nav-langue button")) {
      b.setAttribute("aria-pressed", String(b.dataset.langue === langue));
    }
  }

  function changer(v) {
    if (!DISPONIBLES.includes(v) || v === langue) return;
    langue = v;
    try { localStorage.setItem("hermes-langue", v); } catch {}
    appliquer();
    for (const fn of abonnes) { try { fn(); } catch {} }
  }

  document.addEventListener("click", (e) => {
    const b = e.target.closest && e.target.closest("#nav-langue button[data-langue]");
    if (b) changer(b.dataset.langue);
  });

  appliquer();

  return {
    t,
    locale: () => LOCALES[langue],
    langue: () => langue,
    surChangement: (fn) => { abonnes.add(fn); return () => abonnes.delete(fn); },
  };
})();

const t = Langues.t;
