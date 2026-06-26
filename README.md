# sw2api

stagewise 多账号反向代理 + Web 管理面板。OpenAI 兼容接口，支持多账号自动切号、限额冷却。

支持**手动输入 Token** 添加账号，也支持**批量导入**（`email|token` 格式）。

## 功能

- **反向代理** — 将 OpenAI 格式请求转发到 `api.stagewise.io/v1/ai/chat/completions`
- **多账号管理** — 存储多个 stagewise 账号，自动轮换
- **手动添加账号** — 输入邮箱 + Token 即可添加，无需 OTP 登录
- **批量导入** — 粘贴多行 `email|token` 一键导入
- **两种调度策略**：
  - `specific` — 指定单独账号（默认），支持 `X-Account-Email` 请求头指定
  - `fill_first` — 填充：按邮箱排序连续使用第一个可用账号，不可用自动切下一个
- **限额自动切号** — 上游 403（限额）时禁用账号 + 返回 429+Retry-After，下游 retry 时自动用下一个可用账号
- **冷却系统** — 429 冷却 60s、5xx 冷却 30s（内存自动恢复）；401/402/403 永久 disabled
- **自动解禁** — 刷新 usage 时若所有窗口 `usedPercent < 100` 则自动解禁账号
- **Dashboard** — 实时查看账号状态（Available / Cooling / Disabled）、调用日志
- **3×2 分页账号视图** — 固定 3 列 2 行分页，Refresh 只刷新当前页

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动 Web 面板（含反代）
python webui.py
# 访问 http://localhost:8080 在 Accounts 页添加账号

# 或命令行启动反代
python proxy.py --port 11434 --strategy fill_first
```

## 添加账号

### 手动添加（WebUI）

在 WebUI 的 **Accounts** 页，填写 Email 和 Token 后点击 "Add Account"。

### 批量导入（WebUI）

在 Accounts 页展开 "Batch Import"，粘贴以下格式内容：

```
email1@example.com|token1_string_here
email2@example.com|token2_string_here
email3@example.com|token3_string_here
```

### API 添加

```bash
# 单个添加
curl -X POST http://localhost:8080/api/accounts/add \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "token": "<your-token>"}'

# 批量导入（JSON 格式）
curl -X POST http://localhost:8080/api/accounts/add-batch \
  -H "Content-Type: application/json" \
  -d '{"accounts": [{"email": "user1@example.com", "token": "<token1>"}, {"email": "user2@example.com", "token": "<token2>"}]}'

# 批量导入（纯文本格式）
curl -X POST http://localhost:8080/api/accounts/add-batch \
  -H "Content-Type: application/json" \
  -d '{"batch_text": "user1@example.com|token1\nuser2@example.com|token2"}'
```

## Web 界面

```
http://localhost:8080/             Dashboard
http://localhost:8080/accounts     多账号管理 + 添加/批量导入
http://localhost:8080/chat         LLM 聊天
http://localhost:8080/api-explorer  API 调试
http://localhost:8080/proxy        反代控制 + 策略切换
```

## 策略说明

| 策略 | 行为 | 适用场景 |
|---|---|---|
| `specific` | 始终使用 `activeAccount`，支持 `X-Account-Email` 请求头指定 | 单一账号直连 |
| `fill_first` | 按邮箱排序，连续使用第一个可用账号，不可用自动切下一个 | 最大化利用滚动窗口配额 |

## 限额与切号流程

```
请求 → 账号A → 上游 403 (PLAN_LIMIT_EXCEEDED)
         ↓
反代: disabled[A] = True + 返回 429 + Retry-After
         ↓
下游 CLI/SDK 收到 429 → 自动 retry
         ↓
retry 选号: A 已 disabled 被跳过 → 账号B → 成功
```

- 上游 429 → 账号冷却 60s（内存，自动恢复）+ 透传 429+Retry-After
- 上游 5xx → 账号冷却 30s（内存，自动恢复）+ 改写 429+Retry-After
- 上游 401/402/403 → 账号永久 disabled，下次刷新 usage 且限额窗口 < 100% 时自动解禁
- 所有账号都不可用 → 返回 429 + Retry-After: <最近可用账号恢复秒数>

## 项目结构

```
proxy.py           — 反向代理服务器
webui.py           — Flask Web 面板
call_log.py        — 调用日志
templates/
  index.html       — 前端界面
data/              — 运行时配置（config.json，已 gitignore）
```

---

社区：[Linux.do](https://linux.do/)
