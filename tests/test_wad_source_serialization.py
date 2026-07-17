import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import zipfile


REPO_ROOT = Path(__file__).resolve().parents[1]


def read_text(relative_path):
    return (REPO_ROOT / relative_path).read_text(encoding="utf-8")


class WadSourceSerializationTests(unittest.TestCase):
    def test_packager_writes_wad_source_as_json_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            marker = tmp / "shell-executed"
            malicious_url = f"https://mirror.example/DOOM1.WAD$(touch${{IFS}}{marker})"
            expected_sha = "a" * 64
            env = os.environ.copy()
            env.update(
                {
                    "PAIRPUTER_MICROVM_CONTEXT_DIR": str(REPO_ROOT / "capsules/hellbox-doom"),
                    "PAIRPUTER_DOOM_CONTEXT_OUT_DIR": str(tmp / "out"),
                    "PAIRPUTER_DOOM1_WAD_URL": malicious_url,
                    "PAIRPUTER_DOOM1_WAD_SHA256": expected_sha,
                }
            )

            result = subprocess.run(
                [str(REPO_ROOT / "substrate/package-doom-image.sh")],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )

            zip_paths = [Path(line) for line in result.stdout.splitlines() if line.endswith(".zip")]
            self.assertTrue(zip_paths, result.stdout)
            self.assertFalse(marker.exists())

            with zipfile.ZipFile(zip_paths[-1]) as archive:
                names = set(archive.namelist())
                self.assertIn("wad-source.json", names)
                self.assertNotIn("wad-source.env", names)
                payload = json.loads(archive.read("wad-source.json").decode("utf-8"))

            self.assertEqual(payload["DOOM1_WAD_URL"], malicious_url)
            self.assertEqual(payload["DOOM1_WAD_SHA256"], expected_sha)

    def test_microvm_build_reads_wad_source_without_sourcing_shell(self):
        dockerfile = read_text("capsules/hellbox-doom/Dockerfile")

        self.assertIn("COPY wad-source.json /tmp/wad-source.json", dockerfile)
        self.assertIn("json.load(open(sys.argv[1]", dockerfile)
        self.assertNotIn(". /tmp/wad-source", dockerfile)
        self.assertNotIn("COPY wad-source.env", dockerfile)


if __name__ == "__main__":
    unittest.main()
