"""Export Excel par URL (M6), fichier .xlsx ecrit avec la seule bibliotheque standard (zipfile + XML).

Onglet « Synthese » : une ligne par URL (unites, taux d'obsolescence, propositions par statut).
Un onglet par URL : unites, statuts, verdicts, sources et decisions.

Taux d'obsolescence d'une page : definition unique de ``m5.taux_unites`` / ``m5.taux_obsolescence`` (la meme que
l'interface), sur la version de reference de la page (derniere version qui porte des unites).
"""
import io
import re
import zipfile
from xml.sax.saxutils import escape

from chaine import noyau

STATUTS_PROPOSITION = ('proposee', 'a_rediger', 'acceptee', 'appliquee', 'modifiee', 'rejetee', 'archive', 'reportee')
LIMITE_CELLULE = 32000
_INVALIDE = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]')

ENTETES_PAGE = ['Unité', 'Phrase', 'Position', 'Statut', 'Classe d\'obsolescence', 'Statut temporel', 'Date d\'ancrage',
                'Verdict', 'Vérifiée le', 'Critique', 'Type d\'affirmation', 'Sources', 'Proposition', 'Statut proposition',
                'Avant', 'Après', 'Texte final', 'Décision', 'Note de décision', 'Décidée le']
LARGEURS_PAGE = [18, 60, 9, 16, 16, 18, 14, 14, 20, 9, 20, 60, 18, 16, 60, 60, 60, 12, 40, 20]


def taux_obsolescence(unites):
    """Taux d'obsolescence d'une liste d'unites (dicts) ; None si aucune unite verifiable (``m5.taux_unites``)."""
    from chaine import m5
    return m5.taux_unites(unites)


def _collecter():
    """Feuilles a ecrire : liste de dicts (nom, entetes, lignes, largeurs, pourcentages)."""
    S = noyau.srv()
    proposals_colonnes = noyau.colonnes('proposals')
    decision = 'decision' if 'decision' in proposals_colonnes else "NULL"
    pages = S.query('SELECT p.id pid, p.url, p.title, v.id vid, v.created, (SELECT count(*) FROM units u WHERE u.version_id=v.id) n '
                    'FROM pages p JOIN versions v ON v.page_id=p.id ORDER BY p.url, v.created DESC, v.rowid DESC')
    reference = {}
    for ligne in pages:
        courant = reference.get(ligne['pid'])
        if courant is None or (courant['n'] == 0 and ligne['n'] > 0):
            reference[ligne['pid']] = ligne
    sources = {}
    for s in S.query("SELECT unit_id, url, domain, source_date, kind FROM sources WHERE COALESCE(url,'')<>'' ORDER BY unit_id, kind, source_date"):
        sources.setdefault(s['unit_id'], []).append(f"{s['url']} ({s['domain'] or ''}{', ' + s['source_date'] if s['source_date'] else ''}) [{s['kind']}]")
    propositions = {}
    for pr in S.query(f'SELECT pr.*, {decision} AS decision_col, v.page_id FROM proposals pr JOIN versions v ON v.id=pr.version_id ORDER BY pr.created'):
        propositions.setdefault(pr['page_id'], []).append(pr)
    synthese_lignes, feuilles, noms = [], [], set()
    for position, (pid, ligne) in enumerate(sorted(reference.items(), key=lambda x: x[1]['url'] or ''), start=1):
        unites = S.query('SELECT u.*, ct.type claim_type FROM units u LEFT JOIN claim_types ct ON ct.unit_id=u.id WHERE u.version_id=? '
                         'ORDER BY COALESCE(u.span_start,0), u.id', (ligne['vid'],))
        props = propositions.get(pid, [])
        par_unite = {}
        for pr in props:
            par_unite.setdefault(pr['unit_id'], []).append(pr)
        lignes = []
        for u in unites:
            liste = par_unite.pop(u['id'], [None])
            for pr in liste:
                lignes.append(_ligne_unite(u, pr, sources.get(u['id'], [])))
        for orphelines in par_unite.values():
            for pr in orphelines:
                lignes.append(_ligne_unite({'id': pr['unit_id'], 'text': pr['before']}, pr, []))
        comptes = {s: sum(1 for pr in props if pr['status'] == s) for s in STATUTS_PROPOSITION}
        synthese_lignes.append([ligne['url'], ligne['title'], len(unites), taux_obsolescence(unites), len(props)] + [comptes[s] for s in STATUTS_PROPOSITION])
        feuilles.append({'nom': _nom_feuille(ligne['url'], position, noms), 'entetes': ENTETES_PAGE, 'lignes': lignes, 'largeurs': LARGEURS_PAGE, 'pourcentages': []})
    entetes = ['URL', 'Titre', 'Unités', 'Taux d\'obsolescence', 'Propositions'] + [s.replace('_', ' ') for s in STATUTS_PROPOSITION]
    synthese = {'nom': 'Synthese', 'entetes': entetes, 'lignes': synthese_lignes, 'largeurs': [60, 40, 9, 14, 12] + [11] * len(STATUTS_PROPOSITION),
                'pourcentages': [3]}
    return [synthese] + feuilles


def _ligne_unite(u, pr, sources):
    return [u.get('id'), u.get('text') or u.get('quote'), u.get('span_start'), u.get('status'), u.get('obs_class'), u.get('temporal_status'),
            u.get('anchor_date'), u.get('verdict'), u.get('verified_at'), 'oui' if u.get('critical') else ('non' if u.get('critical') is not None else None),
            u.get('claim_type'), '\n'.join(sources) or None,
            pr['kind'] if pr else None, pr['status'] if pr else None, pr['before'] if pr else None, pr['after'] if pr else None,
            pr['final_text'] if pr else None, pr['decision_col'] if pr else None, pr['decision_note'] if pr else None, pr['decided_at'] if pr else None]


def _nom_feuille(url, position, deja):
    """Nom d'onglet valide (31 caracteres, sans []:*?/\\), unique, issu du chemin de l'URL."""
    chemin = re.sub(r'^[a-z]+://(www\.)?', '', url or '').strip('/')
    morceau = chemin.split('/')[-1] or chemin or 'page'
    morceau = re.sub(r'[\[\]:*?/\\]', '-', morceau)
    nom = f'{position:02d} {morceau}'[:31]
    while nom.lower() in deja or nom.lower() == 'synthese':
        nom = (nom[:27] + f'~{position}')[:31]
    deja.add(nom.lower())
    return nom


def _lettre(index):
    texte = ''
    index += 1
    while index:
        index, reste = divmod(index - 1, 26)
        texte = chr(65 + reste) + texte
    return texte


def _cellule(reference, valeur, style=0):
    if valeur is None or valeur == '':
        return ''
    marque = f' s="{style}"' if style else ''
    if isinstance(valeur, bool):
        valeur = 'oui' if valeur else 'non'
    if isinstance(valeur, (int, float)):
        return f'<c r="{reference}"{marque}><v>{valeur!r}</v></c>'
    texte = _INVALIDE.sub('', str(valeur))[:LIMITE_CELLULE]
    style_texte = marque or ' s="2"' if '\n' in texte else marque
    return f'<c r="{reference}"{style_texte} t="inlineStr"><is><t xml:space="preserve">{escape(texte)}</t></is></c>'


def _xml_feuille(feuille):
    entetes = feuille['entetes']
    lignes = [entetes] + feuille['lignes']
    colonnes = ''.join(f'<col min="{i + 1}" max="{i + 1}" width="{w}" customWidth="1"/>' for i, w in enumerate(feuille['largeurs']))
    corps = []
    for numero, ligne in enumerate(lignes, start=1):
        cellules = []
        for i, valeur in enumerate(ligne):
            style = 1 if numero == 1 else (3 if i in feuille['pourcentages'] else 0)
            cellules.append(_cellule(f'{_lettre(i)}{numero}', valeur, style))
        corps.append(f'<row r="{numero}">{"".join(cellules)}</row>')
    derniere = f'{_lettre(len(entetes) - 1)}{len(lignes)}'
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<dimension ref="A1:{derniere}"/>'
            '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            f'<cols>{colonnes}</cols><sheetData>{"".join(corps)}</sheetData>'
            f'<autoFilter ref="A1:{derniere}"/></worksheet>')


STYLES = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
          '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
          '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font></fonts>'
          '<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
          '<fill><patternFill patternType="solid"><fgColor rgb="FF305496"/><bgColor indexed="64"/></patternFill></fill></fills>'
          '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
          '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
          '<cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top"/></xf>'
          '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment vertical="center" wrapText="1"/></xf>'
          '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0" applyAlignment="1"><alignment vertical="top" wrapText="1"/></xf>'
          '<xf numFmtId="10" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/></cellXfs>'
          '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>')


def ecrire_xlsx(feuilles):
    """Classeur .xlsx (octets) a partir de ``[{nom, entetes, lignes, largeurs, pourcentages}]``."""
    types = ''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                    for i in range(1, len(feuilles) + 1))
    contenu = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
               '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
               '<Default Extension="xml" ContentType="application/xml"/>'
               '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
               '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
               + types + '</Types>')
    racine = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
              '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
    classeur = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'
                + ''.join(f'<sheet name="{escape(f["nom"], {chr(34): "&quot;"})}" sheetId="{i}" r:id="rId{i}"/>' for i, f in enumerate(feuilles, start=1))
                + '</sheets></workbook>')
    liens = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             + ''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>'
                       for i in range(1, len(feuilles) + 1))
             + f'<Relationship Id="rId{len(feuilles) + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
    tampon = io.BytesIO()
    with zipfile.ZipFile(tampon, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', contenu)
        archive.writestr('_rels/.rels', racine)
        archive.writestr('xl/workbook.xml', classeur)
        archive.writestr('xl/_rels/workbook.xml.rels', liens)
        archive.writestr('xl/styles.xml', STYLES)
        for i, feuille in enumerate(feuilles, start=1):
            archive.writestr(f'xl/worksheets/sheet{i}.xml', _xml_feuille(feuille))
    return tampon.getvalue()


def excel():
    """Export Excel (octets .xlsx) : onglet de synthese + un onglet par URL, avec les decisions de revue."""
    return ecrire_xlsx(_collecter())
