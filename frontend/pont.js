"use strict";
/*
 * Le pont entre la page et la plateforme.
 *
 * Même surface que dans Hermes : `window.api.invoke(canal, argument)`, `on(canal, fn)`, un flux SSE.
 * Trois différences, toutes imposées par la nouvelle API :
 *
 *   1. Les écritures portent un jeton CSRF. Le serveur pose un cookie `okxq_csrf` à la première
 *      requête authentifiée ; toute méthode non sûre doit le renvoyer en en-tête. Sans cela, le
 *      serveur refuse — et il a raison : un POST déclenché par un autre site ne doit rien commander.
 *   2. Un 403 n'est plus une fin de partie silencieuse : le pont l'annonce sur le canal `pont-etat`
 *      pour que l'en-tête affiche « lecture seule » ou renvoie vers la porte d'accès.
 *   3. Les routes v1 sont accessibles par `api.get(chemin)` : les nouvelles vues (Décisions,
 *      Recherche, JEV, Risque) lisent l'API versionnée plutôt qu'un canal de compatibilité.
 */

(function () {
  const ecouteurs = new Map();   // canal -> Set de fonctions
  let flux = null;
  let reconnexionMs = 1000;
  let dernierRefus = null;

  function normaliser(canal) {
    // « ai-log » et « ai:log » désignent la même chose.
    return String(canal).replace(/:/g, "-");
  }

  function surCanal(canal, charge) {
    const s = ecouteurs.get(normaliser(canal));
    if (!s) return;
    for (const fn of s) { try { fn(charge); } catch (e) { console.error("[pont]", e); } }
  }

  function lireCookie(nom) {
    for (const part of String(document.cookie || "").split(";")) {
      const i = part.indexOf("=");
      if (i < 0) continue;
      if (part.slice(0, i).trim() === nom) return decodeURIComponent(part.slice(i + 1));
    }
    return null;
  }

  function ouvrirFlux() {
    try { if (flux) flux.close(); } catch {}
    flux = new EventSource("/api/flux");
    flux.onopen = () => { reconnexionMs = 1000; surCanal("pont-etat", { relie: true }); };
    flux.onmessage = (ev) => {
      try {
        const m = JSON.parse(ev.data);
        surCanal(m.canal, m.charge);
      } catch {}
    };
    flux.onerror = () => {
      surCanal("pont-etat", { relie: false, refus: dernierRefus });
      try { flux.close(); } catch {}
      setTimeout(ouvrirFlux, reconnexionMs);
      reconnexionMs = Math.min(reconnexionMs * 2, 30000);
    };
  }

  async function lireReponse(rep, canal) {
    if (rep.status === 403) {
      let corps = null;
      try { corps = await rep.json(); } catch {}
      dernierRefus = (corps && (corps.error || corps.message)) || "ACCES_REFUSE";
      surCanal("pont-etat", { relie: true, refus: dernierRefus });
      // Le refus est une RÉPONSE, pas une exception muette : la page doit pouvoir l'afficher.
      return { ok: false, error: dernierRefus, message: (corps && corps.message) || null, canal };
    }
    if (!rep.ok) {
      let corps = null;
      try { corps = await rep.json(); } catch {}
      return { ok: false, error: (corps && corps.error) || `HTTP_${rep.status}`, message: (corps && corps.message) || null, canal };
    }
    dernierRefus = null;
    return rep.json();
  }

  async function invoke(canal, argument) {
    const entetes = { "Content-Type": "application/json" };
    const csrf = lireCookie("okxq_csrf");
    if (csrf) entetes["x-csrf-token"] = csrf;
    const rep = await fetch("/api/" + encodeURIComponent(canal), {
      method: "POST",
      headers: entetes,
      credentials: "same-origin",
      body: JSON.stringify({ arg: argument === undefined ? null : argument }),
    });
    return lireReponse(rep, canal);
  }

  async function post(chemin, corps) {
    const entetes = { "Content-Type": "application/json" };
    const csrf = lireCookie("okxq_csrf");
    if (csrf) entetes["x-csrf-token"] = csrf;
    const rep = await fetch(chemin, {
      method: "POST",
      headers: entetes,
      credentials: "same-origin",
      body: JSON.stringify(corps || {}),
    });
    return lireReponse(rep, chemin);
  }

  async function get(chemin, parametres) {
    const url = new URL(chemin, location.origin);
    for (const [k, v] of Object.entries(parametres || {})) {
      if (v !== undefined && v !== null) url.searchParams.set(k, String(v));
    }
    const rep = await fetch(url.pathname + url.search, { credentials: "same-origin" });
    return lireReponse(rep, chemin);
  }

  function on(canal, fn) {
    const c = normaliser(canal);
    if (!ecouteurs.has(c)) ecouteurs.set(c, new Set());
    ecouteurs.get(c).add(fn);
    return () => ecouteurs.get(c).delete(fn);
  }

  // Une première lecture authentifie la session (la clé de l'URL devient un cookie) AVANT le flux :
  // sans elle, l'EventSource partirait sans session et serait refusé.
  async function amorcer() {
    try { await get("/api/v1/system/status"); } catch {}
    ouvrirFlux();
  }

  window.api = {
    invoke,
    get,
    post,
    on,
    subscribe: (fn) => on("ai-log", fn),
    surSante: (fn) => on("health-tick", fn),
    surLien: (fn) => on("pont-etat", fn),
    dernierRefus: () => dernierRefus,
  };

  amorcer();
})();
