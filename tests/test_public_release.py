import os
import unittest
from unittest.mock import patch

from test_support import TempProjectCaseMixin, reload_modules


class PublicReleaseTests(TempProjectCaseMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.config, self.store, self.web = reload_modules('config_store', 'monitor_store', 'web_app')

    def test_only_astana_is_created(self):
        self.web._init_db()
        self.assertEqual(set(self.web._city_options(include_inactive=True)), {'astana'})
        self.assertEqual(self.web._city_options()['astana']['facilityId'], '134')

    def test_no_builtin_administrator_credentials(self):
        with patch.dict(os.environ, {'USVISA_ADMIN_EMAIL': '', 'USVISA_ADMIN_PASSWORD': ''}):
            self.assertEqual(self.web._default_admin_email({}), '')
            with self.assertRaises(RuntimeError):
                self.web._default_admin_password({})
            with self.assertRaises(RuntimeError):
                self.web._init_db()

    def test_urls_reject_other_facility_country_and_schedule(self):
        self.config.validate_astana_urls(self.env, required=True)
        for key, value in [
            ('USVISA_DATES_API_URL', self.env['USVISA_DATES_API_URL'].replace('/134.json', '/125.json')),
            ('USVISA_APPOINTMENT_URL', self.env['USVISA_APPOINTMENT_URL'].replace('ru-kz', 'en-pl')),
            ('USVISA_TIMES_API_URL', self.env['USVISA_TIMES_API_URL'].replace('/123456/', '/654321/')),
            ('USVISA_APPOINTMENT_URL', ''),
        ]:
            with self.subTest(key=key, value=value):
                with self.assertRaises(ValueError):
                    self.config.validate_astana_urls(dict(self.env, **{key: value}), required=True)

    def test_facility_override_cannot_change_astana(self):
        with patch.dict(os.environ, {'USVISA_FACILITY_ID': '125'}):
            self.config.load_config_to_env()
            self.assertEqual(os.environ['USVISA_FACILITY_ID'], '134')

    def test_profile_booking_preferences_override_inherited_process_values(self):
        self.web._init_db()
        with self.web._db() as conn:
            user_id = conn.execute("SELECT id FROM users WHERE role = 'admin'").fetchone()['id']
            conn.execute("UPDATE user_profiles SET priority_dates = '', prep_days = 4 WHERE user_id = ?", (user_id,))
        inherited = {'USVISA_PRIORITY_DATES': '2099-01-01', 'USVISA_PREPARATION_DAYS': '99'}
        with patch.object(self.web, '_env_for_subprocess', return_value=inherited.copy()):
            env = self.web._env_for_user_subprocess(user_id)
        self.assertEqual(env['USVISA_PRIORITY_DATES'], '')
        self.assertEqual(env['USVISA_PREPARATION_DAYS'], '4')
        with self.web._db() as conn:
            conn.execute("UPDATE user_profiles SET priority_dates = '2099-02-01,2099-02-02' WHERE user_id = ?", (user_id,))
        with patch.object(self.web, '_env_for_subprocess', return_value=inherited.copy()):
            env = self.web._env_for_user_subprocess(user_id)
        self.assertEqual(env['USVISA_PRIORITY_DATES'], '2099-02-01,2099-02-02')
