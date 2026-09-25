# BTOS Snapshot

Save the exact U.S. Census Business Trends and Outlook Survey (BTOS) workbook behind an analysis. The Census publishes new BTOS data every two weeks, and researchers have encountered revised workbooks at the same download location. This tool keeps dated raw copies with a SHA-256 checksum, source URL, response metadata, and worksheet names.

It reads only public Census pages and Excel files. No account, API key, browser, or Python package install is required.

## Run

Use Python 3.11 or newer:

```sh
python btos_snapshot.py list
python btos_snapshot.py save urr
```

The first command lists the workbooks currently advertised on the [Census downloads page](https://www.census.gov/hfp/btos/data_downloads). The second saves the unit response rate workbook under `snapshots/urr/` with a neighboring JSON manifest. Choose another key from `list` to save a different workbook, for example `national` or `ai-supplement-table-2026`.

To put snapshots elsewhere:

```sh
python btos_snapshot.py save state --out CENSUS_DATA_DIR
```

Running the same command again checks Census response metadata and the saved file checksum. When the workbook is unchanged, it keeps the existing snapshot. A changed workbook gets a new timestamped file; the earlier copy stays intact. The tool downloads one workbook per run and stops with an error if Census changes the page format, redirects off its site, or serves an invalid or oversized Excel file.

## Why preserve the file?

The [Census API guide](https://www.census.gov/hfp/btos/downloads/BTOS%20API%20Reference%20Documentation.pdf) says some BTOS data are available only in Excel. [EIG Research documented](https://github.com/EIG-Research/ai-btos) a later workbook vintage removing earlier AI rows, requiring them to keep and combine copies from different dates. This tool preserves the original bytes so users can reproduce an analysis. It does not claim that different survey questions or periods are directly comparable.

The workbooks are Census publications. [Cite the Census Bureau and the vintage you used](https://www.census.gov/about/policies/citation.html); conclusions from your analysis are your responsibility. The tool stores raw workbooks locally and does not change suppressed values.

## Check

```sh
python -m unittest discover -s tests
```

The tool has been tested against the live September 24, 2026 BTOS page on Windows with Python 3.11 and 3.13. Its source-specific discovery depends on the current Census download page. A saved copy cannot recover a workbook revision that disappeared before you ran the tool.

## Scope

This is the first specific source tool. A shared interface for multiple scrapers comes later, after individual tools have actual users and stable behavior. The tool is free to use.
