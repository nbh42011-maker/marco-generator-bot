# bot.py — Final Stable Build (full features)
import os
import json
import time
import asyncio
import datetime
import discord
from discord import app_commands
from discord.ext import commands, tasks
from typing import Optional, List

# ---------------- CONFIG ----------------
GUILD_ID = 1452717489656954961        # your server ID
FREE_GEN_ROLE_ID = 1467913996723032315
EXCLUSIVE_ROLE_ID = 1453906576237924603
BOOST_ROLE_ID = 1453187878061478019
ADMIN_ROLE_ID = 1452719764119093388   # admin role for privileged commands
STAFF_NOTIFY_USER_ID = 884084052854984726
RESTOCK_CHANNEL_ID = 1478792670049599618  # dedicated restock channel

STOCK_FILE = "stock.json"
PRESENCE_TEXT = ".gg/nV3x85Jeq | BEST DROPS + GEN IN DISCORD"

FREE_COOLDOWN = 180   # seconds (3 minutes)
EXCL_COOLDOWN = 60    # seconds (1 minute)

RESYNC_COOLDOWN = 60 * 60  # 1 hour between manual /resync-commands runs

# ---------------- BOT / INTENTS ----------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------------- STORAGE & LOCK ----------------
_file_lock = asyncio.Lock()

def ensure_stock_file():
    if not os.path.exists(STOCK_FILE):
        with open(STOCK_FILE, "w") as f:
            json.dump({"FREE": {}, "EXCLUSIVE": {}, "categories": []}, f, indent=4)

def load_stock_from_disk():
    ensure_stock_file()
    with open(STOCK_FILE, "r") as f:
        return json.load(f)

def save_stock_to_disk(data):
    with open(STOCK_FILE, "w") as f:
        json.dump(data, f, indent=4)

# in-memory cached stock (kept consistent with file using lock)
stock_data = load_stock_from_disk()

async def safe_load_stock():
    global stock_data
    async with _file_lock:
        stock_data = load_stock_from_disk()
        return stock_data

async def safe_save_stock():
    async with _file_lock:
        save_stock_to_disk(stock_data)

# ---------------- COOLDOWNS / RESYNC GUARD ----------------
_cooldowns = {}  # {(user_id, "FREE"|"EXCLUSIVE"): timestamp}
_last_resync_ts = 0

def now_ts():
    return time.time()

def check_cooldown(user_id: int, typ: str) -> int:
    """Return remaining seconds or 0 if none."""
    key = (user_id, typ)
    last = _cooldowns.get(key, 0)
    limit = FREE_COOLDOWN if typ == "FREE" else EXCL_COOLDOWN
    remaining = int(limit - (now_ts() - last)) if now_ts() - last < limit else 0
    return remaining

def set_cooldown(user_id: int, typ: str):
    _cooldowns[(user_id, typ)] = now_ts()

# ---------------- ADMIN CHECK DECORATOR ----------------
def is_admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        # ensure this runs in-guild and user has admin role
        user = interaction.user
        if not hasattr(user, "roles"):
            return False
        return any(r.id == ADMIN_ROLE_ID for r in getattr(user, "roles", []))
    return app_commands.check(predicate)

# ---------------- AUTOCOMPLETE ----------------
async def category_autocomplete(interaction: discord.Interaction, current: str):
    await safe_load_stock()
    cats = stock_data.get("categories", [])
    return [app_commands.Choice(name=c, value=c) for c in cats if current.lower() in c.lower()][:25]

async def type_autocomplete(interaction: discord.Interaction, current: str):
    options = ["free", "exclusive"]
    return [app_commands.Choice(name=o.capitalize(), value=o) for o in options if current.lower() in o.lower()][:25]

# ---------------- UTIL / FORMATTING ----------------
def format_stock_embed():
    data = stock_data
    embed = discord.Embed(title="📦 Marcos Gen • Stock Overview", color=discord.Color.blue())
    free_lines = []
    excl_lines = []
    for cat in data.get("categories", []):
        free_lines.append(f"**{cat}** → {len(data.get('FREE', {}).get(cat, []))}")
        excl_lines.append(f"**{cat}** → {len(data.get('EXCLUSIVE', {}).get(cat, []))}")
    embed.add_field(name="🆓 Free Stock", value="\n".join(free_lines) or "No categories", inline=False)
    embed.add_field(name="💎 Exclusive Stock", value="\n".join(excl_lines) or "No categories", inline=False)
    embed.set_footer(text="Professional • Secure • Automated")
    return embed

def user_has_required_status(member: discord.Member) -> bool:
    # best-effort check for custom status; use /verify for reliability
    for act in getattr(member, "activities", []):
        if isinstance(act, discord.CustomActivity) and act.name:
            if PRESENCE_TEXT.lower() in act.name.lower():
                return True
    return False

# ---------------- STARTUP (no auto-sync) ----------------
@bot.event
async def on_ready():
    # Do not auto-sync commands on startup to avoid rate-limits from repeated deploys.
    print(f"✅ Logged in as {bot.user} (id: {bot.user.id})")
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name=PRESENCE_TEXT))
    boost_check_loop.start()

# ---------------- GLOBAL APP COMMAND ERROR HANDLER ----------------
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Handle missing permissions cleanly
    if isinstance(error, app_commands.MissingRole):
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        except Exception:
            pass
        return
    if isinstance(error, app_commands.CommandNotFound):
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ That command isn't available on this instance. Ask an admin to run `/resync-commands`.", ephemeral=True)
        except Exception:
            pass
        return

    # Fallback: show friendly message, log error
    print(f"[AppCommandError] {error!r}")
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message("An unexpected error occurred while processing the command. Staff has been notified.", ephemeral=True)
    except Exception:
        pass
    # notify staff (best-effort)
    try:
        staff = await bot.fetch_user(STAFF_NOTIFY_USER_ID)
        await staff.send(f"[Error] User {interaction.user} triggered an error: {error!r}")
    except Exception:
        pass

# ---------------- BOOST CHECK LOOP ----------------
@tasks.loop(minutes=5)
async def boost_check_loop():
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return
    boost_role = guild.get_role(BOOST_ROLE_ID)
    exclusive_role = guild.get_role(EXCLUSIVE_ROLE_ID)
    for member in guild.members:
        try:
            if member.premium_since:
                if boost_role and boost_role not in member.roles:
                    await member.add_roles(boost_role)
                if exclusive_role and exclusive_role not in member.roles:
                    await member.add_roles(exclusive_role)
            else:
                if boost_role and boost_role in member.roles:
                    await member.remove_roles(boost_role)
                if exclusive_role and exclusive_role in member.roles:
                    await member.remove_roles(exclusive_role)
        except Exception:
            continue

# ---------------- GEN UI ----------------
class GenSelect(discord.ui.Select):
    def __init__(self, typ: str):
        # typ is "FREE" or "EXCLUSIVE"
        opts = []
        for cat in stock_data.get("categories", []):
            cnt = len(stock_data.get(typ, {}).get(cat, []))
            label = f"{cat} — {cnt}"
            opts.append(discord.SelectOption(label=label[:100], value=cat, description=f"{cnt} in stock" if cnt else "Out of stock"))
        super().__init__(placeholder="Choose a category", min_values=1, max_values=1, options=opts[:25])
        self.typ = typ

    async def callback(self, interaction: discord.Interaction):
        # heavy work — defer
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
        # Try DM first
        try:
            await interaction.user.send(f"{'💎' if self.typ == 'EXCLUSIVE' else '🎉'} **Here is your item from {cat}:**\n```{item}```")
            await interaction.followup.send("✅ Sent to your DMs.", ephemeral=True)
        except Exception:
            await interaction.followup.send(f"🎁 **Here is your item from {cat}:**\n```{item}```", ephemeral=True)
        # staff log
        try:
            staff = await bot.fetch_user(STAFF_NOTIFY_USER_ID)
            await staff.send(f"[Generate] {interaction.user} got item from {cat} ({self.typ})")
        except Exception:
            pass

class GenView(discord.ui.View):
    def __init__(self, typ: str):
        super().__init__(timeout=60)
        self.add_item(GenSelect(typ))

# ---------------- USER COMMANDS ----------------
@tree.command(name="gen", description="Generate a Free item")
async def cmd_gen(interaction: discord.Interaction):
    # Prefer /verify for stable role granting; we still check for presence informatively
    if not user_has_required_status(interaction.user):
        # instruct user to /verify
        await interaction.response.send_message(
            ("❌ Free Gen requires the custom status to be set.\n"
             "Please set your custom status to include:\n"
             f"`{PRESENCE_TEXT}`\n\n"
             "Then run `/verify` to get Free access."),
            ephemeral=True
        )
        return
    await safe_load_stock()
    await interaction.response.send_message("📦 Select a Free category:", view=GenView("FREE"), ephemeral=True)

@tree.command(name="exclusive-gen", description="Generate an Exclusive item")
async def cmd_exclusive_gen(interaction: discord.Interaction):
    # require exclusive role
    if EXCLUSIVE_ROLE_ID not in [r.id for r in getattr(interaction.user, "roles", [])]:
        await interaction.response.send_message("❌ You need the Exclusive role to use this command.", ephemeral=True)
        return
    await safe_load_stock()
    await interaction.response.send_message("💎 Select an Exclusive category:", view=GenView("EXCLUSIVE"), ephemeral=True)

@tree.command(name="stock", description="View current stock")
async def cmd_stock(interaction: discord.Interaction):
    await safe_load_stock()
    embed = format_stock_embed()
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------------- ADMIN COMMANDS ----------------
@tree.command(name="addcategory", description="Add a category (Admin only)")
@is_admin_check()
async def cmd_addcategory(interaction: discord.Interaction, category: str):
    await interaction.response.defer(ephemeral=True)
    await safe_load_stock()
    if category in stock_data.get("categories", []):
        await interaction.followup.send("❌ Category already exists.", ephemeral=True)
        return
    stock_data.setdefault("categories", []).append(category)
    stock_data.setdefault("FREE", {})[category] = []
    stock_data.setdefault("EXCLUSIVE", {})[category] = []
    await safe_save_stock()
    await interaction.followup.send(f"✅ Category `{category}` added.", ephemeral=True)

@tree.command(name="removecategory", description="Remove a category (Admin only)")
@is_admin_check()
async def cmd_removecategory(interaction: discord.Interaction, category: str):
    await interaction.response.defer(ephemeral=True)
    await safe_load_stock()
    if category not in stock_data.get("categories", []):
        await interaction.followup.send("❌ Category does not exist.", ephemeral=True)
        return
    stock_data["categories"].remove(category)
    stock_data["FREE"].pop(category, None)
    stock_data["EXCLUSIVE"].pop(category, None)
    await safe_save_stock()
    await interaction.followup.send(f"✅ Category `{category}` removed.", ephemeral=True)

@tree.command(name="addstock", description="Add stock (Admin only)")
@is_admin_check()
@app_commands.autocomplete(type=type_autocomplete, category=category_autocomplete)
async def cmd_addstock(interaction: discord.Interaction, type: str, category: str, stock: Optional[str] = None, file: Optional[discord.Attachment] = None):
    await interaction.response.defer(ephemeral=True)
    t = type.lower()
    if t not in ("free", "exclusive"):
        await interaction.followup.send("❌ Type must be `free` or `exclusive`.", ephemeral=True)
        return
    key = "FREE" if t == "free" else "EXCLUSIVE"
    await safe_load_stock()
    if category not in stock_data.get("categories", []):
        await interaction.followup.send("❌ Invalid category.", ephemeral=True)
        return

    new_items = []
    if file:
        try:
            raw = await file.read()
            lines = [l.strip() for l in raw.decode(errors="ignore").splitlines() if l.strip()]
        except Exception:
            await interaction.followup.send("❌ Could not read attached file. Use plain .txt.", ephemeral=True)
            return
        for line in lines:
            if line not in stock_data[key].get(category, []):
                new_items.append(line)
    elif stock:
        lines = [l.strip() for l in stock.splitlines() if l.strip()]
        for line in lines:
            if line not in stock_data[key].get(category, []):
                new_items.append(line)
    else:
        await interaction.followup.send("❌ Provide stock text or attach a .txt file.", ephemeral=True)
        return

    stock_data[key].setdefault(category, []).extend(new_items)
    await safe_save_stock()
    await interaction.followup.send(f"✅ Added {len(new_items)} new item(s) to `{category}`.", ephemeral=True)

    # Ping restock channel
    restock_channel = bot.get_channel(RESTOCK_CHANNEL_ID) or interaction.guild.get_channel(RESTOCK_CHANNEL_ID)
    role_id = FREE_GEN_ROLE_ID if key == "FREE" else EXCLUSIVE_ROLE_ID
    if restock_channel:
        await restock_channel.send(f"<@&{role_id}> 🔔 `{category}` was restocked ({len(new_items)} new item(s)).")

@tree.command(name="removestock", description="Remove stock items (Admin only)")
@is_admin_check()
@app_commands.autocomplete(type=type_autocomplete, category=category_autocomplete)
async def cmd_removestock(interaction: discord.Interaction, type: str, category: str, stock: Optional[str] = None, file: Optional[discord.Attachment] = None):
    await interaction.response.defer(ephemeral=True)
    t = type.lower()
    if t not in ("free", "exclusive"):
        await interaction.followup.send("❌ Type must be `free` or `exclusive`.", ephemeral=True)
        return
    key = "FREE" if t == "free" else "EXCLUSIVE"
    await safe_load_stock()
    if category not in stock_data.get("categories", []):
        await interaction.followup.send("❌ Invalid category.", ephemeral=True)
        return

    removed = 0
    if file:
        try:
            raw = await file.read()
            lines = [l.strip() for l in raw.decode(errors="ignore").splitlines() if l.strip()]
        except Exception:
            await interaction.followup.send("❌ Could not read attached file. Use plain .txt.", ephemeral=True)
            return
        for line in lines:
            if line in stock_data[key].get(category, []):
                while line in stock_data[key][category]:
                    stock_data[key][category].remove(line)
                    removed += 1
    elif stock:
        lines = [l.strip() for l in stock.splitlines() if l.strip()]
        for line in lines:
            if line in stock_data[key].get(category, []):
                while line in stock_data[key][category]:
                    stock_data[key][category].remove(line)
                    removed += 1
    else:
        await interaction.followup.send("❌ Provide items to remove via text or attach a .txt file.", ephemeral=True)
        return

    await safe_save_stock()
    await interaction.followup.send(f"✅ Removed {removed} item(s) from `{category}`.", ephemeral=True)

@tree.command(name="restock", description="Replace stock for a category (Admin only)")
@is_admin_check()
@app_commands.autocomplete(type=type_autocomplete, category=category_autocomplete)
async def cmd_restock(interaction: discord.Interaction, type: str, category: str, stock: Optional[str] = None, file: Optional[discord.Attachment] = None):
    await interaction.response.defer(ephemeral=True)
    t = type.lower()
    if t not in ("free", "exclusive"):
        await interaction.followup.send("❌ Type must be `free` or `exclusive`.", ephemeral=True)
        return
    key = "FREE" if t == "free" else "EXCLUSIVE"
    await safe_load_stock()
    if category not in stock_data.get("categories", []):
        await interaction.followup.send("❌ Invalid category.", ephemeral=True)
        return

    new_items = []
    if file:
        try:
            raw = await file.read()
            lines = [l.strip() for l in raw.decode(errors="ignore").splitlines() if l.strip()]
            new_items = list(dict.fromkeys(lines))
        except Exception:
            await interaction.followup.send("❌ Could not read attached file. Use plain .txt.", ephemeral=True)
            return
    elif stock:
        lines = [l.strip() for l in stock.splitlines() if l.strip()]
        new_items = list(dict.fromkeys(lines))
    else:
        await interaction.followup.send("❌ Provide stock text or attach a .txt file.", ephemeral=True)
        return

    stock_data[key][category] = new_items
    await safe_save_stock()
    await interaction.followup.send(f"♻️ `{category}` fully restocked with {len(new_items)} item(s).", ephemeral=True)

    restock_channel = bot.get_channel(RESTOCK_CHANNEL_ID) or interaction.guild.get_channel(RESTOCK_CHANNEL_ID)
    role_id = FREE_GEN_ROLE_ID if key == "FREE" else EXCLUSIVE_ROLE_ID
    if restock_channel:
        await restock_channel.send(f"<@&{role_id}> 🚀 `{category}` fully restocked with {len(new_items)} item(s).")

# ---------------- VERIFY (custom status -> grant Free role) ----------------
@tree.command(name="verify", description="Verify your custom status and receive Free Gen role")
async def cmd_verify(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    member = interaction.user
    # check custom activity
    correct = False
    for act in getattr(member, "activities", []):
        if isinstance(act, discord.CustomActivity) and act.name:
            if PRESENCE_TEXT.lower() in act.name.lower():
                correct = True
                break
    if not correct:
        await interaction.followup.send(
            ("❌ Verification failed. Your custom status must include:\n"
             f"`{PRESENCE_TEXT}`\n\n"
             "Set that, then run `/verify` again."),
            ephemeral=True
        )
        return
    role = interaction.guild.get_role(FREE_GEN_ROLE_ID)
    try:
        if role and role not in member.roles:
            await member.add_roles(role)
        await interaction.followup.send("✅ Verification successful — Free Gen role granted.", ephemeral=True)
    except Exception:
        await interaction.followup.send("⚠️ Could not assign role. Check bot permissions.", ephemeral=True)

# ---------------- REDEEM EXCLUSIVE (modal) ----------------
class RedeemModal(discord.ui.Modal, title="Redeem Exclusive Gift Card"):
    payment_type = discord.ui.TextInput(label="Payment Type", placeholder="e.g. PayPal, CashApp, Gift Card")
    code = discord.ui.TextInput(label="Redeem Code", placeholder="Paste the redeem code here")

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            staff = await bot.fetch_user(STAFF_NOTIFY_USER_ID)
            await staff.send(
                f"🔔 Redeem Request\nUser: {interaction.user} ({interaction.user.id})\n"
                f"Payment Type: {self.payment_type.value}\nCode: `{self.code.value}`"
            )
        except Exception:
            pass
        await interaction.followup.send("✅ Your code has been submitted for verification. Staff will handle it shortly.", ephemeral=True)

@tree.command(name="redeem-exclusive", description="Redeem Exclusive access via gift card")
async def cmd_redeem(interaction: discord.Interaction):
    await interaction.response.send_modal(RedeemModal())

# ---------------- ADMIN: RESYNC COMMANDS (manual, safe) ----------------
@tree.command(name="resync-commands", description="(Admin) Register/sync commands to the guild (use only when needed)")
@is_admin_check()
async def cmd_resync(interaction: discord.Interaction):
    global _last_resync_ts
    await interaction.response.defer(ephemeral=True)
    now = now_ts()
    if now - _last_resync_ts < RESYNC_COOLDOWN:
        await interaction.followup.send("❌ Commands were resynced recently. Wait before running again to avoid rate limits.", ephemeral=True)
        return
    try:
        guild_obj = discord.Object(id=GUILD_ID)
        await tree.sync(guild=guild_obj)
        _last_resync_ts = now_ts()
        await interaction.followup.send("✅ Commands synced to guild.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ Missing access when syncing commands. Ensure bot has applications.commands scope & is in guild.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

# ---------------- RUN ----------------
if __name__ == "__main__":
    TOKEN = os.getenv("TOKEN")
    if not TOKEN:
        print("[ERROR] TOKEN environment variable not set. Set TOKEN and restart.")
    else:
        ensure_stock_file()
        stock_data = load_stock_from_disk()
        bot.run(TOKEN)
