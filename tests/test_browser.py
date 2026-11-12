import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sketchfab_link_v2.browser import model_uid, browser_platform, CDP


class BrowserTests(unittest.TestCase):
    def test_only_model_links(self):
        uid='a'*32
        self.assertEqual(model_uid('https://sketchfab.com/3d-models/test-'+uid+'?a=1'),uid)
        for url in ('http://sketchfab.com/3d-models/'+uid,'https://evil.com/'+uid,'https://sketchfab.com/models/abc'):
            with self.assertRaises(ValueError): model_uid(url)

    def test_platforms(self):
        for system,machine,want in [('Windows','AMD64','win64'),('Darwin','arm64','mac-arm64'),('Darwin','x86_64','mac-x64'),('Linux','x86_64','linux64'),('Linux','aarch64','linux-arm64')]:
            self.assertEqual(browser_platform(system,machine),want)

    def test_cdp_external_endpoint_rejected(self):
        with self.assertRaises(ValueError): CDP('ws://example.com/devtools/page/1')


if __name__=='__main__': unittest.main(verbosity=2)
