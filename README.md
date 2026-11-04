# 豆包用户反馈智能分析工作流

这是一个面向产品经理的公开用户反馈（VOC）分析项目。项目使用 n8n 编排采集、质量筛选、Issue 拆解、两阶段聚类、Priority Score、Product Insight 和 PM Review 流程，并使用 DeepSeek 生成结构化分析结果。

## 项目组成

- `Doubao - Dynamic Analysis Test .json`：当前主分析与 PM Review 工作流（批量提速版）。
- `Doubao - Analysis Control API .json`：当前启动分析 API，调用上述主工作流。
- `Doubao - Analysis Status API.json`：查询批次状态 API。
- `Doubao - Analysis Error Handler.json`：统一错误处理工作流。
- `ai-cache-proxy/`：DeepSeek 请求代理、缓存、校验及测试。
- `frontend/`：默认前端入口，已对齐当前提速测试版 Webhook 路径。
- `docs/`：项目说明、采集说明、数据存储说明、操作手册和当前提示词。
- `evaluation/`：聚类错误案例复盘材料与批量处理离线测试。

## 运行前准备

1. 准备可运行的 n8n 实例，并确认工作流使用的 Data Tables 已存在。**如果当前部署已正常运行，无需重新导入或重启。**
2. 将四个工作流 JSON 导入 n8n。导入后核对 Workflow ID、Data Table ID、Webhook 路径和错误工作流配置；这些 ID 与具体 n8n 环境绑定。
3. 在 `ai-cache-proxy/` 下复制 `.env.example` 为 `.env`，设置自己的 `DEEPSEEK_API_KEY`。不要提交 `.env`。
4. 构建并启动缓存代理，使其与 n8n 位于同一 Docker 网络，并将持久化卷挂载到 `/data`。详细说明见 [`ai-cache-proxy/README.md`](./ai-cache-proxy/README.md)。
5. 根据自己的本地或部署环境检查 `frontend/app.js` 中的 n8n Webhook 基础地址。当前前端默认调用提速测试版接口；启动请求使用 `application/x-www-form-urlencoded`，避免浏览器因 JSON Content-Type 发起 OPTIONS 预检。若前端端口不是 `5500`，还需同步调整启动 API 与状态 API Webhook 的 Allowed Origins (CORS)。

## 缓存说明

当前提速版以下 8 个阶段使用缓存：`quality`、`quality-batch`、`issue`、`issue-batch`、`clustering`、`global-consolidation`、`problem-insight`、`positive-insight`。其中 `quality` 和 `issue` 为旧版单条阶段兼容保留。

`positive-overall`（正向整体报告）绕过缓存，由 n8n 的解析与校验节点处理。缓存数据库位于 Docker 持久化数据卷，不随项目源码分发。批量处理分别使用 `quality-batch`、`issue-batch` 独立缓存阶段。

`quality-batch` 和 `issue-batch` 还支持可选的单评论缓存：设置 `REVIEW_CACHE_ENABLED=1` 后，调整批次顺序或混入少量新评论时，只会把未命中的评论发送给模型。该功能默认关闭，可通过恢复为 `0` 立即回退，不会改变原有整批缓存行为。

## 文档入口

- [项目说明](./docs/项目说明.md)
- [数据采集说明](./docs/数据采集说明.md)
- [数据存储说明](./docs/数据库说明.md)
- [工作流操作手册](./docs/工作流操作手册.md)
- [前端启动故障修复说明](./docs/前端启动故障修复说明.md)
- [标签体系设计](./docs/标签体系设计.md)
- [大模型提示词总览](./docs/大模型提示词/提示词总览.md)

## GitHub 与安全注意事项

- 本仓库的公开版本应使用清理后的当前源码快照，不要推送曾包含真实评论 CSV / Excel 等数据文件的旧本地 Git 历史。
- 仓库不包含真实评论 CSV / Excel、分析数据、SQLite 缓存、`.env` 或本地虚拟环境。
- `.env.example` 只有占位符，部署时必须填入自己的凭据。
- 如果原始 `.env` 中的真实 API Key 曾被提交到任何远程仓库或分享给他人，请先在服务商后台撤销并重新生成。
- 当前主工作流包含一个固定的请求 `PhoneGUID`。公开仓库前应确认它不是个人或生产环境专属标识；更换后必须重新验证采集接口。
- 工作流 JSON 与前端仍需要针对实际 n8n 环境做导入、连通性和端到端验证；代码入库不等于已经完成部署验收。

## 本地离线检查

在 **项目根目录** 执行（无需启动工作流，不会调用 DeepSeek）：

```powershell
python -m pip install -r .\ai-cache-proxy\requirements.txt
python -m pip install pytest
Push-Location .\ai-cache-proxy
python -m pytest -q
Pop-Location
node .\evaluation\performance\test_batch_workflow.js
```

以上为源码与模拟响应测试，不能代替本机 n8n、Data Tables 和 Dashboard 的真实运行验收。

系统输出用于辅助产品分析，不代表豆包全部用户，也不自动替代产品经理的判断。

## 批量处理版本

提速测试版的导入、代理更新、测试和回退步骤见 [`docs/提速测试版说明.md`](docs/提速测试版说明.md)。本仓库只保留当前正在使用的批量版导出；历史旧版工作流不在仓库中。
