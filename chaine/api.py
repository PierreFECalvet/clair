"""Routes HTTP de la chaine (``/api/chaine/*``), appelees par ``server.Handler``.

``lire(chemin, parametres)`` sert les GET, ``ecrire(chemin, donnees)`` les POST.
Les garde-fous du serveur (hote, origine, jeton de session pour les POST,
refus d'ecrire pendant un traitement) sont appliques par ``server.Handler``
avant l'appel. Chaque fonction renvoie ``(code, objet JSON)`` ou, pour un
telechargement, ``(code, octets, type MIME, nom de fichier)``.

Listes paginees par 50 (``offset``, ``limite`` <= 200) avec filtres ; aucune
liste ne charge les vecteurs ni toutes les unites d'un coup. Documentation
complete et exemples : ``docs/API.md``.
"""
import json
import re
import sqlite3
from datetime import date

from chaine import noyau

LIMITE = 50
LIMITE_MAX = 200
TEXTE_PREUVE_MAX = 1500
REGLAGES_LIBRES = ('m2:llm', 'm2:auteurs_experts', 'm6.pages_datees', 'm6.format_lien', 'm1.fcm.moteur',
                   'm1.reduction.methode', 'm1:entites:wikidata', 'gsc:fichier')
OPTIONS_UNITES = ('auto', 'A', 'B', 'C')
_RE_DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_RE_DOMAINE = re.compile(r'^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$')


class Introuvable(Exception):
    """Objet demande absent (reponse 404)."""


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def _s():
    """Module serveur branche."""
    return noyau.srv()


def _param(parametres, nom, defaut=None):
    """Premiere valeur d'un parametre de requete (``parse_qs``), espaces retires."""
    valeurs = parametres.get(nom)
    if not valeurs:
        return defaut
    valeur = str(valeurs[0]).strip()
    return valeur if valeur != '' else defaut


def _entier(parametres, nom, defaut, minimum=0, maximum=None):
    """Parametre entier borne."""
    brut = _param(parametres, nom)
    if brut is None:
        return defaut
    try:
        valeur = int(brut)
    except ValueError as erreur:
        raise ValueError('Paramètre ' + nom + ' : entier attendu.') from erreur
    valeur = max(minimum, valeur)
    if maximum is not None:
        valeur = min(maximum, valeur)
    return valeur


def _pagination(parametres):
    """(offset, limite) depuis les parametres."""
    return _entier(parametres, 'offset', 0), _entier(parametres, 'limite', LIMITE, 1, LIMITE_MAX)


def _json_lu(texte, defaut=None):
    """JSON stocke -> objet, ou ``defaut``."""
    if texte in (None, ''):
        return defaut
    if isinstance(texte, (dict, list)):
        return texte
    try:
        return json.loads(texte)
    except (TypeError, ValueError):
        return defaut


def _marques(liste):
    """Marqueurs SQL."""
    return ','.join('?' for _ in liste)


def _page_liste(sql_from, conditions, args, ordre, colonnes, offset, limite):
    """Requete paginee : ``{total, offset, limite, elements}``."""
    clause = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
    total = _s().query('SELECT count(*) n ' + sql_from + clause, tuple(args), True)['n']
    lignes = _s().query('SELECT ' + colonnes + ' ' + sql_from + clause + ' ORDER BY ' + ordre + ' LIMIT ? OFFSET ?',
                        tuple(args) + (limite, offset))
    return {'total': total, 'offset': offset, 'limite': limite, 'elements': lignes}


def _tronquer(texte, maximum):
    """Texte tronque avec la longueur d'origine."""
    texte = texte or ''
    if len(texte) <= maximum:
        return texte
    return texte[:maximum] + '…'


# ---------------------------------------------------------------------------
# Lecture
# ---------------------------------------------------------------------------

def unites(parametres):
    """Liste paginee des unites, filtres : statut, classe, type, sourcing, temporel, verdict, critique, page, groupe,
    extracteur, q (texte), exclues (0 par defaut, 1, tous), sans_faits (1)."""
    offset, limite = _pagination(parametres)
    conditions = []
    args = []
    filtres = (('statut', 'u.status'), ('classe', 'u.obs_class'), ('sourcing', 'u.sourcing'),
               ('temporel', 'u.temporal_status'), ('verdict', 'u.verdict'), ('extracteur', 'u.extractor'),
               ('type', 'ct.type'))
    for nom, colonne in filtres:
        valeur = _param(parametres, nom)
        if valeur is None:
            continue
        if valeur == 'aucun':
            conditions.append(colonne + ' IS NULL')
        else:
            conditions.append(colonne + '=?')
            args.append(valeur)
    critique = _param(parametres, 'critique')
    if critique in ('0', '1'):
        conditions.append('COALESCE(u.critical,0)=?')
        args.append(int(critique))
    page = _param(parametres, 'page')
    if page:
        conditions.append('(u.version_id=? OR v.page_id=?)')
        args.extend([page, page])
    groupe = _param(parametres, 'groupe')
    if groupe:
        conditions.append('EXISTS(SELECT 1 FROM unit_group_members m WHERE m.unit_id=u.id AND m.group_id=?)')
        args.append(groupe)
    texte = _param(parametres, 'q')
    if texte:
        conditions.append('u.text LIKE ?')
        args.append('%' + texte[:200] + '%')
    exclues = _param(parametres, 'exclues', '0')
    if exclues in ('0', '1'):
        conditions.append('COALESCE(u.excluded,0)=?')
        args.append(int(exclues))
    if _param(parametres, 'sans_faits') == '1':
        conditions.append('NOT EXISTS(SELECT 1 FROM facts f WHERE f.unit_id=u.id)')
    sql_from = ('FROM units u LEFT JOIN versions v ON v.id=u.version_id LEFT JOIN pages p ON p.id=v.page_id '
                'LEFT JOIN claim_types ct ON ct.unit_id=u.id')
    colonnes = ('u.id, substr(u.text,1,400) text, u.status, u.temporal_status, u.obs_class, u.sourcing, ct.type, u.verdict, '
                'u.critical, u.critical_score, u.centrality, u.anchor_date, u.anchor_kind, u.expires, u.extractor, u.excluded, '
                'u.source_manquante, u.version_id, u.span_start, p.url, p.title, '
                '(SELECT m.group_id FROM unit_group_members m WHERE m.unit_id=u.id LIMIT 1) group_id, '
                '(SELECT count(*) FROM facts f WHERE f.unit_id=u.id) faits, '
                '(SELECT count(*) FROM sources s WHERE s.unit_id=u.id) sources, '
                '(SELECT count(*) FROM proposals pr WHERE pr.unit_id=u.id) propositions')
    resultat = _page_liste(sql_from, conditions, args, 'p.url, u.span_start, u.id', colonnes, offset, limite)
    resultat['unites'] = resultat.pop('elements')
    return resultat


def unite(unit_id):
    """Carte complete d'une unite : tout ce que l'interface doit montrer."""
    serveur = _s()
    ligne = serveur.query(
        'SELECT u.*, v.page_id, v.published, v.updated, v.page_type, p.url, p.title FROM units u '
        'LEFT JOIN versions v ON v.id=u.version_id LEFT JOIN pages p ON p.id=v.page_id WHERE u.id=?', (unit_id,), True)
    if not ligne:
        raise Introuvable('Unité introuvable : ' + str(unit_id))
    ligne.pop('vector', None)
    ligne['encodee'] = bool(serveur.query("SELECT 1 x FROM units WHERE id=? AND vector IS NOT NULL AND vector<>''",
                                          (unit_id,), True))
    chunk = serveur.query('SELECT id, kind, section, start, "end", context FROM chunks WHERE id=?', (ligne['chunk_id'],), True)
    if chunk:
        contexte = _json_lu(chunk.pop('context'), {}) or {}
        chunk['contexte'] = {cle: contexte.get(cle) for cle in ('titre', 'section', 'date', 'date_type', 'reference')}
        chunk['contexte']['coreferences'] = (contexte.get('coreferences') or [])[:10]
    page = {'version_id': ligne.get('version_id'), 'page_id': ligne.pop('page_id'), 'url': ligne.pop('url'),
            'titre': ligne.pop('title'), 'publication': ligne.pop('published'), 'mise_a_jour': ligne.pop('updated'),
            'type': ligne.pop('page_type')}
    faits = serveur.query('SELECT id, subject, subject_id, predicate, object, object_norm, object_type, scope, period, origin, '
                          'span_start, span_end, evidence_id, verbalization, nli_score, status, replaced_by, run_id, created FROM facts '
                          'WHERE unit_id=? ORDER BY span_start, created', (unit_id,))
    hypotheses = serveur.query('SELECT id, kind, text, subject, predicate, object, reason, resolved_fact_id, resolved_value, '
                               'evidence_id, span_start, span_end, created '
                               'FROM hypotheses WHERE unit_id=? ORDER BY kind, created', (unit_id,))
    entites = serveur.query('SELECT e.id, e.name, e.type, e.external_id, ue."start", ue."end", ue.role FROM unit_entities ue '
                            'JOIN entities e ON e.id=ue.entity_id WHERE ue.unit_id=? ORDER BY ue."start"', (unit_id,))
    toutes_sources = serveur.query('SELECT id, url, domain, cited_entity, kind, scope, confidence, inherited_from, contribution, '
                                   'source_date, substr(passage,1,500) passage, created FROM sources WHERE unit_id=? '
                                   'ORDER BY inherited_from IS NOT NULL, confidence DESC', (unit_id,))
    sources = {'propres': [s for s in toutes_sources if not s['inherited_from']],
               'heritees': [s for s in toutes_sources if s['inherited_from']]}
    type_affirmation = serveur.query('SELECT type, confidence, rule, depends_on FROM claim_types WHERE unit_id=?', (unit_id,), True)
    if type_affirmation:
        type_affirmation['depends_on'] = _json_lu(type_affirmation['depends_on'], type_affirmation['depends_on'])
    groupes = []
    for groupe in serveur.query('SELECT g.id, g.label, g.frame, g.legacy_claim_id, g.merged_into FROM unit_group_members m '
                                'JOIN unit_groups g ON g.id=m.group_id WHERE m.unit_id=?', (unit_id,)):
        groupe['frame'] = _json_lu(groupe['frame'], groupe['frame'])
        groupe['membres'] = serveur.query(
            'SELECT u.id, substr(u.text,1,300) text, u.version_id, p.url, p.title, u.status, u.temporal_status, u.anchor_date '
            'FROM unit_group_members m JOIN units u ON u.id=m.unit_id LEFT JOIN versions v ON v.id=u.version_id '
            'LEFT JOIN pages p ON p.id=v.page_id WHERE m.group_id=? ORDER BY u.anchor_date, u.id LIMIT 50', (groupe['id'],))
        groupe['nombre_membres'] = serveur.query('SELECT count(*) n FROM unit_group_members WHERE group_id=?',
                                                 (groupe['id'],), True)['n']
        paternite = serveur.query('SELECT parent_unit_id, criterion, detail, human, created FROM paternity WHERE group_id=?',
                                  (groupe['id'],), True)
        if paternite:
            paternite['detail'] = _json_lu(paternite['detail'], paternite['detail'])
        groupe['paternite'] = paternite
        groupes.append(groupe)
    relations = serveur.query(
        'SELECT r.a, r.b, r.relation, r.direction, r.nli_ab, r.nli_ba, r.engine, r.reason, r.created, '
        'substr(o.text,1,300) autre_texte, o.version_id autre_version FROM unit_relations r '
        'JOIN units o ON o.id=CASE WHEN r.a=? THEN r.b ELSE r.a END WHERE r.a=? OR r.b=? ORDER BY r.relation LIMIT 100',
        (unit_id, unit_id, unit_id))
    sujets = serveur.query('SELECT t.id, t.label, t.method, tm.degree FROM topic_membership tm JOIN topics t ON t.id=tm.topic_id '
                           "WHERE tm.target_kind='unit' AND tm.target_id=? ORDER BY tm.degree DESC LIMIT 10", (unit_id,))
    requetes = []
    for requete in serveur.query('SELECT q.* FROM query_units qu JOIN queries q ON q.id=qu.query_id WHERE qu.unit_id=? '
                                 'ORDER BY q.created', (unit_id,)):
        requetes.append(_requete_publique(requete))
    ids_requetes = [r['id'] for r in requetes]
    appels = []
    if ids_requetes:
        appels = serveur.query(
            'SELECT id, query_id, endpoint, query, status, http_status, units, units_remaining, duration_ms, cached_from, created '
            'FROM ibou_calls WHERE query_id IN (' + _marques(ids_requetes) + ') ORDER BY created', tuple(ids_requetes))
    ids_groupes = [g['id'] for g in groupes]
    preuves = []
    if ids_groupes:
        preuves = serveur.query(
            'SELECT id, query_id, group_id, call_id, url, title, domain, page_date, passage_start, relevance, kept, reject_reason, '
            'nli, nli_score, weight, substr(text,1,' + str(TEXTE_PREUVE_MAX) + ') text, length(text) longueur, created '
            'FROM evidence WHERE group_id IN (' + _marques(ids_groupes) + ') ORDER BY kept DESC, weight DESC, created LIMIT 60',
            tuple(ids_groupes))
    propositions = []
    from chaine import m6
    for proposition in serveur.query('SELECT id FROM proposals WHERE unit_id=? ORDER BY created', (unit_id,)):
        propositions.append(m6.carte(proposition['id']))
    liens = serveur.query('SELECT * FROM link_recommendations WHERE unit_id=? ORDER BY created', (unit_id,))
    return {
        'unite': ligne, 'page': page, 'chunk': chunk, 'paragraphe': paragraphe_unite(ligne), 'origines': origines_unite(ligne),
        'faits': faits, 'hypotheses': hypotheses, 'entites': entites,
        'sources': sources, 'type': type_affirmation, 'groupes': groupes, 'relations': relations, 'sujets': sujets,
        'requetes': requetes, 'appels_ibou': appels, 'preuves': preuves, 'propositions': propositions,
        'liens_recommandes': liens,
    }


COLONNES_RUN = ('status_run', 'temporal_run', 'critical_run', 'verdict_run', 'anchor_run', 'obs_run', 'facts_run_id', 'run_id')


def origines_unite(ligne):
    """Pour chaque colonne ``*_run`` de l'unite : l'execution qui l'a ecrite (``{etape, debut, statut, double_passe}``)
    ou la decision de revue (``{etape: 'decision', proposition}``) ; None si inconnue."""
    ids = {ligne.get(c) for c in COLONNES_RUN if ligne.get(c) and not str(ligne.get(c)).startswith('decision:')}
    executions = {}
    if ids:
        for run in _s().query('SELECT id, step, started, status, engines FROM step_runs WHERE id IN (' + _marques(ids) + ')',
                              tuple(ids)):
            moteurs_run = _json_lu(run['engines'], {}) or {}
            executions[run['id']] = {'etape': run['step'], 'debut': run['started'], 'statut': run['status'],
                                     'double_passe': moteurs_run.get('double_passe') if isinstance(moteurs_run, dict) else None}
    sortie = {}
    for colonne in COLONNES_RUN:
        valeur = ligne.get(colonne)
        if not valeur:
            sortie[colonne] = None
        elif str(valeur).startswith('decision:'):
            sortie[colonne] = {'etape': 'decision', 'proposition': str(valeur)[len('decision:'):]}
        else:
            sortie[colonne] = executions.get(valeur)
    return sortie


PARAGRAPHE_MAX = 2400


def paragraphe_unite(ligne):
    """Paragraphe entier de la version qui contient la phrase de l'unite, avec les offsets de la phrase.

    ``{texte, debut, fin, phrase_debut, phrase_fin, tronque, trouve}`` : ``debut``/``fin`` sont les offsets du
    paragraphe dans le texte de la version, ``phrase_debut``/``phrase_fin`` ceux de la phrase dans ``texte``.
    Paragraphe de plus de 2 400 caracteres : coupe autour de la phrase (``tronque``). Phrase introuvable dans la
    version (texte modifie depuis) : ``trouve`` faux et ``texte`` = texte de l'unite.
    """
    from chaine import m6
    version = _s().query('SELECT text FROM versions WHERE id=?', (ligne.get('version_id'),), True)
    texte = (version or {}).get('text') or ''
    norme = lambda t: re.sub(r'\s+', ' ', t or '').strip()
    debut, fin = ligne.get('span_start'), ligne.get('span_end')
    citation = ligne.get('quote') or ligne.get('text') or ''
    if not (texte and isinstance(debut, int) and isinstance(fin, int) and 0 <= debut < fin <= len(texte)
            and norme(texte[debut:fin]) == norme(citation)):
        position = texte.find(citation) if texte and citation else -1
        if position < 0:
            unite_texte = ligne.get('text') or ''
            return {'texte': unite_texte, 'debut': None, 'fin': None, 'phrase_debut': 0, 'phrase_fin': len(unite_texte),
                    'tronque': False, 'trouve': False}
        debut, fin = position, position + len(citation)
    a, b = m6.bornes_paragraphe(texte, debut, fin)
    tronque = False
    if b - a > PARAGRAPHE_MAX:
        marge = max(200, (PARAGRAPHE_MAX - (fin - debut)) // 2)
        a, b, tronque = max(a, debut - marge), min(b, fin + marge), True
    return {'texte': texte[a:b], 'debut': a, 'fin': b, 'phrase_debut': debut - a, 'phrase_fin': fin - a,
            'tronque': tronque, 'trouve': True}


FILTRES_PAGES = {
    'sans_date': "COALESCE(v.published,'')=''",
    'sans_maj': "COALESCE(v.updated,'')=''",
    'propositions': "EXISTS(SELECT 1 FROM proposals pr WHERE pr.version_id=v.id AND pr.status IN ('proposee','a_rediger','reportee'))",
}


def pages(parametres):
    """Liste paginee des versions de pages avec leurs compteurs, sujets et taux d'obsolescence.

    Filtres : ``q`` (URL ou titre), ``dernieres=1``, ``filtre`` (``sans_date``, ``sans_maj``, ``propositions``).
    ``taux_obsolescence`` : definition unique de ``m5.taux_obsolescence`` (aussi utilisee par l'export Excel).
    """
    from chaine import m5
    offset, limite = _pagination(parametres)
    conditions = []
    args = []
    texte = _param(parametres, 'q')
    if texte:
        conditions.append('(p.url LIKE ? OR p.title LIKE ?)')
        args.extend(['%' + texte[:200] + '%'] * 2)
    if _param(parametres, 'dernieres') == '1':
        conditions.append('v.rowid=(SELECT w.rowid FROM versions w WHERE w.page_id=v.page_id ORDER BY w.created DESC, w.rowid DESC LIMIT 1)')
    filtre = _param(parametres, 'filtre')
    if filtre:
        if filtre not in FILTRES_PAGES:
            raise ValueError('filtre : ' + ', '.join(FILTRES_PAGES) + '.')
        conditions.append(FILTRES_PAGES[filtre])
    colonnes = ('v.id version_id, v.page_id, p.url, p.title, v.created, v.published, v.updated, v.author, v.page_type, '
                'length(v.text) caracteres, v.segment_run IS NOT NULL segmentee, '
                '(v.rowid=(SELECT w.rowid FROM versions w WHERE w.page_id=v.page_id ORDER BY w.created DESC, w.rowid DESC LIMIT 1)) derniere, '
                '(SELECT count(*) FROM units u WHERE u.version_id=v.id) unites, '
                '(SELECT count(*) FROM units u WHERE u.version_id=v.id AND u.status=\'a_verifier\' AND COALESCE(u.excluded,0)=0) a_verifier, '
                '(SELECT count(*) FROM units u WHERE u.version_id=v.id AND u.temporal_status=\'perimee_par_date\') perimees, '
                '(SELECT count(*) FROM units u WHERE u.version_id=v.id AND u.critical=1) critiques, '
                '(SELECT count(*) FROM chunks c WHERE c.version_id=v.id AND c.kind IS NOT NULL) segments, '
                '(SELECT count(*) FROM proposals pr WHERE pr.version_id=v.id AND pr.status IN (\'proposee\',\'a_rediger\',\'reportee\')) '
                'propositions_en_attente')
    resultat = _page_liste('FROM versions v JOIN pages p ON p.id=v.page_id', conditions, args, 'p.url, v.created', colonnes,
                           offset, limite)
    lignes = resultat.pop('elements')
    ids = [l['version_id'] for l in lignes]
    taux = m5.taux_obsolescence(ids) if ids else {}
    sujets = {}
    if ids:
        for sujet in _s().query(
                "SELECT tm.target_id, t.label, tm.degree FROM topic_membership tm JOIN topics t ON t.id=tm.topic_id "
                "WHERE tm.target_kind='version' AND tm.target_id IN (" + _marques(ids) + ') ORDER BY tm.degree DESC', tuple(ids)):
            liste = sujets.setdefault(sujet['target_id'], [])
            if len(liste) < 3:
                liste.append({'libelle': sujet['label'], 'degre': round(float(sujet['degree'] or 0), 3)})
    for ligne in lignes:
        ligne['taux_obsolescence'] = taux.get(ligne['version_id'])
        ligne['sujets'] = sujets.get(ligne['version_id'], [])
    resultat['pages'] = lignes
    return resultat


def groupes(parametres):
    """Liste paginee des groupes d'unites (hors groupes fusionnes). Filtres : relation, paternite (critere),
    multi (1 : au moins deux membres), page, q."""
    offset, limite = _pagination(parametres)
    conditions = ['g.merged_into IS NULL', 'EXISTS(SELECT 1 FROM unit_group_members m WHERE m.group_id=g.id)']
    args = []
    relation = _param(parametres, 'relation')
    if relation:
        # Sous-requete non correlee (calculee une fois) : groupes dont un membre porte la relation.
        conditions.append('g.id IN (SELECT m.group_id FROM unit_relations r JOIN unit_group_members m ON m.unit_id=r.a '
                          'WHERE r.relation=? UNION SELECT m.group_id FROM unit_relations r JOIN unit_group_members m '
                          'ON m.unit_id=r.b WHERE r.relation=?)')
        args.extend([relation, relation])
    critere = _param(parametres, 'paternite')
    if critere == 'aucune':
        conditions.append('pa.group_id IS NULL')
    elif critere:
        conditions.append('pa.criterion=?')
        args.append(critere)
    if _param(parametres, 'multi') == '1':
        conditions.append('(SELECT count(*) FROM unit_group_members m WHERE m.group_id=g.id)>1')
    page = _param(parametres, 'page')
    if page:
        conditions.append('EXISTS(SELECT 1 FROM unit_group_members m JOIN units u ON u.id=m.unit_id WHERE m.group_id=g.id '
                          'AND u.version_id=?)')
        args.append(page)
    texte = _param(parametres, 'q')
    if texte:
        conditions.append('g.label LIKE ?')
        args.append('%' + texte[:200] + '%')
    colonnes = ('g.id, g.label, g.frame, g.legacy_claim_id, g.created, pa.parent_unit_id, pa.criterion paternite, pa.human, '
                '(SELECT count(*) FROM unit_group_members m WHERE m.group_id=g.id) membres, '
                '(SELECT count(DISTINCT u.version_id) FROM unit_group_members m JOIN units u ON u.id=m.unit_id WHERE m.group_id=g.id) pages')
    resultat = _page_liste('FROM unit_groups g LEFT JOIN paternity pa ON pa.group_id=g.id', conditions, args,
                           'membres DESC, g.id', colonnes, offset, limite)
    for groupe in resultat['elements']:
        groupe['frame'] = _json_lu(groupe['frame'], groupe['frame'])
    ids = [g['id'] for g in resultat['elements']]
    relations = {}
    if ids:
        for ligne in _s().query(
                'SELECT m.group_id, r.relation, count(*) n FROM unit_group_members m JOIN unit_relations r ON r.a=m.unit_id '
                'WHERE m.group_id IN (' + _marques(ids) + ') GROUP BY m.group_id, r.relation', tuple(ids)):
            relations.setdefault(ligne['group_id'], {})[ligne['relation']] = ligne['n']
    for groupe in resultat['elements']:
        groupe['relations'] = relations.get(groupe['id'], {})
    resultat['groupes'] = resultat.pop('elements')
    return resultat


def _requete_publique(requete):
    """Requete decodee pour l'interface, avec ``envoyable`` et sa raison."""
    from chaine import m4
    from chaine import m5
    requete = dict(requete)
    requete['checks'] = _json_lu(requete.get('checks'), requete.get('checks'))
    cadre = _json_lu(requete.pop('frame', None), {}) or {}
    requete['cadre'] = {cle: cadre.get(cle) for cle in ('sujet', 'lieu', 'predicat', 'classe', 'famille', 'valeur', 'inconnues',
                                                       'perimee', 'origine', 'gabarit_brut')}
    periode = cadre.get('periode')
    requete['cadre']['periode'] = periode.get('libelle') if isinstance(periode, dict) else periode
    checks = requete['checks'] if isinstance(requete.get('checks'), dict) else {}
    lisibles = problemes_lisibles(checks.get('problemes'))
    requete['problemes_lisibles'] = lisibles
    raison = None
    statut = requete.get('status')
    if requete.get('template') == 'ancien-pipeline':
        raison = "requête migrée de l'ancien parcours : remplacée par M4"
    elif not m5.envoyable_repli(requete):
        raison = 'repli « Est-ce que <unité> ? » : unité en attente de décomposition (M1 faits)'
    elif statut == 'a_revoir':
        raison = 'contrôles échoués : ' + ('; '.join(lisibles) if lisibles else 'raison non enregistrée')
        rejet = checks.get('reformulation_rejetee')
        if isinstance(rejet, dict):
            raison += (' ; la reformulation automatique a aussi échoué' if rejet.get('texte')
                       else ' ; pas de reformulation automatique (LLM local indisponible)')
        raison += '. Corrigez la requête à la main pour la rendre envoyable.'
    elif statut == 'plus_tard':
        raison = 'non critique : envoyée seulement si vous incluez la file « plus tard » au lancement de M5'
    elif statut == 'envoyee':
        raison = 'déjà envoyée à Ibou'
    elif statut == 'abandonnee':
        raison = 'abandonnée (remplacée ou devenue inutile)'
    elif statut != 'prete':
        raison = 'statut ' + str(statut)
    requete['envoyable'] = raison is None
    requete['raison_non_envoyable'] = raison
    requete['repli'] = m4.est_repli(requete.get('template'))
    return requete


_PROBLEMES = (
    (re.compile(r'^requete vide$'), lambda m: 'requête vide'),
    (re.compile(r'^plus de (\d+) caracteres$'), lambda m: 'plus de ' + m.group(1) + ' caractères'),
    (re.compile(r'^nombre de mots hors de (\d+) a (\d+)$'), lambda m: 'nombre de mots hors de ' + m.group(1) + ' à ' + m.group(2)),
    (re.compile(r'^identifiant ou nom de predicat : (.*)$'), lambda m: 'identifiant technique dans le texte : ' + ', '.join(
        'tiret bas (_)' if x == 'underscore' else ('nom de prédicat « ' + x[9:] + ' »' if x.startswith('predicat ') else '« ' + x + ' »')
        for x in m.group(1).split(', '))),
    (re.compile(r'^fragment de JSON$'), lambda m: 'fragment de JSON'),
    (re.compile(r'^plus de (\d+) questions$'), lambda m: 'plus de ' + m.group(1) + ' questions'),
    (re.compile(r'^accents absents : (.*)$'), lambda m: 'accents manquants sur : ' + ', '.join('« ' + x + ' »' for x in m.group(1).split(', '))),
    (re.compile(r'^date au format ISO$'), lambda m: 'date au format technique (AAAA-MM-JJ)'),
    (re.compile(r'^score de naturel insuffisant \((.*)\)$'), lambda m: 'formulation peu naturelle (score ' + m.group(1) + ')'),
    (re.compile(r'^la question presuppose la valeur$'),
     lambda m: 'la question contient déjà la valeur à vérifier (biais de confirmation)'),
    (re.compile(r'^la reformulation presuppose la valeur$'),
     lambda m: 'la reformulation contient déjà la valeur à vérifier (biais de confirmation)'),
    (re.compile(r'^la reformulation ne nomme plus le sujet du cadre$'), lambda m: 'la reformulation ne nomme plus le sujet'),
    (re.compile(r'^la requete ne nomme plus le sujet du cadre \((.*)\)$'),
     lambda m: 'la requête ne nomme plus le sujet du cadre (« ' + m.group(1) + ' »)'),
    (re.compile(r'^LLM indisponible ou sortie invalide$'), lambda m: 'LLM local indisponible ou réponse invalide'),
)


def problemes_lisibles(problemes):
    """Problemes de controle d'une requete (texte ASCII de M4) en phrases francaises."""
    sortie = []
    for probleme in problemes or []:
        texte = probleme if isinstance(probleme, str) else str((probleme or {}).get('detail') or (probleme or {}).get('code') or '')
        for motif, traduire in _PROBLEMES:
            trouve = motif.match(texte)
            if trouve:
                texte = traduire(trouve)
                break
        sortie.append(texte)
    return sortie


def requetes(parametres):
    """Liste paginee des requetes M4. Filtres : statut, kind (ouverte|controle), critique, repli (0|1), q."""
    offset, limite = _pagination(parametres)
    conditions = []
    args = []
    statut = _param(parametres, 'statut')
    if statut:
        conditions.append('q.status=?')
        args.append(statut)
    kind = _param(parametres, 'kind')
    if kind:
        conditions.append('q.kind=?')
        args.append(kind)
    critique = _param(parametres, 'critique')
    if critique in ('0', '1'):
        conditions.append('COALESCE(q.critical,0)=?')
        args.append(int(critique))
    repli = _param(parametres, 'repli')
    if repli == '1':
        conditions.append("COALESCE(q.template,'') LIKE '%unite:fermee'")
    elif repli == '0':
        conditions.append("COALESCE(q.template,'') NOT LIKE '%unite:fermee'")
    texte = _param(parametres, 'q')
    if texte:
        conditions.append('q.text LIKE ?')
        args.append('%' + texte[:200] + '%')
    colonnes = ('q.*, (SELECT count(*) FROM query_units qu WHERE qu.query_id=q.id) unites, '
                '(SELECT count(*) FROM ibou_calls c WHERE c.query_id=q.id) appels')
    resultat = _page_liste('FROM queries q', conditions, args, 'COALESCE(q.critical,0) DESC, COALESCE(q.priority,0) DESC, q.id',
                           colonnes, offset, limite)
    resultat['requetes'] = [_requete_publique(r) for r in resultat.pop('elements')]
    return resultat


def verifications(parametres):
    """Liste paginee des appels Ibou (sans le corps des reponses). Filtres : statut, requete, ancien (0|1)."""
    offset, limite = _pagination(parametres)
    conditions = []
    args = []
    statut = _param(parametres, 'statut')
    if statut:
        conditions.append('c.status=?')
        args.append(statut)
    requete = _param(parametres, 'requete')
    if requete:
        conditions.append('c.query_id=?')
        args.append(requete)
    ancien = _param(parametres, 'ancien')
    if ancien == '1':
        conditions.append("COALESCE(c.legacy_call_id,'')<>''")
    elif ancien == '0':
        conditions.append("COALESCE(c.legacy_call_id,'')=''")
    colonnes = ('c.id, c.query_id, c.endpoint, c.query, c.status, c.http_status, c.units, c.units_remaining, c.max_results, '
                'c.duration_ms, c.cached_from, c.legacy_call_id, c.created, length(c.response) taille_reponse, '
                'q.kind requete_kind, q.critical, '
                '(SELECT count(*) FROM evidence e WHERE e.call_id=c.id) preuves, '
                '(SELECT count(*) FROM evidence e WHERE e.call_id=c.id AND e.kept=1) preuves_retenues')
    resultat = _page_liste('FROM ibou_calls c LEFT JOIN queries q ON q.id=c.query_id', conditions, args, 'c.created DESC, c.id',
                           colonnes, offset, limite)
    for appel in resultat['elements']:
        appel['resultats'] = None
    ids = [a['id'] for a in resultat['elements']]
    if ids:
        for ligne in _s().query('SELECT id, response FROM ibou_calls WHERE id IN (' + _marques(ids) + ')', tuple(ids)):
            donnees = _json_lu(ligne['response'], None)
            if isinstance(donnees, dict) and isinstance(donnees.get('results'), list):
                for appel in resultat['elements']:
                    if appel['id'] == ligne['id']:
                        appel['resultats'] = len(donnees['results'])
    resultat['appels'] = resultat.pop('elements')
    resultat['budgets'] = _s().query('SELECT id, cap, used, created FROM budgets ORDER BY created DESC LIMIT 10')
    return resultat


def propositions(parametres):
    """Cartes de revue M6 (``m6.cartes``), paginees. ``statut`` : un statut, ``en_attente`` ou vide (tous) ;
    ``motif`` (code du motif), ``page`` (version_id ou page_id), ``unite`` (unit_id).

    ``motifs`` : nombre de propositions par motif pour le statut demande (sans les autres filtres), pour le filtre.
    """
    from chaine import m6
    offset, limite = _pagination(parametres)
    statut = _param(parametres, 'statut')
    resultat = m6.cartes(statut=statut, offset=offset, limite=limite, motif=_param(parametres, 'motif'),
                         page=_param(parametres, 'page'), unite=_param(parametres, 'unite'))
    resultat['bilan'] = m6.bilan()
    if statut == 'en_attente':
        statuts = list(m6.EN_ATTENTE)
    elif statut:
        statuts = [statut]
    else:
        statuts = []
    clause = (' WHERE status IN (' + _marques(statuts) + ')') if statuts else ''
    resultat['motifs'] = {l['kind']: l['n'] for l in _s().query(
        'SELECT kind, count(*) n FROM proposals' + clause + ' GROUP BY kind ORDER BY n DESC', tuple(statuts))}
    return resultat


def liens(parametres):
    """Liens internes recommandes (M3, ``link_recommendations``), pagines. Filtres : ``statut``, ``page``
    (version source ou cible), ``q`` (ancre ou texte de l'unite)."""
    offset, limite = _pagination(parametres)
    conditions = []
    args = []
    statut = _param(parametres, 'statut')
    if statut:
        conditions.append('l.status=?')
        args.append(statut)
    page = _param(parametres, 'page')
    if page:
        conditions.append('(l.from_version=? OR l.to_version=?)')
        args.extend([page, page])
    texte = _param(parametres, 'q')
    if texte:
        conditions.append('(l.anchor LIKE ? OR u.text LIKE ?)')
        args.extend(['%' + texte[:200] + '%'] * 2)
    sql_from = ('FROM link_recommendations l LEFT JOIN units u ON u.id=l.unit_id '
                'LEFT JOIN versions vf ON vf.id=l.from_version LEFT JOIN pages pf ON pf.id=vf.page_id '
                'LEFT JOIN versions vt ON vt.id=l.to_version LEFT JOIN pages pt ON pt.id=vt.page_id')
    colonnes = ('l.id, l.unit_id, l.from_version, l.to_version, l.anchor, l.reason, l.status, l.created, '
                'substr(u.text,1,300) texte_unite, pf.url source_url, pf.title source_titre, pt.url cible_url, pt.title cible_titre, '
                "(SELECT pr.id FROM proposals pr WHERE pr.unit_id=l.unit_id AND pr.kind='lien_interne' ORDER BY pr.created DESC LIMIT 1) proposition_id, "
                "(SELECT pr.status FROM proposals pr WHERE pr.unit_id=l.unit_id AND pr.kind='lien_interne' ORDER BY pr.created DESC LIMIT 1) proposition_statut")
    resultat = _page_liste(sql_from, conditions, args, 'pf.url, u.span_start, l.id', colonnes, offset, limite)
    resultat['liens'] = resultat.pop('elements')
    resultat['par_statut'] = {l['status']: l['n'] for l in _s().query(
        'SELECT status, count(*) n FROM link_recommendations GROUP BY status')}
    return resultat


def appel(call_id):
    """Un appel Ibou : requete envoyee (et son cadre), unites couvertes et leur verdict, resultats renvoyes,
    preuves retenues ou ecartees (avec la raison) et verdict NLI de chaque preuve."""
    serveur = _s()
    ligne = serveur.query('SELECT id, query_id, endpoint, query, status, http_status, units, units_remaining, max_results, '
                          'duration_ms, cached_from, legacy_call_id, created, response FROM ibou_calls WHERE id=?', (call_id,), True)
    if not ligne:
        raise Introuvable('Appel Ibou introuvable : ' + str(call_id))
    donnees = _json_lu(ligne.pop('response'), None)
    resultats = []
    if isinstance(donnees, dict) and isinstance(donnees.get('results'), list):
        for element in donnees['results'][:30]:
            if isinstance(element, dict):
                resultats.append({'url': element.get('url'), 'titre': element.get('title'),
                                  'domaine': element.get('host') or element.get('domain')})
    requete = None
    unites_couvertes = []
    if ligne.get('query_id'):
        brute = serveur.query('SELECT q.*, (SELECT count(*) FROM query_units qu WHERE qu.query_id=q.id) unites, '
                              '(SELECT count(*) FROM ibou_calls c WHERE c.query_id=q.id) appels FROM queries q WHERE q.id=?',
                              (ligne['query_id'],), True)
        requete = _requete_publique(brute) if brute else None
        unites_couvertes = serveur.query(
            'SELECT u.id, substr(u.text,1,300) text, u.status, u.verdict, u.verified_at, u.critical, p.title, p.url '
            'FROM query_units qu JOIN units u ON u.id=qu.unit_id LEFT JOIN versions v ON v.id=u.version_id '
            'LEFT JOIN pages p ON p.id=v.page_id WHERE qu.query_id=? ORDER BY p.url, u.span_start LIMIT 50', (ligne['query_id'],))
    preuves = serveur.query(
        'SELECT id, url, title, domain, page_date, relevance, kept, reject_reason, nli, nli_score, weight, extracted, '
        'substr(text,1,' + str(TEXTE_PREUVE_MAX) + ') text, length(text) longueur FROM evidence WHERE call_id=? '
        'ORDER BY kept DESC, weight DESC, relevance DESC', (call_id,))
    ligne['resultats'] = len(resultats) if donnees is not None else None
    for preuve in preuves:
        preuve['selections'] = [e for e in (_json_lu(preuve.pop('extracted'), []) or [])
                               if e.get('type') == 'selection']
    return {'appel': ligne, 'requete': requete, 'unites': unites_couvertes, 'resultats': resultats, 'preuves': preuves,
            'retenues': sum(1 for p in preuves if p['kept']), 'ecartees': sum(1 for p in preuves if not p['kept'])}


def solde():
    """Solde Ibou (unites restantes) ; sans cle : ``disponible`` faux et la raison (aucun appel)."""
    serveur = _s()
    if not serveur.CONFIG.get('ibou_key'):
        return {'disponible': False, 'solde': None,
                'raison': 'Clé Ibou manquante : renseignez-la dans Moteurs et réglages, onglet Connexions.'}
    resultat = serveur.ibou_balance()
    return {'disponible': True, 'solde': resultat.get('remaining', 0), 'raison': None}


def journal(parametres):
    """Journal des evenements (table ``events``), pagine, du plus recent au plus ancien. ``q`` : texte contenu ;
    ``chaine=1`` : evenements de la chaine seulement."""
    offset, limite = _pagination(parametres)
    conditions = []
    args = []
    texte = _param(parametres, 'q')
    if texte:
        conditions.append('message LIKE ?')
        args.append('%' + texte[:200] + '%')
    if _param(parametres, 'chaine') == '1':
        conditions.append("message LIKE 'Chaîne%'")
    resultat = _page_liste('FROM events', conditions, args, 'created DESC, id', 'id, message, created', offset, limite)
    resultat['evenements'] = resultat.pop('elements')
    return resultat


def evaluation(parametres):
    """Mesures du protocole d'evaluation, saisies humaines, apprentissage M6."""
    from chaine import evaluation as module
    from chaine import m6
    reference = module.reference_courante()
    saisies = {mesure: len(module._saisies(mesure)) for mesure in module.A_SAISIR}
    return {
        'reference': {'chargee': reference is not None, 'nom': (reference or {}).get('nom'),
                      'pages': len((reference or {}).get('pages') or [])},
        'mesures': module.mesurer(),
        'a_saisir': {mesure: {'libelle': valeurs[1], 'methodologie': valeurs[0], 'notes': saisies[mesure]}
                     for mesure, valeurs in module.A_SAISIR.items()},
        'apprentissage': m6.apprentissage(),
    }


def gabarit_evaluation(parametres):
    """Gabarit d'annotation pre-rempli pour les versions demandees (``versions=a,b``, 20 au plus)."""
    from chaine import evaluation as module
    brut = _param(parametres, 'versions')
    if not brut:
        raise ValueError('Paramètre versions requis (identifiants de versions séparés par des virgules).')
    identifiants = [v.strip() for v in brut.split(',') if v.strip()][:20]
    return module.gabarit(identifiants)


def reglages():
    """Reglages de la chaine exposes a l'interface."""
    from chaine import m4
    from chaine import migration
    from chaine import moteurs
    from chaine import predicats
    from chaine import seuils
    surcharges = seuils.surcharges()
    domaines_regles = noyau.reglage('site:domaines')
    try:
        domaines = json.loads(domaines_regles) if domaines_regles else []
    except ValueError:
        domaines = [d.strip() for d in domaines_regles.split(',') if d.strip()]
    fonctions = sorted(set(moteurs.NIVEAUX) | {'relations'})
    cles_modeles = sorted(set(moteurs.MODELES) | {'relations'})
    return {
        'domaines': domaines if isinstance(domaines, list) else [domaines],
        'domaines_effectifs': sorted(migration.domaines_du_site()),
        'moteurs': {nom: noyau.reglage('moteur:' + nom, 'auto') for nom in fonctions},
        'modeles': {cle: moteurs.modele(cle) if cle != 'relations' else noyau.reglage('modele:relations', 'jackboyla/glirel-large-v0')
                    for cle in cles_modeles},
        'seuils': [{'nom': nom, 'valeur': surcharges.get(nom, defaut), 'defaut': defaut, 'surcharge': nom in surcharges}
                   for nom, defaut in sorted(seuils.DEFAUTS.items())],
        'option_unites': noyau.reglage('m1:unites:option', 'auto'),
        'repli_requetes_unite': m4.repli_autorise(),
        'llm_tiers_autorise': noyau.llm_tiers_autorise(),
        'nli_par_llm': moteurs.nli_par_llm_autorise(),
        'autres': {cle: noyau.reglage(cle) for cle in REGLAGES_LIBRES},
        'predicats': {'version': predicats.version(), 'nombre': len(predicats.noms()), 'chemin': str(predicats.chemin())},
        'choix': {'moteur': list(moteurs.MODES), 'option_unites': list(OPTIONS_UNITES)},
    }


def lire(chemin, parametres):
    """GET ``/api/chaine/...`` -> ``(code, objet)`` ou ``(code, octets, type, nom)``."""
    from chaine import pipeline
    try:
        nom = chemin[len('/api/chaine/'):]
        if nom == 'etat':
            return 200, pipeline.etat()
        if nom == 'unites':
            return 200, unites(parametres)
        if nom.startswith('unite/'):
            return 200, unite(nom[len('unite/'):])
        if nom == 'pages':
            return 200, pages(parametres)
        if nom == 'groupes':
            return 200, groupes(parametres)
        if nom == 'requetes':
            return 200, requetes(parametres)
        if nom == 'verifications':
            return 200, verifications(parametres)
        if nom == 'propositions':
            return 200, propositions(parametres)
        if nom == 'liens':
            return 200, liens(parametres)
        if nom.startswith('appel/'):
            return 200, appel(nom[len('appel/'):])
        if nom == 'solde':
            return 200, solde()
        if nom == 'journal':
            return 200, journal(parametres)
        if nom.startswith('proposition/'):
            from chaine import m6
            try:
                return 200, m6.carte(nom[len('proposition/'):])
            except ValueError as erreur:
                raise Introuvable(str(erreur)) from erreur
        if nom == 'evaluation':
            return 200, evaluation(parametres)
        if nom == 'evaluation/gabarit':
            return 200, gabarit_evaluation(parametres)
        if nom == 'reglages':
            return 200, reglages()
        if nom == 'predicats':
            from chaine import predicats
            return 200, {'version': predicats.version(), 'chemin': str(predicats.chemin()), 'predicats': predicats.tous()}
        if nom == 'export.xlsx':
            from chaine import export
            contenu = export.excel()
            nom_fichier = 'verification-' + date.today().isoformat() + '.xlsx'
            return 200, contenu, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', nom_fichier
        return 404, {'error': 'Route inconnue : ' + chemin}
    except Introuvable as erreur:
        return 404, {'error': str(erreur)}
    except (ValueError, KeyError) as erreur:
        return 400, {'error': str(erreur)}
    except _s().OperationalError as erreur:
        return 502, {'error': str(erreur)}
    except sqlite3.Error:
        return 503, {'error': 'Base SQLite temporairement inaccessible ou occupée. Réessayez dans quelques secondes.'}


# ---------------------------------------------------------------------------
# Ecriture
# ---------------------------------------------------------------------------

def _texte(donnees, nom, maximum=12000, requis=True):
    """Champ texte d'un corps JSON."""
    valeur = donnees.get(nom)
    if valeur is None and not requis:
        return None
    if not isinstance(valeur, str) or (requis and not valeur.strip()):
        raise ValueError('Champ ' + nom + ' : texte requis.')
    if len(valeur) > maximum:
        raise ValueError('Champ ' + nom + ' : ' + str(maximum) + ' caractères au plus.')
    return valeur


def _identifiant(donnees, nom):
    """Identifiant texte court."""
    valeur = donnees.get(nom)
    if not isinstance(valeur, str) or not valeur or len(valeur) > 100:
        raise ValueError('Champ ' + nom + ' : identifiant requis.')
    return valeur


def lancer(donnees):
    """Demarre une etape (ou ``tout``) dans le mecanisme de tache du serveur."""
    from chaine import pipeline
    serveur = _s()
    etape = donnees.get('etape')
    if etape != 'tout' and etape not in pipeline.FONCTIONS:
        raise ValueError('Étape inconnue : ' + str(etape) + ' (attendu : tout, ' + ', '.join(pipeline.FONCTIONS) + ').')
    params = {}
    budget = donnees.get('budget')
    if budget is not None:
        if type(budget) is not int or not 0 <= budget <= 1_000_000:
            raise ValueError('Budget Ibou : entier entre 0 et 1 000 000 unités.')
        params['budget'] = budget
    pages_demandees = donnees.get('pages')
    if pages_demandees is not None:
        if not isinstance(pages_demandees, list) or len(pages_demandees) > 1000 or any(not isinstance(p, str) for p in pages_demandees):
            raise ValueError('pages : liste d’identifiants de versions.')
        params['pages'] = pages_demandees
    for drapeau in ('forcer', 'plus_tard'):
        if drapeau in donnees:
            if type(donnees[drapeau]) is not bool:
                raise ValueError(drapeau + ' : booléen attendu.')
            params[drapeau] = donnees[drapeau]
    option = donnees.get('option')
    if option is not None:
        if option not in OPTIONS_UNITES:
            raise ValueError('option : ' + ', '.join(OPTIONS_UNITES) + '.')
        params['option'] = option
    if etape == 'm5' and 'budget' not in params:
        raise ValueError('Indiquez un budget Ibou (plafond entier) pour M5.')
    serveur.start_job('chaine', lambda: pipeline.executer_tache(etape, **params))
    return {'ok': True, 'etape': etape, 'kind': 'chaine'}


def decision(donnees):
    """Decision de revue M6 : renvoie ``ok``/``raison`` et le detail de ``m6.decider``."""
    from chaine import m6
    proposition = _identifiant(donnees, 'id')
    choix = donnees.get('decision')
    if not isinstance(choix, str) or not choix:
        raise ValueError('Champ decision requis (accepter, modifier, rejeter, archive, reporter).')
    texte = _texte(donnees, 'texte', requis=False)
    note = _texte(donnees, 'note', 2000, requis=False) or ''
    for drapeau in ('propager', 'forcer'):
        if drapeau in donnees and type(donnees[drapeau]) is not bool:
            raise ValueError(drapeau + ' : booléen attendu.')
    resultat = m6.decider(proposition, choix, texte=texte, note=note, propager=bool(donnees.get('propager')),
                          forcer=bool(donnees.get('forcer')))
    resultat.setdefault('raison', None)
    return resultat


def paternite(donnees):
    """Arbitrage humain de la paternite d'un groupe (M3)."""
    from chaine import m3
    return {'ok': True, 'paternite': m3.arbitrer(_identifiant(donnees, 'group_id'), _identifiant(donnees, 'unit_id'))}


def requete(donnees):
    """Correction humaine d'une requete (M4, memes controles) ; ``problemes_lisibles`` en francais."""
    from chaine import m4
    resultat = m4.modifier(_identifiant(donnees, 'id'), _texte(donnees, 'texte', 500))
    resultat['problemes_lisibles'] = problemes_lisibles(resultat.get('problemes'))
    return resultat


CHAMPS_PREDICAT = ('libelle', 'classe', 'volatilite_jours', 'critique', 'gabarit', 'type_objet', 'verbalisation')


def modifier_predicat(donnees):
    """Modifie un predicat de la liste fermee (champs fournis seulement), valide puis ecrit toute la liste.

    ``{nom, libelle?, classe?, volatilite_jours?, critique?, gabarit?, type_objet?, verbalisation?}``. Refus (400,
    rien n'est ecrit) avec une raison en francais. La version de la liste est incrementee.
    """
    from chaine import predicats
    nom = _identifiant(donnees, 'nom')
    liste = predicats.tous()
    cible = next((p for p in liste if p['nom'] == nom), None)
    if cible is None:
        raise Introuvable('Prédicat introuvable : ' + nom)
    changes = {cle: donnees[cle] for cle in CHAMPS_PREDICAT if cle in donnees}
    if not changes:
        raise ValueError('Rien à modifier : indiquez au moins un champ (' + ', '.join(CHAMPS_PREDICAT) + ').')
    if 'libelle' in changes and (not isinstance(changes['libelle'], str) or not changes['libelle'].strip()
                                 or len(changes['libelle']) > 120):
        raise ValueError('Libellé : texte de 1 à 120 caractères.')
    if 'classe' in changes and changes['classe'] not in predicats.CLASSES:
        raise ValueError('Classe : ' + ', '.join(predicats.CLASSES) + '.')
    if 'gabarit' in changes and changes['gabarit'] not in predicats.GABARITS:
        raise ValueError('Gabarit : ' + ', '.join(predicats.GABARITS) + '.')
    if 'type_objet' in changes and changes['type_objet'] not in predicats.TYPES_OBJET:
        raise ValueError('Type d’objet : ' + ', '.join(predicats.TYPES_OBJET) + '.')
    if 'critique' in changes and type(changes['critique']) is not bool:
        raise ValueError('Critique : vrai ou faux.')
    if 'volatilite_jours' in changes:
        valeur = changes['volatilite_jours']
        if valeur is not None and (type(valeur) is not int or not 0 < valeur <= 36500):
            raise ValueError('Volatilité : nombre entier de jours entre 1 et 36 500, ou vide (immuable).')
    if 'verbalisation' in changes and (not isinstance(changes['verbalisation'], str) or '{sujet}' not in changes['verbalisation']
                                       or '{objet}' not in changes['verbalisation']):
        raise ValueError('Verbalisation : doit contenir {sujet} et {objet}.')
    if changes.get('classe', cible['classe']) == 'immuable' and changes.get('volatilite_jours', cible['volatilite_jours']) is not None:
        raise ValueError('Un prédicat immuable n’a pas de volatilité : laissez-la vide (ou choisissez une autre classe).')
    if 'libelle' in changes:
        changes['libelle'] = changes['libelle'].strip()
    cible.update(changes)
    resultat = predicats.remplacer(liste)
    return dict(resultat, ok=True, predicat=cible, changes=changes)


def reclasser_sources():
    """Reclasse ``lien_interne`` / ``lien_externe`` des sources selon les domaines du site. Renvoie les compteurs."""
    from chaine import migration
    site = migration.domaines_du_site()
    lignes = _s().query("SELECT id, url, domain, kind FROM sources WHERE kind IN ('lien_interne','lien_externe') AND url IS NOT NULL")
    vers_interne = []
    vers_externe = []
    for ligne in lignes:
        nom = ligne['domain'] or migration.domaine(ligne['url'])
        interne = bool(nom) and migration.est_interne(nom, site)
        if interne and ligne['kind'] == 'lien_externe':
            vers_interne.append(ligne['id'])
        elif not interne and ligne['kind'] == 'lien_interne':
            vers_externe.append(ligne['id'])
    with noyau.connexion() as base:
        for nature, liste in (('lien_interne', vers_interne), ('lien_externe', vers_externe)):
            for debut in range(0, len(liste), 500):
                lot = liste[debut:debut + 500]
                base.execute('UPDATE sources SET kind=? WHERE id IN (' + _marques(lot) + ')', (nature,) + tuple(lot))
    return {'vers_lien_interne': len(vers_interne), 'vers_lien_externe': len(vers_externe), 'domaines': sorted(site)}


def modifier_reglages(donnees):
    """Applique les reglages fournis (chacun valide) ; renvoie les reglages et ce qui a change."""
    from chaine import m4
    from chaine import moteurs
    from chaine import seuils
    changes = {}
    if 'domaines' in donnees:
        liste = donnees['domaines']
        if isinstance(liste, str):
            liste = [d for d in re.split(r'[\s,;]+', liste) if d]
        if not isinstance(liste, list) or len(liste) > 50:
            raise ValueError('domaines : liste de domaines (50 au plus).')
        propres = []
        for element in liste:
            if not isinstance(element, str):
                raise ValueError('domaines : textes attendus.')
            nom = element.strip().lower()
            nom = re.sub(r'^https?://', '', nom).split('/')[0]
            if nom.startswith('www.'):
                nom = nom[4:]
            if not _RE_DOMAINE.match(nom):
                raise ValueError('Domaine invalide : ' + element)
            propres.append(nom)
        noyau.definir_reglage('site:domaines', json.dumps(sorted(set(propres))) if propres else None)
        changes['domaines'] = reclasser_sources()
    if 'moteurs' in donnees:
        valeurs = donnees['moteurs']
        if not isinstance(valeurs, dict):
            raise ValueError('moteurs : objet {fonction: auto|local|llm|regles}.')
        autorisees = set(moteurs.NIVEAUX) | {'relations'}
        for nom, valeur in valeurs.items():
            if nom not in autorisees:
                raise ValueError('Fonction de moteur inconnue : ' + str(nom))
            if valeur not in moteurs.MODES:
                raise ValueError('Mode de moteur invalide pour ' + nom + ' : ' + str(valeur))
        for nom, valeur in valeurs.items():
            noyau.definir_reglage('moteur:' + nom, None if valeur == 'auto' else valeur)
        changes['moteurs'] = valeurs
    if 'modeles' in donnees:
        valeurs = donnees['modeles']
        if not isinstance(valeurs, dict):
            raise ValueError('modeles : objet {cle: nom du modele}.')
        autorisees = set(moteurs.MODELES) | {'relations'}
        for cle, valeur in valeurs.items():
            if cle not in autorisees:
                raise ValueError('Clé de modèle inconnue : ' + str(cle))
            if valeur is not None and (not isinstance(valeur, str) or not valeur.strip() or len(valeur) > 200):
                raise ValueError('Nom de modèle invalide pour ' + cle)
        for cle, valeur in valeurs.items():
            noyau.definir_reglage('modele:' + cle, valeur.strip() if valeur else None)
        changes['modeles'] = valeurs
    if 'seuils' in donnees:
        valeurs = donnees['seuils']
        if not isinstance(valeurs, dict):
            raise ValueError('seuils : objet {nom: valeur} (null pour revenir au défaut).')
        converties = {}
        for nom, valeur in valeurs.items():
            if nom not in seuils.DEFAUTS:
                raise ValueError('Seuil inconnu : ' + str(nom))
            try:
                converties[nom] = None if valeur is None else seuils.convertir(nom, valeur)
            except (TypeError, ValueError) as erreur:
                attendu = type(seuils.DEFAUTS[nom]).__name__
                raise ValueError('Seuil ' + nom + ' : valeur invalide (' + attendu + ' attendu, défaut '
                                 + json.dumps(seuils.DEFAUTS[nom]) + ').') from erreur
        for nom, valeur in converties.items():
            if valeur is None:
                seuils.reinitialiser(nom)
            else:
                seuils.definir(nom, valeur)
        changes['seuils'] = converties
    if 'option_unites' in donnees:
        option = donnees['option_unites']
        if option not in OPTIONS_UNITES:
            raise ValueError('option_unites : ' + ', '.join(OPTIONS_UNITES) + '.')
        noyau.definir_reglage('m1:unites:option', None if option == 'auto' else option)
        changes['option_unites'] = option
    if 'repli_requetes_unite' in donnees:
        valeur = donnees['repli_requetes_unite']
        if type(valeur) is not bool:
            raise ValueError('repli_requetes_unite : booléen attendu.')
        noyau.definir_reglage(m4.REGLAGE_REPLI, '1' if valeur else None)
        changes['repli_requetes_unite'] = valeur
    for cle, reglage in (('llm_tiers_autorise', noyau.REGLAGE_LLM_TIERS), ('nli_par_llm', moteurs.REGLAGE_NLI_PAR_LLM)):
        # Hierarchie des moyens et double passe (docs/MOTEURS.md) : desactives par defaut.
        if cle in donnees:
            valeur = donnees[cle]
            if type(valeur) is not bool:
                raise ValueError(cle + ' : booléen attendu.')
            noyau.definir_reglage(reglage, '1' if valeur else None)
            changes[cle] = valeur
    if 'autres' in donnees:
        valeurs = donnees['autres']
        if not isinstance(valeurs, dict):
            raise ValueError('autres : objet {cle: valeur}.')
        for cle, valeur in valeurs.items():
            if cle not in REGLAGES_LIBRES:
                raise ValueError('Réglage inconnu : ' + str(cle))
            if valeur is not None and (not isinstance(valeur, str) or len(valeur) > 2000):
                raise ValueError('Réglage ' + cle + ' : texte de 2 000 caractères au plus.')
        for cle, valeur in valeurs.items():
            noyau.definir_reglage(cle, valeur if valeur else None)
        changes['autres'] = valeurs
    if not changes:
        raise ValueError('Aucun réglage reconnu (domaines, moteurs, modeles, seuils, option_unites, repli_requetes_unite, autres).')
    moteurs.reinitialiser()
    noyau.vider_reglages()
    return {'ok': True, 'changes': changes, 'reglages': reglages()}


def dates_page(donnees):
    """Date de publication / de mise a jour d'une version ; prepare le recalcul (M1 page, M3 paternite)."""
    serveur = _s()
    version_id = _identifiant(donnees, 'version_id')
    if not serveur.query('SELECT 1 x FROM versions WHERE id=?', (version_id,), True):
        raise Introuvable('Version introuvable : ' + version_id)
    champs = {}
    for nom, colonne in (('published', 'published'), ('updated', 'updated')):
        if nom not in donnees:
            continue
        valeur = donnees[nom]
        if valeur in (None, ''):
            champs[colonne] = ''
            continue
        if not isinstance(valeur, str) or not _RE_DATE.match(valeur):
            raise ValueError(nom + ' : date AAAA-MM-JJ attendue.')
        date.fromisoformat(valeur)
        champs[colonne] = valeur
    if not champs:
        raise ValueError('Indiquez published et/ou updated (AAAA-MM-JJ, vide pour effacer).')
    with noyau.connexion() as base:
        base.execute('UPDATE versions SET ' + ', '.join(c + '=?' for c in champs) + ', segment_run=NULL WHERE id=?',
                     tuple(champs.values()) + (version_id,))
        unites_maj = base.execute('UPDATE units SET anchor_run=NULL, obs_run=NULL WHERE version_id=?', (version_id,)).rowcount
        paternites = base.execute(
            "DELETE FROM paternity WHERE COALESCE(human,0)=0 AND criterion IN ('a_arbitrer','date_publication') AND group_id IN ("
            'SELECT m.group_id FROM unit_group_members m JOIN units u ON u.id=m.unit_id WHERE u.version_id=?)',
            (version_id,)).rowcount
    serveur.event('Chaîne · dates de la page ' + version_id + ' : ' + json.dumps(champs, ensure_ascii=False))
    return {'ok': True, 'version_id': version_id, 'dates': champs, 'unites_a_reancrer': unites_maj,
            'paternites_a_recalculer': paternites,
            'suite': 'relancer m1.page (ancrage, classes) puis m3 (paternité) : POST /api/chaine/lancer'}


def remplacer_predicats(donnees):
    """Remplace la liste fermee des predicats apres validation."""
    from chaine import predicats
    liste = donnees.get('predicats')
    version = donnees.get('version')
    if version is not None and (not isinstance(version, str) or len(version) > 100):
        raise ValueError('version : texte court attendu.')
    resultat = predicats.remplacer(liste, version)
    return dict(resultat, ok=True)


def evaluation_ecrire(donnees):
    """``{action: 'charger', reference}`` ou ``{action: 'saisir', mesure, valeur, etiquette?}``."""
    from chaine import evaluation as module
    action = donnees.get('action')
    if action == 'charger':
        reference = donnees.get('reference')
        if not isinstance(reference, dict):
            raise ValueError('reference : objet JSON du jeu de référence attendu.')
        chargee = module.charger(reference)
        return {'ok': True, 'nom': chargee.get('nom'), 'pages': len(chargee.get('pages') or [])}
    if action == 'saisir':
        mesure = donnees.get('mesure')
        valeur = donnees.get('valeur')
        if not isinstance(valeur, (bool, int, float)):
            raise ValueError('valeur : nombre (note 1 à 5) ou booléen (accord) attendu.')
        etiquette = donnees.get('etiquette')
        if etiquette is not None and not isinstance(etiquette, str):
            raise ValueError('etiquette : texte attendu.')
        return {'ok': True, 'notes': module.saisir(mesure, valeur, etiquette)}
    raise ValueError("action : 'charger' ou 'saisir'.")


def ecrire(chemin, donnees):
    """POST ``/api/chaine/...`` -> ``(code, objet)``."""
    from chaine import pipeline
    serveur = _s()
    try:
        nom = chemin[len('/api/chaine/'):]
        if nom == 'lancer':
            return 200, lancer(donnees)
        if nom == 'migrer':
            serveur.start_job('chaine', lambda: pipeline.executer_tache('migration'))
            return 200, {'ok': True, 'etape': 'migration', 'kind': 'chaine'}
        if nom == 'decision':
            return 200, decision(donnees)
        if nom == 'paternite':
            return 200, paternite(donnees)
        if nom == 'requete':
            return 200, requete(donnees)
        if nom == 'reglages':
            return 200, modifier_reglages(donnees)
        if nom == 'page':
            return 200, dates_page(donnees)
        if nom == 'predicats':
            return 200, remplacer_predicats(donnees)
        if nom == 'predicat':
            return 200, modifier_predicat(donnees)
        if nom == 'evaluation':
            return 200, evaluation_ecrire(donnees)
        return 404, {'error': 'Route inconnue : ' + chemin}
    except Introuvable as erreur:
        return 404, {'error': str(erreur)}
    except (ValueError, KeyError, TypeError) as erreur:
        message = erreur.args[0] if isinstance(erreur, KeyError) and erreur.args else str(erreur)
        return 400, {'error': str(message)}
    except serveur.OperationalError as erreur:
        return 502, {'error': str(erreur)}
    except sqlite3.Error:
        return 503, {'error': 'Base SQLite temporairement inaccessible ou occupée. Réessayez dans quelques secondes.'}
