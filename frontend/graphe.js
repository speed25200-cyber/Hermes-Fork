/* ============================================================
   La vue graphique d'une position.

   Un clic sur une carte ouvre l'instrument en chandelles, avec les
   niveaux de la position posés dessus : entrée, take-profit, stop,
   liquidation, prix. C'est la réponse à une question simple que la
   carte ne peut pas traiter : « où est mon stop PAR RAPPORT au
   marché ? » — une distance ne se lit que sur une échelle.

   Tout le dessin est un seul SVG reconstruit d'un bloc. À l'échelle
   d'un graphe (quelques centaines de nœuds), reconstruire est plus
   simple ET plus sûr que réconcilier : aucun état intermédiaire ne
   peut survivre à tort. L'exception est le réticule, redessiné seul
   au mouvement du pointeur — reconstruire trois cents chandelles à
   chaque millimètre de souris ferait ramer le téléphone qui est
   précisément l'écran principal ici.
   ============================================================ */
"use strict";

const Graphe = (() => {

  const CADRES = ["1m", "5m", "15m", "1H", "4H", "1D"];
  const MIN_VISIBLES = 18;          // en deçà, zoomer n'apprend plus rien

  const G = {
    ouvert: false,
    instId: null,
    bar: "5m",
    rows: [],                        // [ts, o, h, l, c, vol], du plus ancien au plus récent
    a: 0, b: 0,                      // fenêtre visible, indexes fractionnaires
    montre: null,                    // { x, y } du réticule, en px
    minuterie: null,
    geste: null,                     // attente, glisse, pince ou croix
    presse: null,                    // lecheance de lappui long
  };

  const $g = (id) => document.getElementById(id);
  const NS = "http://www.w3.org/2000/svg";
  const el = (nom, attrs) => {
    const n = document.createElementNS(NS, nom);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  };

  /* ===== données ===== */

  function positionCourante() {
    const liste = (E.pf && E.pf.openPositionsDetails) || [];
    return liste.find((p) => p.symbol === G.instId) || null;
  }

  async function charger() {
    try {
      const r = await api.invoke("chandelles", { instId: G.instId, bar: G.bar });
      if (!r || !r.ok || !Array.isArray(r.rows) || !r.rows.length) {
        $g("g-zone").innerHTML = `<div class="attente">${ech(t("gr.echec", { v: r && r.error ? " — " + r.error : "" }))}</div>`;
        return;
      }
      // Pendant un geste ou une inertie, on ne touche a rien : la serie
      // glisse dun cran quand OKX fait tourner sa fenetre de 300, et
      // rebaser les indexes sous le doigt teleporterait le graphe. La
      // prochaine echeance appliquera les donnees, main levee.
      if (G.geste || G.enInertie()) return;
      const colle = G.rows.length && G.b >= G.rows.length - 1.5;   // l'œil était au bord droit
      const memesBornes = G.rows.length && G.rows[0][0] === r.rows[0][0];
      G.rows = r.rows;
      if (!memesBornes || colle || G.b === 0) {
        G.b = G.rows.length;
        G.a = Math.max(0, G.b - 120);
      } else {
        G.b = Math.min(G.b, G.rows.length);
        G.a = Math.max(0, Math.min(G.a, G.b - MIN_VISIBLES));
      }
      dessiner();
    } catch (e) {
      $g("g-zone").innerHTML = `<div class="attente">${ech(t("gr.injoignable"))}</div>`;
    }
  }

  /* ===== échelles ===== */

  // Des graduations « rondes » : 1, 2 ou 5 fois une puissance de dix.
  // Un axe gradué à 0,0371 / 0,0446 / 0,0521 se lit comme un code ;
  // à 0,038 / 0,040 / 0,042 il se lit comme un prix.
  function graduations(min, max, cible) {
    const brut = (max - min) / Math.max(1, cible);
    const p = Math.pow(10, Math.floor(Math.log10(brut)));
    const pas = [1, 2, 5, 10].map((m) => m * p).find((v) => v >= brut) || 10 * p;
    const debut = Math.ceil(min / pas) * pas;
    const out = [];
    for (let v = debut; v <= max + pas * 1e-9; v += pas) out.push(v);
    return out;
  }

  function heure(ts) {
    const d = new Date(ts);
    const hm = String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
    if (G.bar === "1D") return d.toLocaleDateString(Langues.locale(), { day: "2-digit", month: "short" });
    return hm;
  }
  function heurePleine(ts) {
    const d = new Date(ts);
    return d.toLocaleDateString(Langues.locale(), { day: "2-digit", month: "short" }) + " " +
           String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }

  /* ===== le dessin ===== */

  function dessiner() {
    const zone = $g("g-zone");
    const W = zone.clientWidth, H = zone.clientHeight;
    if (!W || !H || !G.rows.length) return;

    const AXE_D = 74, AXE_B = 22, HAUT = 10;
    const pw = W - AXE_D, ph = H - AXE_B - HAUT;
    const volH = Math.round(ph * 0.14);

    const i0 = Math.max(0, Math.floor(G.a)), i1 = Math.min(G.rows.length, Math.ceil(G.b));
    const visibles = G.rows.slice(i0, i1);
    if (!visibles.length) return;

    const pos = positionCourante();
    const dernier = G.rows[G.rows.length - 1];
    const prixCourant = (pos && pos.markPrice) || dernier[4];

    // Le domaine vertical : ce que le marché a fait, PLUS les niveaux
    // de la position — un take-profit hors cadre serait précisément ce
    // que cette vue existe pour montrer. La liquidation, souvent très
    // loin, n'étire l'échelle que si elle est raisonnablement proche.
    let bas = Infinity, hautP = -Infinity;
    for (const k of visibles) { if (k[3] < bas) bas = k[3]; if (k[2] > hautP) hautP = k[2]; }
    const etendue0 = hautP - bas || bas * 0.01 || 1;
    const niveaux = [];
    if (pos) {
      if (pos.entryPrice > 0) niveaux.push(pos.entryPrice);
      if (pos.takeProfit > 0) niveaux.push(pos.takeProfit);
      if (pos.stopActuel > 0) niveaux.push(pos.stopActuel);
      if (pos.liqPrice > 0 && Math.abs(pos.liqPrice - prixCourant) < etendue0 * 1.6) niveaux.push(pos.liqPrice);
    }
    for (const v of niveaux) { if (v < bas) bas = v; if (v > hautP) hautP = v; }
    const marge = (hautP - bas) * 0.07 || bas * 0.004 || 1;
    bas -= marge; hautP += marge;

    let volMax = 0;
    for (const k of visibles) if (k[5] > volMax) volMax = k[5];
    volMax = volMax || 1;
    const cw = pw / Math.max(1e-9, G.b - G.a);
    const corps = Math.max(1, Math.min(13, cw * 0.62));

    const x = (i) => (i + 0.5 - G.a) * cw;
    const y = (v) => HAUT + (1 - (v - bas) / (hautP - bas)) * (ph - volH - 6);

    const style = getComputedStyle(document.documentElement);
    const C = (n) => style.getPropertyValue(n).trim();
    const cGain = C("--gain"), cPerte = C("--perte"), cBord = C("--bord"),
          cT2 = C("--texte-2"), cT3 = C("--texte-3"), cBon = C("--bon"),
          cCrit = C("--critique"), cSurface = C("--surface");

    /* Le dessin est assemblé en TEXTE, puis remis au navigateur en un
       seul morceau — et surtout, tout ce qui se répète par chandelle
       est FUSIONNÉ : les trois cents mèches vertes ne font qu'un seul
       path, les corps rouges un autre, les volumes deux de plus. Un
       cadre passe ainsi d'environ neuf cents nœuds à une vingtaine.
       C'est la différence entre un pincement qui suit le doigt et un
       pincement qui rame : le téléphone ne rend pas neuf cents objets
       en huit millisecondes, il en rend vingt sans y penser. */
    const morceaux = [];
    const texte = (tx, ty, contenu, fill, ancre, gras) =>
      `<text x="${tx}" y="${ty}" fill="${fill}" font-size="10"${gras ? ' font-weight="600"' : ""}` +
      `${ancre ? ` text-anchor="${ancre}"` : ""} font-family="var(--num)">${contenu}</text>`;

    /* — grille + graduations — */
    let dGrille = "";
    const gradsY = graduations(bas, hautP, Math.max(3, Math.round(ph / 64)));
    for (const v of gradsY) {
      const yy = y(v).toFixed(1);
      dGrille += `M0 ${yy}H${pw}`;
      morceaux.push(texte(pw + 8, (y(v) + 3.5).toFixed(1), prix(v), cT3));
    }
    const BAR_MS = { "1m": 60e3, "5m": 300e3, "15m": 900e3, "1H": 3600e3, "4H": 14400e3, "1D": 86400e3 };
    const RONDS = [60e3, 300e3, 900e3, 1800e3, 3600e3, 7200e3, 14400e3, 43200e3, 86400e3, 172800e3];
    const besoin = (92 / cw) * (BAR_MS[G.bar] || 300e3);
    const rond = RONDS.find((v) => v >= besoin) || RONDS[RONDS.length - 1];
    const decalage = new Date(G.rows[0][0]).getTimezoneOffset() * 60e3;
    const barMs = BAR_MS[G.bar] || 300e3;
    for (let i = i0; i < i1; i++) {
      const r = (((G.rows[i][0] - decalage) % rond) + rond) % rond;
      if (r >= barMs) continue;
      const px = x(i).toFixed(1);
      dGrille += `M${px} ${HAUT}V${HAUT + ph}`;
      morceaux.push(texte(px, H - 7, heure(G.rows[i][0]), cT3, "middle"));
    }
    morceaux.unshift(`<path data-role="grille" d="${dGrille}" stroke="${cBord}" stroke-opacity=".4" fill="none" shape-rendering="crispEdges"/>`);

    /* — volumes puis chandelles, en quatre chemins par couleur — */
    let volG = "", volP = "", mecheG = "", mecheP = "", corpsG = "", corpsP = "";
    const demiC = corps / 2;
    for (let i = i0; i < i1; i++) {
      const k = G.rows[i];
      const monte = k[4] >= k[1];
      const cx = x(i);
      const vh = Math.max(1, (k[5] / volMax) * volH);
      const rectVol = `M${(cx - demiC).toFixed(1)} ${(HAUT + ph - vh).toFixed(1)}h${corps.toFixed(1)}v${vh.toFixed(1)}h${(-corps).toFixed(1)}Z`;
      const meche = `M${cx.toFixed(1)} ${y(k[2]).toFixed(1)}V${y(k[3]).toFixed(1)}`;
      const yO = y(k[1]), yC = y(k[4]);
      const rectCorps = `M${(cx - demiC).toFixed(1)} ${Math.min(yO, yC).toFixed(1)}h${corps.toFixed(1)}v${Math.max(1, Math.abs(yO - yC)).toFixed(1)}h${(-corps).toFixed(1)}Z`;
      if (monte) { volG += rectVol; mecheG += meche; corpsG += rectCorps; }
      else       { volP += rectVol; mecheP += meche; corpsP += rectCorps; }
    }
    const eMeche = Math.max(1, corps * 0.14).toFixed(1);
    if (volG) morceaux.push(`<path data-role="vol" d="${volG}" fill="${cGain}" fill-opacity=".16"/>`);
    if (volP) morceaux.push(`<path data-role="vol" d="${volP}" fill="${cPerte}" fill-opacity=".16"/>`);
    if (mecheG) morceaux.push(`<path data-role="meche" d="${mecheG}" stroke="${cGain}" stroke-width="${eMeche}" fill="none"/>`);
    if (mecheP) morceaux.push(`<path data-role="meche" d="${mecheP}" stroke="${cPerte}" stroke-width="${eMeche}" fill="none"/>`);
    if (corpsG) morceaux.push(`<path data-role="corps" d="${corpsG}" fill="${cGain}"/>`);
    if (corpsP) morceaux.push(`<path data-role="corps" d="${corpsP}" fill="${cPerte}"/>`);

    /* — les niveaux de la position — */
    const ligne = (v, couleur, etiq, pointille) => {
      if (!(v > 0) || v < bas || v > hautP) return;
      const py = y(v).toFixed(1);
      morceaux.push(`<line x1="0" x2="${pw}" y1="${py}" y2="${py}" stroke="${couleur}" stroke-width="1.2"` +
        (pointille ? ` stroke-dasharray="${pointille}"` : "") + `/>`);
      const larg = Math.min(72, Math.max(58, etiq.length * 6.4 + 10));
      morceaux.push(`<rect x="${pw + 1}" y="${(y(v) - 9).toFixed(1)}" width="${larg}" height="18" rx="4" fill="${couleur}"/>`);
      morceaux.push(texte(pw + 1 + larg / 2, (y(v) + 3.6).toFixed(1), etiq, cSurface, "middle", true));
    };

    if (pos) {
      const verrou = pos.stopActuel > 0 && pos.entryPrice > 0 &&
        (pos.side === "LONG" ? pos.stopActuel >= pos.entryPrice : pos.stopActuel <= pos.entryPrice);
      ligne(pos.entryPrice, cT2, t("gr.entree.tag"), "5 4");
      ligne(pos.takeProfit, cBon, "TP");
      ligne(pos.stopActuel, verrou ? cBon : cCrit, pos.stopMode === "TRAIL" ? "TRAIL" : verrou ? t("gr.seuil.tag") : "SL");
      ligne(pos.liqPrice, cPerte, "LIQ", "2 4");
    }
    const cPrix = dernier[4] >= dernier[1] ? cGain : cPerte;
    ligne(prixCourant, cPrix, prix(prixCourant), "1.5 3");

    morceaux.push(`<g id="g-croix" pointer-events="none"></g>`);

    zone.innerHTML = `<svg viewBox="0 0 ${W} ${H}" width="${W}" height="${H}">${morceaux.join("")}</svg>`;
    G._dims = { W, H, pw, ph, HAUT, AXE_D, y, x, bas, hautP, cw, i0, i1 };

    /* Sous le doigt, SEUL le dessin bouge. La tête, la lecture OHLC et
       les niveaux du pied ne changent pas pendant un geste — la
       position est la même — mais les reconstruire à chaque image
       relançait la mise en page du panneau entier, et c'est elle qui
       mangeait le budget, pas les chandelles. Ils seront remis à jour
       au premier dessin posé, main levée. */
    const enGeste = !!G.geste || G.enInertie();
    if (!enGeste) {
      lecture(null);
      niveauxPied(pos, prixCourant);
      entete(pos);
    }
    if (G.montre) reticule(G.montre.x, G.montre.y);
  }

  /* La ligne OHLC au-dessus du dessin. Sans réticule elle décrit la
     dernière chandelle ; sous le réticule, celle qu'on survole. */
  function lecture(i) {
    const zone = $g("g-ohlc");
    const k = (i != null && G.rows[i]) || G.rows[G.rows.length - 1];
    if (!k) { zone.innerHTML = ""; return; }
    const monte = k[4] >= k[1];
    const cls = monte ? "gain" : "perte";
    zone.innerHTML =
      `<span class="et">${ech(heurePleine(k[0]))}</span>` +
      `<span class="${cls}"><span class="et">${ech(t("gr.o"))}</span> <b>${prix(k[1])}</b></span>` +
      `<span class="${cls}"><span class="et">${ech(t("gr.h"))}</span> <b>${prix(k[2])}</b></span>` +
      `<span class="${cls}"><span class="et">${ech(t("gr.b"))}</span> <b>${prix(k[3])}</b></span>` +
      `<span class="${cls}"><span class="et">${ech(t("gr.c"))}</span> <b>${prix(k[4])}</b></span>` +
      `<span><span class="et">${ech(t("gr.vol"))}</span> <b>${taille(k[5])}</b></span>`;
  }

  function reticule(px, py) {
    const d = G._dims;
    if (!d) return;
    const g = document.getElementById("g-croix");
    if (!g) return;
    while (g.firstChild) g.removeChild(g.firstChild);
    if (px == null) { lecture(null); return; }

    const i = Math.round(px / d.cw + G.a - 0.5);
    if (i < 0 || i >= G.rows.length) { lecture(null); return; }
    const cx = d.x(i);

    g.appendChild(el("line", { x1: cx.toFixed(1), x2: cx.toFixed(1), y1: d.HAUT, y2: d.HAUT + d.ph,
      stroke: "currentColor", "stroke-opacity": ".35", "stroke-dasharray": "3 3" }));
    if (py >= d.HAUT && py <= d.HAUT + d.ph) {
      g.appendChild(el("line", { x1: 0, x2: d.pw, y1: py.toFixed(1), y2: py.toFixed(1),
        stroke: "currentColor", "stroke-opacity": ".35", "stroke-dasharray": "3 3" }));
      const v = d.bas + (1 - (py - d.HAUT) / (d.ph - Math.round(d.ph * 0.14) - 6)) * (d.hautP - d.bas);
      const texte = prix(v);
      g.appendChild(el("rect", {
        x: d.pw + 1, y: (py - 9).toFixed(1),
        width: Math.min(72, texte.length * 6.4 + 10), height: 18, rx: 4,
        fill: "var(--texte)", "fill-opacity": ".92"
      }));
      const t = el("text", { x: d.pw + 1 + Math.min(72, texte.length * 6.4 + 10) / 2, y: (py + 3.6).toFixed(1),
        fill: "var(--surface)", "font-size": 10, "font-weight": 600,
        "text-anchor": "middle", "font-family": "var(--num)" });
      t.textContent = texte;
      g.appendChild(t);
    }
    lecture(i);
  }

  /* ===== la tête et le pied ===== */

  function entete(pos) {
    const long = pos ? pos.side === "LONG" : true;
    const sens = $g("g-sens");
    sens.className = "sens " + (long ? "long" : "short");
    sens.textContent = pos ? (long ? "LONG" : "SHORT") : "";
    sens.hidden = !pos;
    $g("g-lev").textContent = pos ? "×" + (pos.leverage || "?") : "";
    $g("g-sym").textContent = court(G.instId);
    const u = $g("g-pnl"), p = $g("g-pct");
    if (pos) {
      const pnl = Number(pos.unrealizedPnl) || 0;
      u.className = "u " + signe(pnl);
      u.textContent = usd(pnl);
      p.className = "p " + signe(pnl);
      p.textContent = t("pos.delamarge", { v: pct(Number(pos.pnlPctOfMargin) || 0) });
    } else { u.textContent = ""; p.textContent = t("gr.fermee"); p.className = "p"; }
  }

  function niveauxPied(pos, prixCourant) {
    const zone = $g("g-niveaux");
    if (!pos) { zone.innerHTML = `<span class="g-niv">${ech(t("gr.plusouverte"))}</span>`; return; }
    const d = (v) => (pos.entryPrice > 0 && v > 0)
      ? " · " + (((v - pos.entryPrice) / pos.entryPrice) * 100).toLocaleString(Langues.locale(), { maximumFractionDigits: 2, minimumFractionDigits: 2 }) + " %"
      : "";
    const verrou = pos.stopActuel > 0 && pos.entryPrice > 0 &&
      (pos.side === "LONG" ? pos.stopActuel >= pos.entryPrice : pos.stopActuel <= pos.entryPrice);
    const morceaux = [];
    const niv = (etiq, v, couleur, pointille, note) => {
      if (!(v > 0)) return;
      morceaux.push(`<span class="g-niv${pointille ? " pointille" : ""}" style="--c:${couleur}">
        <i></i>${etiq} <b>${prix(v)}</b>${note || ""}</span>`);
    };
    niv(ech(t("gr.niv.entree")), pos.entryPrice, "var(--texte-2)", true);
    niv(ech(t("gr.niv.tp")), pos.takeProfit, "var(--bon)", false, d(pos.takeProfit));
    niv(ech(pos.stopMode === "TRAIL" ? t("gr.niv.trail") : verrou ? t("gr.niv.stopverrou") : t("gr.niv.stop")),
        pos.stopActuel, verrou ? "var(--bon)" : "var(--critique)", false, d(pos.stopActuel));
    niv(ech(t("gr.niv.liq")), pos.liqPrice, "var(--perte)", true, d(pos.liqPrice));
    niv(ech(t("gr.niv.prix")), prixCourant, "var(--texte)", false, d(prixCourant));
    morceaux.push(`<span class="g-niv" style="opacity:.75">${ech(t("gr.niv.tenue"))} <b>${duree(pos.entryTime)}</b></span>`);
    zone.innerHTML = morceaux.join("");
  }

  /* ===== la cadence ===== */

  /* Un seul dessin par image décran. Pendant un glissement, le doigt
     émet bien plus dévénements que lécran naffiche dimages :
     reconstruire le SVG à chacun, cest dessiner des cadres que
     personne ne verra et faire ramer précisément le téléphone quon
     vise. On note quun dessin est dû, et requestAnimationFrame le
     paie une fois. */
  let cadreDu = false;
  function planifier() {
    if (cadreDu) return;
    cadreDu = true;
    requestAnimationFrame(() => { cadreDu = false; dessiner(); });
  }

  /* ===== gestes : molette, glisser, pincer ===== */

  function borner() {
    const n = G.rows.length;
    const larg = Math.max(MIN_VISIBLES, Math.min(n, G.b - G.a));
    G.a = Math.max(-larg * 0.15, Math.min(G.a, n - larg * 0.5));
    G.b = G.a + larg;
    if (G.b > n + larg * 0.15) { G.b = n + larg * 0.15; G.a = G.b - larg; }
  }

  function zoomer(facteur, fx) {
    const pivot = G.a + fx * (G.b - G.a);
    G.a = pivot - (pivot - G.a) * facteur;
    G.b = pivot + (G.b - pivot) * facteur;
    borner(); planifier();
  }

  const doigts = new Map();

  /* Au doigt, trois intentions se partagent le meme contact, et on ne
     sait laquelle quen le regardant vivre :

       bouger vite         -> panoramique, puis inertie au lacher
       rester appuye       -> reticule, qui suit le doigt (appui long)
       toucher et relacher -> reticule pose sur la chandelle touchee

     A la souris cest plus simple : survoler promene le reticule,
     glisser deplace, la molette zoome. */
  const SEUIL_GLISSE = 7;        // px avant de trancher pour le panoramique
  const APPUI_LONG_MS = 240;

  let inertieId = null;
  function arreterInertie() { if (inertieId) { cancelAnimationFrame(inertieId); inertieId = null; } }
  G.enInertie = () => !!inertieId;

  /* Linertie : le graphe continue sur la lancee du doigt puis se pose.
     Cest elle qui fait la difference entre « ca bouge » et « cest
     fluide » — un arret net au lacher rend chaque geste sec. La
     vitesse decroit dun facteur constant par image, et on sarrete
     quand le mouvement passe sous le dixieme de pixel. */
  function lancerInertie(vitesse) {
    arreterInertie();
    if (!Number.isFinite(vitesse) || Math.abs(vitesse * (G._dims?.cw || 1)) < 0.35) return;
    let v = vitesse, avant = performance.now();
    const pas = (t) => {
      const dt = Math.min(48, t - avant); avant = t;
      G.a -= v * dt; G.b -= v * dt;
      v *= Math.pow(0.94, dt / 16.7);
      borner(); planifier();
      if (Math.abs(v * dt * (G._dims?.cw || 1)) > 0.1) inertieId = requestAnimationFrame(pas);
      else { inertieId = null; planifier(); }   // le dessin complet, une fois posee
    };
    inertieId = requestAnimationFrame(pas);
  }

  function brancherGestes(zone) {
    zone.addEventListener("wheel", (e) => {
      e.preventDefault();
      arreterInertie();
      const r = zone.getBoundingClientRect();
      zoomer(Math.exp(e.deltaY * 0.0016), (e.clientX - r.left) / Math.max(1, (G._dims?.pw || r.width)));
    }, { passive: false });

    zone.addEventListener("pointerdown", (e) => {
      // La capture peut lever (pointeur deja parti, evenement de
      // synthese) ; la perdre coute un geste, la laisser lever coute
      // le gestionnaire entier.
      try { zone.setPointerCapture(e.pointerId); } catch {}
      arreterInertie();
      doigts.set(e.pointerId, { x: e.clientX, y: e.clientY });
      clearTimeout(G.presse);

      if (doigts.size === 2) {
        const [p1, p2] = [...doigts.values()];
        const r = zone.getBoundingClientRect();
        /* Trois choix qui font la difference entre un pincement quon
           subit et un pincement quon ne remarque pas :

           La distance est EUCLIDIENNE. Mesuree sur le seul axe X, deux
           doigts un peu verticaux — pouce et index dune main — donnent
           un ecart proche de zero et des facteurs de zoom erratiques.
           Un plancher borne ce que la division peut produire.

           Lancre est le MILIEU DES DOIGTS, pas le centre de la
           fenetre : on zoome sur ce quon pince, sinon le contenu fuit
           lendroit meme quon designe.

           Et le milieu est relu A CHAQUE mouvement : deplacer les deux
           doigts pendant le pincement deplace aussi la fenetre. Un
           vrai geste fait toujours les deux a la fois ; les separer
           oblige a deux gestes la ou la main nen fait quun. */
        const ecart0 = Math.max(30, Math.hypot(p1.x - p2.x, p1.y - p2.y));
        const mid0 = ((p1.x + p2.x) / 2 - r.left) / Math.max(1, G._dims?.pw || r.width);
        G.geste = {
          type: "pince", ecart0,
          pivot: G.a + Math.min(1, Math.max(0, mid0)) * (G.b - G.a),
          larg0: G.b - G.a,
        };
        G.montre = null; reticule(null);   // le reticule na rien a faire sous un pincement
        return;
      }

      G.geste = { type: "attente", x0: e.clientX, y0: e.clientY, t0: performance.now(),
                  x: e.clientX, a: G.a, b: G.b, vitesse: 0, tactile: e.pointerType !== "mouse" };
      if (G.geste.tactile) {
        // Lappui long : si le doigt na pas boug avant lecheance,
        // cest le reticule quil demande, pas un deplacement.
        G.presse = setTimeout(() => {
          if (G.geste && G.geste.type === "attente") {
            G.geste.type = "croix";
            const r = zone.getBoundingClientRect();
            G.montre = { x: e.clientX - r.left, y: e.clientY - r.top };
            reticule(G.montre.x, G.montre.y);
          }
        }, APPUI_LONG_MS);
      }
    });

    zone.addEventListener("pointermove", (e) => {
      const r = zone.getBoundingClientRect();
      const px = e.clientX - r.left, py = e.clientY - r.top;
      if (doigts.has(e.pointerId)) doigts.set(e.pointerId, { x: e.clientX, y: e.clientY });

      if (G.geste && G.geste.type === "pince" && doigts.size === 2) {
        const [p1, p2] = [...doigts.values()];
        const ecart = Math.max(30, Math.hypot(p1.x - p2.x, p1.y - p2.y));
        const larg = G.geste.larg0 * (G.geste.ecart0 / ecart);
        const f = Math.min(1, Math.max(0, ((p1.x + p2.x) / 2 - r.left) / Math.max(1, G._dims?.pw || 1)));
        // La chandelle qui etait sous le milieu des doigts au depart
        // reste sous le milieu des doigts : cest toute la regle.
        G.a = G.geste.pivot - f * larg;
        G.b = G.a + larg;
        borner(); planifier();
        return;
      }

      if (G.geste && G.geste.type === "croix") {
        G.montre = { x: px, y: py };
        reticule(px, py);
        return;
      }

      if (G.geste && (G.geste.type === "attente" || G.geste.type === "glisse")) {
        if (G.geste.type === "attente") {
          if (Math.hypot(e.clientX - G.geste.x0, e.clientY - G.geste.y0) < SEUIL_GLISSE) return;
          clearTimeout(G.presse);          // le doigt bouge : ce nest pas un appui long
          G.geste.type = "glisse";
          G.montre = null; reticule(null);   // un reticule fige sous un panoramique ment
        }
        const maintenant = performance.now();
        const dxTotal = e.clientX - G.geste.x0;
        const di = dxTotal / Math.max(1e-9, G._dims?.cw || 1);
        // La vitesse est lissee : un seul echantillon nerveux au moment
        // du lacher enverrait le graphe a lautre bout de la serie.
        const dt = Math.max(1, maintenant - (G.geste.t || G.geste.t0));
        const vInst = (e.clientX - (G.geste.x ?? G.geste.x0)) / dt / Math.max(1e-9, G._dims?.cw || 1);
        G.geste.vitesse = 0.75 * (G.geste.vitesse || 0) + 0.25 * vInst;
        G.geste.x = e.clientX; G.geste.t = maintenant;
        G.a = G.geste.a - di; G.b = G.geste.b - di;
        borner(); planifier();
        return;
      }

      // Souris libre : le reticule suit le survol.
      if (e.pointerType === "mouse" && !doigts.size) {
        G.montre = { x: px, y: py };
        reticule(px, py);
      }
    });

    const finDoigt = (e) => {
      clearTimeout(G.presse);
      const geste = G.geste;
      doigts.delete(e.pointerId);

      if (!doigts.size) {
        G.geste = null;
        planifier();                        // le dessin pose, complet, main levee
        if (geste && geste.type === "glisse") lancerInertie(geste.vitesse || 0);
        else if (geste && geste.type === "attente" && geste.tactile &&
                 performance.now() - geste.t0 < 260) {
          // Un toucher bref : le reticule se pose la, et y reste — sur
          // telephone il ny a pas de survol pour le faire vivre.
          const r = zone.getBoundingClientRect();
          G.montre = { x: e.clientX - r.left, y: e.clientY - r.top };
          reticule(G.montre.x, G.montre.y);
        }
      } else if (doigts.size === 1) {
        const [p] = [...doigts.values()];
        G.geste = { type: "glisse", x0: p.x, y0: p.y, t0: performance.now(), x: p.x, a: G.a, b: G.b, vitesse: 0, tactile: true };
      }
    };
    zone.addEventListener("pointerup", finDoigt);
    zone.addEventListener("pointercancel", finDoigt);
    zone.addEventListener("pointerleave", (e) => {
      if (e.pointerType === "mouse" && !doigts.size) { G.montre = null; reticule(null); }
    });
    zone.addEventListener("dblclick", () => { arreterInertie(); G.b = G.rows.length; G.a = Math.max(0, G.b - 120); planifier(); });
  }

  /* ===== ouverture, fermeture, cycle de vie ===== */

  function cadres() {
    const zone = $g("g-cadres");
    zone.innerHTML = CADRES.map((c) =>
      `<button role="tab" data-bar="${c}" aria-pressed="${c === G.bar}">${c}</button>`).join("");
    zone.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
      if (b.dataset.bar === G.bar) return;
      G.bar = b.dataset.bar;
      G.rows = []; G.a = 0; G.b = 0;
      cadres();
      $g("g-zone").innerHTML = `<div class="attente">${ech(t("gr.chargement"))}</div>`;
      charger();
    }));
  }

  function ouvrir(instId) {
    G.instId = instId; G.ouvert = true;
    G.rows = []; G.a = 0; G.b = 0; G.montre = null;
    const voile = $g("g-voile");
    voile.hidden = false;
    requestAnimationFrame(() => voile.classList.add("ouvert"));
    document.body.style.overflow = "hidden";
    cadres();
    entete(positionCourante());
    $g("g-zone").innerHTML = `<div class="attente">${ech(t("gr.chargement"))}</div>`;
    $g("g-niveaux").innerHTML = "";
    $g("g-ohlc").innerHTML = "";
    charger();
    // Les chandelles se rafraîchissent d'elles-mêmes : le cache serveur
    // absorbe l'empressement, la page n'a qu'à demander.
    clearInterval(G.minuterie);
    G.minuterie = setInterval(() => { if (G.ouvert) charger(); }, 15000);
    $g("g-fermer").focus();
  }

  function fermer() {
    G.ouvert = false;
    clearInterval(G.minuterie);
    clearTimeout(G.presse);
    arreterInertie();
    G.geste = null; doigts.clear();
    const voile = $g("g-voile");
    voile.classList.remove("ouvert");
    document.body.style.overflow = "";
    setTimeout(() => { if (!G.ouvert) voile.hidden = true; }, 320);
  }

  /* Appelé par la boucle de la page à chaque relevé : la tête, les
     niveaux et la ligne de prix restent vivants sans refaire un
     aller-retour chandelles. */
  function battement() {
    if (!G.ouvert || !G.rows.length) return;
    if (G.geste || G.enInertie()) return;   // jamais sous le doigt
    planifier();
  }

  function brancher() {
    $g("g-fermer").addEventListener("click", fermer);
    $g("g-voile").addEventListener("click", (e) => { if (e.target === $g("g-voile")) fermer(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && G.ouvert) fermer(); });

    // La délégation : les cartes vivent et meurent au fil des relevés,
    // un écouteur par carte mourrait avec elle.
    document.addEventListener("click", (e) => {
      const carte = e.target.closest && e.target.closest(".pos[data-sym], #z-tab tbody tr[data-sym]");
      if (carte) ouvrir(carte.dataset.sym);
    });

    brancherGestes($g("g-zone"));
    new ResizeObserver(() => { if (G.ouvert && G.rows.length) dessiner(); }).observe($g("g-zone"));
  }

  brancher();
  return { ouvrir, fermer, battement, estOuvert: () => G.ouvert };
})();
