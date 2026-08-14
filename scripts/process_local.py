"""Local PDF Processor (non-ArXiv papers).

Process a LOCAL PDF into the knowledge database using BibTeX metadata, mirroring
`arxiv_download.py process` but without any ArXiv download. Use this for papers
that are not on ArXiv (journal/conference PDFs you already have on disk).

Metadata comes from a BibTeX entry — either a .bib file or pasted inline text.
The richer the BibTeX (include an `abstract = {...}` field), the better the
database entry; only `title` is strictly required.

Usage:
    process_local.py process <pdf_path> --bib <bibfile> [--tag <tag>] [--topic <topic>] [--engine <engine>] [--config <config>] [--allow-new-topic] [--force]
    process_local.py process <pdf_path> --bibtext <text> [--tag <tag>] [--topic <topic>] [--engine <engine>] [--config <config>] [--allow-new-topic] [--force]
    process_local.py (-h | --help)

Commands:
    process         Copy PDF into the store, parse BibTeX, append to references.bib,
                    add a (non-ArXiv) entry to abstracts.json, convert PDF -> markdown,
                    rebuild the citation graph, and print a JSON result with a
                    `summary_target` path (write the summary there, as for ArXiv papers).

Options:
    -h --help          Show this help message
    --bib <bibfile>    Path to a .bib file containing ONE entry (use - to read stdin / pasted text).
    --bibtext <text>   Inline BibTeX entry text (alternative to --bib).
    --tag <tag>        Short tag (filename + key). Default: derived from the title.
    --topic <topic>    Topic folder for the summary. Default: config `default_topic`.
                       Must already exist unless --allow-new-topic is passed.
    --allow-new-topic  Permit creating a topic folder that does not exist yet. Ask the user first.
    --force            Store even if this paper is already in the database under another tag.
    --engine <engine>  Conversion engine: markitdown or pypdf (config default if omitted).
    --config <config>  Path to config.yaml (auto-resolved from skill dir if omitted).

Notes:
    * The abstracts.json entry has `arxiv_id: null` and adds `doi`, `journal`,
      `source: "local-pdf"`, and `bibtex_key`. The skill's `search` command works
      unchanged; the citation graph skips null ArXiv IDs automatically.
"""

from __future__ import annotations

"""
Python 3
06 / 08 / 2026
@author: z_tjona

"I find that I don't understand things unless I try to program them."
-Donald E. Knuth

"Either mathematics is too big for the human mind or the human mind is more than a machine."
-Kurt Godël
"""

import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from docopt import docopt

# Reuse the ArXiv script's helpers (same scripts/ folder)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from arxiv_download import (  # noqa: E402
    DEFAULT_CONFIG,
    append_bibtex,
    build_citation_graph,
    convert_pdf_to_markdown,
    find_existing_paper,
    generate_tag_options,
    load_config,
)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


def parse_bibtex(text: str) -> tuple[str, str, dict[str, str]]:
    """Parse a single BibTeX entry into (entrytype, key, fields).

    Handles brace-delimited `{...}` and quote-delimited `"..."` field values,
    with balanced-brace counting so nested braces survive.
    """
    text = text.strip()
    m = re.search(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", text)
    if not m:
        raise ValueError("Could not find a BibTeX entry (expected '@type{key, ...}').")
    entrytype, key = m.group(1).lower(), m.group(2).strip()
    fields: dict[str, str] = {}
    i = m.end()
    n = len(text)
    while i < n:
        fm = re.match(r"\s*([a-zA-Z][a-zA-Z\-]*)\s*=\s*", text[i:])
        if not fm:
            break
        name = fm.group(1).lower()
        i += fm.end()
        if i >= n:
            break
        ch = text[i]
        if ch == "{":
            depth, start = 0, i
            while i < n:
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        i += 1
                        break
                i += 1
            val = text[start + 1 : i - 1]
        elif ch == '"':
            start = i + 1
            i += 1
            while i < n and text[i] != '"':
                i += 1
            val = text[start:i]
            i += 1
        else:  # bare value (number, etc.)
            start = i
            while i < n and text[i] not in ",}":
                i += 1
            val = text[start:i].strip()
        fields[name] = re.sub(r"\s+", " ", val.replace("\n", " ")).strip()
        cm = re.match(r"\s*,", text[i:])
        if cm:
            i += cm.end()
        else:
            break
    return entrytype, key, fields


def _clean(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[{}]", "", s)).strip()


def metadata_from_bib(fields: dict[str, str]) -> dict[str, Any]:
    """Map BibTeX fields to the metadata dict used by the database."""
    raw_authors = _clean(fields.get("author", ""))
    authors: list[str] = []
    for a in re.split(r"\s+and\s+", raw_authors):
        a = a.strip()
        if not a:
            continue
        if "," in a:  # "Last, First" -> "First Last"
            last, first = (p.strip() for p in a.split(",", 1))
            authors.append(f"{first} {last}".strip())
        else:
            authors.append(a)
    doi = _clean(fields.get("doi", ""))
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    url = _clean(fields.get("url", "")) or (f"https://doi.org/{doi}" if doi else "")
    return {
        "title": _clean(fields.get("title", "")),
        "authors": authors,
        "abstract": _clean(fields.get("abstract", "")),
        "doi": doi,
        "url": url,
        "journal": _clean(fields.get("journal", "") or fields.get("booktitle", "")),
        "year": _clean(fields.get("year", ""))[:4],
    }


def store_abstract_local(
    meta: dict[str, Any], tag: str, topic: str, key: str, papers_folder: str | Path
) -> Path:
    """Add/refresh a non-ArXiv paper entry in abstracts.json."""
    db_path = Path(papers_folder) / "database" / "abstracts.json"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = (
        json.loads(db_path.read_text(encoding="utf-8"))
        if db_path.exists()
        else {"papers": {}}
    )
    db["papers"][tag] = {
        "title": meta["title"],
        "authors": meta["authors"],
        "abstract": meta["abstract"],
        "arxiv_id": None,
        "doi": meta["doi"],
        "url": meta["url"],
        "journal": meta["journal"],
        "published": meta["year"],
        "updated": meta["year"],
        "version": None,
        "categories": [],
        "topic": topic,
        "tag": tag,
        "source": "local-pdf",
        "bibtex_key": key,
        "added_date": datetime.now().isoformat(),
    }
    db_path.write_text(json.dumps(db, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"Abstract stored in {db_path} (source=local-pdf, arxiv_id=null)",
        file=sys.stderr,
    )
    return db_path


def cmd_process(
    pdf_path: str,
    bib_text: str,
    tag: str | None,
    topic: str | None,
    config: dict[str, Any],
    engine: str | None,
    allow_new_topic: bool = False,
    force: bool = False,
) -> None:
    papers_folder = config["papers_folder"]
    entrytype, key, fields = parse_bibtex(bib_text)
    meta = metadata_from_bib(fields)
    if not meta["title"]:
        raise ValueError("BibTeX entry has no 'title' field.")

    resolved_tag = tag or generate_tag_options(meta["title"])[0]
    resolved_topic = topic or config.get("default_topic") or "general"

    # Local papers have no ArXiv ID, so title/DOI matching is the only defence
    # against storing the same paper twice under two different tags.
    clash = find_existing_paper(
        papers_folder,
        tag=resolved_tag,
        title=meta["title"],
        doi=meta.get("doi"),
    )
    if clash and not force:
        print(
            json.dumps(
                {
                    "error": "duplicate_paper",
                    "message": (
                        f"This paper is already in the database as "
                        f"'{clash['existing_tag']}' (matched on {clash['matched_on']}). "
                        f"Storing it under the new tag '{resolved_tag}' would create a "
                        "second entry for the same paper. Re-run with that tag to "
                        "refresh it, or pass --force for a separate entry."
                    ),
                    "requested_tag": resolved_tag,
                    **clash,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        sys.exit(3)

    db_dir = Path(papers_folder) / "database"
    existing_topics = (
        sorted(
            d.name
            for d in db_dir.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
        if db_dir.exists()
        else []
    )
    if resolved_topic not in existing_topics and not allow_new_topic:
        print(
            json.dumps(
                {
                    "error": "new_topic_requires_confirmation",
                    "message": (
                        f"Topic '{resolved_topic}' does not exist yet. Creating topic "
                        "folders casually fragments the database. Reuse an existing "
                        "topic via --topic, or re-run with --allow-new-topic to create "
                        "it deliberately. Ask the user before creating a new topic."
                    ),
                    "requested_topic": resolved_topic,
                    "existing_topics": existing_topics,
                    "title": meta["title"],
                },
                indent=2,
            )
        )
        sys.exit(2)

    print(f"Paper: {meta['title']}", file=sys.stderr)
    print(f"Authors: {', '.join(meta['authors'])}", file=sys.stderr)
    print(f"Tag: {resolved_tag} | Topic: {resolved_topic}", file=sys.stderr)

    src = Path(pdf_path)
    if not src.exists():
        raise ValueError(f"PDF not found: {src}")
    dst_pdf = Path(papers_folder) / "pdf" / f"{resolved_tag}.pdf"
    dst_pdf.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst_pdf)
    print(f"PDF copied to {dst_pdf}", file=sys.stderr)

    append_bibtex(bib_text.strip() + "\n", papers_folder)
    abs_db = store_abstract_local(
        meta, resolved_tag, resolved_topic, key, papers_folder
    )

    topic_folder = Path(papers_folder) / "database" / resolved_topic
    topic_folder.mkdir(parents=True, exist_ok=True)
    summary_path = topic_folder / f"{resolved_tag}_summary.md"

    md_path = Path(papers_folder) / "md" / f"{resolved_tag}.md"
    resolved_engine = engine or config.get("pdf_engine") or "markitdown"
    md_info = convert_pdf_to_markdown(dst_pdf, md_path, resolved_engine)

    print("Rebuilding citation graph...", file=sys.stderr)
    build_citation_graph(papers_folder)

    result = {
        "status": "success",
        "tag": resolved_tag,
        "topic": resolved_topic,
        "arxiv_id": None,
        "doi": meta["doi"],
        "title": meta["title"],
        "authors": meta["authors"],
        "abstract": meta["abstract"],
        "published": meta["year"],
        "journal": meta["journal"],
        "source": "local-pdf",
        "bibtex_key": key,
        "files": {
            "pdf": str(dst_pdf),
            "markdown": str(md_path),
            "bibtex": str(Path(papers_folder) / "references.bib"),
            "abstracts_db": str(abs_db),
            "summary_target": str(summary_path),
        },
        "markdown_info": md_info,
    }
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(
        f"\nDone! Local paper '{resolved_tag}' processed under topic '{resolved_topic}'.",
        file=sys.stderr,
    )


def main() -> None:
    args = docopt(__doc__)
    config = load_config(args["--config"] if args["--config"] else str(DEFAULT_CONFIG))
    if args["process"]:
        if args["--bibtext"]:
            bib_text = args["--bibtext"]
        else:
            bp = args["--bib"]
            bib_text = (
                sys.stdin.read() if bp == "-" else Path(bp).read_text(encoding="utf-8")
            )
        try:
            cmd_process(
                args["<pdf_path>"],
                bib_text,
                args["--tag"],
                args["--topic"],
                config,
                args["--engine"],
                allow_new_topic=bool(args["--allow-new-topic"]),
                force=bool(args["--force"]),
            )
        except Exception as e:  # noqa: BLE001
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
