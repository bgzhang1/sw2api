# sw2api

stagewise 多账号反向代理 + Web 管理面板。支持指定单独账号、填充、轮询三种账户调度策略。

## 功能

- **反向代理** — 将 OpenAI 格式请求透明转发到 `api.stagewise.io/v1/ai/chat/completions`
- **多账号管理** — 存储多个 stagewise 账号，自动轮换
- **三种调度策略**：
  - `specific` — 指定单独账号（默认）
  - `fill_first` — 填充：烧完一个账号再切下一个
  - `round_robin` — 轮询：均分请求
- **冷却/封禁系统** — 429/5xx 自动冷却（指数退避），403 标记封禁
- **Token 用量持久化** — 记录每小时/每天/每周用量曲线
- **24h 可用性监控** — 每 10 分钟一格的健康度热力图
- **本地 API 密钥管理** — 为客户端生成独立密钥，带月度限额
- **Dashboard** — 实时查看用量、账号状态、可用性

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 登录
python webui.py
# 访问 http://localhost:8080 在 Login 页输入邮箱 OTP

# 或命令行启动反代
python proxy.py --login
python proxy.py --port 11434 --strategy fill_first
```

## Web 界面

```
http://localhost:8080/             Dashboard
http://localhost:8080/login        Email OTP 登录
http://localhost:8080/accounts     多账号管理
http://localhost:8080/chat         LLM 聊天
http://localhost:8080/api-explorer  API 调试
http://localhost:8080/proxy        反代控制 + 策略切换
http://localhost:8080/apikeys      本地 API 密钥管理
```

## OpenAI 兼容客户端

```python
from openai import OpenAI
client = OpenAI(
    api_key="sk-stagewise-<your-local-key>",
    base_url="http://localhost:11434/v1"
)
```

## 策略说明

| 策略 | 行为 | 适用场景 |
|---|---|---|
| `specific` | 始终使用 `activeAccount`，支持 `X-Account-Email` 请求头指定 | 单一账号直连 |
| `fill_first` | 按邮箱排序，连续使用第一个可用账号，冷却后自动切到下一个 | 最大化利用滚动窗口配额 |
| `round_robin` | 全局游标轮询所有可用账号 | 负载均衡 |

## 开放 API（注册机对接）

### 发送验证码

```bash
curl -X POST http://localhost:8080/api/send-otp \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com"}'

# 响应
{"success": true, "message": "OTP sent", "email": "user@example.com"}
```

### 验证 OTP 并添加账号

```bash
curl -X POST http://localhost:8080/api/verify-otp \
  -H "Content-Type: application/json" \
  -d '{"email": "user@example.com", "otp": "123456"}'

# 响应（返回完整 token）
{
  "success": true,
  "token": "<full-session-token>",
  "token_preview": "<truncated>...",
  "email": "user@example.com",
  "activeAccount": "user@example.com"
}
```

验证成功后自动保存到 `config.json` 并设为活跃账号。

## 项目结构

```
proxy.py           — 反向代理服务器
webui.py           — Flask Web 面板
key_manager.py     — 本地 API 密钥管理
health_tracker.py  — 24h 可用性追踪
usage_store.py     — Token 用量持久化
templates/
  index.html       — 前端界面
```

---

社区：[Linux.do](https://linux.do/)
