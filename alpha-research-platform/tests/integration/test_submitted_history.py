from datetime import datetime,timezone,timedelta
import unittest
import uuid
from unittest.mock import MagicMock,patch

import test_local_workflow as local_workflow
from alpha_platform.db.models import SubmittedAlpha,SubmittedImport,SubmittedPage,WorkspacePreference,QuotaLedger,SimulationRun,CatalogScope
from alpha_platform.research import submitted,campaigns
from alpha_platform.research.catalog import CatalogRateLimited


def record(index,score=None):
    return {'id':f'alpha{index}','type':'REGULAR','stage':'OS','status':'ACTIVE',
            'regular':{'code':'rank(close)+rank(volume)'},'dateSubmitted':'2025-01-01T00:00:00Z',
            'settings':{'instrumentType':'EQUITY','language':'FASTEXPR','region':'USA','delay':1,'universe':'TOP3000',
                        'decay':0,'neutralization':'SUBINDUSTRY','truncation':.08,'nanHandling':'ON'},
            'is':{'sharpe':5,'fitness':3,'turnover':.1},'os':{'sharpe':2},'score':score}


class SubmittedHistoryTests(unittest.TestCase):
    setUp=local_workflow.PostgresWorkflowTests.setUp
    tearDown=local_workflow.PostgresWorkflowTests.tearDown

    def seed(self,previous=False,owner='owner'):
        identity=uuid.uuid4()
        with self.sessions() as db:
            db.add(SubmittedImport(account_id=owner,import_id=identity,status='QUEUED'))
            db.add(WorkspacePreference(key='submitted-account',value={'id':owner}))
            if previous:db.add(SubmittedAlpha(**submitted.alpha_record(record('old',score=42),owner,uuid.uuid4())))
            db.commit()
        return identity

    def execute(self,pages,owner='owner'):
        with patch.object(submitted,'SessionLocal',self.sessions),patch.object(submitted.catalog,'ResearchSession',MagicMock()),patch.object(submitted,'account',return_value=owner),patch.object(submitted,'read',side_effect=pages) as read,patch.object(submitted,'schedule_resume') as resume:
            submitted.run_sync('owner')
            return read,resume

    def test_all_pages_publish_and_never_create_simulations(self):
        self.seed()
        read,_=self.execute([{'count':53,'results':[record(i) for i in range(50)]},{'count':53,'results':[record(i,12) for i in range(50,53)]}])
        self.assertEqual(read.call_args_list[1].args[2]['offset'],50)
        self.assertEqual(read.call_args_list[0].args[2]['stage'],'OS')
        with patch.object(submitted,'SessionLocal',self.sessions):
            self.assertEqual(submitted.status()['status'],'COMPLETE')
            self.assertEqual(submitted.status()['saved'],53)
            self.assertEqual(submitted.browse()['count'],53)
            self.assertEqual(len(submitted.browse()['results']),50)
            self.assertEqual(submitted.export()['count'],53)
        with self.sessions() as db:
            self.assertEqual(db.query(SubmittedPage).count(),0)
            self.assertEqual(db.query(SimulationRun).count(),0)
            self.assertEqual(db.query(QuotaLedger).count(),0)

    def test_unsubmitted_record_in_list_is_scanned_but_not_published(self):
        self.seed()
        draft=record('draft')
        draft['status']='UNSUBMITTED'
        draft.pop('dateSubmitted')
        self.execute([{'count':2,'results':[draft,record(1)]}])
        with patch.object(submitted,'SessionLocal',self.sessions):
            self.assertEqual(submitted.status()['downloaded'],2)
            self.assertEqual(submitted.status()['saved'],1)
            self.assertEqual(submitted.browse()['results'][0]['id'],'alpha1')
            with self.assertRaises(ValueError):submitted.detail('alphadraft')

    def test_interrupted_pages_resume_and_previous_history_stays_published(self):
        identity=self.seed(previous=True)
        self.execute([{'count':51,'results':[record(i) for i in range(50)]},TimeoutError()])
        with patch.object(submitted,'SessionLocal',self.sessions):
            self.assertEqual(submitted.browse()['count'],1)
            self.assertEqual(submitted.status()['downloaded'],50)
            with patch.object(submitted.catalog,'ResearchSession',MagicMock()),patch.object(submitted,'account',return_value='owner'),patch.object(submitted.WORKER,'submit'):
                submitted.queue_sync()
        with self.sessions() as db:self.assertEqual(db.get(SubmittedImport,'owner').import_id,identity)
        read,_=self.execute([{'count':51,'results':[record(50)]}])
        self.assertEqual(read.call_args.args[2]['offset'],50)
        with patch.object(submitted,'SessionLocal',self.sessions):
            self.assertEqual(submitted.status()['status'],'COMPLETE')
            self.assertEqual(submitted.browse()['count'],52) # Older absent records are retained.

    def test_rate_limit_pauses_and_preserves_resume_position(self):
        self.seed()
        _,resume=self.execute([{'count':51,'results':[record(i) for i in range(50)]},CatalogRateLimited(datetime.now(timezone.utc)+timedelta(minutes=5))])
        resume.assert_called_once()
        with self.sessions() as db:
            state=db.get(SubmittedImport,'owner')
            self.assertEqual(state.status,'PAUSED')
            self.assertEqual(state.downloaded,50)
            self.assertIsNotNone(state.retry_at)

    def test_repeated_page_never_overwrites_saved_history(self):
        self.seed(previous=True)
        self.execute([{'count':2,'results':[record(1)]},{'count':2,'results':[record(1)]}])
        with patch.object(submitted,'SessionLocal',self.sessions):
            self.assertEqual(submitted.browse()['count'],1)
            self.assertTrue(submitted.status()['restart_required'])

    def test_account_change_is_rejected_and_same_id_can_exist_for_two_accounts(self):
        self.seed(previous=True)
        self.execute([],owner='different-owner')
        with self.sessions() as db:
            db.add(SubmittedAlpha(**submitted.alpha_record(record('old',99),'another-owner',uuid.uuid4())))
            db.commit()
        with patch.object(submitted,'SessionLocal',self.sessions):
            self.assertEqual(submitted.detail('alphaold')['score'],42)
            self.assertEqual(submitted.browse()['count'],1)
            self.assertEqual(submitted.status()['status'],'ERROR')

    def test_stop_retains_staging_and_recovery_never_replays_remote_work(self):
        self.seed(previous=True)
        def page(*args):
            with patch.object(submitted,'SessionLocal',self.sessions):submitted.stop()
            return {'count':1,'results':[record(1)]}
        self.execute(page)
        with patch.object(submitted,'SessionLocal',self.sessions):
            self.assertEqual(submitted.status()['status'],'STOPPED')
            self.assertEqual(submitted.browse()['count'],1)
            with self.sessions() as db:
                db.get(SubmittedImport,'owner').status='RUNNING';db.commit()
            submitted.recover_interrupted()
            self.assertEqual(submitted.status()['status'],'INTERRUPTED')

    def test_imported_parent_preserves_settings_and_only_creates_a_draft(self):
        self.seed()
        self.execute([{'count':1,'results':[record(1)]}])
        from alpha_platform.research.campaigns import CampaignInput,ManualPlanInput
        from alpha_platform.config.operators import load_catalog
        with self.sessions() as db:
            db.add(CatalogScope(scope_id='EQUITY/USA/1/TOP3000',settings={'instrumentType':'EQUITY','region':'USA','delay':1,'universe':'TOP3000'},
                status='COMPLETE',datasets=[],fields=[{'id':'close','type':'MATRIX','description':'Closing price'},{'id':'volume','type':'MATRIX','description':'Trading volume'}],
                capabilities={'neutralizations':['SUBINDUSTRY'],'operators':[{'name':n} for n in load_catalog()]}))
            db.commit()
        body=CampaignInput(name='Develop imported alpha',objective='Test controlled revisions of this imported parent',mode='redevelop',provider='manual',model='manual',
            source_expression='rank(close)+rank(volume)',parent_submitted_alpha_id='alpha1',max_attempts=50,
            manual_plan=ManualPlanInput(scope_id='EQUITY/USA/1/TOP3000',expressions=['parent'],neutralization='SUBINDUSTRY'))
        with patch.object(campaigns,'SessionLocal',self.sessions),patch.object(campaigns.WORKER,'submit') as worker:
            result=campaigns.create_campaign(body)
            worker.assert_not_called()
            self.assertEqual(result['status'],'DRAFT')
            self.assertEqual(result['brief']['redevelopment']['parent']['metrics']['sharpe'],5)
            self.assertEqual(result['brief']['settings']['nan_handling'],'ON')
            self.assertEqual(result['brief']['redevelopment']['parent']['alpha_id'],'alpha1')
        with self.sessions() as db:self.assertEqual(db.query(SimulationRun).count(),0)
