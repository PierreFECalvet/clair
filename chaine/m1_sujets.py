"""M1 etapes 7 et 8 : embeddings, sujets (fuzzy c-means) et deduplication des unites.

Etape 7
    ``encoder`` calcule les embeddings locaux (titre + unite, titre + debut de page),
    par lots, de facon reprenable. ``sujets`` reduit les pages a 10-50 dimensions
    (UMAP si installe, sinon PCA numpy, sinon PCA pur Python pour de petits
    volumes), lance un fuzzy c-means a exposant de flou bas, choisit le nombre de
    sujets par indices de validite (Xie-Beni, coefficient de partition) et stocke
    les appartenances. Les unites heritent des sujets de leur page.

Etape 8
    ``dedoublonner`` regroupe les doublons sans jamais en supprimer :
    exacts (triplets normalises), puis candidats semantiques a l'interieur d'un
    sujet, qualifies par NLI dans les deux sens, le LLM local ne tranchant que la
    zone grise. Les groupes migres sont etendus ou fusionnes (champ
    ``unit_groups.merged_into``), jamais supprimes.

Tout import tiers (numpy, umap, hdbscan, skfuzzy, faiss) est paresseux avec repli.
Contrat : docs/CONTRAT.md ; specification : docs/METHODOLOGIES.txt lignes 168-184.
"""
import hashlib
import importlib
import json
import math
import random
import re
import time
import unicodedata
from collections import Counter, defaultdict

from chaine import moteurs, noyau, schema, seuils


RELATIONS_PAIRE = ('equivalence', 'precision', 'conflit', 'sans_rapport')

TACHE_DEDUP = (
    "Question fermee. Pour chaque paire (id, a, b) d'affirmations extraites de pages d'un meme site, dis laquelle "
    "des relations suivantes s'applique, sans connaissance exterieure : equivalence (a et b disent la meme chose), "
    "a_precise_b (a contient b et ajoute un detail), b_precise_a (b contient a et ajoute un detail), "
    "conflit (a et b ne peuvent pas etre vraies ensemble a la meme date), sans_rapport (aucune des relations "
    "precedentes). Les scores NLI fournis sont indicatifs. Reponds pour chaque id, sans en omettre."
)

noyau.schema('chaine:dedup_paire', noyau.objet({
    'resultats': noyau.tableau(noyau.objet({
        'id': {'type': 'integer'},
        'relation': noyau.enum(['equivalence', 'a_precise_b', 'b_precise_a', 'conflit', 'sans_rapport']),
    })),
}))

MOTS_VIDES = frozenset(
    'alors apres aussi autre autres avant avec avoir bien cela celle celles celui cette ceux chaque comme dans '
    'depuis donc dont elle elles encore entre etre faire fait font leur leurs mais meme mieux moins notre nous '
    'nouveau nouvelle notamment plus pour pourquoi quand quel quelle quels quelles sans sera seront ses sont '
    'sous sur tous tout toute toutes tres votre vous ainsi cest etait etaient etant ete peut peuvent comment '
    'afin lors vers selon deja tant fois'.split()
)


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def _np():
    """numpy si disponible, sinon None (les tests peuvent le neutraliser)."""
    try:
        import numpy
        return numpy
    except ImportError:
        return None


def _seuil(nom, defaut=None):
    """Seuil de ``chaine.seuils`` (tous les seuils du module y sont declares ; ``defaut`` est ignore)."""
    return seuils.get(nom)


def _sans_accents(texte):
    """Texte en ASCII sans accent (valeurs stockees)."""
    decompose = unicodedata.normalize('NFKD', str(texte or ''))
    return ''.join(c for c in decompose if not unicodedata.combining(c)).encode('ascii', 'ignore').decode('ascii')


def _normaliser_texte(texte):
    """Forme de comparaison : minuscules, sans accent ni ponctuation, espaces reduits."""
    brut = _sans_accents(texte).lower()
    return ' '.join(re.sub(r'[^a-z0-9]+', ' ', brut).split())


def _vers_json(vecteur):
    """Serialisation compacte d'un vecteur (5 decimales)."""
    return json.dumps([round(float(x), 5) for x in vecteur], separators=(',', ':'))


def _lire_vecteur(texte):
    """Vecteur stocke -> liste de floats, ou None si illisible."""
    if not texte:
        return None
    try:
        valeur = json.loads(texte)
    except ValueError:
        return None
    if not isinstance(valeur, list) or not valeur:
        return None
    try:
        return [float(x) for x in valeur]
    except (TypeError, ValueError):
        return None


def _erreur(message):
    """Leve l'erreur operationnelle du serveur."""
    raise noyau.srv().OperationalError(message)


def _job(**champs):
    """Met a jour l'etat du traitement du serveur."""
    noyau.srv().JOB.update(**champs)


def _arret_demande():
    """Vrai si l'arret a ete demande (ne pas masquer cette erreur)."""
    arret = getattr(noyau.srv(), 'STOP', None)
    return bool(arret is not None and arret.is_set())


# ---------------------------------------------------------------------------
# Etape 7a : embeddings
# ---------------------------------------------------------------------------

def texte_unite(titre, texte):
    """Texte encode pour une unite : titre de la page puis unite."""
    titre = (titre or '').strip()
    return (titre + '\n' + (texte or '').strip()).strip()


def texte_page(titre, chapo, texte):
    """Texte encode pour une page : titre, chapo, puis debut du texte."""
    limite = int(_seuil('m1.encodage.page_caracteres'))
    morceaux = [(titre or '').strip(), (chapo or '').strip(), (texte or '').strip()]
    return '\n'.join(m for m in morceaux if m)[:limite]


def encoder(cibles='unites', forcer=False):
    """Calcule et stocke les embeddings (``units.vector`` ou ``versions.vector``).

    ``cibles`` : ``'unites'`` ou ``'pages'`` (versions portant au moins une unite).
    Par lots de ``m1.encodage.taille_lot`` ; une ecriture par lot, donc reprenable :
    sans ``forcer``, seules les lignes sans vecteur sont encodees.
    Renvoie un dict de compteurs.
    """
    if cibles not in ('unites', 'pages'):
        raise ValueError("cibles doit valoir 'unites' ou 'pages'.")
    serveur = noyau.srv()
    condition = '' if forcer else " AND (x.vector IS NULL OR x.vector='')"
    if cibles == 'unites':
        lignes = serveur.query(
            'SELECT x.id, x.text, p.title FROM units x LEFT JOIN versions v ON v.id=x.version_id '
            'LEFT JOIN pages p ON p.id=v.page_id WHERE COALESCE(x.excluded,0)=0 AND x.text IS NOT NULL'
            + condition + ' ORDER BY x.id'
        )
        textes = [texte_unite(ligne['title'], ligne['text']) for ligne in lignes]
        table = 'units'
    else:
        lignes = serveur.query(
            'SELECT x.id, x.text, x.lede, p.title FROM versions x LEFT JOIN pages p ON p.id=x.page_id '
            'WHERE EXISTS(SELECT 1 FROM units u WHERE u.version_id=x.id AND COALESCE(u.excluded,0)=0)'
            + condition + ' ORDER BY x.id'
        )
        textes = [texte_page(ligne['title'], ligne['lede'], ligne['text']) for ligne in lignes]
        table = 'versions'
        limite = int(_seuil('m1.encodage.page_caracteres'))
        tronquees = sum(1 for ligne in lignes if len('\n'.join(m for m in ((ligne['title'] or '').strip(),
                        (ligne['lede'] or '').strip(), (ligne['text'] or '').strip()) if m)) > limite)
    deja = 0
    if not forcer:
        if cibles == 'unites':
            total_cible = serveur.query("SELECT count(*) n FROM units WHERE COALESCE(excluded,0)=0 AND text IS NOT NULL", (), True)['n']
        else:
            total_cible = serveur.query(
                'SELECT count(*) n FROM versions x WHERE EXISTS(SELECT 1 FROM units u WHERE u.version_id=x.id '
                'AND COALESCE(u.excluded,0)=0)', (), True)['n']
        deja = max(0, total_cible - len(lignes))
    resultat = {'cible': cibles, 'a_encoder': len(lignes), 'encodes': 0, 'deja_encodes': deja, 'lots': 0, 'moteur': None}
    if cibles == 'pages':
        # Les pages sont encodees sur leur debut (m1.encodage.page_caracteres) : la troncature est comptee, pas silencieuse.
        resultat['tronquees'] = tronquees
    if not lignes:
        return resultat
    taille = max(1, int(_seuil('m1.encodage.taille_lot')))
    etape_nom = 'm1.encodage.' + cibles
    _job(stage=etape_nom, done=0, total=len(lignes), message='Embeddings des ' + cibles + ' : ' + str(len(lignes)) + ' a encoder')
    with noyau.etape(etape_nom, moteurs={'embeddings': 'auto'}, prompt='', seuils=['m1.reduction.dimensions'], items=len(lignes)) as run:
        for debut in range(0, len(lignes), taille):
            serveur.checkpoint()
            lot = lignes[debut:debut + taille]
            vecteurs, moteur = moteurs.embeddings(textes[debut:debut + taille])
            if not vecteurs:
                _erreur("Aucun moteur d'embeddings disponible : installez sentence-transformers ou activez l'API d'embeddings.")
            if len(vecteurs) != len(lot):
                _erreur("Le moteur d'embeddings a renvoye %d vecteurs pour %d textes." % (len(vecteurs), len(lot)))
            run.moteur('embeddings', moteur)
            with noyau.connexion() as base:
                base.executemany(
                    'UPDATE ' + table + ' SET vector=? WHERE id=?',
                    [(_vers_json(v), ligne['id']) for v, ligne in zip(vecteurs, lot)],
                )
            resultat['encodes'] += len(lot)
            resultat['lots'] += 1
            resultat['moteur'] = moteur
            _job(done=resultat['encodes'], message='Embeddings des ' + cibles + ' : %d/%d' % (resultat['encodes'], len(lignes)))
        run.note = 'encodes=%d lots=%d' % (resultat['encodes'], resultat['lots'])
        if cibles == 'pages':
            run.note += ' tronquees=%d (debut de page sur %d caracteres)' % (resultat['tronquees'], limite)
    return resultat


def charger_vecteurs(table, ids=None):
    """Charge les vecteurs stockes d'une table (``units`` ou ``versions``).

    Renvoie ``(ids, matrice)`` : matrice numpy float32 normalisee (ou liste de
    listes sans numpy). Seule la dimension majoritaire est conservee.
    """
    if table not in ('units', 'versions'):
        raise ValueError('table inconnue : ' + table)
    serveur = noyau.srv()
    sql = 'SELECT id, vector FROM ' + table + " WHERE vector IS NOT NULL AND vector!=''"
    if table == 'units':
        sql += ' AND COALESCE(excluded,0)=0'
    lignes = serveur.query(sql + ' ORDER BY id')
    autorises = set(ids) if ids is not None else None
    lus = []
    for ligne in lignes:
        if autorises is not None and ligne['id'] not in autorises:
            continue
        vecteur = _lire_vecteur(ligne['vector'])
        if vecteur is not None:
            lus.append((ligne['id'], vecteur))
    if not lus:
        return [], _matrice([])
    dimension = Counter(len(v) for _, v in lus).most_common(1)[0][0]
    lus = [(i, v) for i, v in lus if len(v) == dimension]
    return [i for i, _ in lus], _normaliser([v for _, v in lus])


def _matrice(lignes):
    """Liste de listes -> ndarray float32 si numpy, sinon liste inchangee."""
    numpy = _np()
    if numpy is None:
        return lignes
    return numpy.asarray(lignes, dtype=numpy.float32) if lignes else numpy.zeros((0, 0), dtype=numpy.float32)


def _normaliser(lignes):
    """Normalise chaque vecteur a la norme 1."""
    numpy = _np()
    if numpy is not None:
        matrice = numpy.asarray(lignes, dtype=numpy.float32)
        normes = numpy.linalg.norm(matrice, axis=1, keepdims=True)
        normes[normes == 0] = 1.0
        return matrice / normes
    resultat = []
    for ligne in lignes:
        norme = math.sqrt(sum(x * x for x in ligne)) or 1.0
        resultat.append([x / norme for x in ligne])
    return resultat


# ---------------------------------------------------------------------------
# Etape 7b : reduction de dimension
# ---------------------------------------------------------------------------

def reduire(X, dimensions=None, methode='auto'):
    """Reduit ``X`` (n x d) a ``dimensions`` composantes (10 a 50 par defaut 20).

    ``methode`` : ``auto`` (UMAP si installe, sinon PCA), ``pca`` ou ``umap``.
    Renvoie ``(Z, infos)`` ; ``infos`` = methode utilisee, dimensions, variance expliquee (PCA).
    """
    dimensions = int(dimensions or _seuil('m1.reduction.dimensions'))
    numpy = _np()
    n = len(X)
    d = X.shape[1] if numpy is not None and hasattr(X, 'shape') else (len(X[0]) if n else 0)
    k = max(1, min(dimensions, n - 1, d))
    if methode in ('auto', 'umap') and numpy is not None and moteurs.paquet_present('umap') and n > k + 2:
        try:
            umap = importlib.import_module('umap')
            projecteur = umap.UMAP(n_components=k, n_neighbors=max(2, min(15, n - 1)), metric='cosine', random_state=0)
            Z = projecteur.fit_transform(numpy.asarray(X, dtype=numpy.float32))
            return Z, {'methode': 'umap', 'dimensions': k, 'variance': None}
        except Exception as erreur:
            if methode == 'umap':
                raise
            note = 'UMAP en echec (' + type(erreur).__name__ + ') : PCA'
            Z, infos = _pca(X, k)
            infos['note'] = note
            return Z, infos
    if methode == 'umap':
        _erreur('UMAP demande mais non installe.')
    return _pca(X, k)


def _pca(X, k):
    """PCA : numpy (SVD) ou repli pur Python pour de petits volumes."""
    numpy = _np()
    if numpy is not None:
        M = numpy.asarray(X, dtype=numpy.float64)
        centre = M - M.mean(axis=0)
        _, valeurs, composantes = numpy.linalg.svd(centre, full_matrices=False)
        k = min(k, len(valeurs))
        total = float((valeurs ** 2).sum()) or 1.0
        variance = float((valeurs[:k] ** 2).sum() / total)
        return centre @ composantes[:k].T, {'methode': 'pca', 'dimensions': k, 'variance': variance}
    limite = int(_seuil('m1.python.pages_max'))
    if len(X) > limite:
        _erreur('numpy absent : le repli pur Python est limite a %d pages (%d fournies).' % (limite, len(X)))
    return _pca_python(X, k)


def _pca_python(X, k):
    """PCA sans numpy : iteration de puissance avec deflation sur la matrice de Gram."""
    n = len(X)
    d = len(X[0])
    moyenne = [sum(ligne[j] for ligne in X) / n for j in range(d)]
    C = [[ligne[j] - moyenne[j] for j in range(d)] for ligne in X]
    G = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i, n):
            valeur = sum(a * b for a, b in zip(C[i], C[j]))
            G[i][j] = valeur
            G[j][i] = valeur
    trace = sum(G[i][i] for i in range(n)) or 1.0
    colonnes = []
    explique = 0.0
    premiere = None
    for composante in range(min(k, n - 1)):
        v = [1.0 + ((i * 7919 + composante * 104729) % 13) / 13.0 for i in range(n)]
        norme = math.sqrt(sum(x * x for x in v))
        v = [x / norme for x in v]
        valeur = 0.0
        for _ in range(200):
            w = [sum(G[i][j] * v[j] for j in range(n)) for i in range(n)]
            norme = math.sqrt(sum(x * x for x in w))
            if norme < 1e-14:
                break
            w = [x / norme for x in w]
            ecart = max(abs(a - b) for a, b in zip(v, w))
            v = w
            valeur = norme
            if ecart < 1e-10:
                break
        if premiere is None:
            premiere = valeur
        if valeur <= 1e-9 * (premiere or 1.0):
            break
        colonnes.append([x * math.sqrt(valeur) for x in v])
        explique += valeur
        for i in range(n):
            for j in range(n):
                G[i][j] -= valeur * v[i] * v[j]
    Z = [[colonne[i] for colonne in colonnes] for i in range(n)]
    return Z, {'methode': 'pca_python', 'dimensions': len(colonnes), 'variance': explique / trace}


# ---------------------------------------------------------------------------
# Etape 7c : fuzzy c-means et indices de validite
# ---------------------------------------------------------------------------

def _dist2(X, V):
    """Distances euclidiennes au carre (n x c), numpy."""
    numpy = _np()
    d2 = (X ** 2).sum(axis=1)[:, None] + (V ** 2).sum(axis=1)[None, :] - 2.0 * X @ V.T
    return numpy.maximum(d2, 1e-12)


def _appartenances(X, V, m):
    """Degres u_ik = d_ik^(-2/(m-1)) / somme, calcules sans debordement meme pour m proche de 1."""
    numpy = _np()
    d2 = _dist2(X, V)
    rapport = d2 / d2.min(axis=1, keepdims=True)
    poids = rapport ** (-1.0 / (m - 1.0))
    return poids / poids.sum(axis=1, keepdims=True)


def _centres_initiaux(X, c, hasard):
    """Initialisation de type k-means++ (deterministe pour une graine donnee)."""
    numpy = _np()
    n = X.shape[0]
    indices = [int(hasard.integers(n))]
    for _ in range(1, c):
        d2 = _dist2(X, X[indices]).min(axis=1)
        total = float(d2.sum())
        if total <= 0:
            indices.append(int(hasard.integers(n)))
            continue
        indices.append(int(hasard.choice(n, p=d2 / total)))
    return X[indices].copy()


def fcm(X, c, m=None, tolerance=None, iterations=None, graine=0):
    """Fuzzy c-means, numpy (repli pur Python sans numpy).

    Renvoie ``(U, V, J, iterations)`` : appartenances (n x c, lignes de somme 1),
    centres, critere, nombre d'iterations. ``m`` doit etre > 1 (1,1 a 1,5 recommande).
    """
    m = float(m if m is not None else _seuil('m1.fcm.m'))
    if m <= 1.0:
        raise ValueError("L'exposant de flou m doit etre strictement superieur a 1.")
    tolerance = float(tolerance if tolerance is not None else _seuil('m1.fcm.tolerance'))
    iterations = int(iterations or _seuil('m1.fcm.iterations_max'))
    numpy = _np()
    if numpy is None:
        return _fcm_python(X, c, m, tolerance, iterations, graine)
    X = numpy.asarray(X, dtype=numpy.float64)
    hasard = numpy.random.default_rng(graine)
    V = _centres_initiaux(X, c, hasard)
    U = _appartenances(X, V, m)
    fait = 0
    for fait in range(1, iterations + 1):
        Um = U ** m
        V = (Um.T @ X) / numpy.maximum(Um.sum(axis=0), 1e-12)[:, None]
        U_suivant = _appartenances(X, V, m)
        ecart = float(numpy.abs(U_suivant - U).max())
        U = U_suivant
        if ecart < tolerance:
            break
    J = float(((U ** m) * _dist2(X, V)).sum())
    return U, V, J, fait


def _fcm_skfuzzy(X, c, m, tolerance, iterations, graine):
    """FCM par scikit-fuzzy (mise en forme identique a ``fcm``)."""
    numpy = _np()
    cluster = importlib.import_module('skfuzzy.cluster')
    centres, u, _, _, historique, fait, _ = cluster.cmeans(
        numpy.asarray(X, dtype=numpy.float64).T, c, m, error=tolerance, maxiter=iterations, seed=graine)
    U = numpy.asarray(u).T
    V = numpy.asarray(centres)
    J = float(((U ** m) * _dist2(numpy.asarray(X, dtype=numpy.float64), V)).sum())
    return U, V, J, int(fait)


def _fcm_python(X, c, m, tolerance, iterations, graine):
    """FCM sans numpy, pour de petits volumes (listes de listes)."""
    hasard = random.Random(graine)
    n = len(X)
    k = len(X[0])

    def d2(a, b):
        return max(sum((x - y) ** 2 for x, y in zip(a, b)), 1e-12)

    centres = [list(X[hasard.randrange(n)])]
    while len(centres) < c:
        distances = [min(d2(x, v) for v in centres) for x in X]
        total = sum(distances)
        seuil = hasard.random() * total
        cumul = 0.0
        choisi = n - 1
        for index, valeur in enumerate(distances):
            cumul += valeur
            if cumul >= seuil:
                choisi = index
                break
        centres.append(list(X[choisi]))

    def appartenances(V):
        lignes = []
        for x in X:
            distances = [d2(x, v) for v in V]
            plus_petite = min(distances)
            poids = [(valeur / plus_petite) ** (-1.0 / (m - 1.0)) for valeur in distances]
            somme = sum(poids)
            lignes.append([p / somme for p in poids])
        return lignes

    U = appartenances(centres)
    fait = 0
    for fait in range(1, iterations + 1):
        nouveaux = []
        for j in range(c):
            poids = [ligne[j] ** m for ligne in U]
            somme = sum(poids) or 1e-12
            nouveaux.append([sum(poids[i] * X[i][t] for i in range(n)) / somme for t in range(k)])
        U_suivant = appartenances(nouveaux)
        ecart = max(abs(a - b) for lu, ln in zip(U, U_suivant) for a, b in zip(lu, ln))
        U, centres = U_suivant, nouveaux
        if ecart < tolerance:
            break
    J = sum((U[i][j] ** m) * d2(X[i], centres[j]) for i in range(n) for j in range(c))
    return U, centres, J, fait


def indices_validite(X, U, V, m):
    """Indices de validite d'une partition floue.

    ``xie_beni`` (plus petit = meilleur), ``coefficient_partition`` (PC, plus grand =
    meilleur mais biaise vers les petits c) et sa version corrigee ``pc_modifie``
    (0 = partition uniforme, 1 = partition nette).
    """
    numpy = _np()
    if numpy is not None:
        X = numpy.asarray(X, dtype=numpy.float64)
        U = numpy.asarray(U, dtype=numpy.float64)
        V = numpy.asarray(V, dtype=numpy.float64)
        n, c = U.shape
        compacite = float(((U ** m) * _dist2(X, V)).sum())
        ecarts = _dist2(V, V)
        ecarts[numpy.diag_indices(c)] = numpy.inf
        separation = float(ecarts.min()) if c > 1 else 1e-12
        pc = float((U ** 2).sum() / n)
    else:
        n = len(U)
        c = len(U[0])
        compacite = sum((U[i][j] ** m) * max(sum((a - b) ** 2 for a, b in zip(X[i], V[j])), 1e-12)
                        for i in range(n) for j in range(c))
        separation = min(sum((a - b) ** 2 for a, b in zip(V[i], V[j])) for i in range(c) for j in range(i + 1, c)) if c > 1 else 1e-12
        pc = sum(u * u for ligne in U for u in ligne) / n
    xb = compacite / (n * max(separation, 1e-12))
    pc_modifie = 1.0 - (c / (c - 1.0)) * (1.0 - pc) if c > 1 else 0.0
    return {'xie_beni': xb, 'coefficient_partition': pc, 'pc_modifie': pc_modifie}


def choisir_c(resultats):
    """Choisit c d'apres Xie-Beni (minimum) et coefficient de partition corrige (maximum).

    ``resultats`` : liste de dicts avec ``c``, ``xie_beni``, ``pc_modifie``. On somme le
    rang de chaque c selon les deux indices ; en cas d'egalite, le plus petit Xie-Beni gagne.
    """
    if not resultats:
        raise ValueError('Aucun resultat a comparer.')
    par_xb = sorted(resultats, key=lambda r: (r['xie_beni'], r['c']))
    par_pc = sorted(resultats, key=lambda r: (-r['pc_modifie'], r['c']))
    rang = {r['c']: 0 for r in resultats}
    for position, r in enumerate(par_xb):
        rang[r['c']] += position
    for position, r in enumerate(par_pc):
        rang[r['c']] += position
    meilleur = min(resultats, key=lambda r: (rang[r['c']], r['xie_beni'], r['c']))
    return meilleur['c']


def clusteriser(X, c=None, dimensions=None, m=None, c_min=None, c_max=None, reduction='auto', moteur='auto'):
    """Reduction puis FCM, avec choix de c par indices de validite si ``c`` est None.

    ``X`` : n x d (matrice numpy ou liste de listes), idealement normalisee.
    Renvoie un dict : ``U`` (n x c, liste de listes), ``centres`` (dans l'espace reduit),
    ``c``, ``m``, ``indices`` (une ligne par c essaye), ``reduction``, ``moteur``,
    ``secondes_reduction``, ``secondes_fcm``.
    """
    m = float(m if m is not None else _seuil('m1.fcm.m'))
    if not 1.0 < m <= 2.0:
        raise ValueError("m doit etre compris entre 1 (exclu) et 2 ; 1,1 a 1,5 recommande.")
    n = len(X)
    c_min = int(c_min or _seuil('m1.fcm.c_min'))
    c_max = int(c_max or _seuil('m1.fcm.c_max'))
    debut = time.monotonic()
    Z, infos_reduction = reduire(X, dimensions, reduction)
    secondes_reduction = time.monotonic() - debut
    tolerance = float(_seuil('m1.fcm.tolerance'))
    iterations = int(_seuil('m1.fcm.iterations_max'))
    graine = int(_seuil('m1.fcm.graine'))
    numpy = _np()
    nom_moteur = 'numpy' if numpy is not None else 'python'
    ajuster = fcm
    if moteur in ('auto', 'skfuzzy') and numpy is not None and moteurs.paquet_present('skfuzzy'):
        ajuster = lambda Zc, cc, mm, tol, it, g: _fcm_skfuzzy(Zc, cc, mm, tol, it, g)
        nom_moteur = 'skfuzzy'
    elif moteur == 'skfuzzy':
        _erreur('scikit-fuzzy demande mais non installe.')
    debut = time.monotonic()
    if c is not None:
        valeurs = [int(c)]
    else:
        plafond = max(c_min, min(c_max, n // 2))
        valeurs = [v for v in range(c_min, plafond + 1) if v < n]
        if not valeurs:
            valeurs = [max(1, min(c_min, n))]
    essais = {}
    nombre_departs = 1 if numpy is None else int(_seuil('m1.fcm.initialisations'))
    for valeur in valeurs:
        noyau.srv().checkpoint()
        if valeur == 1:
            U = numpy.ones((n, 1)) if numpy is not None else [[1.0] for _ in range(n)]
            V = (numpy.asarray(Z).mean(axis=0, keepdims=True) if numpy is not None
                 else [[sum(r[t] for r in Z) / n for t in range(len(Z[0]))]])
            essais[1] = (U, V, 0.0, 0)
            continue
        meilleur = None
        for depart in range(nombre_departs):
            resultat = (ajuster(Z, valeur, m, tolerance, iterations, graine + depart))
            if meilleur is None or resultat[2] < meilleur[2]:
                meilleur = resultat
        essais[valeur] = meilleur
    lignes = []
    for valeur, (U, V, J, fait) in essais.items():
        if valeur < 2:
            continue
        ligne = {'c': valeur, 'critere': J, 'iterations': fait}
        ligne.update(indices_validite(Z, U, V, m))
        lignes.append(ligne)
    retenu = int(c) if c is not None else (choisir_c(lignes) if lignes else valeurs[0])
    U, V, J, fait = essais[retenu]
    secondes_fcm = time.monotonic() - debut
    return {
        'U': U.tolist() if hasattr(U, 'tolist') else U,
        'centres': V.tolist() if hasattr(V, 'tolist') else V,
        'c': retenu,
        'm': m,
        'indices': lignes,
        'reduction': infos_reduction,
        'moteur': nom_moteur,
        'secondes_reduction': secondes_reduction,
        'secondes_fcm': secondes_fcm,
    }


def affecter(U, seuil=None):
    """Appartenances retenues : pour chaque ligne, ``[(j, degre)]`` au-dela du seuil.

    Une page appartient souvent a plusieurs sujets. Si aucun degre n'atteint le seuil
    (c eleve, partition floue), le sujet de plus fort degre est conserve.
    """
    seuil = float(seuil if seuil is not None else _seuil('m1.fcm.appartenance'))
    retenues = []
    for ligne in U:
        choix = [(j, float(d)) for j, d in enumerate(ligne) if d >= seuil]
        if not choix:
            meilleur = max(range(len(ligne)), key=lambda j: ligne[j])
            choix = [(meilleur, float(ligne[meilleur]))]
        retenues.append(choix)
    return retenues


# ---------------------------------------------------------------------------
# Etape 7d : sujets (stockage, libelles, heritage)
# ---------------------------------------------------------------------------

def libelles(textes_par_page, appartenances, nombre_sujets, termes=None):
    """Libelle deterministe de chaque sujet : termes distinctifs (c-TF-IDF), sans LLM.

    ``textes_par_page`` : liste de textes alignee sur les pages ; ``appartenances`` :
    liste de ``[(sujet, degre)]``. Renvoie une liste de chaines ASCII, un par sujet.
    """
    termes = int(termes or _seuil('m1.libelle.termes'))
    effectifs = [Counter() for _ in range(nombre_sujets)]
    for texte, memberships in zip(textes_par_page, appartenances):
        mots = [w for w in re.findall(r'[a-z]{4,}', _sans_accents(texte).lower()) if w not in MOTS_VIDES]
        if not mots:
            continue
        compte = Counter(mots)
        for sujet, degre in memberships:
            for mot, nombre in compte.items():
                effectifs[sujet][mot] += degre * nombre
    total_mot = Counter()
    for effectif in effectifs:
        total_mot.update(effectif)
    moyenne = (sum(sum(e.values()) for e in effectifs) / max(1, nombre_sujets)) or 1.0
    resultat = []
    for sujet, effectif in enumerate(effectifs):
        masse = sum(effectif.values()) or 1.0
        scores = []
        for mot, valeur in effectif.items():
            if valeur < 2.0:
                continue
            scores.append((-(valeur / masse) * math.log(1.0 + moyenne / total_mot[mot]), mot))
        scores.sort()
        retenus = [mot for _, mot in scores[:termes]]
        resultat.append(' / '.join(retenus) if retenus else 'sujet-' + str(sujet + 1))
    return resultat


def sujets(methode='fcm', forcer=False):
    """Calcule les sujets au niveau page, puis les propage aux unites.

    ``methode`` : ``'fcm'`` (defaut) ou ``'hdbscan'`` (comparaison, import paresseux ;
    ne fixe pas c et isole le bruit). Les sujets de chaque methode coexistent dans
    ``topics.method`` ; ``dedoublonner(methode=...)`` choisit celle qu'il utilise.
    Sans ``forcer``, un calcul deja fait pour toutes les pages encodees est conserve
    (l'heritage vers les unites est toujours remis a jour).
    """
    if methode not in ('fcm', 'hdbscan'):
        raise ValueError("methode doit valoir 'fcm' ou 'hdbscan'.")
    serveur = noyau.srv()
    ids, X = charger_vecteurs('versions')
    resultat = {'methode': methode, 'pages': len(ids), 'sujets': 0, 'saute': False}
    if len(ids) == 0:
        a_encoder = serveur.query(
            'SELECT count(*) n FROM versions x WHERE EXISTS(SELECT 1 FROM units u WHERE u.version_id=x.id '
            'AND COALESCE(u.excluded,0)=0)', (), True)['n']
        if not a_encoder:
            # Rien a encoder (aucune unite) : l'etape se saute proprement au lieu d'echouer.
            resultat.update(saute=True, raison='aucune page avec unites a encoder')
            return resultat
        _erreur("Aucune page encodee : lancez d'abord encoder('pages').")
    existants = serveur.query('SELECT id FROM topics WHERE method=?', (methode,))
    if existants and not forcer:
        couvertes = serveur.query(
            "SELECT count(DISTINCT target_id) n FROM topic_membership WHERE target_kind='version' AND topic_id IN "
            "(SELECT id FROM topics WHERE method=?)", (methode,), True)['n']
        if couvertes >= len(ids):
            resultat.update(saute=True, sujets=len(existants), unites_heritees=_heriter(methode))
            return resultat
    _job(stage='m1.sujets', done=0, total=len(ids), message='Sujets (%s) sur %d pages' % (methode, len(ids)))
    with noyau.etape('m1.sujets', moteurs={'sujets': methode}, prompt='',
                     seuils=['m1.reduction.dimensions', 'm1.fcm.m', 'm1.fcm.c_min', 'm1.fcm.c_max', 'm1.fcm.appartenance'],
                     pages=len(ids)) as run:
        if methode == 'fcm':
            appartenance, noms, infos = _sujets_fcm(ids, X)
        else:
            appartenance, noms, infos = _sujets_hdbscan(ids, X)
        run.moteur('sujets', methode + ':' + str(infos.get('moteur', infos.get('reduction', {}).get('methode', ''))))
        run.items = len(noms)
        run.note = json.dumps({k: v for k, v in infos.items() if k not in ('indices',)}, default=str)[:900]
        _ecrire_sujets(run.id, methode, ids, X, appartenance, noms)
        resultat['unites_heritees'] = _heriter(methode)
    resultat.update(infos)
    resultat['sujets'] = len(noms)
    resultat['libelles'] = noms
    return resultat


def _sujets_fcm(ids, X):
    """Sujets FCM : renvoie (appartenances par page, libelles, infos)."""
    n = len(ids)
    if n < 4:
        retenues = [[(0, 1.0)] for _ in ids]
        noms = libelles(_textes_pages(ids), retenues, 1)
        return retenues, noms, {'c': 1, 'reduction': {'methode': 'aucune', 'dimensions': 0}, 'indices': [],
                                'secondes_reduction': 0.0, 'secondes_fcm': 0.0, 'moteur': 'trivial'}
    reglage_reduction = noyau.reglage('m1.reduction.methode', 'auto')
    reglage_moteur = noyau.reglage('m1.fcm.moteur', 'auto')
    calcul = clusteriser(X, reduction=reglage_reduction, moteur=reglage_moteur)
    retenues = affecter(calcul['U'])
    noms = libelles(_textes_pages(ids), retenues, calcul['c'])
    infos = {k: calcul[k] for k in ('c', 'm', 'indices', 'reduction', 'moteur', 'secondes_reduction', 'secondes_fcm')}
    return retenues, noms, infos


def _sujets_hdbscan(ids, X):
    """Sujets HDBSCAN a appartenance souple (import paresseux) ; le bruit n'a pas de sujet."""
    try:
        hdbscan = importlib.import_module('hdbscan')
    except ImportError:
        _erreur("hdbscan n'est pas installe : methode de comparaison indisponible (pip install hdbscan).")
    numpy = _np()
    if numpy is None:
        _erreur('HDBSCAN demande numpy.')
    Z, infos_reduction = reduire(X, None, noyau.reglage('m1.reduction.methode', 'auto'))
    debut = time.monotonic()
    taille = int(_seuil('m1.hdbscan.taille_min'))
    classeur = hdbscan.HDBSCAN(min_cluster_size=max(2, min(taille, len(ids))), prediction_data=True).fit(Z)
    souple = numpy.asarray(hdbscan.all_points_membership_vectors(classeur))
    if souple.ndim != 2 or souple.shape[1] == 0:
        return [[] for _ in ids], [], {'reduction': infos_reduction, 'bruit': len(ids), 'secondes_fcm': time.monotonic() - debut,
                                       'moteur': 'hdbscan'}
    seuil = float(_seuil('m1.fcm.appartenance'))
    retenues = [[(int(j), float(d)) for j, d in enumerate(ligne) if d >= seuil] for ligne in souple]
    bruit = int(sum(1 for etiquette in classeur.labels_ if etiquette < 0))
    noms = libelles(_textes_pages(ids), retenues, souple.shape[1])
    return retenues, noms, {'reduction': infos_reduction, 'bruit': bruit, 'secondes_fcm': time.monotonic() - debut,
                            'moteur': 'hdbscan', 'c': int(souple.shape[1]), 'secondes_reduction': 0.0}


def _textes_pages(ids):
    """Titre (compte triple) + debut du texte des versions, dans l'ordre de ``ids``."""
    serveur = noyau.srv()
    textes = {}
    for debut in range(0, len(ids), 400):
        morceau = ids[debut:debut + 400]
        marques = ','.join('?' for _ in morceau)
        for ligne in serveur.query(
            'SELECT v.id, v.text, p.title FROM versions v LEFT JOIN pages p ON p.id=v.page_id WHERE v.id IN (' + marques + ')',
            tuple(morceau),
        ):
            titre = (ligne['title'] or '')
            textes[ligne['id']] = (titre + ' ') * 3 + (ligne['text'] or '')[:8000]
    return [textes.get(i, '') for i in ids]


def _ecrire_sujets(run_id, methode, ids, X, appartenance, noms):
    """Remplace les sujets de la methode : ``topics`` (centroide dans l'espace d'origine) et ``topic_membership``."""
    serveur = noyau.srv()
    numpy = _np()
    identifiants = [serveur.uid() for _ in noms]
    centroides = []
    for sujet in range(len(noms)):
        poids = [(index, dict(ligne).get(sujet, 0.0)) for index, ligne in enumerate(appartenance)]
        poids = [(i, w) for i, w in poids if w > 0]
        if numpy is not None and poids and hasattr(X, 'shape'):
            lignes = numpy.asarray([X[i] for i, _ in poids], dtype=numpy.float64)
            coefficients = numpy.asarray([w for _, w in poids])[:, None]
            centre = (lignes * coefficients).sum(axis=0) / coefficients.sum()
            norme = numpy.linalg.norm(centre) or 1.0
            centroides.append((centre / norme).tolist())
        elif poids:
            total = sum(w for _, w in poids)
            dimension = len(X[0])
            centre = [sum(w * X[i][t] for i, w in poids) / total for t in range(dimension)]
            norme = math.sqrt(sum(x * x for x in centre)) or 1.0
            centroides.append([x / norme for x in centre])
        else:
            centroides.append([])
    with noyau.connexion() as base:
        anciens = [r[0] for r in base.execute('SELECT id FROM topics WHERE method=?', (methode,))]
        for ancien in anciens:
            base.execute('DELETE FROM topic_membership WHERE topic_id=?', (ancien,))
        base.execute('DELETE FROM topics WHERE method=?', (methode,))
        base.executemany(
            'INSERT INTO topics(id,label,method,centroid,run_id) VALUES(?,?,?,?,?)',
            [(identifiants[j], noms[j], methode, _vers_json(centroides[j]) if centroides[j] else None, run_id)
             for j in range(len(noms))],
        )
        base.executemany(
            "INSERT INTO topic_membership(topic_id,target_kind,target_id,degree) VALUES(?,'version',?,?)",
            [(identifiants[j], ids[i], round(d, 6)) for i, ligne in enumerate(appartenance) for j, d in ligne],
        )


def _heriter(methode):
    """Les unites heritent des sujets de leur page (une seule requete). Renvoie le nombre de lignes."""
    with noyau.connexion() as base:
        base.execute(
            "DELETE FROM topic_membership WHERE target_kind='unit' AND topic_id IN (SELECT id FROM topics WHERE method=?)",
            (methode,),
        )
        curseur = base.execute(
            "INSERT INTO topic_membership(topic_id,target_kind,target_id,degree) "
            "SELECT tm.topic_id,'unit',u.id,tm.degree FROM topic_membership tm "
            "JOIN topics t ON t.id=tm.topic_id AND t.method=? "
            "JOIN units u ON u.version_id=tm.target_id WHERE tm.target_kind='version'",
            (methode,),
        )
        return curseur.rowcount


# ---------------------------------------------------------------------------
# Etape 8a : candidats semantiques
# ---------------------------------------------------------------------------

def _voisins_numpy(M, k, seuil, bloc):
    """k plus proches voisins par cosinus, par blocs. Renvoie {(i, j): cosinus} avec i < j."""
    numpy = _np()
    n = M.shape[0]
    paires = {}
    if n < 2:
        return paires
    kk = min(k, n - 1)
    for debut in range(0, n, bloc):
        S = M[debut:debut + bloc] @ M.T
        lignes = numpy.arange(S.shape[0])
        S[lignes, debut + lignes] = -2.0
        indices = numpy.argpartition(-S, kk - 1, axis=1)[:, :kk]
        valeurs = numpy.take_along_axis(S, indices, axis=1)
        rangees, colonnes = numpy.nonzero(valeurs >= seuil)
        for r, col in zip(rangees.tolist(), colonnes.tolist()):
            i = debut + r
            j = int(indices[r, col])
            cle = (i, j) if i < j else (j, i)
            score = float(valeurs[r, col])
            if score > paires.get(cle, -2.0):
                paires[cle] = score
    return paires


def _voisins_faiss(M, k, seuil):
    """Meme resultat avec FAISS (IndexFlatIP) ; None si FAISS est indisponible."""
    if not moteurs.paquet_present('faiss'):
        return None
    numpy = _np()
    try:
        faiss = importlib.import_module('faiss')
        index = faiss.IndexFlatIP(M.shape[1])
        index.add(numpy.ascontiguousarray(M, dtype=numpy.float32))
        scores, voisins = index.search(numpy.ascontiguousarray(M, dtype=numpy.float32), min(k + 1, M.shape[0]))
    except Exception:
        return None
    paires = {}
    for i in range(M.shape[0]):
        for score, j in zip(scores[i].tolist(), voisins[i].tolist()):
            if j < 0 or j == i or score < seuil:
                continue
            cle = (i, j) if i < j else (j, i)
            if score > paires.get(cle, -2.0):
                paires[cle] = score
    return paires


def _voisins_python(M, k, seuil):
    """Repli pur Python pour de petits volumes (n^2)."""
    limite = int(_seuil('m1.python.unites_max'))
    n = len(M)
    if n > limite:
        _erreur('numpy absent : le repli pur Python est limite a %d unites par sujet (%d).' % (limite, n))
    paires = {}
    for i in range(n):
        scores = [(sum(a * b for a, b in zip(M[i], M[j])), j) for j in range(n) if j != i]
        scores.sort(reverse=True)
        for score, j in scores[:k]:
            if score >= seuil:
                cle = (i, j) if i < j else (j, i)
                paires[cle] = max(score, paires.get(cle, -2.0))
    return paires


def voisins_proches(M, k, seuil, moteur='auto', bloc=None):
    """Paires de voisins ``{(i, j): cosinus}`` d'une matrice normalisee (FAISS, numpy par blocs, ou Python)."""
    numpy = _np()
    if numpy is None:
        return _voisins_python(M, k, seuil)
    if moteur in ('auto', 'faiss'):
        resultat = _voisins_faiss(M, k, seuil)
        if resultat is not None:
            return resultat
    return _voisins_numpy(M, k, seuil, int(bloc or _seuil('dedup.bloc')))


def _portees(methode):
    """Portees de comparaison : liste de ``(libelle, [ids d'unites])``.

    Une portee par sujet (les unites d'un meme sujet heritent de leur page) ; les unites
    sans sujet vont dans ``_sans_sujet`` ; sans aucun sujet calcule, une portee unique ``tout``.
    """
    serveur = noyau.srv()
    unites = [ligne['id'] for ligne in serveur.query(
        "SELECT id FROM units WHERE COALESCE(excluded,0)=0 AND vector IS NOT NULL AND vector!='' ORDER BY id")]
    lignes = serveur.query(
        "SELECT tm.topic_id, tm.target_id FROM topic_membership tm JOIN topics t ON t.id=tm.topic_id AND t.method=? "
        "WHERE tm.target_kind='unit'", (methode,))
    if not lignes:
        return [('tout', unites)], False
    par_sujet = defaultdict(set)
    avec_sujet = set()
    for ligne in lignes:
        par_sujet[ligne['topic_id']].add(ligne['target_id'])
        avec_sujet.add(ligne['target_id'])
    portees = [(sujet, sorted(membres)) for sujet, membres in sorted(par_sujet.items())]
    sans = [i for i in unites if i not in avec_sujet]
    if sans:
        portees.append(('_sans_sujet', sans))
    return portees, True


def candidats_semantiques(methode='fcm', k=None, seuil=None):
    """Paires candidates de doublons : voisins cosinus a l'interieur d'un meme sujet.

    Renvoie ``(paires, stats)``, ``paires`` = liste triee de ``(a, b, cosinus)`` avec
    ``a < b`` (identifiants d'unites), du cosinus le plus fort au plus faible. Ne fait
    aucun appel NLI ni LLM.
    """
    k = int(k or _seuil('dedup.voisins_max'))
    seuil = float(seuil if seuil is not None else _seuil('dedup.cosinus'))
    ids_tous, M_tous = charger_vecteurs('units')
    position = {identifiant: index for index, identifiant in enumerate(ids_tous)}
    portees, avec_sujets = _portees(methode)
    meilleures = {}
    comparees = 0
    for libelle, membres in portees:
        noyau.srv().checkpoint()
        presents = [position[i] for i in membres if i in position]
        if len(presents) < 2:
            continue
        comparees += len(presents)
        numpy = _np()
        sous = M_tous[presents] if numpy is not None else [M_tous[p] for p in presents]
        for (i, j), score in voisins_proches(sous, k, seuil).items():
            a, b = ids_tous[presents[i]], ids_tous[presents[j]]
            cle = (a, b) if a < b else (b, a)
            if score > meilleures.get(cle, -2.0):
                meilleures[cle] = score
    paires = sorted(((a, b, s) for (a, b), s in meilleures.items()), key=lambda t: (-t[2], t[0], t[1]))
    stats = {'portees': len(portees), 'avec_sujets': avec_sujets, 'unites_comparees': comparees,
             'unites_sans_vecteur': 0, 'k': k, 'seuil': seuil}
    return paires, stats


# ---------------------------------------------------------------------------
# Etape 8b : qualification NLI, zone grise, LLM
# ---------------------------------------------------------------------------

def qualifier_probas(p_ab, p_ba, implication=None, contradiction=None, bas=None):
    """Classe une paire a partir des probabilites NLI dans les deux sens.

    ``p_ab`` : NLI (premisse a, hypothese b) ; ``p_ba`` : sens inverse ; dicts de
    probabilites ``implication`` / ``contradiction`` / ``neutre``.
    Renvoie ``(relation, direction)`` ou ``('zone_grise', '')`` :
      * implication dans les deux sens : ``equivalence``, ``mutuelle`` ;
      * dans un seul sens : ``precision`` (``a_precise_b`` si a implique b) ;
      * contradiction nette (sans implication) : ``conflit``, transmis a M3 ;
      * ni implication ni contradiction, partout sous ``bas`` : ``sans_rapport`` ;
      * tout le reste (entre ``bas`` et le seuil) : zone grise, tranchee par le LLM.
    """
    implication = float(implication if implication is not None else _seuil('nli.implication'))
    contradiction = float(contradiction if contradiction is not None else _seuil('nli.contradiction'))
    bas = float(bas if bas is not None else _seuil('m1.zone_grise')[0])
    impl_ab = p_ab['implication'] >= implication
    impl_ba = p_ba['implication'] >= implication
    contra = p_ab['contradiction'] >= contradiction or p_ba['contradiction'] >= contradiction

    def neutre(p):
        return max(p['implication'], p['contradiction']) < bas

    if impl_ab and impl_ba and not contra:
        return 'equivalence', 'mutuelle'
    if contra and not (impl_ab or impl_ba):
        return 'conflit', ''
    if contra:
        return 'zone_grise', ''
    if impl_ab != impl_ba:
        autre = p_ba if impl_ab else p_ab
        if neutre(autre) or autre['implication'] < bas:
            return 'precision', 'a_precise_b' if impl_ab else 'b_precise_a'
        return 'zone_grise', ''
    if neutre(p_ab) and neutre(p_ba):
        return 'sans_rapport', ''
    return 'zone_grise', ''


def _signature(texte_a, texte_b):
    """Empreinte des deux textes : invalide le cache si une unite a change."""
    return hashlib.sha1((texte_a + '\x1f' + texte_b).encode('utf-8')).hexdigest()[:16]


def _preparer_tables():
    """Colonne ``unit_groups.merged_into`` et cache des paires qualifiees (tables de ce module)."""
    schema.colonne('unit_groups', 'merged_into', 'TEXT')
    with noyau.connexion() as base:
        base.execute(
            'CREATE TABLE IF NOT EXISTS dedup_nli(a TEXT, b TEXT, sig TEXT, cosinus REAL, nli_ab TEXT, nli_ba TEXT, '
            'engine TEXT, llm TEXT, llm_engine TEXT, run_id TEXT, created TEXT, PRIMARY KEY(a,b))'
        )
        base.execute('CREATE INDEX IF NOT EXISTS idx_dedup_nli_b ON dedup_nli(b)')
        base.execute('CREATE INDEX IF NOT EXISTS idx_ch_unit_groups_merged ON unit_groups(merged_into)')


def _qualifier_par_nli(run, paires, textes, forcer):
    """NLI dans les deux sens pour les paires absentes du cache ; ecrit le cache lot par lot."""
    serveur = noyau.srv()
    lot_taille = max(1, int(_seuil('dedup.taille_lot_nli')))
    total = len(paires)
    fait = 0
    moteur_nli = ''
    for debut in range(0, total, lot_taille):
        serveur.checkpoint()
        lot = paires[debut:debut + lot_taille]
        demandes = []
        for a, b, _ in lot:
            demandes.append((textes[a], textes[b]))
            demandes.append((textes[b], textes[a]))
        sorties = moteurs.nli_lot(demandes)
        if len(sorties) != len(demandes):
            _erreur('NLI : %d resultats pour %d paires.' % (len(sorties), len(demandes)))
        lignes = []
        horodatage = serveur.now()
        for index, (a, b, cosinus) in enumerate(lot):
            sortie_ab = sorties[2 * index]
            sortie_ba = sorties[2 * index + 1]
            moteur_nli = sortie_ab.get('engine') or moteur_nli
            lignes.append((a, b, _signature(textes[a], textes[b]), cosinus,
                           json.dumps(sortie_ab['probas']), json.dumps(sortie_ba['probas']),
                           moteur_nli, run.id, horodatage))
        with noyau.connexion() as base:
            base.executemany(
                'INSERT OR REPLACE INTO dedup_nli(a,b,sig,cosinus,nli_ab,nli_ba,engine,llm,llm_engine,run_id,created) '
                'VALUES(?,?,?,?,?,?,?,NULL,NULL,?,?)', lignes)
        fait += len(lot)
        _job(done=fait, message='Deduplication : NLI %d/%d paires (%d appels)' % (fait, total, 2 * fait))
    run.moteur('nli', moteur_nli)
    return moteur_nli


def _trancher_zone_grise(run, grises, textes):
    """Le LLM local tranche les paires de la zone grise (question fermee, schema contraint).

    ``grises`` : liste de ``(a, b, p_ab, p_ba)``. Renvoie ``{(a, b): relation_llm}`` ; les paires
    sans reponse valide restent sans decision. Un arret demande est propage.
    """
    serveur = noyau.srv()
    taille = max(1, int(_seuil('dedup.llm_lot')))
    decisions = {}
    for debut in range(0, len(grises), taille):
        serveur.checkpoint()
        lot = grises[debut:debut + taille]
        payload = {
            'mode': 'chaine:dedup_paire',
            'paires': [
                {'id': index, 'a': textes[a], 'b': textes[b],
                 'nli_ab': {k: round(v, 3) for k, v in p_ab.items()},
                 'nli_ba': {k: round(v, 3) for k, v in p_ba.items()}}
                for index, (a, b, p_ab, p_ba) in enumerate(lot)
            ],
        }
        try:
            sortie = noyau.llm(TACHE_DEDUP, payload)
        except serveur.OperationalError:
            if _arret_demande():
                raise
            run.note = (run.note + ' ; ' if run.note else '') + 'LLM indisponible pour la zone grise'
            break
        run.moteur('llm', noyau.moteur_llm())
        for element in sortie['resultats']:
            index = element['id']
            if 0 <= index < len(lot):
                a, b = lot[index][0], lot[index][1]
                decisions.setdefault((a, b), element['relation'])
    return decisions


def _relation_depuis_llm(decision):
    """Relation du LLM -> ``(relation, direction)``."""
    if decision == 'equivalence':
        return 'equivalence', 'mutuelle'
    if decision == 'a_precise_b':
        return 'precision', 'a_precise_b'
    if decision == 'b_precise_a':
        return 'precision', 'b_precise_a'
    return decision, ''


# ---------------------------------------------------------------------------
# Etape 8c : doublons exacts, groupes
# ---------------------------------------------------------------------------

class _Fusion:
    """Union-find sur identifiants (groupes)."""

    def __init__(self):
        self.parent = {}

    def trouver(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def unir(self, a, b):
        ra, rb = self.trouver(a), self.trouver(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)

    def composantes(self):
        resultat = defaultdict(list)
        for x in list(self.parent):
            resultat[self.trouver(x)].append(x)
        return resultat


def doublons_exacts():
    """Classes de doublons certains, sans modele.

    1. Meme ensemble de triplets normalises (entite canonique, predicat, valeur normalisee,
       portee et periode lorsqu'elles existent) : un fait seul ne suffit pas, l'ensemble des faits
       de l'unite doit etre identique. 2. Texte identique apres normalisation (3 mots au moins).
    Renvoie une liste de ``(identifiants tries, critere)``.
    """
    serveur = noyau.srv()
    cles_faits = defaultdict(set)
    for ligne in serveur.query(
        "SELECT f.unit_id, f.subject_id, f.predicate, f.object_norm, f.scope, f.period FROM facts f "
        "JOIN units u ON u.id=f.unit_id WHERE COALESCE(u.excluded,0)=0 AND f.replaced_by IS NULL "
        "AND COALESCE(f.status,'') NOT IN ('rejete','rejetee') "
        "AND f.subject_id IS NOT NULL AND f.subject_id!='' AND f.predicate IS NOT NULL AND f.predicate!='' "
        "AND f.object_norm IS NOT NULL AND f.object_norm!=''"
    ):
        cles_faits[ligne['unit_id']].add((ligne['subject_id'], ligne['predicate'], ligne['object_norm'],
                                          ligne['scope'] or '', ligne['period'] or ''))
    par_triplets = defaultdict(list)
    for unite, cles in cles_faits.items():
        par_triplets[tuple(sorted(cles))].append(unite)
    classes = [(sorted(u), 'triplet_identique') for u in par_triplets.values() if len(u) > 1]
    par_texte = defaultdict(list)
    for ligne in serveur.query('SELECT id, text FROM units WHERE COALESCE(excluded,0)=0 AND text IS NOT NULL'):
        forme = _normaliser_texte(ligne['text'])
        if len(forme.split()) >= 3:
            par_texte[forme].append(ligne['id'])
    classes.extend((sorted(u), 'texte_identique') for u in par_texte.values() if len(u) > 1)
    return classes


def _groupes_par_unite():
    """``{unite: [groupes]}`` d'apres ``unit_group_members``."""
    resultat = defaultdict(list)
    for ligne in noyau.srv().query('SELECT unit_id, group_id FROM unit_group_members ORDER BY group_id'):
        resultat[ligne['unit_id']].append(ligne['group_id'])
    return resultat


def _creer_groupes_singletons(run):
    """Un groupe par unite encore sans groupe (les groupes migres existent deja)."""
    serveur = noyau.srv()
    orphelines = serveur.query(
        "SELECT u.id, u.text FROM units u WHERE COALESCE(u.excluded,0)=0 AND NOT EXISTS("
        "SELECT 1 FROM unit_group_members m WHERE m.unit_id=u.id) ORDER BY u.id")
    if not orphelines:
        return 0
    horodatage = serveur.now()
    groupes = [(serveur.uid(), ligne['id'], (ligne['text'] or '')[:300]) for ligne in orphelines]
    with noyau.connexion() as base:
        base.executemany(
            'INSERT INTO unit_groups(id,frame,label,legacy_claim_id,run_id,created) VALUES(?,NULL,?,NULL,?,?)',
            [(g, libelle, run.id, horodatage) for g, _, libelle in groupes])
        base.executemany('INSERT OR IGNORE INTO unit_group_members(group_id,unit_id) VALUES(?,?)',
                         [(g, u) for g, u, _ in groupes])
    return len(groupes)


def _fusionner_groupes(aretes):
    """Fusionne les groupes relies par des equivalences ; ne supprime aucun groupe ni aucun membre.

    ``aretes`` : couples d'unites equivalentes. Le survivant d'une composante est le groupe
    migre (``legacy_claim_id``) portant le plus de membres, a defaut le plus petit identifiant.
    Les membres des groupes absorbes sont deplaces vers le survivant ; chaque groupe absorbe
    est conserve avec ``merged_into`` = survivant (ses identifiants restent donc resolubles).
    Renvoie ``(groupes_absorbes, membres_deplaces)``.
    """
    serveur = noyau.srv()
    par_unite = _groupes_par_unite()
    fusion = _Fusion()
    for a, b in aretes:
        for groupe_a in par_unite.get(a, []):
            for groupe_b in par_unite.get(b, []):
                fusion.unir(groupe_a, groupe_b)
    composantes = {racine: membres for racine, membres in fusion.composantes().items() if len(membres) > 1}
    if not composantes:
        return 0, 0
    infos = {ligne['id']: ligne for ligne in serveur.query(
        'SELECT g.id, g.legacy_claim_id, g.frame, (SELECT count(*) FROM unit_group_members m WHERE m.group_id=g.id) n '
        'FROM unit_groups g')}
    absorbes = 0
    deplaces = 0
    with noyau.connexion() as base:
        for membres in composantes.values():
            survivant = min(membres, key=lambda g: (0 if infos[g]['legacy_claim_id'] else 1, -infos[g]['n'], g))
            for groupe in membres:
                if groupe == survivant:
                    continue
                lignes = [r[0] for r in base.execute('SELECT unit_id FROM unit_group_members WHERE group_id=?', (groupe,))]
                for unite in lignes:
                    base.execute('INSERT OR IGNORE INTO unit_group_members(group_id,unit_id) VALUES(?,?)', (survivant, unite))
                base.execute('DELETE FROM unit_group_members WHERE group_id=?', (groupe,))
                base.execute('UPDATE unit_groups SET merged_into=? WHERE id=?', (survivant, groupe))
                if not infos[survivant]['frame'] and infos[groupe]['frame']:
                    base.execute('UPDATE unit_groups SET frame=? WHERE id=?', (infos[groupe]['frame'], survivant))
                    infos[survivant]['frame'] = infos[groupe]['frame']
                absorbes += 1
                deplaces += len(lignes)
    return absorbes, deplaces


def _cadre_depuis_faits(unites):
    """Cadre JSON ``{sujet, predicat, portee, periode}`` du premier fait des unites, ou None."""
    if not unites:
        return None
    marques = ','.join('?' for _ in unites)
    ligne = noyau.srv().query(
        'SELECT subject, predicate, scope, period FROM facts WHERE unit_id IN (' + marques + ') '
        'AND predicate IS NOT NULL ORDER BY unit_id, id LIMIT 1', tuple(unites), True)
    if not ligne:
        return None
    return json.dumps({'sujet': ligne['subject'], 'predicat': ligne['predicate'],
                       'portee': ligne['scope'], 'periode': ligne['period']}, ensure_ascii=False)


def _ecrire_relations(run, lignes):
    """Ecrit ``unit_relations`` sans ecraser les relations posees par un autre module (M3, humain)."""
    if not lignes:
        return 0
    ecrites = 0
    with noyau.connexion() as base:
        for ligne in lignes:
            existante = base.execute('SELECT reason FROM unit_relations WHERE a=? AND b=?', (ligne[0], ligne[1])).fetchone()
            if existante is not None and not str(existante[0] or '').startswith('m1_dedup'):
                continue
            base.execute(
                'INSERT OR REPLACE INTO unit_relations(a,b,relation,direction,nli_ab,nli_ba,engine,reason,run_id,created) '
                'VALUES(?,?,?,?,?,?,?,?,?,?)', ligne)
            ecrites += 1
    return ecrites


# ---------------------------------------------------------------------------
# Etape 8 : deduplication
# ---------------------------------------------------------------------------

def dedoublonner(forcer=False, methode='fcm'):
    """Regroupe les doublons d'unites, sans en supprimer aucun.

    1. Exacts (triplets normalises, puis texte identique) : sans modele, groupes fusionnes.
    2. Candidats : plus proches voisins cosinus a l'interieur d'un sujet de ``methode``,
       au-dessus de ``dedup.cosinus``, au plus ``dedup.voisins_max`` voisins, hors paires deja
       dans un meme groupe, une paire par couple de groupes, au plus ``dedup.nli_paires_max`` paires
       qualifiees par execution (les plus proches d'abord ; une relance continue).
       Le nombre d'appels NLI (2 par paire) est annonce dans ``S.JOB['message']`` avant execution.
    3. NLI dans les deux sens (``moteurs.nli_lot``) : implication mutuelle -> ``equivalence`` (groupes
       fusionnes), un seul sens -> ``precision``, contradiction -> ``conflit`` (transmis a M3, non tranche),
       sinon ``sans_rapport``.
    4. Zone grise du NLI : le LLM local tranche (question fermee, schema ``chaine:dedup_paire``).
    5. Cache : ``dedup_nli`` conserve les probabilites NLI ; une paire deja qualifiee n'est pas repayee
       sans ``forcer``. ``unit_relations.nli_ab/nli_ba`` stockent P(implication) de chaque sens.
    Renvoie un dict de compteurs.
    """
    serveur = noyau.srv()
    _preparer_tables()
    _job(stage='m1.dedup', done=0, total=0, message='Deduplication : preparation')
    compteurs = {
        'unites': 0, 'unites_sans_vecteur': 0, 'groupes_crees': 0, 'classes_exactes': 0, 'unites_exactes': 0,
        'candidates': 0, 'deja_qualifiees': 0, 'paires_nli': 0, 'appels_nli': 0, 'tronquees': 0,
        'equivalence': 0, 'precision': 0, 'conflit': 0, 'sans_rapport': 0, 'zone_grise': 0,
        'llm_tranchees': 0, 'non_tranchees': 0, 'relations_ecrites': 0, 'groupes_fusionnes': 0,
        'membres_deplaces': 0, 'portees': 0,
    }
    with noyau.etape('m1.dedup', moteurs={'dedup': 'regles+nli'}, prompt='dedup-paire-v1',
                     seuils=['dedup.cosinus', 'dedup.voisins_max', 'm1.zone_grise', 'nli.implication', 'nli.contradiction']) as run:
        compteurs['groupes_crees'] += _creer_groupes_singletons(run)
        # --- 1. exacts
        textes = {ligne['id']: ligne['text'] or '' for ligne in serveur.query(
            'SELECT id, text FROM units WHERE COALESCE(excluded,0)=0')}
        compteurs['unites'] = len(textes)
        horodatage = serveur.now()
        relations = []
        aretes = []
        for membres, critere in doublons_exacts():
            representant = membres[0]
            compteurs['classes_exactes'] += 1
            compteurs['unites_exactes'] += len(membres)
            for autre in membres[1:]:
                a, b = (representant, autre) if representant < autre else (autre, representant)
                relations.append((a, b, 'equivalence', 'mutuelle', 1.0, 1.0, 'regles', 'm1_dedup:' + critere, run.id, horodatage))
                aretes.append((a, b))
        compteurs['relations_ecrites'] += _ecrire_relations(run, relations)
        absorbes, deplaces = _fusionner_groupes(aretes)
        compteurs['groupes_fusionnes'] += absorbes
        compteurs['membres_deplaces'] += deplaces
        # --- 2. candidats semantiques
        paires, stats = candidats_semantiques(methode)
        compteurs['portees'] = stats['portees']
        compteurs['candidates'] = len(paires)
        par_unite = _groupes_par_unite()

        def groupe_de(unite):
            groupes = par_unite.get(unite)
            return groupes[0] if groupes else unite

        retenues = []
        vus_groupes = set()
        une_par_groupe = bool(_seuil('dedup.une_paire_par_groupe'))
        for a, b, cosinus in paires:
            ga, gb = groupe_de(a), groupe_de(b)
            if ga == gb:
                continue
            if une_par_groupe:
                cle = (ga, gb) if ga < gb else (gb, ga)
                if cle in vus_groupes:
                    continue
                vus_groupes.add(cle)
            retenues.append((a, b, cosinus))
        cache = {}
        if not forcer:
            for ligne in serveur.query('SELECT a,b,sig FROM dedup_nli'):
                cache[(ligne['a'], ligne['b'])] = ligne['sig']
        a_qualifier = []
        for a, b, cosinus in retenues:
            if not forcer and cache.get((a, b)) == _signature(textes.get(a, ''), textes.get(b, '')):
                compteurs['deja_qualifiees'] += 1
            else:
                a_qualifier.append((a, b, cosinus))
        limite = int(_seuil('dedup.nli_paires_max'))
        if len(a_qualifier) > limite:
            compteurs['tronquees'] = len(a_qualifier) - limite
            a_qualifier = a_qualifier[:limite]
        compteurs['paires_nli'] = len(a_qualifier)
        compteurs['appels_nli'] = 2 * len(a_qualifier)
        portee_txt = 'par sujet (%d portees)' % stats['portees'] if stats['avec_sujets'] else 'sans sujet (portee unique)'
        _job(message=(
            'Deduplication : %d candidates %s (cosinus >= %.2f, %d voisins max) ; %d deja qualifiees en cache ; '
            '%d paires a qualifier = %d appels NLI%s'
        ) % (len(retenues), portee_txt, stats['seuil'], stats['k'], compteurs['deja_qualifiees'], len(a_qualifier),
             compteurs['appels_nli'], (' ; %d reportees a la prochaine execution' % compteurs['tronquees'])
             if compteurs['tronquees'] else ''), done=0, total=len(a_qualifier))
        run.note = serveur.JOB.get('message', '')
        # --- 3. NLI
        if a_qualifier:
            _qualifier_par_nli(run, a_qualifier, textes, forcer)
        # --- 4. classement, zone grise, LLM
        cles = [(a, b) for a, b, _ in retenues]
        lignes_cache = {}
        for debut in range(0, len(cles), 400):
            lot = cles[debut:debut + 400]
            conditions = ' OR '.join('(a=? AND b=?)' for _ in lot)
            arguments = tuple(x for paire in lot for x in paire)
            for ligne in serveur.query(
                'SELECT a,b,sig,nli_ab,nli_ba,engine,llm,llm_engine FROM dedup_nli WHERE ' + conditions, arguments):
                lignes_cache[(ligne['a'], ligne['b'])] = ligne
        classees = {}
        grises = []
        for a, b, cosinus in retenues:
            ligne = lignes_cache.get((a, b))
            if ligne is None or ligne['sig'] != _signature(textes.get(a, ''), textes.get(b, '')):
                continue
            p_ab, p_ba = json.loads(ligne['nli_ab']), json.loads(ligne['nli_ba'])
            relation, direction = qualifier_probas(p_ab, p_ba)
            moteur = ligne['engine'] or 'nli'
            raison = 'm1_dedup:nli'
            if relation == 'zone_grise':
                compteurs['zone_grise'] += 1
                if ligne['llm']:
                    relation, direction = _relation_depuis_llm(ligne['llm'])
                    moteur, raison = ligne['llm_engine'] or 'llm', 'm1_dedup:llm'
                    compteurs['llm_tranchees'] += 1
                else:
                    grises.append((a, b, p_ab, p_ba))
                    continue
            classees[(a, b)] = (relation, direction, p_ab, p_ba, moteur, raison)
        if grises:
            _job(message='Deduplication : %d paires en zone grise soumises au LLM local' % len(grises))
            decisions = _trancher_zone_grise(run, grises, textes)
            moteur_llm = noyau.moteur_llm()
            with noyau.connexion() as base:
                for (a, b), decision in decisions.items():
                    base.execute('UPDATE dedup_nli SET llm=?, llm_engine=? WHERE a=? AND b=?', (decision, moteur_llm, a, b))
            for a, b, p_ab, p_ba in grises:
                if (a, b) in decisions:
                    relation, direction = _relation_depuis_llm(decisions[(a, b)])
                    classees[(a, b)] = (relation, direction, p_ab, p_ba, moteur_llm, 'm1_dedup:llm')
                    compteurs['llm_tranchees'] += 1
                else:
                    compteurs['non_tranchees'] += 1
        # --- 5. ecriture des relations, fusion des equivalents
        horodatage = serveur.now()
        relations = []
        aretes = []
        for (a, b), (relation, direction, p_ab, p_ba, moteur, raison) in classees.items():
            compteurs[relation] += 1
            if relation == 'conflit':
                raison += ' contradiction=%.2f/%.2f transmis_m3' % (p_ab['contradiction'], p_ba['contradiction'])
            relations.append((a, b, relation, direction, p_ab['implication'], p_ba['implication'], moteur, raison, run.id, horodatage))
            if relation == 'equivalence':
                aretes.append((a, b))
        compteurs['relations_ecrites'] += _ecrire_relations(run, relations)
        absorbes, deplaces = _fusionner_groupes(aretes)
        compteurs['groupes_fusionnes'] += absorbes
        compteurs['membres_deplaces'] += deplaces
        _remplir_cadres(run)
        run.items = compteurs['relations_ecrites']
        run.note = (run.note + ' | ' if run.note else '') + json.dumps(compteurs, sort_keys=True)
    _job(message='Deduplication terminee : %d equivalences, %d precisions, %d conflits transmis a M3, %d groupes fusionnes'
         % (compteurs['equivalence'], compteurs['precision'], compteurs['conflit'], compteurs['groupes_fusionnes']))
    return compteurs


def _remplir_cadres(run):
    """Pose le cadre des groupes (sujet, predicat, portee, periode) quand un fait le donne et qu'il manque."""
    serveur = noyau.srv()
    lignes = serveur.query(
        'SELECT g.id, m.unit_id FROM unit_groups g JOIN unit_group_members m ON m.group_id=g.id '
        'WHERE g.frame IS NULL AND g.merged_into IS NULL AND EXISTS(SELECT 1 FROM unit_group_members n WHERE n.group_id=g.id '
        'AND n.unit_id!=m.unit_id) ORDER BY g.id')
    par_groupe = defaultdict(list)
    for ligne in lignes:
        par_groupe[ligne['id']].append(ligne['unit_id'])
    mises_a_jour = []
    for groupe, unites in par_groupe.items():
        cadre = _cadre_depuis_faits(unites)
        if cadre:
            mises_a_jour.append((cadre, groupe))
    if mises_a_jour:
        with noyau.connexion() as base:
            base.executemany('UPDATE unit_groups SET frame=? WHERE id=?', mises_a_jour)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def executer(forcer=False):
    """Etapes 7 et 8 : embeddings des pages et des unites, sujets, deduplication.

    Reprenable (les vecteurs, les sujets et les paires qualifiees deja calcules sont conserves
    sans ``forcer``). Renvoie un dict de compteurs par sous-etape.
    """
    resultat = {}
    resultat['encodage_pages'] = encoder('pages', forcer=forcer)
    resultat['encodage_unites'] = encoder('unites', forcer=forcer)
    resultat['sujets'] = {k: v for k, v in sujets('fcm', forcer=forcer).items()
                          if k not in ('indices', 'libelles')}
    resultat['dedup'] = dedoublonner(forcer=forcer)
    return resultat
