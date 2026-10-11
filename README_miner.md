# PIC 资源挖掘：高召回取证与持续模型流水线

工程：`/Users/ryo/PIC_layout/`，Python 3.9+，macOS/Linux。目标是发现可供人工筛选的 PIC 版图、PDK、生成代码和论文关联数据，优先关注有源器件、电极、金属互连、规模和制造背景。模型判断是线索筛选，不代替版图验证或 benchmark 构建。

## 启动与配置

```sh
cd /Users/ryo/PIC_layout
python -m pip install -r requirements.txt
python main.py --continuous
```

`python main.py` 默认持续运行。根目录 `.env` 必填 `OLLAMA_BASE_URL`、`OLLAMA_MODEL`、`GITHUB_TOKEN`；使用 python-dotenv，已有进程环境变量优先。私网地址、端口和凭证不写入代码或种子；输出与缓存证据经过敏感值清洗。升级不改动真实 `.env`，选项可参考 `.env.example`；已有显式 `DISCOVERY_INTERVAL`、`PREFETCH_PER_SOURCE` 会继续生效，未配置时使用下表新默认值。

| 配置 | 默认值 | 用途 |
|---|---:|---|
| `PREFETCH_PER_SOURCE` | 8 | 每个来源最多缓存的就绪条目，范围 1–100 |
| `MODEL_CHUNKS` | 8 | 每轮资源研判最多读取的新证据窗口，范围 1–256 |
| `MODEL_KEEP_ALIVE` | -1 | 保持模型驻留；`-1`/`0`/纯数字秒数转为 JSON 数字，`5m` 等保留时长字符串 |
| `DISCOVERY_INTERVAL` | 2.5 | 一个查询完成后的轮询间隔；不暂停其他查询或推理 |
| `REPO_RECHECK_INTERVAL` | 1800 | 已处理仓库版本复查间隔；最少 60 秒，避免旧 2.5 秒配置反复读 Releases |
| `IDLE_INTERVAL` | 0.25 | 就绪队列暂空时检查间隔；不在有任务时休眠 |
| `DISCOVERY_OVERLAP` | 600 | 增量查询时间窗重叠 |
| `HTTP_TIMEOUT` / `OLLAMA_TIMEOUT` | 30 / 300 | 网络读取超时，单位秒 |
| `MAX_RETRY_WAIT` | 30 | 普通请求单次重试预算；平台真实限流等待可更长 |

```sh
python main.py --once --max-items 10
python main.py --once --retry-review
python main.py --review-offline
python main.py --reassess-offline --max-items 20
python main.py --github-query 'photonic heater layout'
python main.py --arxiv-query 'cat:physics.optics AND all:tapeout'
python main.py --seeds seeds.json
```

`--once` 每个发现游标最多读一页，处理本轮就绪条目后退出；未处理的预取证据继续留在 SQLite。`--max-items` 限制消费的就绪研判批次数，`--max-repos` 限制其中的 GitHub 批次数；持续模式同一大资源可能分多批轮询；采集线程跳过的未变更资源不计入此数量。停止时可能已预取额外条目，下一次恢复。

`--org` / `--repo` 替换组织和显式仓库种子，保留搜索查询。只发现指定仓库时使用独立种子文件，如 `{"repositories":["owner/repo"]}`；已有持久化任务不会被删除。

`seeds.json` 保留原有平铺关键词，并支持 `github_query_groups` / `arxiv_query_groups`（组名 → 查询列表）。组内、平铺与 CLI 追加查询自动去重；展开时交错取各组关键词。当前合计 **131 条 GitHub 查询、24 条 arXiv 查询**，覆盖 PDK/工具链、有源器件/电极、规模电路/布线、制造/开放数据、材料平台及中文线索。可以直接增删分组，无需改代码。例如：

```json
{
  "github_query_groups": {
    "devices": ["\"phase shifter\" layout", "\"photonic\" electrode"],
    "platforms": ["gdsfactory", "\"TFLN\" layout"]
  },
  "arxiv_query_groups": {
    "open_design": ["cat:physics.optics AND all:tapeout"]
  }
}
```


## 高召回入口与三档输出

所有发现渠道入队的资源均可进入语义研判。光学、导出、制造、电气器件等正则只提供特征与阅读优先级，**关键词未命中、没有静态导出调用或没有摘要外链，不再直接判无关**。源码可反向提供 PIC/TFLN/Pockels/硅光等领域证据。

System Prompt 仍限定 PIC：传统电子 ASIC/Analog/VLSI/PCB 即使有 GDS 或 metal routing，也应由模型判 `LEVEL_3_IRRELEVANT`。字段预检、Pydantic 严格类型、枚举、长度、额外字段拒绝、重复键拒绝和原文引文校验全部保留。

| 等级 | 含义 | 落盘方式 |
|---|---|---|
| `LEVEL_1_CONFIRMED` | 直接 PIC 版图、实际导出或实体资源证据 | Level 1 看板 |
| `LEVEL_2_PROBABLE` | 有价值工具/PDK、强设计线索、开放声明或尚待核实的托管资源 | Level 2 看板 |
| `LEVEL_3_IRRELEVANT` | 完整检查后的无关资源/纯理论，或明确属于传统电子项目 | 仅扫描状态，不写命中卡片 |

取证不完整、分块预算未覆盖全部材料时，模型的否定结果进入 Review，避免把“没看到”写成“不存在”。已验证的 Level 1/2 不会因后续分块格式错误而被抹掉。Review 是处理状态，不是第四个模型等级。

模型只输出四个字段：`level`、`summary`、`physical_features`、`evidence`。Ollama 请求固定使用 `"format": "json"`，不向服务端发送复杂 Pydantic JSON Schema；字段结构与三档枚举继续由 Prompt 说明，Python 本地使用 `Assessment` 执行严格类型、字段及结构校验。Level 1/2 必须附具体特征及原文引用；引用既要出现在实际输入窗口，也要出现在保存的对应原始证据块正文中。文件标题不能充当正文引用。重试切片保留原文，不生成省略号、改写空白或额外行号；引用失败时提供少量原文复制候选，但绝不自动替模型补造或修复引用。E 编号在预览、原文块与模型窗口之间保持一致。不得执行外部文本中的指令、代码或任意工具。

每次请求显式 `num_ctx=8192`、`think=false`、`format="json"`、`temperature=0`；首次 `num_predict=2048`，已完成但校验失败时重试提高至 `4096`。关闭独立思考输出，避免思考内容耗尽 JSON 生成预算。模型证据最多 3,500 字符；JSON/字段/引用校验失败后带具体错误重试，证据压到最多 1,500 字符并要求纯 JSON。`done_reason=length` 不直接丢弃：完整 JSON 仍须通过严格字段与原文引用校验，未闭合或不合法的 JSON 才重试；不补造字段或闭合括号。仅在确认上一请求完成后重试。每块最多两次已完成的生成，资源每轮最多 `MODEL_CHUNKS` 个新窗口。扩大窗口数量不扩大显存上下文。

底层 GDS/OAS 解析/几何引擎和其原生测试导出不构成 PIC 资产；gdstk/gdspy 自身的教学/测试布局不得升为 Level 1，确有光子几何示例时最多作为 Level 2 工具线索保留；纯 PyTorch 光神经网络训练、矩阵仿真/校准也不构成资产。Level 1 需要具体 PIC 几何/器件布局与物理文件或导出证据，Level 2 需要 PIC PDK、版图生成工具/组件或可信开放设计线索。仓库仅使用 gdstk/PyTorch 不会被拒绝；混合项目中的真实 PIC 设计资产继续保留。静态排除只在完整取证且明确纯底层/算法语境、没有实际设计结构或公开 PDK 线索时生效，其他情形由模型语义判断。Level 3 摘要携带原因标签，便于统计，不强制未知项目符合一套词库。

## 原文保存与多轮阅读

SQLite `evidence_blobs` 按内容 SHA256 去重保存已取得的完整、清洗过的文档和源码；notebook 只保留 code，排除 output/base64。证据块包含稳定 E 编号、来源、版本、路径及原文哈希。扫描和就绪任务只保存原文引用，避免反复复制大文件。

`evidence_json.text` 仍是最多 10,000 字符的预览；**它不再是新资源唯一保存的材料**。模型从原文生成带重叠的窗口，特征只用于排序，按来源文件分配代表窗口，再读其他窗口。保留覆盖说明；记录 `examined_chunks` 和已验证结果。持续模式中有新阅读进度且仍有未读窗口时，保存进度并回到 Round-Robin 队列，自动继续读下一批；不会让单个大仓一次独占所有推理。达到 Level 1 或完整覆盖后结算。下一次同协议 Review 重评也读取尚未读过的窗口；切换协议则重新阅读，避免继承旧模型裁决。

单资源当前仍有有界取证：GitHub 最多 16 个 Markdown/RST 文档、40 个源码；Zenodo 最多 40 个文本附件；每个源文件最多 8 MiB。完整文件树保留路径线索，未读文件标记覆盖不足。超过安全大小的文件不会作为原始模型输入。旧扫描只有压缩预览时，离线重评无法恢复过去未抓取的材料。

## 独立采集与单并发推理

运行使用 `PipelineCruise`：

```text
GitHub collector ─┐
arXiv collector ──┘─> SQLite prepared / 原文缓存 ─> 单模型 Worker ─> 扫描状态与 hits.md
                                  SQLite writer 负责所有数据库访问
```

**Zenodo 临时暂停**：默认只启动 GitHub、arXiv 采集线程，并只轮询这两路发现游标和就绪研判任务。已有 Zenodo 扫描结果、待处理任务、缓存证据、失败计数及搜索水位线保留；新提取的 Zenodo 外链仍可入队等待恢复，不发起 Zenodo HTTP 请求。`--zenodo-query` 当前不会启用采集。恢复时将 `Cruise.SOURCES` 加回 `zenodo`；两个调度器也支持显式传入 `sources=("github", "arxiv", "zenodo")`，无需迁移数据库。已运行进程需正常退出后重新启动以应用修改。

每个启用来源一个采集线程，HTTP Session 只由所属采集线程使用；arXiv API/正文共享节拍。SQLite 连接仅属于专用 writer 线程，其他线程通过请求队列访问，避免跨线程使用 sqlite3 连接。模型 Worker 使用进程内锁与工程运行锁，始终单并发。

模型处理条目期间，采集可以继续准备后续条目。GitHub 的 403/429 等待只占用 GitHub 采集通道，其他来源和已准备证据继续推进。就绪任务按 GitHub → arXiv 轮询，空来源自动跳过；轮转位置持久化。等待日志每 30 秒报告队列状态，避免空闲时刷屏。

采集与研判采用两层轮转：每个采集器在到期查询之间循环，每个查询一次只推进一页或一次窗口拆分；Worker 在来源之间轮转。查询轮转位置与发现页入队同事务提交，重启后接续。某个查询结束仅影响自身游标，不暂停采集线程。

采集优先准备已有就绪任务：证据队列为空时，每处理 2 个任务就给发现查询一次机会（避免被未变更仓库复查占满）；有缓存时每 4 个任务轮到一次发现。单来源缓存达到 `PREFETCH_PER_SOURCE` 时暂停该来源预取，Worker 消费后继续，控制本机缓存规模。启动时缩短旧的健康查询冷却，不覆盖错误退避、不启用 Zenodo。

GitHub 历史查询覆盖完成后保留水位线并退出活跃轮询，后续由同词增量通道持续跟进；不再每小时调度已完成历史游标，也不重扫 2008 年。arXiv 历史通道使用固定提交日期窗口和提交时间排序；结果较多时按分钟边界二分、优先近期再推进历史，逐窗保存偏移。旧 arXiv 历史游标首次使用新窗口逻辑时重建一次覆盖，已入库资源继续去重；完成后保持休眠于调度之外，由更新时间增量通道跟进新论文和修订。完成搜索不等于模型读完，已有缓存仍继续消费。

只有队列确实暂空、平台限流、来源故障或达到缓存上限时才等待；无法承诺在 API 不可用或已无新资源时 GPU 始终满载。日志显示当前查询、历史/增量通道、页码/偏移、局部下次可用时间及证据队列状态。


Ollama `keep_alive` 的纯数字环境值在发请求前转为 JSON 数字：常驻必须发送 `"keep_alive": -1`，不能发送无单位字符串 `"-1"`（[官方说明](https://docs.ollama.com/faq#how-do-i-keep-a-model-loaded-in-memory-or-make-it-unload-immediately)）。HTTP 400 等明确拒绝显示 `[MODEL REJECTED]` 和经过限长/脱敏的服务端 `error` 字段，不输出原始请求、响应体或 HTTP 头；保留 prepared 与证据，修正后重启。不对配置错误自动重发。

模型连接超时、响应断流或 `done != true` 时，只暂停模型通道，**绝不自动再次 POST**：未知请求可能仍占用服务器。持续模式下采集继续至缓存上限；用户确认服务器请求结束后重启恢复。`--once` 保存任务后返回失败状态。暂停状态会明确显示日志，不承诺在服务故障下继续满载。

Ctrl+C/SIGTERM 只设退出标志；当前模型结果完成严格验证、数据库提交及报告刷新后退出，未开始的新分块留待恢复。采集的频控等待可立即停止，已在途的 HTTP 请求受超时限制；取得的完整证据安全保存，尚未完成的任务重新入队。所有采集线程退出后关闭数据库，持久化的 prepared 条目在重启后直接进入研判。

## 多源发现与静态取证

- **GitHub**：组织、显式仓库、forks、README 下游引用、全网 repository search。按 updated 倒序分页，created 历史窗和 pushed 增量窗独立持久化；超过 1,000 条按时间二分。历史覆盖完成后保存完成标志并停用该历史游标，增量搜索继续推进。
- **GitHub 证据**：固定真实 HEAD 读取文件树，递归结果截断时补读子树；读取各目录 Markdown/RST、源码、`.gitattributes`、LFS 指针及 Releases 附件清单。源码不依赖光学字典批准，已读代码不要求导出正则命中才保存。
- **arXiv**：官方 Atom 发现；历史提交日期窗口二分/分页与更新时间增量通道独立推进；常规取证接入 HTML 正文、PDF fallback，提取 Availability 与正文链接，同时保存有界完整文本。原始标题、摘要、评论及 Atom 链接保留；正文不可用时留存元数据并标注覆盖不足。
- **Zenodo（采集已暂停，适配器保留）**：REST 搜索元数据、附件清单和有界文本。GET 显式 `allow_redirects=True`，规范化 `/api/records/{id}`，限制 5 次同来源跳转。记录跳到新 ID 时合并别名，结算规范 ID，防止旧断点反复处理。
- **外链**：已提取的 GitHub/Zenodo 链接在取证完成后入队，不再等待论文 Level 1/2。保存论文/仓库到目标的来源边，保留发现出处。OSF、一般 DOI、网盘暂作为静态指针供人工核对。

仓库版本包含真实 commit SHA、版图 Release 指纹以及描述/topics/默认分支等元数据指纹。HEAD 不变而新增资源描述也能重新研判。远端版本未变且同协议已处理时跳过源码和模型；确认版本仍需 API 请求。Prompt、Schema、模型、阅读预算及流程版本参与 `pipeline_key`；升级首次启动重新排队已有旧协议目标，保留原扫描历史和 FAILED 熔断状态。

大型 GDS/OAS、有效 LFS 指针和 Release 版图附件属于直接资源线索，但本程序尚不做完整几何、层映射、电极连通性、许可可用性或工业规模验证。压缩包仍只识别名称，不展开执行或读取内部实体；非默认分支和子模块也未递归取证。

## 免爬取复核

```sh
python main.py --review-offline
python main.py --reassess-offline --max-items 20
```

`--review-offline` 重评当前 Review；`--reassess-offline` 重评旧协议当前的 Level 3/Review，跳过已经被新协议解决的资源。只创建本地模型客户端，不创建 GitHub/arXiv/Zenodo 客户端，不读种子；从 SQLite 原文或旧预览恢复。这里“离线”指不爬取，仍调用 `.env` 配置的局域网 Ollama。

成功后更新 Level 1/2 看板；完整否定移除旧卡片，覆盖不足或非法输出保留 Review。保存旧扫描历史，Ctrl+C 后可继续。同协议已读进度保存；旧协议进度和裁决不作为新协议结果。

## 频控、失败与持久化

GitHub/Zenodo 请求至少间隔 2.5 秒；arXiv API/正文共同至少 3.5 秒。GitHub 认证搜索有独立限额，窗口拆分继续遵守搜索节拍和服务端响应头（[GitHub 官方说明](https://docs.github.com/en/rest/search/search)）；arXiv 日期语法、提交时间排序及分页切片依据 [官方 API 手册](https://info.arxiv.org/help/api/user-manual.html)。遵守 `Retry-After` / `X-RateLimit-Reset` 的较晚期限，打印倒计时；GitHub 二级限流也执行退避。公开 API 可按 `.env` 覆盖；GitHub Token 不跨来源发送，GitHub/Ollama 禁止自动跳转，LAN 模型流量不继承代理/netrc。

Zenodo 302 循环、404、410 等记录级 3xx/4xx，连续三次失败后进入 `FAILED`，不自动复活。429、5xx 和网络临时故障不消耗永久记录熔断计数，按退避计划重试；游标错误局部隔离，不跳过未入队页。已准备条目和任务结算可断点恢复。

SQLite `.pic_miner/state.sqlite3` 使用 WAL、FULL 同步、复合主键和 B-tree 索引；任务去重为 O(log N) 的精确查询。发现页入队与游标前移同事务，别名合并不覆盖已准备条目。原文、prepared、调用追踪、扫描、任务、游标、报告均持久化。

`model_calls` 保存输入字符数、尝试次数、耗时、Ollama Token/生成统计、实际生成预算、正文/思考字符数、校验错误及经清洗的有界响应。它用于诊断上下文截断、JSON 失败和推理耗时；当前没有 GPU 利用率采样或监控网页。

## 过滤漏斗与可观测性

看板头部显示进入研判的去重扫描资源数、已命中（L1+L2）、过滤、待核对、取证/模型异常和首次研判中数量；资源按 `(source,item_id)` 的最新状态计数，续读、重评和版本更新不重复增加资源总量。漏斗各状态列互斥：扫描总量 = 已命中（L1+L2） + 已过滤 + 待核对 + 取证/模型异常 + 首次研判中；已命中不包含过滤记录。另列升级后累计的处理批次、未变更/已待核对跳过、模型请求、引用/JSON 校验失败、模型状态异常和来源失败事件。重试恢复的校验失败属于事件，不等于当前 Review 资源数。

过滤原因包括纯底层工具、纯算法/校准、非 PIC、缺少设计资产；Review 原因区分原文引用、JSON/字段和覆盖不足，取证/模型失败也单独展示。历史记录只回填有依据的最新状态与原因，未知原因标为历史未分类，旧事件计数不编造。SQLite 用主键索引更新 `resource_outcomes` 和固定类别的 `funnel_counters`，与扫描终态同事务；看板刷新读取小型计数器，避免每次遍历历史扫描表。即使全被过滤，也生成漏斗看板；采集跳过/失败的统计在 Worker 空闲时最多约 30 秒刷新一次。校验失败的分块不会被调度器立即自动重读，可显式离线复核。

## hits.md 与测试

看板依次展示实时统计、Level 1、Level 2、Pending Review（末尾附录）；按资源主键保留当前状态，区内按更新时间排序。各分区独立编号为 `[L1-01]`、`[L2-01]`、`[R-01]` 等，刷新时随当前排序重新编号，便于指代当前看板条目。旧版已发布看板在首次刷新时自动升级排版。卡片包含外链、摘要/原因、物理线索、主要路径与折叠引用，不展示原始大段代码。SQLite 先提交，报告通过临时文件、fsync 与原子替换发布；保留人工备注和报告 outbox。升级不会仅因关键词缺失隐藏旧命中。

```sh
python -m unittest discover -s tests -v
```

测试全部使用本地临时 SQLite 和模拟 API/模型。覆盖旧功能、查询分组/公平轮转/历史窗口恢复/供料背压、中文/材料平台反例、源码独立取证、原文恢复、Schema/引文校验、分块进度、单模型并发、采集与推理同时推进、规范 ID 合并、实际 SIGINT、安全熔断与断点恢复。

目前完成高召回入口和流水线重构。模型主动规划补证、自动解包附件、完整 GDS 几何检验、逐特征事实引用和人工反馈学习仍属于后续能力，不应把当前候选看板当成已验证的工业 benchmark。
