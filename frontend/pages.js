"use strict";
/* ============================================================
   Les quatre vues ajoutées par la plateforme quantitative :
   Décisions, Recherche, JEV, Risque/exploitation.

   Elles remplacent l'onglet Laboratoire d'Hermes et gardent EXACTEMENT son langage visuel :
   mêmes blocs, mêmes tuiles, mêmes jetons de couleur, mêmes animations d'entrée.

   Deux règles tiennent ce fichier, et elles ne sont pas décoratives.

   La première : une métrique inconnue s'écrit « Non disponible », jamais zéro. Un zéro se lit comme
   une mesure ; une absence n'en est pas une, et la confondre avec 0 % de taux de gain ou 0 USDT de
   perte fait prendre des décisions sur du vide.

   La seconde : une commande critique affiche le COMPTE et le MODE dans sa confirmation, et son suivi
   ne disparaît pas parce que le serveur a répondu. Une demande acceptée n'est pas une action
   effectuée ; l'écran doit montrer le résultat observé, résidus compris.
   ============================================================ */

const Pages = (() => {
  const $p = (id) => document.getElementById(id);
  const ech = (s) => String(s == null ? "" : s)
    .replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* Un nombre absent reste absent. */
  const ND = () => `<span class="indispo">${ech(t("indispo"))}</span>`;
  const num = (v, d = 2) => (v === null || v === undefined || v === "" || Number.isNaN(Number(v)))
    ? ND()
    : Number(v).toLocaleString(Langues.locale(), { minimumFractionDigits: d, maximumFractionDigits: d });
  const pct = (v, d = 2) => (v === null || v === undefined || v === "") ? ND() : num(Number(v) * 100, d) + " %";
  const txt = (v) => (v === null || v === undefined || v === "") ? ND() : ech(v);
  const quand = (iso) => {
    if (!iso) return ND();
    const d = new Date(iso);
    if (isNaN(d)) return ND();
    return ech(d.toLocaleString(Langues.locale(), { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit", second: "2-digit" }));
  };
  const vide = (cle) => `<div class="vide" style="padding:26px">${ech(t(cle))}</div>`;
  const table = (entetes, lignes) => lignes.length
    ? `<div class="roule-x"><table class="tableau-simple"><thead><tr>${
        entetes.map((h) => `<th>${ech(h)}</th>`).join("")}</tr></thead><tbody>${lignes.join("")}</tbody></table></div>`
    : "";
  const puce = (texte, classe) => `<span class="puce ${classe || ""}">${ech(texte)}</span>`;

  /* ===== navigation ===== */

  const PAGES = [
    { id: "marche", nav: "nav-marche", page: null },
    { id: "decisions", nav: "nav-decisions", page: "page-decisions", charge: () => chargerDecisions() },
    { id: "recherche", nav: "nav-recherche", page: "page-recherche", charge: () => chargerRecherche() },
    { id: "jev", nav: "nav-jev", page: "page-jev", charge: () => chargerJev() },
    { id: "risque", nav: "nav-risque", page: "page-risque", charge: () => chargerRisque() },
  ];
  let courante = "marche";
  let minuterie = null;

  function pageMarche() {
    return document.querySelector(".page:not([id^='page-'])");
  }

  function montrer(id) {
    courante = id;
    const marche = pageMarche();
    if (marche) marche.hidden = id !== "marche";
    for (const p of PAGES) {
      if (p.page) { const el = $p(p.page); if (el) el.hidden = p.id !== id; }
      const bouton = $p(p.nav);
      if (bouton) bouton.setAttribute("aria-pressed", String(p.id === id));
    }
    try { localStorage.setItem("okxq-page", id); } catch {}
    const cible = PAGES.find((p) => p.id === id);
    if (cible && cible.charge) cible.charge();
    clearInterval(minuterie);
    // Les pages de lecture se rafraîchissent plus lentement que le marché : elles ne changent pas
    // à la seconde, et marteler l'API pour rien coûte de la base de données.
    minuterie = setInterval(() => {
      const c = PAGES.find((p) => p.id === courante);
      if (c && c.charge) c.charge();
    }, 15000);
  }

  for (const p of PAGES) {
    const bouton = $p(p.nav);
    if (bouton) bouton.addEventListener("click", () => montrer(p.id));
  }

  /* ===== en-tête : mode et nature des données ===== */

  function majEntete(etat) {
    const jeton = $p("j-run-mode");
    const libelle = $p("t-run-mode");
    if (jeton && libelle && etat && etat.mode) {
      jeton.dataset.mode = etat.mode;
      libelle.textContent = etat.mode;
      jeton.title = etat.account_scope ? `${etat.mode} · ${etat.account_scope}` : etat.mode;
    }
    const bandeau = $p("bandeau-synthetique");
    if (bandeau) bandeau.hidden = !(etat && etat.synthetic_data);
  }

  /* ===== Décisions ===== */

  async function chargerDecisions() {
    const r = await api.get("/api/v1/decisions", { limit: 60 });
    const zone = $p("dec-liste");
    const rejets = $p("dec-rejets");
    if (!zone || !r || r.ok === false) {
      if (zone) zone.innerHTML = vide("dec.vide");
      return;
    }
    const items = r.items || [];
    $p("dec-n").textContent = items.length ? String(items.length) : "—";
    zone.innerHTML = items.length ? table(
      [t("dec.cutoff"), t("dec.issue"), t("dec.raisons"), t("dec.modele"), t("dec.univers"), t("dec.duree")],
      items.map((d) => {
        const classe = d.outcome === "TRADE" ? "bon" : d.outcome === "FAILED" ? "critique" : "attention";
        const duree = d.timings_ms && typeof d.timings_ms === "object"
          ? Object.values(d.timings_ms).reduce((a, b) => a + (Number(b) || 0), 0)
          : null;
        return `<tr>
          <td>${quand(d.cutoff_at)}</td>
          <td>${puce(d.outcome || "?", classe)}</td>
          <td>${(d.reason_codes || []).map((c) => ech(c)).join(", ") || ND()}</td>
          <td>${txt(d.model_id)}</td>
          <td>${txt(d.universe_version)}</td>
          <td class="num">${duree === null ? ND() : num(duree, 0) + " ms"}</td>
        </tr>`;
      })
    ) : vide("dec.vide");

    const lignes = [];
    for (const d of items) {
      for (const a of d.rejected_alternatives || []) {
        lignes.push(`<tr>
          <td>${quand(d.cutoff_at)}</td>
          <td>${txt(a.instrument || a.inst_id || a.intent_id)}</td>
          <td>${txt(a.side)}</td>
          <td>${txt(a.reason)}</td>
          <td class="num">${a.net_edge === undefined ? ND() : num(a.net_edge, 6)}</td>
          <td class="num">${a.uncertainty_penalty === undefined ? ND() : num(a.uncertainty_penalty, 6)}</td>
        </tr>`);
      }
    }
    if (rejets) {
      $p("dec-rej-n").textContent = lignes.length ? String(lignes.length) : "—";
      rejets.innerHTML = lignes.length
        ? table([t("dec.cutoff"), t("pos.instrument"), t("pos.sens"), t("dec.raisons"), t("dec.net"), t("dec.incertitude")], lignes)
        : vide("dec.vide");
    }
  }

  /* ===== Recherche ===== */

  async function chargerRecherche() {
    const [exp, mod] = await Promise.all([
      api.get("/api/v1/experiments", { limit: 40 }),
      api.get("/api/v1/models", { limit: 40 }),
    ]);
    const zone = $p("rech-liste");
    if (zone) {
      const items = (exp && exp.items) || [];
      $p("rech-n").textContent = items.length ? String(items.length) : "—";
      zone.innerHTML = items.length ? table(
        [t("rech.plan"), t("rech.statut"), t("rech.periode"), t("rech.essais"), t("rech.independant")],
        items.map((e) => `<tr>
          <td>${txt(e.plan_id)}</td>
          <td>${puce(e.status || "?", e.status === "DONE" ? "bon" : "attention")}</td>
          <td>${quand(e.started_at)}</td>
          <td class="num">${e.trials === undefined || e.trials === null ? ND() : num(e.trials, 0)}</td>
          <td>${e.final_test_consulted_at
            ? puce(t("rech.consultee"), "critique")
            : puce(t("rech.independant"), "bon")}</td>
        </tr>`)
      ) : vide("rech.vide");
    }
    const zm = $p("rech-modeles");
    if (zm) {
      const items = (mod && mod.items) || [];
      $p("rech-mod-n").textContent = items.length ? String(items.length) : "—";
      zm.innerHTML = items.length ? table(
        [t("dec.modele"), t("rech.statut"), t("rech.periode"), "commit", "artefact"],
        items.map((m) => `<tr>
          <td>${txt(m.model_id)}</td>
          <td>${puce(m.status || "?", m.status === "LIVE_APPROVED" ? "critique" : m.status === "VALIDATED_OFFLINE" ? "bon" : "attention")}</td>
          <td>${quand(m.period_start)} → ${quand(m.period_end)}</td>
          <td>${txt(m.code_commit)}</td>
          <td>${txt(m.artifact_sha256 ? String(m.artifact_sha256).slice(0, 12) : null)}</td>
        </tr>`)
      ) : vide("rech.vide");
    }
    const za = $p("rech-abc");
    if (za) {
      const rapports = (exp && exp.jev_variants) || [];
      $p("rech-abc-n").textContent = rapports.length ? String(rapports.length) : "—";
      za.innerHTML = rapports.length ? table(
        ["variante", t("dec.net"), t("rech.periode"), t("rech.independant")],
        rapports.map((v) => `<tr>
          <td>${txt(v.variant)}</td>
          <td class="num">${v.net_pnl === undefined ? ND() : num(v.net_pnl, 4)}</td>
          <td>${quand(v.period_start)}</td>
          <td>${v.independent ? puce(t("rech.independant"), "bon") : puce(t("rech.consultee"), "critique")}</td>
        </tr>`)
      ) : vide("rech.vide");
    }
  }

  /* ===== JEV ===== */

  async function chargerJev() {
    const [statut, evenements] = await Promise.all([
      api.get("/api/v1/jev/status"),
      api.get("/api/v1/jev/events", { limit: 40 }),
    ]);
    const tuiles = $p("jev-tuiles");
    if (tuiles && statut && statut.ok !== false) {
      const t1 = (etiq, valeur, sous) => `<div class="tuile entre"><div class="e">${ech(etiq)}</div>
        <div class="v">${valeur}</div><div class="s">${ech(sous || "")}</div></div>`;
      tuiles.innerHTML = [
        t1(t("jev.mode"), ech(statut.influence_mode || "?"), statut.enabled ? "" : "désactivé"),
        t1(t("jev.disponible"), statut.available === null || statut.available === undefined
          ? ND() : (statut.available ? "oui" : "non"), statut.unavailable_reason || ""),
        t1(t("jev.age"), statut.age_seconds === null || statut.age_seconds === undefined
          ? ND() : num(statut.age_seconds, 0) + " s", ""),
        t1(t("jev.latence"), statut.latency_p95_ms === null || statut.latency_p95_ms === undefined
          ? ND() : num(statut.latency_p95_ms, 0) + " ms", "p95"),
        t1(t("jev.cache"), statut.cache_hit_ratio === null || statut.cache_hit_ratio === undefined
          ? ND() : pct(statut.cache_hit_ratio, 1), ""),
        t1(t("jev.erreurs"), statut.errors === null || statut.errors === undefined ? ND() : num(statut.errors, 0), ""),
        t1(t("jev.budget"), statut.daily_spend_usd === null || statut.daily_spend_usd === undefined
          ? ND() : num(statut.daily_spend_usd, 2) + " $", statut.max_daily_spend_usd ? `/ ${statut.max_daily_spend_usd} $` : ""),
      ].join("");
    }
    const zs = $p("jev-sources");
    if (zs) {
      const sources = (statut && statut.sources) || [];
      $p("jev-src-n").textContent = sources.length ? String(sources.length) : "—";
      zs.innerHTML = sources.length ? table(
        ["source", t("jev.age"), "documents", t("jev.statut")],
        sources.map((s) => `<tr>
          <td>${txt(s.source)}</td>
          <td class="num">${s.age_seconds === null || s.age_seconds === undefined ? ND() : num(s.age_seconds, 0) + " s"}</td>
          <td class="num">${s.documents === undefined ? ND() : num(s.documents, 0)}</td>
          <td>${txt(s.status)}</td>
        </tr>`)
      ) : vide("jev.vide");
    }
    const ze = $p("jev-evenements");
    if (ze) {
      const items = (evenements && evenements.items) || [];
      $p("jev-ev-n").textContent = items.length ? String(items.length) : "—";
      ze.innerHTML = items.length ? table(
        [t("jev.type"), t("jev.mapping"), t("jev.version"), t("jev.age"), t("jev.statut")],
        items.map((e) => `<tr>
          <td>${txt(e.event_kind)}</td>
          <td>${txt(e.asset_mapping)}</td>
          <td>${txt(e.model_effective || e.model_version)}</td>
          <td class="num">${e.age_seconds === null || e.age_seconds === undefined ? ND() : num(e.age_seconds, 0) + " s"}</td>
          <td>${puce(e.status || "?", e.status === "ok" ? "bon" : e.status === "late" ? "attention" : "critique")}</td>
        </tr>`)
      ) : vide("jev.vide");
    }
  }

  /* ===== Risque et exploitation ===== */

  let contexte = { mode: "?", compte: "?" };

  async function chargerRisque() {
    const [etat, incidents, qualite] = await Promise.all([
      api.get("/api/v1/system/status"),
      api.get("/api/v1/risk/events", { limit: 40 }),
      api.get("/api/v1/data/quality", { limit: 40 }),
    ]);
    if (etat && etat.ok !== false) {
      contexte = { mode: etat.mode || "?", compte: etat.account_scope || "?" };
      majEntete(etat);
      // `/api/v1/system/status` publie ce bloc sous `halts`, et il vaut null quand aucun
      // RiskState n'existe encore : chaque champ retombe alors sur « Non disponible ».
      const risque = etat.halts || {};
      const ze = $p("risq-etat");
      if (ze) {
        const ligne = (etiq, valeur) => `<div class="t-ligne"><span>${ech(etiq)}</span><b>${valeur}</b></div>`;
        ze.innerHTML =
          ligne(t("risq.halt"), risque.halt_level ? puce(risque.halt_level, risque.halt_level === "NONE" ? "bon" : "critique") : ND())
          + ligne(t("dec.raisons"), txt(risque.halt_reason))
          + ligne(t("risq.perte"), risque.day_realized_loss === undefined ? ND() : num(risque.day_realized_loss, 2))
          + ligne(t("risq.hwm"), risque.high_water_mark_equity === undefined ? ND() : num(risque.high_water_mark_equity, 2))
          + ligne(t("risq.limites"), txt(risque.limits_version));
        $p("risq-etat-n").textContent = risque.halt_level || "—";
      }
    }
    const zi = $p("risq-incidents");
    if (zi) {
      const items = (incidents && incidents.items) || [];
      $p("risq-inc-n").textContent = items.length ? String(items.length) : "—";
      zi.innerHTML = items.length ? table(
        [t("dec.cutoff"), "sévérité", "motif", "portée", "action", "résultat"],
        items.map((e) => `<tr>
          <td>${quand(e.created_at)}</td>
          <td>${puce(e.severity || "?", e.severity === "CRITICAL" ? "critique" : e.severity === "WARN" ? "attention" : "")}</td>
          <td>${txt(e.reason_code)}</td>
          <td>${txt(e.affected_scope)}</td>
          <td>${txt(e.requested_action)}</td>
          <td>${txt(e.observed_result)}</td>
        </tr>`)
      ) : vide("risq.vide");
    }
    const zq = $p("risq-qualite");
    if (zq) {
      const items = (qualite && qualite.items) || [];
      $p("risq-qual-n").textContent = items.length ? String(items.length) : "—";
      zq.innerHTML = items.length ? table(
        [t("dec.cutoff"), "canal", "instrument", "type", "sévérité"],
        items.map((e) => `<tr>
          <td>${quand(e.occurred_at)}</td>
          <td>${txt(e.channel)}</td>
          <td>${txt(e.inst_id)}</td>
          <td>${txt(e.kind)}</td>
          <td>${puce(e.severity || "?", e.severity === "CRITICAL" ? "critique" : "attention")}</td>
        </tr>`)
      ) : vide("risq.vide");
    }
  }

  /* Confirmation contextuelle : le compte ET le mode sont sous les yeux avant d'agir. */
  function confirmer(titre, action) {
    return new Promise((resoudre) => {
      const voile = document.createElement("div");
      voile.className = "voile-confirm";
      voile.innerHTML = `<div class="boite-confirm" role="dialog" aria-modal="true">
        <h3>${ech(t("risq.confirm.titre"))}</h3>
        <div>${ech(titre)}</div>
        <div class="cible">${ech(t("risq.confirm.compte"))} : ${ech(contexte.compte)}<br>
          ${ech(t("risq.confirm.mode"))} : ${ech(contexte.mode)}</div>
        <div class="actions">
          <button class="mini" data-non>${ech(t("risq.confirm.non"))}</button>
          <button class="primaire" data-oui>${ech(t("risq.confirm.oui"))}</button>
        </div>
      </div>`;
      document.body.appendChild(voile);
      const fermer = (valeur) => { voile.remove(); resoudre(valeur); };
      voile.querySelector("[data-non]").addEventListener("click", () => fermer(false));
      voile.querySelector("[data-oui]").addEventListener("click", () => fermer(true));
      voile.addEventListener("click", (e) => { if (e.target === voile) fermer(false); });
    });
  }

  const suivis = new Map();   // request_id -> dernier état connu

  function rendreSuivi() {
    const zone = $p("risq-suivi");
    if (!zone) return;
    if (!suivis.size) { zone.innerHTML = ""; return; }
    zone.innerHTML = table(
      ["demande", "action", t("rech.statut"), "résultat observé", "résidus"],
      [...suivis.values()].map((s) => `<tr>
        <td>${txt(s.request_id)}</td>
        <td>${txt(s.action)}</td>
        <td>${puce(s.status || "?", s.status === "APPLIED" ? "bon" : s.status === "REFUSED" || s.status === "FAILED" ? "critique" : "attention")}</td>
        <td>${txt(typeof s.observed_result === "object" ? JSON.stringify(s.observed_result) : s.observed_result)}</td>
        <td class="num">${s.residuals === undefined ? ND() : num(s.residuals, 0)}</td>
      </tr>`)
    );
  }

  async function suivre(requestId) {
    const r = await api.get(`/api/v1/control/requests/${encodeURIComponent(requestId)}`);
    if (r && r.ok !== false) {
      suivis.set(requestId, {
        request_id: requestId,
        action: r.action,
        status: r.status,
        observed_result: r.observed_result,
        residuals: Array.isArray(r.residual_exposure) ? r.residual_exposure.length : undefined,
      });
      rendreSuivi();
      // Le suivi CONTINUE tant que la demande n'est pas conclue : un HTTP 200 ne conclut rien.
      if (r.status === "REQUESTED" || r.status === "PENDING") setTimeout(() => suivre(requestId), 5000);
    }
  }

  async function commander(commande, libelle) {
    if (!(await confirmer(libelle))) return;
    // Le corps porte le motif, la portée, l'acteur et un identifiant de demande idempotent :
    // l'API refuse une commande sans motif, et un double-clic ne crée pas deux demandes.
    const minute = new Date().toISOString().slice(0, 16).replace(/[-:T]/g, "");
    const r = await api.post(`/api/v1/control/${commande}`, {
      reason: `demande depuis l'interface : ${libelle}`,
      scope: contexte.compte,
      request_id: `ui-${commande}-${minute}`,
      actor: "interface",
    });
    if (r && r.ok && r.request_id) {
      suivis.set(r.request_id, { request_id: r.request_id, action: r.action, status: r.status });
      rendreSuivi();
      suivre(r.request_id);
    } else {
      suivis.set(`refus-${Date.now()}`, {
        request_id: "—", action: commande, status: "REFUSED",
        observed_result: (r && (r.message || r.error)) || t("risq.refus"),
      });
      rendreSuivi();
    }
  }

  const COMMANDES = [
    ["b-pause", "pause", "risq.pause"],
    ["b-annuler", "cancel-entry-orders", "risq.annuler"],
    ["b-flatten", "request-flatten", "risq.flatten"],
    ["b-reprise", "request-resume", "risq.reprise"],
  ];
  for (const [id, canal, cle] of COMMANDES) {
    const bouton = $p(id);
    if (bouton) bouton.addEventListener("click", () => commander(canal, t(cle)));
  }

  /* ===== amorçage ===== */

  api.get("/api/v1/system/status").then((etat) => {
    if (etat && etat.ok !== false) {
      contexte = { mode: etat.mode || "?", compte: etat.account_scope || "?" };
      majEntete(etat);
    }
  }).catch(() => {});

  let demandee = "marche";
  try { demandee = localStorage.getItem("okxq-page") || "marche"; } catch {}
  if (PAGES.some((p) => p.id === demandee)) montrer(demandee);

  Langues.surChangement(() => {
    const c = PAGES.find((p) => p.id === courante);
    if (c && c.charge) c.charge();
    rendreSuivi();
  });

  return { montrer, majEntete, chargerDecisions, chargerRecherche, chargerJev, chargerRisque };
})();
