#!/usr/bin/env python3
"""Save dated, verifiable copies of public Census BTOS workbooks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree


PAGE_URL = "https://www.census.gov/hfp/btos/data_downloads"
SOURCE_DIR = "/hfp/btos/downloads/"
USER_AGENT = "btos-snapshot/0.1 (public BTOS workbook preservation)"
MAX_PAGE_BYTES = 2_000_000
MAX_WORKBOOK_BYTES = 100_000_000
GROUPS = {"currentDocuments": "core", "aiSupplementQuestions": "ai-supplement"}
SHEET_TAG = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}sheet"


@dataclass(frozen=True)
class Dataset:
    key: str
    name: str
    group: str
    url: str


class ScriptTags(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            source = dict(attrs).get("src")
            if source:
                self.sources.append(source)


def census_url(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    return parts.scheme == "https" and parts.hostname == "www.census.gov"


def get_text(url: str) -> str:
    if not census_url(url):
        raise ValueError("Refusing a non-Census URL")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=25) as response:
        if not census_url(response.geturl()):
            raise ValueError("Census redirected to a different host")
        body = response.read(MAX_PAGE_BYTES + 1)
    if len(body) > MAX_PAGE_BYTES:
        raise ValueError("Census page exceeds the expected size")
    return body.decode("utf-8")


def field_value(js_object: str, field: str) -> str | None:
    match = re.search(r"\b" + re.escape(field) + r':"((?:[^"\\]|\\.)*)"', js_object)
    return json.loads('"' + match.group(1) + '"') if match else None


def parse_datasets(bundle: str) -> dict[str, Dataset]:
    datasets: dict[str, Dataset] = {}
    for variable, group in GROUPS.items():
        match = re.search(r"\." + variable + r"=\[(.*?)\]", bundle, re.DOTALL)
        if not match:
            raise ValueError(f"Census download list {variable} was not found; the page may have changed")
        found = 0
        for item in re.findall(r"\{[^{}]*\}", match.group(1)):
            location = field_value(item, "location")
            name = field_value(item, "name")
            if not location or not name:
                continue
            key = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            source = urllib.parse.urljoin(PAGE_URL, location)
            parts = urllib.parse.urlsplit(source)
            if not census_url(source) or not parts.path.startswith(SOURCE_DIR) or not parts.path.endswith(".xlsx"):
                raise ValueError(f"Unexpected Census download URL for {name}: {source}")
            if key in datasets:
                raise ValueError(f"Duplicate Census dataset name: {name}")
            encoded_path = urllib.parse.quote(urllib.parse.unquote(parts.path), safe="/")
            url = urllib.parse.urlunsplit((parts.scheme, parts.netloc, encoded_path, "", ""))
            datasets[key] = Dataset(key, name, group, url)
            found += 1
        if not found:
            raise ValueError(f"Census download list {variable} is empty; the page may have changed")
    return datasets


def discover_datasets() -> dict[str, Dataset]:
    parser = ScriptTags()
    parser.feed(get_text(PAGE_URL))
    scripts = [
        urllib.parse.urljoin(PAGE_URL, source)
        for source in parser.sources
        if re.fullmatch(r"js/app\.[a-f0-9]+\.js", source)
    ]
    if len(scripts) != 1:
        raise ValueError("Census application script was not found; the page may have changed")
    return parse_datasets(get_text(scripts[0]))


def workbook_sheets(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as workbook:
        bad_member = workbook.testzip()
        if bad_member:
            raise ValueError(f"Workbook ZIP contains a corrupt member: {bad_member}")
        try:
            xml = workbook.read("xl/workbook.xml")
        except KeyError as error:
            raise ValueError("Download is a ZIP but not an Excel workbook") from error
    names = [sheet.attrib["name"] for sheet in ElementTree.fromstring(xml).iter(SHEET_TAG)]
    if not names:
        raise ValueError("Excel workbook has no worksheets")
    return names


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def latest_snapshot(folder: Path) -> dict | None:
    manifests = sorted(folder.glob("*.json"))
    if not manifests:
        return None
    record = json.loads(manifests[-1].read_text(encoding="utf-8"))
    file_name = record.get("file")
    digest = record.get("sha256")
    if (
        not isinstance(file_name, str)
        or "/" in file_name
        or "\\" in file_name
        or Path(file_name).name != file_name
        or not file_name.endswith(".xlsx")
        or not isinstance(digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", digest)
    ):
        raise ValueError(f"Invalid snapshot manifest: {manifests[-1]}")
    return record


def download(dataset: Dataset, output: Path) -> tuple[str, Path | None]:
    folder = output / dataset.key
    folder.mkdir(parents=True, exist_ok=True)
    previous = latest_snapshot(folder)
    previous_path = folder / previous["file"] if previous else None
    previous_ok = bool(
        previous_path
        and previous_path.is_file()
        and sha256_file(previous_path) == previous["sha256"]
    )

    headers = {"User-Agent": USER_AGENT}
    if previous and previous_ok:
        if previous.get("etag"):
            headers["If-None-Match"] = previous["etag"]
        if previous.get("last_modified"):
            headers["If-Modified-Since"] = previous["last_modified"]
    request = urllib.request.Request(dataset.url, headers=headers)
    for attempt in range(3):
        try:
            response = urllib.request.urlopen(request, timeout=45)
            break
        except urllib.error.HTTPError as error:
            if error.code == 304 and previous_ok:
                return "unchanged", previous_path
            if error.code not in (429, 502, 503, 504) or attempt == 2:
                raise
            time.sleep(2**attempt)

    temporary: Path | None = None
    try:
        with response:
            if not census_url(response.geturl()):
                raise ValueError("Census redirected the workbook to a different host")
            with tempfile.NamedTemporaryFile(dir=folder, prefix=".download-", suffix=".part", delete=False) as stream:
                temporary = Path(stream.name)
                digest = hashlib.sha256()
                size = 0
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_WORKBOOK_BYTES:
                        raise ValueError("Workbook exceeds the 100 MB safety limit")
                    digest.update(chunk)
                    stream.write(chunk)
            etag = response.headers.get("ETag")
            last_modified = response.headers.get("Last-Modified")

        sheets = workbook_sheets(temporary)
        sha256 = digest.hexdigest()
        if previous_ok and sha256 == previous["sha256"]:
            return "unchanged", previous_path

        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        stem = f"{stamp}-{sha256[:12]}"
        workbook_path = folder / f"{stem}.xlsx"
        manifest_path = folder / f"{stem}.json"
        record = {
            "dataset": dataset.name,
            "group": dataset.group,
            "source_url": dataset.url,
            "discovered_from": PAGE_URL,
            "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
            "etag": etag,
            "last_modified": last_modified,
            "bytes": size,
            "sha256": sha256,
            "worksheets": sheets,
            "file": workbook_path.name,
        }
        os.replace(temporary, workbook_path)
        temporary = None
        manifest_temp = manifest_path.with_suffix(".json.part")
        try:
            manifest_temp.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
            os.replace(manifest_temp, manifest_path)
        finally:
            manifest_temp.unlink(missing_ok=True)
        return "saved", workbook_path
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Preserve dated copies of public Census BTOS workbooks")
    parser.add_argument("command", choices=("list", "save"))
    parser.add_argument("dataset", nargs="?", help="Dataset key shown by the list command")
    parser.add_argument("--out", type=Path, default=Path("snapshots"), help="Local snapshot directory")
    args = parser.parse_args(argv)
    if (args.command == "save") != bool(args.dataset):
        parser.error("save needs one dataset key; list takes no dataset key")
    try:
        datasets = discover_datasets()
        if args.command == "list":
            for dataset in datasets.values():
                print(f"{dataset.key:34} {dataset.group:14} {dataset.name}")
            return 0
        dataset = datasets.get(args.dataset)
        if not dataset:
            parser.error(f"Unknown dataset {args.dataset!r}; run 'list' to see current choices")
        status, path = download(dataset, args.out)
        print(f"{status}: {path}")
        return 0
    except (OSError, ValueError, urllib.error.URLError, zipfile.BadZipFile) as error:
        print(f"BTOS snapshot failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
