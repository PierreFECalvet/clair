"""Migration de l'ancien pipeline vers le schema de la chaine (contrat 4.1).

Idempotente (identifiants deterministes, INSERT OR IGNORE), une transaction
par lot. Les tables anciennes ne sont jamais modifiees (seules les colonnes
ajoutees de ``evidence`` sont renseignees). Rapport de compteurs.
"""
import hashlib
import json
import re
import unicodedata
from urllib.parse import urlparse

from chaine import noyau
from chaine import seuils

MOIS = {
    'janvier': 1, 'janv': 1, 'fevrier': 2, 'fevr': 2, 'fev': 2, 'mars': 3, 'avril': 4, 'avr': 4,
    'mai': 5, 'juin': 6, 'juillet': 7, 'juil': 7, 'aout': 8, 'septembre': 9, 'sept': 9,
    'octobre': 10, 'oct': 10, 'novembre': 11, 'nov': 11, 'decembre': 12, 'dec': 12,
}

STATUTS = {
    'validated': ('sourcee', 'confirmee'),
    'correct': ('a_verifier', 'corrigee'),
    'review': ('a_verifier', None),
    'pending': ('a_verifier', None),
    'not_needed': ('non_verifiable', None),
}

STATUTS_PATCH = {
    'proposed': 'proposee',
    'applied': 'appliquee',
    'conflict': 'reportee',
    'accepted': 'acceptee',
    'rejected': 'rejetee',
}

TABLES_SUIVIES = ('units', 'unit_groups', 'unit_group_members', 'sources', 'queries', 'query_units', 'ibou_calls', 'proposals')
TABLES_ANCIENNES = ('claims', 'occurrences', 'claim_merges', 'screenings', 'calls', 'patches', 'evidence')

_MOTIF_MOIS = '|'.join(sorted(MOIS, key=len, reverse=True))
_DATE_COMPLETE = re.compile(r'\b(1er|[0-3]?\d)\s+(' + _MOTIF_MOIS + r')\.?\s+((?:19|20)\d{2})\b')
_DATE_MOIS = re.compile(r'\b(' + _MOTIF_MOIS + r')\.?\s+((?:19|20)\d{2})\b')
_DATE_NUMERIQUE = re.compile(r'\b([0-3]?\d)/([01]?\d)/((?:19|20)\d{2})\b')
_DATE_ISO = re.compile(r'\b((?:19|20)\d{2})-([01]\d)-([0-3]\d)\b')
_ANNEE = re.compile(r'(?<![\d.,])((?:19|20)\d{2})(?![\d.,]\d)')


def sans_accents(texte):
    """Minuscules sans diacritiques."""
    decompose = unicodedata.normalize('NFD', texte.casefold())
    return ''.join(c for c in decompose if not unicodedata.combining(c))


def lire_dates(texte):
    """Toutes les dates lisibles d'une expression, en ISO ``AAAA-MM-JJ``.

    Annee seule -> 1er janvier ; mois et annee -> 1er du mois. Renvoie une
    liste de tuples ``(iso, precision)`` avec precision ``jour|mois|annee``.
    """
    if not texte:
        return []
    source = sans_accents(texte).replace('’', "'")
    trouvees = []
    occupe = []

    def libre(debut, fin):
        return all(fin <= a or debut >= b for a, b in occupe)

    def ajouter(match, annee, mois, jour, precision):
        if not libre(match.start(), match.end()):
            return
        try:
            iso = _iso(int(annee), int(mois), int(jour))
        except ValueError:
            return
        occupe.append((match.start(), match.end()))
        trouvees.append((match.start(), iso, precision))

    for m in _DATE_ISO.finditer(source):
        ajouter(m, m.group(1), m.group(2), m.group(3), 'jour')
    for m in _DATE_NUMERIQUE.finditer(source):
        ajouter(m, m.group(3), m.group(2), m.group(1), 'jour')
    for m in _DATE_COMPLETE.finditer(source):
        jour = 1 if m.group(1) == '1er' else m.group(1)
        ajouter(m, m.group(3), MOIS[m.group(2)], jour, 'jour')
    for m in _DATE_MOIS.finditer(source):
        ajouter(m, m.group(2), MOIS[m.group(1)], 1, 'mois')
    for m in _ANNEE.finditer(source):
        ajouter(m, m.group(1), 1, 1, 'annee')
    trouvees.sort()
    return [(iso, precision) for _, iso, precision in trouvees]


def ancre_periode(periode):
    """Date d'ancrage tiree de ``claims.period`` : la plus recente date lisible, ou None."""
    dates = lire_dates(periode or '')
    if not dates:
        return None
    return max(iso for iso, _ in dates)


def domaine(url):
    """Domaine d'une URL sans ``www.``, en minuscules ; '' si illisible."""
    try:
        hote = urlparse(url).hostname or ''
    except ValueError:
        return ''
    hote = hote.lower()
    if hote.startswith('www.'):
        hote = hote[4:]
    return hote


def domaines_du_site(supplementaires=None):
    """Domaines consideres comme internes.

    Union de : domaines des URL http(s) de ``pages``, reglage ``site:domaines``
    (liste JSON ou texte separe par des virgules), argument ``supplementaires``.
    """
    serveur = noyau.srv()
    resultat = set()
    for ligne in serveur.query("SELECT url FROM pages WHERE url LIKE 'http%'"):
        nom = domaine(ligne['url'])
        if nom:
            resultat.add(nom)
    reglage = serveur.query("SELECT value FROM settings WHERE key='site:domaines'", one=True)
    if reglage and reglage['value']:
        try:
            valeurs = json.loads(reglage['value'])
        except ValueError:
            valeurs = reglage['value'].split(',')
        if isinstance(valeurs, str):
            valeurs = valeurs.split(',')
        resultat.update(domaine('http://' + v.strip()) for v in valeurs if v.strip())
    for valeur in supplementaires or []:
        resultat.add(domaine('http://' + valeur.strip()))
    resultat.discard('')
    return resultat


def est_interne(nom, site):
    """Vrai si le domaine ``nom`` appartient au site (ou a un sous-domaine)."""
    return any(nom == d or nom.endswith('.' + d) for d in site)


def migrer(taille_lot=500, domaines_site=None):
    """Migre l'ancien pipeline vers les tables de la chaine (contrat 4.1).

    Idempotente : relancer ne cree rien de nouveau. Une transaction par lot de
    ``taille_lot`` occurrences ; ``S.checkpoint()`` entre deux lots.
    Renvoie un rapport : compteurs avant/apres, lignes creees par type, spans
    retrouves/introuvables (avec exemples), lignes ignorees.
    """
    serveur = noyau.srv()
    rapport = {
        'avant': _compter(TABLES_SUIVIES),
        'anciennes': _compter(TABLES_ANCIENNES),
        'crees': {nom: 0 for nom in TABLES_SUIVIES},
        'spans': {'retrouves_chunk': 0, 'retrouves_version': 0, 'introuvables': 0, 'exemples_introuvables': []},
        'ancres': {'phrase': 0, 'publication': 0, 'inconnue': 0},
        'ignores': {},
        'evidence_rattachees': 0,
    }
    site = domaines_du_site(domaines_site)
    rapport['domaines_site'] = sorted(site)
    with noyau.etape('migration', moteurs={'migration': 'regles'}, prompt='', seuils=['m2.confiance.paragraphe']) as run:
        serveur.JOB.update(stage='migration', done=0, total=rapport['anciennes']['occurrences'], message='Migration des occurrences')
        _migrer_unites(run, rapport, site, taille_lot)
        _migrer_groupes(run, rapport)
        _migrer_requetes(run, rapport)
        _migrer_appels(rapport)
        _migrer_propositions(run, rapport)
        _rattacher_preuves(rapport, taille_lot)
        run.items = sum(rapport['crees'].values())
        run.note = 'crees=' + json.dumps(rapport['crees'], sort_keys=True)
    rapport['apres'] = _compter(TABLES_SUIVIES)
    rapport['run_id'] = run.id
    rapport['stock'] = _stock()
    serveur.JOB.update(message='Migration terminee')
    return rapport


# ---------------------------------------------------------------------------
# Etapes internes
# ---------------------------------------------------------------------------

def _migrer_unites(run, rapport, site, taille_lot):
    """occurrences -> units (+ sources)."""
    serveur = noyau.srv()
    confiance = seuils.get('m2.confiance.paragraphe')
    identifiants = [
        ligne['id'] for ligne in serveur.query(
            'SELECT o.id FROM occurrences o WHERE NOT EXISTS(SELECT 1 FROM units u WHERE u.id=o.id) ORDER BY o.id'
        )
    ]
    rapport['deja_migrees'] = rapport['anciennes']['occurrences'] - len(identifiants)
    # Une version deja decomposee par la chaine (unites extraites par M1) appartient a la chaine : les
    # affirmations que l'ancien parcours y aurait ajoutees ensuite ne sont pas migrees (doublons).
    decomposees = {
        ligne['id'] for ligne in serveur.query(
            "SELECT o.id FROM occurrences o JOIN chunks ch ON ch.id=o.chunk_id WHERE NOT EXISTS(SELECT 1 FROM units u WHERE u.id=o.id) "
            "AND EXISTS(SELECT 1 FROM units x WHERE x.version_id=ch.version_id AND COALESCE(x.extractor,'')<>'migration')"
        )
    }
    if decomposees:
        rapport['ignores']['version_decomposee_par_la_chaine'] = len(decomposees)
        identifiants = [i for i in identifiants if i not in decomposees]
    versions = {}
    verifications = {
        ligne['claim_id']: ligne['d']
        for ligne in serveur.query('SELECT claim_id, max(created) d FROM checks GROUP BY claim_id')
    }
    fait = 0
    for debut in range(0, len(identifiants), taille_lot):
        serveur.checkpoint()
        lot = identifiants[debut:debut + taille_lot]
        marques = ','.join('?' for _ in lot)
        lignes = serveur.query(
            'SELECT o.id, o.claim_id, o.chunk_id, o.quote, o.sources, c.id cid, c.text claim_text, c.period, c.status, '
            'c.excluded, c.vector, ch.id chid, ch.version_id, ch.start, ch.text chunk_text, v.published '
            'FROM occurrences o LEFT JOIN claims c ON c.id=o.claim_id LEFT JOIN chunks ch ON ch.id=o.chunk_id '
            'LEFT JOIN versions v ON v.id=ch.version_id WHERE o.id IN (' + marques + ')',
            tuple(lot),
        )
        horodatage = serveur.now()
        with noyau.connexion() as base:
            for ligne in lignes:
                if ligne['cid'] is None:
                    _ignorer(rapport, 'occurrence_sans_affirmation')
                    continue
                if ligne['chid'] is None:
                    _ignorer(rapport, 'occurrence_sans_chunk')
                    continue
                version_id = ligne['version_id']
                if version_id not in versions:
                    texte = serveur.query('SELECT text FROM versions WHERE id=?', (version_id,), True)
                    versions[version_id] = (texte or {}).get('text') or ''
                unite = _unite(ligne, versions[version_id], rapport)
                statut, verdict = STATUTS.get(ligne['status'] or 'pending', ('a_verifier', None))
                verifiee = verifications.get(ligne['claim_id']) if verdict else None
                curseur = base.execute(
                    'INSERT OR IGNORE INTO units(id,version_id,chunk_id,text,quote,span_start,span_end,anchor_date,anchor_kind,'
                    'temporal_status,status,verdict,verified_at,extractor,vector,legacy_claim_id,excluded,run_id,created) '
                    'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (ligne['id'], version_id, ligne['chunk_id'], ligne['claim_text'], unite['quote'],
                     unite['start'], unite['end'], unite['ancre'], unite['type_ancre'], 'a_jour', statut, verdict, verifiee,
                     'migration', ligne['vector'], ligne['claim_id'], int(ligne['excluded'] or 0), run.id, horodatage),
                )
                if curseur.rowcount:
                    rapport['crees']['units'] += 1
                    rapport['spans'][unite['span']] += 1
                    rapport['ancres'][unite['type_ancre']] += 1
                    if unite['span'] == 'introuvables' and len(rapport['spans']['exemples_introuvables']) < 5:
                        rapport['spans']['exemples_introuvables'].append({'occurrence': ligne['id'], 'quote': (ligne['quote'] or '')[:160]})
                rapport['crees']['sources'] += _sources(base, ligne, unite, site, confiance, run.id, horodatage)
        fait += len(lot)
        serveur.JOB.update(done=fait)


def _unite(ligne, texte_version, rapport):
    """Span, citation exacte et ancre d'une occurrence."""
    serveur = noyau.srv()
    citation = ligne['quote'] or ''
    texte_chunk = ligne['chunk_text'] or ''
    resultat = {'quote': citation, 'start': None, 'end': None, 'span': 'introuvables'}
    trouvee = serveur.source_quote(citation, texte_chunk) if citation.strip() else None
    if trouvee is not None:
        position = texte_chunk.find(trouvee)
        debut = int(ligne['start'] or 0) + position
        if texte_version[debut:debut + len(trouvee)] == trouvee:
            resultat.update(quote=trouvee, start=debut, end=debut + len(trouvee), span='retrouves_chunk')
    if resultat['start'] is None and citation.strip() and texte_version:
        trouvee = serveur.source_quote(citation, texte_version)
        if trouvee is not None:
            debut = texte_version.find(trouvee)
            resultat.update(quote=trouvee, start=debut, end=debut + len(trouvee), span='retrouves_version')
    ancre = ancre_periode(ligne['period'])
    if ancre:
        resultat.update(ancre=ancre, type_ancre='phrase')
    else:
        publication = ancre_periode(ligne['published'])
        if publication:
            resultat.update(ancre=publication, type_ancre='publication')
        else:
            resultat.update(ancre=None, type_ancre='inconnue')
    return resultat


def _sources(base, ligne, unite, site, confiance, run_id, horodatage):
    """occurrences.sources -> sources ; renvoie le nombre de lignes creees."""
    try:
        liste = json.loads(ligne['sources'] or '[]')
    except ValueError:
        liste = []
    if not isinstance(liste, list):
        liste = [liste]
    creees = 0
    for element in liste:
        valeur = str(element or '').strip()
        if not valeur:
            continue
        identifiant = _id('source', ligne['id'], valeur)
        if valeur.lower().startswith(('http://', 'https://')):
            nom = domaine(valeur)
            nature = 'lien_interne' if est_interne(nom, site) else 'lien_externe'
            url = valeur
            entite = None
        else:
            nom = None
            nature = 'source_nommee'
            url = None
            entite = valeur[:300]
        portee = 'phrase' if url and url in (unite['quote'] or '') else 'paragraphe'
        curseur = base.execute(
            'INSERT OR IGNORE INTO sources(id,unit_id,url,domain,cited_entity,kind,scope,confidence,contribution,run_id,created) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            (identifiant, ligne['id'], url, nom, entite, nature, portee, confiance, 'migration', run_id, horodatage),
        )
        creees += curseur.rowcount
    return creees


def _migrer_groupes(run, rapport):
    """claims -> unit_groups (+ membres). Les affirmations sans occurrence sont ignorees."""
    serveur = noyau.srv()
    horodatage = serveur.now()
    with noyau.connexion() as base:
        curseur = base.execute(
            "INSERT OR IGNORE INTO unit_groups(id,frame,label,legacy_claim_id,run_id,created) "
            "SELECT c.id, NULL, substr(c.text,1,300), c.id, ?, ? FROM claims c "
            "WHERE EXISTS(SELECT 1 FROM units u WHERE u.legacy_claim_id=c.id AND u.extractor='migration')",
            (run.id, horodatage),
        )
        rapport['crees']['unit_groups'] += curseur.rowcount
        curseur = base.execute(
            "INSERT OR IGNORE INTO unit_group_members(group_id,unit_id) "
            "SELECT u.legacy_claim_id, u.id FROM units u JOIN unit_groups g ON g.id=u.legacy_claim_id "
            "WHERE u.extractor='migration'"
        )
        rapport['crees']['unit_group_members'] += curseur.rowcount
        sans = base.execute('SELECT count(*) FROM claims c WHERE NOT EXISTS(SELECT 1 FROM occurrences o WHERE o.claim_id=c.id)').fetchone()[0]
    if sans:
        rapport['ignores']['affirmation_sans_occurrence'] = sans


def _migrer_requetes(run, rapport):
    """screenings.search_query -> queries (+ query_units)."""
    serveur = noyau.srv()
    horodatage = serveur.now()
    lignes = serveur.query(
        "SELECT s.claim_id, s.route, s.search_query, c.status, c.excluded, "
        "(SELECT count(*) FROM calls k WHERE k.claim_id=s.claim_id) appels, "
        "(SELECT count(*) FROM unit_groups g WHERE g.id=s.claim_id) groupe "
        "FROM screenings s LEFT JOIN claims c ON c.id=s.claim_id "
        "WHERE s.search_query IS NOT NULL AND trim(s.search_query)!='' ORDER BY s.claim_id"
    )
    with noyau.connexion() as base:
        for ligne in lignes:
            if ligne['status'] is None:
                _ignorer(rapport, 'requete_sans_affirmation')
                continue
            identifiant = _id('query', ligne['claim_id'])
            if ligne['excluded']:
                statut = 'abandonnee'
            elif ligne['status'] == 'pending' and ligne['route'] == 'search':
                statut = 'prete'
            elif ligne['appels'] or ligne['status'] in ('validated', 'correct'):
                statut = 'envoyee'
            else:
                statut = 'a_revoir'
            groupe = ligne['claim_id'] if ligne['groupe'] else None
            curseur = base.execute(
                'INSERT OR IGNORE INTO queries(id,group_id,text,kind,template,status,attempts,edited,run_id,created) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)',
                (identifiant, groupe, ligne['search_query'].strip(), 'ouverte', 'ancien-pipeline', statut,
                 1 if statut == 'envoyee' else 0, 0, run.id, horodatage),
            )
            rapport['crees']['queries'] += curseur.rowcount
            if groupe:
                curseur = base.execute(
                    'INSERT OR IGNORE INTO query_units(query_id,unit_id) SELECT ?, unit_id FROM unit_group_members WHERE group_id=?',
                    (identifiant, groupe),
                )
                rapport['crees']['query_units'] += curseur.rowcount


def _migrer_appels(rapport):
    """calls -> ibou_calls (legacy_call_id)."""
    serveur = noyau.srv()
    lignes = serveur.query('SELECT * FROM calls ORDER BY created')
    with noyau.connexion() as base:
        for ligne in lignes:
            requete = None
            if ligne.get('claim_id'):
                requete = _id('query', ligne['claim_id'])
                if not base.execute('SELECT 1 FROM queries WHERE id=?', (requete,)).fetchone():
                    requete = None
            max_results = None
            restantes = None
            try:
                reponse = json.loads(ligne['response'] or '{}')
            except ValueError:
                reponse = {}
            if isinstance(reponse, dict):
                max_results = reponse.get('max_results')
                restantes = reponse.get('units_remaining')
            texte = ligne['query'] or ''
            empreinte = hashlib.sha256((str(ligne['endpoint']) + '\n' + ' '.join(texte.split()).casefold()).encode()).hexdigest()
            curseur = base.execute(
                'INSERT OR IGNORE INTO ibou_calls(id,query_id,endpoint,query,fingerprint,max_results,status,http_status,units,'
                'units_remaining,response,headers,duration_ms,cached_from,legacy_call_id,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (ligne['id'], requete, ligne['endpoint'], texte, empreinte, _entier(max_results), ligne['status'],
                 ligne.get('http_status'), ligne['units'], _entier(restantes), ligne['response'], None,
                 ligne.get('duration_ms'), None, ligne['id'], ligne['created']),
            )
            rapport['crees']['ibou_calls'] += curseur.rowcount


def _migrer_propositions(run, rapport):
    """patches -> proposals (legacy_patch_id)."""
    serveur = noyau.srv()
    lignes = serveur.query('SELECT * FROM patches ORDER BY created')
    with noyau.connexion() as base:
        for ligne in lignes:
            candidates = base.execute(
                "SELECT id, quote FROM units WHERE legacy_claim_id=? AND version_id=? ORDER BY id",
                (ligne['claim_id'], ligne['version_id']),
            ).fetchall()
            unite = None
            for candidate in candidates:
                if candidate['quote'] == ligne['old']:
                    unite = candidate['id']
                    break
            if unite is None and candidates:
                unite = candidates[0]['id']
            if unite is None:
                _ignorer(rapport, 'proposition_sans_unite')
            statut = STATUTS_PATCH.get(ligne['status'], 'reportee')
            note = '' if ligne['status'] in STATUTS_PATCH and ligne['status'] != 'conflict' else 'ancien statut : ' + str(ligne['status'])
            curseur = base.execute(
                'INSERT OR IGNORE INTO proposals(id,unit_id,version_id,kind,"before","after",method,status,decision_note,'
                'final_text,legacy_patch_id,run_id,created) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (ligne['id'], unite, ligne['version_id'], 'corrigee', ligne['old'], ligne['new'], 'reecriture_llm', statut,
                 note, ligne['new'] if statut == 'appliquee' else None, ligne['id'], run.id, ligne['created']),
            )
            rapport['crees']['proposals'] += curseur.rowcount


def _rattacher_preuves(rapport, taille_lot):
    """evidence : group_id (= ancienne affirmation) et domaine, si absents."""
    serveur = noyau.srv()
    lignes = serveur.query(
        'SELECT e.id, e.claim_id, e.url FROM evidence e JOIN unit_groups g ON g.id=e.claim_id '
        'WHERE e.group_id IS NULL ORDER BY e.id'
    )
    for debut in range(0, len(lignes), taille_lot):
        serveur.checkpoint()
        with noyau.connexion() as base:
            for ligne in lignes[debut:debut + taille_lot]:
                base.execute(
                    'UPDATE evidence SET group_id=?, domain=COALESCE(domain,?) WHERE id=? AND group_id IS NULL',
                    (ligne['claim_id'], domaine(ligne['url'] or '') or None, ligne['id']),
                )
                rapport['evidence_rattachees'] += 1


# ---------------------------------------------------------------------------
# Outils
# ---------------------------------------------------------------------------

def _stock():
    """Etat du stock migre (toutes executions confondues)."""
    serveur = noyau.srv()
    ligne = serveur.query(
        "SELECT count(*) n, sum(span_start IS NULL) sans_span, sum(anchor_kind='phrase') ancre_phrase "
        "FROM units WHERE extractor='migration'",
        one=True,
    )
    return {cle: int(ligne[cle] or 0) for cle in ('n', 'sans_span', 'ancre_phrase')}


def _compter(tables):
    """Nombre de lignes par table."""
    serveur = noyau.srv()
    return {table: serveur.query(f'SELECT count(*) n FROM {table}', one=True)['n'] for table in tables}


def _ignorer(rapport, motif):
    """Compte une ligne ignoree."""
    rapport['ignores'][motif] = rapport['ignores'].get(motif, 0) + 1


def _id(*parties):
    """Identifiant deterministe de 16 caracteres."""
    return hashlib.sha1('|'.join(str(p) for p in ('migration',) + parties).encode()).hexdigest()[:16]


def _entier(valeur):
    """Entier ou None."""
    try:
        return int(valeur) if valeur is not None else None
    except (TypeError, ValueError):
        return None


def _iso(annee, mois, jour):
    """Date ISO validee."""
    from datetime import date
    return date(annee, mois, jour).isoformat()
