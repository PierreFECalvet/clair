"""Schema SQLite de la chaine (contrat section 4).

``init()`` est idempotent : il cree les tables nouvelles, etend les tables
existantes par ALTER TABLE (jamais de recreation) et pose les index.
"""
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from chaine import noyau

# Colonnes ajoutees aux tables existantes du serveur.
EXTENSIONS = {
    'versions': [
        ('author', 'TEXT'), ('updated', 'TEXT'), ('simhash', 'TEXT'), ('page_type', 'TEXT'),
        ('page_type_engine', 'TEXT'), ('xml', 'TEXT'), ('meta', 'TEXT'), ('lede', 'TEXT'),
    ],
    'chunks': [
        ('section', 'TEXT'), ('end', 'INTEGER'), ('context', 'TEXT'), ('kind', 'TEXT'),
    ],
    'evidence': [
        ('query_id', 'TEXT'), ('group_id', 'TEXT'), ('call_id', 'TEXT'), ('domain', 'TEXT'),
        ('page_date', 'TEXT'), ('passage_start', 'INTEGER'), ('relevance', 'REAL'), ('kept', 'INTEGER'),
        ('reject_reason', 'TEXT'), ('nli', 'TEXT'), ('nli_score', 'REAL'), ('weight', 'REAL'), ('run_id', 'TEXT'),
    ],
    'api_log': [
        ('step', 'TEXT'),
    ],
}

# Colonnes ajoutees aux tables nouvelles de la chaine (bases creees avant leur introduction).
# ``units.*_run`` : execution (step_runs.id) qui a ecrit en dernier status, temporal_status, critical, verdict ;
# ``step_runs.params`` : parametres de l'execution (date du jour, budget...) pour la rejouer.
EXTENSIONS_CHAINE = {
    'units': [('status_run', 'TEXT'), ('temporal_run', 'TEXT'), ('critical_run', 'TEXT'), ('verdict_run', 'TEXT'),
              ('status_suivi', 'TEXT')],   # statut suivi par une unite « dependante » (le plus grave de ses dependances, M4)
    'step_runs': [('params', 'TEXT')],
}

# Tables existantes minimales, au cas ou init() serait appele sur une base vide
# sans server.init() (memes definitions que server.init).
TABLES_SERVEUR = '''
CREATE TABLE IF NOT EXISTS pages(id TEXT PRIMARY KEY,url TEXT UNIQUE,title TEXT);
CREATE TABLE IF NOT EXISTS versions(id TEXT PRIMARY KEY,page_id TEXT,text TEXT,created TEXT,published TEXT,hash TEXT,prepared INTEGER DEFAULT 0,vector TEXT);
CREATE TABLE IF NOT EXISTS chunks(id TEXT PRIMARY KEY,version_id TEXT,position INTEGER,start INTEGER,text TEXT,prepared INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS evidence(id TEXT PRIMARY KEY,claim_id TEXT,url TEXT,title TEXT,text TEXT,external_chunk TEXT,created TEXT);
CREATE TABLE IF NOT EXISTS api_log(id TEXT PRIMARY KEY,created TEXT,service TEXT,endpoint TEXT,operation TEXT,claim_id TEXT,model TEXT,status TEXT,http_status INTEGER,duration_ms INTEGER,units INTEGER DEFAULT 0,tokens_in INTEGER,tokens_out INTEGER,request TEXT,response TEXT,error TEXT,call_id TEXT);
'''

TABLES = '''
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS step_runs(id TEXT PRIMARY KEY, step TEXT, started TEXT, ended TEXT, status TEXT, engines TEXT,
    prompt TEXT, thresholds TEXT, pages INTEGER, items INTEGER, local_seconds REAL, tokens_in INTEGER, tokens_out INTEGER,
    ibou_units INTEGER, note TEXT);
CREATE TABLE IF NOT EXISTS units(id TEXT PRIMARY KEY, version_id TEXT, chunk_id TEXT, text TEXT, quote TEXT,
    span_start INTEGER, span_end INTEGER,
    anchor_date TEXT, anchor_kind TEXT,
    expires TEXT,
    obs_class TEXT, obs_rule TEXT,
    sourcing TEXT, sourcing_conf REAL, sourcing_rule TEXT,
    source_manquante INTEGER DEFAULT 0,
    temporal_status TEXT DEFAULT 'a_jour',
    status TEXT,
    verdict TEXT, verified_at TEXT,
    centrality REAL, critical INTEGER, critical_score REAL,
    extractor TEXT, nli_score REAL, vector TEXT, legacy_claim_id TEXT, excluded INTEGER DEFAULT 0, run_id TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS entities(id TEXT PRIMARY KEY, name TEXT, type TEXT, aliases TEXT, external_id TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS unit_entities(unit_id TEXT, entity_id TEXT, "start" INTEGER, "end" INTEGER, role TEXT,
    PRIMARY KEY(unit_id, entity_id, "start"));
CREATE TABLE IF NOT EXISTS facts(id TEXT PRIMARY KEY, unit_id TEXT, subject_id TEXT, subject TEXT, predicate TEXT, object TEXT,
    object_norm TEXT, object_type TEXT,
    scope TEXT, period TEXT, origin TEXT,
    span_start INTEGER, span_end INTEGER, verbalization TEXT, nli_score REAL, status TEXT, replaced_by TEXT, run_id TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS hypotheses(id TEXT PRIMARY KEY, unit_id TEXT, kind TEXT,
    text TEXT, subject TEXT, predicate TEXT, object TEXT, reason TEXT, resolved_fact_id TEXT, run_id TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS topics(id TEXT PRIMARY KEY, label TEXT, method TEXT, centroid TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS topic_membership(topic_id TEXT, target_kind TEXT, target_id TEXT, degree REAL,
    PRIMARY KEY(topic_id, target_kind, target_id));
CREATE TABLE IF NOT EXISTS unit_groups(id TEXT PRIMARY KEY, frame TEXT, label TEXT, legacy_claim_id TEXT, run_id TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS unit_group_members(group_id TEXT, unit_id TEXT, PRIMARY KEY(group_id, unit_id));
CREATE TABLE IF NOT EXISTS unit_relations(a TEXT, b TEXT, relation TEXT,
    direction TEXT, nli_ab REAL, nli_ba REAL, engine TEXT, reason TEXT, run_id TEXT, created TEXT, PRIMARY KEY(a, b));
CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, unit_id TEXT, url TEXT, domain TEXT, cited_entity TEXT,
    kind TEXT,
    scope TEXT,
    confidence REAL, inherited_from TEXT, contribution TEXT, source_date TEXT, passage TEXT, run_id TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS claim_types(unit_id TEXT PRIMARY KEY, type TEXT,
    confidence REAL, rule TEXT, depends_on TEXT, run_id TEXT);
CREATE TABLE IF NOT EXISTS paternity(group_id TEXT PRIMARY KEY, parent_unit_id TEXT, criterion TEXT,
    detail TEXT, human INTEGER DEFAULT 0, run_id TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS link_recommendations(id TEXT PRIMARY KEY, unit_id TEXT, from_version TEXT, to_version TEXT,
    anchor TEXT, reason TEXT, status TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS queries(id TEXT PRIMARY KEY, group_id TEXT, text TEXT, kind TEXT,
    template TEXT, frame TEXT, priority REAL, critical INTEGER, humanity REAL, checks TEXT, status TEXT,
    attempts INTEGER DEFAULT 0, edited INTEGER DEFAULT 0, run_id TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS query_units(query_id TEXT, unit_id TEXT, PRIMARY KEY(query_id, unit_id));
CREATE TABLE IF NOT EXISTS ibou_calls(id TEXT PRIMARY KEY, query_id TEXT, endpoint TEXT, query TEXT, fingerprint TEXT,
    max_results INTEGER, status TEXT, http_status INTEGER,
    units INTEGER, units_remaining INTEGER, response TEXT, headers TEXT, duration_ms INTEGER, cached_from TEXT,
    legacy_call_id TEXT, created TEXT);
CREATE TABLE IF NOT EXISTS proposals(id TEXT PRIMARY KEY, unit_id TEXT, version_id TEXT, kind TEXT,
    "before" TEXT, "after" TEXT, paragraph TEXT, facts_changed TEXT, facts_kept TEXT, sources TEXT, method TEXT,
    checks TEXT, diff_ratio REAL, criticality REAL,
    status TEXT,
    decision_note TEXT, decided_at TEXT, final_text TEXT, legacy_patch_id TEXT, run_id TEXT, created TEXT);
'''

INDEX = [
    ('units', 'version_id'), ('units', 'chunk_id'), ('units', 'legacy_claim_id'), ('units', 'status'),
    ('units', 'temporal_status'), ('units', 'obs_class'), ('units', 'run_id'),
    ('unit_entities', 'entity_id'),
    ('entities', 'name'),
    ('facts', 'unit_id'), ('facts', 'subject_id'), ('facts', 'predicate'), ('facts', 'replaced_by'),
    ('hypotheses', 'unit_id'), ('hypotheses', 'resolved_fact_id'),
    ('topic_membership', 'target_kind, target_id'),
    ('unit_groups', 'legacy_claim_id'),
    ('unit_group_members', 'unit_id'),
    ('unit_relations', 'b'), ('unit_relations', 'relation'),
    ('sources', 'unit_id'), ('sources', 'inherited_from'), ('sources', 'domain'),
    ('claim_types', 'type'),
    ('paternity', 'parent_unit_id'),
    ('link_recommendations', 'unit_id'),
    ('queries', 'group_id'), ('queries', 'status'),
    ('query_units', 'unit_id'),
    ('ibou_calls', 'query_id'), ('ibou_calls', 'fingerprint'), ('ibou_calls', 'legacy_call_id'),
    ('proposals', 'unit_id'), ('proposals', 'version_id'), ('proposals', 'status'), ('proposals', 'legacy_patch_id'),
    ('evidence', 'query_id'), ('evidence', 'group_id'), ('evidence', 'call_id'),
    ('api_log', 'step'),
    ('step_runs', 'step'),
    ('versions', 'page_id'),
]

_IDENTIFIANT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')
_TYPES = {'TEXT', 'INTEGER', 'REAL', 'BLOB', 'NUMERIC'}


def init(db_path=None):
    """Cree ou etend le schema de la chaine. Idempotent.

    ``db_path`` : chemin de la base ; par defaut celle du serveur branche
    (``S.DB``, sous ``S.LOCK``). Renvoie la liste des modifications faites
    (``table.colonne`` ajoutees, tables creees), vide si tout existait deja.
    """
    chemin, verrou = _cible(db_path)
    Path(chemin).parent.mkdir(parents=True, exist_ok=True)
    with verrou:
        with closing(sqlite3.connect(chemin, timeout=30)) as base:
            with base:
                avant = _noms_tables(base)
                base.executescript(TABLES_SERVEUR)
                base.executescript(TABLES)
                creees = sorted(_noms_tables(base) - avant)
                modifications = ['table ' + nom for nom in creees]
                for table, liste in list(EXTENSIONS.items()) + list(EXTENSIONS_CHAINE.items()):
                    for nom, type_sql in liste:
                        if _ajouter_colonne(base, table, nom, type_sql):
                            modifications.append(table + '.' + nom)
                for table, cols in INDEX:
                    nom_index = 'idx_ch_' + table + '_' + cols.replace(', ', '_')
                    base.execute(f'CREATE INDEX IF NOT EXISTS {nom_index} ON {table}({_quoter_liste(cols)})')
    return modifications


def colonne(table, nom, type_sql, db_path=None):
    """Ajoute la colonne ``nom`` a ``table`` si elle manque. Idempotent.

    Reserve aux tables propres d'un module (contrat 4). Renvoie True si la
    colonne a ete ajoutee.
    """
    chemin, verrou = _cible(db_path)
    with verrou:
        with closing(sqlite3.connect(chemin, timeout=30)) as base:
            with base:
                if table not in _noms_tables(base):
                    raise ValueError('Table inconnue : ' + table)
                return _ajouter_colonne(base, table, nom, type_sql)


def colonnes(table, db_path=None):
    """Liste ordonnee des colonnes d'une table."""
    chemin, verrou = _cible(db_path)
    with verrou:
        with closing(sqlite3.connect(chemin, timeout=30)) as base:
            return [ligne[1] for ligne in base.execute('SELECT * FROM pragma_table_info(?)', (table,))]


def _ajouter_colonne(base, table, nom, type_sql):
    """ALTER TABLE ADD COLUMN si absente ; renvoie True si ajoutee."""
    if not _IDENTIFIANT.match(table) or not _IDENTIFIANT.match(nom):
        raise ValueError('Identifiant SQL invalide : ' + table + '.' + nom)
    type_base = type_sql.split()[0].upper() if type_sql.strip() else ''
    if type_base not in _TYPES:
        raise ValueError('Type SQL non autorise : ' + type_sql)
    if not re.fullmatch(r"[A-Za-z]+(\s+(DEFAULT\s+('[A-Za-z0-9_]*'|-?\d+(\.\d+)?)|NOT\s+NULL))*", type_sql.strip(), re.I):
        raise ValueError('Definition de colonne non autorisee : ' + type_sql)
    existantes = {ligne[1] for ligne in base.execute('SELECT * FROM pragma_table_info(?)', (table,))}
    if nom in existantes:
        return False
    base.execute(f'ALTER TABLE {table} ADD COLUMN "{nom}" {type_sql}')
    return True


def _quoter_liste(cols):
    """Met entre guillemets une liste de colonnes « a, b »."""
    return ', '.join('"' + c.strip() + '"' for c in cols.split(','))


def _noms_tables(base):
    """Ensemble des tables d'une connexion."""
    return {ligne[0] for ligne in base.execute("SELECT name FROM sqlite_master WHERE type='table'")}


class _SansVerrou:
    """Verrou nul pour un chemin explicite hors serveur."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _cible(db_path):
    """Chemin et verrou a utiliser."""
    if db_path is not None:
        if noyau.est_branche() and Path(str(noyau.srv().DB)) == Path(str(db_path)):
            return str(db_path), noyau.srv().LOCK
        return str(db_path), _SansVerrou()
    serveur = noyau.srv()
    return str(serveur.DB), serveur.LOCK
