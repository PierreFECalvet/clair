"""Protocole d'evaluation : jeu de reference annote, gabarit d'annotation, mesures et seuils.

Format du jeu de reference (JSON) ::

    {"version": 1, "nom": "...",
     "pages": [{"version_id": "...", "url": "...", "type_page": "actualite|guide_pratique|analyse|fiche_produit|tribune",
                "texte": "(facultatif)",
                "unites": [{"id": "u1", "span": [debut, fin], "classe": "immuable|etat_courant|annonce|metadonnee|usage",
                            "type": "information_source|opinion|interpretation|connaissance_generale|sans_source",
                            "faits": [{"span": [debut, fin], "sujet": "", "predicat": "", "objet": ""}]}]}],
     "paires": [{"a": "u1", "b": "u2", "relation": "equivalence|precision|contradiction"}],
     "groupes": [{"id": "g1", "unites": ["u1", "u2"], "page_parente": "<version_id ou url>"}],
     "grappes": [{"id": "c1", "pages": ["<version_id ou url>", ...]}]}

Une unite annotee est appariee a une unite de la base par identifiant (meme version), sinon par
recouvrement de spans (intersection / union >= 0.5). Un fait est apparie par recouvrement de span
dans la meme version. Les notes humaines (M4, M5) se saisissent par ``saisir``.
"""
import json
import re

from chaine import noyau
from chaine import seuils

CLE_REFERENCE = 'evaluation:reference'
CLE_SAISIE = 'evaluation:saisie:'
RECOUVREMENT_MIN = 0.5
CLASSES = ('immuable', 'etat_courant', 'annonce', 'metadonnee', 'usage')
TYPES = ('information_source', 'opinion', 'interpretation', 'connaissance_generale', 'sans_source')
RELATIONS = ('equivalence', 'precision', 'contradiction')
PRIORITAIRES = ('sans_source', 'opinion')
A_SAISIR = {
    'm4.note_humaine': ('M4 requêtes', "Note humaine à l'aveugle : « aurait pu être tapée par quelqu'un » (1 à 5)", 'evaluation.m4.note_humaine'),
    'm5.accord': ('M5 verdicts', 'Accord avec une vérification manuelle (confirmée et corrigée)', 'evaluation.m5.accord'),
}


def _seuil(nom, defaut=None):
    try:
        return seuils.get(nom)
    except KeyError:
        return defaut


# ---------------------------------------------------------------------------
# Chargement et validation
# ---------------------------------------------------------------------------

def valider(reference):
    """Problemes de format d'un jeu de reference (liste vide si conforme)."""
    problemes = []
    if not isinstance(reference, dict) or not isinstance(reference.get('pages'), list):
        return ['« pages » (liste) est obligatoire.']
    ids = set()
    for i, page in enumerate(reference['pages']):
        if not (page.get('version_id') or page.get('url')):
            problemes.append(f'pages[{i}] : version_id ou url requis.')
        for j, u in enumerate(page.get('unites', [])):
            chemin = f'pages[{i}].unites[{j}]'
            if not u.get('id'):
                problemes.append(chemin + ' : id manquant.')
            ids.add(u.get('id'))
            if not (u.get('id') or u.get('span')):
                problemes.append(chemin + ' : span manquant.')
            if u.get('span') is not None and not _span_valide(u['span']):
                problemes.append(chemin + ' : span invalide.')
            if u.get('classe') and u['classe'] not in CLASSES:
                problemes.append(f"{chemin} : classe inconnue ({u['classe']}).")
            if u.get('type') and u['type'] not in TYPES:
                problemes.append(f"{chemin} : type inconnu ({u['type']}).")
            for k, f in enumerate(u.get('faits', [])):
                if not _span_valide(f.get('span')):
                    problemes.append(f'{chemin}.faits[{k}] : span manquant ou invalide (un fait sans span est refusé).')
    for i, paire in enumerate(reference.get('paires', [])):
        if paire.get('relation') not in RELATIONS:
            problemes.append(f'paires[{i}] : relation inconnue.')
        for cle in ('a', 'b'):
            if paire.get(cle) not in ids:
                problemes.append(f'paires[{i}].{cle} : unité inconnue.')
    for i, groupe in enumerate(reference.get('groupes', [])):
        if not groupe.get('page_parente'):
            problemes.append(f'groupes[{i}] : page_parente manquante.')
    return problemes


def _span_valide(span):
    return isinstance(span, (list, tuple)) and len(span) == 2 and all(isinstance(x, int) for x in span) and 0 <= span[0] < span[1]


def charger(reference_json):
    """Charge et memorise le jeu de reference (dict, texte JSON ou chemin de fichier) ; renvoie le dict.

    Leve ValueError si le format est invalide.
    """
    if isinstance(reference_json, dict):
        reference = reference_json
    else:
        texte = str(reference_json)
        if not texte.lstrip().startswith('{'):
            with open(texte, encoding='utf-8') as fichier:
                texte = fichier.read()
        reference = json.loads(texte)
    problemes = valider(reference)
    if problemes:
        raise ValueError('Jeu de référence invalide : ' + ' ; '.join(problemes[:8]))
    noyau.definir_reglage(CLE_REFERENCE, json.dumps(reference, ensure_ascii=False))
    return reference


def reference_courante():
    """Jeu de reference memorise, ou None."""
    texte = noyau.reglage(CLE_REFERENCE)
    return json.loads(texte) if texte else None


# ---------------------------------------------------------------------------
# Gabarit d'annotation
# ---------------------------------------------------------------------------

def gabarit(version_ids):
    """Pre-remplit un fichier d'annotation depuis la base pour ``version_ids`` (a relire a la main).

    Reprend unites (span, classe, type), faits (span), groupes (page parente deduite de la paternite) et
    paires (relations M3). Chaque element porte ``relu: false`` : c'est la sortie des moteurs, pas une verite.
    """
    S = noyau.srv()
    pages, ids_unites = [], set()
    for vid in version_ids:
        v = S.query('SELECT v.id, v.text, v.page_type, p.url FROM versions v JOIN pages p ON p.id=v.page_id WHERE v.id=?', (vid,), True)
        if not v:
            continue
        unites = []
        for u in S.query('SELECT u.id, u.quote, u.text, u.span_start, u.span_end, u.obs_class, ct.type FROM units u '
                         'LEFT JOIN claim_types ct ON ct.unit_id=u.id WHERE u.version_id=? AND u.span_start IS NOT NULL ORDER BY u.span_start', (vid,)):
            faits = [{'span': [f['span_start'], f['span_end']], 'sujet': f['subject'], 'predicat': f['predicate'], 'objet': f['object'], 'relu': False}
                     for f in S.query('SELECT subject, predicate, object, span_start, span_end FROM facts WHERE unit_id=? AND span_start IS NOT NULL '
                                      'AND span_end>span_start ORDER BY span_start', (u['id'],))]
            ids_unites.add(u['id'])
            unites.append({'id': u['id'], 'span': [u['span_start'], u['span_end']], 'extrait': (u['quote'] or u['text'] or '')[:120],
                           'classe': u['obs_class'] if u['obs_class'] in CLASSES else '', 'type': u['type'] if u['type'] in TYPES else '',
                           'faits': faits, 'relu': False})
        pages.append({'version_id': vid, 'url': v['url'], 'type_page': v['page_type'] or '', 'texte': v['text'], 'unites': unites})
    groupes, paires = [], []
    if ids_unites:
        marques = ','.join('?' for _ in ids_unites)
        membres = {}
        for g in S.query(f'SELECT m.group_id, m.unit_id FROM unit_group_members m WHERE m.unit_id IN ({marques})', tuple(ids_unites)):
            membres.setdefault(g['group_id'], []).append(g['unit_id'])
        for groupe, liste in membres.items():
            if len(liste) < 2:
                continue
            parent = S.query('SELECT v.page_id, v.id vid FROM paternity pa JOIN units u ON u.id=pa.parent_unit_id JOIN versions v ON v.id=u.version_id '
                             'WHERE pa.group_id=?', (groupe,), True)
            groupes.append({'id': groupe, 'unites': sorted(liste), 'page_parente': parent['vid'] if parent else '', 'relu': False})
        carte = {'equivalence': 'equivalence', 'precision': 'precision', 'conflit': 'contradiction'}
        for r in S.query(f'SELECT a, b, relation FROM unit_relations WHERE a IN ({marques}) AND b IN ({marques})', tuple(ids_unites) * 2):
            if r['relation'] in carte:
                paires.append({'a': r['a'], 'b': r['b'], 'relation': carte[r['relation']], 'relu': False})
    return {'version': 1, 'nom': 'gabarit à relire', 'pages': pages, 'paires': paires, 'groupes': groupes, 'grappes': []}


# ---------------------------------------------------------------------------
# Notes humaines (a saisir)
# ---------------------------------------------------------------------------

def saisir(mesure, valeur, etiquette=None):
    """Enregistre une note humaine pour ``m4.note_humaine`` (1 a 5) ou ``m5.accord`` (vrai/faux, etiquette
    ``confirmee`` ou ``corrigee``). Renvoie le nombre de notes enregistrees pour cette mesure."""
    if mesure not in A_SAISIR:
        raise ValueError('Mesure inconnue : ' + str(mesure) + ' (attendu : ' + ', '.join(A_SAISIR) + ')')
    if mesure == 'm4.note_humaine' and not 1 <= float(valeur) <= 5:
        raise ValueError('La note doit être comprise entre 1 et 5.')
    liste = _saisies(mesure)
    liste.append({'valeur': float(valeur) if mesure == 'm4.note_humaine' else bool(valeur), 'etiquette': etiquette})
    noyau.definir_reglage(CLE_SAISIE + mesure, json.dumps(liste))
    return len(liste)


def _saisies(mesure):
    texte = noyau.reglage(CLE_SAISIE + mesure)
    return json.loads(texte) if texte else []


# ---------------------------------------------------------------------------
# Appariement
# ---------------------------------------------------------------------------

def _iou(a, b):
    debut, fin = max(a[0], b[0]), min(a[1], b[1])
    if fin <= debut:
        return 0.0
    return (fin - debut) / (max(a[1], b[1]) - min(a[0], b[0]))


def _resoudre_version(page):
    S = noyau.srv()
    if page.get('version_id') and S.query('SELECT 1 x FROM versions WHERE id=?', (page['version_id'],), True):
        return page['version_id']
    if page.get('url'):
        ligne = S.query('SELECT v.id FROM versions v JOIN pages p ON p.id=v.page_id WHERE p.url=? ORDER BY v.created DESC, v.rowid DESC LIMIT 1',
                        (page['url'],), True)
        return ligne['id'] if ligne else None
    return None


def _prf(vrais, predits, attendus):
    precision = vrais / predits if predits else None
    rappel = vrais / attendus if attendus else None
    f1 = 2 * precision * rappel / (precision + rappel) if precision and rappel else (0.0 if precision is not None and rappel is not None else None)
    return precision, rappel, f1


def _f1_par_classe(couples):
    """{classe: (f1, effectif_attendu)} depuis des couples (attendu, predit)."""
    resultat = {}
    for classe in {a for a, _ in couples} | {p for _, p in couples}:
        vrais = sum(1 for a, p in couples if a == classe and p == classe)
        predits = sum(1 for _, p in couples if p == classe)
        attendus = sum(1 for a, _ in couples if a == classe)
        resultat[classe] = (_prf(vrais, predits, attendus)[2] or 0.0, attendus)
    return resultat


def _appariement(reference):
    """Contexte d'appariement : unites annotees -> unites de la base, faits, pages."""
    S = noyau.srv()
    contexte = {'unites': {}, 'faits_gold': [], 'versions': [], 'page_de_version': {}, 'alias_pages': {}, 'gold_non_trouvees': 0}
    for page in reference['pages']:
        vid = _resoudre_version(page)
        if vid is None:
            continue
        contexte['versions'].append(vid)
        pid = S.query('SELECT page_id FROM versions WHERE id=?', (vid,), True)['page_id']
        for cle in (page.get('version_id'), page.get('url'), vid):
            if cle:
                contexte['alias_pages'][cle] = pid
        base = S.query('SELECT id, span_start, span_end FROM units WHERE version_id=? AND span_start IS NOT NULL', (vid,))
        par_id = {u['id']: u for u in base}
        for gold in page.get('unites', []):
            trouve = par_id.get(gold['id'])
            if trouve is None and gold.get('span'):
                meilleur = max(base, key=lambda u: _iou(gold['span'], (u['span_start'], u['span_end'])), default=None)
                if meilleur and _iou(gold['span'], (meilleur['span_start'], meilleur['span_end'])) >= RECOUVREMENT_MIN:
                    trouve = meilleur
            if trouve is None:
                contexte['gold_non_trouvees'] += 1
            contexte['unites'][gold['id']] = {'gold': gold, 'db': trouve['id'] if trouve else None, 'version_id': vid}
            for f in gold.get('faits', []):
                contexte['faits_gold'].append((vid, tuple(f['span'])))
    return contexte


# ---------------------------------------------------------------------------
# Mesures
# ---------------------------------------------------------------------------

def _ligne(methodologie, mesure, valeur, seuil=None, sens='min', note=''):
    ok = None
    if valeur is not None and seuil is not None:
        ok = valeur >= seuil if sens == 'min' else valeur <= seuil
    return {'methodologie': methodologie, 'mesure': mesure, 'valeur': None if valeur is None else round(valeur, 4), 'seuil': seuil, 'ok': ok,
            'sens': sens, 'a_saisir': False, 'note': note}


def mesurer(reference=None):
    """Calcule les mesures du tableau « Mesures et seuils de depart » depuis la base.

    Renvoie une liste de dicts ``methodologie``, ``mesure``, ``valeur``, ``seuil``, ``ok`` (+ ``sens``,
    ``a_saisir``, ``note``). ``valeur`` est None quand la mesure n'est pas calculable (pas de reference,
    pas de donnees). Les mesures humaines (M4, M5) valent la moyenne des notes saisies ; sans note elles
    sont listees avec ``a_saisir`` vrai. Les couts n'ont pas de plafond (suivi).
    """
    S = noyau.srv()
    reference = reference or reference_courante()
    lignes = []
    if reference:
        lignes.extend(_mesures_reference(S, reference))
    else:
        lignes.append(dict(_ligne('Jeu de référence', 'Aucun jeu de référence chargé (evaluation.charger)', None), a_saisir=False))
    lignes.extend(_mesures_humaines())
    lignes.extend(_mesures_m6(S))
    lignes.extend(_mesures_couts(S))
    return lignes


def _mesures_reference(S, reference):
    ctx = _appariement(reference)
    lignes = []
    versions = ctx['versions']
    if not versions:
        return [_ligne('Jeu de référence', 'Aucune page du jeu de référence trouvée dans la base', None)]
    marques = ','.join('?' for _ in versions)
    # M1 faits : precision et rappel
    faits = S.query(f"SELECT f.id, f.span_start, f.span_end, f.nli_score, u.version_id FROM facts f JOIN units u ON u.id=f.unit_id "
                    f"WHERE u.version_id IN ({marques}) AND COALESCE(f.status,'') NOT IN ('rejete')", tuple(versions))
    faits = [f for f in faits if f['span_start'] is not None and f['span_end'] is not None]
    gold = ctx['faits_gold']
    predits_ok = sum(1 for f in faits if any(v == f['version_id'] and _iou((f['span_start'], f['span_end']), s) >= RECOUVREMENT_MIN for v, s in gold))
    trouves = sum(1 for v, s in gold if any(f['version_id'] == v and _iou((f['span_start'], f['span_end']), s) >= RECOUVREMENT_MIN for f in faits))
    lignes.append(_ligne('M1 unités et faits', 'Précision des faits (appariés aux faits annotés)', predits_ok / len(faits) if faits else None,
                         _seuil('evaluation.m1.precision_faits', 0.98)))
    seuil_nli = _seuil('m1.faits.nli_implication', 0.8)
    avec_nli = [f for f in faits if f['nli_score'] is not None]
    lignes.append(_ligne('M1 unités et faits', 'Précision des faits impliqués par le texte (NLI stocké, complément)',
                         sum(1 for f in avec_nli if f['nli_score'] >= seuil_nli) / len(avec_nli) if avec_nli else None,
                         _seuil('evaluation.m1.precision_faits', 0.98)))
    lignes.append(_ligne('M1 unités et faits', 'Rappel par rapport aux faits annotés', trouves / len(gold) if gold else None,
                         _seuil('evaluation.m1.rappel_faits', 0.80)))
    # M1 classes : F1 macro
    couples = []
    for entree in ctx['unites'].values():
        if entree['gold'].get('classe') and entree['db']:
            ligne = S.query('SELECT obs_class FROM units WHERE id=?', (entree['db'],), True)
            couples.append((entree['gold']['classe'], ligne['obs_class'] or 'inconnu'))
    f1 = _f1_par_classe(couples)
    lignes.append(_ligne('M1 filtrage', 'F1 macro sur les classes d\'obsolescence', sum(v[0] for v in f1.values()) / len(f1) if f1 else None,
                         _seuil('evaluation.m1.f1_classes', 0.85)))
    # M1 deduplication : paires equivalentes
    en_base = {e['db']: k for k, e in ctx['unites'].items() if e['db']}
    gold_eq = {frozenset((p['a'], p['b'])) for p in reference.get('paires', []) if p['relation'] == 'equivalence'}
    predit_eq = set()
    if en_base:
        m2 = ','.join('?' for _ in en_base)
        groupes = {}
        for m in S.query(f'SELECT group_id, unit_id FROM unit_group_members WHERE unit_id IN ({m2})', tuple(en_base)):
            groupes.setdefault(m['group_id'], []).append(en_base[m['unit_id']])
        for liste in groupes.values():
            predit_eq.update(frozenset((a, b)) for i, a in enumerate(liste) for b in liste[i + 1:])
        for r in S.query(f"SELECT a, b FROM unit_relations WHERE relation='equivalence' AND a IN ({m2}) AND b IN ({m2})", tuple(en_base) * 2):
            predit_eq.add(frozenset((en_base[r['a']], en_base[r['b']])))
    vrais = len(gold_eq & predit_eq)
    precision, rappel, _ = _prf(vrais, len(predit_eq), len(gold_eq))
    lignes.append(_ligne('M1 déduplication', 'Précision sur les paires équivalentes', precision, _seuil('evaluation.m1.dedup_precision', 0.90)))
    lignes.append(_ligne('M1 déduplication', 'Rappel sur les paires équivalentes', rappel, _seuil('evaluation.m1.dedup_rappel', 0.80)))
    # M2 typologie
    couples = []
    for entree in ctx['unites'].values():
        if entree['gold'].get('type') and entree['db']:
            ligne = S.query('SELECT type FROM claim_types WHERE unit_id=?', (entree['db'],), True)
            couples.append((entree['gold']['type'], (ligne or {}).get('type') or 'inconnu'))
    f1 = _f1_par_classe(couples)
    for type_affirmation in sorted(f1):
        if type_affirmation == 'inconnu' or f1[type_affirmation][1] == 0:
            continue
        etiquette = ' (prioritaire)' if type_affirmation in PRIORITAIRES else ''
        lignes.append(_ligne('M2 typologie', f'F1 du type {type_affirmation}{etiquette}', f1[type_affirmation][0], _seuil('evaluation.m2.f1_types', 0.80)))
    if not couples:
        lignes.append(_ligne('M2 typologie', 'F1 par type', None, _seuil('evaluation.m2.f1_types', 0.80)))
    # M3 paternite
    bons, total = 0, 0
    for groupe in reference.get('groupes', []):
        db_units = [ctx['unites'][u]['db'] for u in groupe.get('unites', []) if u in ctx['unites'] and ctx['unites'][u]['db']]
        if not db_units:
            continue
        total += 1
        m2 = ','.join('?' for _ in db_units)
        parent = S.query(f'SELECT v.page_id FROM unit_group_members m JOIN paternity pa ON pa.group_id=m.group_id JOIN units u ON u.id=pa.parent_unit_id '
                         f'JOIN versions v ON v.id=u.version_id WHERE m.unit_id IN ({m2}) LIMIT 1', tuple(db_units), True)
        attendu = ctx['alias_pages'].get(groupe['page_parente'])
        if attendu is None:
            ligne = S.query('SELECT v.page_id FROM versions v JOIN pages p ON p.id=v.page_id WHERE v.id=? OR p.url=? LIMIT 1',
                            (groupe['page_parente'], groupe['page_parente']), True)
            attendu = ligne['page_id'] if ligne else None
        if parent and attendu and parent['page_id'] == attendu:
            bons += 1
    lignes.append(_ligne('M3 paternité', 'Part des groupes avec la bonne page parente', bons / total if total else None, _seuil('evaluation.m3.paternite', 0.85)))
    # M3 faux conflits
    gold_paires = {frozenset((p['a'], p['b'])): p['relation'] for p in reference.get('paires', [])}
    signalees = []
    if en_base:
        m2 = ','.join('?' for _ in en_base)
        for r in S.query(f"SELECT a, b FROM unit_relations WHERE relation='conflit' AND a IN ({m2}) AND b IN ({m2})", tuple(en_base) * 2):
            signalees.append(frozenset((en_base[r['a']], en_base[r['b']])))
    faux = sum(1 for paire in signalees if gold_paires.get(paire) != 'contradiction')
    lignes.append(_ligne('M3 contradictions', 'Faux conflits parmi les contradictions signalées', faux / len(signalees) if signalees else None,
                         _seuil('evaluation.m3.faux_conflits_max', 0.10), 'max',
                         'Une contradiction signalée absente de l\'annotation compte comme fausse.'))
    return lignes


def _mesures_humaines():
    lignes = []
    notes = [s['valeur'] for s in _saisies('m4.note_humaine')]
    methodologie, libelle, seuil = A_SAISIR['m4.note_humaine']
    ligne = _ligne(methodologie, libelle, sum(notes) / len(notes) if notes else None, _seuil(seuil, 4.0))
    ligne.update(a_saisir=not notes, note=f'{len(notes)} note(s) saisie(s)' if notes else 'À saisir : evaluation.saisir("m4.note_humaine", note)')
    lignes.append(ligne)
    accords = [s for s in _saisies('m5.accord') if s.get('etiquette') in (None, 'confirmee', 'corrigee')]
    methodologie, libelle, seuil = A_SAISIR['m5.accord']
    ligne = _ligne(methodologie, libelle, sum(1 for a in accords if a['valeur']) / len(accords) if accords else None, _seuil(seuil, 0.90))
    ligne.update(a_saisir=not accords, note=f'{len(accords)} verdict(s) saisi(s)' if accords else 'À saisir : evaluation.saisir("m5.accord", vrai_ou_faux, "confirmee"|"corrigee")')
    lignes.append(ligne)
    return lignes


def _mesures_m6(S):
    from chaine import m6
    stats = m6.apprentissage()
    ligne = _ligne('M6 propositions', 'Part acceptée sans modification en revue', stats['taux_sans_modification'],
                   _seuil('evaluation.m6.acceptees_sans_modif', 0.70), note=f"{stats['total_decisions']} décision(s)")
    return [ligne]


def _mesures_couts(S):
    lignes = []
    pages = S.query('SELECT count(DISTINCT version_id) n FROM units', one=True)['n'] or 0
    for etape in S.query("SELECT step, SUM(pages) pages, SUM(local_seconds) secondes, SUM(tokens_in) tin, SUM(tokens_out) tout, SUM(ibou_units) ibou "
                         "FROM step_runs WHERE status='terminee' GROUP BY step ORDER BY step"):
        divise = etape['pages'] or pages
        if not divise:
            continue
        for cle, libelle in (('secondes', 'Temps local par page (s)'), ('tin', 'Tokens LLM en entrée par page'),
                             ('tout', 'Tokens LLM en sortie par page'), ('ibou', 'Unités Ibou par page')):
            ligne = _ligne('Coût', f"{libelle} · {etape['step']}", (etape[cle] or 0) / divise, None, note='Suivi, plafond à fixer')
            lignes.append(ligne)
    return lignes
