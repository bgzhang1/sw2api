"""stagewise 全自动注册脚本

自动创建临时邮箱 -> 通过 Playwright 注册 -> 自动读取 OTP -> 完成验证 -> 提取 Token

用法:
  pip install playwright requests
  playwright install chromium

  python auto_register.py                    # 注册 1 个
  python auto_register.py --count 5           # 批量 5 个
  python auto_register.py --output accts.json # 导出 JSON 供 WebUI 导入
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


class TempMail:
    """Mail.tm 临时邮箱"""

    def __init__(self):
        self.sess = requests.Session()
        self.sess.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        r = self.sess.get(f"{MAIL_API}/domains", timeout=10)
        domains = r.json().get("hydra:member", [])
        if not domains:
            raise RuntimeError("无法获取 Mail.tm 域名")
        self.domain = domains[0]["domain"]
        local = f"sw{uuid.uuid4().hex[:8]}"
        self.email = f"{local}@{self.domain}"
        self.password = uuid.uuid4().hex[:16]
        r = self.sess.post(f"{MAIL_API}/accounts", json={"address": self.email, "password": self.password})
        if r.status_code not in (200, 201):
            raise RuntimeError(f"创建临时邮箱失败: {r.text}")
        r = self.sess.post(f"{MAIL_API}/token", json={"address": self.email, "password": self.password})
        self.sess.headers.update({"Authorization": f"Bearer {r.json()['token']}"})
        print(f"   邮箱: {self.email}")

    def wait_for_otp(self, timeout=120):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = self.sess.get(f"{MAIL_API}/messages", timeout=10)
                for msg in r.json().get("hydra:member", []):
                    r2 = self.sess.get(f"{MAIL_API}/messages/{msg['id']}", timeout=10)
                    body = r2.json().get("text", "") or r2.json().get("html", "")
                    m = re.search(r"\b(\d{6})\b", body)
                    if m:
                        return m.group(1)
            except Exception:
                pass
            time.sleep(2)
        raise TimeoutError("OTP 等待超时")

    def cleanup(self):
        try:
            requests.delete(f"{MAIL_API}/accounts/{self.email}", timeout=3)
        except Exception:
            pass


def register(temp_mail=None, email=None, headless=False):
    """全自动注册一个 stagewise 账号。

    返回: {"email": str, "token": str} 或抛出异常
    """
    from playwright.sync_api import sync_playwright

    if email and not temp_mail:
        # 使用已有邮箱 — 需要手动提供 OTP
        pass

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
        page.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")

        try:
            # 1. 打开页面
            page.goto(CONSOLE_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)  # 等 React 和 Turnstile 加载

            # 2. 填入邮箱
            target_email = email or temp_mail.email
            page.fill("#email", target_email)
            page.wait_for_timeout(500)

            # 3. 提交 (Turnstile 会在无头/有头模式自动通过)
            page.click("button[type='submit']")
            page.wait_for_timeout(2000)

            # 检查是否进入验证码页
            current_text = page.evaluate("document.body.innerText")
            if "error" in current_text.lower() and "captcha" in current_text.lower():
                # Turnstile 没通过 — 等待用户手动完成
                print("   等待手动完成 Turnstile 验证...")
                page.wait_for_timeout(15000)

            print("   等待 OTP...")

            # 4. 获取 OTP
            if temp_mail:
                otp = temp_mail.wait_for_otp(timeout=120)
                print(f"   收到 OTP: {otp}")
            elif email:
                otp = input(f"   OTP 已发送到 {email}，请输入: ").strip()
            else:
                raise ValueError("需要 email 或 temp_mail")

            # 5. 填入 OTP
            page.wait_for_timeout(1000)

            # 尝试多种 OTP 输入方式
            inputs_before = page.locator("input[type='text']").all()
            page.wait_for_timeout(500)

            # 查找 OTP 输入框
            otp_filled = False
            try:
                # 方式 1: 6 个独立输入框
                for i, d in enumerate(otp):
                    inp = page.locator(f"input[type='text']:not(#email)").nth(i)
                    if inp.is_visible(timeout=1000):
                        inp.fill(d)
                        otp_filled = True
            except Exception:
                pass

            if not otp_filled:
                try:
                    # 方式 2: 单个输入框
                    page.fill("input[autocomplete='one-time-code']", otp)
                    otp_filled = True
                except Exception:
                    pass

            if not otp_filled:
                # 方式 3: 所有 text input
                for i, d in enumerate(otp):
                    try:
                        page.locator("input[type='text']").nth(i + 1).fill(d)
                    except Exception:
                        pass

            page.wait_for_timeout(1000)

            # 6. 提交验证码
            try:
                page.locator("button[type='submit']").click()
            except Exception:
                pass

            # 7. 等待跳转到控制台
            page.wait_for_timeout(5000)

            # 8. 提取 token
            token = page.evaluate("""() => {
                // localStorage
                for (const k of Object.keys(localStorage)) {
                    try {
                        const v = JSON.parse(localStorage[k]);
                        if (v && typeof v === 'object') {
                            if (v.accessToken) return v.accessToken;
                            if (v.token) return v.token;
                            if (v.sessionToken) return v.sessionToken;
                        }
                    } catch(e) {}
                }
                return null;
            }""")

            if not token:
                for c in ctx.cookies():
                    if any(t in c["name"].lower() for t in ("token", "session", "auth")):
                        token = c["value"]
                        break

            ctx.close()
            browser.close()

            if token:
                print(f"   Token: {token[:30]}...")
                return {"email": target_email, "token": token}
            else:
                print("   ⚠️ Token 未自动提取，请手动从浏览器获取")
                return {"email": target_email, "token": None}

        except Exception:
            ctx.close()
            browser.close()
            raise


def main():
    parser = argparse.ArgumentParser(description="stagewise 全自动注册")
    parser.add_argument("--count", "-n", type=int, default=1, help="注册数量")
    parser.add_argument("--headless", action="store_true", help="无头模式（默认显示浏览器）")
    parser.add_argument("--output", "-o", type=str, help="输出 JSON 文件")
    parser.add_argument("--email", type=str, help="指定邮箱（默认用临时邮箱）")
    args = parser.parse_args()

    accounts = []
    for i in range(args.count):
        print(f"\n--- 第 {i+1}/{args.count} 个账号 ---")

        try:
            tm = None
            if not args.email:
                tm = TempMail()

            result = register(
                temp_mail=tm,
                email=args.email if args.count == 1 else None,
                headless=args.headless,
            )

            if tm:
                tm.cleanup()

            if result and result.get("token"):
                accounts.append(result)
                if args.output:
                    with open(args.output, "w", encoding="utf-8") as f:
                        json.dump(accounts, f, indent=2, ensure_ascii=False)

        except Exception as e:
            print(f"  失败: {e}")
            if args.count == 1:
                raise

    # 输出
    print(f"\n{'='*50}")
    print(f"  完成: {len(accounts)}/{args.count}")
    print(f"{'='*50}")

    if accounts:
        print(f"\n📥 导入 WebUI:\n")
        print(f"  curl -X POST http://localhost:8080/api/accounts/add-batch \\")
        print(f'    -H "Content-Type: application/json" \\')
        print(f"    -d '{json.dumps({'accounts': accounts}, ensure_ascii=False)}'")
        print()
        for a in accounts:
            print(f"  {a['email']}|{a['token']}")


if __name__ == "__main__":
    sys.exit(main())
