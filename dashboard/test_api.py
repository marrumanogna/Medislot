
from pathlib import Path
import requests

# Read API URL from .env without requiring python-dotenv
env = {}

for line in Path(".env").read_text().splitlines():
    line = line.strip()

    if line and not line.startswith("#") and "=" in line:
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")

base_url = env["API_BASE_URL"].rstrip("/")
url = base_url + "/auth/register/doctor"

print("Testing URL:", url)

try:
    response = requests.post(
        url,
        json={},
        timeout=20
    )

    print("HTTP STATUS:", response.status_code)
    print("RESPONSE:", response.text)

except requests.RequestException as error:
    print("CONNECTION ERROR:", repr(error))
