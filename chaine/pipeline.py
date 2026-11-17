"""Orchestration de la chaine M1 a M6 (lot integration).

Ordre et dependances (docs/METHODOLOGIES.txt, « Vue d'ensemble » et
« Ordre de test conseille ») ::

    migration (une fois, puis reprise des nouvelles affirmations de l'ancien parcours)
    -> m1.page    segmentation, contexte, ancrage, classes d'obsolescence, peremption
    -> m1.unites  unites d'information des versions sans unites (dernieres versions des pages)
    -> m1.faits   faits atomiques de toutes les unites, migrees comprises
    -> m1.sujets  embeddings, sujets (FCM), deduplication (groupes)
    -> m2         sources explicites et typologie
    -> m3         voisins, qualification, paternite, propagation, statuts temporels
    -> m4         statuts finaux et requetes
    -> m5         criticite puis verification Ibou (critiques d'abord, budget plafonne)
    -> m6         propositions de correction

Fonctions publiques :

- ``preparer()`` : schema de la chaine et colonnes ajoutees par les modules (idempotent) ;
- ``etat()`` : compteurs par methodologie et par statut, reste a faire, moteurs, couts ;
- ``resume()`` : etat leger pour ``server.pulse`` ;
- ``prerequis(etape, ...)`` : raison qui empeche une etape, ou None ;
- ``lancer(etape, **params)`` : une etape ou une sous-etape ;
- ``tout(budget=0, ...)`` : enchainement reprenable, arret propre et motive.

Chaque etape s'execute dans le thread du traitement du serveur (``start_job``) :
``S.JOB['etape_chaine']`` porte le nom de l'etape de ce module, ``S.JOB['stage']``
le libelle plus fin pose par les modules (voir ``LIBELLES_STAGE``).
"""
import json
import sqlite3
import time

from chaine import noyau

# Etapes de l'enchainement, dans l'ordre.
ORDRE = ('migration', 'm1.page', 'm1.unites', 'm1.faits', 'm1.sujets', 'm2', 'm3', 'm4', 'm5', 'm6')

# Sous-etapes lancables seules (nom -> etape parente).
SOUS_ETAPES = {
    'm1.peremption': 'm1.page',
    'm1.dedup': 'm1.sujets',
    'm3.paternite': 'm3',
    'm5.criticite': 'm5',
}

LIBELLES = {
    'migration': "Migration de l'ancien parcours",
    'm1.page': 'M1 · segmentation, contexte, ancrage, classes',
    'm1.unites': "M1 · unités d'information",
    'm1.faits': 'M1 · faits atomiques',
    'm1.sujets': 'M1 · embeddings, sujets, dédoublonnage',
    'm1.peremption': 'M1 · annonces périmées',
    'm1.dedup': 'M1 · dédoublonnage seul',
    'm2': 'M2 · sources et typologie',
    'm3': 'M3 · voisins, paternité, propagation, statuts temporels',
    'm3.paternite': 'M3 · paternité, propagation, statuts (sans qualification NLI)',
    'm4': 'M4 · statuts finaux et requêtes',
    'm5': 'M5 · criticité et vérification Ibou',
    'm5.criticite': 'M5 · criticité seule',
    'm6': 'M6 · propositions de correction',
    'tout': 'Chaîne complète',
}

# Valeurs de ``S.JOB['stage']`` posees par les modules (documentees dans docs/API.md).
LIBELLES_STAGE = {
    'chaine': 'Démarrage de la chaîne',
    'migration': "Migration de l'ancien parcours",
    'm1.page': 'M1 · page (avant segmentation)',
    'm2': 'M2 (avant sources)',
    'm3': 'M3 (avant voisins)',
    'm4': 'M4 (avant statuts)',
    'm5': 'M5 (avant criticité)',
    'm6': 'M6 (avant propositions)',
    'm1.segmentation': 'Segmentation des pages',
    'm1.contexte': 'Coréférences et dates',
    'm1.ancrage': 'Ancrage temporel des unités',
    'm1.obsolescence': "Classes d'obsolescence",
    'm1.peremption': 'Annonces périmées',
    'm1.unites': "Unités d'information",
    'm1.faits': 'Faits atomiques',
    'm1.encodage.pages': 'Embeddings des pages',
    'm1.encodage.unites': 'Embeddings des unités',
    'm1.sujets': 'Sujets (fuzzy c-means)',
    'm1.dedup': 'Dédoublonnage des unités',
    'm2.sources': 'Sources explicites',
    'm2.typologie': 'Typologie des affirmations',
    'm2.manquantes': 'Références internes manquantes',
    'm3.voisins': 'Recherche des voisins',
    'm3.qualification': 'Qualification des paires',
    'm3.paternite': 'Centralité et paternité',
    'm3.propagation': 'Propagation des sources',
    'm3.statuts': 'Statuts temporels',
    'm4.statuts': 'Statuts finaux',
    'm4.requetes': 'Requêtes de vérification',
    'm5.criticite': 'Criticité',
    'm5.verification': 'Vérification Ibou',
    'm6.proposer': 'Propositions de correction',
}

CLE_MIGRE = 'chaine:migre'
CLE_DERNIER = 'chaine:dernier_resultat'
CLASSES_SANS_FAITS = ('usage', 'metadonnee')

_PREPARE = {'base': None}


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def _s():
    """Module serveur branche."""
    return noyau.srv()


def _un(sql, args=()):
    """Premier entier d'une requete de comptage."""
    ligne = _s().query(sql, args, True)
    if not ligne:
        return 0
    return int(list(ligne.values())[0] or 0)


def _repartition(sql, args=()):
    """Dictionnaire cle -> nombre depuis ``SELECT cle, n``."""
    resultat = {}
    for ligne in _s().query(sql, args):
        valeurs = list(ligne.values())
        cle = valeurs[0] if valeurs[0] is not None else 'aucun'
        resultat[str(cle)] = int(valeurs[1] or 0)
    return resultat


def _json(valeur):
    """Serialisation JSON stable."""
    return json.dumps(valeur, ensure_ascii=False, sort_keys=True, default=str)


def _job(**valeurs):
    """Met a jour l'etat du traitement du serveur."""
    _s().JOB.update(**valeurs)


def _marques(liste):
    """Marqueurs SQL ``?,?,...``."""
    return ','.join('?' for _ in liste)


def _par_lots(liste, taille=500):
    """Decoupe une liste en tranches."""
    for debut in range(0, len(liste), taille):
        yield liste[debut:debut + taille]


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def preparer():
    """Schema de la chaine et colonnes ajoutees par les modules. Idempotent.

    Appele par ``server.init()``. Les colonnes hors contrat des modules
    (``chunks.run_id``, ``units.facts_run_id``, ``unit_groups.merged_into``,
    ``proposals.decision``...) sont creees des le demarrage pour que l'API
    puisse les lire avant la premiere execution des modules.
    """
    from chaine import m1_page
    from chaine import m1_unites
    from chaine import m3
    from chaine import m5
    from chaine import m6
    from chaine import schema
    serveur = _s()
    schema.init()
    m1_page._COLONNES_PRETES.discard(str(serveur.DB))
    m1_page._assurer_colonnes()
    m1_unites.preparer_schema()
    m3._PREPARE['base'] = None
    m3._preparer()
    m5._preparer_schema()
    m6.assurer_schema()
    serveur.query('CREATE INDEX IF NOT EXISTS idx_ch_units_facts_run ON units(facts_run_id)')
    serveur.query('CREATE INDEX IF NOT EXISTS idx_ch_unit_groups_merged ON unit_groups(merged_into)')
    serveur.query('CREATE INDEX IF NOT EXISTS idx_ch_step_runs_started ON step_runs(started)')
    _PREPARE['base'] = str(serveur.DB)


def _assurer():
    """``preparer()`` une fois par base."""
    if _PREPARE['base'] != str(_s().DB):
        preparer()


# ---------------------------------------------------------------------------
# Selections (ce qui reste a faire)
# ---------------------------------------------------------------------------

SQL_OCCURRENCES_A_MIGRER = (
    'SELECT o.id FROM occurrences o JOIN claims c ON c.id=o.claim_id JOIN chunks ch ON ch.id=o.chunk_id '
    'WHERE NOT EXISTS(SELECT 1 FROM units u WHERE u.id=o.id) '
    "AND NOT EXISTS(SELECT 1 FROM units x WHERE x.version_id=ch.version_id AND COALESCE(x.extractor,'')<>'migration')"
)

SQL_DERNIERES_VERSIONS = (
    'SELECT v.id FROM versions v WHERE v.rowid=(SELECT w.rowid FROM versions w WHERE w.page_id=v.page_id '
    'ORDER BY w.created DESC, w.rowid DESC LIMIT 1)'
)

SQL_UNITES_SANS_FAITS = (
    "SELECT u.id FROM units u WHERE COALESCE(u.excluded,0)=0 AND u.facts_run_id IS NULL "
    "AND COALESCE(u.obs_class,'') NOT IN ('usage','metadonnee') AND u.text IS NOT NULL"
)

# Unites a verifier qui n'ont ni fait ni cadre : aucune requete envoyable tant que M1 faits n'a pas tourne.
SQL_EN_ATTENTE_DECOMPOSITION = (
    "SELECT count(*) n FROM units u WHERE COALESCE(u.excluded,0)=0 AND u.status IN ('a_verifier','a_verifier_faible') "
    "AND COALESCE(u.obs_class,'') NOT IN ('immuable','metadonnee','usage') "
    "AND COALESCE(u.sourcing,'')<>'connaissance_generale' "
    "AND NOT EXISTS(SELECT 1 FROM claim_types t WHERE t.unit_id=u.id AND t.type='connaissance_generale') "
    "AND NOT EXISTS(SELECT 1 FROM facts f WHERE f.unit_id=u.id AND COALESCE(f.status,'') NOT IN ('rejete','historique'))"
)


def versions_sans_unites():
    """Dernieres versions des pages qui n'ont encore aucune unite (pages nouvelles, versions creees par M6)."""
    return [ligne['id'] for ligne in _s().query(
        SQL_DERNIERES_VERSIONS + ' AND NOT EXISTS(SELECT 1 FROM units u WHERE u.version_id=v.id) ORDER BY v.created, v.id')]


def unites_sans_faits():
    """Unites (migrees comprises) dont les faits n'ont pas ete extraits."""
    return [ligne['id'] for ligne in _s().query(SQL_UNITES_SANS_FAITS + ' ORDER BY u.version_id, u.span_start, u.id')]


def reste():
    """Ce qui reste a faire a chaque etape (nombres et explication)."""
    _assurer()
    resultat = {}
    resultat['migration'] = {
        'a_faire': _un('SELECT count(*) n FROM (' + SQL_OCCURRENCES_A_MIGRER + ')'),
        'detail': "occurrences de l'ancien parcours pas encore migrées",
    }
    resultat['m1.page'] = {
        'a_faire': _un('SELECT count(*) n FROM versions WHERE segment_run IS NULL')
        + _un('SELECT count(*) n FROM units WHERE anchor_run IS NULL OR obs_run IS NULL'),
        'versions_a_segmenter': _un('SELECT count(*) n FROM versions WHERE segment_run IS NULL'),
        'unites_a_ancrer': _un('SELECT count(*) n FROM units WHERE anchor_run IS NULL'),
        'unites_a_classer': _un('SELECT count(*) n FROM units WHERE obs_run IS NULL'),
        'detail': 'versions à segmenter et unités à ancrer ou classer',
    }
    resultat['m1.unites'] = {
        'a_faire': len(versions_sans_unites()),
        'detail': 'dernières versions de page sans unité',
    }
    resultat['m1.faits'] = {
        'a_faire': _un('SELECT count(*) n FROM (' + SQL_UNITES_SANS_FAITS + ')'),
        'detail': 'unités dont les faits ne sont pas extraits (migrées comprises)',
    }
    resultat['m1.sujets'] = {
        'a_faire': _un("SELECT count(*) n FROM units WHERE COALESCE(excluded,0)=0 AND text IS NOT NULL AND (vector IS NULL OR vector='')")
        + _un('SELECT count(*) n FROM units u WHERE COALESCE(u.excluded,0)=0 AND NOT EXISTS('
              'SELECT 1 FROM unit_group_members m WHERE m.unit_id=u.id)'),
        'unites_sans_vecteur': _un("SELECT count(*) n FROM units WHERE COALESCE(excluded,0)=0 AND text IS NOT NULL "
                                   "AND (vector IS NULL OR vector='')"),
        'unites_sans_groupe': _un('SELECT count(*) n FROM units u WHERE COALESCE(u.excluded,0)=0 AND NOT EXISTS('
                                  'SELECT 1 FROM unit_group_members m WHERE m.unit_id=u.id)'),
        'detail': 'unités sans embedding ou sans groupe',
    }
    from chaine import m2
    resultat['m2'] = {
        'a_faire': _un('SELECT count(*) n FROM units u WHERE ' + m2.CLAUSE_ELIGIBLE + ' AND u.sourcing IS NULL'),
        'detail': 'unités obsolescibles non typées (les immuables sont hors périmètre de M2)',
    }
    resultat['m3'] = {
        'a_faire': _un('SELECT count(*) n FROM unit_groups g WHERE g.merged_into IS NULL AND EXISTS('
                       'SELECT 1 FROM unit_group_members m WHERE m.group_id=g.id) AND NOT EXISTS('
                       'SELECT 1 FROM paternity p WHERE p.group_id=g.id)'),
        'a_arbitrer': _un("SELECT count(*) n FROM paternity WHERE criterion='a_arbitrer'"),
        'detail': 'groupes sans paternité',
    }
    en_attente = _un(SQL_EN_ATTENTE_DECOMPOSITION)
    resultat['m4'] = {
        'a_faire': max(0, _un(
            "SELECT count(*) n FROM units u WHERE COALESCE(u.excluded,0)=0 AND u.status IN ('a_verifier','a_verifier_faible') "
            "AND NOT EXISTS(SELECT 1 FROM query_units qu JOIN queries q ON q.id=qu.query_id WHERE qu.unit_id=u.id "
            "AND q.status<>'abandonnee')") - en_attente),
        'en_attente_decomposition': en_attente,
        'detail': 'unités à vérifier sans requête (hors unités en attente de décomposition M1 faits)',
    }
    resultat['m5'] = {
        'a_faire': _un("SELECT count(*) n FROM queries WHERE kind='ouverte' AND status='prete' AND critical=1 "
                       "AND COALESCE(template,'') NOT IN ('ancien-pipeline') AND COALESCE(template,'') NOT LIKE '%unite:fermee'"),
        'plus_tard': _un("SELECT count(*) n FROM queries WHERE kind='ouverte' AND status IN ('prete','plus_tard') "
                         "AND COALESCE(critical,0)=0 AND COALESCE(template,'') NOT IN ('ancien-pipeline') "
                         "AND COALESCE(template,'') NOT LIKE '%unite:fermee'"),
        'detail': 'requêtes critiques prêtes (la file « plus tard » attend une demande explicite)',
    }
    resultat['m6'] = {
        'a_faire': _un("SELECT count(*) n FROM units u WHERE COALESCE(u.excluded,0)=0 AND (u.verdict IN ('corrigee','completee') "
                       "OR u.temporal_status='perimee_par_date') AND NOT EXISTS(SELECT 1 FROM proposals p WHERE p.unit_id=u.id)"),
        'en_revue': _un("SELECT count(*) n FROM proposals WHERE status IN ('proposee','a_rediger','reportee')"),
        'detail': 'unités corrigées, complétées ou périmées sans proposition',
    }
    return resultat


# ---------------------------------------------------------------------------
# Prerequis
# ---------------------------------------------------------------------------

def _etat_moteurs():
    """``moteurs.etat()`` (ne charge aucun modele)."""
    from chaine import moteurs
    return moteurs.etat()


def _nli_disponible(etat_moteurs=None):
    """Raison d'indisponibilite du NLI, ou None."""
    etat_moteurs = etat_moteurs or _etat_moteurs()
    nli = etat_moteurs.get('nli') or {}
    if nli.get('actif') in (None, 'aucun'):
        return ('aucun moteur NLI : installez le modèle local (installer_modeles.cmd) ou configurez un LLM dans les '
                'connexions (repli NLI par LLM contraint)')
    return None


def _embeddings_disponibles(etat_moteurs=None):
    """Raison d'indisponibilite des embeddings, ou None."""
    etat_moteurs = etat_moteurs or _etat_moteurs()
    plongements = etat_moteurs.get('embeddings') or {}
    if plongements.get('actif') in (None, 'aucun'):
        return ("aucun moteur d'embeddings : installez sentence-transformers (installer_modeles.cmd) ou activez "
                "les embeddings du fournisseur dans les connexions")
    return None


def _llm():
    """(disponible, raison) du LLM configure."""
    from chaine import moteurs
    return moteurs._llm_disponible()


def prerequis(etape, budget=None, option=None):
    """Raison (texte) qui empeche ``etape`` de s'executer, ou None.

    Ne verifie que les moyens (moteurs, cles, budget) ; l'absence de travail
    n'est pas un empechement.
    """
    from chaine import m1_unites
    from chaine import moteurs
    if etape in ('m1.unites',):
        effective = m1_unites.choisir_option(option or 'auto')
        if effective in ('C', 'D'):
            ok, raison = _llm()
            if not ok:
                return 'option ' + effective + ' : ' + raison
            # Les unites du LLM sont controlees par un NLI distinct du LLM (double passe).
            return _nli_disponible() or moteurs.raison_double_passe()
        return None
    if etape == 'm1.faits':
        extracteur = m1_unites.choisir_extracteur()
        if extracteur == 'aucun':
            ok, raison = _llm()
            return ('aucun extracteur de relations (' + (raison or 'GLiREL absent') + ') : configurez un LLM local dans les '
                    'connexions ou installez GLiREL')
        if extracteur == 'llm':
            return _nli_disponible() or moteurs.raison_double_passe()
        return _nli_disponible()
    if etape in ('m1.sujets',):
        return _embeddings_disponibles()
    if etape in ('m1.dedup',):
        return None
    if etape == 'm3':
        return _nli_disponible()
    if etape == 'm5':
        if type(budget) is not int or budget <= 0:
            return 'aucun budget Ibou : indiquez un plafond entier positif'
        if not _s().CONFIG.get('ibou_key'):
            return 'clé Ibou manquante (connexions)'
        ok, raison = _llm()
        if not ok:
            return 'selection du contenu des preuves : ' + (raison or 'LLM indisponible')
        return _nli_disponible() or moteurs.raison_double_passe()
    return None


# ---------------------------------------------------------------------------
# Etapes
# ---------------------------------------------------------------------------

def _migration(params):
    from chaine import migration
    rapport = migration.migrer()
    noyau.definir_reglage(CLE_MIGRE, _s().now())
    return rapport


def _m1_page(params):
    from chaine import m1_page
    return m1_page.executer(version_ids=params.get('pages'), forcer=params.get('forcer', False),
                            aujourdhui=params.get('aujourdhui'))


def _m1_peremption(params):
    from chaine import m1_page
    return m1_page.perimer(params.get('aujourdhui'))


def _apres_nouvelles_unites(unit_ids, aujourdhui):
    """Ancrage et classe d'obsolescence des unites creees ou enrichies (rapport du lot m1_page)."""
    from chaine import m1_page
    if not unit_ids:
        return {}
    resultat = {}
    resultat['ancrage'] = m1_page.ancrer(unit_ids=unit_ids, forcer=True)
    resultat['obsolescence'] = m1_page.classer_obsolescence(unit_ids=unit_ids, forcer=True, aujourdhui=aujourdhui)
    resultat['peremption'] = m1_page.perimer(aujourdhui)
    return resultat


def _m1_unites(params):
    """Etape 4 seule, sur les versions sans unites ; les faits sont l'etape suivante."""
    from chaine import m1_unites
    serveur = _s()
    versions = params.get('pages') or versions_sans_unites()
    effective = m1_unites.choisir_option(params.get('option') or 'auto')
    if effective == 'D':
        raise ValueError("L'option D est réservée au jeu de référence.")
    compteurs = m1_unites._compteurs_vides()
    if not versions:
        return dict(compteurs, option=effective)
    _job(stage='m1.unites', done=0, total=len(versions), message="Unités d'information")
    with noyau.etape('m1.unites', moteurs={'unites': effective},
                     prompt=m1_unites.PROMPT_UNITES if effective == 'C' else '',
                     seuils=['m1.unites.mots_min', 'm1.unites.mots_max', 'm1.unites.span_recouvrement_min',
                             'nli.implication']) as run:
        for numero, version_id in enumerate(versions, 1):
            serveur.checkpoint()
            m1_unites.unites(version_id, effective, params.get('forcer', False), False, run, compteurs)
            run.pages = compteurs['versions']
            _job(done=numero, message='Unités : ' + str(compteurs['unites']) + ' retenues')
        run.items = compteurs['unites']
        run.note = _json({k: v for k, v in compteurs.items() if k != 'unit_ids'})
        if compteurs['versions_en_echec']:
            # Panne du LLM (option C) : l'etape n'est pas « terminee » avec 0 unite, elle echoue et le dit.
            run.echec = (str(compteurs['versions_en_echec']) + ' version(s) en échec : '
                         + '; '.join(compteurs.get('erreurs') or ['erreur inconnue']))
    nouvelles = compteurs.pop('unit_ids', [])
    # Les anciens chunks de ces versions ne doivent pas etre re-extraits par l'ancien parcours.
    with noyau.connexion() as base:
        for lot in _par_lots(list(versions)):
            base.execute('UPDATE chunks SET prepared=1 WHERE kind IS NULL AND version_id IN (' + _marques(lot) + ')',
                         tuple(lot))
            base.execute('UPDATE versions SET prepared=1 WHERE id IN (' + _marques(lot) + ')', tuple(lot))
    compteurs['option'] = effective
    compteurs['suite'] = _apres_nouvelles_unites(nouvelles, params.get('aujourdhui'))
    if compteurs['versions_en_echec']:
        compteurs['_incomplet'] = ('extraction des unités en échec pour ' + str(compteurs['versions_en_echec']) + ' version(s) sur '
                                   + str(len(versions)) + ' (option ' + effective + ') : '
                                   + (compteurs.get('erreurs') or ['erreur inconnue'])[0])
    return compteurs


def _m1_faits(params):
    """Etape 5 pour toutes les unites sans faits, migrees comprises, puis reclassement par predicat."""
    from chaine import m1_unites
    cibles = params.get('unit_ids') or unites_sans_faits()
    if params.get('pages'):
        pages = set(params['pages'])
        cibles = [ligne['id'] for lot in _par_lots(cibles) for ligne in _s().query(
            'SELECT id, version_id FROM units WHERE id IN (' + _marques(lot) + ')', tuple(lot)) if ligne['version_id'] in pages]
    if not cibles:
        return {'unites': 0}
    resultat = m1_unites.faits(cibles, forcer=params.get('forcer', False))
    enrichies = [ligne['id'] for lot in _par_lots(cibles) for ligne in _s().query(
        'SELECT DISTINCT unit_id id FROM facts WHERE unit_id IN (' + _marques(lot) + ')', tuple(lot))]
    resultat['suite'] = _apres_nouvelles_unites(enrichies, params.get('aujourdhui'))
    return resultat


def _dedup_tronquee(dedup):
    """Raison d'incompletude si la deduplication a reporte des paires (``dedup.nli_paires_max``), sinon None."""
    reportees = int((dedup or {}).get('tronquees') or 0)
    if not reportees:
        return None
    return (str(reportees) + ' paire(s) de déduplication reportée(s) à la prochaine exécution (plafond dedup.nli_paires_max) : '
            'relancez l\'étape pour terminer')


def _m1_sujets(params):
    """Etapes 7 et 8 ; sans embeddings, seul le dedoublonnage exact (et les groupes) est fait."""
    from chaine import m1_sujets
    raison = _embeddings_disponibles()
    if raison is None:
        resultat = m1_sujets.executer(forcer=params.get('forcer', False))
        tronquee = _dedup_tronquee(resultat.get('dedup'))
        if tronquee:
            resultat['_incomplet'] = tronquee
        return resultat
    resultat = {'dedup': m1_sujets.dedoublonner(forcer=params.get('forcer', False)), 'embeddings': raison}
    resultat['_incomplet'] = raison + ' ; seuls le dédoublonnage exact et les groupes ont été faits'
    return resultat


def _m1_dedup(params):
    from chaine import m1_sujets
    resultat = m1_sujets.dedoublonner(forcer=params.get('forcer', False))
    tronquee = _dedup_tronquee(resultat)
    if tronquee:
        resultat['_incomplet'] = tronquee
    return resultat


def _m2(params):
    from chaine import m2
    return m2.executer(version_ids=params.get('pages'), forcer=params.get('forcer', False),
                       aujourdhui=params.get('aujourdhui'))


def _m3(params):
    from chaine import m3
    raison = _nli_disponible()
    resultat = m3.executer(forcer=params.get('forcer', False), aujourdhui=params.get('aujourdhui'),
                           qualifier_paires=raison is None, paires_max=params.get('paires_max'))
    if raison is not None:
        resultat['_incomplet'] = raison + ' ; étapes 1-2 (voisins, qualification) non faites, étapes 3-5 faites'
    return resultat


def _m3_paternite(params):
    from chaine import m3
    return m3.executer(forcer=params.get('forcer', False), aujourdhui=params.get('aujourdhui'), qualifier_paires=False)


def _m4(params):
    from chaine import m4
    ok, _ = _llm()
    return m4.executer(aujourdhui=params.get('aujourdhui'), forcer=params.get('forcer', False), reformuler_llm=ok)


def _m5(params):
    from chaine import m5
    return m5.executer(params['budget'], tout=bool(params.get('plus_tard')), aujourdhui=params.get('aujourdhui'))


def _m5_criticite(params):
    from chaine import m5
    return m5.criticite(params.get('aujourdhui'))


def _m6(params):
    from chaine import m6
    return m6.executer(forcer=params.get('forcer', False), aujourdhui=params.get('aujourdhui'))


FONCTIONS = {
    'migration': _migration,
    'm1.page': _m1_page,
    'm1.peremption': _m1_peremption,
    'm1.unites': _m1_unites,
    'm1.faits': _m1_faits,
    'm1.sujets': _m1_sujets,
    'm1.dedup': _m1_dedup,
    'm2': _m2,
    'm3': _m3,
    'm3.paternite': _m3_paternite,
    'm4': _m4,
    'm5': _m5,
    'm5.criticite': _m5_criticite,
    'm6': _m6,
}


def lancer(etape, budget=None, pages=None, forcer=False, aujourdhui=None, plus_tard=False, option=None, paires_max=None,
           unit_ids=None):
    """Execute une etape ou une sous-etape ; renvoie ``{etape, ok, resultat, duree_s, raison?}``.

    ``plus_tard=True`` (M5) traite aussi la file « plus tard » (unites non critiques).
    ``ok`` est faux, avec ``raison``, si un prerequis manque (rien n'est fait)
    ou si l'etape n'a pu faire qu'une partie de son travail (``partiel``).
    Les arrets demandes (``S.STOP``) et les erreurs operationnelles sont
    propages : ``start_job`` les journalise et l'etape est reprenable.
    """
    if etape == 'tout':
        return tout(budget=budget or 0, aujourdhui=aujourdhui, forcer=forcer)
    if etape not in FONCTIONS:
        raise ValueError('Étape inconnue : ' + str(etape) + ' (attendu : tout, ' + ', '.join(FONCTIONS) + ')')
    _assurer()
    serveur = _s()
    params = {'budget': budget, 'pages': list(pages) if pages else None, 'forcer': bool(forcer), 'aujourdhui': aujourdhui,
              'plus_tard': bool(plus_tard), 'option': option, 'paires_max': paires_max, 'unit_ids': unit_ids}
    _job(etape_chaine=etape, stage=etape, message=LIBELLES.get(etape, etape))
    raison = prerequis(etape, budget=budget, option=option)
    # m1.sujets et m3 font ce qu'ils peuvent sans le moteur manquant puis signalent un resultat partiel.
    if raison is not None and etape not in ('m1.sujets', 'm3'):
        resultat = {'etape': etape, 'ok': False, 'raison': raison, 'resultat': None, 'duree_s': 0.0}
        _memoriser(resultat)
        return resultat
    debut = time.monotonic()
    from datetime import date
    jour = str(aujourdhui)[:10] if aujourdhui else date.today().isoformat()
    with noyau.parametres(aujourdhui=jour, budget=budget, forcer=bool(forcer) or None, etape_lancee=etape):
        sortie = FONCTIONS[etape](params)
    resultat = {'etape': etape, 'ok': True, 'resultat': sortie, 'duree_s': round(time.monotonic() - debut, 2)}
    if isinstance(sortie, dict) and sortie.get('_incomplet'):
        resultat['ok'] = False
        resultat['partiel'] = True
        resultat['raison'] = sortie.pop('_incomplet')
    if etape == 'm5' and isinstance(sortie, dict) and sortie.get('arret'):
        resultat['arret_m5'] = sortie['arret']
    serveur.event('Chaîne · ' + LIBELLES.get(etape, etape) + (' : terminé' if resultat['ok'] else ' : ' + resultat['raison']))
    _memoriser(resultat)
    return resultat


def tout(budget=0, aujourdhui=None, forcer=False):
    """Enchaine toutes les etapes dans l'ordre ; reprenable.

    La migration et l'extraction des unites ne tournent que s'il reste du
    travail ; les autres etapes sont reprenables et ne refont pas ce qui est
    fait. S'arrete au premier prerequis manquant ou a la premiere etape
    incomplete, et dit pourquoi : ``{ok, arret, raison, etapes}``. Un budget
    Ibou nul saute M5 (sans arret) : M6 propose alors les corrections des
    annonces perimees seulement.
    """
    _assurer()
    etapes = []
    for etape in ORDRE:
        _s().checkpoint()
        if etape in ('migration', 'm1.unites', 'm1.faits'):
            restant = reste()[etape]['a_faire']
            if restant == 0:
                etapes.append({'etape': etape, 'ok': True, 'saute': True, 'raison': 'rien à faire'})
                continue
        if etape == 'm5' and (type(budget) is not int or budget <= 0):
            etapes.append({'etape': etape, 'ok': True, 'saute': True, 'raison': 'aucun budget Ibou : M5 non lancé'})
            continue
        resultat = lancer(etape, budget=budget, forcer=forcer, aujourdhui=aujourdhui)
        etapes.append(resultat)
        if not resultat['ok']:
            sortie = {'etape': 'tout', 'ok': False, 'arret': etape, 'raison': resultat['raison'], 'etapes': etapes}
            _memoriser(sortie)
            return sortie
    sortie = {'etape': 'tout', 'ok': True, 'arret': None, 'raison': '', 'etapes': etapes}
    _memoriser(sortie)
    return sortie


def executer_tache(etape, **params):
    """Corps d'une tache ``start_job`` : leve ``OperationalError`` motivee si la chaine s'arrete.

    ``start_job`` passe alors le traitement en pause avec ce message
    (« Étape … interrompue : <raison> »).
    """
    resultat = lancer(etape, **params)
    _job(chaine=_resume_resultat(resultat))
    if not resultat.get('ok'):
        arret = resultat.get('arret') or resultat.get('etape')
        raise _s().OperationalError(LIBELLES.get(arret, arret) + ' : ' + str(resultat.get('raison')))
    return resultat


def _resume_resultat(resultat):
    """Version courte d'un resultat (pour ``S.JOB``)."""
    court = {cle: resultat.get(cle) for cle in ('etape', 'ok', 'arret', 'raison', 'duree_s', 'partiel')}
    if resultat.get('etapes'):
        court['etapes'] = [{cle: e.get(cle) for cle in ('etape', 'ok', 'saute', 'raison', 'duree_s')} for e in resultat['etapes']]
    return court


CLE_ISSUES = 'chaine:issues'


def _memoriser(resultat):
    """Dernier resultat (resume) dans ``settings`` pour l'interface, et issue de l'etape (``issues``)."""
    try:
        noyau.definir_reglage(CLE_DERNIER, _json(dict(_resume_resultat(resultat), date=_s().now())))
        if resultat.get('etape') and resultat['etape'] != 'tout':
            issues = issues_etapes()
            issues[resultat['etape']] = {
                'issue': 'terminee' if resultat.get('ok') else ('partielle' if resultat.get('partiel') else 'arretee'),
                'raison': None if resultat.get('ok') else resultat.get('raison'), 'date': _s().now()}
            noyau.definir_reglage(CLE_ISSUES, _json(issues))
    except sqlite3.Error:
        pass


def issues_etapes():
    """Derniere issue de chaque etape lancee : ``{etape: {issue: terminee|partielle|arretee, raison, date}}``."""
    brut = noyau.reglage(CLE_ISSUES)
    try:
        valeur = json.loads(brut) if brut else {}
    except ValueError:
        valeur = {}
    return valeur if isinstance(valeur, dict) else {}


# ---------------------------------------------------------------------------
# Etat
# ---------------------------------------------------------------------------

def est_migre():
    """Vrai si une migration s'est terminee sur cette base."""
    return bool(_s().query("SELECT 1 x FROM step_runs WHERE step='migration' AND status='terminee' LIMIT 1", one=True))


def compteurs():
    """Compteurs par methodologie et par statut (agregats SQL, aucune liste)."""
    _assurer()
    resultat = {}
    # Definition unique : pages = URL distinctes (qui ont au moins une version) ; versions comptees a part.
    # Les dates manquantes se comptent sur la derniere version de chaque page.
    resultat['pages'] = {
        'pages': _un('SELECT count(DISTINCT p.url) n FROM pages p WHERE EXISTS(SELECT 1 FROM versions v WHERE v.page_id=p.id)'),
        'pages_sans_date_publication': _un('SELECT count(*) n FROM (' + SQL_DERNIERES_VERSIONS + " AND COALESCE(v.published,'')='')"),
        'pages_sans_date_maj': _un('SELECT count(*) n FROM (' + SQL_DERNIERES_VERSIONS + " AND COALESCE(v.updated,'')='')"),
        'versions': _un('SELECT count(*) n FROM versions'),
        'versions_segmentees': _un('SELECT count(*) n FROM versions WHERE segment_run IS NOT NULL'),
        'avec_date_publication': _un("SELECT count(*) n FROM versions WHERE COALESCE(published,'')<>''"),
        'avec_date_maj': _un("SELECT count(*) n FROM versions WHERE COALESCE(updated,'')<>''"),
        'par_type': _repartition('SELECT page_type, count(*) n FROM versions GROUP BY page_type'),
    }
    resultat['m1'] = {
        'unites': _un('SELECT count(*) n FROM units'),
        'unites_exclues': _un('SELECT count(*) n FROM units WHERE COALESCE(excluded,0)=1'),
        'par_extracteur': _repartition('SELECT extractor, count(*) n FROM units GROUP BY extractor'),
        'par_classe': _repartition('SELECT obs_class, count(*) n FROM units GROUP BY obs_class'),
        'par_ancre': _repartition('SELECT anchor_kind, count(*) n FROM units GROUP BY anchor_kind'),
        'faits': _un('SELECT count(*) n FROM facts'),
        'faits_par_statut': _repartition('SELECT status, count(*) n FROM facts GROUP BY status'),
        'unites_avec_faits': _un('SELECT count(DISTINCT unit_id) n FROM facts'),
        'unites_decomposees': _un('SELECT count(*) n FROM units WHERE facts_run_id IS NOT NULL'),
        'hypotheses': _repartition('SELECT kind, count(*) n FROM hypotheses GROUP BY kind'),
        'entites': _un('SELECT count(*) n FROM entities'),
        'unites_encodees': _un("SELECT count(*) n FROM units WHERE vector IS NOT NULL AND vector<>''"),
        'sujets': _repartition('SELECT method, count(*) n FROM topics GROUP BY method'),
        'groupes': _un('SELECT count(*) n FROM unit_groups WHERE merged_into IS NULL'),
        'groupes_fusionnes': _un('SELECT count(*) n FROM unit_groups WHERE merged_into IS NOT NULL'),
    }
    resultat['m2'] = {
        'par_sourcing': _repartition('SELECT sourcing, count(*) n FROM units WHERE COALESCE(excluded,0)=0 GROUP BY sourcing'),
        'par_type': _repartition('SELECT type, count(*) n FROM claim_types GROUP BY type'),
        'sources_par_type': _repartition('SELECT kind, count(*) n FROM sources GROUP BY kind'),
        'sources_manquantes': _un('SELECT count(*) n FROM units WHERE source_manquante=1'),
    }
    resultat['m3'] = {
        'relations': _repartition('SELECT relation, count(*) n FROM unit_relations GROUP BY relation'),
        'paternite': _repartition('SELECT criterion, count(*) n FROM paternity GROUP BY criterion'),
        'par_statut_temporel': _repartition(
            'SELECT temporal_status, count(*) n FROM units WHERE COALESCE(excluded,0)=0 GROUP BY temporal_status'),
        'liens_recommandes': _repartition('SELECT status, count(*) n FROM link_recommendations GROUP BY status'),
    }
    resultat['m4'] = {
        'par_statut': _repartition('SELECT status, count(*) n FROM units WHERE COALESCE(excluded,0)=0 GROUP BY status'),
        'requetes_par_statut': _repartition('SELECT status, count(*) n FROM queries GROUP BY status'),
        'requetes_par_type': _repartition('SELECT kind, count(*) n FROM queries GROUP BY kind'),
        'requetes_repli_unite': _un("SELECT count(*) n FROM queries WHERE COALESCE(template,'') LIKE '%unite:fermee'"),
        'en_attente_decomposition': _un(SQL_EN_ATTENTE_DECOMPOSITION),
    }
    resultat['m5'] = {
        'critiques': _un('SELECT count(*) n FROM units WHERE critical=1 AND COALESCE(excluded,0)=0'),
        'criticite_calculee': _un('SELECT count(*) n FROM units WHERE critical IS NOT NULL'),
        'verdicts': _repartition('SELECT verdict, count(*) n FROM units WHERE verdict IS NOT NULL GROUP BY verdict'),
        'appels_par_statut': _repartition('SELECT status, count(*) n FROM ibou_calls GROUP BY status'),
        'unites_ibou': _un("SELECT COALESCE(sum(units),0) n FROM ibou_calls WHERE COALESCE(legacy_call_id,'')=''"),
        'preuves_retenues': _un('SELECT count(*) n FROM evidence WHERE kept=1'),
    }
    resultat['m6'] = {
        'par_statut': _repartition('SELECT status, count(*) n FROM proposals GROUP BY status'),
        'par_type': _repartition('SELECT kind, count(*) n FROM proposals GROUP BY kind'),
    }
    return resultat


def couts():
    """Couts par etape depuis ``step_runs`` : secondes locales, tokens, unites Ibou, par page."""
    lignes = _s().query(
        'SELECT step, count(*) executions, sum(status=\'terminee\') terminees, sum(status=\'erreur\') erreurs, '
        'sum(status=\'interrompue\') interrompues, COALESCE(sum(local_seconds),0) secondes_locales, '
        'COALESCE(sum(tokens_in),0) tokens_in, COALESCE(sum(tokens_out),0) tokens_out, '
        'COALESCE(sum(ibou_units),0) unites_ibou, COALESCE(sum(pages),0) pages, COALESCE(sum(items),0) items, '
        'max(ended) derniere, min(started) premiere FROM step_runs GROUP BY step ORDER BY min(started)')
    resultat = []
    for ligne in lignes:
        pages = int(ligne['pages'] or 0)
        ligne['secondes_locales'] = round(float(ligne['secondes_locales'] or 0), 3)
        if pages:
            ligne['par_page'] = {
                'secondes_locales': round(ligne['secondes_locales'] / pages, 3),
                'tokens_in': round(ligne['tokens_in'] / pages, 1),
                'tokens_out': round(ligne['tokens_out'] / pages, 1),
                'unites_ibou': round(ligne['unites_ibou'] / pages, 2),
            }
        else:
            ligne['par_page'] = None
        resultat.append(ligne)
    return resultat


def executions(limite=15):
    """Dernieres executions (``step_runs``)."""
    lignes = _s().query(
        'SELECT id, step, started, ended, status, engines, prompt, pages, items, local_seconds, tokens_in, tokens_out, '
        'ibou_units, note FROM step_runs ORDER BY started DESC, rowid DESC LIMIT ?', (int(limite),))
    for ligne in lignes:
        try:
            ligne['engines'] = json.loads(ligne['engines'] or '{}')
        except ValueError:
            pass
        ligne['note'] = (ligne['note'] or '')[:400]
    return lignes


def moteurs_et_replis():
    """Moteurs actifs, replis, LLM, Ibou, extracteurs."""
    from chaine import m1_unites
    etat_moteurs = _etat_moteurs()
    from chaine import moteurs
    ok, raison = _llm()
    config = _s().CONFIG
    return {
        'fonctions': etat_moteurs,
        'llm': {'disponible': ok, 'raison': raison, 'fournisseur': config.get('provider'), 'modele': config.get('model'),
                'type': noyau.type_fournisseur(), 'tiers_autorise': noyau.llm_tiers_autorise(),
                'nli_par_llm_autorise': moteurs.nli_par_llm_autorise(),
                'double_passe': (etat_moteurs.get('nli') or {}).get('double_passe'),
                'raison_double_passe': moteurs.raison_double_passe()},
        'ibou': {'cle': bool(config.get('ibou_key'))},
        'extracteur_relations': m1_unites.choisir_extracteur(),
        'option_unites': m1_unites.choisir_option('auto'),
    }


def etat():
    """Etat complet de la chaine pour l'interface (``/api/chaine/etat``)."""
    _assurer()
    serveur = _s()
    dernier = noyau.reglage(CLE_DERNIER)
    try:
        dernier = json.loads(dernier) if dernier else None
    except ValueError:
        dernier = None
    moteurs_etat = moteurs_et_replis()
    prerequis_etapes = {}
    for etape in ORDRE:
        prerequis_etapes[etape] = prerequis(etape, budget=1)
    return {
        'migre': est_migre(),
        'migration': serveur.query("SELECT id, started, ended, status, note FROM step_runs WHERE step='migration' "
                                   "ORDER BY started DESC LIMIT 1", one=True),
        'ordre': [{'etape': e, 'libelle': LIBELLES[e]} for e in ORDRE],
        'sous_etapes': [{'etape': e, 'parente': p, 'libelle': LIBELLES[e]} for e, p in SOUS_ETAPES.items()],
        'compteurs': compteurs(),
        'reste': reste(),
        'prerequis': prerequis_etapes,
        'moteurs': moteurs_etat,
        'couts': couts(),
        'executions': executions(),
        'dernier_resultat': dernier,
        'issues': issues_etapes(),
        'job': {cle: serveur.JOB.get(cle) for cle in ('status', 'kind', 'stage', 'etape_chaine', 'message', 'done', 'total')},
    }


def resume():
    """Etat leger de la chaine pour ``server.pulse`` (quelques comptages)."""
    try:
        _assurer()
        serveur = _s()
        job = serveur.JOB
        return {
            'migre': est_migre(),
            'unites': _un('SELECT count(*) n FROM units'),
            'requetes_pretes': _un("SELECT count(*) n FROM queries WHERE status='prete'"),
            'propositions_en_attente': _un("SELECT count(*) n FROM proposals WHERE status IN ('proposee','a_rediger','reportee')"),
            'etape': job.get('etape_chaine') if job.get('kind') == 'chaine' else None,
            'stage': job.get('stage') if job.get('kind') == 'chaine' else None,
            'derniere_execution': serveur.query('SELECT step, status, ended FROM step_runs ORDER BY started DESC, rowid DESC LIMIT 1',
                                                one=True),
        }
    except (sqlite3.Error, RuntimeError):
        return None
