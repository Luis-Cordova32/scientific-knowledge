# Scientific Knowledge Skill

A [Claude Code / Copilot Agent](https://docs.claude.com/en/docs/claude-code) skill that
turns your assistant into a research-paper librarian. Give it an ArXiv URL, an ID, or a
local PDF, and it downloads, converts, summarizes, and files the paper into a personal
knowledge database organized by topic — with BibTeX references, citation graphs, and
critical, honest summaries.

Developed by **Jonathan Zea**.

## What it does

- **Download & process ArXiv papers** — fetch metadata, download the PDF, convert it to
  Markdown, append a BibTeX entry, and write a structured, critical summary.
- **Ingest local (non-ArXiv) PDFs** — journal or conference papers you already have on
  disk, using a BibTeX entry for metadata.
- **Organize by topic** — papers are filed into topic folders you control (or the skill
  infers from ArXiv categories), so the database stays navigable as it grows.
- **Track a searchable database** — `abstracts.json` indexes every paper; a local regex
  `search` command finds papers by tag, title, author, topic, or abstract without ever
  hand-parsing JSON.
- **Build a citation graph** — automatically extracts which stored papers cite each
  other, so you can see how ideas in your database connect.
- **Discover new papers** — search ArXiv by keyword, automatically excluding anything
  already in your database.
- **Synthesize across papers** — generate topic-level or cross-topic synthesis
  documents that surface shared assumptions, contradictions, and open gaps.
- **Preserve your notes** — every summary has a `## My Notes` section that survives
  regeneration, so your personal annotations are never overwritten.
- **Batch process** — a dedicated agent mode processes long paper lists unattended,
  without burning your main chat's context window.

## Requirements

- Python 3.10+
- Packages: `docopt`, `pyyaml`, `requests`, `markitdown`, `pypdf`

```bash
pip install -r requirements.txt
```

- Internet access (for the ArXiv API and PDF downloads)

## Installation

Clone or copy this folder into your skills directory, for example:

```
~/.claude/skills/scientific-knowledge/
```

Then set the environment variable your `config.yaml` points at (default: `PAPERS_DB`)
to wherever you want your paper database to live on this machine:

```bash
export PAPERS_DB="$HOME/papers/agent"      # macOS/Linux
setx PAPERS_DB "%USERPROFILE%\papers\agent" # Windows
```

`papers_folder` can itself be its own git repository if you want the database
version-controlled and synced separately from the skill.

## Configuration

All settings live in [`config.yaml`](config.yaml):

| Key | Description |
|---|---|
| `papers_folder` | Where papers, PDFs, markdown, and the database are stored. Use an environment variable reference — never a machine-specific absolute path — since this file is meant to be shared/tracked in git. |
| `arxiv_api_url` | ArXiv API endpoint (rarely needs changing). |
| `default_topic` | Fallback topic folder when none is specified. |
| `pdf_engine` | `markitdown` (default) or `pypdf` (fallback). |

## Usage

Once installed, just talk to your assistant naturally:

- *"Summarize this paper: https://arxiv.org/abs/2301.12345"*
- *"Add this PDF to my database, here's the BibTeX..."*
- *"Find papers in my database about sparse autoencoders"*
- *"Discover recent papers on mechanistic interpretability"*
- *"Give me a synthesis of everything I have on reinforcement learning"*

The assistant follows the workflows and rules defined in [`SKILL.md`](SKILL.md), which is
loaded automatically when the skill is relevant.

For batch processing many papers at once, use the dedicated **Scientific Knowledge**
agent mode instead of the main chat — it runs in its own isolated context window.

## Project structure

```
config.yaml              # Machine-specific configuration (env-var based)
SKILL.md                 # Instructions the assistant follows (workflows, rules, template)
requirements.txt          # Python dependencies
references/
  REFERENCE.md            # Full folder layout, database schema, and CLI reference
scripts/
  arxiv_download.py       # fetch / process / discover / search / citations (ArXiv)
  process_local.py        # process (local, non-ArXiv PDFs via BibTeX)
```

See [references/REFERENCE.md](references/REFERENCE.md) for the complete database schema
and CLI reference for both scripts.

## License

[MIT](LICENSE)

## Author

**Jonathan Zea**
