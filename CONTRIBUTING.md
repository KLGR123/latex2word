# Contributing to latex2word

**English** | [中文](CONTRIBUTING_CN.md)

Thank you for helping make latex2word more robust. The more real-world LaTeX papers people run through this tool, the more edge cases get discovered and fixed — that is how the library improves.

This guide covers how to contribute a **rendering bug fix**, which is the most common and most valuable type of contribution.

---

## What we are looking for

We especially welcome fixes for:

- Incorrect or missing rendering in the output Word document (equations, tables, figures, footnotes, cross-references)
- LaTeX environments or commands that are not handled or crash the pipeline
- Translation artifacts that break document structure
- Bibliography / citation edge cases

Changes to translation prompts, provider logic, or configuration defaults require extra discussion — please open an issue first before submitting a PR for those.

---

## Before you start

1. **Search existing issues and PRs** to make sure the bug has not already been reported or fixed.
2. If you are unsure whether something is a bug or intended behavior, open an issue to discuss it first.
3. Keep one PR focused on one bug. Do not bundle unrelated fixes.

---

## How to submit a bug fix PR

Every bug fix PR must include the following four parts. PRs missing any part will be asked to revise before review.

### 1. Screenshot — the broken output

Attach a screenshot of the affected section in the Word document (`final.docx`) showing what is wrong.

- Crop to the relevant paragraph or element; full-page screenshots are hard to review.
- If the issue is subtle, annotate the screenshot (circle or arrow) to point out the problem.

### 2. Minimal LaTeX example

Provide the shortest LaTeX snippet that reproduces the issue. Ideally a single `\begin{...}...\end{...}` block or a few lines of source — not an entire paper.

```latex
% Example: table with merged cells that renders incorrectly
\begin{tabular}{|c|c|}
  \hline
  \multicolumn{2}{|c|}{Header} \\
  \hline
  A & B \\
  \hline
\end{tabular}
```

If the bug only appears with a specific paper and you cannot reduce it, attach or link the relevant `.tex` file.

### 3. Code fix with explanation

Describe what was wrong and how your fix addresses it. Reference the specific file and function you changed.

Example:

> `latex2word/rendering/table.py` — `_render_merged_cell()` was not accounting for `\multicolumn` spans greater than 2, causing the cell content to be dropped. Fixed by iterating over the full span width before advancing the column index.

Keep the fix minimal. Do not refactor surrounding code or rename variables unless directly related to the bug.

### 4. Screenshot — the fixed output

Attach a screenshot of the same section after applying your fix, showing the correct rendering.

Use the same crop and zoom level as the "broken" screenshot so reviewers can compare them side by side.

---

## PR title format

Use the `fix:` prefix and describe the broken behavior concisely:

```
fix: multicolumn table cells dropped when span > 2
fix: \footnote inside figure caption crashes postprocessing
fix: numbered list resets to 1 after every paragraph
```

---

## Code style

- Follow the conventions of the file you are editing.
- Do not add comments that describe what the code does — only add one if the *why* is non-obvious.
- Do not introduce new dependencies without prior discussion.

---

## What happens after you open a PR

1. A maintainer will review the screenshots and the fix within a few days.
2. If changes are needed you will receive comments on the PR.
3. Once approved, the PR is merged into `main`.

Thank you for contributing.
