"""SimHash de Charikar, en Python pur (M5 etape 3 : quasi-copies).

Le document (M5 etape 3) ecarte « les quasi-copies du site audite, reperees par
SimHash, la methode deja en place » (celle de ContentKB). Empreinte de 64 bits
calculee sur des fenetres de mots (shingles) normalises : minuscules, sans
accents, ponctuation retiree. Deux textes sont des quasi-copies quand la
distance de Hamming de leurs empreintes est au plus ``m5.simhash.distance_max``.

Aucune dependance : ``hashlib`` (blake2b) seulement.
"""
import hashlib
import re
import unicodedata

BITS = 64
TAILLE_FENETRE = 3
_MASQUE = (1 << BITS) - 1
_MOT = re.compile(r'[a-z0-9]+')


def mots(texte):
    """Mots normalises (minuscules, sans accents, alphanumeriques)."""
    decompose = unicodedata.normalize('NFD', (texte or '').casefold())
    sans_accents = ''.join(c for c in decompose if not unicodedata.combining(c))
    return _MOT.findall(sans_accents)


def fenetres(texte, taille=TAILLE_FENETRE):
    """Fenetres de ``taille`` mots consecutifs (les mots seuls si le texte est plus court)."""
    liste = mots(texte)
    if len(liste) < taille:
        return list(liste)
    return [' '.join(liste[i:i + taille]) for i in range(len(liste) - taille + 1)]


def _hachage(element):
    """Hachage stable de 64 bits d'une fenetre."""
    condense = hashlib.blake2b(element.encode('utf-8'), digest_size=8).digest()
    return int.from_bytes(condense, 'big')


def empreinte(texte, taille=TAILLE_FENETRE):
    """Empreinte SimHash (entier de 64 bits) ; None si le texte n'a aucun mot."""
    elements = fenetres(texte, taille)
    if not elements:
        return None
    poids = {}
    for element in elements:
        poids[element] = poids.get(element, 0) + 1
    vecteur = [0] * BITS
    for element, compte in poids.items():
        valeur = _hachage(element)
        for bit in range(BITS):
            if valeur >> bit & 1:
                vecteur[bit] += compte
            else:
                vecteur[bit] -= compte
    resultat = 0
    for bit in range(BITS):
        if vecteur[bit] > 0:
            resultat |= 1 << bit
    return resultat & _MASQUE


def hexa(valeur):
    """Empreinte en texte hexadecimal de 16 caracteres (stockage ``versions.simhash``)."""
    if valeur is None:
        return None
    return format(int(valeur) & _MASQUE, '016x')


def depuis_hexa(texte):
    """Empreinte depuis son texte hexadecimal ; None si illisible."""
    try:
        return int(str(texte), 16) & _MASQUE
    except (TypeError, ValueError):
        return None


def _entier(valeur):
    """Accepte un entier ou un texte hexadecimal."""
    if valeur is None:
        return None
    if isinstance(valeur, int):
        return valeur & _MASQUE
    return depuis_hexa(valeur)


def distance(a, b):
    """Distance de Hamming entre deux empreintes (entiers ou textes hexa) ; None si l'une manque."""
    x = _entier(a)
    y = _entier(b)
    if x is None or y is None:
        return None
    return bin(x ^ y).count('1')


def proches(a, b, distance_max):
    """Vrai si les deux empreintes sont a ``distance_max`` bits ou moins."""
    ecart = distance(a, b)
    return ecart is not None and ecart <= distance_max


def regrouper(empreintes, distance_max):
    """Regroupe des empreintes proches (union-find).

    ``empreintes`` : dict ``identifiant -> empreinte``. Renvoie un dict
    ``identifiant -> identifiant du representant`` (le premier dans l'ordre
    d'insertion). Comparaison deux a deux : reserve aux petits ensembles
    (documents renvoyes pour une requete, une vingtaine au plus).
    """
    cles = list(empreintes)
    parent = {cle: cle for cle in cles}

    def racine(cle):
        while parent[cle] != cle:
            parent[cle] = parent[parent[cle]]
            cle = parent[cle]
        return cle

    for i, a in enumerate(cles):
        for b in cles[i + 1:]:
            if proches(empreintes[a], empreintes[b], distance_max):
                ra = racine(a)
                rb = racine(b)
                if ra != rb:
                    premier, second = (ra, rb) if cles.index(ra) < cles.index(rb) else (rb, ra)
                    parent[second] = premier
    return {cle: racine(cle) for cle in cles}
