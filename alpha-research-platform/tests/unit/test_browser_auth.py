import unittest
from unittest.mock import MagicMock, patch
from alpha_platform.brain_client import browser_auth as auth


class BrowserAuthTests(unittest.TestCase):
    def setUp(self):
        auth.PENDING.clear()
        auth.CONNECTED = False

    def response(self, status, headers=None):
        r = MagicMock(status_code=status, headers=headers or {})
        r.json.return_value = {"token": {"expiry": 3600}}
        return r

    def test_direct_login_saves_cookies_without_password(self):
        session = MagicMock()
        session.post.return_value = self.response(201)
        session.get.return_value = self.response(200)
        with patch.object(auth.requests, "Session", return_value=session), patch.object(auth, "_save_session_cookies") as save:
            result = auth.login("research@example.com", "secret-password")
        self.assertEqual(result["status"], "CONNECTED")
        save.assert_called_once_with(session)
        self.assertIsNone(session.auth)
        self.assertNotIn("secret-password", str(result))

    def test_biometric_link_then_verified_session(self):
        session = MagicMock()
        session.post.side_effect = [self.response(401, {"WWW-Authenticate":"persona", "Location":"/authentication/persona/test"}), self.response(201)]
        session.get.return_value = self.response(200)
        with patch.object(auth.requests, "Session", return_value=session), patch.object(auth, "_save_session_cookies") as save:
            result = auth.login("research@example.com", "secret-password")
            save.assert_not_called()
            self.assertTrue(result["verification_url"].startswith("https://api.worldquantbrain.com/authentication/"))
            attempt = auth.PENDING[result["attempt_id"]]
            attempt["next_check"] = 0
            self.assertEqual(auth.check(result["attempt_id"])["status"], "CONNECTED")
            save.assert_called_once()
        self.assertFalse(auth.PENDING)
        self.assertIsNone(session.auth)

    def test_reject_external_verification_link(self):
        session = MagicMock()
        session.post.return_value = self.response(401, {"WWW-Authenticate":"persona", "Location":"https://example.com/authentication/test"})
        with patch.object(auth.requests, "Session", return_value=session), patch.object(auth, "_save_session_cookies") as save:
            with self.assertRaises(ValueError):
                auth.login("research@example.com", "secret-password")
            save.assert_not_called()
        self.assertFalse(auth.PENDING)

    def test_pending_responses_do_not_discard_biometric_session(self):
        for status in (200, 400, 403, 429):
            with self.subTest(status=status):
                self.setUp()
                session=MagicMock()
                session.post.side_effect=[self.response(401,{"WWW-Authenticate":"persona","Location":"/authentication/persona/test"}),self.response(status)]
                with patch.object(auth.requests,"Session",return_value=session),patch.object(auth,"_save_session_cookies") as save:
                    result=auth.login("research@example.com","secret-password")
                    auth.PENDING[result["attempt_id"]]["next_check"]=0
                    self.assertEqual(auth.check(result["attempt_id"])["status"],"BIOMETRIC_REQUIRED")
                    self.assertEqual(auth.state()["attempt_id"],result["attempt_id"])
                    save.assert_not_called()

    def test_created_challenge_without_authenticated_cookie_remains_pending(self):
        session=MagicMock()
        session.post.side_effect=[self.response(401,{"WWW-Authenticate":"persona","Location":"/authentication/persona/test"}),self.response(201)]
        session.get.return_value=self.response(204)
        with patch.object(auth.requests,"Session",return_value=session),patch.object(auth,"_save_session_cookies") as save:
            result=auth.login("research@example.com","secret-password")
            auth.PENDING[result["attempt_id"]]["next_check"]=0
            self.assertEqual(auth.check(result["attempt_id"])["status"],"BIOMETRIC_REQUIRED")
            save.assert_not_called()

    def test_wrong_password_returns_safe_message(self):
        session = MagicMock()
        session.post.return_value = self.response(401)
        with patch.object(auth.requests, "Session", return_value=session):
            with self.assertRaises(ValueError) as error:
                auth.login("research@example.com", "secret-password")
        self.assertNotIn("secret-password", str(error.exception))
        session.close.assert_called_once()

    def test_pending_checks_are_throttled_and_cancel_clears_credentials(self):
        session = MagicMock()
        session.post.return_value = self.response(401, {"WWW-Authenticate":"persona", "Location":"/authentication/persona/test"})
        with patch.object(auth.requests, "Session", return_value=session), patch.object(auth, "_load_session_cookies", return_value=None):
            result = auth.login("research@example.com", "secret-password")
            auth.check(result["attempt_id"])
            session.post.assert_called_once()
            auth.cancel(result["attempt_id"])
        self.assertFalse(auth.PENDING)
        self.assertIsNone(session.auth)

    def test_login_api_requires_local_token_and_never_echoes_password(self):
        from fastapi.testclient import TestClient
        from alpha_platform.api.main import app, LOCAL_TOKEN
        with patch("alpha_platform.api.main.startup_recovery"), TestClient(app) as client, patch.object(auth, "login", return_value={"status":"CONNECTED"}) as login:
            body={"email":"research@example.com","password":"secret-password"}
            self.assertEqual(client.post("/api/brain/login",json=body).status_code,403)
            response=client.post("/api/brain/login",json=body,headers={"X-Local-Token":LOCAL_TOKEN})
            self.assertEqual(response.status_code,200)
            login.assert_called_once_with("research@example.com","secret-password")
            invalid=client.post("/api/brain/login",json={"email":"bad","password":"secret-password"},headers={"X-Local-Token":LOCAL_TOKEN})
            self.assertEqual(invalid.status_code,422)
            self.assertNotIn("secret-password",invalid.text)


if __name__ == "__main__":
    unittest.main()
