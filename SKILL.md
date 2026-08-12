---
name: scientific-knowledge
description: Download, convert, organize, and summarize scientific research papers from ArXiv. Manage a knowledge database of abstracts, BibTeX references, and detailed summaries organized by research topic. Use when the user mentions ArXiv papers, scientific literature, research summaries, paper management, or academic references.
compatibility: Requires Python 3.10+, pip packages docopt pyyaml requests markitdown pypdf, and internet access for ArXiv API.
metadata:
  author: Jonathan Zea
  version: "1.0"
---

# Scientific Knowledge Skill

## When to Use
- User shares an ArXiv URL or paper ID
- User asks to find, download, or read a scientific paper
- User wants to summarize or organize research papers
- User asks about previously stored papers or research topics
- User has a **local PDF of a non-ArXiv paper** (journal/conference) to add — see *Workflow: Process a Local (non-ArXiv) PDF*

## Configuration

All paths are defined in `config.yaml` in this skill's directory. The scripts load it
automatically — but read it once yourself to know the value of `papers_folder`, since
you will need it to construct file paths (e.g. to read `abstracts.json` or summaries).
`papers_folder` may contain environment variables (e.g. `$OneDriveConsumer`, `$HOME`, or
a custom one like `$PAPERS_DB`); the scripts expand them at runtime via
`os.path.expandvars()`. `config.yaml` is tracked in git and shared across machines, so
it must never contain a machine-specific absolute path — only an env var reference. Set
the matching environment variable once per machine, pointing it at wherever the papers
live there.

**`papers_folder` may itself be a standalone git repository** — check with
`git -C <papers_folder> rev-parse --is-inside-work-tree` rather than assuming. If it is:
after ingesting papers or writing summaries, tell the user the DB has uncommitted
changes (do not commit it yourself unless asked). Machine-specific facts like the exact
path, paper/summary counts, or the live topic list are **not** stable across devices —
discover them at runtime (list `database/`, read `abstracts.json`) instead of trusting
any count written down previously.

Install dependencies before first use:
```
pip install docopt pyyaml requests markitdown pypdf
```

## Script Location

All scripts live inside the skill directory itself:
```
~/.claude/skills/scientific-knowledge/scripts/
```
Always use the full path when running commands — **do not** run them relative to the workspace or project folder.

If the script is not found at this path, **stop and ask the user** where the `scientific-knowledge` skill folder is located before proceeding.

## Before First Use On A Machine: verify, do not assume

`papers_folder` in `config.yaml` points at an **environment variable** so the file
stays machine-agnostic in git. That variable must be set per machine. If it is
not, every lookup silently misses.

Run this once at the start of any session that touches the database:

```
python ~/.claude/skills/scientific-knowledge/scripts/arxiv_download.py search "sparse"
```

- A **normal JSON result with count > 0** means the database is reachable.
- **`Error: papers_folder is '$VAR' -- ... NOT SET`** (exit 2) means the env var
  is missing. Fix with `setx PAPERS_DB "<path>"` (Windows; opens in NEW shells
  only) or `export PAPERS_DB=...`, then retry.
- **`ModuleNotFoundError`** means you are using the wrong interpreter -- see below.

**Use the project's virtualenv interpreter, not bare `python`.** The skill's
dependencies (`requests`, `markitdown`, `pypdf`) are commonly installed only in a
project venv. On Deep Brain: `.venv/Scripts/python.exe`. A bare `python` may lack
`requests` and die at import.

## Command Execution Rules

- **Always run commands with `isBackground=false`** (foreground). These scripts finish in seconds, so foreground capture is always reliable — unlike long-running SAE training scripts which may need background.
- **Errors in the output ARE the output** — if the script prints a 429 rate limit error or any other error message, that is a real result, not a capture failure. Do not switch to background mode, temp files, or `2>&1` redirects in response to a script error.
- **Run commands directly** — never redirect output to a temp file (`> /tmp/...`), chain with `cat`, or **pipe the JSON into another parser** (`| python -c ...`, `| jq`). Read the JSON directly from the terminal output. A parser in the pipeline swallows tracebacks and turns a crash into what looks like an empty result.
- **Never suppress stderr** (`2>/dev/null`) and do not use `2>&1` redirects. Errors ARE the output; a hidden `ModuleNotFoundError` or config error is indistinguishable from "no matches" once stderr is discarded. This is exactly how a paper that WAS in the database got reported as absent.
- **`count: 0` is a claim that needs corroboration, not a conclusion.** Before telling the user a paper is absent, confirm the database is reachable (a control search that SHOULD hit, e.g. `search "sparse"`), and try the tag, the title, and the first author separately.
- **Rate limit (429):** wait 10–15 seconds, then retry the same foreground command once. If it fails again, report the error to the user and stop.

## Workflow: Download and Process a Paper

Given an ArXiv URL or ID (e.g., `https://arxiv.org/abs/2301.12345` or `2301.12345`):

1. **Fetch metadata and show the user:**
   ```
   python ~/.claude/skills/scientific-knowledge/scripts/arxiv_download.py fetch <arxiv_id>
   ```
   Show the title, authors, and abstract. The JSON output includes:
   - `tag_options`: list of 3 suggested tags — present these as **selectable options**
   - `suggested_topic`: auto-inferred from ArXiv categories
   - `existing_topics`: list of topic folders already in the database
   - `version`: the ArXiv version number (integer, e.g. 1, 2, 7)
   - `already_in_database`: (only if duplicate) existing tag, topic, title, date, and version info

   **If `already_in_database` is present**, verify that ALL expected files actually exist
   before reporting the paper as fully stored. Use the `tag` and `topic` from the
   `already_in_database` object to check each path under `papers_folder`:

   | File | Expected path |
   |---|---|
   | PDF | `pdf/<tag>.pdf` |
   | Markdown | `md/<tag>.md` |
   | Summary | `database/<topic>/<tag>_summary.md` |
   | Abstract entry | `database/abstracts.json` (already confirmed by `already_in_database`) |

   - If **all files exist**: tell the user the paper is already fully stored and ask if they want to re-process or skip.
   - If **any file is missing**: tell the user which files are missing and automatically re-process the paper (no need to ask).
   - If `already_in_database.newer_version_available` is `true`, inform the user that
     a newer version exists (v`stored_version` → v`latest_version`) and offer to
     re-download and re-process it.

   **Ask the user to pick a tag and topic** using the `AskUserQuestion` tool (Claude Code;
   in a VS Code / Copilot host the equivalent is `vscode/askQuestions`):
   - **Tag question** (`header: "Tag"`): options = `tag_options`, first one labelled
     "(Recommended)". "Other" already covers a custom tag — do not add one.
     Prefer a tag that includes the **first author's surname** or a **distinctive title
     token** over a generic descriptive phrase (e.g. `karvonen-sae-boardgames`, not
     `sae-evaluation-metrics`) — an opaque tag is hard to find later even when
     `search` works correctly.
   - **Topic question** (`header: "Topic"`): put `suggested_topic` first, labelled
     "(Recommended)", then the closest entries from `existing_topics`. `AskUserQuestion`
     allows **at most 4 options per question**, so pick the 3 closest existing topics
     rather than dumping the full list; "Other" covers a brand-new topic.
   Ask both questions in a **single** `AskUserQuestion` call (two entries in `questions`).

2. **Process the paper** (everything in one command):
   ```
   python ~/.claude/skills/scientific-knowledge/scripts/arxiv_download.py process <arxiv_id> --tag <tag> --topic <topic>
   ```
   This automatically:
   - Downloads the PDF to `pdf/<tag>.pdf`
   - Appends BibTeX to `references.bib`
   - Adds abstract to `database/abstracts.json`
   - Converts PDF to markdown at `md/<tag>.md` (with UTF-8 encoding fix)
   - Outputs JSON with all file paths, abstract, and `summary_target` path
   - `markdown_info.read_until_line` tells you where references/appendices start
   - If `--topic` is omitted, auto-infers from ArXiv categories

   **If `process` fails** (non-zero exit code or error in output):
   - Retry **once** with `--engine pypdf` (fallback converter):
     ```
     python ~/.claude/skills/scientific-knowledge/scripts/arxiv_download.py process <arxiv_id> --tag <tag> --topic <topic> --engine pypdf
     ```
   - If that also fails, **stop and report the error to the user.** Do not retry further.

   **Guard exit codes.** `process` refuses rather than corrupting the database. Both
   guards run *before* anything is written, so a refusal leaves no partial state:

   | Exit | `error` | Meaning | What to do |
   |---|---|---|---|
   | 3 | `duplicate_paper` | This paper already exists under a **different tag** (matched on ArXiv ID, normalized title, or DOI). | Do **not** retry with another tag. Re-run with `existing_tag` to refresh it, or skip. `--force` only if the user explicitly wants a second entry. |
   | 2 | `new_topic_requires_confirmation` | `--topic` names a folder that does not exist. | Pick from `existing_topics`. Ask the user before adding `--allow-new-topic`. |
   | 2 | `unresolved_topic` | ArXiv categories map to no known topic. | Ask the user to choose from `existing_topics`. **Never** invent a topic from a category code (`cs.CC` → `cs-cc` is exactly the bug this prevents). |

3. **Read the markdown** at `files.markdown` up to line `markdown_info.read_until_line`.
   This skips references and appendices. The full file remains on disk if needed.

4. **Write a summary** to `files.summary_target` using the template below.

The only user interaction is picking the tag and topic in step 1.

## Workflow: Process a Local (non-ArXiv) PDF

For papers that are **not on ArXiv** (journal/conference PDFs you already have on disk),
use `process_local.py` instead of `arxiv_download.py`. Metadata comes from a **BibTeX
entry** — a `.bib` file or pasted inline text — rather than the ArXiv API.

```
python ~/.claude/skills/scientific-knowledge/scripts/process_local.py process <pdf_path> --bib <bibfile> [--tag <tag>] [--topic <topic>]
```

- `--bib <bibfile>` — path to a `.bib` file with ONE entry. Use `--bib -` to read pasted
  BibTeX from stdin, or `--bibtext "<entry>"` to pass it inline.
- Include an `abstract = {...}` field in the BibTeX for a complete database entry; only
  `title` is strictly required. `doi`, `author`, `year`, `journal` are used if present.
- `--tag` defaults to a slug from the title; `--topic` defaults to the config `default_topic`.

This copies the PDF to `pdf/<tag>.pdf`, appends the BibTeX to `references.bib`, adds an
`abstracts.json` entry (`arxiv_id: null`, plus `doi`/`journal`/`source: "local-pdf"`/`bibtex_key`),
converts the PDF to `md/<tag>.md`, and prints the same JSON result as the ArXiv `process`
(with `summary_target` and `markdown_info.read_until_line`). **Then read the markdown and
write the summary to `summary_target` exactly as for ArXiv papers** (same template, step 4).

If `process` fails on conversion, retry once with `--engine pypdf`. Local papers are skipped
by the citation graph's ID matching (no ArXiv ID) but remain fully searchable via
`arxiv_download.py search`.

**Duplicate and topic guards apply here too** — same exit codes as the ArXiv `process`
(3 = `duplicate_paper`, 2 = topic problem), and they run before anything is written.
Because a local paper has no ArXiv ID, the duplicate check matches on **normalized title**
(case, punctuation, LaTeX macros and inline math are stripped) and on **DOI** (with or
without the `https://doi.org/` prefix). If you get exit 3, the paper is already stored —
re-run with `existing_tag` to refresh it rather than inventing a new tag.

## Workflow: Access Knowledge Database

- **Find a paper / answer a question about stored papers**: Use the local search command — never parse `abstracts.json` manually:
  ```
  python scripts/arxiv_download.py search "<pattern>" [--field tag|title|author|topic|abstract]
  ```
  Omit `--field` to search all fields. `<pattern>` is a case-insensitive regex. Output is JSON with matching papers (tag, title, authors, topic, arxiv_id, abstract).
- **Find papers by topic**: List folders under `<papers_folder>/database/`, or `search "<topic>" --field topic`.
  ⚠️ **A paper's `topic` is where it is FILED, not what it is about.** The canonical
  board-game SAE paper (Karvonen et al., *Measuring Progress in Dictionary Learning
  ... with Board Game Models*) is filed under `sparse-autoencoders`, not `board-games`.
  **Never conclude a paper is absent from browsing one topic folder** — always use
  `search` across all fields, and try title words, the tag, and the author surname.
- **Read a summary**: Read `<papers_folder>/database/<topic>/<tag>_summary.md`
- **Read the full paper**: Read `<papers_folder>/md/<tag>.md`
- **Get references**: Read `<papers_folder>/references.bib` (one entry per ingested paper)
- **Read a cross-topic synthesis**: `<papers_folder>/synthesis/` — see *Workflow: Topic Synthesis*
- **Check the ingestion queue**: `<papers_folder>/papers-to-review.md` — a batched, resumable
  queue with **pre-assigned tag + topic** per row and a `Status` column
  (`pending` / `done` / `failed` / `skipped`). When it exists, its tag/topic **override**
  the skill's `tag_options[0]` / `suggested_topic` defaults, and you must update the
  `Status` cell after each paper so the batch is resumable.
- **`pending.bib`** is a *staging* file (bulk BibTeX imported from a systematic-literature
  review, ~212 entries), **not** the ingested set. Do not read it to answer "what is in the
  database" — use `search` / `abstracts.json` for that.

## Workflow: Batch Processing (Multiple Papers)

> **Use the dedicated agent mode for batch work.** Open the `Scientific Knowledge` agent
> (from `scientific-knowledge.agent.md`) instead of running from the main chat. It has its
> own isolated context window — processing 20+ papers won't consume your main chat context.

When the user provides a list of paper IDs to process without continuous interaction,
the agent must execute the loop itself by calling tools one at a time.

**Critical rules:**
- **Do NOT write a helper script** to process the batch. Execute the steps directly.
- **Do NOT ask the user for confirmation** at any point during batch processing.
- **Do NOT batch multiple `fetch` calls into a single shell command or script.**
- **Do NOT retry a failed command more than once** — try `--engine pypdf` once, then move on.
- Process papers one by one: fetch → process → write summary → next paper.

For each paper ID in the list (deduplicated):

1. Run `fetch <arxiv_id>` to get metadata
2. If `already_in_database` is present, verify all files exist (`pdf/<tag>.pdf`, `md/<tag>.md`, `database/<topic>/<tag>_summary.md`). If all exist, skip silently. If any are missing, re-process **using the existing tag from `already_in_database`** — never a freshly generated one.
3. Pick `tag_options[0]` as the tag — no user confirmation
4. Use `suggested_topic` as the topic — no user confirmation. If `topic_needs_user_choice` is `true`, `suggested_topic` will be `null`: pick the closest entry from `existing_topics` and note the choice in the final report. **Never** create a new topic during a batch run.
5. Run `process <arxiv_id> --tag <tag> --topic <topic>`
   - If it fails, retry **once** with `--engine pypdf`
   - **Exit 3 (`duplicate_paper`) is not a failure to retry** — the paper is already stored under `existing_tag`. Count it as skipped and move on. Never re-run with a different tag to get around it, and never pass `--force` in a batch.
   - If that also fails, mark as failed and **move on to the next paper immediately** — do not retry further
6. Write the summary to `files.summary_target` using the template
7. Move immediately to the next paper

Report a final summary to the user **only at the end**:
- How many were processed / skipped / failed
- List of summary files written
- Any cross-references found (`cites_in_database` in process output)

## Workflow: Discover New Papers

When the user asks to find papers on a topic or research area:

```
python scripts/arxiv_download.py discover "<keywords>" --max <n>
```

This searches ArXiv and **automatically excludes papers already in the database**.
Present the results to the user with title, authors, abstract snippet, and ArXiv ID.
The user can then pick which ones to process with the normal download workflow.

## Workflow: Topic Synthesis

When the user asks to compare papers, understand a research area, or get an overview of a topic:

1. Read all summary files under `<papers_folder>/database/<topic>/`
2. Read `<papers_folder>/database/citations.json` to understand citation relationships
3. Write the synthesis with:
   - Shared assumptions across papers
   - Contradictions or tensions between results
   - Evolution of ideas (which paper builds on which, using citation data)
   - Open gaps that no paper addresses
   - A citation mini-map showing connections between papers in this topic

**Where it goes — two conventions, pick by scope:**

- **Single-topic, throwaway view** → `<papers_folder>/database/<topic>/_synthesis.md`,
  always regenerated from scratch (a cached view, not permanent state).
- **Cross-topic / dated snapshot** → `<papers_folder>/synthesis/YYYY-MM-DD_<slug>.md`.
  Prefer this convention (when a `synthesis/` folder already exists on this machine, or
  the user asks for one) for any synthesis a research document will cite. Read
  `synthesis/README.md` first, then:
  - Frontmatter records **date, scope** (which folders + the selection rule),
    **corpus counts**, and the **DB git commit** (`git -C <papers_folder> rev-parse --short HEAD`)
    so the snapshot is reproducible and diffable against a later run.
  - Reference papers by DB tag in `[[double-brackets]]`, resolving to
    `database/<topic>/<tag>_summary.md`.
  - These files are **never overwritten** — a refresh is a new dated file that explicitly
    says it *supersedes* the previous one, and both get a line in `synthesis/README.md`.

## Workflow: Citation Graph

To rebuild the full citation graph across all stored papers:

```
python scripts/arxiv_download.py citations
```

This mechanically extracts ArXiv IDs from each paper's references section (regex, no LLM)
and cross-references them with the database. Saves `database/citations.json`.

The `process` command also does this automatically for each new paper — if the JSON output
contains `cites_in_database`, tell the user which stored papers are cited by the new one.

## Writing Summaries — Preserving Personal Notes

1. Before writing, check if the summary file (`<papers_folder>/database/<topic>/<tag>_summary.md`) already exists.
2. If it exists, read it and look for a `## My Notes` section at the end.
3. If found, copy everything from `## My Notes` to the end of the file.
4. Write the new summary content using `create_file`.
5. Append the preserved `## My Notes` block at the end.

If no `## My Notes` section exists yet, append an empty one after writing:

```markdown
## My Notes
<!-- Add your personal annotations, connections, and ideas here. This section is preserved on regeneration. -->
```

**`Summarized by`** must be the actual model identifier you are running as (e.g. `Claude Sonnet 5`,
`GPT-5`) — never a placeholder or the agent/tool name. If a summary is regenerated by a
different model later, overwrite this field with the new model's name so it always
reflects whoever wrote the current content.

## Summary File Template

Use this structure when creating summary files:

```markdown
# <Paper Title>

**Authors:** <comma-separated authors>
**ArXiv ID:** <id>
**Date:** <published date>
**Version:** v<version>
**Topic:** <topic>
**Tag:** <tag>
**Summarized by:** <LLM model name that wrote this summary>

## Key Findings
- Main result 1
- Main result 2

## Methods
- Methodology description
- Key techniques used
- Datasets and scale (number of samples, parameters, compute)

## Results
- Quantitative results with specific numbers
- Comparisons with baselines (which baselines? are they strong/recent?)
- Statistical significance if reported (confidence intervals, p-values, variance across runs)

## Critical Assessment

Evaluate the paper honestly. Research often has a positive bias — authors may omit negative results, cherry-pick metrics, or overclaim. Flag any of these:

- **Overclaimed conclusions**: Do the results actually support the claims? Are conclusions hedged appropriately or inflated?
- **Missing baselines or comparisons**: Are important baselines absent? Are comparisons fair (same compute, data, hyperparameter tuning)?
- **Cherry-picked results**: Are only the best metrics/datasets shown? Is there evidence of selective reporting?
- **Reproducibility concerns**: Is there enough detail to reproduce? Code/data available? Compute requirements realistic?
- **Scale and generalization**: Do results hold beyond the specific setup? Small-scale only? Single domain?
- **Statistical rigor**: Are results averaged over multiple runs? Error bars reported? Or single-run numbers?
- **Limitations acknowledged**: Does the paper discuss its own limitations honestly, or are they buried/omitted?
- **Potential confounds**: Could simpler explanations account for the results?

## Conclusions
- Main conclusions (as stated by authors)
- Future work directions

## Relevance & Notes
- Why this paper matters for the research topic
- Connections to other papers in the database
- Open questions this raises
```

## Reference

See [references/REFERENCE.md](references/REFERENCE.md) for the complete folder structure, database schema, and script CLI reference.