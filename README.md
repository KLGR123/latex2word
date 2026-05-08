# latex2word

**English** | [中文](README_CN.md)

Convert LaTeX papers into translated Word documents — with consistent formatting and paragraph-level translation in one pass.

> **Actively maintained.** This project is under active development. Edge cases from real-world papers drive every improvement — the more papers people run through it, the more robust it becomes. Community contributions are very welcome.

## Why latex2word?

After publishing academic papers, researchers often need to submit a Word version — for graduate thesis requirements, institutional archives, or journal editorial systems that only accept `.docx`. The problem is that no existing tool handles both tasks well at the same time:

- **pandoc** is the standard LaTeX-to-Word converter, but its compatibility with real-world papers is poor. Complex environments (algorithms, custom theorem styles, CJK mixed text, multi-file projects) frequently break or produce malformed output.
- **Manual copy-paste** from a PDF is time-consuming and loses all structure.
- **Translation tools** work on plain text and have no concept of LaTeX structure, so figures, equations, and cross-references get destroyed.

latex2word is built specifically for this workflow: it understands LaTeX structure, preserves it through translation, and produces a properly formatted Word document in one command.

## Features

**Multi-paper, multi-chapter**
Process multiple papers in a single run. Each paper lives in a numbered subfolder; chapter numbers propagate automatically into figure labels, table labels, and section references (`图1-1`, `2.3节`, etc.).

**Bibliography merging and deduplication**
Citations from multiple papers are merged into a single reference list. Duplicate entries (same DOI or title) are detected and collapsed automatically — no manual cleanup needed.

**Paragraph-level translation with high concurrency**
Text is chunked at the paragraph level and sent to an LLM provider in parallel batches. Concurrency, batch size, and the provider/model are all configurable. Checkpointing lets you resume a long run without re-translating completed sections.

**Custom terminology**
Supply a glossary of domain-specific terms (e.g. proper nouns, abbreviations, field-specific phrases) that the translator must preserve or render in a specific way. Terms are injected into the translation prompt automatically.

**Rich element support**
The following LaTeX elements are rendered into the Word document without manual intervention:

| Element | How it is handled |
|---------|------------------|
| Figures | Embedded as images with captions |
| Tables | Converted to Word tables, including merged cells |
| Equations | Rendered via pandoc/MathML → OMML (native Word math) |
| Algorithms / pseudocode | Preserved as formatted code blocks |
| Footnotes | Carried through as Word footnotes |
| Cross-references | Resolved and rewritten (`\ref`, `\cite`, `\label`) |
| Theorem environments | Labeled with configurable Chinese display names |

**Multiple LLM providers**
Supports DeepSeek, OpenAI, Moonshot, and any OpenAI-compatible endpoint. Switch providers with a single flag.

**Fully configurable pipeline**
Every stage — chunking strategy, translation prompts, rendering styles, font sizes, label formats — can be overridden via `configs/pipeline.json` and `configs/rules.json` without touching source code.

---

## Install

### 1. Clone the repo and enter the directory

```bash
git clone https://github.com/KLGR123/latex2word.git
cd latex2word
```

### 2. Run the installer (recommended)

The installer creates a conda environment, installs all Python dependencies, installs pandoc, and generates a `secrets.env` template in one step:

```bash
bash install.sh
```

Then activate the environment:

```bash
conda activate latex2word
```

**Options:**

```bash
bash install.sh --no-pandoc   # skip pandoc installation
bash install.sh --no-python   # skip Python/conda setup
```

If conda is not available, the script falls back to installing into the current Python environment (Python 3.10+ required).

### 2 (alternative). Manual setup

<details>
<summary>Expand for manual steps</summary>

**Create and activate a conda environment:**

```bash
conda create -n latex2word python=3.11
conda activate latex2word
```

**Install Python dependencies:**

```bash
pip install -e .
```

**Install pandoc** (required for equation rendering):

| Platform | Command |
|----------|---------|
| macOS | `brew install pandoc` |
| Ubuntu / Debian | `sudo apt-get install pandoc` |
| Fedora | `sudo dnf install pandoc` |
| Windows / other | [pandoc.org/installing.html](https://pandoc.org/installing.html) |

</details>

### 3. Set your API key

Open `secrets.env` (created by the installer, or create it yourself) and paste your API key:

```bash
# secrets.env
OPENAI_API_KEY=sk-proj-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Only the key for the provider you intend to use needs to be filled in. For example, to use DeepSeek instead:

```bash
DEEPSEEK_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

Supported providers and their key names:

| Provider | Environment variable |
|----------|---------------------|
| OpenAI | `OPENAI_API_KEY` |
| DeepSeek | `DEEPSEEK_API_KEY` |
| Moonshot | `MOONSHOT_API_KEY` |
| Any OpenAI-compatible endpoint | `OPENAI_API_KEY` + `--base-url` flag |

## Input Layout

Put each paper in a numbered subfolder:

```text
inputs/
  1/
    main.tex
    references.bib
    figures/
  2/
    paper.tex
    paper.bbl
```

The folder name becomes the chapter number in labels such as `图1-1` and `1.2节`.

## Usage

Run the full pipeline:

```bash
python main.py --stage all
```

Or after editable install:

```bash
latex2word --stage all
```

Run one stage at a time:

```bash
python main.py --stage preprocess
python main.py --stage translate
python main.py --stage postprocess
```

Common overrides:

```bash
python main.py --provider deepseek --model deepseek-chat --concurrency 8
python main.py --inputs-dir inputs --outputs-dir outputs
python main.py --print-config
```

## Configuration

Pipeline behavior is configured in `configs/pipeline.json`.

Key sections:

| Section | Controls |
|---------|----------|
| `paths` | Input/output/config locations |
| `preprocess` | TeX inlining, bibliography parsing, chunking |
| `translate` | Provider, model, concurrency, glossary, checkpointing |
| `postprocess` | Reference replacement, DOCX rendering |

Rule-like settings live in `configs/rules.json`. Currently configurable rules include:

- `preprocess.chunk_block_envs` — LaTeX environments kept as a single chunk
- `translation.prompts` — system, glossary, and batch-response prompt text
- `translation.syntax.extra_patterns` — extra regexes that survive translation unchanged
- `translation.section_title_cache` — cached heading translations to skip API calls
- `translation.skip_envs` — environments that are not translated
- `postprocess.label_env_categories` — environment-to-label mappings (figure/table/math/code)
- `rendering.fonts`, `rendering.sizes`, `rendering.colors` — DOCX style defaults
- `rendering.math_env_labels` — Chinese display names for theorem-like environments
- `rendering.envs` — additional LaTeX environments for the DOCX renderer classifier

Rules are merged with safe built-in defaults, so you only need to specify what you want to override.

## Outputs

```text
outputs/
  chunks.json
  citations.json
  translated.json
  labeled.json
  refmap.json
  replaced.json
  final.docx
```

Intermediate JSON files are kept by default so you can resume from any stage. To clean up, pass `--cleanup-translated` / `--cleanup-chunks` etc., or set the relevant flags in `configs/pipeline.json`.

## Package Layout

```text
latex2word/
  preprocessing/    TeX inlining, macro expansion, formatting, bibliography, chunking
  translation/      LLM providers, batching, syntax preservation, section cache
  postprocessing/   Paragraph labeling, refmap building, reference replacement
  rendering/        DOCX rendering — figures, equations, tables, footnotes
```

## Contributing

We welcome bug reports and fixes — especially rendering edge cases discovered from real-world LaTeX papers. The more papers people run through this tool, the more robust it becomes.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the PR process.

## Join Us

Scan the QR code below to join our WeChat group for discussion, feedback, and community support.

<p align="center">
  <img src="assets/wechat_qr.png" width="200" alt="WeChat Group QR Code" />
</p>

If the QR code has expired, open an issue and we will post a fresh one.
