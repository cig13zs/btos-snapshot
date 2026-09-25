import io
import json
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from unittest.mock import patch

from btos_snapshot import Dataset, download, latest_snapshot, parse_datasets


class FakeResponse(io.BytesIO):
    def __init__(self, body, url, headers=None):
        super().__init__(body)
        self.url = url
        self.headers = headers or {}

    def geturl(self):
        return self.url


def sample_workbook():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as workbook:
        workbook.writestr(
            "xl/workbook.xml",
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="URR - National" sheetId="1"/></sheets></workbook>',
        )
    return output.getvalue()


class SnapshotTests(unittest.TestCase):
    def test_current_census_lists_are_parsed(self):
        bundle = (
            'e.currentDocuments=[{location:"./downloads/State.xlsx",name:"State"}];'
            'e.aiSupplementQuestions=[{name:"AI Supplement Table 2026",'
            'location:"./downloads/AI_Supplement_Table_2026.xlsx",filetype:"XLSX"}]'
        )
        datasets = parse_datasets(bundle)
        self.assertEqual(datasets["state"].url, "https://www.census.gov/hfp/btos/downloads/State.xlsx")
        self.assertEqual(datasets["ai-supplement-table-2026"].group, "ai-supplement")

    def test_offsite_download_link_is_rejected(self):
        bundle = (
            'e.currentDocuments=[{location:"https://example.com/file.xlsx",name:"State"}];'
            'e.aiSupplementQuestions=[{name:"AI",location:"./downloads/AI.xlsx"}]'
        )
        with self.assertRaisesRegex(ValueError, "Unexpected Census download URL"):
            parse_datasets(bundle)

    def test_save_keeps_one_copy_when_source_is_unchanged(self):
        dataset = Dataset("urr", "URR", "core", "https://www.census.gov/hfp/btos/downloads/URR.xlsx")
        first = FakeResponse(sample_workbook(), dataset.url, {"ETag": '"v1"'})
        unchanged = urllib.error.HTTPError(dataset.url, 304, "Not Modified", {}, None)
        with tempfile.TemporaryDirectory() as directory:
            with patch("btos_snapshot.urllib.request.urlopen", side_effect=[first, unchanged]):
                first_status, first_path = download(dataset, Path(directory))
                second_status, second_path = download(dataset, Path(directory))
            self.assertEqual((first_status, second_status), ("saved", "unchanged"))
            self.assertEqual(first_path, second_path)
            self.assertEqual(len(list((Path(directory) / "urr").glob("*.xlsx"))), 1)
            self.assertEqual(len(list((Path(directory) / "urr").glob("*.json"))), 1)
            manifest = latest_snapshot(Path(directory) / "urr")
            self.assertEqual(manifest["worksheets"], ["URR - National"])

    def test_invalid_workbook_is_not_saved(self):
        dataset = Dataset("urr", "URR", "core", "https://www.census.gov/hfp/btos/downloads/URR.xlsx")
        with tempfile.TemporaryDirectory() as directory:
            with patch("btos_snapshot.urllib.request.urlopen", return_value=FakeResponse(b"not excel", dataset.url)):
                with self.assertRaises(zipfile.BadZipFile):
                    download(dataset, Path(directory))
            self.assertEqual(list((Path(directory) / "urr").iterdir()), [])

    def test_manifest_cannot_refer_outside_its_snapshot_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            for path in ("../outside.xlsx", "..\\outside.xlsx"):
                (folder / "20260925.json").write_text(
                    json.dumps({"file": path, "sha256": "0" * 64}), encoding="utf-8"
                )
                with self.assertRaisesRegex(ValueError, "Invalid snapshot manifest"):
                    latest_snapshot(folder)


if __name__ == "__main__":
    unittest.main()
