"""Acces au serveur, schemas LLM de la chaine et executions rejouables.

Contrat : docs/CONTRAT.md section 2. Ce module n'importe jamais ``server`` :
le serveur est fourni par ``chaine.brancher(module)``.
"""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

_ETAT = {'serveur': None}
_SCHEMAS = {}
_VERROU = threading.RLock()

STATUTS_EXECUTION = ('en_cours', 'terminee', 'erreur', 'interrompue')


# ---------------------------------------------------------------------------
# Branchement
# ---------------------------------------------------------------------------

def brancher(module_serveur):
    """Enregistre le module serveur. Idempotent ; renvoie le module."""
    if module_serveur is None:
        raise ValueError('Module serveur absent.')
    manquants = [nom for nom in ('query', 'uid', 'now', 'LOCK', 'DB', 'CTX') if not hasattr(module_serveur, nom)]
    if manquants:
        raise ValueError('Module serveur incomplet : ' + ', '.join(manquants))
    with _VERROU:
        _ETAT['serveur'] = module_serveur
    return module_serveur


def srv():
    """Renvoie le module serveur branche ; leve RuntimeError sinon."""
    serveur = _ETAT['serveur']
    if serveur is None:
        raise RuntimeError('Chaine non branchee : appelez chaine.brancher(module_serveur).')
    return serveur


def est_branche():
    """Vrai si un serveur est branche."""
    return _ETAT['serveur'] is not None


@contextmanager
def connexion():
    """Connexion SQLite transactionnelle sous le verrou du serveur.

    Une seule transaction pour tout le bloc (commit a la sortie, rollback
    sur exception). Lignes en ``sqlite3.Row``. A utiliser pour les ecritures
    par lots ; pour une requete isolee, preferer ``S.query``.
    """
    serveur = srv()
    ouverte = getattr(serveur.CTX, 'db', None)
    if ouverte is not None:
        # Bloc imbrique : meme transaction, validee ou annulee par le bloc exterieur.
        yield ouverte
        return
    with serveur.LOCK:
        base = sqlite3.connect(serveur.DB, timeout=30)
        base.row_factory = sqlite3.Row
        # Tant que le bloc est ouvert, ``S.query`` de ce thread passe par cette connexion : une
        # seconde connexion se bloquerait contre le verrou de la transaction (« database is locked »
        # des que des lignes volumineuses font deborder le cache SQLite avant le commit).
        serveur.CTX.db = base
        try:
            with base:
                yield base
        finally:
            serveur.CTX.db = None
            base.close()


_REGLAGES = {'base': None, 'instant': 0.0, 'valeurs': {}}


def reglage(cle, defaut=None):
    """Valeur texte de ``settings.key`` (cache de 2 s), ou ``defaut``.

    Sans serveur branche ou sans table ``settings`` : ``defaut``.
    """
    if not est_branche():
        return defaut
    serveur = srv()
    base = str(serveur.DB)
    if _REGLAGES['base'] != base or time.monotonic() - _REGLAGES['instant'] > 2.0:
        try:
            lignes = serveur.query('SELECT key,value FROM settings')
        except sqlite3.Error:
            lignes = []
        _REGLAGES['valeurs'] = {ligne['key']: ligne['value'] for ligne in lignes}
        _REGLAGES['base'] = base
        _REGLAGES['instant'] = time.monotonic()
    valeur = _REGLAGES['valeurs'].get(cle)
    if valeur is None or valeur == '':
        return defaut
    return valeur


def definir_reglage(cle, valeur):
    """Ecrit ``settings.key`` (``None`` supprime la cle) et vide le cache."""
    serveur = srv()
    if valeur is None:
        serveur.query('DELETE FROM settings WHERE key=?', (cle,))
    else:
        serveur.query('INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)', (cle, str(valeur)))
    vider_reglages()


def vider_reglages():
    """Oublie le cache des reglages."""
    _REGLAGES['base'] = None
    _REGLAGES['instant'] = 0.0
    _REGLAGES['valeurs'] = {}


def colonnes(table):
    """Ensemble des colonnes d'une table (vide si la table n'existe pas)."""
    lignes = srv().query('SELECT name FROM pragma_table_info(?)', (table,))
    return {ligne['name'] for ligne in lignes}


# ---------------------------------------------------------------------------
# Schemas de sortie structuree (section 2.1)
# ---------------------------------------------------------------------------

def objet(proprietes):
    """Schema JSON strict d'un objet : tous les champs requis, aucun champ en plus."""
    return {
        'type': 'object',
        'properties': proprietes,
        'required': list(proprietes),
        'additionalProperties': False,
    }


def tableau(elements):
    """Schema JSON d'un tableau."""
    return {'type': 'array', 'items': elements}


def enum(valeurs):
    """Schema JSON d'une chaine a valeurs fermees."""
    return {'type': 'string', 'enum': list(valeurs)}


def schema(mode, definition):
    """Enregistre le schema JSON strict d'un mode ``chaine:<nom>``.

    ``definition`` est un dict (schema fixe) ou une fonction ``payload -> dict``
    (schema dependant de la charge, par exemple un enum d'identifiants).
    Reenregistrer un mode remplace sa definition. Renvoie la definition.
    """
    if not isinstance(mode, str) or not mode.startswith('chaine:'):
        raise ValueError('Le mode doit commencer par « chaine: » : ' + repr(mode))
    if not (isinstance(definition, dict) or callable(definition)):
        raise TypeError('Le schema doit etre un dict ou une fonction payload -> dict.')
    with _VERROU:
        _SCHEMAS[mode] = definition
    return definition


def schema_pour(payload):
    """Schema JSON a imposer au LLM pour ``payload['mode']`` (mode ``chaine:``).

    Appele par ``server.output_schema`` (lot integration). Leve KeyError si le
    mode n'est pas enregistre.
    """
    mode = payload.get('mode') if isinstance(payload, dict) else None
    definition = _SCHEMAS.get(mode)
    if definition is None:
        raise KeyError('Schema inconnu pour le mode ' + repr(mode))
    if callable(definition):
        return definition(payload)
    return definition


def modes():
    """Liste des modes enregistres."""
    return sorted(_SCHEMAS)


def valider(donnees, definition, chemin='$'):
    """Controle leger d'un JSON contre un schema (sous-ensemble de JSON Schema).

    Gere type, properties, required, additionalProperties, items, enum,
    minimum, maximum. Renvoie la liste des problemes (vide si conforme).
    """
    problemes = []
    attendu = definition.get('type')
    types = {
        'object': dict,
        'array': list,
        'string': str,
        'boolean': bool,
        'integer': int,
        'number': (int, float),
    }
    if attendu in types:
        valide = isinstance(donnees, types[attendu])
        if attendu in ('integer', 'number') and isinstance(donnees, bool):
            valide = False
        if not valide:
            problemes.append(f'{chemin} : type {attendu} attendu')
            return problemes
    if 'enum' in definition and donnees not in definition['enum']:
        problemes.append(f'{chemin} : valeur hors liste ({donnees!r})')
    if attendu in ('integer', 'number'):
        if 'minimum' in definition and donnees < definition['minimum']:
            problemes.append(f'{chemin} : inferieur au minimum')
        if 'maximum' in definition and donnees > definition['maximum']:
            problemes.append(f'{chemin} : superieur au maximum')
    if attendu == 'object':
        proprietes = definition.get('properties', {})
        for cle in definition.get('required', []):
            if cle not in donnees:
                problemes.append(f'{chemin}.{cle} : champ manquant')
        for cle, valeur in donnees.items():
            if cle in proprietes:
                problemes.extend(valider(valeur, proprietes[cle], f'{chemin}.{cle}'))
            elif definition.get('additionalProperties') is False:
                problemes.append(f'{chemin}.{cle} : champ non prevu')
    if attendu == 'array' and 'items' in definition:
        for index, element in enumerate(donnees):
            problemes.extend(valider(element, definition['items'], f'{chemin}[{index}]'))
    return problemes


FOURNISSEURS_LOCAUX = ('lmstudio',)
REGLAGE_LLM_TIERS = 'chaine:llm_tiers_autorise'


def _vrai(valeur):
    """Valeur de reglage interpretee comme booleen (1, true, oui)."""
    return str(valeur or '').strip().lower() in ('1', 'true', 'oui', 'vrai', 'yes')


def type_fournisseur(fournisseur=None):
    """``local`` (LM Studio, sur le poste) ou ``tiers`` (API distante : OpenAI, OpenRouter) pour le fournisseur configure."""
    nom = fournisseur if fournisseur is not None else srv().CONFIG.get('provider')
    return 'local' if nom in FOURNISSEURS_LOCAUX else 'tiers'


def llm_tiers_autorise():
    """Vrai si le reglage ``chaine:llm_tiers_autorise`` permet aux etapes « LLM local » d'utiliser un LLM tiers."""
    return _vrai(reglage(REGLAGE_LLM_TIERS, '0'))


def raison_llm_local():
    """Raison qui empeche un appel « LLM local » de la chaine, ou None.

    Hierarchie des moyens : un LLM tiers payant n'intervient qu'en dernier recours ; les etapes
    de la chaine qui demandent un LLM local refusent donc un fournisseur tiers, sauf reglage explicite.
    """
    if type_fournisseur() == 'tiers' and not llm_tiers_autorise():
        return ('le fournisseur LLM configuré (' + str(srv().CONFIG.get('provider') or '') + ') est un LLM tiers (API distante) : '
                'les étapes « LLM local » de la chaîne ne l\'utilisent pas ; configurez LM Studio dans les connexions '
                'ou autorisez explicitement le réglage ' + REGLAGE_LLM_TIERS)
    return None


def llm(tache, payload):
    """Appelle ``S.llm`` (LLM local) puis revalide la sortie contre le schema du mode.

    Double passe « controle de schema » du document. Leve
    ``S.OperationalError`` si la sortie n'est pas conforme, ou si le
    fournisseur est tiers sans ``chaine:llm_tiers_autorise`` (``raison_llm_local``).
    """
    serveur = srv()
    refus = raison_llm_local()
    if refus:
        raise serveur.OperationalError(refus)
    definition = schema_pour(payload)
    resultat = serveur.llm(tache, payload)
    problemes = valider(resultat, definition)
    if problemes:
        raise serveur.OperationalError('Sortie LLM non conforme au schema ' + payload['mode'] + ' : ' + '; '.join(problemes[:5]))
    return resultat


def moteur_llm():
    """Identifiant du LLM configure, par exemple ``llm:lmstudio:qwen3-8b``."""
    config = srv().CONFIG
    return 'llm:' + str(config.get('provider') or '') + ':' + str(config.get('model') or '')


# ---------------------------------------------------------------------------
# Executions rejouables (section 2.2)
# ---------------------------------------------------------------------------

_LOCAL = threading.local()


def _pile():
    """Executions ouvertes dans ce thread (la derniere est la courante)."""
    if not hasattr(_LOCAL, 'pile'):
        _LOCAL.pile = []
    return _LOCAL.pile


def noter_moteur(nom, identifiant):
    """Note sur l'execution courante le moteur reellement utilise (ex. ``resultat['engine']`` du NLI)."""
    pile = _pile()
    if pile and identifiant:
        pile[-1].moteur(nom, identifiant)


@contextmanager
def parametres(**valeurs):
    """Parametres de lancement (``aujourdhui``, ``budget``...) enregistres dans ``step_runs.params``
    de chaque execution ouverte dans le bloc (exigence « tout est rejouable »)."""
    precedent = getattr(_LOCAL, 'parametres', None)
    fusion = dict(precedent or {})
    fusion.update({cle: valeur for cle, valeur in valeurs.items() if valeur is not None})
    _LOCAL.parametres = fusion
    try:
        yield fusion
    finally:
        _LOCAL.parametres = precedent


def parametres_courants():
    """Parametres de lancement en vigueur dans ce thread (dict, eventuellement vide)."""
    return dict(getattr(_LOCAL, 'parametres', None) or {})


class Execution:
    """Ligne ``step_runs`` en cours. ``id`` va dans la colonne ``run_id`` des lignes produites."""

    def __init__(self, nom, moteurs, prompt, seuils, pages, items):
        self.id = srv().uid()
        self.etape = nom
        self.moteurs = dict(moteurs or {})
        self.prompt = prompt or ''
        self.seuils = seuils
        self.pages = int(pages or 0)
        self.items = int(items or 0)
        self.note = ''
        self.params = {}               # parametres de lancement (step_runs.params)
        self.echec = None              # message : l'etape s'est deroulee sans exception mais a echoue (statut 'erreur')
        self.statut = 'en_cours'
        self.debut = None
        self.fin = None
        self.secondes_locales = 0.0
        self.tokens_entree = 0
        self.tokens_sortie = 0
        self.unites_ibou = 0

    def moteur(self, nom, identifiant):
        """Note le moteur effectivement utilise (ex. renvoye par ``moteurs.nli``)."""
        if identifiant:
            self.moteurs[nom] = identifiant


class etape:
    """Gestionnaire de contexte d'une etape rejouable (contrat 2.2).

    ::

        with noyau.etape('m1.unites', moteurs={'unites': 'llm:qwen3'}, prompt='unites-v1',
                         seuils={...}, pages=n) as run:
            ...                 # run.id dans la colonne run_id des lignes produites
            run.pages += 1

    A l'entree : ligne ``step_runs`` (statut ``en_cours``) et ``S.CTX.step = run.id``.
    A la sortie : ``S.CTX.step`` restaure, couts sommes depuis ``api_log.step``
    (renseigne par ``server.api_log`` depuis ``S.CTX.step`` quand le serveur
    declare ``API_LOG_STEP = True`` ; sinon repli ``marquer_api_log``),
    statut ``terminee`` / ``erreur`` / ``interrompue``. Les exceptions sont propagees.

    ``seuils`` : dict de valeurs, ou liste de noms resolus par ``seuils.get``.
    ``local_seconds`` = duree murale moins la duree cumulee des appels API journalises.
    Etapes imbriquees : chaque etape ne compte que les appels faits hors de ses
    sous-etapes.
    """

    def __init__(self, nom, moteurs=None, prompt='', seuils=None, pages=0, items=0, params=None):
        self.run = Execution(nom, moteurs, prompt, _resoudre_seuils(seuils), pages, items)
        self.run.params = parametres_courants()
        self.run.params.update(params or {})
        self._depart = 0.0
        self._precedent = None

    def __enter__(self):
        serveur = srv()
        if 'step_runs' not in _tables():
            raise RuntimeError('Schema de la chaine absent : appelez chaine.schema.init().')
        run = self.run
        run.debut = serveur.now()
        self._depart = time.monotonic()
        serveur.query(
            'INSERT INTO step_runs(id,step,started,status,engines,prompt,thresholds,pages,items) VALUES(?,?,?,?,?,?,?,?,?)',
            (run.id, run.etape, run.debut, 'en_cours', _json(run.moteurs), run.prompt, _json(run.seuils), run.pages, run.items),
        )
        if run.params and 'params' in colonnes('step_runs'):
            serveur.query('UPDATE step_runs SET params=? WHERE id=?', (_json(run.params), run.id))
        self._precedent = getattr(serveur.CTX, 'step', None)
        serveur.CTX.step = run.id
        _pile().append(run)
        return run

    def __exit__(self, type_exc, exc, trace):
        serveur = srv()
        run = self.run
        serveur.CTX.step = self._precedent
        pile = _pile()
        if run in pile:
            pile.remove(run)
        run.fin = serveur.now()
        duree = time.monotonic() - self._depart
        if exc is None:
            run.statut = 'erreur' if run.echec else 'terminee'
        elif isinstance(exc, getattr(serveur, 'OperationalError', ())) and _arret_demande(serveur):
            run.statut = 'interrompue'
        else:
            run.statut = 'erreur'
        note = run.note
        if exc is None and run.echec:
            note = (note + ' ; ' if note else '') + 'echec : ' + str(run.echec)[:500]
        if exc is not None and not note:
            note = type(exc).__name__ + ' : ' + str(exc)[:500]
        couts = {'tokens_in': 0, 'tokens_out': 0, 'ibou_units': 0, 'api_ms': 0}
        if 'step' in colonnes('api_log'):
            if not getattr(serveur, 'API_LOG_STEP', False):
                # Serveur ancien (api_log sans colonne step) : rattachement par bornes de temps.
                marquer_api_log(run)
            couts = couts_api(run.id)
        else:
            note = (note + ' ; ' if note else '') + 'colonne api_log.step absente : couts non mesures'
        run.secondes_locales = max(0.0, duree - couts['api_ms'] / 1000.0)
        run.tokens_entree = couts['tokens_in']
        run.tokens_sortie = couts['tokens_out']
        run.unites_ibou = couts['ibou_units']
        run.note = note
        serveur.query(
            'UPDATE step_runs SET ended=?,status=?,engines=?,thresholds=?,pages=?,items=?,local_seconds=?,'
            'tokens_in=?,tokens_out=?,ibou_units=?,note=? WHERE id=?',
            (run.fin, run.statut, _json(run.moteurs), _json(run.seuils), int(run.pages), int(run.items),
             round(run.secondes_locales, 3), run.tokens_entree, run.tokens_sortie, run.unites_ibou, note, run.id),
        )
        return False


def marquer_api_log(run):
    """Renseigne ``api_log.step`` sur les lignes creees pendant l'execution ``run``.

    Neutralise depuis le lot integration : ``server.api_log`` ecrit lui-meme
    ``step`` (``S.API_LOG_STEP = True``) et ``etape`` n'appelle plus cette
    fonction. Elle ne sert plus qu'a un serveur qui ne declare pas ce drapeau.

    Ancienne solution provisoire tant que ``server.api_log`` n'ecrivait pas la colonne :
    toutes les lignes ``step IS NULL`` dont ``created`` tombe dans
    [run.debut, run.fin] recoivent ``run.id``. ``api_log`` ne garde pas le
    thread emetteur : un appel fait au meme moment par un autre thread (test de
    connexion depuis l'interface, par exemple) serait rattache a tort. Le
    serveur n'executant qu'un traitement a la fois (``start_job``), ce cas est
    marginal. Les lignes qui portent deja un ``step`` ne sont jamais modifiees,
    donc la fonction devient sans effet une fois l'integration faite.

    Integration propre (lot integration, dans ``server.py``) :

    1. ``api_log`` doit nommer ses colonnes, car l'INSERT positionnel
       (``INSERT INTO api_log VALUES(?,...17)``) echoue des que la colonne
       ``step`` existe, et l'erreur est avalee par ``except sqlite3.Error`` ::

           query('INSERT INTO api_log(id,created,service,endpoint,operation,claim_id,model,status,'
                 'http_status,duration_ms,units,tokens_in,tokens_out,request,response,error,call_id,step) '
                 'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (..., call_id, getattr(CTX,'step',None)))

    2. meme correction pour les ``INSERT INTO evidence VALUES(?,?,?,?,?,?,?)``
       (``collect``, ``screen_claim_scoped``, ``expert_review_scoped``), qui
       echouent apres l'extension de ``evidence``.
    3. ``start_job.run`` remet ``CTX.step=None`` comme il le fait pour ``claim_id``.

    Renvoie le nombre de lignes marquees.
    """
    serveur = srv()
    fin = run.fin or serveur.now()
    with connexion() as base:
        curseur = base.execute(
            'UPDATE api_log SET step=? WHERE step IS NULL AND created>=? AND created<=?',
            (run.id, run.debut, fin),
        )
        return curseur.rowcount


def couts_api(run_id):
    """Somme des couts journalises dans ``api_log`` pour une execution."""
    ligne = srv().query(
        "SELECT COALESCE(SUM(tokens_in),0) tokens_in, COALESCE(SUM(tokens_out),0) tokens_out, "
        "COALESCE(SUM(CASE WHEN service='ibou' THEN units ELSE 0 END),0) ibou_units, "
        "COALESCE(SUM(duration_ms),0) api_ms FROM api_log WHERE step=?",
        (run_id,),
        True,
    )
    return {cle: int(ligne[cle] or 0) for cle in ('tokens_in', 'tokens_out', 'ibou_units', 'api_ms')}


def execution_courante():
    """Identifiant de l'execution en cours dans ce thread, ou None."""
    return getattr(srv().CTX, 'step', None)


def _arret_demande(serveur):
    """Vrai si l'utilisateur a demande l'arret du traitement."""
    arret = getattr(serveur, 'STOP', None)
    return bool(arret is not None and arret.is_set())


def _tables():
    """Noms des tables de la base courante."""
    lignes = srv().query("SELECT name FROM sqlite_master WHERE type='table'")
    return {ligne['name'] for ligne in lignes}


def _resoudre_seuils(seuils_demandes):
    """Accepte un dict de valeurs ou une liste de noms de seuils."""
    if seuils_demandes is None:
        return {}
    if isinstance(seuils_demandes, dict):
        return dict(seuils_demandes)
    from chaine import seuils
    return {nom: seuils.get(nom) for nom in seuils_demandes}


def _json(valeur):
    """Serialisation JSON stable pour stockage."""
    return json.dumps(valeur, ensure_ascii=False, sort_keys=True, default=str)
