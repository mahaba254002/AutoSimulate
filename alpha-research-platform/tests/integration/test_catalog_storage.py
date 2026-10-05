import unittest
from unittest.mock import patch
from datetime import datetime,timezone
from sqlalchemy import event
import test_local_workflow as local_workflow
from alpha_platform.db.models import CatalogScope,ScopedField
from alpha_platform.research import catalog


class CatalogStorageTests(unittest.TestCase):
    setUp=local_workflow.PostgresWorkflowTests.setUp
    tearDown=local_workflow.PostgresWorkflowTests.tearDown

    def test_all_migrations_install_in_empty_isolated_schema(self):
        from alembic import command
        from alembic.config import Config
        from sqlalchemy import text
        from alpha_platform.db.models import Base
        Base.metadata.drop_all(self.engine)
        config=Config('alembic.ini')
        with self.engine.begin() as conn:
            conn.execute(text(f'SET LOCAL search_path TO "{self.schema}", public'))
            config.attributes['connection']=conn
            config.attributes['version_table_schema']=self.schema
            command.upgrade(config,'head')
            self.assertEqual(conn.execute(text(f'SELECT version_num FROM "{self.schema}".alembic_version')).scalar(),'0006')
            self.assertEqual(conn.execute(text('SELECT count(*) FROM catalog_field')).scalar(),0)

    def test_legacy_snapshot_migration_and_downgrade_preserve_records(self):
        import importlib
        from alembic.migration import MigrationContext
        from alembic.operations import Operations
        from alpha_platform.db.models import ScopedDataset
        from sqlalchemy import text
        migration=importlib.import_module('db.migrations.versions.0006_indexed_catalogue')
        ScopedField.__table__.drop(self.engine)
        ScopedDataset.__table__.drop(self.engine)
        datasets=[{'id':'fund','name':'Global Fundamental Data','category':{'id':'fundamental','name':'Fundamental'}}]
        fields=[{'id':'saved','dataset':{'id':'fund'},'description':'Previously synced field','coverage':.8,'dateCoverage':1}]
        with self.sessions() as db:
            db.add(CatalogScope(scope_id='old-scope',settings={},legacy_datasets=datasets,legacy_fields=fields,status='COMPLETE'))
            db.commit()
        with self.engine.begin() as conn, patch.object(migration,'op',Operations(MigrationContext.configure(conn))):
            migration.upgrade()
        with self.sessions() as db:
            scope=db.get(CatalogScope,'old-scope')
            self.assertEqual(scope.datasets,datasets)
            self.assertEqual(scope.fields,fields)
            row=db.get(ScopedField,('old-scope','saved'))
            self.assertEqual(row.category_id,'fundamental')
            self.assertEqual(float(row.instrument_coverage),80)
        with self.engine.begin() as conn, patch.object(migration,'op',Operations(MigrationContext.configure(conn))):
            migration.downgrade()
            row=conn.execute(text("SELECT datasets,fields FROM catalog_scope WHERE scope_id='old-scope'")).one()
            self.assertEqual(row[0],datasets)
            self.assertEqual(row[1],fields)

    def seed(self):
        with self.sessions() as db:
            for region in ('GLB','USA'):
                db.add(CatalogScope(scope_id=f'EQUITY/{region}/1/TOP3000',settings={'region':region,'delay':1,'universe':'TOP3000'},
                    status='COMPLETE',synced_at=datetime.now(timezone.utc),
                    datasets=[{'id':'fund','name':'Global Fundamental Data','category':{'id':'fundamental','name':'Fundamental'}},
                              {'id':'other','name':'Model Data','category':{'id':'model','name':'Model'}}],
                    fields=[{'id':'shared','dataset':{'id':'fund'},'description':f'Fundamental field for {region}','type':'MATRIX','coverage':.9,'dateCoverage':1},
                            {'id':'second','dataset':{'id':'fund'},'description':'Another fundamental measurement','type':'MATRIX','coverage':.8,'dateCoverage':.95},
                            {'id':'sparse','dataset':{'id':'other'},'description':'Sparse model field','type':'MATRIX','coverage':.2,'dateCoverage':1}]))
            db.commit()

    def test_scope_isolation_sql_pagination_and_hierarchy(self):
        self.seed()
        statements=[]
        event.listen(self.engine,'before_cursor_execute',lambda conn,cursor,sql,params,ctx,many:statements.append(sql))
        with patch.object(catalog,'SessionLocal',self.sessions):
            result=catalog.browse('EQUITY/GLB/1/TOP3000','fields',dataset_id='fund',category_id='fundamental',min_instrument_coverage=80,min_date_coverage=90,limit=1)
            self.assertEqual(result['count'],2)
            self.assertEqual(len(result['results']),1)
            self.assertEqual(result['dataset']['name'],'Global Fundamental Data')
            next_page=catalog.browse('EQUITY/GLB/1/TOP3000','fields',dataset_id='fund',min_instrument_coverage=80,offset=1,limit=1)
            self.assertNotEqual(result['results'][0]['id'],next_page['results'][0]['id'])
            self.assertIn('GLB',next_page['results'][0]['description'])
            self.assertTrue(any('LIMIT' in sql for sql in statements))
            self.assertFalse(any('catalog_scope.fields' in sql or 'catalog_scope.datasets' in sql for sql in statements))
            self.assertEqual(catalog.browse('EQUITY/GLB/1/TOP3000','fields',search='%')['count'],0)
            datasets=catalog.browse('EQUITY/GLB/1/TOP3000','datasets',category_id='fundamental')
            self.assertEqual(datasets['results'][0]['synced_field_count'],2)

    def test_refresh_updates_records_atomically_and_preserves_other_scopes(self):
        self.seed()
        with self.sessions() as db:
            scope=db.get(CatalogScope,'EQUITY/GLB/1/TOP3000')
            scope.fields=[{'id':'shared','dataset':'fund','description':'Updated fundamental description','type':'MATRIX','coverage':1,'dateCoverage':1}]
            db.commit()
            self.assertEqual(db.query(ScopedField).filter_by(scope_id=scope.scope_id).count(),1)
            self.assertEqual(scope.fields[0]['description'],'Updated fundamental description')
            scope.fields=[]
            db.flush()
            db.rollback()
            self.assertEqual(len(scope.fields),1)
            self.assertEqual(db.query(ScopedField).filter_by(scope_id='EQUITY/USA/1/TOP3000').count(),3)

    def test_alpha_count_strict_bounds_and_complete_filtered_export(self):
        self.seed()
        with self.sessions() as db:
            scope=db.get(CatalogScope,'EQUITY/GLB/1/TOP3000')
            scope.fields=[{'id':f'field{i:03}', 'dataset':'fund', 'alphaCount':6+i,
                           'coverage':.9,'dateCoverage':1} for i in range(105)] + [
                {'id':'lower','dataset':'fund','alphaCount':5},
                {'id':'upper','dataset':'fund','alphaCount':500},
                {'id':'unknown','dataset':'fund'},
                {'id':'invalid','dataset':'fund','alphaCount':'unavailable'}]
            db.commit()
        with patch.object(catalog,'SessionLocal',self.sessions):
            result=catalog.browse('EQUITY/GLB/1/TOP3000','fields',dataset_id='fund',
                                  alpha_count_above=5,alpha_count_below=500)
            self.assertEqual(result['count'],105)
            self.assertEqual(len(result['results']),50)
            exported=catalog.export_dataset('EQUITY/GLB/1/TOP3000','fund',
                                            alpha_count_above=5,alpha_count_below=500)
            self.assertEqual(exported['field_count'],105)
            self.assertEqual(len(exported['fields']),105)
            self.assertTrue(all(5<f['alphaCount']<500 for f in exported['fields']))
            self.assertEqual(exported['dataset']['name'],'Global Fundamental Data')
            self.assertEqual(len(catalog.export_dataset('EQUITY/GLB/1/TOP3000','fund')['fields']),109)
            with self.assertRaises(ValueError):
                catalog.export_dataset('EQUITY/GLB/1/TOP3000','missing')
