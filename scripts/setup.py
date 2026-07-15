"""
One-time setup: adds 'Компания' rich_text field to the Notion vacancies DB.
Run once: python3 setup.py
"""
import json
import os
import urllib.request

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_DATABASE_ID = "f71f92e0-c976-4cf2-bb56-8063b5cea681"

payload = json.dumps({"properties": {"Компания": {"rich_text": {}}}}).encode()
req = urllib.request.Request(
    f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}",
    data=payload,
    method="PATCH",
)
req.add_header("Authorization", f"Bearer {NOTION_TOKEN}")
req.add_header("Notion-Version", "2022-06-28")
req.add_header("Content-Type", "application/json")

with urllib.request.urlopen(req, timeout=30) as resp:
    result = json.loads(resp.read())

if "Компания" in result.get("properties", {}):
    print("✅ Field 'Компания' added to Notion DB")
else:
    print("⚠️  Field may already exist or something went wrong")
    print(json.dumps(result.get("properties", {}).keys(), ensure_ascii=False))
