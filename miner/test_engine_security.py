#!/usr/bin/env python3
"""Fail-closed tests for the SoloLuck Miner pinned-engine logic (headless).
Covers: success, checksum mismatch (modified binary), missing checksum,
missing download / timeout, unsupported CPU, valid + invalid cached engine,
read-only destination, interrupted (truncated) download, unverified
user-supplied engine, and a real-bytes end-to-end verify of the actual
cpuminer-opt v26.1 release archive.
Run: python3 test_engine_security.py -v
"""
import hashlib, io, os, shutil, socket, tempfile, unittest, zipfile
import sololuck_miner as m

GOOD = b'MZfake-engine-good'
GOOD_SHA = hashlib.sha256(GOOD).hexdigest()

def make_zip(files):
    b = io.BytesIO()
    with zipfile.ZipFile(b, 'w') as z:
        for name, data in files.items():
            z.writestr('cpuminer-opt-test/' + name, data)
    return b.getvalue()

class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='slm-test-')
        self.appdir = tempfile.mkdtemp(prefix='slm-app-')
        self._orig = (m.engine_dir, m.app_dir, m._http_get, m.preferred_builds,
                      m.ENGINE_ZIP_SHA256, dict(m.ENGINE_FILE_SHA256))
        m.engine_dir = lambda: self.tmp
        m.app_dir = lambda: self.appdir
        m.preferred_builds = lambda: ['cpuminer-sse2.exe']
        self.zip = make_zip({'cpuminer-sse2.exe': GOOD})
        m.ENGINE_ZIP_SHA256 = hashlib.sha256(self.zip).hexdigest()
        m.ENGINE_FILE_SHA256.clear()
        m.ENGINE_FILE_SHA256['cpuminer-sse2.exe'] = GOOD_SHA
        m._http_get = lambda url, **k: self.zip

    def tearDown(self):
        (m.engine_dir, m.app_dir, m._http_get, m.preferred_builds,
         m.ENGINE_ZIP_SHA256, saved) = self._orig
        m.ENGINE_FILE_SHA256.clear(); m.ENGINE_FILE_SHA256.update(saved)
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.appdir, ignore_errors=True)

class TestDownload(Base):
    def test_success_download_and_verify(self):
        eng = m.download_engine()
        self.assertTrue(eng.endswith('cpuminer-sse2.exe'))
        self.assertEqual(m._sha256_file(eng), GOOD_SHA)

    def test_modified_archive_fails_closed(self):
        bad = bytearray(self.zip); bad[10] ^= 0xFF
        m._http_get = lambda url, **k: bytes(bad)
        with self.assertRaisesRegex(RuntimeError, 'SECURITY'):
            m.download_engine()
        self.assertIsNone(m.find_local_engine())  # nothing installed

    def test_interrupted_download_fails_closed(self):
        m._http_get = lambda url, **k: self.zip[:len(self.zip)//2]
        with self.assertRaisesRegex(RuntimeError, 'SECURITY'):
            m.download_engine()
        self.assertIsNone(m.find_local_engine())

    def test_wrong_pinned_checksum_fails(self):
        m.ENGINE_ZIP_SHA256 = '0' * 64
        with self.assertRaisesRegex(RuntimeError, 'SECURITY'):
            m.download_engine()

    def test_missing_member_checksum_never_runs(self):
        del m.ENGINE_FILE_SHA256['cpuminer-sse2.exe']
        with self.assertRaisesRegex(RuntimeError, 'Unsupported CPU|no matching'):
            m.download_engine()

    def test_unsupported_cpu(self):
        m.preferred_builds = lambda: ['cpuminer-nonexistent.exe']
        self.zip = make_zip({'other.txt': b'x'})
        m.ENGINE_ZIP_SHA256 = hashlib.sha256(self.zip).hexdigest()
        m._http_get = lambda url, **k: self.zip
        with self.assertRaisesRegex(RuntimeError, 'Unsupported CPU'):
            m.download_engine()

    def test_network_timeout_propagates(self):
        def boom(url, **k): raise socket.timeout('timed out')
        m._http_get = boom
        with self.assertRaises(socket.timeout):
            m.download_engine()
        self.assertIsNone(m.find_local_engine())

    def test_missing_download_404(self):
        import urllib.error
        def gone(url, **k): raise urllib.error.HTTPError(url, 404, 'nf', {}, None)
        m._http_get = gone
        with self.assertRaises(urllib.error.HTTPError):
            m.download_engine()

    def test_readonly_destination_fails_closed(self):
        if os.geteuid() == 0:
            self.skipTest('root ignores directory permissions')
        os.chmod(self.tmp, 0o555)
        try:
            with self.assertRaises(OSError):
                m.download_engine()
        finally:
            os.chmod(self.tmp, 0o755)

class TestCache(Base):
    def test_valid_cached_engine_used(self):
        p = os.path.join(self.tmp, 'cpuminer-sse2.exe')
        open(p, 'wb').write(GOOD)
        self.assertEqual(m.find_local_engine(), p)

    def test_invalid_cached_engine_quarantined(self):
        p = os.path.join(self.tmp, 'cpuminer-sse2.exe')
        open(p, 'wb').write(b'TAMPERED')
        self.assertIsNone(m.find_local_engine())
        self.assertFalse(os.path.exists(p))
        self.assertTrue(os.path.exists(p + '.quarantined'))

    def test_ensure_engine_redownloads_after_quarantine(self):
        p = os.path.join(self.tmp, 'cpuminer-sse2.exe')
        open(p, 'wb').write(b'TAMPERED')
        eng = m.ensure_engine(confirm_unverified=lambda path: False)
        self.assertEqual(m._sha256_file(eng), GOOD_SHA)

class TestUserOverride(Base):
    def test_unverified_user_engine_refused_headless(self):
        p = os.path.join(self.appdir, 'cpuminer-opt.exe')
        open(p, 'wb').write(b'USERBUILD')
        eng = m.ensure_engine()  # headless default confirm -> refuse
        self.assertNotEqual(eng, p)
        self.assertEqual(m._sha256_file(eng), GOOD_SHA)

    def test_user_engine_matching_manifest_accepted(self):
        m.ENGINE_FILE_SHA256['cpuminer-opt.exe'] = hashlib.sha256(b'USERBUILD').hexdigest()
        p = os.path.join(self.appdir, 'cpuminer-opt.exe')
        open(p, 'wb').write(b'USERBUILD')
        self.assertEqual(m.ensure_engine(), p)

class TestRealRelease(unittest.TestCase):
    """End-to-end with the REAL v26.1 archive bytes and REAL pinned hashes."""
    REAL = '/tmp/cpo261.zip'

    def test_real_archive_verifies_and_installs(self):
        if not os.path.exists(self.REAL):
            self.skipTest('real archive not present')
        blob = open(self.REAL, 'rb').read()
        self.assertEqual(hashlib.sha256(blob).hexdigest(), m.ENGINE_ZIP_SHA256)
        tmp = tempfile.mkdtemp(prefix='slm-real-')
        oe, oh, op = m.engine_dir, m._http_get, m.preferred_builds
        try:
            m.engine_dir = lambda: tmp
            m._http_get = lambda url, **k: blob
            m.preferred_builds = lambda: ['cpuminer-avx2.exe']
            eng = m.download_engine()
            self.assertTrue(eng.endswith('cpuminer-avx2.exe'))
            self.assertEqual(m._sha256_file(eng), m.ENGINE_FILE_SHA256['cpuminer-avx2.exe'])
        finally:
            m.engine_dir, m._http_get, m.preferred_builds = oe, oh, op
            shutil.rmtree(tmp, ignore_errors=True)

if __name__ == '__main__':
    unittest.main(verbosity=2)
