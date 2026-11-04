# SHSF

**Stay Hungry, Stay Foolish.**

发现值得读的研究，追问尚未懂的问题。

一个可自托管的论文发现与阅读工具：从兴趣论文出发，发现相关研究，生成有原文依据的精读报告，并保存你的阅读记录。

[界面预览](#界面预览) · [交互演示](#交互演示) · [安装](#安装) · [AGPL-3.0-only](LICENSE)

## 界面预览

### 从兴趣出发，发现值得读的研究

每日推荐展示相近研究、推荐依据与中英文摘要，支持兴趣反馈与已读标记。

![SHSF 每日推荐：论文卡片、中英文摘要、相近兴趣论文与阅读入口](design/previews/readme-feed.png)

<table>
<tr>
<td width="50%"><strong>精读记录</strong><br>集中查看进度与历史报告，支持搜索、筛选和收藏。</td>
<td width="50%"><strong>单篇精读</strong><br>围绕问题、方法、实验与局限组织报告，区分推断与证据不足。</td>
</tr>
<tr>
<td><img src="design/previews/readme-readings.png" alt="精读记录页面：任务进度、标题搜索、状态筛选和收藏书签"></td>
<td><img src="design/previews/readme-report.png" alt="单篇精读页面：论文摘要、报告与结构化阅读小节"></td>
</tr>
</table>

以上截图由当前前端与模拟数据生成。示例论文、推荐分数与报告文案用于展示界面，不代表真实研究结论或实际推荐效果。

## 交互演示

无需安装 Python、配置数据库或填写模型密钥，即可体验导航、摘要中英切换、阅读反馈、精读报告、标题搜索和收藏。

交互演示暂不随公开仓库分发。

演示页使用内置模拟数据，修改只在当前页面内存中生效，刷新后重置；不会连接真实模型、账户或数据库。

正式应用已实现 arXiv 元数据采集、SPECTER2 相似度推荐、兴趣论文选择、Zotero 只读导入、官方 HTML 精读和精读记录管理。Zotero 导出与 Obsidian 保存尚未实现。

## 安装

需要 Python 3.10+；本文本机验证使用 Python 3.12。依赖由 `uv.lock` 锁定。

```sh
uv sync --extra embedding
```

基础采集不依赖 torch：可以只运行 `uv sync`。首次计算向量需要下载固定版本的 SPECTER2，缓存保存在 `.data/pipeline/models/`。模型在 CPU 上也能运行，支持用 `--device mps` 或 `--device cuda` 显式选择设备。

## 数据库

部署使用 PostgreSQL + pgvector。SQLite 是可显式选择的开发后端，用来离线重放、测试采集，不提供 pgvector 检索能力。

### Docker / 已有 PostgreSQL

```sh
export POSTGRES_PASSWORD='replace-with-your-local-password'
docker compose up -d
export SHSF_DATABASE_URL='postgresql://shsf:replace-with-your-local-password@127.0.0.1:5432/shsf'
.venv/bin/python -m shsf init
```

生产环境也可将 `SHSF_DATABASE_URL` 指向已有 PostgreSQL；迁移账号需要安装 `vector` 扩展的权限。迁移通过版本和校验和追踪，已执行迁移不能随意修改。

### 当前 Mac：独立本地 PostgreSQL

此工作区已安装项目内 PostgreSQL 16.2 和 pgvector 0.8.0，仅通过 Unix socket 访问，没有 TCP 监听。数据位于 `.data/postgres/`，新安装默认创建 `shsf` 和 `shsf_test`。已有安装沿用 `.data/pipeline/database-url` 中的数据库名称、socket 和端口，不迁移数据。

其他同类本机环境可用以下可复现安装步骤（可选依赖，不用于生产部署）：

```sh
uv sync --extra embedding --extra local-postgres
.venv/bin/python scripts/local_postgres.py setup
.venv/bin/python -m shsf init
```

`setup` 在项目虚拟环境内安装向量扩展；必要时下载 SHA-256 校验过的 pgvector 0.8.0 官方源码并编译，需要 C 编译器和 make。不会安装系统服务。数据库只信任本机 Unix socket 登录，不暴露网络端口。

日常管理：

```sh
.venv/bin/python scripts/local_postgres.py start
.venv/bin/python scripts/local_postgres.py status
.venv/bin/python scripts/local_postgres.py stop
```

setup/start 写入 `.data/pipeline/database-url`，此后 CLI 自动使用它。选择顺序：`--database-url` > `SHSF_DATABASE_URL` > 当前 data-dir 中的 `database-url` > SQLite 开发库。

完全离线的开发库：

```sh
.venv/bin/python -m shsf --database-url sqlite:///.data/dev.sqlite3 init
```

`.env.example` 是配置模板；CLI 自动读取项目根目录 `.env`，不会作为 shell 脚本执行。进程环境变量优先。从仓库根目录运行命令；所有原始数据和模型缓存默认保存在 `.data/pipeline/`。

## 个人画像与采集计划

```sh
.venv/bin/python -m shsf plan
```

`config/robotics.json` 配置六个主题：全身策略、类人控制、具身学习、机器人系统、具身智能体和基础方法。

- 2023 年至今的 `cs.RO` 元数据广泛保留。
- 2016 年以来的历史/外围论文通过主题词及机器人上下文筛选。
- 少量明确列出的基础方法允许早于 2016 年；更全面的经典论文需要通过后续种子或显式 ID 补充。
- 外围分类包括机器学习、视觉、控制、图形学、分布式系统、操作系统、网络、架构等；`cs.SY` 归一化为 `eess.SY`。
- 词法命中只用于低成本召回，不是语义推荐，也不能证明论文质量。会议、开源与实验质量目前仅记录为偏好，尚未自动验证。
- 画像指纹参与同步任务标识。修改画像会开启新的同步范围，不丢弃之前已入库的数据。

## 验证一小批真实数据

```sh
.venv/bin/python -m shsf fetch 2303.04137 2406.09246 2410.24164
.venv/bin/python -m shsf search 'diffusion'
.venv/bin/python -m shsf status
.venv/bin/python -m shsf report --output .data/pipeline/corpus-report.json
.venv/bin/python -m shsf embed --batch-size 3 --batches 1
```

`fetch` 使用 arXiv Atom API 精确获取指定 ID，属于显式导入，可以超出画像范围；这不代表已将它们设置为用户种子。单次最多 50 篇。每次成功 HTTP 响应先存入 `raw/arxiv/`，然后才解析、入库。

`report` 展示年份、六个主题的覆盖、采集完成状态及当前模型的有效向量比例。向量统计会排除旧正文指纹、其他模型和已删除论文，避免历史向量数量被误认为当前覆盖率。主题可重叠，因此主题计数之和不等于论文总数。

向量就绪后，可以用一篇论文找相似论文，检查 embedding 是否有实际区分能力：

```sh
.venv/bin/python -m shsf neighbors 2303.04137 --limit 10
```

这使用当前模型、当前正文指纹的余弦相似度，并排除论文自身。它是论文间相似性检索，不是个性化推荐分数，也不是用户喜欢概率。PostgreSQL 使用 pgvector 运算；SQLite 仅提供开发用精确扫描。

重放已有响应（不请求网络）：

```sh
.venv/bin/python -m shsf replay /absolute/path/to/response.xml
```

默认执行画像筛选；`--force` 用于有意导入范围外论文。正常入库会创建待执行的向量任务，重放不会重复创建相同任务。

## 历史回填与每日增量

历史 OAI 分页从较早的修改记录开始。首次使用时可先补一批最新论文，让本地检索尽早覆盖当前研究：

```sh
.venv/bin/python -m shsf recent --category cs.RO --limit 200
```

这只获取按首次提交时间倒序的一批候选（单次最多 1,000 条），仍执行画像入库规则；它不推进 OAI 水位，也不声称覆盖完整历史或全部新增论文。与 OAI 管道通过 arXiv ID 去重，可以同时使用。

使用官方 OAI-PMH `arXivRaw` 格式，保存论文版本历史。默认一次处理一页，适合先检查结果：

```sh
.venv/bin/python -m shsf harvest --category cs.RO --max-pages 1
```

重复相同命令会继续已提交的分页进度。确认后可用 `--max-pages 100` 处理更长批次；每页条数由服务端控制，不能把 max-pages 理解为论文数量。外围学科单独启动，例如：

```sh
.venv/bin/python -m shsf harvest --category eess.SY --max-pages 10
.venv/bin/python -m shsf harvest --category cs.LG --max-pages 10
```

外围学科仍需先读取该分类元数据，再在本地按画像筛选；不是服务端语义过滤，宽分类可能扫描很多最终不入库的记录。OAI 按元数据修改日期组织，不能按原始发表年份直接抓取，因此历史时间范围是在解析后筛选。

历史任务的 `complete` 变为 true 后，对同一学科运行增量：

```sh
.venv/bin/python -m shsf harvest --category cs.RO --incremental --max-pages 10
```

服务器已配置每日 08:00（北京时间）自动同步最新公告、续跑已初始化分类的增量采集、补齐向量并生成推荐；失败后当天每小时重试。历史回填仍需单独运行。

可靠性规则：

- 同一数据目录的 arXiv 请求通过文件锁串行，至少间隔 3.1 秒，重试也遵循间隔。多个数据目录或机器的调用方仍需统一限速。
- 对 429/暂时服务错误退避，并遵守 `Retry-After`；过长等待会明确终止，留待稍后续跑。
- 每页的论文、向量任务、删除标记、同步进度在同一事务内提交；失败则整页回滚。
- 原始 XML 和请求 manifest 独立保留，解析失败也能排查、重放。
- 只有整轮分页结束后才推进增量水位，取本轮第一份服务端响应日期，并保留两天重叠。
- `resumptionToken` 每日过期。过期时重置到相同范围的开头并提示重跑，依靠去重避免重复；绝不使用“最大已见日期”跳过未读记录。请尽量在连续批次中完成单个分类的初始回填。
- OAI 删除标记不等于论文撤稿。当前标记 metadata_deleted 并排除该记录，尚未实现完整的撤稿语义识别。
- `arXivRaw` 的作者是非结构化字符串，原样放入 authors_json 的单个元素，不凭逗号猜测作者边界。Atom 精确导入含结构化作者名。

## 异步 embedding

采集器只创建任务，向量由独立命令执行：

```sh
.venv/bin/python -m shsf embed --batch-size 8 --batches 10
```

采用 `allenai/specter2_base` + `allenai/specter2` proximity adapter，基座和 adapter 都固定到不可变提交。输入为 `title + tokenizer.sep_token + abstract`，最多 512 token，取 CLS、L2 归一化，768 维。长摘要会截断，这是当前基线的明确限制。

模型配置（包括 adapter、修订、输入格式、截断与归一化）共同决定 model_key。每个任务按 `(arxiv_id, model_key, input_hash)` 唯一去重；只有标题/摘要改变才重新生成。旧向量保留用于追踪，未来检索必须联合 papers 当前 input_hash 与选定 model_key，不能混用版本。

任务具备租约、排他领取、指数重试和最大重试次数。PostgreSQL 使用 `FOR UPDATE SKIP LOCKED`，多个 worker 可并行；SQLite 通过写事务串行领取。模型先加载，再领取任务，避免首次权重下载占满租约。模型缓存已就绪后可以离线执行：

```sh
HF_ENDPOINT=https://huggingface.co HF_HUB_OFFLINE=1 .venv/bin/python -m shsf embed --batches 10
```

若本机配置了不可达的 Hugging Face 镜像，可用 `HF_ENDPOINT=https://huggingface.co` 显式选择官方站点。脚本不会自动替换用户的镜像配置。当前没有伪造 embedding，也不会用 TF-IDF 或随机向量替代模型结果。

## 数据库表

| 表 | 内容 |
|---|---|
| papers | 最新论文元数据、版本历史、内容指纹、原始响应引用 |
| paper_profile_matches | 画像指纹、主题命中和入库原因 |
| paper_contents | 按需获取、解析和缓存官方 HTML；不使用 PDF/LaTeX 回退 |
| ingestion_jobs / ingestion_pages | 分页状态、水位和原始响应审计 |
| embedding_models | 不可变模型配置及维度 |
| embedding_jobs | 异步向量任务、租约与重试 |
| paper_embeddings | 按模型和输入版本保存的真实向量 |
| schema_migrations | 迁移版本与校验和 |

PostgreSQL 使用 pgvector 的 vector 类型，当前未创建近邻索引；采集初期先验证数据质量，后续按选定模型建立索引。JSON 结构字段当前以 JSON 文本保存，方便与 SQLite 开发后端保持一致。

全文策略已确定：**只获取官方 HTML，按需缓存和解析，不退回 LaTeX/PDF**。没有 HTML 的论文仍可入库并计算标题/摘要向量。仅预留状态，不会把推导出的 HTML 地址当作已验证可用。

## 测试

```sh
.venv/bin/python -m unittest discover -s tests -v
```

默认执行离线解析和 SQLite 合约测试。PostgreSQL 合约测试必须显式指定隔离库，绝不自动复用项目数据库：

```sh
SHSF_TEST_DATABASE_URL='postgresql:///shsf_test?host=/tmp/shsf-pg-socket&port=55439' \
  .venv/bin/python -m unittest discover -s tests -v
```

测试会清空该测试库中的应用数据。覆盖版本去重、旧版本保护、输入更新与过期任务、事务回滚、任务租约与重试、OAI 续传、token 过期恢复、增量水位及画像筛选。真实网络与模型验证独立执行，测试中的替身向量不写入项目数据库。

## 官方参考

- [arXiv OAI-PMH](https://info.arxiv.org/help/oa/index.html)
- [arXiv API](https://info.arxiv.org/help/api/user-manual.html)
- [arXiv 批量访问](https://info.arxiv.org/help/bulk_data.html)
- [SPECTER2](https://huggingface.co/allenai/specter2)

Thank you to arXiv for use of its open access interoperability.

### 本地种子选择网页与 Zotero 导入

在 PostgreSQL 已启动的情况下运行：

```sh
.venv/bin/python -m shsf serve --port 8766
```

打开 <http://127.0.0.1:8766>。网页从保存的数据库连接配置读取现有文献库，也支持全局 `--database-url` 指定独立数据库。服务仅监听 `127.0.0.1`。

- 左侧管理种子，右侧按标题/摘要、作者、arXiv ID 或 DOI 检索；支持分页、按相关性或发表时间排序。
- 草稿自动保存，最多 100 篇，建议 20–40 篇；确认会保存独立元数据快照，后续调整不会覆盖旧快照，直到再次确认。
- Zotero：点击“从 Zotero 导入”，填写具有**个人文献库读取权限**的 API Key，User ID 可自动识别。选择收藏夹、预览并勾选条目，再导入。收藏夹只包含直接归属条目，子收藏夹可单独选择。
- 只使用 Zotero GET API，不下载附件、不向 Zotero 写入。API Key 仅保留在服务内存中，断开或重启后需重新连接；不要把密钥写进配置或提交到仓库。
- 按 arXiv ID、DOI、无标识冲突的精确标题匹配与去重。无匹配或多义条目保留元数据并标记待处理；当前不自动抓取这些论文，也不后台自动重匹配，可在文献入库后重新导入或搜索添加。
- 确认保存种子，可进入每日推荐生成 embedding 基线推荐，不触发个性化训练。Agent 精读已按需接入；Zotero 导出与 Obsidian 写入属于后续阶段。

本机服务为单用户应用；多个页面同时编辑发生版本冲突时会重新读取当前选择并提示重试。检索为本地关键词匹配，不调用 embedding 服务。

### 每日推荐：embedding 基线

服务器默认打开每日推荐页（端口 8765）；日更完成后自动生成，也可手动点击“生成今日推荐”。不训练个人模型。

- 使用已确认种子在当前 SPECTER2 模型下的有效向量；未就绪种子单独计数，未确认的草稿不参与推荐。
- 每篇就绪种子分别召回全时段与最近官方公告批次的相邻论文，再按最相近的 3 篇种子的平均余弦相似度排序。每批最多 20 篇，优先安排最新公告中的 6 篇新论文，不足时补其他相关研究；页面显示实际数量。减少重复标题/DOI，并适度分散内容和种子方向。
- 排除种子论文、已反馈/已读论文、近 30 天之前批次已展示的论文，以及失效向量和已撤回记录。未计算向量的候选无法参加推荐，页面同时展示当前与本批生成时的覆盖率。
- 同一日期（Asia/Shanghai）、已确认种子配置、公告日期、算法和模型对应一份固定批次。刷新只读取，新增向量不会改变当天已经生成的结果。重新确认种子后可生成新配置的批次。
- 每篇展示平均余弦相似度及相近种子，分数不是喜欢概率。摘要使用原始论文摘要，不调用 LLM。
- 感兴趣/不感兴趣/已读反馈持久化，可再次点击撤销。反馈先用于后续新批次去重，不用于训练或调整模型权重。
- 摘要默认英文，可切换中文；后台使用已保存的推荐解读模型预先翻译并缓存。切换语言不调用模型，
- 每日批次自动生成，支持手动生成；没有消息推送。Zotero 导出和 Obsidian 写入仍未接入；Agent 精读从论文详情页按需生成。

向量补算进度可查看 `.data/pipeline/embedding-backfill.log`，进程中断后用现有 `embed` 命令继续，已完成任务不会重复计算。

### SHSF 更名与已有安装

正式品牌为 **SHSF · Stay Hungry, Stay Foolish.**；Python 包、命令为 `shsf`，环境变量使用 `SHSF_*`。更新安装后运行 `python -m shsf serve --port 8766` 或 `shsf serve --port 8766`。

现有 `.data/`、数据库连接文件、迁移校验、种子、反馈和模型缓存保持原位。后台进程需要使用新命令重新启动。已有环境变量配置需切换为 `SHSF_*`。

新的独立安装可使用文档中的 `shsf_test` 示例；已有安装的测试连接应指向原来的隔离测试库，禁止将 `SHSF_TEST_DATABASE_URL` 指向正式库。`scripts/local_postgres.py start` 会输出沿用配置得到的连接地址。

### Agent 模型配置与 ChatGPT 登录

打开 <http://127.0.0.1:8766/#agent>。当前接入配置、官方登录、账户模型选择和显式连接测试；配置完成后可在推荐卡片和论文详情页按需生成解读。

项目根目录 `.env` 会自动读取，进程环境变量优先。API 配置示例：

```dotenv
SHSF_AGENT_MODE=api
SHSF_LLM_PROVIDER=openai_compatible
SHSF_LLM_BASE_URL=https://api.openai.com/v1
SHSF_LLM_API_KEY=填入自己的密钥
SHSF_LLM_PROTOCOL=chat_completions
SHSF_LLM_MODEL=填入服务支持的模型名称
SHSF_AGENT_EXPLAIN_MODEL=
SHSF_AGENT_READ_MODEL=
SHSF_LLM_TIMEOUT=60
```

- `BASE_URL` 填基础路径，程序按协议追加 `/chat/completions` 或 `/responses`。协议可选 `chat_completions`、`responses`。
- 两种任务的模型留空时回退到 `SHSF_LLM_MODEL`。超时为 5–180 秒。本机 HTTP 模型服务可不填密钥；远程服务使用 HTTPS。
- 编辑 `.env` 后在页面点击“重新读取 .env”。数据库连接变量默认注释，继续沿用已有 `.data/pipeline/database-url`，避免复制示例后意外切换数据库。
- 页面保存的通道选择保存在 `.data/pipeline/agent/chatgpt.json` 的 settings 字段中，优先于 `.env` 的 `SHSF_AGENT_MODE`。API 地址、密钥和模型仍以 `.env`/进程环境为准。
- ChatGPT 通道：点击 `Continue with ChatGPT`，再打开生成的官方授权链接，由用户完成登录和套餐授权。支持保存多个账户注册、切换、重新登录和退出。读取当前账户模型列表，为两种任务选择模型后保存通道。
- 仅身份登录不等于套餐使用授权。账户、工作区和所选模型能否实际调用，以官方返回及连接测试为准；额度不足不自动转用 API。
- OAuth 使用动态注册、PKCE S256、state/nonce 和 ID token 签名/issuer/audience/expiry 校验。首次主机 ID、账户注册和凭据位于 `.data/pipeline/agent/`；目录 0700，凭据原子写入 0600 文件，token 刷新通过文件锁串行化。不会读取或复用 Codex/浏览器已有凭据。
- 退出会尝试撤销远程会话并清除本机 token；网络失败时明确提示远程撤销未确认，可在 ChatGPT 设置中断开 SHSF。客户端注册和主机 ID保留供下次登录。
- 连接测试只发送固定短文本，不发送论文或研究画像，会消耗所选通道少量用量。网页只显示密钥是否配置，不返回密钥或 OAuth token。

也可使用命令行检查配置；默认不发模型请求：

```sh
.venv/bin/python -m shsf agent-config
.venv/bin/python -m shsf agent-config --test --task explain
```

官方协议：[本地应用套餐调用](https://developers.openai.com/siwc/token-sharing-open-source)、[登录](https://developers.openai.com/siwc/token-sharing-open-source/sign-in)、[模型调用](https://developers.openai.com/siwc/token-sharing-open-source/models-and-inference)。真实账户尚需用户授权验证；自动测试使用模拟模型服务和本地签名测试 token。

### 推荐解读与单篇精读

先在 Agent 设置中配置 API 或 ChatGPT 通道。每次点击“生成”才会请求所选模型，不自动切换通道。

- 推荐卡片“推荐解读”：展开后生成摘要解读，输入为标题、摘要、相近种子和研究画像，明确标注未阅读全文。
- 推荐卡片“单篇精读”：进入独立详情页，获取当前版本官方 arXiv HTML，分析正文、公式文本、表格及图注；不进行图片视觉核验。详情页也可直接通过 `/#paper/2406.09246` 打开库内论文。
- HTML 不可用或正文不足时显示失败原因，不回退到 PDF/LaTeX，不生成伪装成全文精读的摘要报告。
- 长论文按完整正文分段提取有引用的笔记，再汇总；可能产生多次模型调用。当前最大正文长度 250,000 字符、最多 20 个分段，超出时停止，不静默截断。
- 报告区分作者陈述、分析推断和证据不足。所有引用 ID 和逐字摘录在服务端与输入资料核对，但这不等于分析结论已被独立验证。
- 报告和分段笔记落库。论文内容/版本、模型通道与配置、研究画像、相近种子或提示词/解析器版本变化会产生新缓存。相同输入复用已有结果。旧报告保留，失败任务需要手动重试。
- 后台任务显示队列、分段和校验进度。离开页面任务继续，重新打开会恢复显示；服务重启后未完成任务标为中断，不自动重复可能计费的请求。
- HTML 原始页面及解析资料位于 `.data/pipeline/contents/`，任务及报告位于 `analysis_jobs`，HTML 状态复用 `paper_contents`。

当前实现是固定步骤的研究阅读工作流，不自行执行论文代码、不自动打开项目仓库或调用外部工具。


## 许可证

SHSF 中本项目有权授权的原创代码与文档采用 **GNU Affero General Public License v3.0 only（AGPL-3.0-only）**，完整条款见 [LICENSE](LICENSE)。

允许个人使用、修改和商业使用。分发本项目或其修改版时，须按许可证保留相关声明并提供对应源码；修改本项目并通过网络向用户提供服务时，须向这些用户显著提供免费获取该修改版对应源码的途径。软件按现状提供，不提供担保。上述说明仅为摘要，以许可证全文为准。

第三方依赖、模型权重、论文原文及其他第三方材料遵循各自的许可证或使用条款，不因本仓库采用 AGPL-3.0-only 而改变授权。
