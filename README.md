# Paper Scraper

Download academic paper PDFs from multiple sources with automatic fallback strategies.

## Features

- **7 download strategies** with automatic fallback:
  1. PMC Open Access FTP
  2. Direct publisher download (AHA, NEJM, Blood, BMJ, JNeurosci)
  3. Unpaywall (Green OA repositories)
  4. Semantic Scholar
  5. Europe PMC
  6. PMID → DOI conversion
  7. Sci-Hub (last resort)

- **~70-90% success rate** depending on paper types
- **Resume support** - interrupt anytime with Ctrl+C, resume later
- **Skip existing** - won't re-download papers you already have
- **Flexible input** - accepts DOI list (.txt) or CSV with doi/pmid/pmcid

## Installation

```bash
git clone https://github.com/JiachenJasonZhong/Paper_Scraper.git
cd Paper_Scraper
pip install -r requirements.txt
```

## Usage

### Basic: DOI list

Create a text file with one DOI per line:

```
10.1038/nature12373
10.1126/science.1234567
10.1016/j.cell.2021.01.001
```

Run:

```bash
python download_ultimate.py --input dois.txt --output ./pdfs --email your@email.com
```

### CSV input
with any combination of `doi`, `pmid`, `pmcid`:

For better coverage, provide a CSV 
```csv
doi,pmid,pmcid
10.1038/nature12373,23903654,PMC3749847
10.1126/science.1234567,25678901,
,12345678,PMC1234567
```

Run:

```bash
python download_ultimate.py --input papers.csv --output ./pdfs --email your@email.com
```

### Options

| Option | Description |
|--------|-------------|
| `--input`, `-i` | Input file (required) |
| `--output`, `-o` | Output directory (default: `./pdf_output`) |
| `--email`, `-e` | Email for API requests |
| `--delay` | Delay between requests in seconds (default: 2.0) |
| `--limit` | Limit number of papers |
| `--test` | Test mode (20 papers only) |
| `--resume` | Resume from previous progress |

### Examples

```bash
# Test with 20 papers
python download_ultimate.py -i papers.csv -o ./pdfs --test

# Resume interrupted download
python download_ultimate.py -i papers.csv -o ./pdfs --resume

# Faster (1s delay, use with caution)
python download_ultimate.py -i papers.csv -o ./pdfs --delay 1.0
```

## Output

```
pdf_output/
├── DOI_10.1038_nature12373.pdf
├── DOI_10.1126_science.1234567.pdf
├── PMID_12345678.pdf
├── _progress.csv          # For resume
└── _results_20240101_120000.csv  # Download results
```

## Requirements

- Python 3.7+
- `requests`
- `cloudscraper` (optional, improves publisher coverage)

## License

MIT
