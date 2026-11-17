"""Une lecture ou une écriture faite par S.query pendant une transaction de la chaîne ne doit pas
se bloquer contre cette transaction (« database is locked » observé sur la base réelle, dont les
lignes volumineuses font déborder le cache SQLite et prendre le verrou exclusif avant le commit)."""
import time
import unittest
import test_pipeline
import chaine
from chaine import noyau,migration

s=test_pipeline.s

class VerrouTests(unittest.TestCase):
    setUp=test_pipeline.PipelineTests.setUp
    tearDown=test_pipeline.PipelineTests.tearDown

    def test_query_inside_a_spilled_transaction_does_not_deadlock(self):
        chaine.brancher(s);chaine.schema.init()
        debut=time.monotonic()
        with noyau.connexion() as base:
            base.execute('PRAGMA cache_size=8')
            for i in range(400):
                base.execute('INSERT INTO units(id,text,vector) VALUES(?,?,?)',(f'u{i}','texte','x'*40000))
            self.assertEqual(s.query('SELECT count(*) n FROM units',one=True)['n'],400)
            s.query('INSERT INTO events VALUES(?,?,?)',(s.uid(),'écriture imbriquée',s.now()))
        self.assertLess(time.monotonic()-debut,10)
        self.assertEqual(s.query('SELECT count(*) n FROM units',one=True)['n'],400)
        self.assertEqual(s.query("SELECT count(*) n FROM events WHERE message='écriture imbriquée'",one=True)['n'],1)

    def test_nested_write_is_rolled_back_with_the_transaction(self):
        chaine.brancher(s);chaine.schema.init()
        with self.assertRaises(RuntimeError):
            with noyau.connexion() as base:
                s.query('INSERT INTO events VALUES(?,?,?)',(s.uid(),'annulée',s.now()))
                raise RuntimeError('échec du lot')
        self.assertEqual(s.query("SELECT count(*) n FROM events WHERE message='annulée'",one=True)['n'],0)
        s.query('INSERT INTO events VALUES(?,?,?)',(s.uid(),'après',s.now()))
        self.assertEqual(s.query("SELECT count(*) n FROM events WHERE message='après'",one=True)['n'],1)

    def test_migration_with_large_vectors(self):
        chaine.brancher(s);chaine.schema.init()
        vid=s.add_content('texte:lot:gros','Page',' '.join(f'Phrase numéro {i} sur Googlebot.' for i in range(300)))
        chunk=s.query('SELECT id FROM chunks WHERE version_id=? ORDER BY position',(vid,),True)['id']
        for i in range(300):
            cid=s.uid();s.query('INSERT INTO claims(id,text,period,vector,created) VALUES(?,?,?,?,?)',(cid,f'Fait {i}','', 'v'*60000,s.now()))
            s.query('INSERT INTO occurrences VALUES(?,?,?,?,?)',(s.uid(),cid,chunk,f'Phrase numéro {i} sur Googlebot.','[]'))
        debut=time.monotonic();rapport=migration.migrer()
        self.assertLess(time.monotonic()-debut,20)
        self.assertEqual(s.query('SELECT count(*) n FROM units',one=True)['n'],300)

    def test_internal_error_message_names_the_cause(self):
        s.JOB.update(status='idle')
        def casse():raise s.sqlite3.OperationalError('database is locked')
        s.start_job('test',casse).wait(2)
        self.assertEqual(s.JOB['status'],'paused');self.assertIn('database is locked',s.JOB['message'])
        s.JOB.update(status='idle')
        def bogue():raise KeyError('champ')
        s.start_job('test',bogue).wait(2)
        self.assertEqual(s.JOB['status'],'error');self.assertIn('KeyError',s.JOB['message']);self.assertIn('champ',s.JOB['message'])

if __name__=='__main__':unittest.main()
