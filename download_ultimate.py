#!/usr/bin/env python3
"""
Ultimate PDF Downloader - Maximum Coverage Academic Paper Downloader

Downloads PDFs from multiple sources with fallback strategies.

STRATEGIES (in priority order):
1. PMC OA FTP      - Free, official NIH Open Access
2. Publishers      - Direct from conquered publishers (AHA, NEJM, Blood, BMJ, JNeurosci)
3. Unpaywall       - Green OA repositories
4. Semantic Scholar - S2 Open Access PDFs
5. Europe PMC      - European mirror of PMC
6. PMID -> DOI     - Convert PMID to DOI for papers without DOI
7. Sci-Hub         - Last resort, highest coverage

INSTALLATION:
    pip install -r requirements.txt

USAGE:
    # Simple: just a list of DOIs (one per line)
    python download_ultimate.py --input dois.txt --output ./pdfs --email your@email.com

    # CSV with columns: doi, pmid, pmcid (any subset works)
    python download_ultimate.py --input papers.csv --output ./pdfs --email your@email.com

    # Test mode (20 papers)
    python download_ultimate.py --input papers.csv --output ./pdfs --email your@email.com --test

    # Resume interrupted download
    python download_ultimate.py --input papers.csv --output ./pdfs --email your@email.com --resume

INPUT FORMATS:
    1. Text file (.txt): One DOI per line
       10.1038/nature12373
       10.1126/science.1234567
       ...

    2. CSV file (.csv): With headers (doi, pmid, pmcid - any combination)
       doi,pmid,pmcid
       10.1038/nature12373,23903654,PMC3749847
       ...

EXPECTED COVERAGE: ~70-90% depending on paper types
"""

import csv
import json
import re
import signal
import time
import requests
import tarfile
import io
import argparse
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from urllib.parse import quote

try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except ImportError:
    HAS_CLOUDSCRAPER = False
    print("Note: Install cloudscraper for better publisher coverage: pip install cloudscraper")

# =============================================================================
# CONFIGURATION
# =============================================================================

# Sci-Hub mirror (usualwant has no captcha)
SCIHUB_URL = "https://sci-hub.usualwant.com"

# Publishers with known PDF patterns
PUBLISHER_CONFIG = {
    '10.1161': {'name': 'AHA', 'domain': 'ahajournals.org'},
    '10.1056': {'name': 'NEJM', 'domain': 'nejm.org'},
    '10.1182': {'name': 'Blood', 'domain': 'ashpublications.org'},
    '10.1523': {'name': 'JNeurosci', 'domain': 'jneurosci.org'},
    '10.1136': {'name': 'BMJ', 'domain': 'bmj.com'},
}


# =============================================================================
# DOWNLOADER
# =============================================================================

class UltimateDownloader:
    def __init__(self, output_dir: Path, email: str, delay: float = 2.0):
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.email = email
        self.delay = delay

        # HTTP clients
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        })

        self.scraper = None
        if HAS_CLOUDSCRAPER:
            self.scraper = cloudscraper.create_scraper(
                browser={'browser': 'firefox', 'platform': 'linux'}
            )

        # Statistics
        self.stats = defaultdict(int)
        self.source_stats = defaultdict(int)
        self._interrupted = False

        # Progress tracking
        self.processed_ids = set()

    # =========================================================================
    # Strategy 1: PMC OA FTP
    # =========================================================================

    def try_pmc_oa_ftp(self, pmcid: str, filepath: Path) -> tuple:
        """Download from PMC Open Access FTP"""
        if not pmcid or 'PMC' not in str(pmcid).upper():
            return False, 'no_pmcid'

        pmcid = pmcid.upper()
        if not pmcid.startswith('PMC'):
            pmcid = 'PMC' + pmcid

        url = f'https://www.ncbi.nlm.nih.gov/pmc/utils/oa/oa.fcgi?id={pmcid}'

        try:
            resp = self.session.get(url, timeout=15)

            if 'idIsNotOpenAccess' in resp.text:
                return False, 'not_oa'

            ftp_match = re.search(r'href="(ftp://[^"]+)"', resp.text)
            if not ftp_match:
                return False, 'no_link'

            https_url = ftp_match.group(1).replace(
                'ftp://ftp.ncbi.nlm.nih.gov',
                'https://ftp.ncbi.nlm.nih.gov'
            )

            dl = self.session.get(https_url, timeout=120)
            if dl.status_code != 200:
                return False, f'dl_{dl.status_code}'

            tar_data = io.BytesIO(dl.content)
            with tarfile.open(fileobj=tar_data, mode='r:gz') as tar:
                for member in tar.getmembers():
                    if member.name.endswith('.pdf'):
                        pdf_content = tar.extractfile(member).read()
                        if self._save_pdf(pdf_content, filepath):
                            return True, 'pmc_oa_ftp'

            return False, 'no_pdf_in_tar'

        except Exception as e:
            return False, f'error:{str(e)[:20]}'

    # =========================================================================
    # Strategy 2: Publishers
    # =========================================================================

    def try_publisher(self, doi: str, filepath: Path) -> tuple:
        """Download from conquered publishers"""
        if not doi or not self.scraper:
            return False, 'no_doi_or_scraper'

        prefix = doi.split('/')[0] if '/' in doi else ''
        config = PUBLISHER_CONFIG.get(prefix)
        if not config:
            return False, 'not_conquered'

        publisher = config['name']

        try:
            # Visit landing page first
            landing_url = f"https://doi.org/{doi}"
            resp = self.scraper.get(landing_url, timeout=30, allow_redirects=True)

            if resp.status_code != 200:
                return False, f'{publisher}_http_{resp.status_code}'

            # Extract PDF link
            pdf_patterns = [
                r'<meta[^>]+citation_pdf_url[^>]+content=["\']([^"\']+)',
                r'href=["\']([^"\']+/doi/pdf/[^"\']+)',
                r'href=["\']([^"\']+article-pdf[^"\']+)',
                r'href=["\']([^"\']+\.full\.pdf)',
            ]

            for pattern in pdf_patterns:
                match = re.search(pattern, resp.text, re.IGNORECASE)
                if match:
                    pdf_url = match.group(1)
                    if pdf_url.startswith('/'):
                        from urllib.parse import urlparse
                        parsed = urlparse(resp.url)
                        pdf_url = f"{parsed.scheme}://{parsed.netloc}{pdf_url}"

                    pdf_resp = self.scraper.get(pdf_url, timeout=60)
                    if self._save_pdf(pdf_resp.content, filepath):
                        return True, f'{publisher}'

            return False, f'{publisher}_no_pdf'

        except Exception as e:
            return False, f'{publisher}_err:{str(e)[:15]}'

    # =========================================================================
    # Strategy 3: Unpaywall
    # =========================================================================

    def try_unpaywall(self, doi: str, filepath: Path) -> tuple:
        """Get repository version from Unpaywall"""
        if not doi:
            return False, 'no_doi'

        url = f"https://api.unpaywall.org/v2/{quote(doi)}?email={self.email}"

        try:
            resp = self.session.get(url, timeout=15)
            if resp.status_code != 200:
                return False, f'api_{resp.status_code}'

            data = resp.json()

            # Try locations by priority
            locations = data.get('oa_locations', [])

            for loc in locations:
                host_type = loc.get('host_type', '')

                # Skip publisher (usually paywalled)
                if host_type == 'publisher':
                    continue

                pdf_url = loc.get('url_for_pdf')
                if pdf_url:
                    try:
                        pdf_resp = self.session.get(pdf_url, timeout=60)
                        if self._save_pdf(pdf_resp.content, filepath):
                            return True, f'unpaywall_{host_type}'
                    except:
                        continue

                # Try PMC
                landing = loc.get('url', '')
                pmc_match = re.search(r'PMC(\d+)', landing, re.IGNORECASE)
                if pmc_match:
                    pmcid = f"PMC{pmc_match.group(1)}"
                    success, source = self.try_pmc_oa_ftp(pmcid, filepath)
                    if success:
                        return True, 'unpaywall_pmc'

            return False, 'no_repo_version'

        except Exception as e:
            return False, f'unpaywall_err:{str(e)[:15]}'

    # =========================================================================
    # Strategy 4: Semantic Scholar
    # =========================================================================

    def try_semantic_scholar(self, doi: str, filepath: Path) -> tuple:
        """Get PDF from Semantic Scholar"""
        if not doi:
            return False, 'no_doi'

        url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{quote(doi)}?fields=openAccessPdf"

        try:
            resp = self.session.get(url, timeout=10)
            if resp.status_code != 200:
                return False, f's2_api_{resp.status_code}'

            data = resp.json()
            oa_pdf = data.get('openAccessPdf')

            if oa_pdf and oa_pdf.get('url'):
                pdf_url = oa_pdf['url']
                pdf_resp = self.session.get(pdf_url, timeout=60)
                if self._save_pdf(pdf_resp.content, filepath):
                    return True, 'semantic_scholar'

            return False, 's2_no_pdf'

        except Exception as e:
            return False, f's2_err:{str(e)[:15]}'

    # =========================================================================
    # Strategy 5: Europe PMC
    # =========================================================================

    def try_europe_pmc(self, pmcid: str, filepath: Path) -> tuple:
        """Download from Europe PMC"""
        if not pmcid:
            return False, 'no_pmcid'

        pmcid = pmcid.upper()
        if not pmcid.startswith('PMC'):
            pmcid = 'PMC' + pmcid

        pdf_url = f"https://europepmc.org/backend/ptpmcrender.fcgi?accid={pmcid}&blobtype=pdf"

        try:
            resp = self.session.get(pdf_url, timeout=60)
            if self._save_pdf(resp.content, filepath):
                return True, 'europe_pmc'
            return False, 'epmc_invalid'
        except Exception as e:
            return False, f'epmc_err:{str(e)[:15]}'

    # =========================================================================
    # Strategy 6: PMID to DOI conversion
    # =========================================================================

    def pmid_to_doi(self, pmid: str) -> str:
        """Convert PMID to DOI via PubMed API"""
        if not pmid:
            return None

        url = f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db=pubmed&id={pmid}&retmode=json"

        try:
            resp = self.session.get(url, timeout=10)
            if resp.status_code != 200:
                return None

            data = resp.json()
            result = data.get('result', {}).get(str(pmid), {})

            for item in result.get('articleids', []):
                if item.get('idtype') == 'doi':
                    return item.get('value')

            return None
        except:
            return None

    # =========================================================================
    # Strategy 7: Sci-Hub (last resort)
    # =========================================================================

    def try_scihub(self, doi: str, filepath: Path) -> tuple:
        """Download from Sci-Hub (usualwant mirror - no captcha)"""
        if not doi:
            return False, 'no_doi'

        client = self.scraper if self.scraper else self.session

        try:
            url = f"{SCIHUB_URL}/{doi}"
            resp = client.get(url, timeout=30, allow_redirects=True)

            if resp.status_code != 200:
                return False, f'scihub_http_{resp.status_code}'

            text_lower = resp.text.lower()

            # Check for captcha or not in database
            if 'captcha' in text_lower:
                return False, 'scihub_captcha'
            if 'request this article' in text_lower or 'mutual aid' in text_lower:
                return False, 'not_in_scihub'

            # Find PDF link
            pdf_url = None

            # iframe
            match = re.search(r'<iframe[^>]+src=["\']([^"\']+)["\']', resp.text)
            if match:
                pdf_url = match.group(1)

            # embed
            if not pdf_url:
                match = re.search(r'<embed[^>]+src=["\']([^"\']+)["\']', resp.text)
                if match:
                    pdf_url = match.group(1)

            # Direct link
            if not pdf_url:
                match = re.search(r'(https?://[^\s"\'<>]+\.pdf)', resp.text)
                if match:
                    pdf_url = match.group(1)

            if not pdf_url:
                return False, 'scihub_no_link'

            # Fix URL
            if pdf_url.startswith('//'):
                pdf_url = 'https:' + pdf_url
            elif pdf_url.startswith('/'):
                pdf_url = SCIHUB_URL + pdf_url

            # Download PDF
            pdf_resp = client.get(pdf_url, timeout=60)
            if self._save_pdf(pdf_resp.content, filepath):
                return True, 'scihub'

            return False, 'scihub_invalid_pdf'

        except requests.Timeout:
            return False, 'scihub_timeout'
        except Exception as e:
            return False, f'scihub_err:{str(e)[:15]}'

    # =========================================================================
    # Helper methods
    # =========================================================================

    def _save_pdf(self, content: bytes, filepath: Path) -> bool:
        """Validate and save PDF"""
        if not content or len(content) < 5000:
            return False
        if b'%PDF' not in content[:1024]:
            return False

        filepath.write_bytes(content)
        return True

    def _make_paper_id(self, doi: str, pmid: str) -> str:
        """Generate paper ID for filename"""
        if doi:
            safe = re.sub(r'[<>:"/\\|?*]', '_', doi)
            return f"DOI_{safe}"
        elif pmid:
            return f"PMID_{pmid}"
        return None

    # =========================================================================
    # Main processing
    # =========================================================================

    def process_paper(self, paper: dict) -> dict:
        """Process single paper, trying all strategies in priority order"""
        doi = paper.get('doi', '').strip()
        pmid = paper.get('pmid', '').strip()
        pmcid = paper.get('pmcid', '').strip()

        paper_id = self._make_paper_id(doi, pmid)
        if not paper_id:
            return {'status': 'skipped', 'error': 'no_id'}

        result = {
            'paper_id': paper_id,
            'doi': doi,
            'pmid': pmid,
            'pmcid': pmcid,
            'status': 'failed',
            'source': None,
            'error': None,
        }

        filepath = self.output_dir / f"{paper_id}.pdf"

        # Check if already exists
        if filepath.exists() and filepath.stat().st_size > 5000:
            result['status'] = 'exists'
            self.stats['exists'] += 1
            return result

        strategies = []

        # Strategy 1: PMC OA FTP (priority when PMCID available)
        if pmcid:
            strategies.append(('pmc_oa_ftp', lambda: self.try_pmc_oa_ftp(pmcid, filepath)))

        # Strategy 2: Conquered publishers
        if doi:
            prefix = doi.split('/')[0] if '/' in doi else ''
            if prefix in PUBLISHER_CONFIG:
                strategies.append(('publisher', lambda: self.try_publisher(doi, filepath)))

        # Strategy 3: Unpaywall
        if doi:
            strategies.append(('unpaywall', lambda: self.try_unpaywall(doi, filepath)))

        # Strategy 4: Semantic Scholar
        if doi:
            strategies.append(('semantic_scholar', lambda: self.try_semantic_scholar(doi, filepath)))

        # Strategy 5: Europe PMC
        if pmcid:
            strategies.append(('europe_pmc', lambda: self.try_europe_pmc(pmcid, filepath)))

        # Strategy 6: PMID to DOI conversion (if no DOI)
        if not doi and pmid:
            converted_doi = self.pmid_to_doi(pmid)
            if converted_doi:
                doi = converted_doi
                result['doi'] = doi
                self.stats['pmid_converted'] += 1

        # Strategy 7: Sci-Hub (last resort)
        if doi:
            strategies.append(('scihub', lambda: self.try_scihub(doi, filepath)))

        # Execute strategies
        errors = []
        for name, strategy_func in strategies:
            try:
                success, info = strategy_func()
                if success:
                    result['status'] = 'downloaded'
                    result['source'] = info
                    self.stats['downloaded'] += 1
                    self.source_stats[info] += 1
                    return result
                else:
                    errors.append(f"{name}:{info}")
            except Exception as e:
                errors.append(f"{name}:exception")

        result['error'] = '; '.join(errors[-3:])  # Keep last 3 errors
        self.stats['failed'] += 1
        return result

    def run(self, papers: list, limit: int = None, progress_file: Path = None) -> list:
        """Run download"""
        if limit:
            papers = papers[:limit]

        total = len(papers)
        print(f"{'='*60}")
        print("Ultimate PDF Downloader")
        print('='*60)
        print(f"Papers: {total}")
        print(f"Output: {self.output_dir}")
        print(f"Delay: {self.delay}s")
        print(f"Cloudscraper: {'Yes' if HAS_CLOUDSCRAPER else 'No'}")
        print()

        # Load existing progress
        if progress_file and progress_file.exists():
            with open(progress_file, 'r') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if row.get('status') in ('downloaded', 'exists'):
                        self.processed_ids.add(row.get('paper_id'))
            print(f"Resuming: {len(self.processed_ids)} already processed")

        results = []

        # Ctrl+C handler
        def on_interrupt(sig, frame):
            print("\n\nInterrupted! Saving progress...")
            self._interrupted = True

        original_handler = signal.signal(signal.SIGINT, on_interrupt)

        try:
            for i, paper in enumerate(papers, 1):
                if self._interrupted:
                    print(f"Stopped at {i}/{total}")
                    break

                # Skip already processed
                paper_id = self._make_paper_id(paper.get('doi'), paper.get('pmid'))
                if paper_id in self.processed_ids:
                    continue

                result = self.process_paper(paper)
                results.append(result)

                status = result['status']
                if status == 'downloaded':
                    print(f"[{i}/{total}] OK {result['source']} | {paper.get('doi', paper.get('pmid', ''))[:40]}")
                elif status == 'exists':
                    pass  # Silent skip
                else:
                    # Progress update every 50 failures
                    if self.stats['failed'] % 50 == 0:
                        print(f"[{i}/{total}] Progress: +{self.stats['downloaded']} ={self.stats['exists']} -{self.stats['failed']}")

                time.sleep(self.delay)

        finally:
            signal.signal(signal.SIGINT, original_handler)

        # Summary
        print(f"\n{'='*60}")
        print("Complete")
        print('='*60)
        print(f"Downloaded: {self.stats['downloaded']}")
        print(f"Already existed: {self.stats['exists']}")
        print(f"PMID converted: {self.stats['pmid_converted']}")
        print(f"Failed: {self.stats['failed']}")

        total_success = self.stats['downloaded'] + self.stats['exists']
        total_processed = total_success + self.stats['failed']
        if total_processed > 0:
            print(f"Success rate: {total_success/total_processed*100:.1f}%")

        print("\nSource breakdown:")
        for source, count in sorted(self.source_stats.items(), key=lambda x: -x[1]):
            print(f"  {source}: {count}")

        return results


# =============================================================================
# INPUT LOADING
# =============================================================================

def load_papers(input_path: Path) -> list:
    """
    Load papers from input file.

    Supports:
    - .txt: One DOI per line
    - .csv: With headers (doi, pmid, pmcid - any combination)
    """
    papers = []

    suffix = input_path.suffix.lower()

    if suffix == '.txt':
        # Plain text: one DOI per line
        with open(input_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    papers.append({'doi': line, 'pmid': '', 'pmcid': ''})

    elif suffix == '.csv':
        # CSV with headers
        with open(input_path, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            for row in reader:
                # Skip already downloaded if status column exists
                status = row.get('status', '')
                if status in ('downloaded', 'already_exists'):
                    continue

                papers.append({
                    'doi': row.get('doi', '').strip(),
                    'pmid': row.get('pmid', '').strip(),
                    'pmcid': row.get('pmcid', '').strip(),
                })

    else:
        # Try as text file
        with open(input_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    papers.append({'doi': line, 'pmid': '', 'pmcid': ''})

    # Filter out papers with no identifiers
    papers = [p for p in papers if p.get('doi') or p.get('pmid') or p.get('pmcid')]

    return papers


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Ultimate PDF Downloader - Download academic papers from multiple sources',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Download from DOI list
  python download_ultimate.py --input dois.txt --output ./pdfs --email you@example.com

  # Download from CSV
  python download_ultimate.py --input papers.csv --output ./pdfs --email you@example.com

  # Test with 20 papers
  python download_ultimate.py --input papers.csv --output ./pdfs --email you@example.com --test

  # Resume interrupted download
  python download_ultimate.py --input papers.csv --output ./pdfs --email you@example.com --resume
        """
    )
    parser.add_argument('--input', '-i', type=str, required=True,
                        help='Input file (.txt with DOIs or .csv with doi/pmid/pmcid columns)')
    parser.add_argument('--output', '-o', type=str, default='./pdf_output',
                        help='Output directory (default: ./pdf_output)')
    parser.add_argument('--email', '-e', type=str, default='research@example.com',
                        help='Email for API requests (required for Unpaywall)')
    parser.add_argument('--delay', type=float, default=2.0,
                        help='Delay between requests in seconds (default: 2.0)')
    parser.add_argument('--limit', type=int,
                        help='Limit number of papers to process')
    parser.add_argument('--test', action='store_true',
                        help='Test mode (process only 20 papers)')
    parser.add_argument('--resume', action='store_true',
                        help='Resume from previous progress')
    args = parser.parse_args()

    # Validate input
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"Error: Input file not found: {input_path}")
        return 1

    # Load papers
    print("Loading papers...")
    papers = load_papers(input_path)
    print(f"Loaded: {len(papers)} papers\n")

    if not papers:
        print("No papers to process.")
        return 0

    if args.test:
        args.limit = 20

    # Output directory
    output_dir = Path(args.output)

    # Progress file for resume
    progress_file = output_dir / '_progress.csv' if args.resume else None

    # Run downloader
    downloader = UltimateDownloader(output_dir, email=args.email, delay=args.delay)
    results = downloader.run(papers, limit=args.limit, progress_file=progress_file)

    # Save results
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    results_file = output_dir / f'_results_{timestamp}.csv'

    with open(results_file, 'w', newline='', encoding='utf-8') as f:
        fieldnames = ['paper_id', 'doi', 'pmid', 'pmcid', 'status', 'source', 'error']
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(results)

    # Save progress for resume
    progress_csv = output_dir / '_progress.csv'
    with open(progress_csv, 'w', newline='', encoding='utf-8') as f:
        fieldnames = ['paper_id', 'doi', 'pmid', 'pmcid', 'status', 'source', 'error']
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(results)

    print(f"\nResults saved: {results_file}")
    print(f"Progress saved: {progress_csv}")

    return 0


if __name__ == '__main__':
    exit(main())
