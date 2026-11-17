"""Seuils de la chaine (contrat 2.3).

Un seul dictionnaire ``DEFAUTS`` a cles pointees, surcharge par la table
``settings`` (cle ``seuil:<nom>``, valeur JSON ou texte). Aucun seuil en dur
dans les modules : ``seuils.get('nli.implication')``.

Les valeurs par defaut sont des points de depart a recalibrer sur le jeu de
reference (Protocole d'evaluation) et sur les decisions de revue (M6).
Les scores NLI sont des probabilites dans [0, 1].
"""
import json
import sqlite3
import time

from chaine import noyau

PREFIXE = 'seuil:'

DEFAUTS = {
    # --- Transverse : NLI et LLM -----------------------------------------
    'nli.implication': 0.80,              # P(implication) minimale pour dire « le texte implique »
    'nli.contradiction': 0.80,            # P(contradiction) minimale pour dire « contredit »
    'nli.taille_lot': 16,                 # paires par lot pour le modele local
    'nli.jetons_max': 512,                # fenetre du modele NLI local ; au-dela, la premisse est decoupee en fenetres
    'llm.nli.taille_lot': 8,              # paires par appel pour le repli LLM contraint
    'llm.classer.taille_lot': 20,         # textes par appel pour le classement par LLM

    # --- M1 etape 2 : segmentation ---------------------------------------
    'm1.segment.phrase_caracteres_max': 1200,   # au-dela, une « phrase » est recoupee
    'm1.segment.chunk_caracteres_max': 4000,    # paragraphe trop long : recoupe par phrases

    # --- M1 etape 4 : unites ---------------------------------------------
    'm1.unites.mots_min': 3,
    'm1.unites.mots_max': 60,
    'm1.unites.span_recouvrement_min': 0.90,    # part des mots de l'unite presents dans son span

    # --- M1 etape 5 : faits et garde-fou -----------------------------------
    'm1.faits.nli_implication': 0.80,           # sous ce seuil, le fait est rejete
    'm1.faits.entites_dans_span': True,         # les entites du fait doivent figurer dans le span
    'm1.entites.score_min': 0.50,               # seuil de detection GLiNER
    'm1.entites.alias_similarite': 0.90,        # canonicalisation par alias (ratio de chaines)

    # --- M1 etape 6 : classes d'obsolescence -----------------------------
    'm1.classe.confiance_min': 0.60,            # classifieur sous ce seuil : classe « inconnu » ou LLM
    'm1.classe.exemples_min': 10,               # exemples par classe pour SetFit (10 a 20)

    # --- M1 etape 7 : embeddings, sujets, FCM -----------------------------
    'm1.reduction.dimensions': 20,              # 10 a 50 dimensions avant FCM
    'm1.fcm.m': 1.3,                            # exposant de flou, bas (1,1 a 1,5)
    'm1.fcm.c_min': 2,
    'm1.fcm.c_max': 12,
    'm1.fcm.appartenance': 0.20,                # degre minimal pour appartenir a un sujet
    'm1.fcm.tolerance': 1e-5,
    'm1.fcm.iterations_max': 300,
    'm1.hdbscan.taille_min': 5,

    # --- M1 etape 8 : deduplication --------------------------------------
    'dedup.cosinus': 0.85,                      # candidats semantiques au-dessus de ce cosinus
    'dedup.voisins_max': 10,
    'm1.zone_grise': [0.40, 0.80],              # [bas, haut] du NLI : entre les deux, le LLM tranche

    # --- M2 : sources et typologie ----------------------------------------
    'm2.confiance.phrase': 0.90,                # source dans la meme phrase
    'm2.confiance.paragraphe': 0.60,            # rattachement au paragraphe
    'm2.confiance.page': 0.40,                  # rattachement a la page (encadre Sources)
    'm2.confiance_min': 0.60,                   # cascade : premiere regle au-dessus l'emporte, sinon LLM
    'm2.opinion.score_min': 0.60,
    'm2.interpretation.nli': 0.80,              # unite impliquee par deux unites de la page
    'm2.connaissance_generale.score_min': 0.60,
    'm2.source_manquante.cosinus': 0.85,        # equivalent sur une autre page du site

    # --- M3 : voisins, qualification, paternite ---------------------------
    'm3.voisins.cosinus': 0.75,
    'm3.voisins.max': 20,
    'm3.reranker.score_min': 0.30,
    'm3.zone_grise': [0.35, 0.75],              # [bas, haut] du NLI : le LLM juge le cadre
    'm3.nettement_plus_recent_jours': 180,      # ecart d'ancrage pour « remplacement » plutot que « conflit »
    'm3.centralite.ecart': 0.05,                # ecart de centralite sous lequel on considere une egalite
    'm3.centralite.poids_titre': 0.40,          # similarite unite / titre + chapo
    'm3.centralite.poids_hn': 0.30,             # entite ou predicat dans le titre ou un Hn
    'm3.centralite.poids_dependances': 0.30,    # part des unites de la page qui en dependent

    # --- M4 : statuts et requetes ------------------------------------------
    'm4.requete.caracteres_max': 200,           # limite de l'API Ibou
    'm4.requete.mots_min': 4,
    'm4.requete.mots_max': 30,
    'm4.requete.questions_max': 2,
    'm4.requete.inconnues_max': 2,              # inconnues ajoutees a la question
    'm4.perplexite_max': 250.0,                 # perplexite au-dessus : formulation artificielle
    'm4.naturel.score_min': 0.50,
    'm4.gsc.similarite_min': 0.50,
    'm4.reformulations_max': 1,                 # une seule reformulation LLM puis revue
    'm4.priorite.connaissance_generale': 0.10,

    # --- M5 : criticite et verification -----------------------------------
    'm5.critique': 0.60,                        # score de criticite au-dessus : verifier maintenant
    'm5.poids.centralite': 0.25,
    'm5.poids.emplacement': 0.20,
    'm5.poids.classe': 0.20,
    'm5.poids.statut': 0.20,
    'm5.poids.anciennete': 0.10,
    'm5.poids.audience': 0.05,
    'm5.endpoint': 'passage',
    'm5.max_results': 10,                       # premiere tranche analysee
    'm5.max_results_critique': 20,              # total recu des le premier appel (reserve incluse)
    'm5.unites_restantes_min': 30,              # sous ce solde Ibou, la file s'arrete proprement
    'm5.retry_after_max_secondes': 120,
    'm5.simhash.distance_max': 3,               # quasi-copies du site audite
    'm5.copie.recouvrement': 0.90,              # passage reprenant l'unite presque mot pour mot
    'm5.pertinence.reranker_min': 0.50,
    'm5.passages_pertinents_min': 2,            # en dessous : verdict « insuffisante »
    'm5.domaines_independants': 2,              # « confirmee » : au moins 2 domaines independants
    'm5.nli.soutien': 0.80,
    'm5.nli.contradiction': 0.80,

    # --- M6 : propositions --------------------------------------------------
    'm6.diff_ratio_max': 0.35,                  # budget de difference en ratio de mots changes
    'm6.tentatives_max': 2,                     # puis « a rediger par un humain »
    'm6.nli.faits_nouveaux': 0.80,
    'm6.nli.faits_conserves': 0.80,

    # --- Seuils reportes depuis les modules (lot integration) --------------
    # Valeurs identiques aux anciennes constantes locales (SEUILS_LOCAUX,
    # DEFAUTS_LOCAUX, LOCAUX, AJOUTS_SEUILS) ; les modules les lisent ici.
    # m1_page
    'm1.acquisition.octets_max': 2_000_000,     # meme limite que server.fetch_page
    'm1.acquisition.delai_secondes': 25,
    'm1.lot_unites': 400,                       # unites par transaction / par lot d'analyse
    # m1_page : scores des regles de classement (etape 6), compares a m1.classe.confiance_min
    'm1.regle.lexique.usage': 0.95,
    'm1.regle.lexique.metadonnee': 0.90,
    'm1.regle.temps.futur_date_future': 0.90,
    'm1.regle.temps.futur_date': 0.85,
    'm1.regle.lexique.prevision': 0.75,
    'm1.regle.temps.futur': 0.55,               # futur sans date : indice faible, le classifieur tranche
    'm1.regle.date_future': 0.70,
    'm1.regle.lexique.marqueur_actuel': 0.85,
    'm1.regle.lexique.fonction_present': 0.80,
    'm1.regle.temps.present_chiffre': 0.75,
    'm1.regle.lexique.evenement_date': 0.90,    # « a eu lieu le », « fonde en » + date passee
    'm1.regle.temps.passe_evenement_date': 0.85,  # temps passe + verbe d'evenement + date passee
    'm1.regle.temps.passe_evenement': 0.55,     # sans date : indice faible
    'm1.regle.lexique.definition': 0.65,
    'm1.regle.lexique.verite_generale': 0.55,   # indice faible (hors liste « immuable » du document)
    'm1.regle.temps.present': 0.45,
    'm1.regle.chiffre_date_passee': 0.55,
    # m1_unites
    'm1.unites.lot_caracteres': 3000,           # taille d'un appel LLM (option C) en caracteres de passages
    'm1.unites.premisse_caracteres': 1500,      # fenetre de texte autour du span pour le NLI
    'm1.faits.taille_lot': 8,                   # unites par appel LLM (faits)
    # m1_sujets
    'm1.encodage.taille_lot': 32,               # textes par appel d'embeddings
    'm1.encodage.page_caracteres': 3000,        # debut de page encode (titre + chapo + texte)
    'm1.fcm.initialisations': 4,                # departs aleatoires par valeur de c (on garde le meilleur)
    'm1.fcm.graine': 7,
    'm1.python.pages_max': 200,                 # repli pur Python : volume maximal en pages
    'm1.python.unites_max': 1500,               # repli pur Python : taille maximale d'un sujet pour le dedoublonnage
    'm1.libelle.termes': 3,
    'dedup.nli_paires_max': 3000,               # paires qualifiees par NLI au plus par execution (2 appels NLI chacune)
    'dedup.taille_lot_nli': 32,                 # paires par appel de nli_lot
    'dedup.llm_lot': 5,                         # paires de la zone grise par appel LLM
    'dedup.bloc': 1024,                         # lignes par bloc du produit matriciel
    'dedup.une_paire_par_groupe': True,         # une seule paire qualifiee par couple de groupes
    # m2
    'm2.interpretation.bonus_connecteur': 0.05,              # NLI confirme + connecteur : confiance relevee de ce bonus
    'm2.interpretation.confiance_max': 0.95,                 # ... sans depasser ce plafond
    'm2.interpretation.paires_max': 3,                       # k meilleures paires soumises au NLI par unite
    'm2.interpretation.candidats_max': 4,                    # candidats retenus avant de former les paires
    'm2.interpretation.recouvrement_sans_connecteur': 0.50,  # sans connecteur : recouvrement minimal du meilleur candidat
    'm2.interpretation.recouvrement_candidat': 0.15,         # recouvrement minimal pour etre candidat
    'm2.interpretation.paires_max_page': 600,                # plafond de paires NLI par page
    'm2.connecteur.non_confirme': 0.80,                      # facteur si le NLI a tourne sans confirmer
    'm2.generale.ambigu_bas': 0.40,                          # sous ce score : sans_source ; entre bas et score_min : LLM
    'm2.sources.paragraphe_max': 3,                          # sources rattachees au paragraphe par unite
    'm2.sources.page_max': 2,                                # references rapprochees au niveau page par unite
    'm2.llm.taille_lot': 10,                                 # unites par appel LLM (question fermee)
    'm2.opinion.page': 0.62,                                 # confiance d'une page de type tribune/edito sans autre indice
    'm2.opinion.classifieur_ambigu_bas': 0.20,               # score de regles au-dessus duquel le classifieur est consulte
    'm2.opinion.classifieur_toutes': False,                  # True : classifieur sur toutes les unites restantes
    'm2.information_source.page_seule': True,                # True : le type de page suffit (document)
    'm2.information_source.premiere_personne': 0.85,
    'm2.information_source.chiffres_auteur': 0.80,
    'm2.information_source.auteur_expert': 0.70,
    'm2.page.confiance_schema': 0.92,
    'm2.page.confiance_rubrique': 0.80,
    'm2.page.confiance_existant': 0.85,
    'm2.page.confiance_lexical_max': 0.55,                   # un classifieur lexical de repli ne decide jamais seul
    'm2.sans_source.confiance_base': 0.60,
    # m3
    'm3.voisins.lexical_min': 0.35,             # mode degrade : part idf des termes porteurs partages
    'm3.voisins.df_max': 0.02,                  # terme porteur : present dans au plus 2 % des unites
    'm3.voisins.candidats_max': 200,            # candidats soumis au reordonnancement
    'm3.recent.facteur_volatilite': 0.5,        # « nettement plus recent » = facteur x volatilite du predicat (0 : seuil fixe)
    'm3.propagation.equivalentes': True,        # les enfants equivalents apportent aussi leurs sources au parent
    'm3.qualification.lot': 32,                 # paires par appel NLI
    'm3.voisins.bloc': 512,
    'm3.centralite.chapo_caracteres': 600,      # chapo de repli (premier paragraphe) : coupe en fin de phrase sous ce nombre                     # unites par bloc du produit matriciel des voisins denses (numpy)
    # m4
    'm4.priorite.a_verifier': 1.0,
    'm4.priorite.source_ancienne': 0.5,
    # m5
    'm5.cache_jours': 30,                       # age maximal d'une reponse Ibou reutilisee
    'm5.volatilite_defaut_jours': 365,          # volatilite d'une unite sans predicat connu
    'm5.intervalle_secondes': 0.36,             # ecart minimal entre deux appels Ibou (debit)
    'm5.essais_max': 3,                         # essais d'un meme appel (429 debit, 503)
    'm5.retry_defaut_secondes': 2.0,            # pause si Retry-After est absent
    'm5.poids.officiel': 1.0,                   # administration, organisme statistique, institution
    'm5.poids.primaire': 0.9,                   # site de l'entite concernee
    'm5.poids.defaut': 0.6,
    'm5.poids.date_inconnue': 0.5,              # facteur si la date de la source est inconnue
    'm5.passage.caracteres_min': 200,           # paragraphes plus courts fusionnes avec le suivant
    'm5.passage.caracteres_max': 1200,          # paragraphes plus longs recoupes par phrases
    'm5.passages_hors_cadre_stockes': 3,        # passages ecartes conserves par page (trace)
    'm5.extraction.passages_max': 5,            # passages lus par le LLM pour une inconnue

    # --- Protocole d'evaluation (seuils de depart du document) -------------
    'evaluation.m1.precision_faits': 0.98,
    'evaluation.m1.rappel_faits': 0.80,
    'evaluation.m1.f1_classes': 0.85,
    'evaluation.m1.dedup_precision': 0.90,
    'evaluation.m1.dedup_rappel': 0.80,
    'evaluation.m2.f1_types': 0.80,
    'evaluation.m3.paternite': 0.85,
    'evaluation.m3.faux_conflits_max': 0.10,
    'evaluation.m4.note_humaine': 4.0,
    'evaluation.m5.accord': 0.90,
    'evaluation.m6.acceptees_sans_modif': 0.70,
}

_CACHE = {'base': None, 'instant': 0.0, 'valeurs': {}}
_DUREE_CACHE = 2.0


def get(nom):
    """Valeur du seuil ``nom`` (surcharge ``settings`` sinon defaut). KeyError si inconnu."""
    if nom not in DEFAUTS:
        raise KeyError('Seuil inconnu : ' + nom)
    surcharges = _surcharges()
    if nom in surcharges:
        return surcharges[nom]
    return DEFAUTS[nom]


def tous():
    """Dictionnaire complet des seuils effectifs."""
    resultat = dict(DEFAUTS)
    resultat.update(_surcharges())
    return resultat


def surcharges():
    """Seuils surcharges dans ``settings`` (nom -> valeur convertie)."""
    return dict(_surcharges())


def definir(nom, valeur):
    """Surcharge un seuil dans ``settings`` (type controle contre le defaut)."""
    if nom not in DEFAUTS:
        raise KeyError('Seuil inconnu : ' + nom)
    convertie = convertir(nom, valeur)
    noyau.srv().query(
        'INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)',
        (PREFIXE + nom, json.dumps(convertie)),
    )
    _vider_cache()
    return convertie


def reinitialiser(nom=None):
    """Supprime la surcharge d'un seuil (ou de tous si ``nom`` est None)."""
    serveur = noyau.srv()
    if nom is None:
        serveur.query('DELETE FROM settings WHERE key LIKE ?', (PREFIXE + '%',))
    else:
        serveur.query('DELETE FROM settings WHERE key=?', (PREFIXE + nom,))
    _vider_cache()


def convertir(nom, valeur):
    """Convertit ``valeur`` (texte JSON ou valeur Python) au type du defaut."""
    defaut = DEFAUTS[nom]
    if isinstance(valeur, str) and not isinstance(defaut, str):
        valeur = json.loads(valeur)
    if isinstance(defaut, bool):
        if isinstance(valeur, bool):
            return valeur
        if valeur in (0, 1):
            return bool(valeur)
        raise ValueError(f'Seuil {nom} : booleen attendu.')
    if isinstance(defaut, int):
        if isinstance(valeur, bool) or float(valeur) != int(float(valeur)):
            raise ValueError(f'Seuil {nom} : entier attendu.')
        return int(float(valeur))
    if isinstance(defaut, float):
        if isinstance(valeur, bool):
            raise ValueError(f'Seuil {nom} : nombre attendu.')
        return float(valeur)
    if isinstance(defaut, list):
        if not isinstance(valeur, (list, tuple)) or len(valeur) != len(defaut):
            raise ValueError(f'Seuil {nom} : liste de {len(defaut)} valeurs attendue.')
        liste = [float(v) for v in valeur]
        if liste != sorted(liste):
            raise ValueError(f'Seuil {nom} : bornes [bas, haut] attendues.')
        return liste
    if isinstance(defaut, str):
        if isinstance(valeur, str):
            try:
                decode = json.loads(valeur)
            except ValueError:
                decode = valeur
            if isinstance(decode, str):
                return decode
        return str(valeur)
    return valeur


def _vider_cache():
    """Oublie les surcharges lues."""
    _CACHE['base'] = None
    _CACHE['instant'] = 0.0
    _CACHE['valeurs'] = {}


def _surcharges():
    """Lit les surcharges (cache court, par chemin de base). Sans serveur : aucune."""
    if not noyau.est_branche():
        return {}
    serveur = noyau.srv()
    base = str(serveur.DB)
    if _CACHE['base'] == base and time.monotonic() - _CACHE['instant'] < _DUREE_CACHE:
        return _CACHE['valeurs']
    valeurs = {}
    try:
        lignes = serveur.query('SELECT key,value FROM settings WHERE key LIKE ?', (PREFIXE + '%',))
    except sqlite3.Error:
        lignes = []
    for ligne in lignes:
        nom = ligne['key'][len(PREFIXE):]
        if nom not in DEFAUTS:
            continue
        try:
            valeurs[nom] = convertir(nom, ligne['value'])
        except (ValueError, TypeError):
            continue
    _CACHE['base'] = base
    _CACHE['instant'] = time.monotonic()
    _CACHE['valeurs'] = valeurs
    return valeurs
