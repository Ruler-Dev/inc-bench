"""Test senza modello e senza rete esterna:  python -m unittest discover -s tests -v"""
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import incidenti as I  # noqa: E402
import providers  # noqa: E402


class TempFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._old = (providers.FILE, I.DATASETS)
        providers.FILE = self.tmp / "providers.json"
        I.DATASETS = self.tmp / "datasets"
        shutil.copytree(ROOT / "datasets" / "esempio", I.DATASETS / "esempio")

    def tearDown(self):
        providers.FILE, I.DATASETS = self._old
        shutil.rmtree(self.tmp, ignore_errors=True)


class Providers(TempFiles):
    def test_default_from_example(self):
        self.assertIn("http://localhost:1234/v1", providers.enabled_bases())

    def test_rejects_inline_key_and_bad_values(self):
        for bad in ([{"name": "x", "base_url": "http://h/v1", "api_key": "sk-123"}],
                    [{"name": "x", "base_url": "ftp://h"}],
                    [{"name": "x", "base_url": "http://a/v1"}, {"name": "x", "base_url": "http://b/v1"}],
                    [{"name": "x", "base_url": "http://a/v1", "api_key_env": "non valida"}]):
            with self.assertRaises(ValueError):
                providers.save(bad)

    def test_key_from_env_only_for_matching_base(self):
        providers.save([{"name": "c", "base_url": "https://api.example.com/v1/", "api_key_env": "OH_TEST_KEY"}])
        os.environ["OH_TEST_KEY"] = "segreto"
        try:
            self.assertEqual(providers.api_key("https://api.example.com/v1"), "segreto")
            self.assertIsNone(providers.api_key("http://localhost:1234/v1"))
            view = json.dumps(providers.public_view())
            self.assertNotIn("segreto", view)
            self.assertTrue(providers.public_view()[0]["api_key_set"])
        finally:
            del os.environ["OH_TEST_KEY"]


class Corpus(TempFiles):
    def test_pool_is_fixed_and_samples_repeat(self):
        _, t1 = I.genera_corpus("piccolo", campione=3)
        _, t2 = I.genera_corpus("piccolo", campione=3)
        _, t3 = I.genera_corpus("piccolo", campione=4)
        self.assertEqual(t1["pool"], 100)
        self.assertEqual([i["id"] for i in t1["incidents"]], [i["id"] for i in t2["incidents"]])
        self.assertNotEqual([i["id"] for i in t1["incidents"]], [i["id"] for i in t3["incidents"]])

    def test_script_fields_match_truth(self):
        docs, truth = I.genera_corpus("medio", campione=11)
        det = I.calcola_deterministici(docs)
        for inc in truth["incidents"]:
            for k in I.DET:
                self.assertEqual(det[inc["id"]][k], inc[k], (inc["id"], k))

    def test_example_dataset_labels_rebuild_full_truth(self):
        docs, truth = I.carica_dataset("esempio")
        _, orig = I.genera_corpus("piccolo", campione=0)
        campi = ["id"] + I.DET + I.SEM + I.DER
        self.assertEqual([{k: i[k] for k in campi} for i in truth["incidents"]],
                         [{k: i[k] for k in campi} for i in orig["incidents"]])

    def test_dataset_names_are_sandboxed(self):
        for nome in ("../x", "a/b", "..", "", "x" * 80):
            with self.assertRaises(ValueError):
                I.salva_dataset(nome, {"siem_export.log": "x"})

    def test_upload_requires_corr_and_known_files(self):
        with self.assertRaises(ValueError):
            I.salva_dataset("nuovo", {"siem_export.log": "2026-01-01T00:00:00Z host=a src=b rule=R corr=-"})
        self.assertFalse((I.DATASETS / "nuovo").exists())
        with self.assertRaises(ValueError):
            I.salva_dataset("nuovo", {"siem_export.log": "x", "altro.txt": "y"})
        riga = '2026-01-01T00:00:00Z host=ws-1 src=192.0.2.1 rule=AUTH_FAIL bytes_out=10 corr=X-1 msg="m"'
        ds = I.salva_dataset("nuovo", {"siem_export.log": riga})
        self.assertEqual((ds["incidenti"], ds["etichettato"]), (1, False))

    def test_benchmark_on_labelled_dataset(self):
        evs = []
        I.run_benchmark(I.mock_factory, dataset="esempio", metodi=["S", "C"], emit=evs.append)
        res = {e["method"]: e for e in evs if e["type"] == "result"}
        self.assertEqual(res["S"]["pct_det"], 100.0)
        self.assertIn("pct_sem", res["C"])

    def test_mock_refuses_unlabelled_dataset(self):
        (I.DATASETS / "esempio" / "truth.json").unlink()
        with self.assertRaises(ValueError):
            I.run_benchmark(I.mock_factory, dataset="esempio", metodi=["S"], emit=lambda e: None)


class WebUI(TempFiles):
    @classmethod
    def setUpClass(cls):
        import webui
        cls.webui = webui
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), webui.Handler)
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def req(self, path, body=None, ctype="application/json", origin=None):
        h = {"Content-Type": ctype} if ctype else {}
        if origin:
            h["Origin"] = origin
        r = urllib.request.Request(self.url + path, data=None if body is None else json.dumps(body).encode(), headers=h)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def test_get_endpoints(self):
        self.assertEqual(self.req("/api/providers")[0], 200)
        code, ds = self.req("/api/datasets")
        self.assertEqual(code, 200)
        self.assertEqual(ds["datasets"][0]["nome"], "esempio")

    def test_post_needs_json_and_same_origin(self):
        body = {"providers": [{"name": "a", "base_url": "http://evil.example/v1"}]}
        self.assertEqual(self.req("/api/providers", body, ctype="text/plain")[0], 403)
        self.assertEqual(self.req("/api/providers", body, origin="http://evil.example")[0], 403)
        self.assertFalse(providers.FILE.exists())
        code, r = self.req("/api/providers", {"providers": [{"name": "a", "base_url": "http://localhost:9/v1"}]})
        self.assertEqual((code, r["ok"]), (200, True))
        self.assertTrue(providers.FILE.exists())


if __name__ == "__main__":
    unittest.main()
