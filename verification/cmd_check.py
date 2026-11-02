"""Read-only: list guild slash commands registered for this bot. GET only."""
import json, sys, time, urllib.request
from dotenv import dotenv_values
tok = dotenv_values(sys.argv[1]).get("BOT_TOKEN")
H = {"Authorization": f"Bot {tok}", "User-Agent": "DiscordBot (local-check, 1.0)"}
def get(path):
    req = urllib.request.Request("https://discord.com/api/v10" + path, headers=H, method="GET")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)
me = get("/users/@me"); app_id = me["id"]
print("bot user:", me.get("username"), "| app id == bot id assumed")
guilds = get("/users/@me/guilds")
print("guilds joined:", len(guilds))
for g in guilds:
    time.sleep(0.5)
    try:
        cmds = get(f"/applications/{app_id}/guilds/{g['id']}/commands")
        print(f"  guild ...{g['id'][-4:]}: {len(cmds)} guild commands:", sorted(c['name'] for c in cmds))
    except Exception as e:
        print(f"  guild ...{g['id'][-4:]}: error {type(e).__name__} {getattr(e,'code','')}")
glob = get(f"/applications/{app_id}/commands")
print("global commands:", len(glob), sorted(c['name'] for c in glob))
