# latex2word

[English](README.md) | **中文**

将 LaTeX 论文转换为翻译好的 Word 文档。

将论文放入 `inputs/` 目录，运行流水线，在 `outputs/` 下获得翻译完成的 `final.docx`。

## 安装

安装 Python 依赖（需要 Python 3.10+）：

```bash
python -m pip install -e .
```

渲染数学公式还需要安装 [pandoc](https://pandoc.org/installing.html)。

在 `secrets.env` 中填入 API Key：

```bash
DEEPSEEK_API_KEY=...
# 也可以：OPENAI_API_KEY=..., MOONSHOT_API_KEY=..., 等
```

## 输入目录结构

将每篇论文放在一个编号子目录下：

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

文件夹名称会作为章节号，用于生成标签，如 `图1-1`、`1.2节`。

## 使用方法

运行完整流水线：

```bash
python main.py --stage all
```

可编辑安装后，等价的命令行工具也可使用：

```bash
latex2word --stage all
```

单独运行某一阶段：

```bash
python main.py --stage preprocess
python main.py --stage translate
python main.py --stage postprocess
```

常用参数覆盖：

```bash
python main.py --provider deepseek --model deepseek-chat --concurrency 8
python main.py --inputs-dir inputs --outputs-dir outputs
python main.py --print-config
```

## 配置说明

流水线行为通过 `configs/pipeline.json` 配置。

主要配置区块：

| 区块 | 控制内容 |
|------|----------|
| `paths` | 输入/输出/配置目录路径 |
| `preprocess` | TeX 内联、参考文献解析、分块 |
| `translate` | 模型提供商、并发数、术语表、断点续传 |
| `postprocess` | 引用替换、DOCX 渲染 |

规则类配置位于 `configs/rules.json`，目前可配置的规则包括：

- `preprocess.chunk_block_envs` — 作为整块保留的 LaTeX 环境
- `translation.prompts` — 系统提示词、术语表提示词、批量响应提示词
- `translation.syntax.extra_patterns` — 翻译过程中必须保持不变的额外正则表达式
- `translation.section_title_cache` — 缓存的章节标题翻译，避免重复 API 调用
- `translation.skip_envs` — 不进行翻译的 LaTeX 环境
- `postprocess.label_env_categories` — 环境到标签的映射（图/表/公式/代码）
- `rendering.fonts`、`rendering.sizes`、`rendering.colors` — DOCX 样式默认值
- `rendering.math_env_labels` — 定理类环境的中文显示名称
- `rendering.envs` — DOCX 渲染器分类器使用的额外 LaTeX 环境

规则会与内置默认值合并，只需指定需要覆盖的部分即可。

## 输出文件

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

默认保留中间 JSON 文件，方便从任意阶段断点续跑。如需清理，可传入 `--cleanup-translated` / `--cleanup-chunks` 等参数，或在 `configs/pipeline.json` 中开启对应选项。

## 包结构

```text
latex2word/
  preprocessing/    TeX 内联、宏展开、格式处理、参考文献解析、分块
  translation/      LLM 提供商、批量请求、语法保护、章节缓存
  postprocessing/   段落标记、引用映射构建、交叉引用替换
  rendering/        DOCX 渲染 — 图片、公式、表格、脚注
```

## 参与贡献

欢迎提交 Bug 报告与修复，尤其是在真实 LaTeX 论文中发现的渲染边缘情况。

请参阅 [CONTRIBUTING_CN.md](CONTRIBUTING_CN.md) 了解 PR 流程规范。
