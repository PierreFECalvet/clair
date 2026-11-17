"""Liste fermee des predicats (M1 etapes 5 et 6, M4, M5).

Le document (M1 etape 5) : « La liste fermee est la cle : 30 a 60 predicats
rendent la deduplication et le filtrage deterministes. Un predicat libre rend
tout le reste flou. » La liste vit dans ``chaine/predicats.json``, a cote de ce
module, pour que l'utilisateur puisse la modifier sans toucher au code (point ouvert
du document : qui la definit et a partir de quel corpus). Le fichier est relu
automatiquement quand sa date de modification change.

Chaque predicat :

- ``nom`` : identifiant ASCII sans accent (valeur stockee dans ``facts.predicate``) ;
- ``libelle`` : libelle accentue pour l'interface ;
- ``classe`` : ``immuable`` | ``etat_courant`` | ``annonce`` (table predicat -> classe de M1 etape 6) ;
- ``volatilite_jours`` : duree de validite typique (None pour un immuable) ;
- ``type_objet`` : type attendu de l'objet (entite, personne, organisation, lieu, date, montant,
  pourcentage, nombre, duree, periode, loi, texte) ;
- ``gabarit`` : famille de question pour M4 (``etat_courant`` | ``connaissance_generale`` | ``annonce``) ;
- ``critique`` : vrai pour date, montant, prix, regle, securite, sante, droit ;
- ``verbalisation`` : patron « {sujet} ... {objet} » utilise par le garde-fou NLI.
"""
import json
import os
import re
import threading
from pathlib import Path

CHEMIN = Path(__file__).resolve().with_name('predicats.json')
CLASSES = ('immuable', 'etat_courant', 'annonce')
GABARITS = ('etat_courant', 'connaissance_generale', 'annonce')
TYPES_OBJET = ('entite', 'personne', 'organisation', 'lieu', 'date', 'montant', 'pourcentage', 'nombre',
               'duree', 'periode', 'loi', 'texte', 'produit')
TAILLE_MIN = 30
TAILLE_MAX = 60
CHAMPS = ('nom', 'libelle', 'classe', 'volatilite_jours', 'type_objet', 'gabarit', 'critique', 'verbalisation')
_NOM_VALIDE = re.compile(r'^[a-z][a-z0-9_]*$')

_VERROU = threading.RLock()
_CACHE = {'chemin': None, 'mtime': None, 'liste': [], 'index': {}, 'version': ''}


class PredicatsInvalides(ValueError):
    """Le fichier de predicats ne respecte pas le format attendu."""


def chemin():
    """Chemin du fichier JSON (variable d'environnement ``FC_PREDICATS`` pour un autre fichier)."""
    autre = os.environ.get('FC_PREDICATS')
    if autre:
        return Path(autre)
    return CHEMIN


def valider(liste):
    """Controle une liste de predicats ; renvoie la liste des problemes (vide si conforme).

    Hors de la fourchette 30-60, un avertissement est renvoye avec le prefixe
    « avertissement : » ; il n'empeche pas le chargement.
    """
    problemes = []
    if not isinstance(liste, list):
        return ['la cle « predicats » doit etre une liste']
    vus = set()
    for index, predicat in enumerate(liste):
        repere = f'predicat {index}'
        if not isinstance(predicat, dict):
            problemes.append(repere + ' : objet attendu')
            continue
        nom = predicat.get('nom')
        repere = f'predicat {index} ({nom})'
        for champ in CHAMPS:
            if champ not in predicat:
                problemes.append(repere + ' : champ manquant ' + champ)
        if not isinstance(nom, str) or not _NOM_VALIDE.match(nom):
            problemes.append(repere + ' : nom ASCII en minuscules attendu')
        elif nom in vus:
            problemes.append(repere + ' : nom en double')
        vus.add(nom)
        if predicat.get('classe') not in CLASSES:
            problemes.append(repere + ' : classe hors liste ' + str(CLASSES))
        if predicat.get('gabarit') not in GABARITS:
            problemes.append(repere + ' : gabarit hors liste ' + str(GABARITS))
        if predicat.get('type_objet') not in TYPES_OBJET:
            problemes.append(repere + ' : type_objet hors liste ' + str(TYPES_OBJET))
        if not isinstance(predicat.get('critique'), bool):
            problemes.append(repere + ' : critique doit etre true ou false')
        volatilite = predicat.get('volatilite_jours')
        if volatilite is not None and (isinstance(volatilite, bool) or not isinstance(volatilite, int) or volatilite <= 0):
            problemes.append(repere + ' : volatilite_jours entier positif ou null attendu')
        verbalisation = predicat.get('verbalisation')
        if not isinstance(verbalisation, str) or '{sujet}' not in verbalisation or '{objet}' not in verbalisation:
            problemes.append(repere + ' : verbalisation avec {sujet} et {objet} attendue')
    if not TAILLE_MIN <= len(liste) <= TAILLE_MAX:
        problemes.append(f'avertissement : {len(liste)} predicats, le document en prevoit {TAILLE_MIN} a {TAILLE_MAX}')
    return problemes


def _charger():
    """Lit le fichier si besoin (cache invalide par la date de modification)."""
    fichier = chemin()
    mtime = fichier.stat().st_mtime
    with _VERROU:
        if _CACHE['chemin'] == str(fichier) and _CACHE['mtime'] == mtime:
            return _CACHE
        donnees = json.loads(fichier.read_text(encoding='utf-8'))
        liste = donnees.get('predicats') if isinstance(donnees, dict) else donnees
        bloquants = [p for p in valider(liste) if not p.startswith('avertissement')]
        if bloquants:
            raise PredicatsInvalides(str(fichier) + ' : ' + ' ; '.join(bloquants[:10]))
        _CACHE['chemin'] = str(fichier)
        _CACHE['mtime'] = mtime
        _CACHE['liste'] = [dict(p) for p in liste]
        _CACHE['index'] = {p['nom']: p for p in _CACHE['liste']}
        _CACHE['version'] = donnees.get('version', '') if isinstance(donnees, dict) else ''
        return _CACHE


def recharger():
    """Force la relecture du fichier ; renvoie le nombre de predicats."""
    with _VERROU:
        _CACHE['chemin'] = None
        _CACHE['mtime'] = None
    return len(_charger()['liste'])


def remplacer(liste, version=None):
    """Remplace la liste fermee (fichier ``chemin()``) apres validation ; renvoie ``{nombre, version, avertissements}``.

    Les problemes bloquants levent ``PredicatsInvalides`` (sous-classe de
    ValueError) sans rien ecrire. Ecriture atomique (fichier temporaire puis
    remplacement) ; la liste precedente est conservee dans ``<fichier>.precedent``.
    """
    problemes = valider(liste)
    bloquants = [p for p in problemes if not p.startswith('avertissement')]
    if bloquants:
        raise PredicatsInvalides('Liste de prédicats refusée : ' + ' ; '.join(bloquants[:10]))
    fichier = chemin()
    with _VERROU:
        ancien = {}
        if fichier.exists():
            texte_ancien = fichier.read_text(encoding='utf-8')
            fichier.with_name(fichier.name + '.precedent').write_text(texte_ancien, encoding='utf-8')
            try:
                ancien = json.loads(texte_ancien)
            except ValueError:
                ancien = {}
        donnees = {
            'version': version or _version_suivante(ancien.get('version', '') if isinstance(ancien, dict) else ''),
            'note': (ancien.get('note') if isinstance(ancien, dict) else None) or 'Liste fermee des predicats.',
            'predicats': [{champ: p[champ] for champ in CHAMPS} for p in liste],
        }
        temporaire = fichier.with_name(fichier.name + '.tmp')
        lignes = ',\n'.join('  ' + json.dumps(p, ensure_ascii=False) for p in donnees['predicats'])
        entete = json.dumps({'version': donnees['version'], 'note': donnees['note']}, ensure_ascii=False, indent=1)[:-2]
        temporaire.write_text(entete + ',\n "predicats": [\n' + lignes + '\n ]\n}\n', encoding='utf-8')
        os.replace(temporaire, fichier)
        recharger()
    return {'nombre': len(liste), 'version': donnees['version'],
            'avertissements': [p for p in problemes if p.startswith('avertissement')]}


def _version_suivante(version):
    """« predicats-v1 » -> « predicats-v2 » (suffixe numerique incremente)."""
    trouve = re.match(r'^(.*?)(\d+)$', version or '')
    if not trouve:
        return (version or 'predicats') + '-v2'
    return trouve.group(1) + str(int(trouve.group(2)) + 1)


def tous():
    """Liste complete des predicats (copies : les modifier ne change pas la liste)."""
    return [dict(p) for p in _charger()['liste']]


def noms():
    """Noms des predicats, dans l'ordre du fichier."""
    return [p['nom'] for p in _charger()['liste']]


def version():
    """Version declaree dans le fichier (tracee dans ``step_runs.prompt``)."""
    return _charger()['version']


def get(nom):
    """Predicat ``nom`` (copie du dict), ou None s'il n'est pas dans la liste fermee."""
    predicat = _charger()['index'].get(nom)
    if predicat is None:
        return None
    return dict(predicat)


def existe(nom):
    """Vrai si ``nom`` est dans la liste fermee."""
    return nom in _charger()['index']


def classe(nom):
    """Classe d'obsolescence du predicat (``immuable|etat_courant|annonce``), ou None si inconnu."""
    predicat = _charger()['index'].get(nom)
    if predicat is None:
        return None
    return predicat['classe']


def critique(nom):
    """Vrai si le predicat est critique (date, montant, prix, regle, securite, sante, droit)."""
    predicat = _charger()['index'].get(nom)
    return bool(predicat and predicat['critique'])


def verbaliser(nom, sujet, objet):
    """Phrase « {sujet} ... {objet} » du predicat ; KeyError si le predicat est inconnu."""
    predicat = _charger()['index'].get(nom)
    if predicat is None:
        raise KeyError('Predicat hors liste fermee : ' + str(nom))
    phrase = predicat['verbalisation'].replace('{sujet}', str(sujet or '').strip())
    phrase = phrase.replace('{objet}', str(objet or '').strip())
    phrase = re.sub(r'\s+', ' ', phrase).strip()
    if phrase:
        phrase = phrase[0].upper() + phrase[1:]
    return phrase


def par_classe(classe_voulue):
    """Noms des predicats d'une classe."""
    return [p['nom'] for p in _charger()['liste'] if p['classe'] == classe_voulue]


def pour_prompt():
    """Liste compacte ``nom : libelle (type_objet)`` a donner au LLM contraint."""
    return [p['nom'] + ' : ' + p['libelle'] + ' (' + p['type_objet'] + ')' for p in _charger()['liste']]
