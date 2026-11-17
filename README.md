# Clair — Atelier de fact-checking

Clair est un back-office local qui repère, dans un corpus de pages web, les affirmations fausses ou devenues obsolètes, puis propose des corrections à valider par un humain. Il s'appuie sur une chaîne de six méthodologies (M1 à M6), sur un LLM au choix (LM Studio, OpenAI ou OpenRouter) et sur le moteur de recherche [Ibou](https://platform.ibou.com) pour la collecte de preuves.

Le serveur est en Python, sans aucune dépendance obligatoire ; l'interface est en HTML, CSS et JavaScript sans framework ; les données sont dans une base SQLite.

> Outil personnel, mono-utilisateur. Le serveur n'écoute que sur la boucle locale et n'a pas d'authentification : ne l'exposez pas sur Internet.

## Ce que fait la chaîne

| Étape | Rôle |
|---|---|
| M1 | Découper les pages en unités d'information ancrées dans le texte, extraire faits, dates et sujets |
| M2 | Identifier et typer les sources citées |
| M3 | Établir la paternité des informations et repérer celles périmées par leur date |
| M4 | Attribuer les statuts et rédiger les requêtes de vérification sous forme de questions |
| M5 | Prioriser, interroger Ibou, lire les sources et rendre un verdict |
| M6 | Proposer les corrections et les soumettre à la revue humaine |

Deux principes guident l'ensemble : un LLM ne décide jamais seul, et rien n'entre en base sans ancrage dans un texte. Le détail est dans [`docs/METHODOLOGIES.txt`](docs/METHODOLOGIES.txt).

## Démarrage

Prérequis : Python 3.11 ou supérieur (développé avec Python 3.13 sous Windows).

Windows : double-cliquer sur `demarrer.cmd`.

Autres systèmes :

```
python3 server.py
```

Ouvrir ensuite <http://127.0.0.1:8765>, puis « Connexions & modèles » pour renseigner les clés et le modèle. La base `data/atelier.sqlite3` est créée au premier lancement.

## Configuration

Les clés d'API se saisissent dans l'interface. Elles restent en mémoire du serveur : elles ne sont écrites ni en base ni dans un fichier, et ne sont jamais renvoyées par l'API. Elles sont donc à ressaisir après un redémarrage, sauf si elles sont fournies par variables d'environnement.

| Variable | Rôle |
|---|---|
| `OPENAI_API_KEY` | Clé OpenAI |
| `OPENROUTER_API_KEY` | Clé OpenRouter |
| `IBOU_API_KEY` | Clé Ibou Platform |
| `FACTCHECK_DB` | Chemin de la base SQLite (défaut : `data/atelier.sqlite3`) |
| `PORT` | Port d'écoute (défaut : `8765`) |

LM Studio n'a pas besoin de clé : démarrer son serveur local et indiquer son URL (défaut : `http://127.0.0.1:1234/v1`).

## Moteurs locaux (optionnels)

L'outil démarre sans rien installer et fait alors tourner tout ce qui relève des règles. Les étapes qui demandent un modèle s'arrêtent en indiquant lequel manque. Pour activer spaCy, GLiNER, le NLI mDeBERTa, les embeddings et le reranker :

```
pip install -r requirements-local.txt
python -m spacy download fr_core_news_lg
```

Sous Windows, `installer_modeles.cmd` fait l'ensemble et précharge les modèles Hugging Face. Voir [`docs/MOTEURS.md`](docs/MOTEURS.md).

## Organisation du dépôt

| Chemin | Contenu |
|---|---|
| `server.py` | Serveur HTTP, accès SQLite, connexions aux fournisseurs, ancien parcours |
| `chaine/` | Les six méthodologies, le pipeline, les moteurs, le schéma et l'API de la chaîne |
| `app/` | Interface (servie par `server.py`) |
| `tests/` | Tests `unittest` ; LLM, NLI, embeddings et Ibou y sont simulés |
| `docs/` | Méthodologies, routes d'API, contrat de développement, moteurs, notes de version |

## Tests

```
python -B -m unittest discover -s tests
```

Aucun appel réseau, aucune dépendance. Compter environ trois minutes.

## Limites connues

- L'import porte sur des URLs et des textes collés : pas de crawler, pas de rendu JavaScript, pas d'extraction PDF.
- Les corrections restent locales ; aucun CMS n'est connecté.
- Les verdicts automatiques sont des évaluations fondées sur les passages collectés, pas des certitudes : la revue humaine reste l'étape finale.
- Les moteurs à modèles Hugging Face n'ont été validés qu'avec des doubles de test.

## Documentation

- [`docs/METHODOLOGIES.txt`](docs/METHODOLOGIES.txt) : référence fonctionnelle
- [`docs/API.md`](docs/API.md) : routes HTTP
- [`docs/CONTRAT.md`](docs/CONTRAT.md) : schéma de données et signatures
- [`docs/MOTEURS.md`](docs/MOTEURS.md) : moteurs locaux et replis
- [`docs/RELECTURE.md`](docs/RELECTURE.md) : écarts relevés en relecture et corrigés
- [`docs/NOTES-DE-VERSION.md`](docs/NOTES-DE-VERSION.md) : historique détaillé des évolutions

## Références

La décomposition des pages en unités d'information (M1) s'est nourrie de la lecture de l'article suivant. Aucun code n'en est repris.

- Youngeun Nam, Joeun Kim, Hwanjun Song, Susik Yoon, Jae-Gil Lee, Byung Suk Lee, « TimelyRAG: Semantic-Temporal Hybrid Retrieval for Time-Critical Question Answering in Overlapping-Evolving Documents », 2026, [arXiv:2609.11572](https://arxiv.org/abs/2609.11572).

## Licence

[MIT](LICENSE) © 2026 Pierre Calvet
