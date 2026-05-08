# latex2word

**English** | [中文](README_CN.md)

Convert LaTeX papers into translated Word documents.

Put your papers under `inputs/`, run the pipeline, and get a translated `final.docx` under `outputs/`.

## Install

Install Python dependencies (requires Python 3.10+):

```bash
python -m pip install -e .
```

[pandoc](https://pandoc.org/installing.html) is also required for equation rendering.

Set your API key in `secrets.env`:

```bash
DEEPSEEK_API_KEY=...
# or: OPENAI_API_KEY=..., MOONSHOT_API_KEY=..., etc.
```

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

We welcome bug reports and fixes — especially rendering edge cases discovered from real-world LaTeX papers.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the PR process.
