import requests, json, time

email = "MilanKade1183@outlook.com"
refresh_token = "M.C556_BAY.0.U.MsaArtifacts.-CoCt!f!iEJ1Xv**QQS3jgzev!mY*sGaNRyyyQx!vidPAsHtFtGzkid3GmqACocy8GOumrD3q8GbxT9RCGH4eKKP*vNEMPlbfzW4tYqYInSf7oJ*ntavX0yYCUcEd5hN5bBG*BvCZf4dqmAsEuUX2sxTErYSP79jSh0IRIKcwOBnTpLlY3!2uiAyObADTOkPFpZSaf23BvfTl1vBXrkjUyx7M4TWlEm*6Df8q2S5dQMDKTqykkgXNaM9vycPAWGVeMFvwO59MPn69il65LwjcEcm0ZXvD3S3VmEV6q2K*iS5y0Rfjst976zhZoS*msM1l41Z1F4WMBwB6olpCywq2XU1h8lDA57F6Mcru7GNc*F6aVMoqJUqDbg!YDmorr1OJ4g$$"
client_id = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"

# First, send OTP to localhost
print("Sending OTP...")
r1 = requests.post("http://localhost:8080/api/send-otp", json={"email": email}, timeout=10)
print(f"  Response: {r1.json()}")

# Wait for email to arrive
print("Waiting 10s for email delivery...")
time.sleep(10)

for mailbox in ["INBOX", "Junk"]:
    r = requests.post("https://www.appleemail.top/api/mail-new", json={
        "refresh_token": refresh_token,
        "client_id": client_id,
        "email": email,
        "mailbox": mailbox,
        "response_type": "json",
    }, timeout=15)
    resp = r.json()
    print(f"\n{mailbox}:")
    print(f"  Response keys: {list(resp.keys())}")
    data = resp.get("data", [])
    print(f"  data count: {len(data)}")
    if data:
        item = data[0]
        print(f"  Full item: {json.dumps(item, ensure_ascii=False)[:2000]}")
