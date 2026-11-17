# Relecture indépendante — écarts à corriger

Relecture du paquet `chaine/` contre `docs/METHODOLOGIES.txt` : 198 exigences, 153 conformes, 34 partielles, 5 absentes, 6 contraires. Tous les écarts ci-dessous ont été reproduits en exécution (scripts du relecteur dans `/tmp/relecture/` : `e2e.py`, `pdg.py`, `srcfaible.py`, `m4regles.py`, `m5verdict.py`, `m6t.py`, `perf.py`, `prof.py`). Aucun n'est détecté par la suite de tests actuelle (464 tests verts).

## Bloquants (résultat faux ou coûteux sans le dire)

**B1. Le piège du PDG revient par la déduplication de M1** (doc l.181, 267-281). `m1_sujets.dedoublonner` écrit `unit_relations.relation='conflit'` d'après le NLI seul, sans contrôle du cadre. M3 saute toute paire déjà présente dans `unit_relations` (`m3.py` ~1750, 1763), puis `statuts_temporels` passe en `remise_en_question` toute unité liée par un `conflit` (`m3.py` ~1680). Observé : « Le PDG de X est A » (2023) / « … B » (2025), paire captée par la dédup (cosinus forcé à 0,5) → `conflit`, les deux en `remise_en_question`, et la requête de contrôle porte l'ancienne valeur. Correctif : M3 requalifie (cadre + ancrages) toute relation dont la raison commence par `m1_dedup` et qui vaut `conflit` ou `precision` ; `statuts_temporels` ne lit que les relations qualifiées par M3.

**B2. M4 classe « sourcée » des unités que M2 a jugées sans source** (doc l.192, 327). `m4.py` ~437-441, 484 : `n_sources` compte toutes les lignes de `sources` sans la confiance. Observé : encadré « Sources » de portée page (confiance 0,40) → M2 `sans_source`, M4 `status='sourcee'`, aucune requête. Correctif : filtrer `confidence >= m2.confiance_min OR kind='verification' OR inherited_from IS NOT NULL`, ou s'appuyer sur `units.sourcing`.

**B3. Des états courants sont classés immuables, donc jamais vérifiés** (doc l.148, 152). `m1_page.py` ~1938 : règles `date_passee` (0,70) et `temps:passe+date` ; une date passée suffit, même celle d'une citation ; les règles « fonction » et « chiffre » sont désactivées dès qu'une date passée est présente ; un passif présent est lu comme un passé. Observé : « Selon une note de 2023, Jean Dupont dirige la société X. » → immuable ; « La société X est dirigée par Jean Dupont (communiqué de mai 2024). » → immuable ; base réelle : 6 547 immuables (57 %) dont 1 686 par `date_passee`. Correctif : « immuable » seulement avec un temps passé ET un verbe d'événement ; sinon ne pas décider par règle et passer au classifieur ; ne plus désactiver « fonction » et « chiffre » par `dates_passees`.

**B4. M5 corrige sur la foi d'un seul domaine** (doc l.445-449). `m5.py` ~1508-1519. Observé : un seul domaine qui contredit → `corrigee` ; deux domaines soutiennent + un plus récent contredit → `corrigee`. Correctif : exiger `m5.domaines_independants` domaines indépendants parmi les sources récentes pour « corrigée » ; sinon `conflictuelle` (sources récentes en désaccord avec les autres) ou `insuffisante`.

## Majeurs

**M1. Double passe illusoire, hiérarchie des moyens non tenue** (l.7, 9). Le NLI de repli est le LLM configuré (`moteurs.py` ~55, 971-1012) et `pipeline.prerequis` le compte comme un NLI : unités de l'option C, faits, extractions M5, réécritures M6 sont revérifiés par le même LLM. Rien ne distingue LLM local et LLM tiers : fournisseur par défaut `openai` (`server.py:16`), donc tout « LLM local » part vers une API payante. `classer` passe au LLM avant le repli lexical. Correctif : typer le fournisseur (local = LM Studio, tiers = OpenAI) ; par défaut refuser le NLI-par-LLM pour contrôler une sortie du même LLM (réglage explicite pour l'autoriser, désactivé par défaut, et alors marqué `double_passe: faible` dans le run) ; les étapes « LLM local » s'arrêtent avec une raison claire si le fournisseur est tiers, sauf réglage explicite `chaine:llm_tiers_autorise` ; `classer` : lexical avant LLM.

**M2. M1 étape 1 non branchée** (l.81-86). `m1_page.acquerir` n'est appelé nulle part ; `/api/import` passe par `server.fetch_page`. `versions.author, updated, page_type, lede, xml` restent NULL, JSON-LD perdu, « Temps de lecture » reste dans le texte. Correctif : `/api/import` et `/api/import-batch` (et la route d'import de la chaîne s'il y en a une) passent par `acquerir`.

**M3. Paires « précision » de la dédup jamais regroupées** (l.262-265). Fusion seulement sur les équivalences (`m1_sujets.py` ~1407) et M3 saute ces paires : 84 des 90 paires de précision hors d'un même groupe. Correctif : unir aussi les précisions (ou M3 appelle `_rattacher` sur les relations existantes — cohérent avec B1).

**M4. Panne du LLM avalée**. `m1_unites.py` ~833-840 : étape `terminee`, `ok=True`, 0 unité ; `tout` plante ensuite dans m1.sujets avec « Aucune page encodée… ». Correctif : `ok=False` et statut d'erreur dès que `versions_en_echec > 0` ; m1.sujets saute proprement s'il n'y a rien à encoder.

**M5. Performance de M3**. `Index.voisins` : cosinus en Python pur, normes recalculées, pool parcouru par unité (`moteurs.py` ~1070, `m3.py` ~900). 137,7 s pour 11 478 unités en 64 dimensions ; ~35 min extrapolées en 1 024. Correctif : matrice numpy normalisée, produit par blocs (`m1_sujets._voisins_numpy` existe), repli Python seulement sans numpy.

**M6. « Source ajoutée » exige `source_manquante`** (`m6.py` ~675, doc l.478). Une unité confirmée sans source et sans équivalent ailleurs n'a aucune proposition. Correctif : déclencher sur `verdict='confirmee'` avec sourçage `sans_source` ou `connaissance_generale`.

**M7. Une requête par unité au lieu d'une par cadre** (`m4.py` ~706-747, doc l.352). Seule la famille dominante est retenue. Correctif : un cadre par (sujet, famille, période).

**M8. Faits « externe » de M5 au span inexploitable** (`m5.py` ~1786-1812). Offsets relatifs à un passage de preuve sans référence ; faits « complétée » parfois `predicate=NULL`. Correctif : `facts.evidence_id` + span relatif à cette preuve ; prédicat obligatoire, sinon `hypotheses`.

**M9. Pas entièrement rejouable** (l.11). `aujourdhui` stocké seulement pour m3.statuts ; `status`, `temporal_status`, `critical`, `verdict` réécrits sans identifiant d'exécution ; moteurs enregistrés pour m5 = ceux de `etat()`, pas ceux utilisés. Correctif : `aujourdhui` dans les paramètres des runs concernés, colonnes `*_run`, `run.moteur(...)` sur le moteur réel.

## Mineurs
- Troncature de la dédup à 3 000 paires non signalée comme incomplète à `tout`.
- « Statut suivi » des unités dépendantes non persisté ; interprétations par connecteur avec `depends_on` vide.
- Sur 503 : unités restées réservées et `ibou_calls.units=coût` → surcompte jusqu'à ×3 (`m5.py` ~709).
- Troncatures silencieuses : NLI à 512 jetons (`moteurs.py` ~941) → découper la prémisse en fenêtres et agréger ; page encodée sur 3 000 caractères ; chapô coupé à 600.
- Scores en dur : `m1_page.py` ~1913-1955 ; `m2.py` ~1426-1431.
- Contrôle des accents limité à une liste de mots : « peripherique parisien … ferme » passe.
- Carte M6 : `verdict_nli` contient la contribution, pas le label ni le score NLI (`m6.py` ~961).
- Coût : moyenne par étape, pas mesure par page (`evaluation.py` ~404).

## Tests manquants
- Piège du PDG via le chemin dédup (B1) ; « corrigée » avec un seul domaine (B4) ; sources de faible confiance en M4 (B2) ; panne LLM en option C (M4) ; import par URL avec JSON-LD suivi de M2 (M2) ; criticité et pertinence de bout en bout (le test d'intégration fixe `m5.critique=0`, reranker constant, NLI lexical).
