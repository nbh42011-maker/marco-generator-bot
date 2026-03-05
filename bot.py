# bot.py — Fixed Full Generator Bot
import os
import json
import time
import asyncio
from typing import Optional, List, Dict

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------- CONFIG ----------------
GUILD_ID = 1452717489656954961          # Your server ID
FREE_GEN_ROLE_ID = 1467913996723032315
EXCLUSIVE_ROLE_ID = 1453906576237924603
BOOST_ROLE_ID = 1453187878061478019
ADMIN_ROLE_ID = 1452719764119093388
STAFF_NOTIFY_USER_ID = 884084052854984726
RESTOCK_CHANNEL_ID = 1478792670049599618

AUTODELETE_CHANNELS = {1478790217971273788, 1454503001363583019}
INVITE_ROLE_ID = FREE_GEN_ROLE_ID

STOCK_FILE = "stock.json"
FREE_COOLDOWN = 180
EXCL_COOLDOWN = 60
RESYNC_COOLDOWN = 60 * 60  # 1 hour
SYNC_ON_START = os.getenv("SYNC_ON_START", "0") == "1"

# ---------------- BOT SETUP ----------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree
_file_lock = asyncio.Lock()

# ---------------- STOCK HELPERS ----------------
def _ensure_stock_file():
    if not os.path.exists(STOCK_FILE):
        with open(STOCK_FILE, "w", encoding="utf-8") as f:
            json.dump({"FREE": {}, "EXCLUSIVE": {}, "categories": []}, f, indent=4)

def _load_stock_from_disk() -> Dict:
    _ensure_stock_file()
    with open(STOCK_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_stock_to_disk(data: Dict):
    with open(STOCK_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

stock_data = _load_stock_from_disk()

async def safe_load_stock():
    global stock_data
    async with _file_lock:
        stock_data = _load_stock_from_disk()
        return stock_data

async def safe_save_stock():
    async with _file_lock:
        _save_stock_to_disk(stock_data)

# ---------------- COOLDOWNS ----------------
_cooldowns: Dict = {}
_last_resync_ts = 0
def now_ts(): return time.time()
def check_cooldown(user_id: int, typ: str) -> int:
    key = (user_id, typ)
    last = _cooldowns.get(key, 0)
    limit = FREE_COOLDOWN if typ == "FREE" else EXCL_COOLDOWN
    rem = int(limit - (now_ts() - last)) if now_ts() - last < limit else 0
    return rem
def set_cooldown(user_id: int, typ: str):
    _cooldowns[(user_id, typ)] = now_ts()

# ---------------- ADMIN CHECK ----------------
def is_admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        user = interaction.user
        return any(r.id == ADMIN_ROLE_ID for r in getattr(user, "roles", []))
    return app_commands.check(predicate)

# ---------------- AUTOCOMPLETE ----------------
async def category_autocomplete(interaction: discord.Interaction, current: str):
    await safe_load_stock()
    cats = stock_data.get("categories", [])
    return [app_commands.Choice(name=c, value=c) for c in cats if current.lower() in c.lower()][:25]

async def stock_type_autocomplete(interaction: discord.Interaction, current: str):
    opts = ["free", "exclusive"]
    return [app_commands.Choice(name=o.capitalize(), value=o) for o in opts if current.lower() in o.lower()][:25]

# ---------------- FORMAT ----------------
def format_stock_embed():
    d = stock_data
    embed = discord.Embed(title="📦 Stock Overview", color=discord.Color.blue())
    free_lines = []
    excl_lines = []
    for cat in d.get("categories", []):
        free_lines.append(f"**{cat}** → {len(d.get('FREE', {}).get(cat, []))}")
        excl_lines.append(f"**{cat}** → {len(d.get('EXCLUSIVE', {}).get(cat, []))}")
    embed.add_field(name="🆓 Free Stock", value="\n".join(free_lines) or "No categories", inline=False)
    embed.add_field(name="💎 Exclusive Stock", value="\n".join(excl_lines) or "No categories", inline=False)
    embed.set_footer(text="Automated • Marcos Gen")
    return embed

def parse_items_from_text(text: str) -> List[str]:
    if not text: return []
    text = text.strip()
    if "\n" in text:
        return [l.strip() for l in text.splitlines() if l.strip()]
    elif "," in text:
        return [l.strip() for l in text.split(",") if l.strip()]
    else:
        return [text]

# ---------------- INVITES ----------------
invites_cache: Dict[int, List[discord.Invite]] = {}
invite_tracker: Dict[int, Dict[int, int]] = {}

# ---------------- GEN UI ----------------
class GenSelect(discord.ui.Select):
    def __init__(self, typ: str):
        opts = []
        for cat in stock_data.get("categories", []):
            cnt = len(stock_data.get(typ, {}).get(cat, []))
            label = f"{cat} — {cnt}"
            opts.append(discord.SelectOption(label=label[:100], value=cat, description=f"{cnt} in stock" if cnt else "Out of stock"))
        super().__init__(placeholder="Choose a category", min_values=1, max_values=1, options=opts[:25])
        self.typ = typ
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await safe_load_stock()
        cat = self.values[0]
        items = stock_data.get(self.typ, {}).get(cat, [])
        if not items:
            await interaction.followup.send("⚠️ That category is out of stock.", ephemeral=True)
            return
        rem = check_cooldown(interaction.user.id, self.typ)
        if rem > 0:
            await interaction.followup.send(f"⏳ Please wait {rem}s before generating again.", ephemeral=True)
            return
        item = items.pop(0)
        await safe_save_stock()
        set_cooldown(interaction.user.id, self.typ)
        dm_ok = True
        try:
            await interaction.user.send(f"{'💎' if self.typ=='EXCLUSIVE' else '🎉'} **Here is your item from {cat}:**\n```{item}```")
        except Exception:
            dm_ok = False
        if dm_ok:
            await interaction.followup.send("✅ Sent to your DMs.", ephemeral=True)
        else:
            await interaction.followup.send(f"⚠️ Could not DM. Here is your item:\n```{item}```", ephemeral=True)

class GenView(discord.ui.View):
    def __init__(self, typ: str):
        super().__init__(timeout=60)
        self.add_item(GenSelect(typ))

# ---------------- USER COMMANDS ----------------
@tree.command(name="gen", description="Generate a Free item")
async def cmd_gen(interaction: discord.Interaction):
    await safe_load_stock()
    if not any(r.id==FREE_GEN_ROLE_ID for r in getattr(interaction.user,"roles",[])):
        await interaction.response.send_message("❌ Free Gen requires role.", ephemeral=True)
        return
    await interaction.response.send_message("📦 Select a Free category:", view=GenView("FREE"), ephemeral=True)

@tree.command(name="exclusive-gen", description="Generate an Exclusive item")
async def cmd_exclusive_gen(interaction: discord.Interaction):
    if EXCLUSIVE_ROLE_ID not in [r.id for r in getattr(interaction.user,"roles",[])]:
        await interaction.response.send_message("❌ Exclusive role required.", ephemeral=True)
        return
    await safe_load_stock()
    await interaction.response.send_message("💎 Select an Exclusive category:", view=GenView("EXCLUSIVE"), ephemeral=True)

@tree.command(name="stock", description="View current stock")
async def cmd_stock(interaction: discord.Interaction):
    await safe_load_stock()
    await interaction.response.send_message(embed=format_stock_embed(), ephemeral=True)

# ---------------- ADMIN COMMANDS ----------------
@tree.command(name="addstock", description="Add stock (Admin only)")
@is_admin_check()
@app_commands.autocomplete(stock_type=stock_type_autocomplete, category=category_autocomplete)
async def cmd_addstock(interaction: discord.Interaction, stock_type: str, category: str, items: Optional[str] = None, file: Optional[discord.Attachment] = None):
    await interaction.response.defer(ephemeral=True)
    key = "FREE" if stock_type.lower() == "free" else "EXCLUSIVE"
    await safe_load_stock()
    if category not in stock_data.get("categories", []):
        await interaction.followup.send("❌ Invalid category.", ephemeral=True)
        return
    new_items: List[str] = []
    if file:
        raw = await file.read()
        lines = parse_items_from_text(raw.decode(errors="ignore"))
        new_items.extend([l for l in lines if l not in stock_data[key].get(category, [])])
    elif items:
        lines = parse_items_from_text(items)
        new_items.extend([l for l in lines if l not in stock_data[key].get(category, [])])
    else:
        await interaction.followup.send("❌ Provide items.", ephemeral=True)
        return
    stock_data[key].setdefault(category, []).extend(new_items)
    await safe_save_stock()
    await interaction.followup.send(f"✅ Added {len(new_items)} item(s) to `{category}`.", ephemeral=True)
    ch = bot.get_channel(RESTOCK_CHANNEL_ID)
    if ch: await ch.send(f"<@&{FREE_GEN_ROLE_ID if key=='FREE' else EXCLUSIVE_ROLE_ID}> 🔔 `{category}` restocked ({len(new_items)} new)")

# ---------------- ON_READY ----------------
@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")
    # populate invites cache
    for guild in bot.guilds:
        try: invites_cache[guild.id] = await guild.invites()
        except: invites_cache[guild.id] = []
    # Start background loops here if any
    # ---- FIX COMMAND SIGNATURE MISMATCH ----
    guild_obj = discord.Object(id=GUILD_ID)
    try:
        print("⚠️ Resetting guild commands...")
        await tree.clear_commands(guild=guild_obj)
        synced = await tree.sync(guild=guild_obj)
        print(f"✅ Commands reset and synced ({len(synced)} commands)")
    except Exception as e:
        print(f"[SYNC ERROR] {e}")
    if SYNC_ON_START:
        try:
            all_synced = await tree.sync()
            print(f"[SYNC_ON_START] global sync: {len(all_synced)} commands")
        except Exception as e:
            print(f"[SYNC_ON_START ERROR] {e}")

# ---------------- RUN ----------------
if __name__ == "__main__":
    TOKEN = os.getenv("TOKEN")
    if not TOKEN:
        print("[ERROR] TOKEN not set")
    else:
        _ensure_stock_file()
        bot.run(TOKEN)
