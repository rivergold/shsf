"""Project renames must reuse the existing local database configuration."""
from pathlib import Path
import tempfile
import unittest
from scripts.local_postgres import local_defaults


class LocalDefaultsTests(unittest.TestCase):
    def test_fresh_install_uses_shsf(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(local_defaults(Path(directory)/'missing'),('shsf','/tmp/shsf-pg-socket',55439))

    def test_reuses_existing_database_socket_and_port(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'database-url'
            config.write_text('postgresql:///legacy_research?host=%2Fprivate%2Ftmp%2Flegacy-socket&port=55440\n')
            self.assertEqual(local_defaults(config),('legacy_research','/private/tmp/legacy-socket',55440))

    def test_sqlite_does_not_override_postgres_defaults(self):
        with tempfile.TemporaryDirectory() as directory:
            config=Path(directory)/'database-url';config.write_text('sqlite:///.data/dev.sqlite3')
            self.assertEqual(local_defaults(config),('shsf','/tmp/shsf-pg-socket',55439))
