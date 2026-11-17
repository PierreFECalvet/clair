# Moteurs locaux de la chaîne (`chaine/moteurs.py`)

L'application fonctionne **sans aucun** de ces moteurs : chaque fonction descend la hiérarchie du document (modèle local → LLM contraint ou API → règles) et renvoie l'identifiant du moteur réellement utilisé (dans le résultat, ou via `moteurs.dernier('<fonction>')`).

## Installer

Windows : double-cliquer `installer_modeles.cmd` (utilise `py -3`). Il installe `requirements-local.txt`, le modèle spaCy `fr_core_news_lg` (repli `md`), précharge les modèles Hugging Face puis affiche `moteurs.etat()`.

À la main :
```
py -3 -m pip install -r requirements-local.txt
py -3 -m spacy download fr_core_news_lg
```

## Quel moteur pour quelle fonction

| Fonction | Moteur local (réglage `modele:<clé>`) | Replis, dans l'ordre |
|---|---|---|
| `phrases` | spaCy `fr_core_news_lg` (clé `spacy`, repli auto `md` puis `sm`) ; SaT `sat-3l-sm` (clé `sat`, paquet `wtpsplit`) | règles (abréviations, initiales, décimaux, guillemets, retours à la ligne) |
| `analyse` | spaCy (clé `spacy`) | `None` |
| `entites` | GLiNER `urchade/gliner_multi-v2.1` (clé `entites`) ; spaCy NER + règles numériques | règles (noms propres multi-mots, montants, pourcentages, dates) |
| `nli`, `nli_lot` | mDeBERTa-v3 XNLI `MoritzLaurer/mDeBERTa-v3-base-xnli-multilingual-nli-2mil7` (clé `nli`) ; prémisse plus longue que `seuil:nli.jetons_max` (512) découpée en fenêtres de phrases, fenêtre la plus probante retenue (`fenetres` dans le résultat) | LLM configuré, mode `chaine:nli` ; **jamais de règles** (erreur si rien) ; jamais pour contrôler une sortie du LLM sans réglage (voir « Double passe ») |
| `embeddings` | sentence-transformers `Qwen/Qwen3-Embedding-0.6B` (clé `embeddings` ; `BAAI/bge-m3` en comparaison) | `S.embed` (fournisseur des connexions) ; sinon `([], 'aucun')` |
| `reordonner` | cross-encoder `BAAI/bge-reranker-v2-m3` (clé `reordonner`) | cosinus des embeddings ; recouvrement lexical pondéré idf |
| `perplexite` | modèle causal `asi/gpt-fr-cased-small` (clé `perplexite`) | `None` |
| `classer` | SetFit sur `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` (clé `classer`) | plus proche centroïde sur embeddings ; centroïde lexical tf-idf ; LLM `chaine:classer` **en dernier** (règles avant LLM : il ne sert que sans exemples) |

Les échelles de score diffèrent selon le moteur (`reordonner` : sigmoïde, cosinus ou part lexicale) : calibrer les seuils par moteur.

## LLM local ou tiers, double passe (comportement par défaut)

Le document impose la hiérarchie règles → modèles locaux → LLM local → LLM tiers (dernier recours) et la **double passe** : toute sortie générative est revérifiée par un *autre* mécanisme.

- **Type de fournisseur** (connexions) : `lmstudio` = **local**, `openai` = **tiers** (API payante). Toutes les étapes « LLM local » de la chaîne (unités option C, faits, zone grise de la déduplication et de M3, question fermée de M2, reformulation M4, extraction M5, réécriture M6, NLI et classement de repli) **refusent un fournisseur tiers** avec une raison claire (`/api/chaine/etat` : `prerequis`, `moteurs.llm.raison`). L'option d'extraction automatique retombe sur A (règles).
  Réglage `chaine:llm_tiers_autorise` = `1` (table `settings`) : autorise explicitement le LLM tiers pour ces étapes.
- **NLI distinct du LLM** : quand le seul NLI disponible est le LLM lui-même (mDeBERTa non installé), contrôler une sortie de ce LLM (unités option C, faits, valeur corrigée ou inconnue lue par M5, réécriture M6) est refusé : `m1.unites` (option C) et `m1.faits` (extracteur LLM) s'arrêtent avec la raison « double passe illusoire », M5 ne corrige pas automatiquement (`conflictuelle`, raison « correction non vérifiable ») et ne complète pas, M6 marque la réécriture « à rédiger ». Les autres usages du NLI (déduplication, qualification M3, verdict M5, interprétations M2) ne contrôlent pas une sortie du LLM et gardent le repli NLI par LLM.
  Réglage `chaine:nli_par_llm` = `1` : autorise ce contrôle par le même LLM ; l'exécution porte alors `double_passe: "faible"` dans `step_runs.engines` (sinon `"forte"`). `moteurs.etat()['nli']['double_passe']` : `forte`, `faible`, `refusee` ou `impossible`.
- Tests : la configuration simulée doit être **déclarée** (`patch.object(moteurs, 'nli_prevu', return_value='test:nli')` : un NLI local distinct du LLM), sinon les étapes ci-dessus s'arrêtent comme sur un poste sans mDeBERTa.

```python
noyau.definir_reglage('chaine:llm_tiers_autorise', '1')   # accepter OpenAI pour les étapes « LLM local »
noyau.definir_reglage('chaine:nli_par_llm', '1')          # accepter une double passe faible
moteurs.raison_double_passe()                              # None, ou la raison du refus
```

## Forcer un repli

Table `settings` :
- `moteur:<fonction>` = `auto` (défaut) | `local` | `llm` | `regles` — niveau le plus haut autorisé. `llm` saute les modèles locaux, `regles` ne garde que les règles (pour `nli`, qui n'a pas de règles, le réglage est ignoré).
- `modele:<clé>` = nom du modèle (ex. `modele:spacy` = `fr_core_news_md`).

```python
from chaine import noyau, moteurs
noyau.definir_reglage('moteur:entites', 'regles')
moteurs.reinitialiser()      # oublie modèles chargés et échecs mémorisés
moteurs.etat()               # actif, disponibles, modèle, version, raison du repli
```

`etat()` ne charge aucun modèle : il vérifie la présence des paquets, des modèles spaCy et (si `huggingface_hub` est installé) du modèle dans le cache Hugging Face, plus les échecs de chargement déjà constatés.


## OpenRouter (LLM tiers)

Fournisseur disponible dans « Moteurs et réglages » › Connexions. La clé est saisie dans l'interface et gardée en mémoire le temps de la session (ou lue dans la variable d'environnement `OPENROUTER_API_KEY`) ; elle n'est écrite ni dans la base ni dans un fichier.

- API compatible OpenAI (`https://openrouter.ai/api/v1/chat/completions`). La sortie est contrainte par schéma JSON strict ; si le modèle le refuse (HTTP 400, 404 ou 422), repli sur le mode JSON simple pour la session, noté dans le journal. La conformité est alors contrôlée après coup par `noyau.llm`.
- « Enregistrer & lister les modèles » liste d'abord les modèles gratuits (`:free`) qui acceptent la sortie par schéma, et affiche le quota quotidien restant des modèles gratuits.
- OpenRouter est un LLM **tiers** : les étapes « LLM local » de la chaîne ne l'utilisent que si le réglage `chaine:llm_tiers_autorise` est activé. Il ne remplace pas le NLI local : sans mDeBERTa, la double passe reste refusée sauf `chaine:nli_par_llm`.
- Pas d'embeddings par ce fournisseur : installer le moteur local.
- Les erreurs 429 (quota ou débit) arrêtent l'étape, qui se reprend là où elle s'est arrêtée.
