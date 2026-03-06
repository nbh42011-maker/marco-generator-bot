# clear_commands.py
import os, sys, requests

APP_ID = "1478522023696273428"
GUILD_ID = "1452717489656954961"
TOKEN = os.getenv("TOKEN")  # recommended; or paste token as string (careful)

if not TOKEN:
    print("ERROR: TOKEN env var not set.")
    sys.exit(1)

def put_empty(url):
    headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
    r = requests.put(url, json=[], headers=headers)
    print(url, "->", r.status_code)
    if r.text:
        print("body:", r.text)

print("Clearing guild commands...")
put_empty(f"https://discord.com/api/v10/applications/{APP_ID}/guilds/{GUILD_ID}/commands")
# optionally clear global:
# print("Clearing global commands...")
# put_empty(f"https://discord.com/api/v10/applications/{APP_ID}/commands")
