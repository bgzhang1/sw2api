import requests
import re
import time
import json

APPLE_EMAIL_API = "https://www.appleemail.top"
LOCAL_API = "http://localhost:8080"

ACCOUNTS_RAW = """MilanKade1183@outlook.com----jh964010----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C556_BAY.0.U.MsaArtifacts.-CoCt!f!iEJ1Xv**QQS3jgzev!mY*sGaNRyyyQx!vidPAsHtFtGzkid3GmqACocy8GOumrD3q8GbxT9RCGH4eKKP*vNEMPlbfzW4tYqYInSf7oJ*ntavX0yYCUcEd5hN5bBG*BvCZf4dqmAsEuUX2sxTErYSP79jSh0IRIKcwOBnTpLlY3!2uiAyObADTOkPFpZSaf23BvfTl1vBXrkjUyx7M4TWlEm*6Df8q2S5dQMDKTqykkgXNaM9vycPAWGVeMFvwO59MPn69il65LwjcEcm0ZXvD3S3VmEV6q2K*iS5y0Rfjst976zhZoS*msM1l41Z1F4WMBwB6olpCywq2XU1h8lDA57F6Mcru7GNc*F6aVMoqJUqDbg!YDmorr1OJ4g$$
RussellCanaan5540@outlook.com----ja569685----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C531_SN1.0.U.MsaArtifacts.-Coll*8y45LRudaPYzhth0QIVllsNS55C90AVAyAsswmh8hsfkVmwDFp6ZpsIn5kaxO2AjBtDHNwrjUcg0eoxE*vh7K0O03Pip4pVJ7a95t3aaCGFwIwK0iVeleSEV8IOFWV!lkmBC6F9TQVXwAboKsBXqMFoeS2!4Rh0LVhwGBUGBxpToByAB*m38DvXl5Rl4eStn32IO!YIshWvNOoNsDvmS5tTF6deiXeVmg4Da!YuSqPgVIgCc5Wjr06nfyOMigR9y!Jvt!7Po7NhhUTT1pd8W2QobrKCL!AAXQZnlsqG!5DiTLVy87vHcCsJH!EGtnhifOlbE6n*ON8Z6qaMP8mPo3Zpb!diUjY7!82wY4kD5swZkv1JLLbinF7R1FrFLw$$
KaisenBalthazar9963@outlook.com----en383608----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C524_BL2.0.U.MsaArtifacts.-Ck0S6ja7iO*X93lm6LWgqNlE8XZ37VV0k0mz4MmHjFs*6h4gaq9UpYGwaJ4Xfk6HfWzu*15qtE588YnUe2pCmvt7oMWoIvAaVMoiyJ54AytyXdUEcu7VqLhqm1XJCOaj3xomq*X0cDOr5NChYyRN!Ovcc3T39x*hYQlCry40K3R*drz8sgiRdrJuyH4Z8mhqEBlxRPSXDK3tFd*0*8uoOKklCejUMbqHqBoF2twsuXyzd!edrq8DGLZ1i7VAXhQxSicJ39kO!ye7smt6UzZLSGrbkc2fynSsZv8NIO70kgiYThoYmNoTKoBGHHn6r6FclzGAwxxvDHjq2WwkOCEFh*Wq5RK68oqkC2TNaElQ*XvfiF265UuRin4BQEr8eJ2M8g$$
ThomasHarrison3142@outlook.com----rs207116----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C523_BL2.0.U.MsaArtifacts.-Ct5v75Ez*AI0*VpiEEIG4MGVcKugd41k564d1AnD!YG6JiHwF0tfabMM8fvJGlyKYu92bu*hS8KBJ85!ZDlk7n8vk9lQENVTJPW9Hb*ncqMyNva2pdQImxt6w2LCNsezCh0icR807wnOyZiifeiTVGMJK3mbHQMh4gSP*fFAJ3r6*n3mHI7kvQ1i4io9mG0Bz16F8TorK2WguCrx02LbAJ4l8qS44tXtBuzxcqthbLcfqzAq4tcjm02dazWip4NpOILtRaXrPv*I1nkqONUQUR*g3Nd!ajFums*8hoQp3eHYJYUv4tgQVnH27hnNVv7hJTYm0lunjYU!lgoPx0PUc*Jk35OzyaHU7g0OPCwFh1g4cFFWfzlVmK!FwM2y6HTjRg$$
ReignJosie2062@outlook.com----dx955042----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C538_BL2.0.U.MsaArtifacts.-Ckf4Of1W94b5!A7znPcTSTCFbqOGK!rbOq5iuBMMC2Jlzlo5kp!kOPLhNPSN6q2IF7wwUh9LY9btMHlTAC9NDL60HRZ1Aqbds76dJnYirJic1Pf5hCSwBbRnFsZdKLMr*NUOzRY9NVOLz5WXFdetU1X5zbQwnBdnSDswWgDgI*IN!8!ZUnpv2oY9OVM0kszFAtYLcLGOq8bx099vOcfCpnndICfn27MQAnoaq06mgyXG!KhYolhn3*XfG4R8kV7BSzBhQtCqdUYfNamneeWX!ImsHtml1!raNvym0OkTedIiofbjJYPekII2Syw4CQ4*kHe4JLLJS0K2sMeh6dYHarcxpvD6P3SNl00KxXGta6xudGLxJds!JgS3ObnPn1UKXw$$
MeridaJulia4893@outlook.com----uo665352----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C555_BL2.0.U.MsaArtifacts.-ChpGUFeXMiZIZfFuMiYdZJJkMVIHkaft1xNFPdMq2duo6rtU0Drswao0mnNMnsD7!d7*V7uE3wYa7*3pGerMNzlu7oO5vCwSanrkM88a0!zDj6fQPy9X9ZDV5FGHDTsTO*9s1F*eNT2Q1K9DtVE!J9nepkrahxOIUySgvCFvhyqOPndg7n88V48Gh1OKO3pY8S85imU2elQeAbfGz4Pq070d9pcWFYfwOSEjBnUwtw0QYZ8nNcy8JroLCfPZT9kzL5RGxmcPgVEU4ByY95k5vyR46T8pR3BAUMGSaNn1fk0FeS2S!A51kHe49k210XvtUHrJVDyIVrvB3bHGN7QIUO9zMJFQac61FlRGs5FcNT598A5d9JoUGaO*6e00aa4Lxw$$
MicahHome6286@outlook.com----ox261032----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C537_BL2.0.U.MsaArtifacts.-CtWSAL4cREySZTEe5c85cEhXiK486JCpKnhyeTnpmrnH1*yy3jFHZuYc5WjbyLJmDpxfKKCB4VBpJb75o9e2wMA2zDIaB7AUS7TIfwgOetq1iw02rdUEUTCEFF0VGqjIFTSGhtYelLkG2r289r8GJ0x9cUk1Uog2sVXPotqXtEC8Lr!vcWXywXHrbcNAI10lCPH1iQsltwEJWs6VR9x*5ikFm47q1j*E79n5v!cAMUnUOLTP*R!4G0yM9JyYY4b6PNkj21tZeJEqYrdYhGjQkcfn9aUX4EQI8H78lqnnGh5m6LMG4gm5qhNM9v3*Eq!Ho0dUWvGV!!Cah!KfCSzsDMeQak6jD8O8FQE!Y31F7ohcJnJWQb0v*sMQLg4IRZ4D0A$$
PlumAlaia1998@outlook.com----uj429538----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C556_BAY.0.U.MsaArtifacts.-CiApFUKtwYji6C0gprknE6gcHdq5*m!TdpiUmmcBPuSzSy9MExcJguXiilgjomaxSnTsRZguYJ4G3s28ZPiysLxnp8SiBUN9rBblPCLbTac0JQqxV*M6aQWR40*t1N6sVI5MaAiwI4RMI95karPPZdpQ3jqagQY2YNum5FvrYw9gE0G1UfbwsceYpxQRAIxBErKOuAQP1TUKZTgI4ymtavk4s0JWO9cgK!LkdLaIfdA1dl7FJHs2kPrV7!SCDKjHOhxifPuAq0WASq6o5Sr!PQTZv*pUNvZZgsHvEUeemApJi9AtdVGwUToYKFkfwgSOS3c*0FzSLanEQ46LEAOfN4jMkwURpgG2pvRdpWsSeLoxn42GtWrNmVprqaZ3OiL0Mw$$
TayaCrew1998@outlook.com----xd803755----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C553_BAY.0.U.MsaArtifacts.-ClFKP3qffEGynzDu7s6msrusLqm3W!eEPdZa6nNghygxoWgoka8z0!xMx8rnvSpNyKOA3wbm6hYtiS!cidb6dtLtAre9EE8yyD18ta1UY7ng5Oamy1eCrppnwyfKhJbSRVan5VRPCrC45ESoJ5ZGzh1TsJ7P08TCuDrX5oVlA1qfMUnApq9q6axMCCoJW2wgQpeblTn!tgtqugWa867LGUdSGLIpLBn*ZJDUkKOIcsPMjoZynbU2PekqRhuKe0gK6pUgVZo*JDUX3J*M6wMEM*I0zIJDFyhjbNSCnbqLzO8XfPoYqqVLAdA8eM3Wrg9FqSWxknKNJLPqXw063jD0YYwgjZJP0UgSeelOS0FwRtLHSX82zap8PUjVJ4FkyyqY0Q$$
ShawnDolly3794@outlook.com----bf178206----9e5f94bc-e8a4-4e73-b8be-63364c29d753----M.C546_BAY.0.U.MsaArtifacts.-CkH8bkOlQNclAPhLzIVrDook7Tdowq6aw6stARho38Un94!Nb1JvwPH4onqJWPiwfTI0orgKXstJxh!laV*hwNb9J96VuvGDJD4qWEmyMgIxNI0CYhLHg7*e5V*eoQAo19vaaRzoA2nfZ3l7doKEqnB83tn4jGGNOQ8Pxifvi4SoOrnQhZQcCaMJIfxs8fkjMLDlEmd*XM2YqhrDVLkpM5*7wu1!kHBNhZVDYoXj2GQTOVrwLPFZ8d5GGUE7eg9HlTTd40COV0WATr!0W6qzlYjXsbclO9G*kgiIQwZtjchih52OqkCMJdgUyw6OQGfVLLpouCoF3qCt23fNB4PgiiMI2AAOBBjLzy4lcJvjVTzEl2I1Nj6F*zcst7rW*Op32g$$"""


def parse_accounts(text):
    accounts = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----", 3)
        if len(parts) == 4:
            email, password, client_id, refresh_token = parts
            accounts.append({
                "email": email,
                "password": password,
                "client_id": client_id,
                "refresh_token": refresh_token,
            })
    return accounts


def send_otp(email):
    r = requests.post(f"{LOCAL_API}/api/send-otp", json={"email": email}, timeout=10)
    return r.json()


def fetch_latest_email(account, mailbox="INBOX", retries=5, delay=3):
    for attempt in range(retries):
        try:
            r = requests.post(f"{APPLE_EMAIL_API}/api/mail-new", json={
                "refresh_token": account["refresh_token"],
                "client_id": account["client_id"],
                "email": account["email"],
                "mailbox": mailbox,
                "response_type": "json",
            }, timeout=15)
            data = r.json()
            if data:
                return data
        except Exception:
            pass
        if attempt < retries - 1:
            time.sleep(delay)
    return None


def extract_otp(email_data):
    text = json.dumps(email_data)
    match = re.search(r'\b(\d{6})\b', text)
    return match.group(1) if match else None


def verify_otp(email, otp):
    r = requests.post(f"{LOCAL_API}/api/verify-otp", json={"email": email, "otp": otp}, timeout=10)
    return r.json()


def main():
    accounts = parse_accounts(ACCOUNTS_RAW)
    print(f"共加载 {len(accounts)} 个账户\n")

    results = []
    for i, acct in enumerate(accounts, 1):
        email = acct["email"]
        print(f"[{i}/{len(accounts)}] {email}")

        # Step 1: send OTP
        try:
            resp = send_otp(email)
            print(f"  -> 发送OTP: {resp.get('message', resp)}")
        except Exception as e:
            print(f"  -> 发送OTP失败: {e}")
            results.append({"email": email, "status": "fail", "step": "send_otp", "error": str(e)})
            continue

        # Step 2: fetch email & extract OTP
        email_data = fetch_latest_email(acct, "INBOX")
        if not email_data:
            email_data = fetch_latest_email(acct, "Junk")
        if not email_data:
            print(f"  -> 获取邮件失败（重试后仍无结果）")
            results.append({"email": email, "status": "fail", "step": "fetch_email"})
            continue

        otp = extract_otp(email_data)
        if not otp:
            print(f"  -> 未找到6位验证码")
            results.append({"email": email, "status": "fail", "step": "extract_otp"})
            continue
        print(f"  -> 验证码: {otp}")

        # Step 3: verify OTP
        try:
            resp = verify_otp(email, otp)
            token = resp.get("token", resp.get("token_preview", ""))
            print(f"  -> 验证成功! token: {token}")
            results.append({"email": email, "status": "success", "token": resp.get("token", "")})
        except Exception as e:
            print(f"  -> 验证失败: {e}")
            results.append({"email": email, "status": "fail", "step": "verify_otp", "error": str(e)})

        # save progress after each account
        with open("register_results.json", "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        time.sleep(2)

    print("\n===== 完成 =====")
    success = [r for r in results if r["status"] == "success"]
    failed = [r for r in results if r["status"] != "success"]
    print(f"成功: {len(success)}, 失败: {len(failed)}")
    print(f"结果已保存到 register_results.json")


if __name__ == "__main__":
    main()
