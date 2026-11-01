# 豆包 AI Cache Proxy v3

这版代理支持当前主工作流的 DeepSeek 请求。原版单条阶段与提速测试版批量阶段并存，共 8 个缓存阶段；`positive-overall` 直通且不缓存。

## 支持阶段

| X-Cache-Stage | 对应节点 |
|---|---|
| `quality` | 旧版单条 Quality Filter（兼容保留） |
| `quality-batch` | 提速测试版批量 Quality Filter |
| `issue` | 旧版单条 Feedback Analyzer（兼容保留） |
| `issue-batch` | 提速测试版批量 Feedback Analyzer |
| `clustering` | DeepSeek - Issue Clustering |
| `global-consolidation` | DeepSeek - Global Cluster Consolidation V1 |
| `problem-insight` | DeepSeek - Problem Product Insight V1 + Retry |
| `positive-insight` | DeepSeek - Positive Product Insight V1 |
| `positive-overall` | DeepSeek - Positive Overall Report（直通，不缓存） |

缓存 Key 包含：

- CACHE_NAMESPACE
- stage
- 完整 DeepSeek 请求 body

所以 Prompt、模型、messages、参数等发生变化时，会产生新的缓存 Key。

其他阶段只缓存：

- HTTP 200
- finish_reason = stop
- 结果 JSON 合法
- 结果符合对应阶段的工作流解析规则

失败、超时、无效 JSON、字段不完整的响应不会写入缓存。

## 正向整体报告：不使用缓存

`positive-overall` 保留 `X-Cache-Stage` 标记用于识别请求，但代理会：

- 不读取该阶段的历史缓存；
- 不写入该阶段的新缓存；
- 不在代理层执行 `positive-overall` 专属 JSON 校验；
- 将 DeepSeek 的 HTTP 200 原始响应直接返回给 n8n。

报告格式和业务规则仍由 n8n 的 `Parse & Validate Positive Overall Report` 节点负责检查。代理启动时也会清理数据库中遗留的 `positive-overall` 缓存记录。提速测试版使用独立的 `quality-batch` 和 `issue-batch` 阶段，旧版 `quality` / `issue` 阶段继续保留，便于回退。

## Docker

继续使用原来的：

- 网络：`doubao-ai-net`
- 数据卷：`doubao-ai-cache-data`
- 数据库：`/data/cache.sqlite3`

不需要创建新的缓存容器或新的数据库。

## 健康检查

```powershell
docker exec n8n node -e "fetch('http://ai-cache-proxy:8000/health').then(r=>r.text()).then(console.log)"
```

正常会返回 `status=ok`、9 个支持阶段，以及 `cache_bypass_stages: ["positive-overall"]`。

## 注意

当前工作流 JSON 已经同时修改：

- 8 个 DeepSeek 节点全部改为访问 `ai-cache-proxy`
- `Problem Product Insight Retry` 与 Problem Insight 共用 `problem-insight`
- 为原来没有 `response_format` 的 Product Insight / Overall Report 请求补上 `response_format: {"type":"json_object"}`
- 没有新增 n8n 缓存节点
- 没有删除原有业务节点

导入工作流前，建议先在 n8n 导出当前版本作为备份。
