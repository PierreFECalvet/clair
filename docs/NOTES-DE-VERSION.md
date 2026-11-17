> Notes de version détaillées, reprises telles quelles du projet d'origine. Les passages qui citent `data/`, `work/` ou des volumes de corpus décrivent l'installation de l'auteur ; ces dossiers ne sont pas dans le dépôt.

# Clair — Atelier de fact-checking

Application HTML/CSS/JavaScript avec serveur Python et base SQLite, installée dans ce dossier. Aucun paquet tiers à installer. Python 3.11 ou supérieur requis (testé avec Python 3.13).

## Démarrage

1. Double-cliquer sur `demarrer.cmd`.
2. Ouvrir **http://127.0.0.1:8765** dans un navigateur.
3. Ouvrir **Connexions & modèles**. Renseigner les clés OpenAI et Ibou, puis l’identifiant exact d’un modèle accessible à votre compte. Le bouton de test de connexion remplit la liste de modèles sans effectuer de génération.
4. Pour LM Studio, choisir ce fournisseur, démarrer son serveur local, renseigner son URL (par défaut `http://127.0.0.1:1234/v1`) et les identifiants de vos modèles chargés. Le modèle de langage doit gérer le JSON. Le modèle d’embeddings est distinct ; adapter son identifiant ou désactiver les embeddings.

Le serveur écoute exclusivement sur la boucle locale. Ne pas exposer ce serveur sur Internet : il s’agit d’un back-office personnel, sans gestion multi-utilisateur. Garder la fenêtre du serveur ouverte. Fermer cette fenêtre arrête les traitements et efface les clés en mémoire.

La progression durable (contenus, chunks terminés, affirmations, brouillons, exclusions et budgets) reste dans SQLite après redémarrage. Le bandeau de progression en mémoire est réinitialisé ; relancer les chunks en attente reprend les éléments non terminés, sans réimporter les fichiers. Un appel API interrompu peut avoir été facturé sans que sa réponse soit enregistrée : il n’est pas repris au milieu.

Le suivi attend désormais la fin d’une actualisation avant de programmer la suivante, et partage une seule requête d’état entre les actions concurrentes. Les vecteurs des affirmations ne sont plus chargés pour afficher le tableau ; les proximités des contenus sont mises en cache une minute. Une perte de connexion au navigateur ne signifie pas nécessairement que l’action a échoué côté serveur : consulter l’état avant de cliquer de nouveau. Les messages distinguent la connexion locale, une actualisation échouée et les erreurs du fournisseur API.

## Chaîne M1 à M6 (mise à jour du 13/11/2026)

L'outil suit désormais les six méthodologies du document « Vérificateur d'obsolescence — Méthodologies » (copie dans `docs/METHODOLOGIES.txt`). Le code est dans le paquet `chaine/` ; l'ancien parcours reste consultable sous « Archives ».

- **Avant le premier lancement** : arrêter le serveur, copier `data/atelier.sqlite3` (sauvegarde), relancer `demarrer.cmd`. Le schéma est étendu au démarrage ; la reprise des affirmations existantes (« Migration ») se lance depuis la première ligne de « À faire ».
- **Sans rien installer**, l'outil démarre et fait tourner ce qui relève des règles : segmentation, dates, classes d'obsolescence, sources et typologie (M2), paternité (M3), statuts (M4), propositions (M6). Les étapes qui demandent un modèle s'arrêtent en disant lequel manque.
- **Moteurs locaux** (spaCy, GLiNER, NLI mDeBERTa, embeddings, reranker) : `installer_modeles.cmd`, détail dans `docs/MOTEURS.md`. Le LLM local attendu est LM Studio ; OpenAI est traité comme un LLM tiers et n'est utilisé pour les étapes « LLM local » que si le réglage correspondant est activé dans « Moteurs et réglages ».
- **Réglages à faire une fois** : domaine(s) du site audité et dates de publication des pages (vue Pages) ; liste de prédicats (`chaine/predicats.json`, modifiable dans l'interface).
- **Documents** : `docs/API.md` (routes), `docs/CONTRAT.md` (schéma et signatures), `docs/RELECTURE.md` (écarts relevés par la relecture et corrigés), `docs/MOTEURS.md`.
- **Tests** : `py -3 -B -m unittest discover -s tests` (507 tests, environ 3 minutes ; LLM, NLI, embeddings et Ibou y sont simulés).

## Requêtes autonomes (15/11/2026)

Les 855 requêtes prêtes et les 51 à revoir ont été relues avec leurs affirmations et passages sources. La correction conserve les identifiants, les associations aux unités et les questions de contrôle. Elle explicite les sujets, précise la relation recherchée, distingue hypothèses et faits à vérifier et retire les dates courantes artificiellement ajoutées aux faits historiques. Elle ne valide pas la véracité des affirmations et ne lance aucune recherche Ibou.

La sauvegarde des lignes initiales, les contextes, les corrections et le script d'application avec contrôle de concurrence sont dans `work/requetes-autonomes/`. Les questions relues sont marquées comme éditées pour résister à une régénération. Le générateur transmet désormais les passages sources à la reformulation et refuse plusieurs formes de questions génériques, anaphores non résolues et erreurs de période. Ces contrôles sont des garde-fous, pas une preuve d'équivalence sémantique. Les corrections du code prennent effet au prochain redémarrage du serveur ; les questions corrigées sont immédiatement disponibles après actualisation de la liste.

## Suivi, À faire, requêtes Ibou et historique des appels (mise à jour du 12/11/2026)

Prend effet au prochain redémarrage du serveur (`demarrer.cmd`). La base est migrée automatiquement : tables `api_log` et `job_runs`, colonnes `claim_id`, `budget_id`, `duration_ms`, `http_status`, `results` sur `calls`. Les appels Ibou déjà enregistrés sont repris dans le journal.

- **Vue d'ensemble** : le parcours en sept étapes avec ses compteurs réels, puis la liste **À faire**, triée, chaque ligne menant à la vue filtrée correspondante. Le bandeau de traitement affiche l'étape, la progression, la cadence horaire et la durée restante estimée. Le total du précontrôle est désormais le reste à faire, pas la taille du corpus.
- **Requêtes Ibou** : les requêtes en attente sont lisibles et modifiables avant envoi, ou écartables (« Ne pas chercher » passe l'affirmation en revue humaine). Des signaux simples désignent celles à relire (longueur, question, mots de commentaire, inexistence, hypothèse, anglais). L'onglet « Déjà envoyées » montre pour chaque appel les résultats renvoyés et le verdict qui a suivi.
- **Historique des appels** : une ligne par échange avec OpenAI / LM Studio, Ibou (cache compris) et les sources citées : opération, affirmation concernée, durée, statut HTTP, tokens, unités, erreur. Le contenu envoyé au modèle est résumé (champs courts en clair, champs longs réduits à leur taille) ; la réponse est conservée jusqu'à 4 000 caractères. Les clés sont masquées. Chaque affirmation liste ses propres échanges.
- **Affirmations sans requête** : une affirmation dont les sources citées ne concluaient pas restait en revue avec le motif « Sujet ou requête non résolu », alors qu'aucune requête n'avait été demandée au modèle. Le précontrôle demande maintenant une requête dans tous les cas et le motif est exact. Pour les décisions déjà enregistrées : filtres **Sans requête** et **Sources inaccessibles**, puis **Préparer des requêtes Ibou** sur la sélection (appels au modèle, aucune unité Ibou ; la recherche reste soumise au budget).
- **Sources lues en entier** : une source citée ou un passage n'est plus coupé à 24 000 caractères. Jusqu'à 120 000 caractères de preuves partent en un appel ; au-delà, les documents sont découpés, les passages les plus proches de l'affirmation partent d'abord et les lots suivants couvrent le reste (6 lots au maximum par affirmation). Le verdict enregistre la part réellement lue (`coverage`) et le motif signale toute lecture incomplète. Les citations sont toujours vérifiées sur le texte entier.
- **Requête Ibou = une question** : la requête est une question neutre et autonome (jamais l'affirmation recopiée), terminée par « ? », de 5 à 32 mots, qui nomme le sujet. Un contrôle déterministe (`query_problems`) s'applique à la proposition du modèle, puis à deux réécritures dédiées ; en cas d'échec l'affirmation reste en revue (« Question automatique refusée »). Une modification manuelle passe par le même contrôle. Un verdict « à corriger » distingue désormais le fait dépassé (`outdated`).
- **Rattrapage** (bouton dans À faire) : remet au précontrôle les affirmations en revue dont une source dépassait l'ancienne coupe, rédige une question pour les autres affirmations restées sans requête, et réécrit les requêtes en attente rédigées avant cette règle. Appels au modèle uniquement.
- Le navigateur n'interroge plus que `/api/pulse` (compteurs) toutes les 5 secondes ; la liste complète des affirmations n'est rechargée que si la base a changé, au plus une fois par minute pendant un traitement.

## Parcours

### Révision approfondie d'une affirmation avec OpenAI

Pour plusieurs affirmations, cocher les lignes puis cliquer sur **Proposer des corrections pour la sélection (N)**. Le serveur enregistre la sélection et le modèle avancé choisi, puis traite les affirmations une par une avec leur propre contexte. Les propositions ne sont pas appliquées et aucun appel Ibou n'est effectué. Chaque analyse peut consommer des crédits OpenAI.

Le panneau de lot indique **N / total analysées** ; le filtre **Dernière sélection traitée** retrouve les affirmations concernées. Ouvrir une affirmation par son titre pour lire son rapport. En cas d'erreur ou de suspension, **Reprendre les restantes** réutilise les résultats terminés du lot, y compris après redémarrage (renseigner à nouveau la clé OpenAI). Les points de reprise sont enregistrés dans `expert_batches` et `expert_batch_items`, atomiquement avec chaque rapport. Un appel interrompu avant enregistrement peut avoir été facturé et devra être refait. Le modèle enregistré au lancement est conservé pour la reprise. Lancer un nouveau lot est une nouvelle demande d'analyse ; utiliser Reprendre pour éviter de refaire les éléments déjà terminés.

Dans **Connexions & modèles**, choisir un **modèle OpenAI pour les révisions approfondies**, distinct du modèle habituel si souhaité. Le bouton dédié liste les modèles accessibles avec la clé OpenAI ; aucun modèle n'est sélectionné ou remplacé automatiquement. Ce choix fonctionne même si le moteur habituel est LM Studio. Il est conservé en mémoire pour la session, comme les autres paramètres.

Ouvrir une affirmation, préciser son contexte (par exemple « Google leaks de 2024 »), ajouter une proposition de correction et éventuellement jusqu'à dix URL. Le modèle reçoit la formulation originale, les citations et leurs contextes, jusqu'à trois documents de 24 000 caractères, le catalogue des sources et jusqu'à quinze extraits de preuves de 24 000 caractères. Le bouton **Voir le contexte du corpus** permet d'inspecter les données locales. Les URL complémentaires et jusqu'à cinq références candidates sont consultées directement, dans une limite totale de dix sources ; les échecs de récupération sont signalés.

La révision fournit une formulation contextualisée, un prompt de vérification réutilisable, des conclusions par sous-affirmation, une correction proposée et les questions non résolues. Elle doit distinguer fidélité au texte et véracité externe, documentation et déploiement effectif, brevet et preuve d'utilisation, priorité absolue et priorité relative. La correction de l'utilisateur est conservée et examinée ; une nuance comme « une des premières » ne doit pas être rejetée au motif qu'elle serait identique à « la première ». Sa justification historique reste à examiner.

Cette action utilise l'API OpenAI Responses avec une sortie structurée et `store: false`. Elle ne fait ni recherche Ibou ni recherche web OpenAI : seules les sources connues ou ajoutées sont consultées. Le rapport indique les recherches complémentaires suggérées, sans prétendre les avoir faites. Une citation exacte absente des preuves transmises ou un changement de sujet bloque la préparation des corrections. Ces garde-fous ne garantissent pas la justesse de l'interprétation du modèle.

Les rapports et leur contexte sont conservés dans `expert_reviews` et inclus dans l'export. Ils ne modifient pas automatiquement les contenus ni leurs verdicts. Pour une proposition étayée, cliquer sur **Préparer les corrections à partir de cette proposition**, puis examiner les passages dans **Corrections**. Cette réécriture utilise le même modèle avancé ; elle crée uniquement des propositions locales, à appliquer et à revérifier dans le parcours existant. Les occurrences modifiées depuis le rapport imposent une nouvelle révision. Les appels OpenAI peuvent être facturés ; aucun appel réel n'est nécessaire pour les tests automatisés.

### Dédoublonnage global confirmé par LLM

La surveillance démarre automatiquement avec un traitement : un contrôleur côté serveur vérifie son état toutes les **5 minutes**. Une réponse de dédoublonnage invalide, un JSON inexploitable, une génération incomplète ou une erreur transitoire de connexion au modèle pendant cette étape programme une reprise au prochain contrôle. Il n'y a jamais de second worker lancé pendant qu'un appel est encore actif. Après une réponse invalide, les candidats de l'affirmation concernée sont traités un par un ; les décisions déjà enregistrées sont réutilisées. Les erreurs, la prochaine vérification et le compteur de reprises sont visibles dans un panneau permanent, avec une notification lors des changements d'état.

Les reprises récupérables continuent jusqu'à réussite ou suspension manuelle et peuvent consommer des crédits supplémentaires du modèle. **Suspendre** annule aussi les reprises programmées. Les erreurs de configuration, de clé, de crédits, les plafonds Ibou et les erreurs internes ne sont pas relancés automatiquement. Le navigateur peut être fermé, mais le serveur doit rester ouvert. Après redémarrage du serveur, il faut renseigner les connexions et relancer le parcours ; la surveillance n'est pas un service Windows.

Le signal exact **full list completed** est enregistré dans le journal et affiché durablement dans le panneau de la session lorsque la passe de dédoublonnage a parcouru toute sa liste sans erreur non résolue. Il désigne cette étape, pas la validation de tous les faits ni l'achèvement d'un corpus dont le précontrôle a été limité à 50 éléments. Une erreur ne saute pas le lot et ne déclenche jamais ce signal.

Après l'extraction, le bouton **Dédoublonner avec le LLM** effectue une seconde passe sur les affirmations déjà présentes, entre chunks et entre contenus. Cette passe est également exécutée automatiquement dans **Tout lancer**, avant le précontrôle autonome et avant la vérification. Il n'est pas nécessaire de réextraire les contenus existants.

Cette passe fonctionne aussi sans embeddings : un index lexical recherche les candidats dans tout le corpus, sur huit termes discriminants, avec un seuil de recouvrement de 0,30 et jusqu'à douze candidats par affirmation. Le LLM compare chaque candidat au fait de référence, avec les périodes et les citations contextualisées. Une similarité seule ne provoque jamais de fusion. Les paraphrases sans vocabulaire commun peuvent être manquées : c'est une recherche de candidats bornée, pas une comparaison exhaustive de toutes les paires. Le rapprochement par embeddings pendant l'extraction reste complémentaire.

Les différences d'années explicites, les décisions humaines et les différences d'exclusion bloquent les fusions automatiques. Les autres distinctions (quantité, négation, attribution, périmètre, période absente) doivent être examinées par le LLM, qui est invité à refuser en cas de doute. Les réponses invalides suspendent le traitement sans fusion du lot.

Chaque fusion est une transaction SQLite : regroupement des occurrences et de leurs sources, conservation des preuves et des contrôles antérieurs, archivage des enregistrements originaux dans `claim_merges`. Les propositions non appliquées sont signalées en conflit et le précontrôle est invalidé pour l'affirmation regroupée. Les textes des articles restent inchangés. Les identifiants regroupés sont visibles dans le détail de l'affirmation ; l'export contient les archives complètes. Il n'y a pas encore de bouton d'annulation automatique d'une fusion.

Les comparaisons sont enregistrées dans `dedup_pairs` pour reprendre sans repayer les mêmes décisions lorsque les textes, périodes et contextes sont identiques. La progression, le nombre de fusions et le nombre de comparaisons sont visibles. Aucun appel Ibou n'est effectué ; les appels au modèle peuvent être facturés. Cette passe porte sur tout le corpus même lorsqu'on choisit ensuite un lot de seulement 50 précontrôles.

### Précontrôle avant Ibou

Après redémarrage du serveur, le bouton **1. Précontrôler sans Ibou** analyse les affirmations avec le modèle configuré (choisir OpenAI dans les connexions). Le lot par défaut porte sur 50 affirmations à contrôler ; on peut choisir 200 ou toutes. Cette étape peut consommer des crédits OpenAI, mais ne demande ni clé ni crédit Ibou. Elle consulte le contexte, le catalogue des documents et les références du texte complet.

Chaque affirmation reçoit un parcours persistant et une justification : **corpus local**, **sources citées suffisantes**, **recherche Ibou nécessaire**, **sans vérification factuelle**, ou **à clarifier sans Ibou**. Les métadonnées et descriptions documentaires restent locales même si leur contrôle échoue. Les opinions et éléments sans enjeu factuel ne sont pas déclarés vrais : ils ont un état distinct « Sans contrôle factuel ». Un fait externe n'est jamais prouvé simplement parce que le corpus l'affirme.

Les sources choisies sont limitées aux URL citées (jusqu'à cinq), consultées directement et évaluées avant d'autoriser une recherche externe. Les échecs sont visibles dans l'historique. Les PDF restent non pris en charge. Le contexte fourni au modèle est borné (huit occurrences, catalogue de 500 versions, cinq documents associés, textes locaux tronqués après 60 000 caractères) : une absence dans un extrait tronqué ne permet pas de conclure à une erreur.

Le bouton **2. Budgéter les recherches restantes** affiche uniquement les affirmations explicitement orientées vers Ibou. Tant que le précontrôle n'est pas fini, ce budget est provisoire. Une seule recherche de passages ciblée est autorisée par affirmation : **au maximum 3 unités**, moins si le cache est réutilisé. Les escalades automatiques vers `/page` et les reformulations payantes sont supprimées. Une preuve non concluante conduit à la revue humaine. Un plafond de zéro effectue le précontrôle sans recherche Ibou.

Les anciens verdicts automatiques sans précontrôle sont réexaminés lors du prochain lancement ; leurs décisions précédentes restent dans `checks`. Les exclusions et décisions humaines sont conservées. Les anciennes propositions non appliquées deviennent des conflits après réexamen. Aucune correction fondée sur un ancien verdict sans précontrôle ne peut être générée. Les corrections de métadonnées locales restent à examiner manuellement. Pour forcer un nouveau contrôle d'une affirmation : **Remettre à vérifier**, justifier, puis relancer le précontrôle. Une reprise réutilise les décisions enregistrées ; l'ajout d'une occurrence invalide le précontrôle de l'affirmation concernée.

Une citation exacte ne suffit plus : le verdict structuré doit confirmer le même sujet et un soutien ou une contradiction explicite. Une absence de mention ou une preuve hors sujet impose une revue. Cela renforce les garde-fous sans garantir l'exactitude sémantique du modèle : les tests de régression utilisent des API simulées et doivent être complétés par l'examen d'un échantillon réel.

### Tout lancer

Le bouton **Tout lancer** (ou **Reprendre le parcours** après interruption) enchaîne : test réel de génération JSON et des embeddings, extraction/dédoublonnage, précontrôle du corpus et des sources citées, estimation des seules recherches nécessaires, recherche sous plafond, puis préparation facultative des propositions de correction. Le plafond est saisi avant le lancement. L’estimation exacte dépend des affirmations extraites ; elle est consignée avant les appels de recherche Ibou. Les appels OpenAI de test et d’extraction peuvent être facturés dès la première étape.

Les corrections ne sont pas appliquées automatiquement. Après application manuelle, utiliser de nouveau **Tout lancer** pour extraire et vérifier les nouvelles versions. Une reprise conserve les travaux terminés mais ouvre une nouvelle campagne de budget. Les étapes détaillées ci-dessous restent disponibles séparément.

### Diagnostic des modèles

### Chunks bloqués et périmètre de vérification

Chaque chunk reçoit maintenant le catalogue des liens de la section **Sources**, **Références** ou **Bibliographie** de la version complète de l’article. En l’absence d’un tel titre, les liens du texte complet sont proposés. Les références abrégées présentes dans le chunk peuvent ainsi être reliées à des URL en fin d’article ; seules les URL effectivement présentes sont conservées dans les occurrences.

Après une citation introuvable, le script classe les références par correspondance lexicale avec le chunk et ses marqueurs de citation, consulte jusqu’à trois sources candidates puis fournit leur texte à la tentative de réparation. Les réponses sont mises en cache 24 heures ; les échecs 5 minutes. Les sources HTML/texte peuvent être récupérées, les PDF et autres formats non pris en charge restent signalés comme indisponibles. Les accès directs aux sources ne consomment pas d’unités Ibou ; la réparation LLM peut consommer des crédits du modèle.

Une source externe reste une preuve candidate : elle ne peut pas remplacer la citation exacte de localisation dans le chunk ni rendre l’affirmation validée pendant l’extraction. Si la réparation ne trouve toujours pas de citation dans le chunk, le brouillon reste à corriger. Le dialogue de réparation affiche le catalogue complet et l’état de récupération des liens consultés.

Lors du précontrôle, le catalogue des sources du texte entier est fourni au modèle, y compris pour les anciennes occurrences sans URL associée. Jusqu’à cinq sources citées sont consultées avant une recherche Ibou ; leur cache est réutilisé. Aucune réextraction nécessaire.

La section **Chunks à corriger** affiche les brouillons invalides et le texte source. Trois actions sont disponibles : **Corriger le brouillon** (modifier l’affirmation, sa citation exacte, sa période ou retirer une entrée), **Réextraire ce chunk**, et **Relancer les chunks en attente**. Enregistrer un brouillon validé reprend uniquement ce chunk ; la source originale n’est pas modifiée. La réextraction et le dédoublonnage peuvent utiliser le LLM et les embeddings.

Les différences limitées aux espaces, retours à la ligne et apostrophes/guillemets typographiques sont rapprochées automatiquement, en conservant comme citation le passage exact du texte original. Les paraphrases, différences de nombres et négations ne sont jamais acceptées par ce rapprochement. Une extraction invalide entraîne une seule tentative automatique de réparation ; si elle échoue, son brouillon est conservé et les autres chunks sont traités. Les erreurs opérationnelles API suspendent toujours le traitement.

Les extractions nouvelles ciblent les faits de fond et demandent au modèle d’ignorer les métadonnées éditoriales (titre, catégorie, nombre de mots, slug). Certaines métadonnées encore extraites sont exclues automatiquement selon des règles textuelles prudentes. Le filtre **Métadonnées probables** permet de retrouver et examiner aussi les anciennes affirmations, sans les exclure rétroactivement à votre insu.

Dans **Affirmations**, sélectionner une page, tous les résultats du filtre ou quelques lignes, puis choisir **Exclure la sélection** ou **Réintégrer la sélection**. Les exclusions sont réversibles, conservées dans SQLite et n’effacent pas les verdicts ni les occurrences. Les affirmations exclues ne comptent plus dans l’estimation et ne passent plus par la vérification ou les propositions de correction. Le filtre **Exclues** les retrouve ; le détail d’une affirmation permet aussi une action individuelle.

Le parcours complet propose une pause après une nouvelle extraction pour sélectionner les affirmations avant toute recherche Ibou. Cette option est cochée par défaut et peut être désactivée dans la fenêtre de lancement. Après sélection, reprendre le parcours réutilise l’extraction terminée. Les échecs de l’ancienne version ne possèdent pas de brouillon enregistré : relancer les chunks en attente une première fois pour obtenir les nouveaux diagnostics et brouillons.

### Test de connexion

**Enregistrer & lister les modèles** vérifie seulement l’accès à `/models`. **Tester réellement les modèles** effectue une courte génération JSON et un embedding si activé ; son résultat apparaît dans le bandeau et le journal. Le parcours complet inclut automatiquement ce test avant l’analyse du corpus.

Le connecteur OpenAI utilise désormais Responses avec un schéma JSON strict propre à chaque opération et un message utilisateur explicite. Il conserve le modèle sélectionné, notamment `gpt-6-luna`. Les erreurs HTTP présentent le service, l’endpoint, le modèle, le message fournisseur et le paramètre/code éventuel, après masquage des clés. Les anciennes erreurs génériques ne peuvent pas être reconstituées rétroactivement.

Après mise à jour du serveur : fermer sa fenêtre, relancer `demarrer.cmd`, recharger la page puis renseigner les clés et modèles (conservés uniquement en mémoire). Les contenus et chunks déjà importés restent dans SQLite. Les tests de cette mise à jour utilisent des réponses API simulées ; ils ne prouvent pas l’accès au modèle dans votre compte.

- **Importer** une URL publique HTML ou un texte collé, avec date de publication facultative. Importer à nouveau la même URL avec un texte différent crée une nouvelle version.
- **Extraire & dédoublonner** : calcul des embeddings des contenus (moyenne des vecteurs de tous les chunks), extraction d’affirmations atomiques, citations exactes, dédoublonnage textuel puis rapprochement des affirmations par embeddings et confirmation LLM. Les périodes différentes ne sont pas fusionnées. Les citations générées qui ne correspondent pas au texte empêchent la finalisation du chunk.
- **Estimer** : le budget porte sur les recherches explicitement autorisées par le précontrôle (0 à 3 unités chacune). Avant la fin du précontrôle, ce montant est provisoire. Le plafond est contrôlé avant chaque appel ; le solde est consulté uniquement si une recherche est nécessaire.
- **Vérifier** : précontrôle du contenu et des sources citées, puis une seule recherche de passages Ibou si nécessaire. Un verdict validé/correction exige une citation exacte de preuve. Les sources du même hôte que le contenu audité sont exclues des preuves utilisées pour le verdict. Une erreur API suspend le traitement ; elle ne rend pas l’affirmation fausse.
- **Revoir** : ouvrir une affirmation pour consulter ses occurrences, preuves et justification. Une décision humaine demande une justification et laisse une trace.
- **Corriger** : préparer les propositions à partir des preuves existantes. Le fait corrigé est évalué à nouveau avant la réécriture. Examiner puis appliquer chaque proposition : cela crée une nouvelle version locale. **Relancer l’extraction puis la vérification avec un nouveau budget** pour contrôler tous les faits des passages réécrits. Les versions précédentes et leurs verdicts restent présents.
- **Exporter** : JSON avec contenus, versions, carte URL/chunk/affirmation, preuves, décisions et corrections. Aucune clé exportée.

## Budget et reprise

### Import groupé de textes

Le bouton **Importer un lot de textes** permet de sélectionner plusieurs fichiers `.txt` UTF-8 (un fichier = un contenu) ou de coller plusieurs textes séparés par une ligne contenant exactement `===CONTENU===`. Les deux méthodes peuvent être combinées dans un même lot. Les paragraphes et lignes vides restent dans leur contenu.

Avant import, la liste affiche chaque titre, un extrait et la taille. Les titres sont modifiables et chaque entrée peut être retirée. Limites : 100 contenus, 300 000 caractères par contenu, 5 millions de caractères par lot. Les fichiers invalides ou vides sont signalés avant l’envoi. Le serveur valide l’ensemble du lot avant de commencer les écritures.

L’import s’exécute en arrière-plan avec progression et suspension. La liste reste dans la fenêtre pendant la session : après une interruption, relancer la même liste réutilise les identifiants sans créer de doublons. Après rechargement du navigateur, cette liste n’est pas conservée ; les contenus déjà importés restent en base. Un nouvel ajout des mêmes fichiers constitue un nouveau lot.

L’import est local, sans appel LLM ou Ibou. Lancer ensuite **Extraire & dédoublonner** pour analyser tous les contenus en attente. Redémarrer le serveur après mise à jour de l’application.

Les tarifs Ibou sont ceux de la capture fournie : URL 1 unité, résultats 2, passage 3, page 5. Les appels sont espacés d’au moins 360 ms dans cette instance. La limite étant partagée par toutes les clés de l’organisation, les autres applications peuvent provoquer des réponses 429 ; le délai `Retry-After` est respecté avec trois tentatives maximum.

Le budget est réservé atomiquement dans SQLite avant l’appel. Les refus documentés 401/402/429 sont remboursés dans le compteur local. Après une erreur réseau ou un autre échec, le coût reste compté comme **incertain**, car un appel a pu être facturé. Aucun réessai automatique sur ces erreurs : corriger la cause puis relancer explicitement. Le cache de recherches réussies est valable 24 heures.

Un plafond concerne une campagne. Reprendre une vérification lance une nouvelle campagne avec le plafond saisi ; le journal conserve toutes les campagnes et leurs consommations. Les affirmations disposant d’un précontrôle à jour ne sont pas retraitées ; les anciens verdicts sans précontrôle sont réexaminés. Une affirmation interrompue reste en attente et réutilise les preuves et résultats disponibles. Après redémarrage, renseigner de nouveau les connexions puis relancer l’étape inachevée.

**Les unités Ibou ne comprennent pas les coûts de génération et d’embeddings OpenAI.** L’extraction peut donc être payante avant l’estimation précise Ibou. Aucun prix monétaire de modèle n’est présumé. LM Studio permet d’exécuter cette partie localement.

## Données et clés

- SQLite : `data/atelier.sqlite3`. Sauvegarder ce fichier lorsque le serveur est arrêté.
- Clés uniquement en mémoire serveur ; les champs du navigateur sont vidés après enregistrement. Elles ne sont ni renvoyées par l’API, ni écrites dans SQLite ou dans un fichier de configuration.
- Variables d’environnement facultatives : `OPENAI_API_KEY`, `IBOU_API_KEY`, `FACTCHECK_DB`, `PORT`.
- Les paramètres de modèle sont également à renseigner après redémarrage.
- Tables : `pages`, `versions`, `chunks`, `claims`, `occurrences`, `evidence`, `checks`, `screenings`, `calls`, `budgets`, `patches`, `events`. Vue SQL : `site_map`.
- Identifiants externes de passages Ibou séparés des identifiants de chunks du corpus.
- Le partage réseau héberge les fichiers du projet. Pour un gros corpus ou plusieurs processus, déplacer la base sur un disque local via `FACTCHECK_DB` ; ne pas lancer plusieurs serveurs sur la même base réseau.

## Périmètre de cette version

L’import porte sur les URLs ajoutées et les textes collés : pas de crawler de site/sitemap, de navigateur pour pages rendues en JavaScript, ni d’extraction PDF. La carte globale couvre le corpus importé. Les embeddings de contenus sont calculés et leurs proximités figurent dans l’export ; les affirmations sont comparées globalement pour ne pas manquer un fait partagé par des pages de thèmes différents.

Les corrections restent locales ; aucun CMS n’est connecté. La boucle de revérification est déclenchée explicitement après application pour garder la maîtrise du budget. Si les preuves restent insuffisantes, remettre l’affirmation en attente pour une nouvelle recherche ou conserver une revue humaine. La détection de sources copiées, l’expiration automatique des verdicts, la mise à jour automatique de dates et la vérification exhaustive d’archives ne sont pas implémentées.

Les verdicts automatiques restent des évaluations fondées sur les passages collectés. Une citation exacte est nécessaire mais ne garantit pas à elle seule la qualité de la source ou l’absence d’erreur d’interprétation. Le dédoublonnage sémantique utilise un seuil conservateur de 0,87 et trois candidats de même période ; il ne garantit pas la fusion de toutes les paraphrases.

## Validation

```powershell
py -3 -B -m unittest discover -s tests -v
node --check app/app.js
```

Tests locaux sans accès payant : couverture des chunks, versionnement, dédoublonnage daté, citations inventées, validation de preuves, source circulaire, budget concurrent, cache, crédits épuisés, erreur réseau, corrections, absence de clés côté client. Les API sont simulées dans les tests. Les connexions réelles doivent être testées avec vos clés et vos modèles. Le contrôle HTTP de l’interface et de SQLite a réussi ; l’outil de navigateur intégré était indisponible pour une inspection visuelle automatisée.

Références d’intégration : [OpenAI Responses](https://developers.openai.com/api/docs/guides/text), [OpenAI embeddings](https://developers.openai.com/api/docs/guides/embeddings), [LM Studio compatible OpenAI](https://lmstudio.ai/docs/developer/openai-compat). Ibou : capture de documentation fournie dans la conversation.


## Sélection des preuves par affirmation (15/11/2026)

M5 effectue désormais une sélection sémantique du contenu de chaque passage
pour chaque fait atomique, entre la présélection par pertinence et le verdict.
Le modèle doit identifier le sujet, la relation, la période et le périmètre exacts,
fournir une citation présente dans le passage et motiver sa décision. Une simple
proximité thématique, une réponse partielle ou une absence de réponse ne valide
ni ne contredit une affirmation. Une sélection indisponible ne retient aucune preuve.

Les citations sélectionnées sont ensuite contrôlées par NLI selon la règle de
double passe configurée. Chaque fait doit satisfaire séparément les exigences
de corroboration. Un fait non couvert empêche de confirmer ou corriger tout le
groupe. Les contradictions à date égale et les contradictions internes à une
source empêchent une confirmation. Les compléments doivent vérifier une relation
sur le sujet, et non seulement la présence de leur propre citation.

Le détail d'un appel Ibou affiche le motif et la citation pour chaque affirmation.
`evidence.kept` reste une présélection de candidats ; les décisions finales par
fait figurent dans `evidence.extracted` (type `selection`, champ `retenue`).
Les faits corrigés conservent le mécanisme d'historique et les propositions M6.
Aucun résultat existant n'est automatiquement requalifié par la mise à jour du code.
Redémarrer le serveur pour charger cette version ; les connexions conservées
uniquement en mémoire devront être renseignées à nouveau.


## Réutilisation des analyses M5 (16/11/2026)

Les sélections sont mémorisées par contenu exact (affirmation, sujet, relation,
période, périmètre, passage), consigne, modèle et base. Les erreurs ne sont pas
mémorisées. La décision finale et ses critères restent recalculés : le cache
conserve la sélection initiale, avant le contrôle NLI. Les identifiants et les
sources sont toujours ceux du passage courant, même si le texte est identique.

Les sélections terminées se rechargent depuis le journal de preuves existant.
Les anciens journaux OpenAI de M5 v2 sont également récupérés lorsque les entrées
sont complètes, le modèle identique et la citation valide. Les aperçus tronqués
ne sont jamais réutilisés. Une interruption peut donc encore nécessiter de
recalculer les analyses qui ne disposent pas d'une trace complète.

Les contrôles NLI identiques et les embeddings API identiques disposent de caches
bornés en mémoire. Un changement de modèle ou de contenu invalide les entrées.
Le contrôle de double passe reste appliqué aux NLI réutilisés. La progression
indique la sélection des preuves, les paires traitées et réutilisées, puis le
contrôle des citations. Le compteur principal reste celui des requêtes entièrement
traitées, et non le nombre d'appels Ibou.

Le stockage sur le NAS et le mécanisme des écritures restent inchangés.
Le processus déjà lancé doit être redémarré pour charger le nouveau code ; ses
clés en mémoire devront être renseignées de nouveau. Les gains dépendent du
nombre de paires réellement identiques ; aucun délai global n'est garanti.


## Résultats Ibou reçus en réserve (16/11/2026)

Chaque recherche M5 demande dès le départ 20 résultats (paramètre existant
`m5.max_results_critique`, au moins `m5.max_results`). Seuls les 10 premiers
(`m5.max_results`) sont téléchargés et analysés initialement. Si le verdict
reste insuffisant ou conflictuel, les rangs 11 à 20 déjà enregistrés sont
analysés, sans deuxième appel Ibou. Cela s'applique aussi aux recherches de
contrôle et aux reformulations. Les groupes déjà résolus ne sont pas réévalués.

Le filtrage des copies porte sur l'ensemble des résultats examinés ; seuls les
nouveaux passages sont ajoutés lors de l'élargissement. Le compteur `elargies`
désigne désormais une extension locale de l'analyse, sans consommation Ibou.
Les requêtes de contrôle et reformulations restent des recherches distinctes
et peuvent consommer des crédits. Les réponses de moins de 11 résultats ne
provoquent pas d'élargissement local. Aucun réglage du stockage NAS n'est changé.

Cette modification doit être chargée par un redémarrage du serveur. Elle ne
modifie pas le code déjà chargé par un traitement en cours.
