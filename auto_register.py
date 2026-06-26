"""stagewise 自动注册脚本
=========================

支持两种模式：

模式 1: 浏览器自动化（推荐）
  - 自动打开浏览器，填入临时邮箱
  - 手动完成 Turnstile 验证码（仅首次需要）
  - 自动从 Mail.tm 读取 OTP 并完成注册
  - 支持批量注册多个账号

模式 2: API 直连
  - 需要从其他渠道获取 Turnstile token
  - 适用于已接入验证码服务的场景

依赖安装:
  pip install playwright requests
  playwright install chromium

用法:
  # 浏览器模式（默认，会打开浏览器窗口）
  python auto_register.py
  python auto_register.py --count 5

  # 无头模式（需要 Turnstile token 来源）
  python auto_register.py --headless --turnstile-token TOKEN

  # 导入到 WebUI
  python auto_register.py --output accounts.json
  curl -X POST http://localhost:8080/api/accounts/add-batch \
    -H "Content-Type: application/json" \
    -d @accounts.json
"""

import argparse
import json
import os
import re
import sys
import time
import uuid

import requests

CONSOLE_URL = "https://console.stagewise.io"
API_HOST = "api.stagewise.io"
MAIL_API = "https://api.mail.tm"


# ═══════════════════════════════════════════════════════════════
#  临时邮箱模块 (Mail.tm)
# ═══════════════════════════════════════════════════════════════

class TempMail:
    """Mail.tm 临时邮箱客户端"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        r = self.session.get(f"{MAIL_API}/domains", timeout=10)
        domains = r.json().get("hydra:member", [])
        if not domains:
            raise RuntimeError("无法获取 Mail.tm 域名")
        self.domain = domains[0]["domain"]
        local_part = f"sw{uuid.uuid4().hex[:8]}"
        self.email = f"{local_part}@{self.domain}"
        self.password = uuid.uuid4().hex[:16]
        r = self.session.post(f"{MAIL_API}/accounts", json={"address": self.email, "password": self.password})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"创建临时邮箱失败: {r.status_code} {r.text}")
        r = self.session.post(f"{MAIL_API}/token", json={"address": self.email, "password": self.password})
        token = r.json().get("token")
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        print(f"  [📧] 临时邮箱: {self.email}")

    def wait_for_otp(self, timeout=120):
        """等待 OTP 邮件，返回 (otp_code, message_id)"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = self.session.get(f"{MAIL_API}/messages", timeout=10)
                msgs = r.json().get("hydra:member", [])
                for msg in msgs:
                    r2 = self.session.get(f"{MAIL_API}/messages/{msg['id']}", timeout=10)
                    body = r2.json().get("text", "") or r2.json().get("html", "")
                    otp_match = re.search(r"\b(\d{6})\b", body)
                    if otp_match:
                        print(f"  [📧] 收到 OTP: {otp_match.group(1)}")
                        return otp_match.group(1), msg["id"]
            except Exception:
                pass
            time.sleep(2)
        raise TimeoutError("等待 OTP 超时")

    def cleanup(self):
        try:
            requests.delete(f"{MAIL_API}/accounts/{self.email}", timeout=5)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  浏览器自动化注册
# ═══════════════════════════════════════════════════════════════

def register_with_browser(temp_mail: TempMail, headless: bool = False,
                          turnstile_token: str = None) -> dict:
    """
    用 Playwright 控制 Chrome 完成 stagewise 注册。

    流程:
      1. 打开 console.stagewise.io
      2. 填入临时邮箱
      3. 手动 / 自动处理 Turnstile
      4. 等待 OTP 邮件 → 自动填入 → 完成注册
      5. 从 localStorage/cookie 提取 session token
    """
    from playwright.sync_api import sync_playwright

    print(f"  [🌐] 启动 Playwright{' (无头模式)' if headless else ''}...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        # 注入反自动化检测
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        """)

        try:
            page.goto(CONSOLE_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_selector("#email", timeout=10000)

            # 2. 填入邮箱
            page.fill("#email", temp_mail.email)
            print(f"  [🌐] 已填入邮箱: {temp_mail.email}")

            # 3. 处理 Turnstile
            if turnstile_token:
                # 注入 Turnstile token
                page.evaluate(f"""
                    document.querySelector('form').addEventListener('submit', function() {{
                        const input = document.createElement('input');
                        input.type = 'hidden';
                        input.name = 'cf-turnstile-response';
                        input.value = '{turnstile_token}';
                        this.appendChild(input);
                    }});
                """)
                page.click("button[type='submit']")
                print("  [🌐] 已注入 Turnstile token 并提交")
            else:
                # 点击提交——Turnstile 可能弹出交互式验证
                page.click("button[type='submit']")
                try:
                    # 检查是否有 Turnstile iframe 出现
                    turnstile_iframe = page.frame_locator("iframe[src*='challenges.cloudflare.com']").first
                    if turnstile_iframe.locator("body").is_visible(timeout=3000):
                        print("\n  ⚠️ 请在浏览器窗口中完成 Turnstile 验证...")
                        print("     完成后脚本将自动继续。\n")
                        # 等待 Turnstile 完成（submit 重新启用）
                        page.wait_for_timeout(5000)
                except Exception:
                    pass

            # 4. 等待 OTP 到达
            print("  [⏳] 等待 OTP 邮件...")
            otp, _ = temp_mail.wait_for_otp(timeout=120)

            # 5. 填入 OTP（等待 OTP 输入框出现）
            page.wait_for_timeout(2000)
            try:
                # 尝试 6 个独立输入框
                digit_inputs = page.locator("input[type='text']:not(#email)")
                count = digit_inputs.count()
                if count >= 6:
                    for i, d in enumerate(otp):
                        digit_inputs.nth(i).fill(d)
                else:
                    # 单个输入框
                    otp_input = page.locator("input[autocomplete='one-time-code']")
                    if otp_input.is_visible(timeout=2000):
                        otp_input.fill(otp)
                    else:
                        page.fill("input[type='text']:not(#email)", otp)
            except Exception:
                # 兜底: 逐字符输入
                for i, d in enumerate(otp, 1):
                    try:
                        page.fill(f"input:nth-of-type({i})", d)
                    except Exception:
                        pass

            # 点击提交
            page.wait_for_timeout(1000)
            try:
                page.click("button[type='submit']")
            except Exception:
                pass

            # 6. 等待跳转到 dashboard / 注册完成
            try:
                page.wait_for_url("**/dashboard**", timeout=30000)
                print("  [✅] 注册成功！控制台已加载")
            except Exception:
                print("  [⚠️] 可能已注册成功，尝试提取 token...")

            # 7. 提取 token
            token = page.evaluate("""() => {
                // 检查 localStorage
                const keys = Object.keys(localStorage);
                for (const k of keys) {
                    try {
                        const v = JSON.parse(localStorage[k]);
                        if (v && typeof v === 'object') {
                            if (v.accessToken) return v.accessToken;
                            if (v.token) return v.token;
                            if (v.sessionToken) return v.sessionToken;
                        }
                    } catch(e) {}
                }
                // 检查 sessionStorage
                for (const k of Object.keys(sessionStorage)) {
                    try {
                        const v = JSON.parse(sessionStorage[k]);
                        if (v && v.token) return v.token;
                    } catch(e) {}
                }
                return null;
            }""")

            if not token:
                # 从 cookie 获取
                for c in ctx.cookies():
                    if any(t in c["name"].lower() for t in ("token", "session", "auth")):
                        token = c["value"]
                        break

            ctx.close()
            browser.close()

            if token:
                print(f"  [🔑] Token ({token[:20]}...{token[-10:]})")
            else:
                print("  [⚠️] 未能提取 token - 请手动从浏览器获取")

            return {"email": temp_mail.email, "token": token}

        except Exception:
            ctx.close()
            browser.close()
            raise


# ═══════════════════════════════════════════════════════════════
#  API 直连注册
# ═══════════════════════════════════════════════════════════════

def register_via_api(email: str, turnstile_token: str, otp_code: str = None) -> dict:
    """用 API 直连注册（需要 Turnstile token 来源）"""
    sess = requests.Session()
    sess.headers.update({
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin": CONSOLE_URL,
        "Referer": f"{CONSOLE_URL}/login",
    })

    # 1. 发送 OTP
    print(f"  [📤] 发送 OTP 到 {email}...")
    r = sess.post(f"https://{API_HOST}/v1/auth/email-otp/send-verification-otp", json={
        "email": email, "type": "sign-in",
        "cf-turnstile-response": turnstile_token,
    })
    if r.status_code != 200:
        raise RuntimeError(f"发送 OTP 失败 ({r.status_code}): {r.text}")
    print(f"  [📤] OTP 已发送")

    # 2. 获取 OTP
    if otp_code:
        otp = otp_code
    else:
        otp = input("  OTP 代码: ").strip()

    # 3. 验证
    print(f"  [🔐] 验证 OTP...")
    r = sess.post(f"https://{API_HOST}/v1/auth/sign-in/email-otp", json={
        "email": email, "otp": otp,
        "cf-turnstile-response": turnstile_token,
    })
    if r.status_code != 200:
        raise RuntimeError(f"验证 OTP 失败 ({r.status_code}): {r.text}")

    j = r.json()
    token = (r.headers.get("set-auth-token")
             or j.get("token")
             or (j.get("data") or {}).get("token"))
    user = j.get("user") or (j.get("data") or {}).get("user")
    if not token:
        raise RuntimeError(f"未获取到 token: 响应={j}")

    return {"email": user.get("email", email) if user else email,
            "token": token, "user": user}


# ═══════════════════════════════════════════════════════════════
#  主逻辑
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="stagewise 自动注册脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--count", "-n", type=int, default=1, help="注册数量")
    parser.add_argument("--headless", action="store_true", help="浏览器无头模式")
    parser.add_argument("--turnstile-token", type=str, help="预设 Turnstile token")
    parser.add_argument("--api-mode", action="store_true", help="API 直连模式（需 Turnstile token）")
    parser.add_argument("--output", "-o", type=str, help="输出 JSON 文件路径")
    parser.add_argument("--email", type=str, help="指定邮箱（默认用临时邮箱）")
    parser.add_argument("--otp", type=str, help="预设 OTP 代码（API 模式）")
    args = parser.parse_args()

    accounts = []

    for i in range(args.count):
        print(f"\n{'='*50}")
        print(f"  账号 {i+1}/{args.count}")
        print(f"{'='*50}\n")

        try:
            if args.api_mode:
                email = args.email or input("  邮箱: ").strip()
                token = args.turnstile_token or input("  Turnstile token: ").strip()
                result = register_via_api(email, token, args.otp)
            else:
                tm = TempMail()
                try:
                    email = args.email or tm.email
                    if args.email:
                        tm.email = args.email
                    result = register_with_browser(tm, headless=args.headless,
                                                   turnstile_token=args.turnstile_token)
                finally:
                    tm.cleanup()

            if result and result.get("token"):
                token = result["token"]
                accounts.append(result)
                print(f"\n  ✅ #{i+1}: {result['email']}")
                print(f"     Token: {token[:30]}...")

                if args.output:
                    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
                    with open(args.output, "w", encoding="utf-8") as f:
                        json.dump(accounts, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"\n  ❌ 失败: {e}")
            if args.count == 1:
                raise

    # ─── 结束 ───
    print(f"\n{'='*50}")
    print(f"  完成: {len(accounts)}/{args.count}")
    print(f"{'='*50}")

    if accounts:
        print(f"\n📥 导入 WebUI 的两种方式:\n")
        print(f"方式 1 - API 批量导入:")
        print(f'  curl -X POST http://localhost:8080/api/accounts/add-batch \\')
        print(f'    -H "Content-Type: application/json" \\')
        print(f'    -d \'{{"accounts": {json.dumps(accounts, ensure_ascii=False)}}}\'')
        print(f"\n方式 2 - 文本格式（在 WebUI Accounts 页面粘贴）:")
        for a in accounts:
            print(f"  {a['email']}|{a['token']}")

    return 0 if len(accounts) == args.count else 1


if __name__ == "__main__":
    sys.exit(main())
