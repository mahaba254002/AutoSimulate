"""Scoped, indexed dataset and field records; preserve legacy snapshots."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    trigram_class = bind.execute(sa.text("SELECT quote_ident(n.nspname)||'.gin_trgm_ops' FROM pg_extension e JOIN pg_namespace n ON n.oid=e.extnamespace WHERE e.extname='pg_trgm'")).scalar_one()
    for kind in ('dataset','field'):
        columns = [sa.Column('scope_id',sa.Text(),sa.ForeignKey('catalog_scope.scope_id',ondelete='CASCADE'),primary_key=True),
                   sa.Column(kind+'_id',sa.Text(),primary_key=True)]
        if kind == 'dataset':
            columns += [sa.Column('name',sa.Text(),nullable=False)]
        else:
            columns += [sa.Column('dataset_id',sa.Text()),sa.Column('dataset_name',sa.Text()),sa.Column('field_type',sa.Text())]
        columns += [sa.Column('category_id',sa.Text()),sa.Column('category_name',sa.Text()),
                    sa.Column('instrument_coverage',sa.Numeric(8,4)),sa.Column('date_coverage',sa.Numeric(8,4)),
                    sa.Column('search_text',sa.Text(),nullable=False),sa.Column('payload',JSONB(),nullable=False)]
        op.create_table('catalog_'+kind,*columns)
        op.create_index('ix_catalog_'+kind+'_category','catalog_'+kind,['scope_id','category_id'])
        op.create_index('ix_catalog_'+kind+'_search','catalog_'+kind,['search_text'],postgresql_using='gin',postgresql_ops={'search_text':trigram_class})
    op.create_index('ix_catalog_field_dataset_coverage','catalog_field',['scope_id','dataset_id','instrument_coverage','date_coverage'])
    from alpha_platform.research.catalog_store import dataset_record, field_record
    bind = op.get_bind()
    metadata=sa.MetaData()
    datasets=sa.Table('catalog_dataset',metadata,autoload_with=bind)
    fields=sa.Table('catalog_field',metadata,autoload_with=bind)
    scopes=bind.execute(sa.text('SELECT scope_id,datasets,fields FROM catalog_scope'))
    for scope_id, dataset_payloads, field_payloads in scopes:
        by_id={p['id']:p for p in dataset_payloads or []}
        for table, values in ((datasets,[{'scope_id':scope_id,**dataset_record(p)} for p in by_id.values()]),
                              (fields,[{'scope_id':scope_id,**field_record(p,by_id)} for p in {p['id']:p for p in field_payloads or []}.values()])):
            for start in range(0,len(values),500):
                bind.execute(table.insert(),values[start:start+500])


def downgrade():
    # Reconstruct snapshots from the current records before removing the index.
    op.execute("""UPDATE catalog_scope s SET
        datasets=COALESCE((SELECT jsonb_agg(payload ORDER BY dataset_id) FROM catalog_dataset d WHERE d.scope_id=s.scope_id),'[]'::jsonb),
        fields=COALESCE((SELECT jsonb_agg(payload ORDER BY field_id) FROM catalog_field f WHERE f.scope_id=s.scope_id),'[]'::jsonb)""")
    op.drop_table('catalog_field')
    op.drop_table('catalog_dataset')
