# DataPrism 架构设计文档

版本：0.1 草案
日期：2026-09-19
状态：待评审

---

## 1. 目标与约束

### 1.1 目标

在内网离线、仅 CPU 的环境中，把一个工程会话内导入的多个 Excel、Word、PDF 文件（含直接选择文件夹递归采集），自动整理为用户可选字段的结构化数据，并以多种格式导出。

关键能力：

1. 多文件、多格式、递归文件夹采集，文件夹名和文件名可作为字段来源。
2. 自动发现「所有可用字段」，包括表格列、键值对、长文本中的属性、路径元数据。
3. 用户选定字段后自动抽取、清洗、合并、校验、导出。
4. 数据源不限于表格。整份 doc 或 pdf 可能就是长文本，长文本抽取是第一等公民，不是表格的附属路径。
5. 每个输出单元格可回溯到源文件的具体位置。

### 1.2 硬约束

| 约束 | 含义 | 对设计的直接影响 |
|---|---|---|
| 目标平台为统信 UOS v20 | Debian 10 底座，GLIBC 2.28，系统 Python 3.7，海光 C86 或兆芯 KX 系列 x86_64 CPU，国产显卡无通用计算能力 | 所有 Linux 轮子的 manylinux 标签不得高于 2.28；llama-server 必须在同构环境本地编译；开发机可为 Windows，但部署验证以 UOS 为准 |
| 离线内网 | 无法在线下载模型、依赖、前端资源 | 所有依赖打成 wheelhouse，模型与前端静态资源随包分发 |
| 仅 CPU | 4B 级模型预填充约每秒数百 token，生成约每秒十余 token | LLM 只看压缩摘要与小块文本，全部调用受 token 预算约束并缓存 |
| 模型上限 4B | 推理与长上下文能力有限 | 确定性解析优先，LLM 只做归并命名、歧义裁决、小块抽取；输出必须受 JSON Schema 约束 |
| 嵌入模型 bge-small-zh-v1.5 | 512 token 上限，中文为主，约 95 MB | 只用于字段名与短文本相似度，不做长文档检索 |
| 模型统一目录且可自定义 | 运行时不得硬编码模型路径 | 模型注册表加目录解析优先级，见第 8 节 |
| OCR 后置 | MinerU 2.5 作为扫描件与图片的预留方案 | 解析器协议与中间表示现在就预留 OCR 字段，实现留空 |
| 界面为 WebUI | 浏览器访问，无桌面 GUI | 后端 FastAPI，前端资源本地打包，禁用一切外部 CDN |

### 1.3 非目标

- 不做在线服务化、多租户、权限系统。
- 不做通用 RAG 问答。
- 不做模型训练或微调。
- 第一阶段不做扫描件、图片、pptx。

---

## 2. 总体架构

### 2.1 分层视图

```
┌────────────────────────────────────────────────────────────────┐
│  WebUI (浏览器)                                                 │
│  文件导入 → 解析进度 → 字段审阅 → 记录粒度 → 预览与审阅 → 导出   │
├────────────────────────────────────────────────────────────────┤
│  API 层 (FastAPI)          任务队列 (进程内 worker)              │
├────────────────────────────────────────────────────────────────┤
│  应用服务层                                                     │
│  Session  Ingest  Parse  Discover  Plan  Extract  Clean  Export │
├───────────────┬───────────────────────┬────────────────────────┤
│  解析器集合    │  字段发现与抽取引擎     │  清洗 / 合并 / 校验      │
│  excel docx   │  结构候选 键值候选      │  类型化 归一 去重        │
│  pdf csv txt  │  文本候选 路径候选      │  Pandera 校验            │
│  (ocr 预留)   │  归并 命名 映射计划     │                        │
├───────────────┴───────────────────────┴────────────────────────┤
│  基础设施层                                                     │
│  DocumentIR  Provenance  ModelRegistry  LLMService  Embedding   │
│  Workspace(DuckDB)  Cache  Config  Logging                       │
├────────────────────────────────────────────────────────────────┤
│  外部进程与模型                                                 │
│  llama-server (Qwen 4B GGUF)  onnxruntime (bge-small-zh)        │
│  LibreOffice headless (旧格式转换, 可选)  MinerU (预留)          │
└────────────────────────────────────────────────────────────────┘
```

### 2.2 数据流

```
文件/文件夹
   │ Ingest: 遍历、识别、哈希、路径元数据
   ▼
SourceFile 清单 ──────────────────────────────┐
   │ Parse: 每种格式一个解析器                  │
   ▼                                          │
DocumentIR (块 + 溯源)                          │
   │ Segment: 长文本切块                         │
   ▼                                          │
Chunk 集合                                     │
   │ Discover: 四类候选来源 → 归并 → 命名        │
   ▼                                          │
CandidateField 列表 ◄── 用户选择、改名、合并 ────┤ WebUI
   │ Plan: 记录粒度 + 字段 → MappingPlan          │
   ▼                                          │
MappingPlan                                    │
   │ Extract: 结构映射 / 键值抽取 / 文本抽取       │
   ▼                                          │
Record 集合 (每个值带 Evidence)                  │
   │ Clean → Merge → Validate                   │
   ▼                                          │
输出表 + 溯源表 ◄── 审阅、修正 ──────────────────┘
   │ Export
   ▼
xlsx / csv / parquet / jsonl / sqlite / markdown
```

### 2.3 设计原则

1. 确定性优先。任何能用规则做对的事都不交给模型。
2. 模型输入压缩。模型只看表头加样本行、短文本块、候选簇摘要，永远不看整份文件。
3. 输出受约束。模型输出一律走 JSON Schema 约束解码，再经 Pydantic 校验。
4. 证据强制。文本抽取的每个值必须附原文片段，片段在源文本中定位不到就判无效。
5. 一切可缓存、可续跑。模型调用按输入哈希缓存，解析结果落库，会话可中断后继续。
6. 一切可回溯。从输出单元格到源文件位置的链路必须完整。
7. 解析器与模型可替换。新格式、新模型、新 OCR 只增加实现，不改上层。

---

## 3. 目录结构

本项目是应用而非可分发的库，采用扁平应用布局，不使用 `src/` 布局，不配置构建后端，不打包发布。运行方式为 `uv run python main.py` 或 `uv run python -m app`。

```
DataPrism/
├── pyproject.toml                # 无 build-system，[tool.uv] package = false
├── main.py                       # 启动入口：解析参数，启动 WebUI 或执行 CLI 子命令
├── dataprism.toml                # 默认配置，可被用户目录与命令行覆盖
├── docs/
│   └── architecture.md
├── models/                       # 默认模型目录，可通过配置覆盖
│   ├── models.toml               # 模型注册表
│   ├── llm/
│   │   └── qwen3.5-4b-q4_k_m.gguf
│   ├── embedding/
│   │   └── bge-small-zh-v1.5/    # onnx 模型与 tokenizer
│   ├── layout/                   # docling 布局与表格模型 (可选)
│   └── ocr/                      # MinerU 2.5 模型 (预留)
├── bin/
│   ├── README.md
│   ├── linux-x86_64/llama-server      # UOS 本地编译产物，不入库
│   └── windows-x86_64/llama-server.exe # 开发机用官方预编译包，不入库
├── .github/
│   ├── docker/debian10.Dockerfile     # 模拟 UOS v20 底座的验证镜像
│   └── workflows/                     # 见 12.1 节
│       ├── build-llama-server.yml
│       ├── wheelhouse.yml
│       ├── offline-smoke.yml
│       └── ocr-manual.yml
├── scripts/
│   ├── build_llama_server.sh          # 在 manylinux_2_28 容器中编译
│   ├── check_glibc_symbols.sh         # 校验二进制引用的 GLIBC 符号版本
│   ├── check_wheel_tags.py            # 校验轮子 manylinux 标签 ≤ 2.28
│   ├── import_check.py                # 离线逐个导入已安装模块
│   ├── ci_download_models.sh          # 联网阶段下载冒烟模型
│   ├── ci_offline_smoke.sh            # 断网容器内的冒烟总控
│   ├── smoke_llm.py  smoke_embedding.py  smoke_docling.py
│   ├── check_no_external_requests.py  # NiceGUI 离线资源扫描
│   ├── make_fixture_pdf.py            # 生成夹具 PDF
│   └── smoke-requirements.txt         # 冒烟专用依赖，后续并入 pyproject
├── app/                          # 应用代码根，按领域分子包
│   ├── __init__.py
│   ├── __main__.py               # python -m app 入口，转调 main.py 逻辑
│   ├── cli.py                    # 命令行子命令
│   ├── config.py                 # 配置加载与目录解析
│   ├── core/                     # 核心数据模型
│   │   ├── ir.py                 # DocumentIR 与块类型
│   │   ├── provenance.py         # 溯源与证据
│   │   ├── fields.py             # CandidateField, FieldSchema
│   │   ├── plan.py               # MappingPlan
│   │   └── records.py            # Record, CellValue
│   ├── workspace/                # 会话与持久化 (DuckDB)
│   │   ├── session.py
│   │   ├── store.py
│   │   └── schema.sql
│   ├── ingest/
│   │   ├── crawler.py            # 递归遍历与文件识别
│   │   └── path_meta.py          # 路径元数据抽取
│   ├── parsers/
│   │   ├── base.py               # Parser 协议与注册
│   │   ├── excel.py
│   │   ├── docx.py
│   │   ├── pdf.py
│   │   ├── text.py               # csv, txt, md
│   │   ├── legacy.py             # doc, xls 转换
│   │   └── ocr.py                # 预留，MinerU 适配
│   ├── segment/
│   │   └── chunker.py            # 长文本切块
│   ├── discover/
│   │   ├── table_region.py       # 表区检测与表头识别
│   │   ├── kv_pattern.py         # 键值模式识别
│   │   ├── text_induce.py        # 长文本属性归纳 (LLM)
│   │   ├── path_fields.py        # 路径候选
│   │   ├── typing.py             # 值类型签名推断
│   │   ├── merge.py              # 跨文件候选归并
│   │   └── naming.py             # 规范命名 (LLM)
│   ├── extract/
│   │   ├── structural.py         # 表格列映射
│   │   ├── kv.py                 # 键值抽取
│   │   ├── text.py               # 长文本字段抽取 (LLM)
│   │   └── reconcile.py          # 同一记录多来源冲突合并
│   ├── clean/
│   │   ├── normalize.py          # 日期、数值、金额、电话、证件、全半角
│   │   └── types.py              # 目标类型转换
│   ├── merge/
│   │   ├── concat.py             # 纵向拼接与列对齐
│   │   └── dedup.py              # 精确与模糊去重
│   ├── validate/
│   │   └── schema.py             # Pandera 校验
│   ├── export/
│   │   ├── base.py
│   │   ├── xlsx.py  csv.py  parquet.py  jsonl.py  sqlite.py  markdown.py
│   ├── llm/
│   │   ├── registry.py           # 模型注册表
│   │   ├── server.py             # llama-server 生命周期
│   │   ├── client.py             # 约束解码、重试、超时
│   │   ├── budget.py             # token 预算与调用配额
│   │   ├── cache.py              # 调用缓存
│   │   └── prompts/              # 提示模板
│   ├── embedding/
│   │   └── bge.py                # onnxruntime 推理
│   ├── api/                      # FastAPI 路由
│   └── webui/                    # 前端页面与本地静态资源
├── data/                         # 本地样例与会话目录，加入 .gitignore
└── tests/
    ├── fixtures/                 # 样例文件
    └── ...
```

`pyproject.toml` 需要相应调整：删除 `[build-system]` 与 `[project.scripts]`，加入 `[tool.uv] package = false`，依赖分组用 `[dependency-groups]` 管理 `dev`、`docling`、`ocr` 等可选组。仓库里现有的 `src/dataprism/__init__.py` 是 `uv init --package` 生成的空壳，切换布局时一并移除。

包名用 `app` 而不是 `dataprism`，是为了与项目名区分，避免日后真要抽出可复用库时发生命名冲突。子包按领域划分，跨领域只允许依赖 `core` 与 `config`，`api` 与 `cli` 是唯一的组合层。

---

## 4. 核心数据模型

所有模型用 Pydantic 定义，可序列化为 JSON 存入 DuckDB。

### 4.1 SourceFile

```python
class SourceFile(BaseModel):
    file_id: str            # sha256 前 16 位 + 相对路径哈希
    root: Path              # 用户选择的根目录
    rel_path: Path          # 相对路径
    kind: Literal["xlsx", "xls", "csv", "docx", "doc", "pdf", "txt", "md", "image", "unknown"]
    size: int
    mtime: datetime
    sha256: str
    path_tokens: list[str]  # 文件夹层级名 + 文件名切分
    parse_status: Literal["pending", "ok", "failed", "skipped"]
    parse_error: str | None
```

### 4.2 DocumentIR 与块

```python
class Provenance(BaseModel):
    file_id: str
    locator: str            # "sheet=销售明细!A1:F20" / "page=3" / "para=12" / "table=2,r=4,c=1"
    page: int | None
    bbox: tuple[float, float, float, float] | None   # PDF 与 OCR 用
    char_span: tuple[int, int] | None                # 文本块内偏移
    ocr_confidence: float | None                     # OCR 预留

class Block(BaseModel):
    block_id: str
    kind: Literal["table", "paragraph", "heading", "kv", "list", "image"]
    order: int              # 文档内阅读顺序
    prov: Provenance

class TableBlock(Block):
    cells: list[list[str | None]]        # 展开合并区域后的网格
    merges: list[tuple[int, int, int, int]]
    header_rows: list[int]               # 识别出的表头行索引，可能为空
    caption: str | None                  # 上方最近标题或段落
    orientation: Literal["vertical", "kv", "unknown"]

class ParagraphBlock(Block):
    text: str
    heading_path: list[str]              # 所在的标题层级路径
    style: str | None

class HeadingBlock(Block):
    text: str
    level: int

class KVBlock(Block):
    pairs: list[tuple[str, str]]         # 段落或表格中识别出的键值对
    source_block_id: str

class DocumentIR(BaseModel):
    file_id: str
    title: str | None
    blocks: list[Block]
    meta: dict[str, str]                 # 作者、创建时间等文档属性
    parser: str
    parser_version: str
```

### 4.3 Chunk

```python
class Chunk(BaseModel):
    chunk_id: str
    file_id: str
    block_ids: list[str]
    heading_path: list[str]
    text: str
    token_count: int
    char_offset_map: list[tuple[int, str, int]]   # (chunk 内偏移, block_id, block 内偏移)
```

### 4.4 CandidateField

```python
class TypeSignature(BaseModel):
    primary: Literal["date", "datetime", "int", "float", "money", "percent",
                     "phone", "id_card", "code", "enum", "bool", "text", "long_text"]
    confidence: float
    enum_values: list[str] | None
    stats: dict[str, float]     # 空值率、唯一率、长度分布等

class CandidateField(BaseModel):
    cand_id: str
    canonical_name: str         # 规范名，可被用户修改
    aliases: list[str]          # 各文件中的原始名
    source_kind: Literal["table_column", "kv", "text_attr", "path"]
    type_sig: TypeSignature
    samples: list[str]          # 最多 5 个去重样本
    coverage_files: int
    coverage_rows: int
    confidence: float
    members: list[CandidateMember]   # 每个来源的具体位置
    merged_from: list[str]      # 归并前的候选 id

class CandidateMember(BaseModel):
    file_id: str
    block_id: str | None
    column_index: int | None    # 表格列
    kv_key: str | None          # 键值对的键
    path_level: int | None      # 路径层级
    text_attr: str | None       # 文本归纳出的属性名
```

### 4.5 MappingPlan

```python
class Granularity(BaseModel):
    unit: Literal["table_row", "document", "section", "entity"]
    section_level: int | None          # unit=section 时按几级标题切
    entity_name: str | None            # unit=entity 时的实体描述，如 "一个合同"

class FieldTarget(BaseModel):
    field_id: str
    name: str
    target_type: str
    required: bool
    cand_ids: list[str]

class SourceMapping(BaseModel):
    file_id: str
    block_id: str | None
    strategy: Literal["column", "kv", "text_llm", "path", "constant"]
    detail: dict                       # 列索引、键名、路径层级、提示模板 id

class MappingPlan(BaseModel):
    plan_id: str
    granularity: Granularity
    fields: list[FieldTarget]
    mappings: dict[str, list[SourceMapping]]   # field_id -> 各文件的映射
    created_at: datetime
    version: int
```

### 4.6 Record 与 CellValue

```python
class Evidence(BaseModel):
    prov: Provenance
    quote: str | None           # 文本抽取的原文片段
    method: Literal["column", "kv", "text_llm", "path", "user_edit"]
    confidence: float

class CellValue(BaseModel):
    raw: str | None
    normalized: Any | None
    evidences: list[Evidence]   # 多来源时保留全部
    status: Literal["ok", "conflict", "invalid", "missing", "edited"]

class Record(BaseModel):
    record_id: str
    file_id: str
    unit_locator: str           # 行号、章节路径或实体序号
    cells: dict[str, CellValue] # field_id -> 值
```

---

## 5. 模块设计

### 5.1 Workspace 会话

- 一个会话对应一个目录，内含 `session.duckdb`、`cache/`、`exports/`。
- DuckDB 表：`source_files`、`documents`（IR JSON）、`chunks`、`candidates`、`plans`、`records`、`cells`、`llm_calls`（缓存）、`events`（审计）。
- 所有阶段幂等，重跑只处理状态为 pending 或输入哈希变化的对象。
- 会话可导出为单个 zip，含计划与溯源，便于在另一台内网机器复现。

### 5.2 Ingest 采集

- 递归遍历用户指定的一个或多个根目录，同时接受单文件列表。
- 文件类型判断以魔数为准，扩展名为辅。伪装扩展名记录为警告。
- 忽略规则：临时文件 `~$*`、隐藏目录、超过阈值的大文件、用户配置的排除模式。
- 路径元数据：把根目录以下每级文件夹名与文件名主干分别切分。切分规则为分隔符、中英数字边界、常见噪声词过滤（最终版、副本、修改、新建）。识别年份、年月、季度、部门、地区、编号、版本号等模式并打类型签名。
- 相同哈希的文件只解析一次，但保留多个路径记录。

### 5.3 Parsers 解析器

协议：

```python
class Parser(Protocol):
    name: str
    version: str
    supported_kinds: frozenset[str]
    def can_parse(self, f: SourceFile) -> bool: ...
    def parse(self, f: SourceFile, ctx: ParseContext) -> DocumentIR: ...
```

`ParseContext` 携带模型目录、临时目录、超时、是否允许调用外部进程等。解析器通过注册表按 `kind` 与优先级选择，失败时按优先级回退到下一个。

**Excel 解析器**

- 用 openpyxl 读取结构信息：合并区域、隐藏 sheet、隐藏行列、单元格样式中的加粗与填充、数字格式。用 python-calamine 快速读取值，避免大文件 openpyxl 过慢。两者按单元格坐标对齐。
- 日期序列号按 sheet 的日期系统转换。公式单元格取缓存值，无缓存值时标记为空并记录警告。
- 每个 sheet 生成一个或多个 TableBlock。表区切分见 5.5。
- 单元格内换行、批注不进入网格，批注存到块的 meta。

**Word 解析器**

- 用 python-docx 按文档顺序遍历段落与表格，保持阅读顺序。标题样式转为 HeadingBlock，维护 heading_path。
- 表格合并单元格通过 python-docx 的底层 XML 判断 gridSpan 与 vMerge，展开为网格并记录 merges。
- 嵌套表格扁平化为独立 TableBlock，并在 meta 中记录父表位置。
- 跨页断开的表格不在解析层拼接，交给 5.5 按列数与表头相似度拼接。
- 页眉页脚、脚注默认忽略，配置可开启。

**PDF 解析器**

- 默认后端为 Docling，MIT 协议。理由：政务公文与招投标文件中无框线表格、三线表、合并单元格常见，pdfplumber 基于坐标投影的表格抽取在这些场景下会列漂移与多行撕裂；Docling 的版面模型与 TableFormer 做拓扑重构，表格保真度明显更高。代价是 CPU 每页约 2.5 到 4.5 秒（海光 C86 估算，需实测），大批量文档需多进程并发。
- Docling 的版面与表格模型放在模型目录 `layout/` 下，通过 `artifacts_path` 显式传入，不依赖环境变量与网络。OCR 引擎选 RapidOCR 配 onnxruntime，不用 tesserocr。
- 快速路径：pdfplumber 作为轻量预扫描与备选后端。预扫描每页统计是否有文本层、是否有表格候选、文本是否乱码，据此决定路由。纯文本且无表格的页面可走 pdfplumber 以节省时间，配置项可关闭快速路径强制全部走 Docling。
- 乱码检测由本项目实现，Docling 与 pdfplumber 都不会自动处理。规则：统计页面文本中 CJK 与 ASCII 可打印字符的占比，私有区、替换字符、控制字符或极高比例的孤立单字节占比超过阈值判为乱码。判为乱码的页面标记 `needs_ocr`，在 Docling 中对该页强制整页 OCR。这类 PDF 通常来自旧虚拟打印机或缺失 ToUnicode CMap，与操作系统字库无关，换字体不能解决。
- 整页无文本层判为扫描页，同样标记 `needs_ocr`。第一阶段这些页面用 Docling 内置 RapidOCR 处理，复杂混排的扫描件留给后续 MinerU 扩展。
- PyMuPDF 不作为默认依赖，因其 AGPL 协议需要单独合规评估。

**文本解析器**

- csv 自动探测编码（utf-8、gbk、gb18030）与分隔符，生成单个 TableBlock。
- txt 与 md 按空行与标题标记切段。

**旧格式转换**

- doc 与 xls 通过 LibreOffice 无头模式转换为 docx 与 xlsx 后复用现有解析器。LibreOffice 是可选外部依赖，未安装时这些文件标记为 skipped 并提示。xls 也可用 xlrd 2.x 直接读取，作为无 LibreOffice 时的降级路径。

**OCR 解析器（预留）**

- 目标方案 MinerU 2.5。它提供两种后端：传统 pipeline 后端（布局检测、公式、表格、OCR 多个小模型组合）和 1.2B 参数的 VLM 后端。无 GPU 的 UOS 机器上只能用 `--backend pipeline --device cpu`，每页约 8 到 18 秒；VLM 后端在 CPU 上每页超过一分钟，不可用。模型全量约 5 到 8 GB，放在 `models/ocr/`。
- 环境隔离：MinerU 依赖链重且对 Python 版本有约束，不并入主环境。为它单独建 Python 3.12 的 uv 环境，主程序以子进程方式调用其命令行，通过文件交换结果。这也隔离了它与 Docling 之间可能的 torch 版本冲突。UOS 上需补齐 `libgl1` 与 `libglib2.0-0` 系统库。
- 接入方式：实现 `OcrParser`，输入扫描 PDF 或图片，输出与其他解析器相同的 DocumentIR，把 MinerU 的 middle json 或 content list 映射为块，bbox 与 OCR 置信度填入 Provenance。MinerU 2.x 的配置文件为 `mineru.json`，模型目录通过配置文件或环境变量指向 `models/ocr/`，具体键名接入时核实。
- 接入后，被标记 `needs_ocr` 且 Docling 内置 RapidOCR 效果不佳的页面改走该解析器，实现混合文档。
- 以上 MinerU 版本、Python 版本范围与配置细节需在联网后核实。

### 5.4 Segment 长文本切块

- 输入 DocumentIR 中连续的段落、标题、列表块。表格块不参与切块。
- 切块策略按优先级：一级标题边界、二级标题边界、段落边界。目标块大小由配置决定，默认 800 token，最大 1200 token，重叠 80 token。token 计数用 llama-server 的 tokenize 接口或本地 tokenizer 估算。
- 每个 Chunk 记录 heading_path，抽取时把标题路径作为上下文前缀，帮助小模型理解段落归属。
- 保留 char_offset_map，使模型返回的原文片段能映射回块与字符偏移。

### 5.5 Discover 字段发现

**表区检测与表头识别**

1. 对 sheet 网格计算非空掩码，按连续空行空列切分成连通区域。面积过小的区域并入相邻区域或作为备注丢弃。
2. 每个区域内，对前 N 行打表头分：字符串占比、唯一率、与下方三行类型反差、加粗或填充、合并区域覆盖、行内非空率。得分最高的连续行段为表头，可多行。
3. 多级表头按合并区域展开，复合名用 `/` 连接，如 `销售额/一季度`。
4. 尾部合计行、平均行按关键字与数值汇总关系识别并剔除。
5. 方向判断：若第一列高度类似表头且列数很少，判为键值表，转为 KVBlock。
6. Word 与 PDF 中的表格同样走 2 到 5。跨页或跨块的表格，列数相同且表头相似度高于阈值或后续块无表头时拼接。

**键值模式识别**

- 在段落中匹配 `键：值`、`键 值`、`键为值`、`键是值` 及英文冒号变体。键长度限制、键必须以名词性词结尾等规则减少误报。
- 一段中连续多行键值对合并为一个 KVBlock。
- 键值候选默认置信度中等，需归并时与其他来源互相佐证。

**长文本属性归纳（LLM）**

这是非表格文档产出字段的主路径。

1. 抽样。每个文件按 heading_path 分层抽样若干 Chunk，默认每文件最多 6 块，全会话最多 N 块（配置）。
2. 归纳。对每块调用模型，Schema 固定为「属性列表」，每项含属性名、值类型、示例值、原文片段。提示要求只列出该文本明确陈述的属性，不推断。
3. 验证。原文片段在块内找不到的项直接丢弃。
4. 聚合。同名或相似名的属性跨块跨文件计数，只有覆盖块数达到阈值或用户手动保留的才进入候选列表。低频属性放在「更多」折叠区。
5. 输出 `source_kind=text_attr` 的候选，members 指向出现过的 chunk。

**路径候选**

- 每个路径层级和文件名模式生成一个候选，如 `路径/第 1 级目录`、`文件名/年份`。
- 层级内取值种类为 1 的层级不生成候选（没有信息量），但可作为常量字段供用户手动添加。

**值类型签名推断**

- 对候选的样本值池按正则与解析器逐一尝试：日期格式集合（含中文年月日、斜杠、点、纯数字）、数值（含千分位、全角、负号变体、括号负数）、金额（货币符号、元、万元）、百分比、手机号、身份证、统一社会信用代码、编码模式（字母数字混合定长）、布尔词表、枚举（唯一值少且重复率高）。
- 输出主类型与置信度，混合类型时主类型为覆盖最多的，并记录冲突率。

**跨文件归并**

1. 候选名规范化：全半角、繁简、去空白与括号注释、去单位后缀。
2. 三重相似度：名称字符串相似度（rapidfuzz）、类型签名与值分布相似度、名称嵌入相似度（bge-small-zh-v1.5）。加权求和。
3. 层次聚类，高于高阈值直接合并，低于低阈值不合并，介于两者的簇进入 LLM 裁决队列。
4. LLM 裁决：输入簇内各候选的名称、类型、三个样本，Schema 为「是否同一字段、规范名、理由」。同簇按两种候选顺序各问一次，答案不一致则不合并并标记给用户。
5. 归并后的候选保留 merged_from，用户可以拆开。

**规范命名（LLM）**

- 对最终候选批量调用模型给出简洁中文规范名与一句描述。一次调用处理最多 20 个候选，输入只有名称、类型、样本。
- 用户改名优先于模型命名，改名结果写入会话词典，下次同名候选直接命中。

### 5.6 Plan 映射计划

- 用户选择字段后，先确定记录粒度：
  - `table_row`：每个表格行一条记录，适合表格为主的数据。
  - `document`：每份文件一条记录，适合合同、报告、公文等长文本，每个字段从全文抽取一次。
  - `section`：每个 N 级标题下的内容一条记录，适合一份文件内含多个同构章节。
  - `entity`：由模型在文本中识别多个实体实例，每个实例一条记录。此模式最依赖模型，默认不启用，仅在用户明确描述实体后开放。
- 计划生成：对每个字段的每个成员，根据来源类型生成 SourceMapping。表格列映射直接记列索引；键值记键名；文本记 chunk 范围与提示模板；路径记层级。
- 未覆盖到的文件字段组合标记为 missing，用户可指定回退策略：留空、常量、或强制文本抽取。
- 计划可保存并应用到新加入的文件。新文件的表头与已有成员按同样归并逻辑匹配，匹配不到的进入待确认列表。

### 5.7 Extract 抽取

**结构映射**：polars 从 TableBlock 网格按列索引取值，每个值的 Evidence 是单元格坐标。

**键值抽取**：按键名精确或规范化匹配，取值。Evidence 是段落位置与键值对索引。

**长文本抽取（LLM）**：

- 按 `document` 粒度时，把该文件所有 Chunk 依次送入模型，每次只带本次要抽取的字段子集，字段数控制在 10 以内，Schema 每个字段为 `{value, quote}` 或 null。
- 模型返回的 quote 必须能在 chunk 中定位（允许空白与标点差异的模糊定位），定位失败则该值无效。
- 同一字段在多个 chunk 都有值时进入 reconcile。
- 按 `section` 粒度时以章节为单位重复上述过程。
- 按 `entity` 粒度时先让模型在每块中列出实体实例与其锚点，再对每个实例抽取字段。

**Reconcile 冲突合并**：

- 规则顺序：用户编辑 > 结构映射 > 键值 > 文本抽取。
- 同优先级多值时：规范化后相同则合并；不同则状态置为 conflict，保留全部 Evidence，界面上让用户选择。可配置自动策略：取首个、取多数、取最长。

### 5.8 Clean 清洗与类型化

- 每个目标字段有目标类型，清洗器按类型执行：
  - 日期：中文年月日、多种分隔符、两位年份、Excel 序列号、时间戳，统一为 ISO 8601。
  - 数值与金额：去千分位、全角、货币符号、单位换算（万元转元可配置）、括号负数。
  - 电话、证件、编码：去空白与分隔符，校验位检查，失败标记 invalid 而不改值。
  - 枚举：按词典映射同义值，未命中的保留原值并标记。
  - 文本：去首尾与不可见字符、统一换行、全半角可选。
- 原值列始终保留，归一值写入 normalized。类型转换失败不丢弃记录。

### 5.9 Merge 合并与去重

- 纵向拼接所有文件的记录，列对齐按 field_id。
- 精确去重按用户指定主键或复合键。
- 模糊去重可选，用 rapidfuzz 对指定字段计算相似度，输出待确认对而不自动删除。跨大量记录时用 splink 的 DuckDB 后端做分块比对。

### 5.10 Validate 校验

- 用 Pandera 的 polars 支持定义 Schema：必填、类型、范围、枚举、正则、唯一。
- 校验结果写入 cells.status，不阻断导出。审计版导出附校验报告。

### 5.11 Export 导出

| 格式 | 说明 |
|---|---|
| xlsx | 宽表一个 sheet，可选按文件分 sheet，异常单元格着色，附溯源 sheet |
| csv | utf-8-sig，兼容 Excel 直接打开 |
| parquet | 含归一后类型，供下游分析 |
| jsonl | 每记录一行，可含 evidences |
| sqlite | records 与 provenance 两张表 |
| markdown | 预览与文档嵌入 |

审计版导出额外包含 MappingPlan JSON、校验报告、模型调用摘要（模型名、版本、调用次数、命中缓存数）。

---

## 6. 模型服务层

### 6.1 LLM 运行时

- 采用 llama.cpp 的 llama-server，二进制放在 `bin/<平台>/` 下。官方 Linux 预编译包在 Ubuntu 22.04 或 24.04 上构建，链接的 GLIBC 高于 UOS v20 的 2.28，直接运行会报 `GLIBC_2.29 not found`。因此 UOS 版本必须自行构建：
  - 默认方案：在 `manylinux_2_28_x86_64` 容器中用其新版 GCC 从源码编译，关闭 `GGML_NATIVE`，显式开启 AVX2 与 FMA，静态链接 libstdc++，产物只依赖 GLIBC 2.28 以下的符号。`scripts/build_llama_server.sh` 固化该流程并记录 llama.cpp 提交号，`build-llama-server` workflow 自动执行并在 debian:10 中启动验证，见 12.1 节。
  - 备选：在 UOS 本机用系统 GCC 编译，前提是 llama.cpp 当前版本仍兼容 GCC 8.3 与 CMake 3.13；或用 musl 静态链接产出无动态依赖的单文件。
  - Windows 开发机直接用官方预编译包。
- OpenMP 默认关闭以免依赖 `libgomp1`，llama.cpp 自带线程池。若目标机已安装 `libgomp1` 且实测有收益，可在构建脚本中开启。
- 线程数默认为物理核心数，不用逻辑核心数。海光与兆芯均支持 AVX2 与 FMA，AVX-512 是否可用按机型探测。
- 运行模式两种，由配置 `llm.endpoint` 决定：为空时 `server.py` 以子进程方式托管 llama-server，负责启动、健康检查、端口分配、退出清理，会话内常驻；不为空时直接连接外部已运行的服务，适合运维用 systemd 用户服务把 llama-server 做成开机自启。
- `client.py` 通过 OpenAI 兼容接口调用，JSON Schema 放在每次请求体的 `response_format` 中，由服务端做语法约束解码，不走命令行传参，因此没有 shell 转义问题。请求参数固定：温度 0，关闭思考模式，设置最大输出 token。
- 输出先经 json_repair 容错，再经 Pydantic 校验，失败最多重试一次并附错误信息提示。
- 并发：CPU 上单请求吞吐最好，默认串行，通过 llama-server 的 `--parallel` 与批处理在多核充足时开放 2 路。

### 6.2 模型选型

- 注册表同时登记两个候选，默认取哪一个由目标机实测决定，实测项为抽取任务的 JSON 合法率、证据定位命中率、每秒生成 token：
  - Qwen3.5-4B，GGUF Q4_K_M。多模态底座，为未来图像输入留出可能。纯文本 GGUF 不加载视觉塔，运行开销与同尺寸纯文本模型接近，此点为推断。
  - Qwen3-4B-Instruct-2507，GGUF Q4_K_M。纯文本模型，原生 262K 上下文，只有非思考模式，无需在请求中关闭思考。llama.cpp 支持成熟，是纯 CPU 确定性抽取场景的稳妥选择。
- 两者 Q4_K_M 权重约 2.5 GB，加上下文缓存常驻内存约 3 到 4 GB，16 GB 内存的信创终端可承载。
- 上下文窗口在服务端限制为 8192，超出的输入在应用层分块。模型原生上下文再长也不放开，CPU 预填充成本与上下文长度线性相关。
- 抽取类任务全部关闭思考模式，思考模式仅在用户手动触发的「解释这个字段」类交互中使用，且仅对支持思考的模型有效。

### 6.3 嵌入服务

- bge-small-zh-v1.5 以 ONNX 格式放在 `models/embedding/`，用 onnxruntime CPU 推理，tokenizer 用 tokenizers 库离线加载。优先使用 int8 量化版（约 24 MB，如 Xenova 发布的 `model_int8.onnx`），fp32 版约 95 MB 作为对照。单条短文本向量化在海光 C86 上约 3 到 6 毫秒，常驻内存低于 100 MB，以上数字需实测。
- onnxruntime 的 Linux 轮子为 `manylinux_2_28`，与 UOS v20 恰好匹配，可直接安装预编译轮子。
- 输入截断到 512 token。仅用于字段名、键名、短属性名的向量化。
- 向量在会话内缓存，键为文本哈希。

### 6.4 预算与配额

- 每个会话有 token 预算与调用次数上限，默认按文件数线性计算，界面显示预计耗时。
- 每类任务有独立配额：文本归纳、归并裁决、命名、文本抽取。超出配额时任务降级，例如归纳停止抽样、裁决改为不合并待用户确认。
- 所有调用记录到 `llm_calls`，缓存键为模型标识加提示模板版本加输入哈希。

### 6.5 性能包络（需实测）

| 指标 | 4B Q4_K_M，8 核桌面 CPU，经验范围 |
|---|---|
| 预填充 | 每秒 100 到 400 token |
| 生成 | 每秒 8 到 20 token |
| 单次调用（1500 输入，200 输出） | 10 到 30 秒 |
| 内存 | 模型约 3 GB 加上下文缓存 |

由此设定：单次输入不超过 3000 token，输出不超过 400 token，200 文件的会话模型调用总数控制在数百次。

---

## 7. 长文本作为第一等数据源

这一节单独强调，因为用户数据可能整份就是 doc 或 pdf 长文本。

### 7.1 用户视角流程

1. 导入一批合同、报告、公文类文件。
2. 系统抽样归纳出属性候选，如「甲方」「合同金额」「签订日期」「履行期限」，每个候选显示来源文件数与示例。
3. 用户勾选字段，选择记录粒度为 `document`。
4. 系统逐文件逐块抽取，每值带原文片段。
5. 用户在表格视图审阅，点击单元格看原文高亮，冲突值二选一。
6. 导出。

### 7.2 关键设计点

- 归纳阶段的抽样必须覆盖文件的不同部分，仅取开头会漏掉尾部的签署信息。默认按标题层级分层抽样，无标题时按位置均匀抽样。
- 抽取阶段的字段子集分批，避免一次要求模型输出十几个字段导致漏项。
- 证据强制是防幻觉的核心，不可关闭。
- 对于可用规则先定位的字段（日期、金额、证件号），先用正则在全文找候选位置，把命中的窗口优先送入模型，而不是从头扫描全部块。这能把长文件的模型调用次数减少一个数量级。
- 长文本抽取结果的置信度由三部分组成：模型自报置信度不采用；用 quote 定位质量、多块一致性、类型校验是否通过来计算。

---

## 8. 模型目录与配置

### 8.1 目录解析优先级

1. 命令行参数 `--models-dir`。
2. 环境变量 `DATAPRISM_MODELS_DIR`。
3. 配置文件 `dataprism.toml` 中的 `models.dir`。
4. 默认值：项目根目录下的 `models/`。

配置文件查找顺序：当前目录、用户目录 `~/.dataprism/`、程序安装目录。

### 8.2 模型注册表 `models.toml`

```toml
[llm.default]
role = "llm"
path = "llm/qwen3.5-4b-q4_k_m.gguf"
sha256 = "..."
context_length = 8192
chat_template = "qwen"
thinking = false

[embedding.default]
role = "embedding"
path = "embedding/bge-small-zh-v1.5"
sha256 = "..."
max_tokens = 512
dim = 512

[layout.docling]
role = "layout"
path = "layout/docling"
optional = true

[ocr.mineru]
role = "ocr"
path = "ocr/mineru-2.5"
optional = true
enabled = false
```

- 启动时校验必需模型存在且哈希匹配，缺失时界面给出明确提示与期望路径。
- 用户可在配置中把任意角色指向其他文件，例如换成 Qwen3-4B-Instruct。
- 注册表支持相对路径（相对模型目录）与绝对路径。
- 第三方库的模型目录统一由本项目注入，不依赖用户手工设置环境变量：Docling 通过 `artifacts_path` 参数指向 `layout/`；MinerU 子进程启动时由本项目写入其配置文件或环境变量指向 `ocr/`；Hugging Face 相关库设置离线模式环境变量，杜绝任何联网尝试。Docling 的模型用 `docling-tools models download` 在联网机拉取后拷入。

### 8.3 其他配置项

- 解析：大文件阈值、排除模式、是否启用 Docling、LibreOffice 路径。
- 切块：目标与最大 token、重叠。
- 发现：抽样上限、归并阈值、候选覆盖阈值。
- LLM：端口、并行数、超时、预算。
- 导出：默认格式、编码、日期格式。
- WebUI：监听地址、端口。

---

## 9. WebUI 与 API

### 9.1 技术选择

- 后端 FastAPI，提供 REST 接口与 WebSocket 进度推送。
- 前端两种可选：
  - NiceGUI，基于 FastAPI 与 Vue，内置 AG Grid 表格，Python 单进程即可，适合快速迭代。需确认其静态资源完全本地打包且无外部字体与 CDN 引用。
  - 独立前端（Vue 或 React）构建后作为静态文件由 FastAPI 托管，前后端分离更清晰，成本更高。
- 建议第一阶段用 NiceGUI，API 层保持独立可测，后续若需要更换前端不影响后端。

### 9.2 页面与交互

1. 会话页：新建、打开、导入文件与文件夹、显示文件清单与解析状态。
2. 字段页：候选列表按覆盖度与置信度排序，支持勾选、改名、合并、拆分、改类型，展示样本与来源。低置信与待裁决项单独分组。
3. 粒度页：选择记录粒度，显示预计记录数与模型调用次数、预计耗时。
4. 预览页：表格视图，异常单元格着色，点击单元格显示原文与位置，冲突值二选一，支持单元格编辑并记入 Evidence。
5. 导出页：选择格式与选项，生成文件并提供下载，历史导出列表。

### 9.3 任务执行

- 解析、发现、抽取为长任务，放入进程内队列，由单独 worker 线程或进程执行，通过 WebSocket 推送进度。
- 任务可取消，已完成的部分持久化，再次运行从断点继续。

### 9.4 API 概要

```
POST /sessions                      新建会话
POST /sessions/{id}/sources         添加文件或目录
POST /sessions/{id}/parse           触发解析
GET  /sessions/{id}/candidates      候选字段列表
PATCH /sessions/{id}/candidates/{cid}  改名、合并、拆分、改类型
POST /sessions/{id}/plan            提交字段选择与粒度
POST /sessions/{id}/extract         触发抽取
GET  /sessions/{id}/records         分页记录
PATCH /sessions/{id}/cells/{rid}/{fid}  编辑或选择冲突值
POST /sessions/{id}/exports         导出
GET  /sessions/{id}/evidence/{rid}/{fid} 原文与位置
WS   /sessions/{id}/events          进度推送
```

---

## 10. 目标平台与离线部署

### 10.1 目标平台

- 部署目标：统信 UOS v20，Debian 10 底座，GLIBC 2.28，x86_64。CPU 为海光 C86 系列或兆芯 KX 系列，均支持 AVX2 与 FMA。显卡为国产显示卡，无 CUDA、ROCm 或其他通用计算栈，一切推理为纯 CPU。
- 系统自带 Python 3.7 不得触碰。用 uv 在用户目录管理独立的 Python 3.13 解释器与虚拟环境，MinerU 另建 Python 3.12 环境。
- 开发机可以是 Windows，代码保持跨平台，但集成测试与性能基准以 UOS 为准。路径、编码、子进程调用不得假设任一平台。

### 10.2 依赖轮子的 GLIBC 约束

wheelhouse 中每个含原生扩展的 Linux 轮子，其 manylinux 标签要求的 GLIBC 版本必须不高于 2.28。构建脚本在打包阶段逐个校验标签并拒绝越界轮子。已知情况：

| 库 | Linux 轮子标签 | 结论 |
|---|---|---|
| onnxruntime | manylinux_2_28 | 恰好匹配 |
| polars | manylinux_2_17，abi3 | 可用 |
| python-calamine | manylinux_2_17 | 可用 |
| pyarrow、duckdb、pydantic-core | manylinux_2_17 或 2_28 | 逐版本核对 |
| torch CPU（Docling 依赖） | manylinux_2_17 或 2_28 | 逐版本核对，体积大 |
| pandera、nicegui、fastapi、pdfplumber | 纯 Python | 可用 |

以上标签为撰写时的认知，`wheelhouse` workflow 以实际下载的轮子文件名为准自动校验，见 12.1 节。

### 10.3 打包与安装

- 依赖用 uv 锁定，在联网机器上按目标平台与 Python 3.13 下载轮子构建 wheelhouse。内网用 `uv sync --offline --find-links` 安装。
- 依赖分组：`dev`、`docling`、`ocr`、`libreoffice` 说明性分组，默认最小安装。`docling` 分组体积较大，因含 torch。
- llama-server 使用第 6.1 节的本地编译产物，Windows 用官方预编译包，分别放在 `bin/linux-x86_64/` 与 `bin/windows-x86_64/`。
- 模型文件、前端静态资源打入分发包，附 sha256 清单。
- UOS 系统依赖：`libgl1`、`libglib2.0-0`（OCR 相关），LibreOffice 可选。

### 10.4 启动自检

- Python 版本、CPU 指令集（AVX2 与 FMA 必需，AVX-512 可选）、GLIBC 版本、可用内存。
- 模型存在与哈希、llama-server 可执行且能加载模型、端口可用。
- 可选组件探测：Docling 模型、MinerU 环境、LibreOffice。
- 禁用所有库的遥测与在线检查，设置 Hugging Face 离线模式环境变量。NiceGUI 自带的 Vue、Quasar 与图标字体均为本地文件，无外部 CDN 请求，但不得使用其 AG Grid 企业版与在线地图组件。

### 10.5 运行方式

- 桌面单机：用户在 UOS 终端或桌面快捷方式启动 `main.py`，浏览器打开本机地址。llama-server 由主程序托管。
- 部门共享：主程序与 llama-server 分别注册为 systemd 用户服务常驻，多人通过内网浏览器访问。统信浏览器基于 Chromium，与 Chrome 表现一致。此模式下 WebUI 无鉴权，仅限可信内网，见 1.3 节非目标。

---

## 11. 错误处理、日志与可观测性

- 解析失败按文件隔离，不影响其他文件。失败原因写入 source_files 并在界面展示。
- 模型调用失败分类：超时、输出不合法、Schema 不匹配。各自有限重试，最终失败的任务标记并可手动重跑。
- 日志分层：应用日志、模型调用日志（含提示哈希、耗时、token 数，不记录完整原文，可配置开启）、审计事件。
- 界面提供会话诊断导出，包含日志与环境信息，便于排查。

---

## 12. 测试策略

- 单元测试：类型签名推断、日期与数值归一、表头识别打分、键值正则、路径切分。均用构造样例，不依赖模型。
- 解析器测试：每种格式准备含合并单元格、多级表头、多表区、跨页表、嵌套表、无文本层页的夹具文件。
- 模型相关测试：用录制回放的方式固定模型输出，测试 Schema 校验、证据定位、冲突合并逻辑。真实模型测试作为可选的慢测试。
- 端到端测试：小型夹具目录跑通导入到导出，比对导出结果与溯源表。
- 性能基准：记录目标机器上的解析速度与模型调用耗时，作为预算参数的依据。

### 12.1 持续验证：用 GitHub Actions 模拟 UOS

目标机在隔离内网，无法作为 CI runner。托管的 Linux runner 是 x86_64 且可运行 Docker，因此用 `debian:10-slim` 复现 UOS v20 的 GLIBC 2.28 底座，用 `--network none` 强制离线，把 10.2 节的轮子约束、6.1 节的编译方案和离线运行逐项自动化。

**验证边界**

| 类别 | 内容 |
|---|---|
| 可严格验证 | llama-server 在 GLIBC 2.28 下可编译、可启动；轮子标签不越界且能离线安装、导入；模型能从本地目录加载并断网运行；JSON Schema 约束输出合法且证据可定位；NiceGUI 页面无外部资源引用 |
| 只能近似 | 一切耗时数字。runner 是 AMD EPYC 4 vCPU，与海光或兆芯差异大，CI 数字只用于相对比较，例如两个候选模型的吞吐比、Docling 与 pdfplumber 的耗时比 |
| 无法验证 | 海光与兆芯的 AVX-512 支持与真实吞吐；统信浏览器的实际渲染；UOS 与 Debian 10 在软件源和补丁上的细微差异 |

**四个 workflow**

| workflow | 触发 | 步骤 | 产物 |
|---|---|---|---|
| `build-llama-server` | 相关脚本变更、手动、被其他 workflow 调用 | 在 `quay.io/pypa/manylinux_2_28_x86_64` 容器中从源码编译，AVX2 与 FMA 开启，AVX-512 与 OpenMP 关闭，静态链接 libstdc++；`objdump` 校验最高 GLIBC 符号不超过 2.28；在 debian:10 断网容器中执行 `--version` | `llama-server` 二进制、`BUILD_INFO.txt`、`SHA256SUMS` |
| `wheelhouse` | `pyproject.toml` 或 `uv.lock` 变更 | `uv export` 锁定依赖；`pip download` 只取 Python 3.13、manylinux 不高于 2.28 的 x86_64 二进制轮子，torch 走 CPU 索引；`check_wheel_tags.py` 逐个校验标签；用 uv 托管的独立 Python 在 debian:10 断网容器中离线安装，`import_check.py` 逐个导入顶层模块 | wheelhouse 目录与 requirements |
| `offline-smoke` | 冒烟脚本变更、手动 | 调用 `build-llama-server` 取二进制；联网阶段下载 GGUF、bge int8 ONNX、Docling 模型并缓存；生成夹具 PDF；断网容器中依次运行 `smoke_llm.py`、`smoke_embedding.py`、`smoke_docling.py`、`check_no_external_requests.py` | 各项 JSON 结果与 llama-server 日志 |
| `ocr-manual` | 仅手动 | 独立 Python 3.12 环境安装 MinerU，下载 pipeline 模型，把夹具 PDF 栅格化成纯图像 PDF，断网容器中以 `--backend pipeline --device cpu` 解析并计时 | MinerU 输出与耗时 |

**选择 manylinux_2_28 镜像而非 debian:10 做编译的原因**：Debian 10 自带 GCC 8.3 与 CMake 3.13，当前 llama.cpp 对 C++17 与 CMake 版本的要求可能超出它。manylinux_2_28 镜像基于 AlmaLinux 8，GLIBC 同为 2.28，但提供新版 GCC 工具链，并通过静态链接新版本 libstdc++ 符号保证产物只依赖基础系统库。这是 Python 生态构建可移植二进制的标准做法。产物再在 debian:10 中启动一次作为最终确认。

**冒烟脚本的断言**

- `smoke_llm.py`：JSON Schema 约束输出的合法率，以及每个抽取值的 quote 能否在原文中定位。默认合法率低于 80% 判失败。同时记录 llama-server 返回的预填充与生成速度。
- `smoke_embedding.py`：三组字段名的相似度关系，例如「联系电话」与「手机号」的相似度必须高于与「合同金额」的相似度。记录单条向量化毫秒数。
- `smoke_docling.py`：断网状态下从 `artifacts_path` 加载模型，夹具 PDF 中的表格必须被识别为 5 行 4 列。记录每页秒数。
- `check_no_external_requests.py`：启动含 `ui.table` 与 `ui.aggrid` 的最小 NiceGUI 应用，递归抓取首页引用的同源资源，任何外部 http(s) 引用或非 200 资源都判失败。这是静态扫描，浏览器级验证留给 self-hosted runner。
- 每次断网阶段开头先访问一次外网并要求失败，证明隔离生效。

**实施细节**

- 所有容器步骤用显式 `docker run`，不用 job 级 `container:` 字段。后者会向容器注入 Node 20，其 GLIBC 下限恰为 2.28，在 Debian 10 上有过兼容问题。
- 工作区以相同绝对路径挂载进容器，虚拟环境内的绝对路径在容器内外一致。
- 容器内的 Python 用 uv 托管的 python-build-standalone，它只要求 GLIBC 2.17，可在 Debian 10 运行；runner 自带的 Python 链接更新的 GLIBC，不能带进容器。
- 模型下载与依赖安装只在联网阶段发生并缓存。单仓库缓存上限 10 GB，4B GGUF 约 2.5 GB 加 Docling 模型可容纳，MinerU 模型单独缓存且只在手动 workflow 中使用。
- `debian:10` 已归档，验证镜像的 apt 源指向 `archive.debian.org`，并预装 `libgl1`、`libglib2.0-0`、`libgomp1`。
- 冒烟依赖暂由 `scripts/smoke-requirements.txt` 单独管理，项目依赖定型后迁入 `pyproject.toml` 的依赖组，届时 `offline-smoke` 改用 `wheelhouse` 的产物。

**最高保真的补充**：若单位有一台能访问 GitHub 的 UOS 开发机，注册为 self-hosted runner，把 `offline-smoke` 与 `ocr-manual` 的目标从容器换成它，耗时数字即为真实数字。隔离内网机不能做 runner，但可以把 CI 产出的 wheelhouse、llama-server、模型校验清单作为 release 资产拷入，再运行同一套冒烟脚本，两边用同一份测试。

**首次运行前需人工校正的点**：默认 GGUF 下载地址与 Xenova ONNX 文件名、`docling-tools models download` 的参数、MinerU 的安装规格、模型下载命令与配置文件位置、llama.cpp 的 CMake 选项名。这些均写在脚本注释中，与 14.2 节的核实清单对应。

---

## 13. 里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| M1 | 核心数据模型、Workspace、Ingest、Excel 与 docx 解析、表区与表头识别、路径候选、类型签名、规则归并、CLI 导出 csv 与 xlsx；CI 基线：`build-llama-server`、`wheelhouse`、`offline-smoke` 首次跑通并校正脚本中的未核实参数 | 不接任何模型即可完成表格类数据的字段发现与成表；CI 绿色 |
| M2 | LLM 服务层、模型注册表、灰区归并裁决、规范命名、缓存与预算 | 同义列跨文件自动合并，调用次数受控 |
| M3 | 切块、长文本属性归纳、document 与 section 粒度抽取、证据定位、冲突合并 | 一批合同类 docx 可产出字段并成表，每值可回溯 |
| M4 | PDF 解析，Docling 默认后端，pdfplumber 快速路径与乱码检测，跨页表拼接 | 文本型 PDF 达到与 docx 相当的效果，无框线表格不错列 |
| M5 | WebUI 全流程、任务队列、审阅编辑、审计导出 | 非技术用户可独立完成一次工程 |
| M6 | wheelhouse 与 llama-server 产物打包为 release、启动自检、性能调优与并行；有条件时接入 UOS self-hosted runner | 在 UOS 内网机器一键部署并通过与 CI 相同的冒烟脚本 |
| M7 | OCR 扩展，MinerU 2.5 pipeline 后端接入，混合文档 | 扫描件进入同一流程 |

---

## 14. 风险与待核实事项

### 14.1 设计风险

- 记录粒度选错会导致整表错误。界面必须在粒度页给出示例记录预览，让用户在抽取前确认。
- 4B 模型在灰区裁决上的一致性可能不足。已用双序投票与用户确认兜底，但裁决队列过长时用户负担大，需要在阈值上做实测调优。
- 无表头的表格与极度杂乱的 sheet 会让表区检测失效。提供手动框选表区与指定表头行的兜底交互。
- 长文本抽取的耗时是主要体验瓶颈。规则预定位与分批字段是关键优化，需在真实数据上验证收益。
- Docling 成为 PDF 默认后端后，每页数秒的耗时使 PDF 解析成为第二个瓶颈。多进程并发与 pdfplumber 快速路由是缓解手段，需在海光或兆芯机器上实测每页耗时后再定默认并发数。
- Docling 与 MinerU 的依赖体积大，且各自带模型，需要与主包的依赖版本协调。MinerU 独立环境，Docling 作为可选分组。
- UOS 的 GLIBC 2.28 是最容易在部署阶段暴露的隐性约束。任何新增原生依赖都必须先过 10.2 节的标签校验。

### 14.2 需联网核实的外部事实

本文档撰写时联网工具不可用，内容基于截至 2026 年 6 月的知识，并吸收了用户提供的一份针对 UOS 平台的第三方调研。第三方调研中已采纳的结论有：llama.cpp 官方预编译包的 GLIBC 冲突与本地编译方案、MinerU 仅 pipeline 后端可用、onnxruntime 与 polars 等轮子的 manylinux 标签、NiceGUI 资源本地化、pdfplumber 的 CID 字体乱码成因、Qwen3-4B-Instruct-2507 的上下文与模式特性。第三方调研中被修正或降级为推断的有：Docling 离线模型环境变量名、MinerU 配置文件名与 Python 版本范围、Docling 自动触发 OCR 的说法、Qwen3.5 多模态底座带来纯文本额外开销的说法、`-jf` 参数与 llama-server 的关系。

实施前逐项核实：

1. Qwen3.5 小尺寸系列的确切型号列表、上下文长度、许可证、GGUF 与 llama.cpp 支持状态。
2. Qwen3-4B-Instruct-2507 的 GGUF 可用性与 llama.cpp 聊天模板。
3. llama.cpp 当前版本在 debian:10 环境下的编译可行性（GCC 8.3 对 C++17 的支持）、musl 静态编译可行性、`response_format` 的 JSON Schema 参数形态。
4. Docling 当前版本对 Python 3.13 的支持、`docling-tools models download` 的输出结构、`artifacts_path` 用法、RapidOCR 集成方式、CPU 每页耗时。
5. MinerU 2.5 的 Python 版本范围、配置文件键名、模型目录设置方式、CPU pipeline 每页耗时、输出格式。
6. Xenova 发布的 bge-small-zh-v1.5 int8 ONNX 的可用性与精度损失，onnxruntime 当前版本的轮子标签。
7. python-calamine、polars、pandera、NiceGUI、duckdb、pyarrow、torch CPU 对 Python 3.13 的支持及各自 Linux 轮子的 manylinux 标签。
8. NiceGUI 当前版本静态资源是否仍完全离线。
9. 海光 C86 与兆芯 KX 系列对 AVX-512 的支持情况，以及 llama.cpp 在这两类 CPU 上的实测吞吐。
10. UOS v20 各版本的 GLIBC 与默认软件源中 LibreOffice、libgl1 的可用性。
