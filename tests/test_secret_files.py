import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ctn_data import secret_files


class SecretFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'data' / 'secrets'
        self.source.mkdir(parents=True)

    def test_legacy_plaintext_and_bom_remain_readable(self):
        path = self.source / 'worker_token'
        path.write_text('synthetic-token', encoding='utf-8-sig')
        self.assertEqual(secret_files.read_secret(path), 'synthetic-token')

    def test_prepare_copies_only_docker_inputs_and_does_not_rewrite_unchanged_files(self):
        (self.source / 'postgres_url').write_text('synthetic-database-url')
        (self.source / 'worker_token').write_text('synthetic-token')
        with patch.object(secret_files, 'restrict_access') as acl:
            self.assertEqual(secret_files.prepare(self.root), 1)
            runtime = self.root / 'data' / 'runtime-secrets' / 'worker_token'
            self.assertEqual(runtime.read_text(), 'synthetic-token')
            self.assertFalse((runtime.parent / 'postgres_url').exists())
            stamp = runtime.stat().st_mtime_ns
            secret_files.prepare(self.root)
            self.assertEqual(runtime.stat().st_mtime_ns, stamp)
            self.assertTrue(acl.called)

    def test_acl_failure_prevents_secret_output(self):
        with patch.object(secret_files, 'restrict_access', side_effect=OSError('denied')):
            with self.assertRaises(OSError):
                secret_files._atomic(self.source / 'new', b'synthetic-secret')
        self.assertFalse((self.source / 'new').exists())

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_migration_encrypts_sources_and_recovery_copy_keeps_runtime_compatible(self):
        (self.source / 'postgres_url').write_text('synthetic-database-url')
        (self.source / 'worker_token').write_text('synthetic-token')
        with patch.object(secret_files, 'restrict_access'):
            self.assertEqual(secret_files.migrate(self.root), 2)
            for name, value in [('postgres_url', 'synthetic-database-url'), ('worker_token', 'synthetic-token')]:
                raw = (self.source / name).read_bytes()
                self.assertTrue(raw.startswith(secret_files.HEADER))
                self.assertNotIn(value.encode(), raw)
                self.assertEqual(secret_files.read_secret(self.source / name), value)
            backup = next((self.source / 'encrypted-backups').iterdir())
            self.assertEqual(secret_files.read_secret(backup / 'worker_token'), 'synthetic-token')
            self.assertEqual((self.root / 'data/runtime-secrets/worker_token').read_text(), 'synthetic-token')
            self.assertEqual(secret_files.migrate(self.root), 0)
            secret_files.write_secret(self.source / 'worker_token', 'changed-synthetic-token')
            self.assertEqual(secret_files.restore(self.root, backup), 2)
            self.assertEqual(secret_files.read_secret(self.source / 'worker_token'), 'synthetic-token')

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_replacement_failure_retains_original_and_encrypted_backup(self):
        path = self.source / 'worker_token'
        path.write_text('synthetic-token')
        original = secret_files._atomic
        def fail_canonical(target, raw):
            if Path(target) == path:
                raise OSError('synthetic replacement failure')
            return original(target, raw)
        with patch.object(secret_files, 'restrict_access'), patch.object(secret_files, '_atomic', side_effect=fail_canonical):
            with self.assertRaises(OSError):
                secret_files.migrate(self.root)
        self.assertEqual(path.read_text(), 'synthetic-token')
        backup = next((self.source / 'encrypted-backups').iterdir())
        self.assertEqual(secret_files.read_secret(backup / 'worker_token'), 'synthetic-token')

    def test_restore_rejects_paths_outside_recovery_directory(self):
        with self.assertRaises(ValueError):
            secret_files.restore(self.root, self.root)

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL')
    def test_real_acl_can_restrict_a_temporary_file_without_losing_access(self):
        path = self.source / 'acl-test'
        path.write_text('synthetic')
        secret_files.restrict_access(self.source)
        secret_files.restrict_access(path)
        self.assertEqual(path.read_text(), 'synthetic')
