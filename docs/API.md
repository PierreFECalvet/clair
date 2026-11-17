# API HTTP de la chaîne M1 à M6 (`/api/chaine/*`)

Document destiné au développeur de l'interface. Il suffit à lui seul : routes, paramètres, réponses réelles (tronquées, obtenues le 13/11/2026 en lançant `server.py` sur une copie migrée de `data/atelier.sqlite3` : 54 pages, 55 versions, 11 761 unités, sans LLM, sans NLI, sans embeddings, sans clé Ibou), valeurs possibles de chaque champ de statut avec un libellé français proposé, libellés d'étape de `JOB['stage']`.

Code : `chaine/api.py` (routes), `chaine/pipeline.py` (enchaînement, état), branchés par `server.py`.

---

## 1. Règles communes

- **Hôte** : `http://127.0.0.1:<port>` (ou `localhost`). Les garde-fous existants s'appliquent (`Handler.guard`) : hôte et origine locaux uniquement.
- **Écritures (POST)** : en-tête `X-Session-Token` obligatoire, valeur lue sur `GET /api/session` (comme l'interface actuelle). Corps JSON (objet), 2 Mo au plus.
- **Pendant un traitement** (`job.status == 'running'`), **tout POST est refusé** (400 « Attendez ou suspendez le traitement avant cette opération. »), décisions de revue comprises. Seul `POST /api/stop` (route existante) passe : il suspend le traitement, qui est reprenable.
- **Erreurs** : `{"error": "message en français"}` avec 400 (paramètre invalide, objet déjà traité), 404 (identifiant inconnu, route inconnue), 502 (erreur opérationnelle : LLM, Ibou…), 503 (base occupée, réessayer).

  ```json
  {"error": "Session invalide. Rechargez la page."}
  {"error": "Indiquez un budget Ibou (plafond entier) pour M5."}
  {"error": "Unité introuvable : inconnu"}
  {"error": "Seuil m5.critique : valeur invalide (float attendu, défaut 0.6)."}
  ```
- **Listes paginées** : paramètres `offset` (défaut 0) et `limite` (défaut 50, maximum 200). Réponse `{"total", "offset", "limite", "<liste>"}`. Aucune liste ne renvoie les vecteurs ; aucune ne charge les 11 500 unités d'un coup. Textes longs tronqués (unités à 400 caractères dans les listes, preuves à 1 500).
- **Valeurs stockées** en ASCII sans accent (`a_verifier`, `perimee_par_date`) : à traduire avec les libellés du §5. Les messages (`raison`, `message`, `error`, `detail`) sont déjà en français.
- **Dates** : ISO `AAAA-MM-JJ` ; horodatages ISO UTC.

### Ancien parcours

Les anciennes routes (`/api/state`, `/api/prepare`, `/api/prescreen`, `/api/verify`, `/api/correct`, `/api/expert-*`…) restent en place. Quand `GET /api/chaine/etat` → `migre == true` (ou `/api/pulse` → `chaine.migre`), l'interface peut reléguer l'ancien parcours (onglet « historique »). L'ancien parcours ne relance jamais la migration ; si on l'utilise encore et qu'il crée de nouvelles affirmations, `reste.migration.a_faire` devient positif et la prochaine migration (idempotente) les reprend.

---

## 2. Exécuter la chaîne

Toute exécution passe par le mécanisme de tâche existant (`start_job`, kind `chaine`) : une seule à la fois, suspension par `POST /api/stop`, historique dans `job_runs` (route existante `/api/history`). Une étape interrompue se relance telle quelle : elle ne refait pas ce qui est fait.

### Ordre et étapes (`etape`)

| `etape` | Libellé proposé | Ce qu'elle fait | Prérequis (sinon arrêt motivé) |
|---|---|---|---|
| `migration` | Migration de l'ancien parcours | occurrences → unités, affirmations → groupes, requêtes, appels, propositions | aucun |
| `m1.page` | M1 · segmentation, contexte, ancrage, classes | toutes les versions ; ancrage et classe des unités pas encore traitées ; périmées par date | aucun (spaCy si installé) |
| `m1.unites` | M1 · unités d'information | dernières versions de page **sans unité** (pages nouvelles, versions créées par une décision M6) | option C : LLM **local** + NLI **distinct du LLM** (double passe). Une panne du LLM rend l'étape `ok: false` (`partiel`, raison citant l'erreur) au lieu de « terminée, 0 unité » |
| `m1.faits` | M1 · faits atomiques | unités sans faits, **migrées comprises** ; puis reclassement par prédicat | LLM **local** (ou GLiREL) **et** NLI ; avec le LLM, NLI **distinct du LLM** (double passe) |
| `m1.sujets` | M1 · embeddings, sujets, dédoublonnage | embeddings, sujets FCM, dédoublonnage, groupes | embeddings (sans eux : dédoublonnage exact et groupes seulement, résultat **partiel**). Sans aucune unité : sujets sautés (`sujets.saute`, `raison`). Dédoublonnage plafonné (`dedup.nli_paires_max`) : résultat **partiel** tant que des paires sont reportées ; relancer continue |
| `m2` | M2 · sources et typologie | sources explicites, types, sources manquantes | aucun |
| `m3` | M3 · voisins, paternité, propagation, statuts temporels | étapes 1 à 5 | NLI (sans lui : étapes 3-5 seulement, résultat **partiel**) |
| `m4` | M4 · statuts finaux et requêtes | statuts, une requête par cadre | aucun (LLM facultatif : reformulation) |
| `m5` | M5 · criticité et vérification Ibou | criticité puis appels Ibou, **critiques d'abord** ; `plus_tard: true` traite aussi la file « plus tard » | `budget` > 0, clé Ibou, NLI |
| `m6` | M6 · propositions de correction | propositions pour la revue | aucun |
| `tout` | Chaîne complète | toutes les étapes dans l'ordre ; migration, unités et faits seulement s'il reste du travail ; `budget: 0` saute M5 | s'arrête à la première étape impossible ou partielle et dit pourquoi |

Sous-étapes lançables seules : `m1.peremption` (annonces périmées seulement), `m1.dedup` (dédoublonnage seul), `m3.paternite` (paternité, propagation, statuts sans qualification NLI), `m5.criticite` (criticité seule, sans Ibou).

**LLM local ou tiers (comportement par défaut, relecture M1).** Le fournisseur `lmstudio` est « local », `openai` est « tiers ». Les étapes « LLM local » (unités option C, faits, zone grise, reformulation, réécriture, classement, NLI de repli) refusent un fournisseur tiers sauf réglage `chaine:llm_tiers_autorise` = `1` ; le contrôle d'une sortie du LLM par un NLI qui est ce même LLM est refusé sauf réglage `chaine:nli_par_llm` = `1` (exécution alors marquée `double_passe: "faible"` dans `executions[].engines`). Voir `docs/MOTEURS.md`. Les raisons apparaissent dans `etat.prerequis` et `etat.moteurs.llm`.

Après une décision M6 acceptée, une nouvelle version de page existe sans unité : `reste["m1.unites"].a_faire` > 0 ; relancer `tout` (ou `m1.page`, `m1.unites`, `m1.faits`…) la décompose.

### `POST /api/chaine/lancer`

Corps : `{"etape": "m1.page" | … | "tout", "budget"?: int 0…1 000 000, "pages"?: [version_id…], "forcer"?: bool, "plus_tard"?: bool, "option"?: "auto"|"A"|"B"|"C"}`.
`budget` est obligatoire pour `m5` ; pour `tout`, 0 ou absent saute M5. `pages` restreint `m1.page`, `m1.unites`, `m1.faits`, `m2`. `forcer` recalcule ce qui est déjà fait (à éviter sur le corpus réel). `option` = extraction des unités (A règles, C LLM).

```json
{"ok": true, "etape": "m1.page", "kind": "chaine"}
```

`POST /api/chaine/migrer {}` est un raccourci de `{"etape": "migration"}` : `{"ok": true, "etape": "migration", "kind": "chaine"}`.

### Suivi : `GET /api/pulse` (route existante, enrichie)

À interroger toutes les 1 à 2 s pendant une tâche. `job` (existant) : `status` (§5), `kind` (`chaine`), `stage` (§6), `etape_chaine` (étape de la table ci-dessus), `message`, `done`, `total`, `rate_per_hour`, `eta_seconds`, `trail` (étapes passées). À la fin d'une tâche de la chaîne, `job.chaine` résume le résultat. Nouveau bloc `chaine` (léger) :

```json
"chaine": {
 "migre": true,
 "unites": 11761,
 "requetes_pretes": 0,
 "propositions_en_attente": 456,
 "etape": "m1.faits",
 "stage": "m1.faits",
 "derniere_execution": {"step": "m1.peremption", "status": "terminee", "ended": "2026-11-13T14:22:09.512311+00:00"}
}
```

Fin normale : `job.status = "completed"`, `job.message = "Traitement demandé terminé"`. Prérequis manquant ou étape partielle : `job.status = "paused"` et `job.message` dit pourquoi, par exemple :

```
Étape m1.faits interrompue : M1 · faits atomiques : aucun extracteur de relations (aucun modele LLM configure) : configurez un LLM dans les connexions ou installez GLiREL
Étape m1.dedup interrompue : M1 · embeddings, sujets, dédoublonnage : aucun moteur d'embeddings : installez sentence-transformers (installer_modeles.cmd) ou activez les embeddings du fournisseur dans les connexions ; seuls le dédoublonnage exact et les groupes ont été faits
Étape m3.statuts interrompue : M3 · voisins, paternité, propagation, statuts temporels : aucun moteur NLI : … ; étapes 1-2 (voisins, qualification) non faites, étapes 3-5 faites
Étape m5 interrompue : M5 · criticité et vérification Ibou : clé Ibou manquante (connexions)
```

Durées observées sur la copie réelle (55 pages, sans moteurs) : `m1.page` 103 s avant migration (segmentation spaCy de toutes les pages), 35 s après ; migration 1,8 s ; `m1.unites` 1,8 s (1 version, 257 unités) ; `m2` 70 s ; `m3` 7,8 s ; `m4` 6,3 s ; `m6` 5,6 s. Les étapes longues mettent à jour `done`/`total`.

---

## 3. Lecture (GET)

### `GET /api/chaine/etat`

État complet (≈ 20 Ko, 0,2 s). Champs : `migre`, `migration` (dernière exécution), `ordre`, `sous_etapes`, `compteurs` (par méthodologie et par statut), `reste` (ce qui reste à faire par étape), `prerequis` (raison qui empêche chaque étape, `null` si rien), `moteurs` (moteur actif et replis par fonction, LLM, Ibou), `couts` (par étape depuis `step_runs`, avec `par_page`), `executions` (15 dernières), `dernier_resultat` (dernier lancement), `job`.

```json
{
 "migre": true,
 "migration": {"id": "e2efdd9f18944f82", "started": "2026-11-13T14:19:57.854405+00:00", "ended": "2026-11-13T14:19:59.545484+00:00", "status": "terminee", "note": "crees={\"ibou_calls\": 27, \"proposals\": 1, \"queries\": 324, …"},
 "ordre": [{"etape": "migration", "libelle": "Migration de l'ancien parcours"}, {"etape": "m1.page", "libelle": "M1 · segmentation, contexte, ancrage, classes"}, "… 8 autres"],
 "sous_etapes": [{"etape": "m1.peremption", "parente": "m1.page", "libelle": "M1 · annonces périmées"}, "… 3 autres"],
 "compteurs": {
  "pages": {"pages": 54, "versions": 55, "versions_segmentees": 55, "avec_date_publication": 0, "avec_date_maj": 1, "par_type": {"aucun": 55}},
  "m1": {"unites": 11761, "unites_exclues": 26, "par_extracteur": {"A:spacy:fr_core_news_lg": 257, "migration": 11504},
         "par_classe": {"annonce": 83, "etat_courant": 6762, "immuable": 4824, "inconnu": 92},
         "par_ancre": {"inconnue": 5734, "maj": 101, "phrase": 5926}, "faits": 0, "faits_par_statut": {}, "unites_avec_faits": 0,
         "unites_decomposees": 0, "hypotheses": {}, "entites": 0, "unites_encodees": 0, "sujets": {}, "groupes": 10618, "groupes_fusionnes": 17},
  "m2": {"par_sourcing": {"aucun": 4818, "connaissance_generale": 11, "dependante": 57, "sans_source": 908, "sourcee_externe": 5816, "sourcee_interne": 125},
         "par_type": {"connaissance_generale": 11, "interpretation": 57, "sans_source": 908},
         "sources_par_type": {"lien_externe": 4381, "lien_interne": 128, "note": 10486, "source_nommee": 2637}, "sources_manquantes": 36},
  "m3": {"relations": {"equivalence": 1142}, "paternite": {"a_arbitrer": 56, "nombre_sources": 81, "sujet_principal": 10481},
         "par_statut_temporel": {"a_jour": 11710, "perimee_par_date": 25}, "liens_recommandes": {"proposee": 401}},
  "m4": {"par_statut": {"aucun": 4818, "a_verifier": 564, "a_verifier_faible": 8, "dependante": 57, "sourcee": 6288},
         "requetes_par_statut": {"abandonnee": 316, "envoyee": 8, "plus_tard": 8}, "requetes_par_type": {"ouverte": 332},
         "requetes_repli_unite": 0, "en_attente_decomposition": 564},
  "m5": {"critiques": 0, "criticite_calculee": 552, "verdicts": {"confirmee": 2337, "corrigee": 33}, "appels_par_statut": {"success": 27},
         "unites_ibou": 0, "preuves_retenues": 0},
  "m6": {"par_statut": {"a_rediger": 72, "appliquee": 1, "proposee": 384}, "par_type": {"corrigee": 34, "lien_interne": 398, "perimee_par_date": 25}}
 },
 "reste": {
  "migration": {"a_faire": 0, "detail": "occurrences de l'ancien parcours pas encore migrées"},
  "m1.page": {"a_faire": 0, "versions_a_segmenter": 0, "unites_a_ancrer": 0, "unites_a_classer": 0, "detail": "versions à segmenter et unités à ancrer ou classer"},
  "m1.unites": {"a_faire": 0, "detail": "dernières versions de page sans unité"},
  "m1.faits": {"a_faire": 11735, "detail": "unités dont les faits ne sont pas extraits (migrées comprises)"},
  "m1.sujets": {"a_faire": 11735, "unites_sans_vecteur": 11735, "unites_sans_groupe": 0, "detail": "unités sans embedding ou sans groupe"},
  "m2": {"a_faire": 0, "detail": "unités obsolescibles non typées (les immuables sont hors périmètre de M2)"},
  "m3": {"a_faire": 0, "a_arbitrer": 56, "detail": "groupes sans paternité"},
  "m4": {"a_faire": 0, "en_attente_decomposition": 564, "detail": "unités à vérifier sans requête (hors unités en attente de décomposition M1 faits)"},
  "m5": {"a_faire": 0, "plus_tard": 8, "detail": "requêtes critiques prêtes (la file « plus tard » attend une demande explicite)"},
  "m6": {"a_faire": 0, "en_revue": 456, "detail": "unités corrigées, complétées ou périmées sans proposition"}
 },
 "prerequis": {
  "migration": null, "m1.page": null, "m1.unites": null,
  "m1.faits": "aucun extracteur de relations (aucun modele LLM configure) : configurez un LLM dans les connexions ou installez GLiREL",
  "m1.sujets": "aucun moteur d'embeddings : installez sentence-transformers (installer_modeles.cmd) ou activez les embeddings du fournisseur dans les connexions",
  "m2": null,
  "m3": "aucun moteur NLI : installez le modèle local (installer_modeles.cmd) ou configurez un LLM dans les connexions (repli NLI par LLM contraint)",
  "m4": null, "m5": "clé Ibou manquante (connexions)", "m6": null
 },
 "moteurs": {
  "fonctions": {
   "nli": {"actif": "aucun", "disponible": [], "modele": "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7", "version": "",
           "raison_repli": "transformers : paquet transformers non installe ; llm : aucun modele LLM configure", "mode": "auto", "charge": false,
           "double_passe": "impossible"},
   "embeddings": {"actif": "aucun", "disponible": [], "modele": "Qwen/Qwen3-Embedding-0.6B", "version": "",
                  "raison_repli": "sentence_transformers : paquet sentence_transformers non installe ; api : cle OpenAI manquante", "mode": "auto", "charge": false},
   "…": "phrases, analyse, entites, reordonner, perplexite, classer (même forme)"
  },
  "llm": {"disponible": false, "raison": "aucun modele LLM configure", "fournisseur": "openai", "modele": "",
          "type": "tiers", "tiers_autorise": false, "nli_par_llm_autorise": false,
          "double_passe": "impossible", "raison_double_passe": "aucun moteur NLI : …"},
  "ibou": {"cle": false},
  "extracteur_relations": "aucun",
  "option_unites": "A"
 },
 "couts": [
  {"step": "m1.segmentation", "executions": 3, "terminees": 3, "erreurs": 0, "interrompues": 0, "secondes_locales": 42.896, "tokens_in": 0, "tokens_out": 0,
   "unites_ibou": 0, "pages": 165, "items": 6378, "derniere": "2026-11-13T14:22:09.247877+00:00", "premiere": "2026-11-13T14:18:14.821302+00:00",
   "par_page": {"secondes_locales": 0.26, "tokens_in": 0.0, "tokens_out": 0.0, "unites_ibou": 0.0}},
  "… une ligne par étape (step_runs.step)"
 ],
 "executions": [{"id": "b2c921bc4fed4c42", "step": "m1.peremption", "started": "…", "ended": "…", "status": "terminee", "engines": {"peremption": "regles"},
                 "prompt": "m1-page-v1", "pages": 0, "items": 0, "local_seconds": 0.002, "tokens_in": 0, "tokens_out": 0, "ibou_units": 0, "note": ""}, "…"],
 "dernier_resultat": {"etape": "tout", "ok": false, "arret": "m1.faits", "raison": "aucun extracteur de relations …", "date": "2026-11-13T14:22:09.727303+00:00",
                      "etapes": [{"etape": "migration", "ok": true, "saute": true, "raison": "rien à faire", "duree_s": null}, {"etape": "m1.page", "ok": true, "duree_s": 0.37}, "…"]},
 "job": {"status": "paused", "kind": "chaine", "stage": "m1.faits", "etape_chaine": "m1.faits", "message": "Étape m1.faits interrompue : …", "done": 0, "total": 0}
}
```

Notes : dans `compteurs`, la clé `"aucun"` regroupe les valeurs nulles (par exemple `units.status` nul = hors périmètre : immuable, métadonnée, usage). `couts.pages` est la somme des pages déclarées par chaque exécution. `moteurs.fonctions.<f>.actif` = `aucun` quand aucun moteur n'est utilisable.

**Lot de finition.**
- `compteurs.pages` : **une seule définition**, partout dans l'interface : `pages` = URL distinctes (pages qui ont au moins une version) ; `versions` = toutes les versions (une décision acceptée en crée une). `pages_sans_date_publication` et `pages_sans_date_maj` se comptent sur la **dernière version de chaque page** (ex. « 53 pages sur 54 sans date de publication »). `avec_date_publication` / `avec_date_maj` restent comptés par version.
- `issues` : dernière issue de chaque étape lancée, `{"<etape>": {"issue": "terminee"|"partielle"|"arretee", "raison": str|null, "date"}}`. `partielle` = l'étape a fait une partie du travail (dédoublonnage plafonné, M3 sans NLI, M1 sujets sans embeddings, extraction des unités en échec sur une version) : relancer continue. `arretee` = prérequis manquant, rien n'a été fait.

```json
"issues": {"m3": {"issue": "partielle", "raison": "aucun moteur NLI : … ; étapes 1-2 (voisins, qualification) non faites, étapes 3-5 faites", "date": "2026-11-13T17:10:55+00:00"},
           "m6": {"issue": "terminee", "raison": null, "date": "…"}}
```

### `GET /api/chaine/unites`

Filtres (tous facultatifs, combinables) : `statut` (units.status), `classe` (obs_class), `type` (type d'affirmation M2), `sourcing`, `temporel` (temporal_status), `verdict`, `critique` (`0`/`1`), `page` (version_id ou page_id), `groupe` (group_id), `extracteur`, `q` (texte contenu), `exclues` (`0` défaut, `1`, `tous`), `sans_faits` (`1`). La valeur `aucun` filtre les valeurs nulles (`?statut=aucun`). Tri : URL puis position dans la page. 35 Ko pour 50 unités, 0,02 s.

```json
{
 "total": 11735, "offset": 0, "limite": 50,
 "unites": [
  {"id": "d7039e1d54c9428c",
   "text": "Chez Google, la topicalité mesure à quel point un document traite une requête et combine l…",
   "status": "sourcee", "temporal_status": "a_jour", "obs_class": "etat_courant", "sourcing": "sans_source", "type": "sans_source",
   "verdict": null, "critical": null, "critical_score": null, "centrality": 0.378947, "anchor_date": null, "anchor_kind": "inconnue",
   "expires": null, "extractor": "migration", "excluded": 0, "source_manquante": 0, "version_id": "7788f0991010408a", "span_start": 164,
   "url": "texte:lot:002619d6669c4724ad95d085efda2430", "title": "33-topicalite", "group_id": "7654d607274e4211",
   "faits": 0, "sources": 1, "propositions": 0},
  "… 49 autres"
 ]
}
```

### `GET /api/chaine/unite/<id>`

Tout ce que la carte d'unité doit montrer (6,6 Ko, 0,02 s) : `unite` (toutes les colonnes sauf le vecteur, + `encodee`), `page` (`version_id`, `page_id`, `url`, `titre`, `publication`, `mise_a_jour`, `type`), `chunk` (segment : `kind`, `section`, `start`, `end`, `contexte` {`titre`, `section`, `date`, `date_type`, `reference`, `coreferences`}), `faits`, `hypotheses`, `entites`, `sources` {`propres`, `heritees`}, `type` (type d'affirmation M2 : `type`, `confidence`, `rule`, `depends_on`), `groupes` (chaque groupe : `frame`, `membres` (50 max) et `nombre_membres`, `paternite`), `relations` (avec `autre_texte`), `sujets`, `requetes` (même forme que la liste des requêtes), `appels_ibou`, `preuves` (60 max, texte tronqué à 1 500 car., `longueur` = taille réelle), `propositions` (cartes de revue complètes, voir `propositions`), `liens_recommandes`. 404 si l'unité n'existe pas.

Champs ajoutés (relecture) : dans `unite`, `status_run`, `temporal_run`, `critical_run`, `verdict_run` (exécution `step_runs.id` — ou `decision:<proposition>` — qui a écrit en dernier `status`, `temporal_status`, `critical`, `verdict`) et `status_suivi` (unité `dependante` : statut le plus grave des unités dont elle dépend, sinon `null`). Dans `faits[]`, `evidence_id` : pour un fait d'origine `externe` (M5), la preuve (`preuves[].id`) dans laquelle il est ancré ; `span_start`/`span_end` sont alors des offsets dans le texte de **cette preuve**, pas de la page. Dans `hypotheses[]`, `resolved_value`, `evidence_id`, `span_start`, `span_end` : inconnue résolue par M5 ; si son prédicat n'est pas dans la liste fermée, elle **reste une hypothèse** (`resolved_fact_id` nul) au lieu de devenir un fait sans prédicat. `executions[]` de `/api/chaine/etat` : `engines.double_passe` (`forte`|`faible`) quand une sortie du LLM a été contrôlée par NLI, moteurs **réellement** utilisés (M5 : `nli`, `reordonner`, `extraction`).

```json
{
 "unite": {"id": "1aabfdf5f1954442", "version_id": "80b07da021ae47d4", "chunk_id": "83cec66522e14f8d",
  "text": "Le brevet US6658423 prévoit un usage pendant le crawl pour accélérer celui-ci et économise…",
  "quote": "during a crawling operation to speed up the crawling and to save bandwidth", "span_start": 22287, "span_end": 22361,
  "anchor_date": "2001-01-01", "anchor_kind": "phrase", "expires": "2001-12-31", "obs_class": "annonce", "obs_rule": "regle:lexique:prevision",
  "sourcing": "sourcee_externe", "sourcing_conf": 0.6, "sourcing_rule": "m2.source.paragraphe.lien_externe", "source_manquante": 0,
  "temporal_status": "perimee_par_date", "status": "a_verifier", "verdict": null, "verified_at": null, "centrality": 0.0, "critical": 0,
  "critical_score": 0.3158, "extractor": "migration", "nli_score": null, "legacy_claim_id": "ad9f2bd04b3b4c7c", "excluded": 0,
  "run_id": "e2efdd9f18944f82", "created": "2026-11-13T14:19:58.050135+00:00", "anchor_run": "3b8d8ee240c84623", "obs_run": "e4d9024a60c940e6",
  "facts_run_id": null, "encodee": false},
 "page": {"version_id": "80b07da021ae47d4", "page_id": "e11fe14bb7b541b9", "url": "texte:lot:11f611c664df4be386c5fec734fc6906",
          "titre": "10-deduplication", "publication": "", "mise_a_jour": null, "type": null},
 "chunk": {"id": "…", "kind": "paragraphe", "section": "…", "start": 22200, "end": 22900,
           "contexte": {"titre": "10-deduplication", "section": "…", "date": null, "date_type": null, "reference": null, "coreferences": []}},
 "faits": [],
 "hypotheses": [],
 "entites": [],
 "sources": {
  "propres": [
   {"id": "41b71ea0568079ad", "url": "https://patents.google.com/patent/US6658423", "domain": "patents.google.com", "cited_entity": null,
    "kind": "lien_externe", "scope": "paragraphe", "confidence": 0.6, "inherited_from": null, "contribution": "migration",
    "source_date": null, "passage": null, "created": "2026-11-13T14:19:58.050135+00:00"},
   {"id": "2f74877c69be36ae", "url": null, "domain": null, "cited_entity": "brevet US6658423", "kind": "note", "scope": "paragraphe",
    "confidence": 0.6, "inherited_from": null, "contribution": "m2", "source_date": null, "passage": "[Documenté : brevet US6658423]", "created": "…"},
   "… 2 autres"],
  "heritees": []
 },
 "type": null,
 "groupes": [
  {"id": "ad9f2bd04b3b4c7c", "label": "Le brevet US6658423 prévoit …", "frame": {"periode": null, "portee": null, "predicat": null, "sujet": "US6658423"},
   "legacy_claim_id": "ad9f2bd04b3b4c7c", "merged_into": null,
   "membres": [{"id": "1aabfdf5f1954442", "text": "Le brevet US6658423 …", "version_id": "80b07da021ae47d4", "url": "texte:lot:…", "title": "10-deduplication",
                "status": "a_verifier", "temporal_status": "perimee_par_date", "anchor_date": "2001-01-01"}],
   "nombre_membres": 1,
   "paternite": {"parent_unit_id": "1aabfdf5f1954442", "criterion": "sujet_principal", "detail": {"membres": 1, "pages": 1, "trivial": "membre_unique"},
                 "human": 0, "created": "…"}}
 ],
 "relations": [], "sujets": [], "requetes": [], "appels_ibou": [], "preuves": [],
 "propositions": [{"id": "fe233144dc2d49a3", "statut": "a_rediger", "motif": {"code": "perimee_par_date", "libelle": "Périmée par date", "…": "…"}, "…": "carte complète, voir propositions"}],
 "liens_recommandes": []
}
```

Forme d'un fait : `{"id", "subject", "subject_id", "predicate", "object", "object_norm", "object_type", "scope", "period", "origin", "span_start", "span_end", "verbalization", "nli_score", "status", "replaced_by", "run_id", "created"}` (spans relatifs au texte de l'unité). Hypothèse : `{"id", "kind", "text", "subject", "predicate", "object", "reason", "resolved_fact_id", "created"}`. Relation : `{"a", "b", "relation", "direction", "nli_ab", "nli_ba", "engine", "reason", "created", "autre_texte", "autre_version"}`. Preuve : `{"id", "query_id", "group_id", "call_id", "url", "title", "domain", "page_date", "passage_start", "relevance", "kept", "reject_reason", "nli", "nli_score", "weight", "text", "longueur", "created"}`. Appel Ibou : voir `verifications`.

Champs ajoutés (lot de finition) :
- `paragraphe` : le paragraphe entier de la version qui contient la phrase, `{"texte", "debut", "fin", "phrase_debut", "phrase_fin", "tronque", "trouve"}`. `debut`/`fin` : offsets du paragraphe dans le texte de la version ; `phrase_debut`/`phrase_fin` : offsets de la phrase **dans `texte`**. Paragraphe de plus de 2 400 caractères : coupé autour de la phrase (`tronque: true`). Phrase introuvable dans la version (texte modifié depuis) : `trouve: false`, `texte` = texte de l'unité. Plus besoin de `/api/content/<version>` (ancien parcours).
- `origines` : pour chaque colonne `status_run`, `temporal_run`, `critical_run`, `verdict_run`, `anchor_run`, `obs_run`, `facts_run_id`, `run_id`, l'exécution qui l'a écrite `{"etape", "debut", "statut", "double_passe"}`, ou `{"etape": "decision", "proposition"}` pour une décision de revue, ou `null`.

```json
"paragraphe": {"texte": "La topicalité est le signal qui mesure … en un score nommé T*. Cette décomposition …", "debut": 0, "fin": 1189,
               "phrase_debut": 0, "phrase_fin": 233, "tronque": false, "trouve": true},
"origines": {"status_run": {"etape": "m4.statuts", "debut": "2026-11-13T16:10:51+00:00", "statut": "terminee", "double_passe": null},
             "verdict_run": {"etape": "decision", "proposition": "a794a1d8a3b84506"}, "critical_run": null, "…": "…"}
```

### `GET /api/chaine/pages`

Une ligne par **version** de page. Filtres : `q` (URL ou titre), `dernieres=1` (dernière version de chaque page). 22 Ko, 0,05 s.

```json
{"total": 55, "offset": 0, "limite": 50,
 "pages": [{"version_id": "7788f0991010408a", "page_id": "5f05a1aa5a3c42f6", "url": "texte:lot:002619d6669c4724ad95d085efda2430", "title": "33-topicalite",
            "created": "2026-11-08T07:56:17.324965+00:00", "published": "", "updated": null, "author": "P.C.", "page_type": null, "caracteres": 29623,
            "segmentee": 1, "derniere": 1, "unites": 168, "a_verifier": 7, "perimees": 0, "critiques": 0, "segments": 106, "propositions_en_attente": 16},
           "… 49 autres"]}
```

`published` / `updated` sont vides sur tout le corpus réel : sans elles, pas d'ancrage relatif ni de critère 3 de paternité. Elles se renseignent par `POST /api/chaine/page`.

Filtre `filtre` (lot de finition, côté serveur, combinable avec la pagination) : `sans_date` (pas de date de publication), `sans_maj`, `propositions` (propositions en attente). Valeur inconnue : 400.

Champs ajoutés :
- `taux_obsolescence` : `{"taux", "obsoletes", "verifiables"}` ou `null` (aucune unité vérifiable). **Définition unique** (`chaine.m5.taux_obsolescence`, `m5.taux_unites`), aussi utilisée par M5 et par l'export Excel : unités obsolètes (verdict `corrigee`, ou statut temporel `perimee_par_date`, `remise_en_question`, `obsolete_historique` sans verdict `confirmee`/`completee`) ÷ unités vérifiables (non exclues, non archivées `historique`, hors classes `immuable`, `metadonnee`, `usage` ; la classe `inconnu` compte comme vérifiable). L'interface ne calcule plus de taux.
- `sujets` : trois sujets (M1 sujets, appartenance de la version) au plus, `[{"libelle", "degre"}]` ; vide tant que M1 sujets n'a pas tourné avec des embeddings.

```json
{"version_id": "7788f0991010408a", "title": "33-topicalite", "…": "…", "taux_obsolescence": {"taux": 0.0, "obsoletes": 0, "verifiables": 115}, "sujets": []}
```

### `GET /api/chaine/groupes`

Groupes d'unités (hors groupes fusionnés). Filtres : `relation` (§5, relations M1/M3 d'un membre), `paternite` (critère, ou `aucune`), `multi=1` (au moins deux membres), `page` (version_id), `q` (libellé). Tri : nombre de membres décroissant. 23 Ko, 0,03 s.

```json
{"total": 10618, "offset": 0, "limite": 50,
 "groupes": [{"id": "fc8da8c6f0d74190", "label": "OpenAI précise que les règles de robots.txt peuvent ne pas s'appliquer lorsque les actions…",
              "frame": {"periode": null, "portee": null, "predicat": null, "sujet": "OpenAI"}, "legacy_claim_id": "fc8da8c6f0d74190",
              "created": "2026-11-13T14:19:59.066735+00:00", "parent_unit_id": "89c7e67a4ec448bb", "paternite": "sujet_principal", "human": 0,
              "membres": 15, "pages": 7, "relations": {"equivalence": 14}},
             "… 49 autres"]}
```

Écran d'arbitrage : `?paternite=a_arbitrer` (56 groupes sur la copie), puis `GET /api/chaine/unites?groupe=<id>` pour les membres, puis `POST /api/chaine/paternite`.

### `GET /api/chaine/requetes`

Filtres : `statut`, `kind` (`ouverte`/`controle`), `critique` (`0`/`1`), `repli` (`1` : requêtes de repli seulement, `0` : sans elles), `q`. Tri : critiques puis priorité. 44 Ko, 0,005 s.

Champs ajoutés : `cadre` (sujet, lieu, prédicat, classe, famille, période, valeur, inconnues, perimee, origine, gabarit_brut), `unites` (nombre d'unités couvertes), `appels` (appels Ibou), `envoyable` (bool) et `raison_non_envoyable`, `repli` (gabarit « Est-ce que <unité> ? »).

```json
{"total": 332, "offset": 0, "limite": 50,
 "requetes": [{"id": "qce829b6717411c6", "group_id": "f73013ddb2e444b9",
               "text": "Est-ce que les pages par ville qui mènent toutes à la même offre sont des pages satellites…",
               "kind": "ouverte", "template": "connaissance_generale", "priority": 0.1053, "critical": 0, "humanity": null,
               "checks": {"composantes": {}, "ok": true, "problemes": [], "score": null}, "status": "plus_tard", "attempts": 0, "edited": 0,
               "run_id": "14f826a1765d48bc", "created": "2026-11-13T14:22:01.962424+00:00", "unites": 1, "appels": 0,
               "cadre": {"sujet": "Les pages par", "lieu": null, "predicat": null, "classe": "connaissance_generale", "famille": null, "valeur": null,
                         "inconnues": [], "perimee": false, "origine": "type", "gabarit_brut": "Est-ce que les pages par ville …", "periode": null},
               "envoyable": false, "raison_non_envoyable": "statut plus_tard", "repli": false},
              "… 49 autres"]}
```

Raisons possibles de `raison_non_envoyable` (en clair, lot de finition) : « requête migrée de l'ancien parcours : remplacée par M4 », « repli « Est-ce que <unité> ? » : unité en attente de décomposition (M1 faits) », pour `a_revoir` « contrôles échoués : <problèmes en clair> [; la reformulation automatique a aussi échoué | ; pas de reformulation automatique (LLM local indisponible)]. Corrigez la requête à la main pour la rendre envoyable. », pour `plus_tard` « non critique : envoyée seulement si vous incluez la file « plus tard » au lancement de M5 », « déjà envoyée à Ibou », « abandonnée (remplacée ou devenue inutile) ».

`problemes_lisibles` (ajouté) : les problèmes de `checks.problemes` (texte ASCII de M4) en français : « accents manquants sur : « requete » », « nombre de mots hors de 4 à 30 », « identifiant technique dans le texte : tiret bas (_) », « date au format technique (AAAA-MM-JJ) », « formulation peu naturelle (score 0,4) », « la question contient déjà la valeur à vérifier (biais de confirmation) », « la requête ne nomme plus le sujet du cadre (« X ») ».

**Requêtes et unités sans faits.** Une unité sans fait ni cadre (cas des 11 500 unités migrées tant que `m1.faits` n'a pas tourné) n'a pas de requête : elle est comptée dans `compteurs.m4.en_attente_decomposition` et `reste.m4.en_attente_decomposition` (564 sur la copie). Le gabarit de repli « Est-ce que <texte de l'unité> ? » est désactivé par défaut et M5 refuse d'envoyer une requête de ce gabarit ; il ne revient que par le réglage `repli_requetes_unite: true` (ou si un humain réécrit la requête).

### `GET /api/chaine/verifications`

Appels Ibou (`ibou_calls`), sans le corps des réponses. Filtres : `statut`, `requete` (query_id), `ancien` (`1` : appels migrés de l'ancien parcours ; `0` : appels M5). Tri : plus récents d'abord. `budgets` : les 10 derniers plafonds (`cap`, `used`). 15 Ko.

```json
{"total": 27, "offset": 0, "limite": 50,
 "appels": [{"id": "8c3df28f8a4441cb", "query_id": null, "endpoint": "page", "query": "Le troisième contenu est classé dans la catégorie « Crawl ». 08/11/2026",
             "status": "success", "http_status": null, "units": 5, "units_remaining": null, "max_results": 5, "duration_ms": null, "cached_from": null,
             "legacy_call_id": "8c3df28f8a4441cb", "created": "2026-11-09T18:24:41.952423+00:00", "taille_reponse": 207808, "requete_kind": null,
             "critical": null, "preuves": 0, "preuves_retenues": 0, "resultats": 5},
            "… 26 autres"],
 "budgets": [{"id": "f2432c339c3f4da4", "cap": 100, "used": 99, "created": "2026-11-09T18:20:31.822867+00:00"}]}
```

### `GET /api/chaine/appel/<id>`

Un appel Ibou et ce qu'il a donné (remplace le rattachement par texte de requête) : `appel` (mêmes champs que la liste, sans le corps ; `resultats` = nombre de résultats), `requete` (forme de la liste des requêtes, `null` pour un appel de l'ancien parcours), `unites` (unités couvertes par la requête : `id`, `text`, `status`, `verdict`, `verified_at`, `critical`, `title`, `url` ; 50 au plus), `resultats` (`[{url, titre, domaine}]` renvoyés par Ibou, 30 au plus), `preuves` (preuves issues de **cet** appel, retenues d'abord : `kept`, `reject_reason`, `nli`, `nli_score`, `relevance`, `weight`, `text` tronqué à 1 500, `longueur`), `retenues`, `ecartees`. 404 si l'appel n'existe pas.

```json
{"appel": {"id": "7750422f4b164b3a", "query_id": "q8ed8f1a7ffc72e0", "endpoint": "/search", "status": "success", "units": 2, "resultats": 2, "…": "…"},
 "requete": {"id": "q8ed8f1a7ffc72e0", "text": "Quel est l'effectif de … en 2026 ?", "cadre": {"…": "…"}, "…": "…"},
 "unites": [{"id": "…", "text": "…", "status": "a_verifier", "verdict": "confirmee", "…": "…"}],
 "resultats": [{"url": "https://www.insee.fr/fr/demo", "titre": "Chiffres 2026", "domaine": null}],
 "preuves": [{"id": "…", "kept": 1, "reject_reason": null, "nli": "implication", "nli_score": 0.93, "…": "…"},
             {"id": "…", "kept": 0, "reject_reason": "anterieure_periode", "…": "…"}],
 "retenues": 1, "ecartees": 1}
```

### `GET /api/chaine/solde`

Solde Ibou par la chaîne (remplace `POST /api/test {target: "ibou"}` de l'ancien parcours). Sans clé : aucune requête, `{"disponible": false, "solde": null, "raison": "Clé Ibou manquante : renseignez-la dans Moteurs et réglages, onglet Connexions."}`. Avec clé : `{"disponible": true, "solde": 812, "raison": null}` (journalisé dans l'historique des appels). Erreur Ibou : 502.

### `GET /api/chaine/propositions`

Cartes de revue M6 (`m6.cartes`), triées par criticité décroissante puis par page. `statut` : un statut (§5), `en_attente` (proposee, a_rediger, reportee) ou vide (toutes). `bilan` : comptes par statut et par type. 162 Ko pour 50 cartes, 0,15 à 0,26 s : préférer `limite=20` pour un premier affichage. `GET /api/chaine/proposition/<id>` rend une seule carte.

```json
{"total": 456, "offset": 0, "limite": 50,
 "cartes": [
  {"id": "a794a1d8a3b84506", "unit_id": "f3ffe5339efb4033", "statut": "proposee", "decision": null,
   "page": {"version_id": "210781aa7e2d41ac", "page_id": "22fd7756d41045f4", "url": "texte:lot:54258c84f2a64cab8ed8de89bac07c62", "titre": "02-ou-une-page-sort-de-la-chaine"},
   "contexte": {"paragraphe": "Période | Conseil courant | Ce qu'il en reste | Pourquoi …", "phrase_debut": 800, "phrase_fin": 886, "debut_absolu": 32876, "fin_absolue": 32962},
   "avant_apres": {"avant": "Une demande ponctuelle reste utile après une correction ; la répétition est sans effet",
                   "apres": "[Une demande ponctuelle reste utile après une correction ; la répétition est sans effet](texte:lot:729548fff0114391a1ae9f7b070d6cee)",
                   "diff": [{"op": "ajoute", "texte": "["}, {"op": "egal", "texte": "Une demande ponctuelle reste utile …"}, {"op": "ajoute", "texte": "](texte:lot:7295…)"}],
                   "diff_ratio": 0.0, "budget": 0.35, "methode": "substitution"},
   "motif": {"code": "lien_interne", "libelle": "Lien interne", "origine": "lien_interne", "libelle_origine": "Lien interne",
             "ancrage": {"date": "2011-08-01", "type": "phrase", "date_fr": "1er août 2011"}},
   "faits": {"modifies": [], "conserves": []},
   "preuves": [{"date": null, "domaine": "", "passage": "{\"groupe\": \"9a499c35f9854b26\", \"motif\": \"source_manquante\", …}",
                "url": "texte:lot:729548fff0114391a1ae9f7b070d6cee", "verdict_nli": null, "origine": "verification"}],
   "paternite": {"groupe": "9a499c35f9854b26", "parent_unit_id": "537a0d866bcc4fcc",
                 "page_parente": {"url": "texte:lot:7295…", "titre": "03-decouverte-des-url", "version_id": "9fadbe52194b4e36"},
                 "critere": "sujet_principal", "detail": "{\"centralite\": …}", "humain": false, "est_parente": false,
                 "autres_pages": [{"unit_id": "537a0d866bcc4fcc", "url": "texte:lot:7295…", "titre": "03-decouverte-des-url", "proposal_id": null, "statut": null, "touchee": false}],
                 "enfants": []},
   "controle": {"ok": true, "problemes": [], "tentatives": []},
   "criticite": 0.1053,
   "decision_detail": {"statut": "proposee", "note": null, "date": null, "texte_final": null, "version_appliquee": null}},
  "… 49 autres"],
 "bilan": {"statuts": {"a_rediger": 72, "appliquee": 1, "proposee": 384}, "types": {"corrigee": 34, "lien_interne": 398, "perimee_par_date": 25}}}
```

`preuves[]` (changement, relecture) : `verdict_nli` est désormais le **label NLI** de M5 sur le passage (`implication`, `contradiction`, `neutre`, ou `null` pour un lien interne) et `score_nli` son score ; le rôle de la source (`confirmation`, `correction`…), qui occupait auparavant `verdict_nli`, est dans `contribution`.

Filtres ajoutés (lot de finition) : `motif` (code du motif : `corrigee`, `perimee_par_date`, `lien_interne`…), `page` (version_id ou page_id), `unite` (unit_id), combinables avec `statut`. `motifs` (ajouté) : nombre de propositions par motif pour le `statut` demandé (sans les autres filtres), pour remplir le filtre de la revue : `"motifs": {"lien_interne": 388, "corrigee": 33, "perimee_par_date": 26}`.

`avant_apres.diff` : liste de `{op, texte}` avec `op` ∈ `egal`, `ajoute`, `supprime` (affichage au mot près). Une carte `a_rediger` n'a pas de texte `apres` : seule la décision `modifier` (texte saisi) est possible ; `controle.problemes[].code` en donne la raison (ex. `faits_absents` « Faits M1/M5 indisponibles pour cette unité », `valeur_corrigee_absente`).

### `GET /api/chaine/liens`

Liens internes recommandés par M3 (`link_recommendations`), paginés. Filtres : `statut` (`proposee`, `acceptee`, `appliquee`, `rejetee`), `page` (version source ou cible), `q` (ancre ou texte de l'unité). Chaque lien : `id`, `unit_id`, `from_version`, `to_version`, `anchor`, `reason` (JSON : `groupe`, `motif` = `source_manquante` | `lien_parent_absent`, `parent`, `url_parent`), `status`, `created`, `texte_unite`, `source_url`, `source_titre`, `cible_url`, `cible_titre`, `proposition_id` et `proposition_statut` (proposition M6 « Lien interne » de l'unité, où se prend la décision). `par_statut` : comptes globaux.

```json
{"total": 390, "offset": 0, "limite": 50, "par_statut": {"proposee": 390},
 "liens": [{"id": "9f886c4c82bc1611aed6", "unit_id": "be06d1c5952e44ed", "anchor": "Le témoignage de Pandu Nayak en 2023 …",
            "reason": "{\"groupe\": \"…\", \"motif\": \"source_manquante\", …}", "status": "proposee", "source_titre": "33-topicalite",
            "cible_titre": "32-cascade-de-classement", "proposition_id": "…", "proposition_statut": "proposee", "…": "…"}]}
```

### `GET /api/chaine/journal`

Journal des événements (table `events`), paginé, du plus récent au plus ancien, sans charger `/api/state` (8,7 Mo sur la base réelle). Filtres : `q` (texte contenu), `chaine=1` (événements de la chaîne seulement). `{"total", "offset", "limite", "evenements": [{"id", "message", "created"}]}`. Les exécutions des étapes (coûts, moteurs, double passe) sont dans `etat.executions`.

### `GET /api/chaine/evaluation`

Protocole d'évaluation : `reference` (jeu de référence chargé ?), `mesures` (liste `{methodologie, mesure, valeur, seuil, ok, sens, a_saisir, note}` ; `ok` nul si non mesurable), `a_saisir` (notes humaines attendues et nombre saisi), `apprentissage` (statistiques de revue M6 pour recalibrer les seuils). 15 Ko.

```json
{"reference": {"chargee": false, "nom": null, "pages": 0},
 "mesures": [{"methodologie": "Jeu de référence", "mesure": "Aucun jeu de référence chargé (evaluation.charger)", "valeur": null, "seuil": null, "ok": null, "sens": "min", "a_saisir": false, "note": ""},
             {"methodologie": "M4 requêtes", "mesure": "Note humaine à l'aveugle : « aurait pu être tapée par quelqu'un » (1 à 5)", "valeur": null, "seuil": 4.0, "ok": null, "sens": "min", "a_saisir": true, "note": "À saisir : …"},
             "… 66 autres"],
 "a_saisir": {"m4.note_humaine": {"libelle": "Note humaine à l'aveugle : « aurait pu être tapée par quelqu'un » (1 à 5)", "methodologie": "M4 requêtes", "notes": 0},
              "m5.accord": {"libelle": "Accord avec une vérification manuelle (confirmée et corrigée)", "methodologie": "M5 verdicts", "notes": 0}},
 "apprentissage": {"total_decisions": 1, "seuil": 0.7, "acceptees": 1, "modifiees": 0, "rejetees": 0, "archivees": 0, "total": 1, "taux_sans_modification": 1.0,
                   "par_motif": {"…": "…"}, "par_methode": {"…": "…"}, "diff_ratio": {"acceptees": {"n": 0, "moyenne": null, "mediane": null}, "…": "…"},
                   "nli_min": {"…": "…"}, "criticite": {"…": "…"}, "budget_suggere": null}}
```

`GET /api/chaine/evaluation/gabarit?versions=<id>,<id>` (20 versions au plus) : fichier d'annotation pré-rempli à relire (`{"version", "nom", "pages": [{"version_id", "url", "type_page", "texte", "unites": [{"id", "span", "extrait", "classe", "type", "faits", "relu": false}]}], "paires", "groupes", "grappes"}`), 80 Ko pour une page. À proposer en téléchargement JSON.

### `GET /api/chaine/reglages`

```json
{"domaines": [], "domaines_effectifs": [],
 "moteurs": {"analyse": "auto", "classer": "auto", "embeddings": "auto", "entites": "auto", "nli": "auto", "perplexite": "auto", "phrases": "auto", "relations": "auto", "reordonner": "auto"},
 "modeles": {"classer": "sentence-transformers/paraphrase-multilingual-mpnet-base-v2", "embeddings": "Qwen/Qwen3-Embedding-0.6B", "entites": "urchade/gliner_multi-v2.1",
             "nli": "MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7", "perplexite": "asi/gpt-fr-cased-small", "relations": "jackboyla/glirel-large-v0",
             "reordonner": "BAAI/bge-reranker-v2-m3", "sat": "sat-3l-sm", "spacy": "fr_core_news_lg"},
 "seuils": [{"nom": "dedup.bloc", "valeur": 1024, "defaut": 1024, "surcharge": false}, {"nom": "dedup.cosinus", "valeur": 0.85, "defaut": 0.85, "surcharge": false}, "… 147 autres"],
 "option_unites": "auto", "repli_requetes_unite": false,
 "autres": {"m2:llm": null, "m2:auteurs_experts": null, "m6.pages_datees": null, "m6.format_lien": null, "m1.fcm.moteur": null, "m1.reduction.methode": null,
            "m1:entites:wikidata": null, "gsc:fichier": null},
 "predicats": {"version": "predicats-v1", "nombre": 54, "chemin": "/chemin/du/depot/chaine/predicats.json"},
 "choix": {"moteur": ["auto", "local", "llm", "regles"], "option_unites": ["auto", "A", "B", "C"]}}
```

`domaines` : réglage `site:domaines` ; `domaines_effectifs` : domaines considérés comme internes (réglage + domaines des URL http des pages). `seuils` : 149 seuils (nom pointé, ex. `m5.critique`), type du défaut (`int`, `float`, `bool`, liste `[bas, haut]`, texte). `moteurs.<fonction>` : niveau le plus haut autorisé (`auto` = meilleur disponible, `local`, `llm` = saute les modèles locaux, `regles` = règles seules). `option_unites` : extraction des unités (`auto` = C si un LLM est configuré, sinon A ; `A` règles ; `C` LLM local contraint ; `B` propositionizer).

### `GET /api/chaine/predicats`

Liste fermée complète (54 prédicats) : `{"version", "chemin", "predicats": [{"nom", "libelle", "classe", "volatilite_jours", "type_objet", "gabarit", "critique", "verbalisation"}]}`.

```json
{"version": "predicats-v1", "chemin": "/chemin/du/depot/chaine/predicats.json",
 "predicats": [{"nom": "est_un", "libelle": "est un / se définit comme", "classe": "immuable", "volatilite_jours": null, "type_objet": "texte",
                "gabarit": "connaissance_generale", "critique": false, "verbalisation": "{sujet} est {objet}"}, "… 53 autres"]}
```

### `GET /api/chaine/export.xlsx`

Téléchargement binaire : `Content-Type: application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, `Content-Disposition: attachment; filename="verification-AAAA-MM-JJ.xlsx"`. Un onglet par URL + synthèse. 1,28 Mo, 0,8 s sur la copie. Lien direct (`<a href>`), pas de JSON.

---

## 4. Écriture (POST, jeton de session)

### `POST /api/chaine/decision`

Corps : `{"id": proposal_id, "decision": "accepter"|"modifier"|"rejeter"|"archive"|"reporter", "texte"?: str (obligatoire pour modifier), "note"?: str, "propager"?: bool, "forcer"?: bool}`.

- `accepter` : applique le texte proposé ; crée une **nouvelle version locale** de la page (`version_id`) ; le site d'origine n'est jamais modifié.
- `modifier` : texte saisi, recontrôlé (NLI, budget de différence) ; en cas d'échec rien n'est appliqué : `ok: false`, `raison: "controle_echoue"`, `controle` détaille. `forcer: true` applique malgré tout.
- `rejeter`, `archive` (l'unité devient historique), `reporter`.
- `propager: true` : même décision pour les propositions des unités enfants (même groupe, l'unité est parente), listées dans `propagees` / `ignorees`.

Réponse : `{id, decision, status, version_id, ok, raison, controle, propagees, ignorees}`. `raison` ∈ `null`, `controle_echoue`, `application_impossible` (passage introuvable ou ambigu dans la page).

```json
{"id": "cd27cc48360f4843", "decision": "accepter", "status": "appliquee", "version_id": "e60caf88a0784a42", "ok": true, "controle": null, "propagees": [], "ignorees": [], "raison": null}
{"id": "a794a1d8a3b84506", "decision": "rejeter", "status": "rejetee", "version_id": null, "ok": true, "controle": null, "propagees": [], "ignorees": [], "raison": null}
```

Erreurs 400 : « Proposition déjà traitée (statut rejetee). », « Rien à accepter : rédigez le texte (décision « modifier »). », « Texte requis pour modifier. », « Décision inconnue : … ».

### `POST /api/chaine/paternite`

Arbitrage humain de la paternité d'un groupe : `{"group_id", "unit_id"}` (l'unité doit être membre). Jamais remplacé par un recalcul ; les sources sont repropagées.

```json
{"ok": true, "paternite": {"group_id": "401c336ba33e425f", "parent_unit_id": "183c51a7606046bf", "criterion": "arbitrage_humain",
  "detail": {"critere_precedent": "a_arbitrer", "precedent": null}, "human": 1, "run_id": "099984da135e4ccd", "created": "2026-11-13T14:22:28.487032+00:00"}}
```

### `POST /api/chaine/requete`

Correction humaine d'une requête : `{"id", "texte"}` (500 caractères au plus). Mêmes contrôles qu'à la génération (200 caractères, 4 à 30 mots, pas d'underscore ni de date ISO, accents, une ou deux questions) et le sujet du cadre doit rester nommé. Si `ok`, la requête devient `prete`, `edited = 1`.

```json
{"ok": false, "problemes": ["nombre de mots hors de 4 a 30", "identifiant ou nom de predicat : underscore", "accents absents : requete", "…"], "controle": {"ok": false, "…": "…"}}
{"ok": true, "problemes": [], "controle": {"ok": true, "problemes": [], "score": null, "composantes": {}, "modifiee_depuis": "Est-ce que les pages par ville …"}}
```

`problemes_lisibles` (ajouté) : les mêmes problèmes en français (voir `GET /api/chaine/requetes`).

### `POST /api/chaine/reglages`

Chaque clé est facultative ; tout est validé avant écriture.

| Clé | Valeur | Effet |
|---|---|---|
| `domaines` | liste de domaines (ou texte séparé par virgules) ; `[]` efface | réglage `site:domaines` ; les sources `lien_interne`/`lien_externe` existantes sont **reclassées** (compteurs dans `changes.domaines`) |
| `moteurs` | `{fonction: "auto"|"local"|"llm"|"regles"}` ; fonctions : `phrases`, `analyse`, `entites`, `nli`, `embeddings`, `reordonner`, `perplexite`, `classer`, `relations` | choix du moteur par fonction |
| `modeles` | `{cle: nom}` ; clés : `spacy`, `sat`, `entites`, `nli`, `embeddings`, `reordonner`, `perplexite`, `classer`, `relations` ; `null` = défaut | nom du modèle |
| `seuils` | `{nom: valeur}` ; `null` = retour au défaut | surcharge (type contrôlé) |
| `option_unites` | `auto`, `A`, `B`, `C` | extraction des unités |
| `repli_requetes_unite` | bool (défaut false) | réactive le repli « Est-ce que <unité> ? » (déconseillé) |
| `autres` | `{cle: texte|null}` ; clés : `m2:llm`, `m2:auteurs_experts`, `m6.pages_datees`, `m6.format_lien`, `m1.fcm.moteur`, `m1.reduction.methode`, `m1:entites:wikidata`, `gsc:fichier` | réglages divers des modules |

Réponse : `{"ok": true, "changes": {...}, "reglages": <même forme que GET>}`.

```json
{"ok": true, "changes": {"domaines": {"vers_lien_interne": 502, "vers_lien_externe": 0, "domaines": ["pierrecalvet.fr"]}, "seuils": {"m5.critique": 0.6}}, "reglages": {"…": "…"}}
```

Réglages ajoutés (relecture M1, booléens, désactivés par défaut ; aussi rendus par `GET /api/chaine/reglages`) : `llm_tiers_autorise` (les étapes « LLM local » acceptent un fournisseur tiers, réglage `chaine:llm_tiers_autorise`) et `nli_par_llm` (le LLM peut contrôler ses propres sorties, double passe faible, réglage `chaine:nli_par_llm`). Voir `docs/MOTEURS.md`.

### `POST /api/chaine/page`

Dates d'une version : `{"version_id", "published"?: "AAAA-MM-JJ"|"" , "updated"?: "AAAA-MM-JJ"|""}`. Prépare le recalcul : la version sera resegmentée (contexte daté), ses unités réancrées et reclassées, les paternités non humaines `a_arbitrer`/`date_publication` des groupes concernés recalculées. Relancer ensuite `m1.page` puis `m3` (ou `tout`).

```json
{"ok": true, "version_id": "7788f0991010408a", "dates": {"published": "2025-03-14", "updated": "2026-01-10"}, "unites_a_reancrer": 168, "paternites_a_recalculer": 3,
 "suite": "relancer m1.page (ancrage, classes) puis m3 (paternité) : POST /api/chaine/lancer"}
```

Sur la copie, après `published = 2025-03-14` sur une page puis `m1.page` : 64 unités ancrées `publication` et 2 `relative` (au lieu de `inconnue`).

### `POST /api/chaine/predicats`

Remplacement validé de la liste fermée : `{"predicats": [...], "version"?: "predicats-v2"}` (même forme que `GET`). Liste refusée (400, rien n'est écrit) si un champ manque, un nom n'est pas en ASCII minuscule ou en double, une classe/un gabarit/un type d'objet est hors liste. Hors de 30 à 60 prédicats : accepté avec `avertissements`. L'ancienne liste est gardée dans `predicats.json.precedent`.

```json
{"nombre": 40, "version": "predicats-v2", "avertissements": [], "ok": true}
```

### `POST /api/chaine/predicat`

Édition d'un seul prédicat (lot de finition) : `{"nom", "libelle"?, "classe"?, "volatilite_jours"?, "critique"?, "gabarit"?, "type_objet"?, "verbalisation"?}` ; seuls les champs fournis changent. Validation serveur, refus 400 avec la raison en français et rien d'écrit : libellé de 1 à 120 caractères, classe ∈ `immuable`, `etat_courant`, `annonce`, gabarit ∈ `etat_courant`, `connaissance_generale`, `annonce`, volatilité entière de 1 à 36 500 jours ou `null`, `critique` booléen, verbalisation avec `{sujet}` et `{objet}`, un prédicat `immuable` sans volatilité. Prédicat inconnu : 404. La liste complète est réécrite par `predicats.remplacer` (version incrémentée, ancienne liste dans `predicats.json.precedent`).

```json
{"ok": true, "nombre": 54, "version": "predicats-v2", "avertissements": [], "predicat": {"nom": "a_pour_nom", "libelle": "…", "volatilite_jours": 120, "…": "…"},
 "changes": {"libelle": "…", "volatilite_jours": 120}}
{"error": "Un prédicat immuable n’a pas de volatilité : laissez-la vide (ou choisissez une autre classe)."}
```

### `POST /api/chaine/evaluation`

- `{"action": "charger", "reference": {...}}` : jeu de référence (format dans `chaine/evaluation.py`, exemple `docs/reference_exemple.json`) → `{"ok": true, "nom", "pages"}`.
- `{"action": "saisir", "mesure": "m4.note_humaine", "valeur": 1…5}` ou `{"action": "saisir", "mesure": "m5.accord", "valeur": true|false, "etiquette": "confirmee"|"corrigee"}` → `{"ok": true, "notes": 1}`.

### `POST /api/chaine/lancer`, `POST /api/chaine/migrer`

Voir §2.

---

## 5. Valeurs des champs de statut (libellés français proposés)

**Tâche** — `job.status` : `idle` Prêt · `running` En cours · `paused` Suspendu (reprenable ; voir `message`) · `completed` Terminé · `error` Erreur interne.
`job_runs.status` : `running`, `paused`, `completed`, `error`, `interrupted` (serveur arrêté pendant le traitement).
`step_runs.status` (exécutions, `etat.executions`) : `en_cours` En cours · `terminee` Terminée · `erreur` Erreur · `interrompue` Interrompue.

**Unité**
- `status` (M4) : `sourcee` Sourcée · `a_verifier` À vérifier · `a_verifier_faible` À vérifier (priorité faible) · `non_verifiable` Non vérifiable (opinion) · `dependante` Dépendante · `historique` Historique · nul : Hors périmètre (immuable, métadonnée, usage).
- `temporal_status` (M3) : `a_jour` À jour · `obsolete_historique` Obsolète (historique) · `remise_en_question` Remise en question · `perimee_par_date` Périmée par date.
- `obs_class` (M1) : `immuable` Immuable · `etat_courant` État courant · `annonce` Annonce · `metadonnee` Métadonnée · `usage` Usage · `inconnu` Inconnue.
- `anchor_kind` : `phrase` Date écrite dans la phrase · `relative` Date relative résolue · `maj` Date de mise à jour · `publication` Date de publication · `inconnue` Inconnue.
- `sourcing` (M2) : `sourcee_externe` Sourcée (externe) · `sourcee_interne` Sourcée (interne) · `auto_sourcee` Auto-sourcée · `dependante` Dépendante · `opinion` Opinion · `connaissance_generale` Connaissance générale · `sans_source` Sans source.
- `type` (claim_types, M2) : `information_source` Information sourcée · `opinion` Opinion · `interpretation` Interprétation · `connaissance_generale` Connaissance générale · `sans_source` Sans source.
- `verdict` (M5) : `confirmee` Confirmée · `corrigee` Corrigée · `completee` Complétée · `insuffisante` Sources insuffisantes · `conflictuelle` Sources contradictoires.
- `critical` : `1` Critique (vérifier maintenant) · `0` Plus tard · nul : non calculée. `source_manquante` : `1` Référence interne manquante. `excluded` : `1` Exclue.
- `extractor` : `migration` Migrée · `A:<moteur>` Règles · `C:llm…` LLM local · `B:…` Propositionizer.

**Fait** — `origin` : `explicite` Explicite · `inference_contextuelle` Inférence contextuelle · `externe` Externe (vérification M5). `status` : `extrait` Extrait · `verifie` Vérifié · `corrige` Corrigé (nouvelle valeur) · `complete` Complété · `historique` Historique (remplacé, `replaced_by`).
**Hypothèse** — `kind` : `connaissance_externe` Connaissance externe · `information_inconnue` Information inconnue · `fait_rejete` Fait rejeté.
**Relation** (`unit_relations.relation`) : `equivalence` Équivalence · `precision` Précision · `remplacement` Remplacement · `conflit` Conflit · `faux_conflit` Faux conflit · `sans_rapport` Sans rapport. `direction` : `mutuelle` (équivalence de M1), `a_precise_b` / `b_precise_a` (précision de M1), ou l'identifiant de l'unité la plus précise ou la plus récente (M3).
**Paternité** — `criterion` : `sujet_principal` Sujet principal de la page · `nombre_sources` Nombre de sources · `date_publication` Date de publication · `arbitrage_humain` Arbitrage humain · `a_arbitrer` À arbitrer.
**Source** — `kind` : `lien_interne` Lien interne · `lien_externe` Lien externe · `source_nommee` Source nommée · `note` Note · `verification` Vérification (M5). `scope` : `phrase` Phrase · `paragraphe` Paragraphe · `page` Page. `contribution` : `migration` Migration · `m2` M2 · `heritee` Héritée (M3) · `confirmation` Confirmation (M5) · `correction` Correction (M5). `inherited_from` non nul = source héritée.
**Lien recommandé** — `status` : `proposee` Proposé · `acceptee` Accepté · `appliquee` Appliqué · `rejetee` Rejeté.
**Requête** — `status` : `prete` Prête · `a_revoir` À revoir · `envoyee` Envoyée · `plus_tard` Plus tard · `abandonnee` Abandonnée. `kind` : `ouverte` Question ouverte · `controle` Question de contrôle. `template` : `etat_courant:<prédicat>` / `etat_courant:generique` État courant · `annonce:futur:<famille>` Annonce (futur) · `annonce:passe:<famille>` Annonce échue (passé) · `connaissance_generale` Connaissance générale · `controle:…` Contrôle · `reformulation:…` Reformulation M5 · `unite:fermee` Repli (jamais envoyé par défaut) · `ancien-pipeline` Ancienne requête (jamais envoyée).
**Appel Ibou** (`ibou_calls.status`) : `success` Réussi · `cache` Repris du cache (0 unité) · `debit` Débit limité (429) · `solde_epuise` Solde épuisé · `refuse` Refusé (401/403) · `indisponible` Service indisponible (503) · `incertain` / `uncertain` Incertain (unités réservées par prudence) · `erreur` Erreur · `refused` Refusé (ancien parcours).
**Preuve** — `kept` : `1` Retenue · `0` Écartée ; `reject_reason` : `site_audite` Site audité · `quasi_copie_site` Quasi-copie du site · `reprise` Reprise d'une même dépêche · `anterieure_periode` Antérieure à la période · `pertinence_faible` Pertinence faible · `inaccessible` Page inaccessible ; `nli` : `implication` Soutient · `contradiction` Contredit · `neutre` Neutre.
**Proposition** — `statut`/`status` : `proposee` Proposée · `a_rediger` À rédiger · `acceptee` Acceptée · `appliquee` Appliquée · `modifiee` Modifiée et appliquée · `rejetee` Rejetée · `archive` Archivée · `reportee` Reportée. `kind`/`motif.code` : `corrigee` Corrigée · `perimee_par_date` Périmée par date · `completee` Complétée · `source_ajoutee` Source ajoutée · `lien_interne` Lien interne · `mention_maj` Mention de mise à jour (déjà dans `motif.libelle`). `methode` : `substitution` Substitution · `reecriture_llm` Réécriture LLM · `humain` Humain. `decision` : `accepter`, `modifier`, `rejeter`, `archive`, `reporter`.
**Page** — `page_type` (M2) : `etude`, `enquete`, `analyse`, `decision`, `expertise`, `rapport`, `recherche`, `livre_blanc`, `benchmark`, `tribune`, `edito`, `editorial`, `opinion`, `chronique`, `billet`, `point_de_vue`, `actualite`, `reference`, `autre` (libellé : le mot accentué). Segment (`chunk.kind`) : `titre`, `paragraphe`, `liste`, `citation`, `note`, `metadonnee`, `usage` ; nul = ancien chunk de 5 000 caractères.

---

## 6. Libellés d'étape (`job.stage`)

`job.etape_chaine` vaut l'étape lancée (§2). `job.stage` est plus fin, posé par les modules (valeurs stables) :

| `stage` | Libellé proposé |
|---|---|
| `chaine` | Démarrage de la chaîne |
| `migration` | Migration de l'ancien parcours |
| `m1.page`, `m2`, `m3`, `m4`, `m5`, `m6`, `m1.faits`, `m1.unites`, `m1.sujets`, `m1.dedup`, `m3.paternite`, `m5.criticite` | Libellé de l'étape (§2), avant que le module ne précise |
| `m1.segmentation` | Segmentation des pages |
| `m1.contexte` | Coréférences et dates |
| `m1.ancrage` | Ancrage temporel des unités |
| `m1.obsolescence` | Classes d'obsolescence |
| `m1.peremption` | Annonces périmées |
| `m1.unites` | Unités d'information |
| `m1.faits` | Faits atomiques |
| `m1.encodage.pages` | Embeddings des pages |
| `m1.encodage.unites` | Embeddings des unités |
| `m1.sujets` | Sujets (fuzzy c-means) |
| `m1.dedup` | Dédoublonnage des unités |
| `m2.sources` | Sources explicites |
| `m2.typologie` | Typologie des affirmations |
| `m2.manquantes` | Références internes manquantes |
| `m3.voisins` | Recherche des voisins |
| `m3.qualification` | Qualification des paires |
| `m3.paternite` | Centralité et paternité |
| `m3.propagation` | Propagation des sources |
| `m3.statuts` | Statuts temporels |
| `m4.statuts` | Statuts finaux |
| `m4.requetes` | Requêtes de vérification |
| `m5.criticite` | Criticité |
| `m5.verification` | Vérification Ibou |
| `m6.proposer` | Propositions de correction |

Les anciens libellés (`prepare`, `extraction`, `embeddings`, `screening`…) restent ceux de l'ancien parcours. La table est aussi disponible côté serveur : `chaine.pipeline.LIBELLES_STAGE`.
