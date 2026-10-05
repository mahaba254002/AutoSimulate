import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
import json

from alpha_platform.research import catalog


class CataloguePacingTests(unittest.TestCase):
    def test_progress_lock_is_retried_without_losing_saved_page(self):
        with TemporaryDirectory() as directory:
            checkpoint=Path(directory)/'fields.jsonl'
            with patch.object(catalog,'read_json',return_value={'count':1,'results':[{'id':'field'}]}),patch.object(
                    Path,'replace',side_effect=PermissionError(13,'locked')),patch.object(catalog.time,'sleep'),patch.object(catalog,'PROGRESS_CACHE',{}):
                self.assertEqual(catalog.fetch_pages(None,'/data-fields',{},checkpoint),[{'id':'field'}])
                self.assertEqual(json.loads(checkpoint.read_text())['results'],[{'id':'field'}])
                self.assertEqual(catalog.PROGRESS_CACHE[str(checkpoint.with_suffix('.progress.json'))]['downloaded'],1)

    def test_progress_publication_recovers_after_temporary_lock(self):
        with TemporaryDirectory() as directory,patch.object(catalog,'PROGRESS_CACHE',{}):
            progress=Path(directory)/'fields.progress.json'
            original=Path.replace
            calls=[]
            def replace(path,target):
                calls.append(target)
                if len(calls)==1:
                    raise PermissionError(13,'locked')
                return original(path,target)
            with patch.object(Path,'replace',replace),patch.object(catalog.time,'sleep'):
                catalog.publish_progress(progress,{'downloaded':50})
            self.assertEqual(json.loads(progress.read_text()),{'downloaded':50})
            self.assertEqual(len(calls),2)

    def test_retry_preserves_checkpoint_seed_only_for_identical_failed_selection(self):
        record=MagicMock(status='ERROR',capabilities={'selection':{'seed':1,'max_fields':200}})
        self.assertEqual(catalog.retry_selection({'seed':2,'max_fields':200},record)['seed'],1)
        self.assertEqual(catalog.retry_selection({'seed':2,'max_fields':100},record)['seed'],2)

    def test_error_distinguishes_file_access_and_network_timeout_without_sensitive_details(self):
        self.assertIn('Local catalogue file access',catalog.sync_error_message(PermissionError(13,'secret path')))
        self.assertNotIn('secret path',catalog.sync_error_message(PermissionError(13,'secret path')))
        self.assertIn('timed out',catalog.sync_error_message(catalog.requests.Timeout('secret url')))

    def test_catalogue_failure_identifies_endpoint_and_redacts_sensitive_detail(self):
        response=MagicMock(status_code=400)
        response.json.return_value={"offset":["Invalid offset"],"password":"PRIVATE", "detail":"contact user@example.com"}
        with patch.object(catalog,"catalog_request",return_value=response):
            with self.assertRaises(ValueError) as error:
                catalog.read_json(None,"/data-fields")
        message=str(error.exception)
        self.assertIn("/data-fields",message)
        self.assertIn("Invalid offset",message)
        self.assertNotIn("PRIVATE",message)
        self.assertNotIn("user@example.com",message)
    def test_partial_headers_and_invalid_headers_preserve_fallback(self):
        self.assertEqual(catalog.header_pacing({})[0],1.5)
        self.assertEqual(catalog.header_pacing({"x-ratelimit-limit-minute":"bad"})[0],1.5)
        self.assertEqual(catalog.header_pacing({"x-ratelimit-remaining-minute":"1"})[0],60)
        self.assertEqual(catalog.header_pacing({"x-ratelimit-remaining-minute":"-1"})[0],1.5)

    def test_advertised_allowance_controls_pace(self):
        delay,values=catalog.header_pacing({"x-ratelimit-limit-minute":"10",
            "x-ratelimit-remaining-minute":"8","x-ratelimit-limit-second":"2",
            "x-ratelimit-remaining-second":"1"})
        self.assertAlmostEqual(delay,60/9)
        self.assertEqual(values["limit_minute"],10)
        self.assertEqual(catalog.header_pacing({"x-ratelimit-limit-minute":"10",
            "x-ratelimit-remaining-minute":"1"})[0],60)

    def test_next_request_waits_after_successful_low_allowance_response(self):
        response=MagicMock(status_code=200,headers={"x-ratelimit-remaining-minute":"1"})
        session=MagicMock()
        session.get.return_value=response
        with patch.object(catalog,"NEXT_REQUEST",0),patch.object(catalog,"cooldown",return_value=None),patch.object(
                catalog.time,"monotonic",return_value=100),patch.object(catalog.time,"sleep") as sleep:
            catalog.catalog_request(session,"get","/data-fields")
            catalog.catalog_request(session,"get","/data-fields")
            self.assertEqual(sleep.call_args.args[0],60)

    def test_existing_retry_after_cooldown_is_not_bypassed(self):
        session=MagicMock()
        with patch.object(catalog,"cooldown",return_value=datetime.now(timezone.utc)+timedelta(minutes=5)):
            with self.assertRaises(catalog.CatalogRateLimited):
                catalog.catalog_request(session,"get","/data-fields")
        session.get.assert_not_called()
