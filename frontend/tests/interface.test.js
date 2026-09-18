"use strict";
/* Tests de l'interface, exécutés par `node --test frontend/tests/`.
 *
 * Ils vérifient ce qui casse en silence sans être testé : une clé de traduction manquante dans une
 * langue, un zéro affiché à la place d'une absence, un canal du pont qui ne correspond plus à une
 * route, et la présence des garde-fous visuels (mode, bandeau de données synthétiques). */

const assert = require("node:assert/strict");
const { test } = require("node:test");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const RACINE = path.join(__dirname, "..");
const lire = (f) => fs.readFileSync(path.join(RACINE, f), "utf8");

function chargerLangues() {
  const source = lire("langues.js");
  const contexte = {
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    localStorage: { getItem: () => null, setItem() {} },
    navigator: { language: "fr-FR" },
    document: {
      documentElement: { lang: "fr", setAttribute() {}, getAttribute: () => null },
      querySelectorAll: () => [],
      addEventListener() {},
      getElementById: () => null,
    },
    console,
  };
  contexte.window = contexte;
  vm.createContext(contexte);
  vm.runInContext(source, contexte);
  return contexte;
}

test("chaque clé de traduction existe dans les trois langues", () => {
  // Le module s'exécute dans un contexte minimal : s'il refuse de se charger (API navigateur
  // manquante), le test porte tout de même sur la SOURCE, qui est ce qui sera servi.
  let dico = null;
  try {
    const ctx = chargerLangues();
    dico = ctx.Langues && ctx.Langues.dictionnaire ? ctx.Langues.dictionnaire() : null;
  } catch {
    dico = null;
  }
  const source = lire("langues.js");
  // Le dictionnaire n'est pas exporté : on découpe la SOURCE par clé. Un `{n}` dans une traduction
  // interdit un découpage naïf sur « } » — on va donc jusqu'à la clé suivante.
  const debuts = [...source.matchAll(/^\s*"([a-z0-9._]+)":\s*\{/gim)];
  assert.ok(debuts.length > 100, `dictionnaire trop petit : ${debuts.length}`);
  const entrees = debuts.map((m, i) => ({
    cle: m[1],
    corps: source.slice(m.index, i + 1 < debuts.length ? debuts[i + 1].index : m.index + 600),
  }));
  const manquantes = entrees.filter(
    (e) => !(/\bfr:/.test(e.corps) && /\ben:/.test(e.corps) && /\bsq:/.test(e.corps))
  );
  assert.deepEqual(manquantes.map((m) => m.cle), [], "entrées sans les trois langues");
  if (dico) assert.ok(Object.keys(dico).length > 100);
});

test("les clés des nouvelles vues sont traduites", () => {
  const source = lire("langues.js");
  for (const cle of [
    "nav.decisions", "nav.recherche", "nav.jev", "nav.risque", "indispo",
    "bandeau.synth.titre", "dec.titre", "rech.experiences", "jev.sources", "risq.etat",
    "risq.confirm.compte", "risq.confirm.mode", "risq.flatten",
  ]) {
    assert.ok(source.includes(`"${cle}"`), `clé absente : ${cle}`);
  }
});

test("le pont envoie le jeton CSRF sur les écritures et expose get/post/invoke", () => {
  const source = lire("pont.js");
  assert.ok(source.includes("x-csrf-token"), "en-tête CSRF absent");
  assert.ok(source.includes("okxq_csrf"), "cookie CSRF non lu");
  for (const nom of ["invoke", "get", "post", "on", "subscribe", "surSante", "surLien"]) {
    assert.ok(new RegExp(`\\b${nom}[,:]`).test(source), `api.${nom} absent`);
  }
  assert.ok(source.includes("/api/flux"), "flux SSE absent");
  assert.ok(source.includes("credentials: \"same-origin\""), "cookies de session non envoyés");
});

test("les canaux appelés par la page existent dans la couche de compatibilité", () => {
  const compat = fs.readFileSync(path.join(RACINE, "..", "src", "okxq", "api", "routes", "compat.py"), "utf8");
  const appels = new Set();
  for (const fichier of ["vue.js", "graphe.js", "pages.js"]) {
    const src = lire(fichier);
    for (const m of src.matchAll(/api\.invoke\(\s*"([^"]+)"/g)) appels.add(m[1]);
  }
  assert.ok(appels.size > 0, "aucun canal détecté");
  for (const canal of appels) {
    assert.ok(compat.includes(`"${canal}"`), `canal non servi par l'API : ${canal}`);
  }
});

test("une valeur absente s'écrit « Non disponible », jamais zéro", () => {
  const vue = lire("vue.js");
  assert.ok(vue.includes("const ND = () => t(\"indispo\")"), "helper d'absence absent");
  assert.ok(/return ND\(\);/.test(vue), "nf() ne rend pas l'absence");
  const pages = lire("pages.js");
  assert.ok(pages.includes("const ND = ()"), "pages.js sans helper d'absence");
  // Aucun formulaire de pose de clés ne subsiste dans la page.
  assert.ok(!vue.includes("api.invoke(\"poser-cles\""), "vue.js pose encore des clés");
  assert.ok(!lire("index.html").includes('id="c-poser"'), "le bouton de pose de clés subsiste");
});

test("le mode et la nature des données sont visibles dans la page", () => {
  const html = lire("index.html");
  assert.ok(html.includes('id="j-run-mode"'), "jeton de mode absent");
  assert.ok(html.includes('id="bandeau-synthetique"'), "bandeau de données synthétiques absent");
  for (const mode of ["PAPER", "SHADOW", "DEMO", "LIVE"]) {
    assert.ok(html.includes(`[data-mode="${mode}"]`), `couleur de mode absente : ${mode}`);
  }
  for (const id of ["page-decisions", "page-recherche", "page-jev", "page-risque"]) {
    assert.ok(html.includes(`id="${id}"`), `page absente : ${id}`);
  }
  assert.ok(html.includes("pages.js"), "pages.js non chargé");
  assert.ok(!html.includes("labo.js"), "labo.js encore référencé");
});

test("les commandes critiques demandent une confirmation montrant compte et mode", () => {
  const pages = lire("pages.js");
  assert.ok(pages.includes("function confirmer("), "pas de confirmation");
  assert.ok(pages.includes("risq.confirm.compte") && pages.includes("risq.confirm.mode"),
    "la confirmation ne montre pas compte et mode");
  assert.ok(pages.includes("/api/v1/control/"), "les commandes ne passent pas par l'API v1");
  assert.ok(pages.includes("request_id"), "aucune idempotence de demande");
  // Le suivi continue tant que la demande n'est pas conclue.
  assert.ok(pages.includes("setTimeout(() => suivre(requestId)"), "le suivi s'arrête à la réponse HTTP");
});

test("tous les scripts de la page sont syntaxiquement valides", () => {
  for (const fichier of ["pont.js", "langues.js", "vue.js", "graphe.js", "pages.js"]) {
    const src = lire(fichier);
    assert.doesNotThrow(() => new vm.Script(src), `syntaxe invalide : ${fichier}`);
  }
});
