# Contrat de développement — chaîne M1 à M6

Référence fonctionnelle : `docs/METHODOLOGIES.txt` (document de référence, à suivre à la lettre ; les numéros de ligne des sections sont donnés par `grep -n "^## \|^### "`).
Ce contrat fixe ce qui est commun à tous les modules. En cas de conflit entre ce contrat et le document, le document gagne sur le fond, le contrat gagne sur les noms et les signatures.

## 1. Cadre

- Projet : racine du dépôt. Application existante : `server.py` (serveur HTTP + SQLite, bibliothèque standard seule), `app/*.js|css|html`, `tests/`.
- La chaîne vit dans le paquet `chaine/`. **Seul le lot « intégration » modifie `server.py`, `app/index.html` et les fichiers `app/` existants.** Chaque lot ne crée/modifie que ses fichiers.
- **L'application doit démarrer et les tests passer sans aucune dépendance installée.** Tout import tiers (spacy, gliner, transformers, torch, sentence_transformers, trafilatura, extruct, htmldate, dateparser, skfuzzy, umap, hdbscan, faiss, openpyxl…) est fait paresseusement dans une fonction, sous `try/except ImportError`, avec repli. numpy lui-même est optionnel.
- Python 3.11+ (poste de référence : Windows, Python 3.13). Pas de `match` exotique, chemins via `pathlib`.
- Dans l'environnement de développement d'origine : PyPI et GitHub sont joignables (spaCy et ses modèles `fr_core_news_*` installables), **huggingface.co ne l'est pas** : les moteurs à modèles Hugging Face (NLI, GLiNER, embeddings, reranker) se testent avec des doubles. Ne jamais prétendre les avoir exécutés.
- Tests : `python3 -B -m unittest discover -s tests` doit rester vert (114 tests existants). Chaque lot ajoute `tests/test_chaine_<lot>.py`, API simulées, base temporaire (voir `tests/test_pipeline.py::PipelineTests.setUp`). Aucun appel réseau réel dans les tests.
- Base d'essai réelle (non versionnée) : `data/atelier.sqlite3`. **Ne jamais l'ouvrir en écriture : la copier dans /tmp d'abord.**
- Langue : identifiants de code en français ou anglais simples, **valeurs stockées en ASCII sans accent** (`a_verifier`, `perimee_par_date`), libellés accentués seulement dans l'interface. Commentaires et messages en français.
- Style : code lisible, une instruction par ligne (ne pas imiter le style compressé de `server.py`), docstring par fonction publique.

## 2. Accès au serveur : `chaine/noyau.py`

`server.py` peut être exécuté comme `__main__` : **ne jamais écrire `import server` dans `chaine/`**. Le serveur se branche lui-même :

```python
# fin de server.py (lot intégration)
import chaine; chaine.brancher(sys.modules[__name__])
```

```python
from chaine import noyau
S = noyau.srv()          # module serveur branché ; lève RuntimeError sinon
S.query(sql, args, one)  # accès SQLite sérialisé (verrou S.LOCK), lignes en dict
S.uid(), S.now(), S.dump(), S.norm()
S.llm(task, payload)     # LLM configuré (OpenAI ou LM Studio), sortie JSON ; schéma via payload['mode'] -> voir 2.1
S.embed(text)            # embeddings du fournisseur configuré, ou None
S.ibou(endpoint, q, budget) / S.reserve / S.ibou_balance()
S.api_log(...), S.scope(claim_id), S.event(message), S.checkpoint(), S.JOB, S.CONFIG, S.OperationalError
S.fetch_page(url), S.public_url(url)
```

Dans les tests : `import server as s` (qui branche) puis `from chaine import m1`.

### 2.1 Sorties structurées du LLM
`S.output_schema(payload)` choisit le schéma JSON strict selon `payload['mode']`. Les modules de la chaîne n'y touchent pas : ils enregistrent leurs schémas avec `noyau.schema(mode, schema_dict)` et passent `payload['mode']='chaine:<nom>'`. Le lot fondation fournit `noyau.schema` et `noyau.schema_pour(payload)` ; le lot intégration fait appeler `noyau.schema_pour` par `output_schema` pour tout mode commençant par `chaine:`.

### 2.2 Exécutions rejouables et coût : `noyau.etape`
```python
with noyau.etape('m1.unites', moteurs={'unites': 'llm:qwen3'}, prompt='unites-v1', seuils={...}, pages=n) as run:
    ...            # run.id à stocker dans la colonne run_id des lignes produites
    run.pages += 1
```
Crée/ferme une ligne `step_runs` : étape, début, fin, versions des moteurs, version du prompt, seuils, nombre de pages, secondes de calcul local, tokens entrée/sortie et unités Ibou (sommés depuis `api_log` sur l'intervalle, colonne `api_log.step` posée par le contexte). Toute ligne produite par une étape porte `run_id`.

### 2.3 Seuils : `chaine/seuils.py`
Un seul dictionnaire `DEFAUTS` (clé pointée, ex. `nli.implication`, `dedup.cosinus`, `m3.zone_grise`, `m5.critique`), surchargé par la table `settings` (clé `seuil:<nom>`). `seuils.get('nli.implication')`. Aucun seuil en dur dans les modules.

## 3. Moteurs : `chaine/moteurs.py`

Hiérarchie des moyens du document : règles → modèles discriminatifs locaux → LLM local contraint → LLM tiers. Chaque fonction choisit le meilleur moteur disponible selon `settings` (`moteur:<nom>` = `auto|local|llm|regles`) et **renvoie toujours l'identifiant du moteur utilisé** pour traçabilité.

```python
phrases(texte) -> list[tuple[int,int]]                       # spaCy fr_core_news_lg / SaT ; repli règles
analyse(texte) -> objet doc spaCy ou None                    # arbre de dépendances, morphologie (None si spaCy absent)
entites(texte, types=None) -> list[dict(text,type,start,end,engine)]   # GLiNER ; repli spaCy NER ; repli règles (majuscules, montants, dates, %)
nli(premisse, hypothese) -> dict(label in {'implication','contradiction','neutre'}, score float, engine)
nli_lot(paires) -> list[dict]                                # mDeBERTa-v3 XNLI local ; repli LLM contraint (mode chaine:nli) ; jamais de repli « règles »
embeddings(textes) -> tuple[list[list[float]], str]          # sentence-transformers (Qwen3-Embedding-0.6B, bge-m3) ; repli S.embed ; sinon ([], 'aucun')
reordonner(requete, passages) -> list[float]                 # bge-reranker-v2-m3 ; repli cosinus embeddings ; repli recouvrement lexical
perplexite(texte) -> float | None                            # petit modèle de langue local ; None si absent
classer(textes, etiquettes, exemples) -> list[dict(label,score,engine)]  # SetFit ; repli plus proche centroïde sur embeddings ; repli LLM
etat() -> dict[nom -> dict(actif, disponible[list], modele, version, raison_repli)]
```
Les modèles sont chargés une fois (cache module), jamais à l'import. `etat()` ne charge rien de lourd. Noms de modèles configurables (`settings` `modele:<nom>`). Fichiers d'accompagnement : `requirements-local.txt`, `installer_modeles.cmd` (Windows, `py -3 -m pip install -r …` + téléchargement spaCy), section README.

## 4. Schéma SQLite (créé par `chaine/schema.py::init(db)`, idempotent)

Tables existantes conservées et **étendues par ALTER TABLE** (jamais recréées) :
- `versions` (= « pages » du document, une ligne par version de page) : + `author TEXT, updated TEXT, simhash TEXT, page_type TEXT, page_type_engine TEXT, xml TEXT, meta TEXT, lede TEXT`. (`published`, `text`, `page_id` existent ; `pages` garde url/titre.)
- `chunks` : + `section TEXT, end INTEGER, context TEXT, kind TEXT` (paragraphe, titre, liste, citation, note).
- `evidence` : + `query_id TEXT, group_id TEXT, call_id TEXT, domain TEXT, page_date TEXT, passage_start INTEGER, relevance REAL, kept INTEGER, reject_reason TEXT, nli TEXT, nli_score REAL, weight REAL, run_id TEXT`.
- `api_log` : + `step TEXT`.

Tables nouvelles :
```sql
settings(key TEXT PRIMARY KEY, value TEXT);
step_runs(id TEXT PRIMARY KEY, step TEXT, started TEXT, ended TEXT, status TEXT, engines TEXT, prompt TEXT, thresholds TEXT, pages INTEGER, items INTEGER, local_seconds REAL, tokens_in INTEGER, tokens_out INTEGER, ibou_units INTEGER, note TEXT);
units(id TEXT PRIMARY KEY, version_id TEXT, chunk_id TEXT, text TEXT, quote TEXT, span_start INTEGER, span_end INTEGER,
      anchor_date TEXT, anchor_kind TEXT,            -- phrase | relative | maj | publication | inconnue
      expires TEXT,                                   -- date de péremption d'une annonce
      obs_class TEXT, obs_rule TEXT,                  -- immuable | etat_courant | annonce | metadonnee | usage | inconnu
      sourcing TEXT, sourcing_conf REAL, sourcing_rule TEXT,   -- sourcee_externe | sourcee_interne | auto_sourcee | dependante | opinion | connaissance_generale | sans_source
      source_manquante INTEGER DEFAULT 0,
      temporal_status TEXT DEFAULT 'a_jour',          -- a_jour | obsolete_historique | remise_en_question | perimee_par_date
      status TEXT,                                    -- sourcee | a_verifier | a_verifier_faible | non_verifiable | dependante | historique
      verdict TEXT, verified_at TEXT,                 -- confirmee | corrigee | completee | insuffisante | conflictuelle
      centrality REAL, critical INTEGER, critical_score REAL,
      extractor TEXT, nli_score REAL, vector TEXT, legacy_claim_id TEXT, excluded INTEGER DEFAULT 0, run_id TEXT, created TEXT);
entities(id TEXT PRIMARY KEY, name TEXT, type TEXT, aliases TEXT, external_id TEXT, created TEXT);
unit_entities(unit_id TEXT, entity_id TEXT, start INTEGER, end INTEGER, role TEXT, PRIMARY KEY(unit_id,entity_id,start));
facts(id TEXT PRIMARY KEY, unit_id TEXT, subject_id TEXT, subject TEXT, predicate TEXT, object TEXT, object_norm TEXT, object_type TEXT,
      scope TEXT, period TEXT, origin TEXT,          -- explicite | inference_contextuelle | externe(après vérification M5)
      span_start INTEGER, span_end INTEGER, verbalization TEXT, nli_score REAL, status TEXT, replaced_by TEXT, run_id TEXT, created TEXT);
hypotheses(id TEXT PRIMARY KEY, unit_id TEXT, kind TEXT,     -- connaissance_externe | information_inconnue | fait_rejete
      text TEXT, subject TEXT, predicate TEXT, object TEXT, reason TEXT, resolved_fact_id TEXT, run_id TEXT, created TEXT);
topics(id TEXT PRIMARY KEY, label TEXT, method TEXT, centroid TEXT, run_id TEXT);
topic_membership(topic_id TEXT, target_kind TEXT, target_id TEXT, degree REAL, PRIMARY KEY(topic_id,target_kind,target_id));   -- target_kind: version | unit
unit_groups(id TEXT PRIMARY KEY, frame TEXT, label TEXT, legacy_claim_id TEXT, run_id TEXT, created TEXT);   -- frame = JSON {sujet, predicat, portee, periode}
unit_group_members(group_id TEXT, unit_id TEXT, PRIMARY KEY(group_id,unit_id));
unit_relations(a TEXT, b TEXT, relation TEXT,   -- equivalence | precision | remplacement | conflit | faux_conflit | sans_rapport
      direction TEXT, nli_ab REAL, nli_ba REAL, engine TEXT, reason TEXT, run_id TEXT, created TEXT, PRIMARY KEY(a,b));
sources(id TEXT PRIMARY KEY, unit_id TEXT, url TEXT, domain TEXT, cited_entity TEXT,
      kind TEXT,                                      -- lien_interne | lien_externe | source_nommee | note | verification
      scope TEXT,                                     -- phrase | paragraphe | page
      confidence REAL, inherited_from TEXT, contribution TEXT, source_date TEXT, passage TEXT, run_id TEXT, created TEXT);
claim_types(unit_id TEXT PRIMARY KEY, type TEXT,      -- information_source | opinion | interpretation | connaissance_generale | sans_source
      confidence REAL, rule TEXT, depends_on TEXT, run_id TEXT);
paternity(group_id TEXT PRIMARY KEY, parent_unit_id TEXT, criterion TEXT,   -- sujet_principal | nombre_sources | date_publication | arbitrage_humain | a_arbitrer
      detail TEXT, human INTEGER DEFAULT 0, run_id TEXT, created TEXT);
link_recommendations(id TEXT PRIMARY KEY, unit_id TEXT, from_version TEXT, to_version TEXT, anchor TEXT, reason TEXT, status TEXT, created TEXT);
queries(id TEXT PRIMARY KEY, group_id TEXT, text TEXT, kind TEXT,   -- ouverte | controle
      template TEXT, frame TEXT, priority REAL, critical INTEGER, humanity REAL, checks TEXT, status TEXT,   -- prete | a_revoir | envoyee | plus_tard | abandonnee
      attempts INTEGER DEFAULT 0, edited INTEGER DEFAULT 0, run_id TEXT, created TEXT);
query_units(query_id TEXT, unit_id TEXT, PRIMARY KEY(query_id,unit_id));
ibou_calls(id TEXT PRIMARY KEY, query_id TEXT, endpoint TEXT, query TEXT, fingerprint TEXT, max_results INTEGER, status TEXT, http_status INTEGER,
      units INTEGER, units_remaining INTEGER, response TEXT, headers TEXT, duration_ms INTEGER, cached_from TEXT, legacy_call_id TEXT, created TEXT);
proposals(id TEXT PRIMARY KEY, unit_id TEXT, version_id TEXT, kind TEXT,   -- corrigee | perimee_par_date | completee | source_ajoutee | lien_interne | mention_maj
      before TEXT, after TEXT, paragraph TEXT, facts_changed TEXT, facts_kept TEXT, sources TEXT, method TEXT,   -- substitution | reecriture_llm | humain
      checks TEXT, diff_ratio REAL, criticality REAL,
      status TEXT,                                    -- proposee | a_rediger | acceptee | modifiee | rejetee | archive | reportee | appliquee
      decision_note TEXT, decided_at TEXT, final_text TEXT, legacy_patch_id TEXT, run_id TEXT, created TEXT);
```
Index sur toutes les clés étrangères utilisées en jointure. Chaque module peut ajouter des colonnes **à ses propres tables** via `schema.colonne(table, nom, type)` (idempotent) ; il le signale dans son rapport.

### 4.1 Migration de l'existant (`chaine/migration.py::migrer()`, idempotente, une transaction par lot)
- `occurrences` → `units` (id = id de l'occurrence, `text` = texte de l'affirmation, `quote` = citation, span retrouvé dans `versions.text`, `legacy_claim_id`, `extractor='migration'`, ancre depuis `claims.period` si date lisible sinon publication).
- `claims` → `unit_groups` (id = id de l'affirmation) + membres ; `claim_merges` conservé tel quel.
- `claims.status` → `units.status`/`verdict` : validated → sourcee/confirmee ; correct → a_verifier/corrigee ; review, pending → a_verifier ; not_needed → non_verifiable ; `excluded` → `units.excluded`.
- `occurrences.sources` → `sources` (lien_externe / lien_interne selon le domaine).
- `screenings.search_query` non vides → `queries` (template='ancien-pipeline', status 'prete' si recherche en attente).
- `calls` → `ibou_calls` (`legacy_call_id`) ; `patches` → `proposals` (`legacy_patch_id`).
- Les tables anciennes restent en place, en lecture. Rapport : compteurs avant/après, spans introuvables.

## 5. Modules et fonctions publiques

Chaque `executer(...)` : reprenable (ne refait pas ce qui porte déjà un `run_id` courant sauf `forcer=True`), appelle `S.checkpoint()` dans ses boucles, met à jour `S.JOB` (`stage='m1.unites'` etc., `done`, `total`, `message`), renvoie un dict de compteurs. Aucun module n'appelle Ibou sauf `m5`.

| Fichier | Fonctions | Section du document |
|---|---|---|
| `chaine/m1_page.py` | `acquerir(url=None, html=None, texte=None, titre='') -> version_id`, `metadonnees(html, url)`, `segmenter(version_id)`, `resoudre_contexte(version_id)`, `dates.resoudre(expr, reference)`, `classer_obsolescence(unit_ids=None)`, `perimer(aujourdhui=None)` | M1 étapes 1, 2, 3, 6 + statut périmée_par_date |
| `chaine/predicats.py` + `predicats.json` | liste fermée (30–60) : `{nom, libelle, classe, volatilite_jours, type_objet, gabarit, critique, verbalisation}` ; `tous()`, `get(nom)` | M1 ét. 5, 6 ; M4 ; M5 |
| `chaine/m1_unites.py` | `unites(version_id, option='auto')` (A règles, B propositionizer, C LLM contraint, D tiers), `faits(unit_ids)`, `verbaliser(fait)`, `garde_fou(fait, unite)` | M1 étapes 4, 5 |
| `chaine/m1_sujets.py` | `encoder(...)`, `sujets(methode='fcm')` (réduction PCA/UMAP, FCM, indices Xie-Beni et coefficient de partition, HDBSCAN optionnel), `dedoublonner()` | M1 étapes 7, 8 |
| `chaine/m2.py` | `sources_explicites(version_id)`, `typer(unit_ids)`, `signaler_manquantes()`, `executer()` | M2 |
| `chaine/m3.py` | `voisins(unit_id)`, `qualifier(a, b)`, `cadre(unit)`, `paternite(group_id)`, `propager(group_id)`, `statuts_temporels()`, `arbitrer(group_id, unit_id)`, `executer()` | M3 |
| `chaine/m4.py` | `statuts()`, `requetes()`, `gabarit(cadre, classe, aujourdhui)`, `controler(texte) -> dict(ok, problemes, score)`, `modifier(query_id, texte)`, `executer()` | M4 |
| `chaine/m5.py` | `criticite()`, `appeler(query_id, budget)`, `recuperer(call_id)`, `filtrer(...)`, `passages(...)`, `verdict(group_id)`, `reecrire(group_id)`, `executer(budget, tout=False)` | M5 |
| `chaine/m6.py` | `perimetre()`, `proposer(unit_id)`, `controler(proposition)`, `decider(proposal_id, decision, texte=None, note='', propager=False)`, `diff_mots(avant, apres)`, `executer()` | M6 |
| `chaine/export.py` | `excel() -> bytes` (un onglet par URL + synthèse, .xlsx écrit avec zipfile/xml de la bibliothèque standard) | M6 export |
| `chaine/evaluation.py` | `charger(reference_json)`, `mesurer() -> list[dict(methodologie, mesure, valeur, seuil, ok)]`, gabarit d'annotation | Protocole d'évaluation |
| `chaine/pipeline.py` | `etat()`, `lancer(etape, **params)`, `tout(budget)` | orchestration (lot intégration) |

Dates : toujours ISO `AAAA-MM-JJ` en base ; « date du jour » injectée (`aujourdhui=None` → `date.today()`), jamais lue en dur, pour que les tests fixent le 13/11/2026.

## 6. API HTTP (lot intégration ; consommée par l'interface)

Lecture (GET, JSON) : `/api/chaine/etat` (compteurs par méthodologie et par statut, moteurs, coûts par étape, dernières exécutions) · `/api/chaine/unites?statut=&classe=&type=&page=&q=&offset=` · `/api/chaine/unite/<id>` (unité, faits, hypothèses, sources propres et héritées, type, groupe, relations, paternité, requêtes, appels Ibou, preuves, propositions) · `/api/chaine/pages` · `/api/chaine/groupes?relation=&offset=` · `/api/chaine/requetes?statut=&offset=` · `/api/chaine/verifications?offset=` · `/api/chaine/propositions?statut=&offset=` · `/api/chaine/evaluation` · `/api/chaine/export.xlsx`.
Écriture (POST, jeton de session comme l'existant) : `/api/chaine/lancer {etape, budget?, pages?, forcer?}` · `/api/chaine/decision {id, decision, texte?, note?, propager?}` · `/api/chaine/paternite {group_id, unit_id}` · `/api/chaine/requete {id, texte}` · `/api/chaine/reglages {moteurs?, seuils?, modeles?}` · `/api/chaine/migrer {}`.

## 7. Rapport attendu de chaque lot (court)

1. Fichiers créés/modifiés. 2. Ce qui est implémenté, par étape du document. 3. **Ce qui n'est pas implémenté ou seulement en repli**, et pourquoi. 4. Ce qui a été réellement exécuté (tests, nombres) et ce qui ne l'a pas été. 5. Colonnes ou fonctions ajoutées hors contrat. 6. Questions ouvertes pour Pierre.
