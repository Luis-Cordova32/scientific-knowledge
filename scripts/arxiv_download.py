"""ArXiv Paper Downloader.

Download scientific papers and metadata from ArXiv.

Usage:
    arxiv_download.py fetch <arxiv_id> [--config <config>]
    arxiv_download.py process <arxiv_id> --tag <tag> [--topic <topic>] [--config <config>] [--engine <engine>] [--allow-new-topic] [--force]
    arxiv_download.py discover <query> [--max <n>] [--config <config>]
    arxiv_download.py search <pattern> [--field <field>] [--config <config>]
    arxiv_download.py citations [--config <config>]
    arxiv_download.py (-h | --help)

Commands:
    fetch           Fetch and display paper metadata (title, authors, abstract, tag_options) as JSON
    process         One-shot: fetch metadata, download PDF, save BibTeX, store abstract, convert to markdown.
                    If --topic is omitted, auto-infers from ArXiv categories. Only --tag is required.
                    Exits 2 rather than creating a topic folder that does not already exist -
                    reuse an existing topic, or pass --allow-new-topic deliberately.

    discover        Search ArXiv for papers matching a query. Excludes papers already in the database.
    search          Search the LOCAL database by tag, title, author, abstract, or topic (regex pattern).
    citations       Rebuild the citation graph from all stored paper markdowns.

Options:
    -h --help           Show this help message
    --config <config>   Path to config.yaml (auto-resolved from skill dir if omitted)
    --tag <tag>         Short descriptive tag for the paper (used as filename and BibTeX key)
    --topic <topic>     Research topic category. If omitted, auto-inferred from ArXiv categories.
    --engine <engine>   Conversion engine: markitdown or pypdf (uses config default if omitted)
    --allow-new-topic   Permit creating a topic folder that does not exist yet. Ask the user first.
    --force             Store even if the paper is already in the database under another tag.
    --max <n>           Maximum results for discover [default: 10]
    --field <field>     Field to search: tag, title, author, abstract, topic, or all [default: all]
"""

"""
Python 3
06 / 08 / 2026
@author: z_tjona

"I find that I don't understand things unless I try to program them."
-Donald E. Knuth

"Either mathematics is too big for the human mind or the human mind is more than a machine."
-Kurt Godël
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from typing import Any

import requests
import yaml
from docopt import docopt

# Ensure UTF-8 stdout/stderr on Windows (avoids charmap errors for non-ASCII author names)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

# Resolve SKILL_DIR from this script's location (scripts/ -> skill root)
SKILL_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SKILL_DIR / "config.yaml"

ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}
ARXIV_HEADERS = {"User-Agent": "ScientificKnowledgeBot/1.0 (mailto:atpeko@proton.me)"}
_last_arxiv_request: float = 0.0


def _throttled_get(url: str, **kwargs: Any) -> requests.Response:
    """requests.get with User-Agent header, 3s throttle, and Retry-After support."""
    global _last_arxiv_request
    elapsed = time.monotonic() - _last_arxiv_request
    if elapsed < 3.0:
        time.sleep(3.0 - elapsed)
    kwargs.setdefault("headers", {}).update(ARXIV_HEADERS)
    response = requests.get(url, **kwargs)
    _last_arxiv_request = time.monotonic()
    if response.status_code == 429:
        retry_after = int(response.headers.get("Retry-After", 10))
        print(f"Rate limited (429). Waiting {retry_after}s...", file=sys.stderr)
        time.sleep(retry_after)
        response = requests.get(url, **kwargs)
        _last_arxiv_request = time.monotonic()
    response.raise_for_status()
    return response


def _xml_text(parent: ET.Element, tag: str, ns: dict[str, str] = ARXIV_NS) -> str:
    """Extract text from an XML sub-element. Raises ValueError if missing."""
    el = parent.find(tag, ns)
    if el is None or el.text is None:
        raise ValueError(f"Missing required XML element: {tag}")
    return el.text


def load_config(config_path: str | Path) -> dict[str, Any]:
    """Load configuration from YAML file.

    Environment variables in string values (e.g. $OneDriveConsumer) are
    expanded automatically so the same config.yaml works across machines.
    """
    config_path = Path(config_path)
    if not config_path.exists():
        print(f"Error: Config file not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    with open(config_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    # Expand env vars in string values (especially papers_folder)
    for key, value in config.items():
        if isinstance(value, str):
            config[key] = os.path.expandvars(value)

    # FAIL LOUDLY on a misconfigured papers_folder. Without this the whole tool
    # degrades silently: an unset env var leaves the literal "$PAPERS_DB" as the
    # path, every lookup misses, and `search` reports {"count": 0} -- which is
    # indistinguishable from "the database genuinely has no match". That is how a
    # paper that WAS in the database got reported as absent.
    folder = config.get("papers_folder", "")
    unexpanded = re.findall(r"\$\w+|\$\{\w+\}", folder)
    if unexpanded:
        print(
            f"Error: papers_folder is '{folder}' -- the environment variable(s) "
            f"{', '.join(unexpanded)} are NOT SET on this machine.\n"
            f"  Set it once, e.g.:  setx PAPERS_DB \"E:/path/to/your/papers-db\"\n"
            f"  (open a new shell afterwards), or edit {config_path}.",
            file=sys.stderr,
        )
        sys.exit(2)
    if not Path(folder).is_dir():
        print(
            f"Error: papers_folder does not exist: {folder}\n"
            f"  Fix the path in {config_path} or the env var it points at.",
            file=sys.stderr,
        )
        sys.exit(2)
    abstracts = Path(folder) / "database" / "abstracts.json"
    if not abstracts.is_file():
        print(
            f"Warning: no database/abstracts.json under {folder}. "
            f"Searches will return nothing until a paper is ingested.",
            file=sys.stderr,
        )
    return config


def extract_arxiv_id(arxiv_input: str) -> str:
    """Extract ArXiv ID from URL or raw ID string.

    Handles formats like:
      - 2301.12345
      - 2301.12345v2
      - https://arxiv.org/abs/2301.12345
      - https://arxiv.org/pdf/2301.12345v1
    """
    match = re.search(r"(\d{4}\.\d{4,5})(v\d+)?", arxiv_input)
    if match:
        return match.group(1)
    raise ValueError(f"Could not extract ArXiv ID from: {arxiv_input}")


def _fetch_metadata_api(
    arxiv_id: str, api_url: str = "http://export.arxiv.org/api/query"
) -> dict[str, Any]:
    """Fetch paper metadata from the ArXiv Atom API (export.arxiv.org)."""
    params = {"id_list": arxiv_id, "max_results": 1}
    response = _throttled_get(api_url, params=params, timeout=30)

    root = ET.fromstring(response.text)
    entry = root.find("atom:entry", ARXIV_NS)
    if entry is None:
        raise ValueError(f"No paper found for ArXiv ID: {arxiv_id}")

    # Check for error
    title_el = entry.find("atom:title", ARXIV_NS)
    if title_el is not None and title_el.text and "Error" in title_el.text:
        summary_el = entry.find("atom:summary", ARXIV_NS)
        error_msg = (
            summary_el.text.strip()
            if summary_el is not None and summary_el.text
            else "Unknown error"
        )
        raise ValueError(f"ArXiv API error: {error_msg}")

    title = _xml_text(entry, "atom:title").strip().replace("\n", " ").replace("  ", " ")
    abstract = _xml_text(entry, "atom:summary").strip()
    published = _xml_text(entry, "atom:published").strip()
    updated = _xml_text(entry, "atom:updated").strip()

    # Extract version from entry ID (e.g., http://arxiv.org/abs/2503.17547v2 → v2)
    entry_id_el = entry.find("atom:id", ARXIV_NS)
    version: int | None = None
    if entry_id_el is not None and entry_id_el.text:
        v_match = re.search(r"v(\d+)", entry_id_el.text)
        if v_match:
            version = int(v_match.group(1))

    authors: list[str] = []
    for author_el in entry.findall("atom:author", ARXIV_NS):
        name_el = author_el.find("atom:name", ARXIV_NS)
        if name_el is not None and name_el.text:
            authors.append(name_el.text.strip())

    # Get PDF link
    pdf_url = None
    for link in entry.findall("atom:link", ARXIV_NS):
        if link.get("title") == "pdf":
            pdf_url = link.get("href")
            break
    if not pdf_url:
        pdf_url = f"https://arxiv.org/pdf/{arxiv_id}.pdf"

    # Get categories
    categories = []
    for cat in entry.findall("atom:category", ARXIV_NS):
        categories.append(cat.get("term"))

    return {
        "arxiv_id": arxiv_id,
        "title": title,
        "authors": authors,
        "abstract": abstract,
        "published": published,
        "updated": updated,
        "version": version,
        "pdf_url": pdf_url,
        "categories": categories,
        "url": f"https://arxiv.org/abs/{arxiv_id}",
    }


def _fetch_metadata_via_abs(arxiv_id: str) -> dict[str, Any]:
    """Fallback metadata fetch that parses the arxiv.org abstract page.

    The export.arxiv.org *API* origin aggressively rate-limits (429) uncached
    queries, while the arxiv.org abstract page is CDN-fronted and stays
    available. ArXiv embeds reliable ``citation_*`` meta tags on that page, so
    we can reconstruct the same metadata dict without the API.
    """
    url = f"https://arxiv.org/abs/{arxiv_id}"
    html = _throttled_get(url, timeout=30).text

    def meta_all(name: str) -> list[str]:
        return re.findall(
            rf'<meta\s+name="{name}"\s+content="([^"]*)"', html, re.IGNORECASE
        )

    def meta_one(name: str) -> str | None:
        vals = meta_all(name)
        return vals[0] if vals else None

    title = meta_one("citation_title")
    if not title:
        raise ValueError(f"No paper found for ArXiv ID: {arxiv_id}")

    # citation_author is "Last, First" -> normalize to "First Last".
    authors: list[str] = []
    for a in meta_all("citation_author"):
        a = a.strip()
        if "," in a:
            last, first = (p.strip() for p in a.split(",", 1))
            authors.append(f"{first} {last}".strip())
        elif a:
            authors.append(a)

    abstract = ""
    m = re.search(r'<blockquote class="abstract[^"]*">(.*?)</blockquote>', html, re.S)
    if m:
        abstract = re.sub(r"<[^>]+>", "", m.group(1))
        abstract = re.sub(r"^\s*Abstract:\s*", "", abstract).strip()

    # Version from the embedded versioned id (e.g. 2604.28119v1).
    version: int | None = None
    v_match = re.search(rf"{re.escape(arxiv_id)}v(\d+)", html)
    if v_match:
        version = int(v_match.group(1))

    # Date: citation_date / citation_online_date are YYYY/MM/DD.
    cdate = meta_one("citation_date") or meta_one("citation_online_date") or ""
    published = cdate.replace("/", "-").strip()

    pdf_url = meta_one("citation_pdf_url") or f"https://arxiv.org/pdf/{arxiv_id}"

    # Categories: subject codes like (cs.LG); primary subject appears first.
    subj = re.search(r'<td class="tablecell subjects">(.*?)</td>', html, re.S)
    scope = subj.group(1) if subj else html
    categories = re.findall(r"\(([a-zA-Z\-]+\.[A-Z]{2,})\)", scope)

    return {
        "arxiv_id": arxiv_id,
        "title": title.strip().replace("\n", " ").replace("  ", " "),
        "authors": authors,
        "abstract": abstract,
        "published": published,
        "updated": published,
        "version": version,
        "pdf_url": pdf_url,
        "categories": categories,
        "url": url,
    }


def fetch_metadata(
    arxiv_id: str, api_url: str = "http://export.arxiv.org/api/query"
) -> dict[str, Any]:
    """Fetch paper metadata, preferring the API and falling back to the abs page.

    ``export.arxiv.org`` (the Atom API) frequently 429s uncached queries. When
    that happens we transparently fall back to scraping the CDN-fronted
    arxiv.org abstract page, which exposes the same facts via meta tags.
    """
    try:
        return _fetch_metadata_api(arxiv_id, api_url)
    except Exception as api_err:  # noqa: BLE001 - any API failure -> fallback
        print(
            f"API metadata fetch failed ({api_err}); "
            "falling back to arxiv.org abstract page...",
            file=sys.stderr,
        )
        return _fetch_metadata_via_abs(arxiv_id)


def generate_tag_options(title: str) -> list[str]:
    """Generate 3 tag options from the paper title for the user to pick from.

    Returns a list of 3 tags:
      - Full: first 5 meaningful words
      - Short: first 3 meaningful words
      - Acronym-based: uses capitalized words / acronyms if present
    """
    stopwords = {
        "a",
        "an",
        "the",
        "of",
        "in",
        "for",
        "and",
        "or",
        "to",
        "with",
        "on",
        "by",
        "from",
        "is",
        "are",
        "was",
        "were",
        "at",
        "as",
        "its",
        "via",
        "using",
        "how",
        "what",
        "when",
        "where",
        "that",
        "this",
        "which",
        "into",
        "between",
        "through",
        "over",
        "under",
        "does",
    }
    words = re.findall(r"[a-zA-Z0-9]+", title)
    meaningful = [w for w in words if w.lower() not in stopwords and len(w) > 1]

    # Option 1: full (first 5 words)
    full_tag = "-".join(w.lower() for w in meaningful[:5])[:50]

    # Option 2: short (first 3 words)
    short_tag = "-".join(w.lower() for w in meaningful[:3])[:50]

    # Option 3: acronym-based — use uppercase words and acronyms from title
    acronyms = [
        w
        for w in re.findall(r"[A-Z][A-Z0-9]+|[A-Z][a-z]+", title)
        if w.lower() not in stopwords
    ]
    if len(acronyms) >= 2:
        acro_tag = "-".join(w if w.isupper() else w.lower() for w in acronyms[:4])[:50]
    else:
        # Fallback: first 4 words
        acro_tag = "-".join(w.lower() for w in meaningful[:4])[:50]

    # Deduplicate while preserving order
    seen = set()
    options = []
    for t in [full_tag, short_tag, acro_tag]:
        if t and t not in seen:
            seen.add(t)
            options.append(t)

    # Ensure at least 2 options
    if len(options) < 2:
        options.append("-".join(w.lower() for w in meaningful[:2])[:50])

    return options


# Returned by infer_topic() when no ArXiv category maps to a known research
# area. Callers must resolve this by asking the user - never write it to disk.
UNRESOLVED_TOPIC = "__unresolved__"

# Mapping from ArXiv category prefixes to human-readable topic names
CATEGORY_TOPIC_MAP = {
    "cs.AI": "artificial-intelligence",
    "cs.CL": "language-models",
    "cs.CV": "computer-vision",
    "cs.LG": "machine-learning",
    "cs.NE": "neural-networks",
    "cs.RO": "robotics",
    "cs.IR": "information-retrieval",
    "cs.MA": "multi-agent",
    "stat.ML": "machine-learning",
    "math.OC": "optimization",
    "q-bio": "computational-biology",
    "physics": "physics",
    "econ": "economics",
}


def infer_topic(categories: list[str], papers_folder: str | Path | None = None) -> str:
    """Infer a topic folder name from ArXiv categories.

    Tries every category in order (not just the primary): exact match first
    (e.g. cs.LG), then prefix match (e.g. q-bio.NC -> q-bio). Only returns a
    topic that maps to a real research area.

    Never invents a topic from a raw ArXiv category code. A paper whose
    categories map to nothing recognised (e.g. cs.CC, cs.CR, eess.IV) returns
    UNRESOLVED_TOPIC so the caller can ask the user instead of silently
    creating a 'cs-cc' folder.
    """
    if not categories:
        return UNRESOLVED_TOPIC

    existing = set(get_existing_topics(papers_folder)) if papers_folder else set()

    # Prefer a mapped topic that already exists in the database.
    mapped = []
    for cat in categories:
        if cat in CATEGORY_TOPIC_MAP:
            mapped.append(CATEGORY_TOPIC_MAP[cat])
        prefix = cat.split(".")[0]
        if prefix in CATEGORY_TOPIC_MAP:
            mapped.append(CATEGORY_TOPIC_MAP[prefix])

    for topic in mapped:
        if topic in existing:
            return topic
    if mapped:
        return mapped[0]
    return UNRESOLVED_TOPIC


def generate_bibtex(metadata: dict[str, Any], tag: str) -> str:
    """Generate a BibTeX entry for the paper."""
    authors_str = " and ".join(metadata["authors"])
    year = metadata["published"][:4]
    return (
        f"@article{{{tag},\n"
        f'  title={{{metadata["title"]}}},\n'
        f"  author={{{authors_str}}},\n"
        f'  journal={{arXiv preprint arXiv:{metadata["arxiv_id"]}}},\n'
        f"  year={{{year}}},\n"
        f'  url={{{metadata["url"]}}}\n'
        f"}}\n"
    )


def download_pdf(pdf_url: str, output_path: str | Path) -> None:
    """Download the PDF file from ArXiv."""
    print(f"Downloading PDF from {pdf_url}...", file=sys.stderr)
    response = _throttled_get(pdf_url, timeout=120, stream=True)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "wb") as f:
        for chunk in response.iter_content(chunk_size=8192):
            f.write(chunk)

    size_kb = output_path.stat().st_size / 1024
    print(f"PDF saved to {output_path} ({size_kb:.0f} KB)", file=sys.stderr)


def store_abstract(
    metadata: dict[str, Any], tag: str, topic: str, papers_folder: str | Path
) -> None:
    """Store abstract in the abstracts.json database."""
    db_path = Path(papers_folder) / "database" / "abstracts.json"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Load existing database or create new
    if db_path.exists():
        with open(db_path, "r", encoding="utf-8") as f:
            db = json.load(f)
    else:
        db = {"papers": {}}

    # Add/update paper entry
    db["papers"][tag] = {
        "title": metadata["title"],
        "authors": metadata["authors"],
        "abstract": metadata["abstract"],
        "arxiv_id": metadata["arxiv_id"],
        "url": metadata["url"],
        "published": metadata["published"],
        "updated": metadata["updated"],
        "version": metadata.get("version"),
        "categories": metadata["categories"],
        "topic": topic,
        "tag": tag,
        "added_date": datetime.now().isoformat(),
    }

    with open(db_path, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

    print(f"Abstract stored in {db_path}", file=sys.stderr)


def append_bibtex(bibtex_entry: str, papers_folder: str | Path) -> None:
    """Append BibTeX entry to references.bib."""
    bib_path = Path(papers_folder) / "references.bib"
    bib_path.parent.mkdir(parents=True, exist_ok=True)

    # Check if entry already exists
    if bib_path.exists():
        existing = bib_path.read_text(encoding="utf-8")
        key_match = re.search(r"@\w+\{(\S+),", bibtex_entry)
        if key_match and key_match.group(1) in existing:
            print(
                f"BibTeX entry '{key_match.group(1)}' already exists, skipping.",
                file=sys.stderr,
            )
            return

    with open(bib_path, "a", encoding="utf-8") as f:
        f.write("\n" + bibtex_entry)

    print(f"BibTeX entry appended to {bib_path}", file=sys.stderr)


def fix_mojibake(text: str) -> str:
    """Fix common UTF-8 mojibake from PDF text extraction.

    PDF extractors sometimes decode UTF-8 bytes as latin-1, producing
    garbled characters like Ã¤ instead of ä. This attempts to reverse that.
    """
    try:
        # Try the round-trip: if text has mojibake, encoding as latin-1
        # recovers the original UTF-8 bytes which we decode properly.
        fixed = text.encode("latin-1").decode("utf-8")
        return fixed
    except (UnicodeDecodeError, UnicodeEncodeError):
        # Not mojibake or mixed content — return as-is
        return text


def strip_trailing_sections(text: str) -> tuple[int, str | None]:
    """Find where References, Appendices, or Supplementary sections start.

    Returns (cut_position, section_name). cut_position is the char index
    where the first trailing section heading begins, or len(text) if none found.
    """
    pattern = re.compile(
        r"^(?:#{1,3}\s+|\d{1,2}\.?\s+)?"
        r"(?:References|Bibliography|Appendi(?:x|ces)"
        r"|Supplementary(?:\s+Material)?|Supplemental(?:\s+Material)?)"
        r"(?:\s+[A-Z])?"
        r"\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    match = pattern.search(text)
    if match:
        section_name = match.group(0).strip()
        return match.start(), section_name
    return len(text), None


def convert_pdf_to_markdown(
    pdf_path: str | Path, md_path: str | Path, engine: str = "markitdown"
) -> dict[str, Any]:
    """Convert PDF to markdown. Saves the full file and computes read_until_line.

    Returns dict with path, total stats, and read_until_line (the line number
    just before References/Appendices start). The agent should read only up to
    that line to skip trailing sections and save tokens.
    """
    md_path = Path(md_path)
    md_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Converting {pdf_path} to markdown using {engine}...", file=sys.stderr)

    if engine == "markitdown":
        try:
            from markitdown import MarkItDown
        except ImportError:
            print(
                "Warning: markitdown not installed, falling back to pypdf",
                file=sys.stderr,
            )
            engine = "pypdf"

    if engine == "markitdown":
        from markitdown import MarkItDown

        md = MarkItDown(enable_plugins=False)
        result = md.convert(str(pdf_path))
        text = result.text_content
    elif engine == "pypdf":
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        parts = []
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text()
            if page_text:
                parts.append(f"<!-- Page {i + 1} -->\n\n{page_text}")
        text = "\n\n---\n\n".join(parts) if parts else "<!-- No text extracted -->"
    else:
        print(f"Error: Unknown engine '{engine}'", file=sys.stderr)
        sys.exit(1)

    # Save full markdown (one file only)
    # Fix common UTF-8 mojibake from PDF extraction
    text = fix_mojibake(text)

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(text)
    total_lines = text.count("\n") + 1
    total_chars = len(text)
    print(
        f"Markdown saved: {md_path} ({total_lines} lines, {total_chars} chars)",
        file=sys.stderr,
    )

    # Compute where trailing sections (references/appendices) start
    cut_pos, section_name = strip_trailing_sections(text)
    if section_name:
        read_until_line = text[:cut_pos].count("\n")  # last content line before section
        skipped_chars = total_chars - cut_pos
        print(
            f"Trailing sections from '{section_name}' at line {read_until_line + 1} "
            f"({skipped_chars:,} chars). Agent should read lines 1-{read_until_line}.",
            file=sys.stderr,
        )
    else:
        read_until_line = total_lines
        skipped_chars = 0

    return {
        "path": str(md_path),
        "lines": total_lines,
        "chars": total_chars,
        "read_until_line": read_until_line,
        "skipped_section": section_name,
        "skipped_chars": skipped_chars,
    }


def get_existing_topics(papers_folder: str | Path) -> list[str]:
    """Scan the database folder and return a sorted list of existing topic names."""
    db_dir = Path(papers_folder) / "database"
    if not db_dir.exists():
        return []
    return sorted(
        d.name for d in db_dir.iterdir() if d.is_dir() and not d.name.startswith(".")
    )


def normalize_title(title: str | None) -> str:
    """Aggressively normalize a title for duplicate matching."""
    import unicodedata

    t = unicodedata.normalize("NFKD", title or "")
    t = re.sub(r"\$[^$]*\$", " ", t)  # strip inline math
    t = re.sub(r"\\[a-zA-Z]+|[{}]", " ", t)  # strip LaTeX macros/braces
    return re.sub(r"[^a-z0-9]+", "", t.lower())


def find_existing_paper(
    papers_folder: str | Path,
    *,
    tag: str,
    arxiv_id: str | None = None,
    title: str | None = None,
    doi: str | None = None,
) -> dict[str, Any] | None:
    """Find an existing entry for this paper stored under a DIFFERENT tag.

    Matches on ArXiv ID (version-insensitive), normalized title, or DOI. Returns
    None when there is no clash, or when the only match is the same tag - storing
    under the same tag is an intentional refresh, not a duplicate.

    This is the guard that prevents the same paper being added twice under two
    different tags, which `store_abstract` (keyed by tag) cannot detect on its own.
    """
    db_path = Path(papers_folder) / "database" / "abstracts.json"
    if not db_path.exists():
        return None
    with open(db_path, "r", encoding="utf-8") as f:
        papers = json.load(f).get("papers", {})

    base_id = re.sub(r"v\d+$", "", arxiv_id) if arxiv_id else None
    ntitle = normalize_title(title)
    ndoi = (doi or "").strip().lower().replace("https://doi.org/", "") or None

    for other, paper in papers.items():
        if other == tag:
            continue
        stored_id = paper.get("arxiv_id")
        if base_id and stored_id and re.sub(r"v\d+$", "", stored_id) == base_id:
            reason = "arxiv_id"
        elif ntitle and normalize_title(paper.get("title")) == ntitle:
            reason = "title"
        elif (
            ndoi
            and (paper.get("doi") or "").strip().lower().replace("https://doi.org/", "")
            == ndoi
        ):
            reason = "doi"
        else:
            continue
        return {
            "matched_on": reason,
            "existing_tag": other,
            "existing_topic": paper.get("topic"),
            "existing_title": paper.get("title"),
            "existing_arxiv_id": stored_id,
            "added_date": paper.get("added_date"),
        }
    return None


def check_duplicates(
    arxiv_id: str, papers_folder: str | Path, latest_version: int | None = None
) -> dict[str, Any] | None:
    """Check if a paper with this arxiv_id already exists in the database.

    Returns None if not found, or a dict with existing paper info.
    If latest_version is provided, compares with stored version.
    """
    db_path = Path(papers_folder) / "database" / "abstracts.json"
    if not db_path.exists():
        return None
    with open(db_path, "r", encoding="utf-8") as f:
        db = json.load(f)
    for tag, paper in db.get("papers", {}).items():
        if paper.get("arxiv_id") == arxiv_id:
            result = {
                "tag": tag,
                "topic": paper.get("topic", ""),
                "title": paper.get("title", ""),
                "added_date": paper.get("added_date", ""),
                "stored_version": paper.get("version"),
            }
            if latest_version and paper.get("version"):
                if latest_version > paper["version"]:
                    result["newer_version_available"] = True
                    result["latest_version"] = latest_version
            return result
    return None


def cmd_fetch(arxiv_id: str, config: dict[str, Any]) -> None:
    """Fetch and display paper metadata as JSON to stdout.

    Includes:
      - tag_options: list of 3 suggested tags (for the user to pick from)
      - suggested_topic: auto-inferred from ArXiv categories
      - existing_topics: list of topic folders already in the database
    """
    try:
        arxiv_id = extract_arxiv_id(arxiv_id)
        api_url = config.get("arxiv_api_url", "http://export.arxiv.org/api/query")
        metadata = fetch_metadata(arxiv_id, api_url)
        metadata["tag_options"] = generate_tag_options(metadata["title"])
        papers_folder = config["papers_folder"]
        existing_topics = get_existing_topics(papers_folder)
        suggested = infer_topic(metadata.get("categories", []), papers_folder)
        metadata["existing_topics"] = existing_topics

        # Never hand back a topic that would silently create a new folder.
        if suggested == UNRESOLVED_TOPIC or suggested not in existing_topics:
            metadata["suggested_topic"] = None
            metadata["topic_needs_user_choice"] = True
            metadata["topic_note"] = (
                f"ArXiv categories {metadata.get('categories', [])} do not map to an "
                "existing topic"
                + (
                    ""
                    if suggested == UNRESOLVED_TOPIC
                    else f" (closest unmapped guess: '{suggested}')"
                )
                + ". Ask the user to pick from existing_topics, or to confirm a new "
                "topic name explicitly. Do NOT invent one from the ArXiv category code."
            )
        else:
            metadata["suggested_topic"] = suggested
            metadata["topic_needs_user_choice"] = False

        # Check if paper is already in the database
        existing = check_duplicates(
            arxiv_id,
            config["papers_folder"],
            latest_version=metadata.get("version"),
        )
        if existing:
            metadata["already_in_database"] = existing

        print(json.dumps(metadata, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error fetching metadata: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_process(
    arxiv_id: str,
    tag: str,
    topic: str | None,
    config: dict[str, Any],
    engine: str | None = None,
    allow_new_topic: bool = False,
    force: bool = False,
) -> None:
    """One-shot command: fetch + store + convert.

    Auto-infers topic from ArXiv categories if not provided.
    Outputs JSON with all file paths so the agent knows exactly what was created.
    """
    try:
        arxiv_id = extract_arxiv_id(arxiv_id)
        papers_folder = config["papers_folder"]
        api_url = config.get("arxiv_api_url", "http://export.arxiv.org/api/query")
        resolved_engine: str = (
            engine if engine else (config.get("pdf_engine") or "markitdown")
        )

        # Fetch metadata
        metadata = fetch_metadata(arxiv_id, api_url)
        print(f"Paper: {metadata['title']}", file=sys.stderr)
        print(f"Authors: {', '.join(metadata['authors'])}", file=sys.stderr)

        # Auto-infer topic if not provided
        resolved_topic: str = (
            topic
            if topic
            else infer_topic(metadata.get("categories", []), papers_folder)
        )
        existing_topics = get_existing_topics(papers_folder)

        clash = find_existing_paper(
            papers_folder,
            tag=tag,
            arxiv_id=metadata.get("arxiv_id"),
            title=metadata.get("title"),
        )
        if clash and not force:
            print(
                json.dumps(
                    {
                        "error": "duplicate_paper",
                        "message": (
                            f"This paper is already in the database as "
                            f"'{clash['existing_tag']}' (matched on "
                            f"{clash['matched_on']}). Storing it under the new tag "
                            f"'{tag}' would create a second entry for the same paper. "
                            "Re-run with the existing tag to refresh it, or pass "
                            "--force if you really want a separate entry."
                        ),
                        "requested_tag": tag,
                        **clash,
                    },
                    indent=2,
                    ensure_ascii=False,
                )
            )
            sys.exit(3)

        if resolved_topic == UNRESOLVED_TOPIC:
            print(
                json.dumps(
                    {
                        "error": "unresolved_topic",
                        "message": (
                            "ArXiv categories "
                            f"{metadata.get('categories', [])} do not map to any known "
                            "research topic. Pick an existing topic with --topic, or "
                            "pass --allow-new-topic <name> if a genuinely new area is "
                            "warranted. Ask the user before creating a new topic."
                        ),
                        "categories": metadata.get("categories", []),
                        "existing_topics": existing_topics,
                        "title": metadata.get("title"),
                    },
                    indent=2,
                )
            )
            sys.exit(2)

        if resolved_topic not in existing_topics and not allow_new_topic:
            print(
                json.dumps(
                    {
                        "error": "new_topic_requires_confirmation",
                        "message": (
                            f"Topic '{resolved_topic}' does not exist yet. Creating "
                            "topic folders casually fragments the database. Reuse an "
                            "existing topic via --topic, or re-run with "
                            "--allow-new-topic to create it deliberately. Ask the "
                            "user before creating a new topic."
                        ),
                        "requested_topic": resolved_topic,
                        "existing_topics": existing_topics,
                        "title": metadata.get("title"),
                    },
                    indent=2,
                )
            )
            sys.exit(2)

        print(f"Topic: {resolved_topic}", file=sys.stderr)

        # Download PDF
        pdf_path = Path(papers_folder) / "pdf" / f"{tag}.pdf"
        download_pdf(metadata["pdf_url"], pdf_path)

        # Generate and append BibTeX
        bibtex = generate_bibtex(metadata, tag)
        append_bibtex(bibtex, papers_folder)

        # Store abstract
        store_abstract(metadata, tag, resolved_topic, papers_folder)

        # Create topic folder for summaries
        topic_folder = Path(papers_folder) / "database" / resolved_topic
        topic_folder.mkdir(parents=True, exist_ok=True)
        summary_path = topic_folder / f"{tag}_summary.md"

        # Convert PDF to markdown
        md_path = Path(papers_folder) / "md" / f"{tag}.md"
        md_info = convert_pdf_to_markdown(pdf_path, md_path, resolved_engine)

        # Rebuild citation graph (also captures cross-references for this paper)
        print("Rebuilding citation graph...", file=sys.stderr)
        graph = build_citation_graph(papers_folder)

        # Output structured JSON result to stdout
        result = {
            "status": "success",
            "tag": tag,
            "topic": resolved_topic,
            "arxiv_id": arxiv_id,
            "title": metadata["title"],
            "authors": metadata["authors"],
            "abstract": metadata["abstract"],
            "published": metadata["published"],
            "categories": metadata["categories"],
            "files": {
                "pdf": str(pdf_path),
                "markdown": str(md_path),
                "bibtex": str(Path(papers_folder) / "references.bib"),
                "abstracts_db": str(
                    Path(papers_folder) / "database" / "abstracts.json"
                ),
                "summary_target": str(summary_path),
            },
            "markdown_info": md_info,
        }

        cross_refs = graph.get("papers", {}).get(tag, {}).get("cites_in_database", [])
        if cross_refs:
            result["cites_in_database"] = cross_refs

        print(json.dumps(result, indent=2, ensure_ascii=False))
        print(
            f"\nDone! Paper '{tag}' fully processed under topic '{resolved_topic}'.",
            file=sys.stderr,
        )

    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


def extract_cited_arxiv_ids(md_path: str | Path) -> list[str]:
    """Extract ArXiv IDs from a paper's markdown file.

    Scans the full file for ArXiv ID patterns (including the references section).
    Returns a sorted deduplicated list of ArXiv IDs found.
    """
    md_path = Path(md_path)
    if not md_path.exists():
        return []
    text = md_path.read_text(encoding="utf-8")
    matches = re.findall(r"(\d{4}\.\d{4,5})", text)
    return sorted(set(matches))


def build_citation_graph(papers_folder: str | Path) -> dict[str, Any]:
    """Build citation graph by extracting ArXiv IDs from all stored paper markdowns.

    For each paper, scans its markdown file's references section for ArXiv IDs
    and cross-references them with papers in the database.

    Returns a dict mapping each tag to its citation info.
    Also saves citations.json in the database folder.
    """
    papers_folder = Path(papers_folder)
    db_path = papers_folder / "database" / "abstracts.json"
    if not db_path.exists():
        return {"papers": {}, "edges": []}

    with open(db_path, "r", encoding="utf-8") as f:
        db = json.load(f)

    # Build arxiv_id -> tag lookup
    id_to_tag = {}
    for tag, paper in db.get("papers", {}).items():
        aid = paper.get("arxiv_id")
        if aid:
            id_to_tag[aid] = tag

    graph = {"papers": {}, "edges": []}

    for tag, paper in db.get("papers", {}).items():
        md_path = papers_folder / "md" / f"{tag}.md"
        own_id = paper.get("arxiv_id", "")

        cited_ids = extract_cited_arxiv_ids(md_path)
        # Remove self-citations
        cited_ids = [cid for cid in cited_ids if cid != own_id]

        # Cross-reference with database
        cites_in_db = []
        cites_external = []
        for cid in cited_ids:
            if cid in id_to_tag:
                cites_in_db.append({"arxiv_id": cid, "tag": id_to_tag[cid]})
                graph["edges"].append({"from": tag, "to": id_to_tag[cid]})
            else:
                cites_external.append(cid)

        graph["papers"][tag] = {
            "arxiv_id": own_id,
            "title": paper.get("title", ""),
            "cites_in_database": cites_in_db,
            "cites_external_count": len(cites_external),
            "cited_by": [],  # filled below
        }

    # Fill cited_by (reverse edges)
    for edge in graph["edges"]:
        if edge["to"] in graph["papers"]:
            graph["papers"][edge["to"]]["cited_by"].append(edge["from"])

    # Save to disk
    citations_path = papers_folder / "database" / "citations.json"
    with open(citations_path, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2, ensure_ascii=False)

    return graph


def cmd_search(pattern: str, field: str, config: dict[str, Any]) -> None:
    """Search the local abstracts.json database by regex pattern.

    Fields: tag, title, author, abstract, topic, all (default).
    Output: JSON with matching papers sorted by relevance (title match first).
    """
    try:
        db_path = Path(config["papers_folder"]) / "database" / "abstracts.json"
        if not db_path.exists():
            print(
                json.dumps(
                    {"query": pattern, "field": field, "count": 0, "results": []}
                ),
            )
            return

        with open(db_path, "r", encoding="utf-8") as f:
            db = json.load(f)

        rx = re.compile(pattern, re.IGNORECASE)
        results = []

        for tag, paper in db.get("papers", {}).items():
            matched = False
            if field in ("tag", "all") and rx.search(tag):
                matched = True
            if (
                not matched
                and field in ("title", "all")
                and rx.search(paper.get("title", ""))
            ):
                matched = True
            if (
                not matched
                and field in ("topic", "all")
                and rx.search(paper.get("topic", ""))
            ):
                matched = True
            if not matched and field in ("author", "all"):
                if any(rx.search(a) for a in paper.get("authors", [])):
                    matched = True
            if (
                not matched
                and field in ("abstract", "all")
                and rx.search(paper.get("abstract", ""))
            ):
                matched = True

            if matched:
                results.append(
                    {
                        "tag": tag,
                        "title": paper.get("title", ""),
                        "authors": paper.get("authors", []),
                        "topic": paper.get("topic", ""),
                        "arxiv_id": paper.get("arxiv_id", ""),
                        "url": paper.get("url", ""),
                        "published": paper.get("published", "")[:10],
                        "abstract": paper.get("abstract", ""),
                    }
                )

        print(
            json.dumps(
                {
                    "query": pattern,
                    "field": field,
                    "count": len(results),
                    "results": results,
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    except re.error as e:
        print(f"Error: Invalid regex pattern: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error searching database: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_discover(query: str, config: dict[str, Any], max_results: int = 10) -> None:
    """Search ArXiv for papers matching a keyword query.

    Excludes papers already in the database by comparing ArXiv IDs.
    Returns results as JSON with metadata for each paper found.
    """
    try:
        papers_folder = Path(config["papers_folder"])
        api_url = config.get("arxiv_api_url", "http://export.arxiv.org/api/query")

        # Get existing ArXiv IDs to filter out
        existing_ids = set()
        db_path = papers_folder / "database" / "abstracts.json"
        if db_path.exists():
            with open(db_path, "r", encoding="utf-8") as f:
                db = json.load(f)
            for paper in db.get("papers", {}).values():
                aid = paper.get("arxiv_id")
                if aid:
                    existing_ids.add(aid)

        # Query ArXiv API (fetch extra to compensate for filtering)
        fetch_count = max_results + len(existing_ids) + 5
        params = {
            "search_query": f"all:{query}",
            "start": 0,
            "max_results": min(fetch_count, 50),
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        print(f"Searching ArXiv for: {query}...", file=sys.stderr)
        response = _throttled_get(api_url, params=params, timeout=30)

        root = ET.fromstring(response.text)
        results = []

        for entry in root.findall("atom:entry", ARXIV_NS):
            # Extract ArXiv ID
            entry_id = entry.find("atom:id", ARXIV_NS)
            if entry_id is None or entry_id.text is None:
                continue
            id_match = re.search(r"(\d{4}\.\d{4,5})", entry_id.text)
            if not id_match:
                continue
            arxiv_id = id_match.group(1)

            # Skip if already in database
            if arxiv_id in existing_ids:
                continue

            title_el = entry.find("atom:title", ARXIV_NS)
            if title_el is None or title_el.text is None:
                continue
            title = title_el.text.strip().replace("\n", " ").replace("  ", " ")

            # Skip ArXiv error entries
            if "Error" in title:
                continue

            abstract_el = entry.find("atom:summary", ARXIV_NS)
            abstract = (
                abstract_el.text.strip()
                if abstract_el is not None and abstract_el.text
                else ""
            )

            published_el = entry.find("atom:published", ARXIV_NS)
            published = (
                published_el.text.strip()
                if published_el is not None and published_el.text
                else ""
            )

            authors: list[str] = []
            for author in entry.findall("atom:author", ARXIV_NS):
                name_el = author.find("atom:name", ARXIV_NS)
                if name_el is not None and name_el.text:
                    authors.append(name_el.text.strip())

            categories: list[str] = [
                cat.get("term", "") for cat in entry.findall("atom:category", ARXIV_NS)
            ]

            results.append(
                {
                    "arxiv_id": arxiv_id,
                    "title": title,
                    "authors": authors[:5],  # Limit to first 5 for readability
                    "abstract": abstract[:500],  # Truncate long abstracts
                    "published": published[:10],
                    "categories": categories[:3],
                    "url": f"https://arxiv.org/abs/{arxiv_id}",
                }
            )

            if len(results) >= max_results:
                break

        output = {
            "query": query,
            "total_new_papers": len(results),
            "already_stored_count": len(existing_ids),
            "results": results,
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))

    except Exception as e:
        print(f"Error discovering papers: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_citations(config: dict[str, Any]) -> None:
    """Build and display the citation graph for all stored papers."""
    try:
        graph = build_citation_graph(config["papers_folder"])
        # Summarize
        total_papers = len(graph["papers"])
        total_edges = len(graph["edges"])
        papers_with_connections = sum(
            1
            for p in graph["papers"].values()
            if p["cites_in_database"] or p["cited_by"]
        )
        output = {
            "status": "success",
            "total_papers": total_papers,
            "total_citation_links": total_edges,
            "papers_with_connections": papers_with_connections,
            "citations_file": str(
                Path(config["papers_folder"]) / "database" / "citations.json"
            ),
            "graph": graph,
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error building citation graph: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    args = docopt(__doc__)

    # Config always resolved from skill root, unless explicitly overridden
    config_path = args["--config"] if args["--config"] else str(DEFAULT_CONFIG)
    config = load_config(config_path)

    if args["fetch"]:
        cmd_fetch(args["<arxiv_id>"], config)
    elif args["process"]:
        cmd_process(
            args["<arxiv_id>"],
            args["--tag"],
            args["--topic"],
            config,
            engine=args["--engine"],
            allow_new_topic=bool(args["--allow-new-topic"]),
            force=bool(args["--force"]),
        )
    elif args["discover"]:
        max_n = int(args["--max"]) if args["--max"] else 10
        cmd_discover(args["<query>"], config, max_results=max_n)
    elif args["search"]:
        field = args["--field"] if args["--field"] else "all"
        cmd_search(args["<pattern>"], field, config)
    elif args["citations"]:
        cmd_citations(config)


if __name__ == "__main__":
    main()
