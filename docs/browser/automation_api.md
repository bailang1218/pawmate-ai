# PawMate 浏览器自动化内核与测试 API

这套 API 与 PawMate 的“浏览器自动化”工作区共用同一个 `BrowserAutomationService`，用于独立测试和外部集成，不会维护第二套 Playwright 状态。桌面 UI 通过 QWebChannel 调用同一服务；异步调用立即返回 request id，完成结果通过信号回传，不阻塞 Qt 主线程。

## 三种能力

1. `workflow`：确定性的 RPA 工作流。支持版本、输入、顺序步骤、`if`、`foreach`、断言、等待、有限重试、取消、检查点和 trace。
2. `agent`：AI 每轮读取页面，只规划一个语义动作，再经过 DSL 校验和风险策略后执行。没有坐标盲点、任意 JavaScript 或无限重试。
3. `ai_generated_workflow`：AI 根据需求生成工作流草稿，先做结构和安全校验；可 dry-run 后再保存、执行。

三种模式复用现有 `browser_goto / browser_read / browser_act / browser_extract` facade，因此底层只有一套 BrowserRouter 和 Playwright 会话。

## 为什么保留 Playwright

保留的是 Playwright/CDP 执行后端，不保留旧的 Windows `SetParent` 浏览器嵌入层。受控浏览器使用独立前台窗口，PawMate 内部只显示自动化状态、工作流和 trace。这样既保留 DOM、可访问性树、网络等待、下载和稳定输入能力，也消除了嵌入层造成的点击穿透、Z-order、缩放、焦点与启动卡死问题。

模型只看见四个高层门面工具；旧的低层 Playwright/native 工具不再重复注册。工作流、AI 直接操作和普通聊天工具调用最终都进入同一 BrowserRouter。

桌面端的浏览器自动化工作台提供四个统一栏目：直接梳理需求、维护工作流、查看运行记录/trace、配置 Playwright/CDP。用户可以完全不经过聊天直接创建 AI 操作或 AI 生成工作流；从聊天创建的任务仍会汇入同一工作流与运行记录。

## 启动

建议通过环境变量传 token，避免 token 留在命令历史中。token 至少 24 个字符，服务只允许绑定 loopback 地址。

```powershell
$env:PAWMATE_AUTOMATION_TOKEN = "replace-with-a-random-token-at-least-24-chars"
.\.venv311\Scripts\python.exe -m pawmate.browser_automation.api --host 127.0.0.1 --port 18766
```

如果没有提供 token，服务会生成并仅在启动时打印一次。

```powershell
$headers = @{
  Authorization = "Bearer $env:PAWMATE_AUTOMATION_TOKEN"
  "Content-Type" = "application/json"
}
Invoke-RestMethod http://127.0.0.1:18766/health
```

`/health` 只返回最小状态，不需要 token；其他接口全部鉴权。请求体上限为 256 KiB，不开放 wildcard CORS。

## 先测试确定性工作流

创建工作流：

```powershell
$workflow = Get-Content -Raw -Encoding UTF8 .\docs\browser\examples\read_page.workflow.json
$workflowBytes = [Text.Encoding]::UTF8.GetBytes($workflow)
$created = Invoke-RestMethod -Method Post `
  -Uri http://127.0.0.1:18766/v1/workflows `
  -Headers $headers `
  -ContentType "application/json; charset=utf-8" `
  -Body $workflowBytes
$workflowId = $created.workflow.id
```

先 dry-run。dry-run 不打开浏览器，也不要求高风险批准，但会返回每一步的风险等级：

```powershell
$run = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:18766/v1/workflows/$workflowId/runs" `
  -Headers $headers `
  -Body '{"inputs":{"url":"https://example.com"},"dry_run":true}'
$runId = $run.run.id
Invoke-RestMethod -Headers $headers -Uri "http://127.0.0.1:18766/v1/runs/$runId"
```

真实执行：

```powershell
$run = Invoke-RestMethod -Method Post `
  -Uri "http://127.0.0.1:18766/v1/workflows/$workflowId/runs" `
  -Headers $headers `
  -Body '{"inputs":{"url":"https://example.com"}}'
$runId = $run.run.id
Invoke-RestMethod -Headers $headers -Uri "http://127.0.0.1:18766/v1/runs/$runId/trace"
```

运行是异步的：创建返回 HTTP 202，通过 run 查询状态。当前浏览器 runtime 一次只运行一个任务；进程内和同一数据根下的跨进程并发启动都会返回 409，不会让多个任务抢同一个页面。

## 高风险审批

提交、发送、付款、删除、现有登录态等动作默认不会执行。显式放行 high risk 时，需要同时提交可审计批准信息：

```json
{
  "inputs": {},
  "allow_high_risk": true,
  "approval": {
    "approved": true,
    "approved_by": "local-api-tester",
    "reason": "已核对目标页面与提交内容"
  }
}
```

critical 动作还需要 `allow_critical: true` 和 `approval.scope: "critical"`。仅设置布尔值但没有批准记录会返回 422。

## AI 生成与 AI 直接操作

这两种模式会把目标以及必要的页面观察发送给 PawMate 当前配置的模型服务，因此 API 要求显式 `allow_model_data: true`。

生成工作流草稿：

```powershell
$body = @{
  goal = "打开 example.com，读取正文并确认页面标题存在"
  context = @{}
  save = $false
  allow_model_data = $true
} | ConvertTo-Json -Depth 10
$bodyBytes = [Text.Encoding]::UTF8.GetBytes($body)
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:18766/v1/workflows/generate -Headers $headers -ContentType "application/json; charset=utf-8" -Body $bodyBytes
```

AI 直接操作：

```powershell
$body = @{
  goal = "打开 example.com 并告诉我页面标题"
  max_turns = 10
  allow_model_data = $true
} | ConvertTo-Json
$bodyBytes = [Text.Encoding]::UTF8.GetBytes($body)
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:18766/v1/agent/runs -Headers $headers -ContentType "application/json; charset=utf-8" -Body $bodyBytes
```

AI 每轮最多执行一个已验证动作；同一个失败动作重复两次会终止，超过 `max_turns` 会终止，页面内容只被视为不可信观察。

## 路由

| 方法 | 路由 | 用途 |
|---|---|---|
| GET | `/health` | 最小健康状态 |
| GET | `/v1/capabilities` | 动作集合、安全默认值和硬限制 |
| POST | `/v1/workflows/validate` | 只校验 DSL |
| POST | `/v1/workflows/generate` | AI 生成并校验草稿 |
| POST/GET | `/v1/workflows` | 创建、列出工作流 |
| GET/PUT | `/v1/workflows/{id}` | 查询、创建新版本 |
| GET | `/v1/workflows/{id}/versions/{version}` | 查询历史版本 |
| POST | `/v1/workflows/{id}/runs` | dry-run 或真实执行 |
| POST | `/v1/agent/runs` | AI 直接操作 |
| GET | `/v1/runs` | 按时间倒序列出运行记录 |
| GET | `/v1/runs/{id}` | 查询检查点和最终结果 |
| POST | `/v1/runs/{id}/cancel` | 取消运行 |
| GET | `/v1/runs/{id}/trace` | 查询逐步 trace |

## 当前安全边界

- 只允许绝对 `http://`、`https://` URL；拒绝 URL 内嵌账号密码。
- DSL 没有 JavaScript、shell、文件 URL 和坐标点击。
- click/fill/type 必须携带真实 `intent` 供风险分类；Enter/Ctrl+Enter 一律按提交动作处理。
- 拒绝云实例 metadata、link-local、multicast 和 unspecified 地址。
- 写动作禁止自动重试；读、等待等动作最多三次，不会从第 0 步重跑整个工作流。
- 工作流最多 200 步、8 层嵌套、100 次循环；单步最长 120 秒。
- secret 输入不会写入 run checkpoint 或 trace，也禁止在工作流中保存 secret 默认值或敏感字段字面量；敏感输入必须引用 `secret=true` 的运行时输入。
- AI 直接操作的原始 goal 只在内存中使用，不写入 run checkpoint。
- 工作流每个版本永久保留；每一步完成后原子写 checkpoint。
- 进程异常退出后不会自动重放可能有副作用的步骤；下次启动会保留检查点并标记 `process_interrupted`。
