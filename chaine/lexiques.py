"""Lexiques francais de la methodologie M2 (sources et typologie des affirmations).

Tous les motifs s'appliquent a un texte « a plat » : minuscules, sans accents,
apostrophes et guillemets normalises, de MEME LONGUEUR que le texte d'origine
(voir ``plat`` ci-dessous), si bien que les offsets d'un motif valent aussi
pour le texte d'origine. Les motifs sont donc ecrits sans accents.

Les poids (0 a 1) sont des points de depart a recalibrer sur le jeu de
reference du protocole d'evaluation. Aucune dependance.
"""
import re
import unicodedata

# ---------------------------------------------------------------------------
# Texte a plat (longueur conservee)
# ---------------------------------------------------------------------------

_TABLE = {}


def _caractere_plat(caractere):
    """Minuscule sans accent d'un caractere, en un seul caractere."""
    resultat = _TABLE.get(caractere)
    if resultat is not None:
        return resultat
    if caractere in '’‘`´':
        resultat = "'"
    elif caractere in '«»“”„':
        resultat = '"'
    elif caractere in '   \r':
        resultat = ' '
    else:
        decompose = unicodedata.normalize('NFD', caractere)
        base = decompose[0] if decompose else caractere
        minuscule = base.lower()
        resultat = minuscule if len(minuscule) == 1 else base
    _TABLE[caractere] = resultat
    return resultat


def plat(texte):
    """Minuscules sans diacritiques, apostrophes droites ; meme longueur que ``texte``."""
    return ''.join(_caractere_plat(c) for c in (texte or ''))


# ---------------------------------------------------------------------------
# Etape 1 : attributions textuelles
# ---------------------------------------------------------------------------

# Marqueurs « prepositionnels » : la source nommee SUIT le marqueur.
ATTRIBUTION_AVANT = re.compile(
    r"\b(?:"
    r"selon|d'apres|"
    r"(?:dans|par|via)\s+(?:un|une|le|son|leur)\s+(?:communique|rapport|bulletin|blog|billet|article|document|etude)(?:\s+de\s+presse)?(?:\s+(?:de|du|des|d'|publie\s+par|diffuse\s+par))?|"
    r"(?:une|l'|cette|la|son|leur)\s*(?:etude|enquete|analyse|note|publication|synthese|mesure|experience)\s+(?:de|du|des|d'|menee\s+par|realisee\s+par|publiee\s+par|conduite\s+par)|"
    r"(?:un|le|ce|son|leur)\s+(?:rapport|sondage|barometre|bilan|livre\s+blanc|brevet|communique|document|billet|article)\s+(?:de|du|des|d'|publie\s+par|redige\s+par|emis\s+par)|"
    r"les\s+(?:chiffres|donnees|statistiques|resultats|estimations)\s+(?:de|du|des|d')"
    r")",
)

# Marqueurs verbaux : la source nommee PRECEDE (sujet) ou SUIT immediatement (inversion).
# Formes strictes : auxiliaire + participe, ou present / imparfait de 3e personne.
ATTRIBUTION_VERBE = re.compile(
    r"\b(?:"
    r"(?:a|ont|avait|avaient|aurait|auraient)\s+(?:indique|annonce|rapporte|precise|declare|affirme|confirme|explique|publie|"
    r"revele|souligne|assure|estime|detaille|rappele|ajoute|note|mesure|observe|ecrit|decrit|admis|reconnu|conclu|constate|"
    r"averti|recommande|communique|signale|montre|demontre|calcule|evalue|releve|etabli)|"
    r"(?:indique|indiquent|annonce|annoncent|rapporte|rapportent|precise|precisent|declare|declarent|affirme|affirment|confirme|confirment|"
    r"explique|expliquent|publie|publient|revele|revelent|souligne|soulignent|assure|assurent|estime|estiment|detaille|rappelle|"
    r"rappellent|ajoute|ajoutent|ecrit|ecrivent|decrit|decrivent|admet|admettent|reconnait|reconnaissent|conclut|concluent|"
    r"constate|constatent|avertit|avertissent|recommande|recommandent|communique|signale|signalent|montre|montrent|"
    r"annoncait|annoncaient|indiquait|indiquaient|expliquait|expliquaient|precisait|precisaient|declarait|declaraient|"
    r"affirmait|affirmaient|rapportait|rapportaient|ecrivait|ecrivaient|decrivait|decrivaient|soulignait|soulignaient|"
    r"publiait|publiaient|confirmait|confirmaient|estimait|estimaient|revelait|revelaient|mesurait|mesuraient)"
    r")\b",
)

# Types d'entites (moteurs.entites) acceptes comme source nommee.
TYPES_SOURCE_NOMMEE = ('organisation', 'personne', 'nom_propre', 'divers', 'collectivite', 'loi', 'institution')

# Mots qui, juste avant « d'apres », en font un adverbe de temps (« l'annee d'apres ») et non une attribution.
D_APRES_ADVERBIAL = re.compile(r"(?:annee|semaine|jour|mois|lendemain|nuit|siecle|an|celui|celle|ceux|celles|trimestre|decennie)\s+$")
# Forme admise pour le nom d'une source (ecarte les fragments de NER comme « Bing : Prometheus « »).
NOM_SOURCE = re.compile(r"^[A-Z\u00c0-\u00de0-9][\w\u00c0-\u017f &'\u2019.\-]*$")

# Determinants et liaisons admis entre un verbe d'attribution et l'entite qui le suit (« a indique l'Insee »).
ECART_ENTITE = re.compile(r"^[\s,]*(?:(?:le|la|les|l'|un|une|a|au|aux|par|de|d'|du|des|chez)\s*)*$")

# ---------------------------------------------------------------------------
# Etape 1 : appareil de citation
# ---------------------------------------------------------------------------

TITRES_BIBLIOGRAPHIE = re.compile(
    r"^\s*(?:#{1,6}\s*|==\s*)?(?:sources?|references?|bibliographie|notes?|notes\s+et\s+references|sources\s+et\s+references|"
    r"pour\s+aller\s+plus\s+loin|ressources)\s*:?\s*$",
)
ENTREE_NUMEROTEE = re.compile(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})[.)])\s+(\S.*)$")
ENTREE_PUCE = re.compile(r"^\s*[-*•–]\s+(\S.*)$")
LEGENDE = re.compile(
    r"^\s*(?:figure|fig\.|graphique|tableau|illustration|schema|capture(?:\s+d'ecran)?|encadre|infographie|carte|photo)\s*\d*\s*[:.\-–—]\s*\S",
)
LIGNE_SOURCE = re.compile(r"^\s*(?:sources?|credits?|credit\s+photo|d'apres|adapte\s+de|realise\s+d'apres)\s*:\s*(\S.*)$")
# Etiquettes d'un marqueur « [Confirme : Google, JS SEO basics] » (etiquette epistemique + source).
ETIQUETTES_EPISTEMIQUES = (
    'confirme', 'documente', 'standard', 'hypothese', 'observe', 'mesure', 'estime', 'rapporte', 'declare',
    'interpretation', 'attribue', 'reconstitue', 'non documente', 'source', 'sources', 'cf', 'voir',
)
MARQUEUR_CROCHETS = re.compile(r"\[\s*([^\]\[:\n]{2,24}?)\s*(?::\s*([^\]\[\n]{2,240}))?\]")
NOTE_NUMERIQUE = re.compile(r"\[(\d{1,3}(?:\s*[,;–\-]\s*\d{1,3})*)\]")
NOTE_EXPOSANT = re.compile(r"(?<=[A-Za-zÀ-ſ.,;:!?)»\"'])([¹²³⁰-⁹]{1,3})")
EXPOSANTS = {'⁰': '0', '¹': '1', '²': '2', '³': '3', '⁴': '4', '⁵': '5', '⁶': '6',
             '⁷': '7', '⁸': '8', '⁹': '9'}

URL = re.compile(r'https?://[^\s<>"«»“”]+')
PONCTUATION_URL = '.,;:!?)]}”’\'"»>'

# ---------------------------------------------------------------------------
# Etape 2 : types de page (rubrique, fil d'Ariane, schema.org)
# ---------------------------------------------------------------------------

# type de page normalise -> famille : 'auteur' (l'auteur fait foi) ou 'opinion'.
FAMILLES_PAGE = {
    'etude': 'auteur', 'enquete': 'auteur', 'analyse': 'auteur', 'decision': 'auteur', 'expertise': 'auteur',
    'rapport': 'auteur', 'recherche': 'auteur', 'livre_blanc': 'auteur', 'benchmark': 'auteur',
    'tribune': 'opinion', 'edito': 'opinion', 'editorial': 'opinion', 'opinion': 'opinion', 'chronique': 'opinion',
    'billet': 'opinion', 'point_de_vue': 'opinion',
    'actualite': None, 'reference': None, 'autre': None,
}

# schema.org @type -> type de page normalise.
SCHEMA_ORG = {
    'scholarlyarticle': 'etude', 'report': 'rapport', 'medicalscholarlyarticle': 'etude', 'analysisnewsarticle': 'analyse',
    'opinionnewsarticle': 'tribune', 'reviewnewsarticle': 'opinion', 'review': 'opinion', 'newsarticle': 'actualite',
    'reportagenewsarticle': 'actualite', 'article': None, 'blogposting': None, 'techarticle': None,
}

# Mots-cles de rubrique / fil d'Ariane / chemin d'URL (normalises) -> type de page.
RUBRIQUES = {
    'etude': 'etude', 'etudes': 'etude', 'enquete': 'enquete', 'enquetes': 'enquete', 'analyse': 'analyse',
    'analyses': 'analyse', 'rapport': 'rapport', 'rapports': 'rapport', 'decision': 'decision', 'decisions': 'decision',
    'jurisprudence': 'decision', 'expertise': 'expertise', 'recherche': 'recherche', 'livre-blanc': 'livre_blanc',
    'livres-blancs': 'livre_blanc', 'benchmark': 'benchmark', 'tribune': 'tribune', 'tribunes': 'tribune',
    'edito': 'edito', 'editos': 'edito', 'editorial': 'editorial', 'opinion': 'opinion', 'opinions': 'opinion',
    'chronique': 'chronique', 'chroniques': 'chronique', 'billet': 'billet', 'billets': 'billet',
    'point-de-vue': 'point_de_vue', 'points-de-vue': 'point_de_vue', 'actualite': 'actualite', 'actualites': 'actualite',
    'actu': 'actualite', 'news': 'actualite',
}

# Etiquettes et exemples de depart du classifieur de type de page (titre + chapo).
ETIQUETTES_PAGE = ('etude', 'analyse', 'tribune', 'actualite', 'reference')
EXEMPLES_PAGE = {
    'etude': [
        "Enquete 2026 : resultats d'une etude aupres de 1 200 professionnels",
        "Rapport annuel : methodologie, echantillon et principaux resultats de l'etude",
        "Etude de cas : mesures realisees sur un panel de sites et conclusions de l'enquete",
        "Decision de la cour : motifs, dispositif et portee de l'arret",
    ],
    'analyse': [
        "Analyse du mecanisme : comment il fonctionne en detail et ce qu'il implique",
        "Decryptage : ce que la fuite revele du fonctionnement interne du systeme",
        "Anatomie d'un algorithme : composants, signaux et limites connues",
        "Expertise technique : diagnostic, causes et recommandations",
    ],
    'tribune': [
        "Tribune : pourquoi il faut en finir avec cette pratique",
        "Edito : ce que nous devons exiger des moteurs de recherche",
        "Mon avis : ce virage est une erreur et voici pourquoi",
        "Point de vue : l'avenir du referencement ne passera pas par la technique seule",
    ],
    'actualite': [
        "Google annonce une mise a jour majeure de son algorithme",
        "Le ministere publie un communique sur la nouvelle reglementation",
        "Lancement : l'entreprise devoile son nouveau produit cette semaine",
        "Breve : la societe annonce une levee de fonds de 20 millions d'euros",
    ],
    'reference': [
        "Glossaire : definition des termes du referencement naturel",
        "Qu'est-ce qu'un fichier robots.txt ? Definition et usages",
        "Guide pratique : les bases du protocole HTTP expliquees simplement",
        "Documentation : syntaxe, parametres et exemples d'utilisation",
    ],
}

# ---------------------------------------------------------------------------
# Etape 2, regle 1 : information source (l'auteur fait foi)
# ---------------------------------------------------------------------------

_OBJETS_AUTEUR = (
    r"enquete|etude|sondage|analyse|mesures?|corpus|echantillon|panel|experience|experimentation|tests?|donnees|calculs?|"
    r"benchmark|releves?|audit|observations?|resultats|chiffres|statistiques|crawl|base\s+de\s+donnees|jeu\s+de\s+donnees|"
    r"estimations?|simulations?|protocole|methode|methodologie"
)
_VERBES_AUTEUR = (
    r"interroge|mesure|analyse|teste|compare|observe|collecte|calcule|releve|audite|enquete|extrait|crawle|echantillonne|"
    r"recense|compte|verifie|constate|evalue|realise|conduit|mene|constitue|simule|reproduit|classe|annote|croise|rapproche"
)
PREMIERE_PERSONNE_METHODO = re.compile(
    r"\b(?:"
    r"(?:notre|nos)\s+(?:" + _OBJETS_AUTEUR + r")\b|"
    r"nous\s+(?:avons|ont|allons|venons\s+de)\s+(?:donc\s+|ensuite\s+|aussi\s+|egalement\s+)?(?:" + _VERBES_AUTEUR + r")\w*|"
    r"(?:j'ai|j'avais|je\s+(?:viens\s+de|vais))\s+(?:donc\s+|ensuite\s+|aussi\s+)?(?:" + _VERBES_AUTEUR + r")\w*|"
    r"(?:dans|sur|a\s+partir\s+de|parmi)\s+(?:notre|nos|mon|mes)\s+(?:echantillon|corpus|panel|jeu|base|donnees|mesures|tests?|releves?)|"
    r"selon\s+(?:nos|mes|notre|mon)\s+(?:" + _OBJETS_AUTEUR + r")\b|"
    r"(?:nous|on)\s+(?:avons|a)\s+(?:interroge|mesure|teste)\w*"
    r")",
)
EXPERT_AUTEUR = re.compile(r"\b(?:en\s+tant\s+qu'(?:expert|auditeur|consultant|praticien)|dans\s+ma\s+pratique|mon\s+experience\s+de)\b")

# ---------------------------------------------------------------------------
# Etape 2, regle 2 : opinion
# ---------------------------------------------------------------------------

# (motif, poids). Les poids se combinent en 1 - produit(1 - poids). Une modalite deontique seule (« il faut »,
# 0,50) ne suffit pas : en contenu technique elle enonce souvent une regle verifiable.
INDICES_OPINION = [
    # Premiere personne d'avis
    (re.compile(r"\b(?:a\s+mon\s+avis|selon\s+moi|a\s+notre\s+avis|d'apres\s+moi|pour\s+ma\s+part|personnellement|de\s+mon\s+point\s+de\s+vue)\b"), 0.80),
    (re.compile(r"\b(?:je|nous|j')\s*(?:pense|penses|croi[ts]|croyons|trouve|trouvons|estime|estimons|considere|considerons|juge|jugeons|prefere|preferons|deplore|deplorons|regrette|regrettons|recommande|recommandons|conseille|conseillons|souhaite|souhaitons|doute|doutons|suppose|supposons)\b"), 0.75),
    (re.compile(r"\bil\s+(?:me|nous)\s+semble\b"), 0.70),
    # Modalites deontiques et evaluatives
    (re.compile(r"\bil\s+(?:faut|faudra|faudrait|vaut\s+mieux|vaudrait\s+mieux|convient\s+de|conviendrait|est\s+(?:souhaitable|preferable|recommande|indispensable|essentiel|crucial|dangereux|regrettable|dommage|temps\s+de|necessaire\s+de|inutile\s+de|illusoire\s+de))\b"), 0.50),
    (re.compile(r"\b(?:mieux\s+vaut|on\s+(?:devrait|ne\s+devrait\s+pas|aurait\s+du)|nous\s+(?:devrions|aurions\s+du)|il\s+ne\s+faut\s+pas)\b"), 0.50),
    (re.compile(r"\b(?:devrait|devraient|devrions|devriez|devrais|aurait\s+du|auraient\s+du)\b"), 0.40),
    # Conditionnel de souhait
    (re.compile(r"\b(?:j'aimerais|nous\s+aimerions|on\s+aimerait|je\s+voudrais|nous\s+voudrions|on\s+voudrait|je\s+souhaiterais|nous\s+souhaiterions|on\s+souhaiterait|il\s+serait\s+(?:bien|mieux|souhaitable|preferable|judicieux|sage|temps)|ce\s+serait\s+(?:bien|mieux|dommage|souhaitable))\b"), 0.75),
    # Adverbes d'attitude
    (re.compile(r"\b(?:malheureusement|heureusement|franchement|clairement\s+(?:mal|mieux)|bizarrement|curieusement|etrangement|helas|sans\s+surprise|a\s+juste\s+titre|a\s+tort|a\s+raison|evidemment\s+(?:pas|mal)|de\s+facon\s+(?:scandaleuse|absurde))\b"), 0.55),
    # Adjectifs appreciatifs
    (re.compile(r"\b(?:excellent(?:e|es|s)?|remarquable(?:s)?|catastrophique(?:s)?|absurde(?:s)?|inquietant(?:e|es|s)?|decevant(?:e|es|s)?|formidable(?:s)?|desastreux|desastreuse(?:s)?|magnifique(?:s)?|deplorable(?:s)?|scandaleu(?:x|se|ses)|ridicule(?:s)?|genial(?:e|es|aux)?|abominable(?:s)?|admirable(?:s)?|affligeant(?:e|es|s)?|lamentable(?:s)?|detestable(?:s)?|merveilleu(?:x|se|ses)|pitoyable(?:s)?|insupportable(?:s)?|brillant(?:e|es|s)?|mediocre(?:s)?|nul(?:le|les|s)?\s+et|malhonnete(?:s)?|honteu(?:x|se|ses)|navrant(?:e|es|s)?|rassurant(?:e|es|s)?|stupide(?:s)?|criticable(?:s)?|salutaire(?:s)?|benefique(?:s)?|regrettable(?:s)?)\b"), 0.50),
    (re.compile(r"\b(?:le\s+meilleur|la\s+meilleure|les\s+meilleur(?:e)?s|le\s+pire|la\s+pire|les\s+pires|bien\s+meilleur(?:e|s|es)?|bien\s+pire|beaucoup\s+mieux|nettement\s+mieux|bien\s+trop|vraiment\s+(?:dommage|mauvais|bon|utile|inutile))\b"), 0.35),
    (re.compile(r"\b(?:trop\s+(?:de|peu|souvent|compliqu\w+|cher|lent|long)|tellement\s+(?:mieux|pire)|a\s+mon\s+sens|j'ai\s+l'impression)\b"), 0.35),
    (re.compile(r"\b(?:pour\s+moi|quel(?:le)?s?\s+\w+\s+!|\bquelle\s+\w+\s+!)\b"), 0.40),
    (re.compile(r"!\s*$"), 0.30),
]

# ---------------------------------------------------------------------------
# Etape 2, regle 3 : interpretation (connecteurs inferentiels)
# ---------------------------------------------------------------------------

CONNECTEURS_INFERENTIELS = [
    (re.compile(r"\b(?:ce\s+qui\s+(?:montre|prouve|demontre|explique|implique|suggere|indique|signifie|laisse\s+penser|revele|confirme|permet\s+de\s+(?:penser|conclure|croire)|veut\s+dire|revient\s+a)|cela\s+(?:montre|prouve|demontre|explique|implique|suggere|indique|signifie|veut\s+dire|revient\s+a|laisse\s+penser|s'explique|confirme)|ceci\s+(?:montre|prouve|signifie|explique|implique|suggere)|ce\s+(?:qui|que)\s+cela\s+(?:signifie|implique))\b"), 0.75),
    (re.compile(r"\b(?:on\s+(?:peut|pourrait|doit)\s+(?:en\s+)?(?:deduire|conclure|inferer|penser|supposer)|on\s+en\s+(?:deduit|conclut|infere)|il\s+(?:en\s+)?(?:resulte|s'ensuit|decoule|en\s+decoule)|tout\s+laisse\s+(?:penser|croire|supposer)|on\s+peut\s+donc|il\s+faut\s+en\s+(?:deduire|conclure))\b"), 0.75),
    (re.compile(r"\b(?:donc|par\s+consequent|en\s+consequence|de\s+ce\s+fait|c'est\s+pourquoi|c'est\s+ce\s+qui\s+explique|par\s+suite|autrement\s+dit|en\s+d'autres\s+termes|d'ou\b)"), 0.65),
    (re.compile(r"\b(?:ainsi\s*,|en\s+definitive|au\s+final|en\s+somme|en\s+resume|en\s+conclusion|en\s+bref)\b"), 0.40),
    (re.compile(r"\[\s*hypothese\s*(?::[^\]]*)?\]"), 0.62),
    (re.compile(r"\b(?:probablement|vraisemblablement|sans\s+doute|il\s+est\s+probable\s+que|il\s+est\s+plausible\s+que|on\s+peut\s+penser\s+que|cela\s+laisse\s+supposer)\b"), 0.45),
]

# ---------------------------------------------------------------------------
# Etape 2, regle 4 : connaissance generale
# ---------------------------------------------------------------------------

# Debut de phrase a sujet generique : article defini/indefini + nom commun en minuscule.
# Sujets « meta » : la phrase parle d'un document, pas d'un fait general (« Le texte indique que... »).
SUJET_META = re.compile(
    r"^\s*(?:(?:le|la|les|un|une|ce|cette|ces)\s+|l')(?:texte|textes|article|articles|brevet|brevets|document|documents|tableau|"
    r"documentation|etude|etudes|rapport|rapports|fuite|enquete|enquetes|source|sources|auteur|billet|page\s+de\s+l'editeur|"
    r"publication|synthese|test|tests|schema|schemas)\b"
)
# Identifiants techniques (camelCase, nom.extension, snake_case) : entite specifique meme sans majuscule initiale.
IDENTIFIANT_TECHNIQUE = re.compile(r"[a-z][A-Z]|\b\w+\.(?:txt|xml|json|html|js|css|php|pdf)\b|\w_\w|\w/\w")
SUJET_GENERIQUE = re.compile(r"^\s*(?:(?:le|la|les|un|une|des|tout|toute|tous|toutes|chaque)\s+|l')[a-z][a-z-]+")
MARQUEURS_GNOMIQUES = re.compile(
    r"\b(?:en\s+general|generalement|toujours|souvent|par\s+definition|habituellement|naturellement|typiquement|"
    r"est\s+(?:un|une|le|la)\s|sont\s+(?:des|les)\s|permet\s+de|permettent\s+de|sert\s+a|servent\s+a|consiste\s+a|"
    r"est\s+compose|sont\s+composes|se\s+compose\s+de|designe|correspond\s+a|constitue)\b"
)
MARQUEURS_ETAT_COURANT = re.compile(
    r"\b(?:actuellement|desormais|aujourd'hui|en\s+ce\s+moment|recemment|depuis|dorenavant|maintenant|cette\s+annee|ce\s+mois|"
    r"derniere?s?|nouveau|nouvelle|nouveaux|nouvelles|a\s+ce\s+jour|en\s+cours|prochain(?:e|s|es)?|bientot|deja|encore|toujours\s+en|"
    r"a\s+venir|le\s+plus\s+(?:recent|utilise|grand|gros)|numero\s+un|leader)\b"
)
TEMPS_NON_GNOMIQUES = re.compile(
    r"\b(?:a\s+(?:ete|etabli|fait|publie|annonce)|ont\s+(?:ete|fait|publie)|avait|avaient|etait|etaient|fut|furent|sera|seront|serait|"
    r"viendra|annoncera|publiera|lancera|devrait|aura|auront|avons|ai\b|ete\b)\b"
)
CHIFFRE = re.compile(r"\d")
ANNEE = re.compile(r"\b(?:19|20)\d{2}\b")
MOIS = re.compile(
    r"\b(?:janvier|fevrier|mars|avril|mai|juin|juillet|aout|septembre|octobre|novembre|decembre|janv|fevr|avr|juil|sept|oct|nov|dec)\b"
)
QUESTION_FERMEE = (
    "Cette phrase enonce-t-elle un fait general, stable et largement connu, independant de toute date ?"
)

# ---------------------------------------------------------------------------
# Subjectivite : etiquettes et exemples de depart (SetFit affine sur une centaine de phrases en cible)
# ---------------------------------------------------------------------------

ETIQUETTES_SUBJECTIVITE = ('subjectif', 'objectif')
EXEMPLES_SUBJECTIVITE = {
    'subjectif': [
        "C'est une decision regrettable qui montre le peu de cas fait des utilisateurs.",
        "Je trouve cette approche tres decevante et franchement mal pensee.",
        "Il faudrait revoir entierement la facon dont ces pages sont structurees.",
        "Cette mise a jour est une excellente nouvelle pour les petits editeurs.",
        "On aimerait que les moteurs soient enfin plus transparents sur ces criteres.",
        "A mon avis, la meilleure strategie reste de ne rien changer.",
        "Le resultat est catastrophique et le choix de cette methode absurde.",
        "Il vaut mieux eviter ce type de montage, qui est vraiment inquietant.",
    ],
    'objectif': [
        "L'entreprise a ete fondee en 1998 et compte 250 salaries.",
        "Le fichier robots.txt est lu avant le telechargement des pages.",
        "Le taux de chomage s'est etabli a 7,4 % au deuxieme trimestre.",
        "Le brevet decrit un procede de classement fonde sur les clics.",
        "La page renvoie un code HTTP 301 vers l'adresse canonique.",
        "L'etude porte sur un echantillon de 1 200 sites.",
        "Le robot telecharge la page puis extrait les liens qu'elle contient.",
        "La documentation indique que le delai moyen est de dix secondes.",
    ],
}
