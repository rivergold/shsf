<div align="center">

# SHSF

**Stay Hungry, Stay Foolish.**

### 发现值得读的研究，追问尚未懂的问题。

从兴趣论文出发，发现相关研究，阅读有原文依据的精读报告。<br>
一个可自托管的个人论文发现与阅读工具。

**[体验演示](#交互演示)** · **[快速开始](#快速开始)** · **[界面预览](#界面预览)** · **[使用指南](#使用指南)**

<sub>Python 3.10+ &nbsp; / &nbsp; PostgreSQL + pgvector &nbsp; / &nbsp; SPECTER2 &nbsp; / &nbsp; AGPL-3.0-only</sub>

</div>

<br>

![SHSF 每日推荐：论文卡片、中英文摘要与相近兴趣论文](design/previews/readme-feed.png)

<p align="center"><sub>从几篇感兴趣的论文出发，建立自己的研究阅读路径。</sub></p>

## 从发现到精读

| 发现相关研究 | 理解论文内容 | 留下阅读记录 |
| :--- | :--- | :--- |
| 选择兴趣论文，使用 SPECTER2 发现相近研究；展示推荐依据，支持中英文摘要。 | 从推荐卡片进入单篇精读，基于官方 arXiv HTML 生成带引用的报告。 | 保存精读任务与历史报告，按标题检索、筛选和收藏，记录兴趣与已读反馈。 |

支持从 Zotero **只读导入**文献，也可以直接检索本地文献库选择兴趣论文。

## 界面预览

<table>
<tr>
<td width="50%"><strong>精读记录</strong><br><sub>阅读进度、历史报告与收藏，集中管理。</sub></td>
<td width="50%"><strong>单篇精读</strong><br><sub>围绕问题、方法、实验与局限组织报告。</sub></td>
</tr>
<tr>
<td><img src="design/previews/readme-readings.png" alt="精读记录：任务进度、标题搜索与收藏"></td>
<td><img src="design/previews/readme-report.png" alt="单篇精读：论文摘要与结构化报告"></td>
</tr>
</table>

<sub>截图与交互演示使用模拟数据，仅用于展示界面，不代表真实研究结论或推荐效果。</sub>

## 交互演示

**不需要安装、数据库或模型密钥。** 可体验每日推荐、摘要切换、阅读反馈、精读报告、搜索与收藏。

下载 [独立演示页面](demo/index.html)，用浏览器打开；或查看 [本地预览与 GitHub Pages 发布说明](demo/README.md)。GitHub 文件预览不会执行 HTML，需要下载后打开或通过静态网站访问。

也可以在仓库根目录运行：

```sh
python3 -m http.server 8000 --bind 127.0.0.1 --directory demo
```

打开 **<http://127.0.0.1:8000/>**。演示操作只保存在当前页面内存中，刷新后重置，不连接真实模型、账户或数据库。

## 快速开始

需要 **Python 3.10+、uv 和 Docker Compose**。以下命令从仓库根目录执行。

### 1. 安装依赖

```sh
uv sync --extra embedding
cp .env.example .env
```

首次计算向量时会下载 SPECTER2 模型，默认缓存在 `.data/pipeline/models/`。可以使用 CPU，也可在向量命令中指定 `--device mps` 或 `--device cuda`。

### 2. 启动数据库

将下方密码替换为自己的本地密码，两处保持一致；连接 URL 中的特殊字符需要 URL 编码。

```sh
export POSTGRES_PASSWORD='replace-with-your-local-password'
docker compose up -d --wait
export SHSF_DATABASE_URL='postgresql://shsf:replace-with-your-local-password@127.0.0.1:5432/shsf'
uv run shsf init
```

已有 PostgreSQL + pgvector 时，直接设置 `SHSF_DATABASE_URL` 并初始化即可。数据库连接也可以写入 `.env`，供后续终端会话使用。

### 3. 导入论文并启动

```sh
uv run shsf fetch 2303.04137 2406.09246 2410.24164
uv run shsf embed --batch-size 3 --batches 1
uv run shsf serve --port 8766
```

打开 **<http://127.0.0.1:8766>**。

这三篇论文用于验证安装。需要更多候选时，运行 `uv run shsf recent --category cs.RO --limit 200`，再运行 `uv run shsf embed --batch-size 8 --batches 25` 补算向量。

## 使用指南

**① 选择兴趣论文** → **② 生成每日推荐** → **③ 按需精读** → **④ 收藏与回顾**

| 操作 | 说明 |
| :--- | :--- |
| 选择兴趣论文 | 在种子管理页检索文献，或从 Zotero 导入。确认选择后用于推荐，建议选择 20–40 篇。 |
| 生成每日推荐 | 候选论文与兴趣论文的向量就绪后，点击“生成今日推荐”；查看相近论文、推荐依据与摘要。 |
| 配置模型 | 在 Agent 设置页配置通道与模型；API 模式的配置模板见 `.env.example`。 |
| 开始精读 | 从论文卡片进入详情页，按需生成报告；在精读记录中查看进度、历史与收藏。 |

基础采集与相似度推荐不需要 LLM 密钥。摘要翻译、推荐解读和单篇精读需要配置模型；模型请求可能产生费用。

<details>
<summary><strong>模型配置示例</strong></summary>

在项目根目录 `.env` 中填写服务配置：

```dotenv
SHSF_AGENT_MODE=api
SHSF_LLM_PROVIDER=openai_compatible
SHSF_LLM_BASE_URL=https://api.openai.com/v1
SHSF_LLM_API_KEY=填入自己的密钥
SHSF_LLM_PROTOCOL=chat_completions
SHSF_LLM_MODEL=填入服务支持的模型名称
```

`BASE_URL` 不包含 `/chat/completions` 或 `/responses` 后缀。编辑后在 Agent 设置页点击“重新读取 .env”，选择通道、模型并执行连接测试。测试会发送固定短文本并消耗少量用量。

两种任务可通过 `SHSF_AGENT_EXPLAIN_MODEL` 和 `SHSF_AGENT_READ_MODEL` 分别设置模型；留空时使用 `SHSF_LLM_MODEL`。不要提交 `.env`、API 密钥或账户凭据。

</details>

<details>
<summary><strong>采集、向量与常用命令</strong></summary>

默认画像为 `config/robotics.json`，面向机器人与具身智能研究，可按自己的方向调整。

```sh
# 查看画像、采集计划和当前状态
uv run shsf plan
uv run shsf status

# 获取一批近期论文
uv run shsf recent --category cs.RO --limit 200

# 历史回填；重复执行会继续分页进度
uv run shsf harvest --category cs.RO --max-pages 10

# 初始回填完成后执行增量采集
uv run shsf harvest --category cs.RO --incremental --max-pages 10

# 补算向量；每次最多处理 10 批
uv run shsf embed --batch-size 8 --batches 10

# 查找一篇论文的相近研究
uv run shsf neighbors 2303.04137 --limit 10
```

采集只创建向量任务，需单独运行 `embed`。每批处理数量受参数限制；有待处理任务时可继续执行。使用 `uv run shsf --help` 查看完整命令。

</details>

<details>
<summary><strong>本地数据与开发验证</strong></summary>

原始响应、模型和账户凭据默认保存在 `.data/pipeline/`。应用读取项目根目录 `.env`，进程环境变量优先。Web 服务为单用户应用，默认仅监听 `127.0.0.1`。

无 PostgreSQL 时，可以显式使用 SQLite 验证离线采集与解析；SQLite 不提供 pgvector 检索能力：

```sh
uv run shsf --database-url sqlite:///.data/dev.sqlite3 init
```

运行离线测试与重新生成演示：

```sh
uv run python -m unittest discover -s tests -v
uv run python scripts/build_demo.py
```

PostgreSQL 合约测试需要通过 `SHSF_TEST_DATABASE_URL` 指定独立测试库，测试会清空该库中的应用数据，请勿指向正式库。

</details>

## 当前范围

- 推荐采用 SPECTER2 相似度基线，不训练个人模型；相似度分数不代表喜欢概率。
- 精读只使用官方 arXiv HTML；没有可用 HTML 时明确失败，不回退到 PDF 或 LaTeX。
- 报告核对引用与逐字摘录，区分作者陈述、分析推断和证据不足；不进行论文图片的视觉核验。
- Zotero 支持只读导入；Zotero 导出、Obsidian 保存和消息推送尚未实现。

## 致谢与许可证

论文元数据来自 arXiv，相似度模型使用 [SPECTER2](https://huggingface.co/allenai/specter2)。Thank you to arXiv for use of its open access interoperability.

本项目有权授权的原创代码与文档采用 **[AGPL-3.0-only](LICENSE)**。第三方依赖、模型权重与论文材料遵循各自的许可条款。
