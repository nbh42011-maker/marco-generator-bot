# bot.py — Full working generator bot (stock + invites + vouches + safe sync)
# Requirements: discord.py, aiohttp
# Set TOKEN in env. Optional: SYNC_ON_START=1 to globally sync once.

import os
import json
import time
import asyncio
from typing import Optional, List, Dict

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------- CONFIG (edit IDs if needed) ----------------
GUILD_ID = 1452717489656954961          # Your server ID (must be exact)
APPLICATION_ID = "1478522023696273428"  # Bot application (client) ID as string
TOKEN_ENV_NAME = "TOKEN"                # env var name for token

FREE_GEN_ROLE_ID = 1467913996723032315  # FreeGen role ID
EXCLUSIVE_ROLE_ID = 1453906576237924603 # Exclusive role ID
BOOST_ROLE_ID = 1453187878061478019
ADMIN_ROLE_ID = 1452719764119093388
STAFF_NOTIFY_USER_ID = 884084052854984726

RESTOCK_CHANNEL_ID = 1478792670049599618  # channel for restock pings
VOUCH_CHANNEL_ID = 1452868333383716915    # vouch posts go here (you provided this)

# Channels where plain user messages should be auto-deleted (no response)
AUTODELETE_CHANNELS = {1478790217971273788, 1454503001363583019}

STOCK_FILE = "stock.json"
VOUCH_FILE = "vouches.json"

# One-time clear markers (avoid repeating dangerous clears)
_CLEARED_MARKER = "commands_cleared.lock"
_CLEARED_GLOBAL_MARKER = "commands_cleared_global.lock"

# cooldowns (seconds)
FREE_COOLDOWN = 180
EXCL_COOLDOWN = 60
RESYNC_COOLDOWN = 60 * 60  # 1 hour between manual resyncs

# Optional: if set to "1" the bot will attempt a one-time global sync on_ready (keep OFF normally)
SYNC_ON_START = os.getenv("SYNC_ON_START", "0") == "1"

# ---------------- INTENTS & BOT ----------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

# ---------------- storage & locks ----------------
_file_lock = asyncio.Lock()
_vouch_lock = asyncio.Lock()

def _ensure_file(path: str, default):
    if not os.path.exists(path):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(default, f, indent=4)
        except Exception:
            pass

def _load_json(path: str):
    _ensure_file(path, {})
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

# ensure data files exist
_ensure_file(STOCK_FILE, {"FREE": {}, "EXCLUSIVE": {}, "categories": []})
_ensure_file(VOUCH_FILE, {"vouches": []})

stock_data = _load_json(STOCK_FILE)
vouch_data = _load_json(VOUCH_FILE)

async def safe_load_stock():
    global stock_data
    async with _file_lock:
        stock_data = _load_json(STOCK_FILE)
        return stock_data

async def safe_save_stock():
    async with _file_lock:
        _save_json(STOCK_FILE, stock_data)

async def safe_load_vouches():
    global vouch_data
    async with _vouch_lock:
        vouch_data = _load_json(VOUCH_FILE)
        return vouch_data

async def safe_save_vouches():
    async with _vouch_lock:
        _save_json(VOUCH_FILE, vouch_data)

# ---------------- cooldowns / resync guard ----------------
_cooldowns: Dict = {}
_last_resync_ts = 0

def now_ts() -> float:
    return time.time()

def check_cooldown(user_id: int, typ: str) -> int:
    key = (user_id, typ)
    last = _cooldowns.get(key, 0)
    limit = FREE_COOLDOWN if typ == "FREE" else EXCL_COOLDOWN
    rem = int(limit - (now_ts() - last)) if now_ts() - last < limit else 0
    return rem

def set_cooldown(user_id: int, typ: str):
    _cooldowns[(user_id, typ)] = now_ts()

# ---------------- admin-check helper ----------------
def is_admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        user = interaction.user
        if not hasattr(user, "roles"):
            return False
        return any(r.id == ADMIN_ROLE_ID for r in getattr(user, "roles", []))
    return app_commands.check(predicate)

# ---------------- autocomplete helpers ----------------
async def category_autocomplete(interaction: discord.Interaction, current: str):
    await safe_load_stock()
    cats = stock_data.get("categories", [])
    return [app_commands.Choice(name=c, value=c) for c in cats if current.lower() in c.lower()][:25]

async def stock_type_autocomplete(interaction: discord.Interaction, current: str):
    opts = ["free", "exclusive"]
    return [app_commands.Choice(name=o.capitalize(), value=o) for o in opts if current.lower() in o.lower()][:25]

# ---------------- formatting ----------------
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

# ---------------- parsing helper (preserves ':') ----------------
def parse_items_from_text(text: str) -> List[str]:
    """
    Parse input text into list of items.
    - If text contains newlines -> split on newlines (preferred)
    - Else if contains commas -> split on commas
    - Else -> single item
    Keeps ':' characters intact.
    """
    if not text:
        return []
    text = text.strip()
    if "\n" in text:
        lines = [l.strip() for l in text.splitlines() if l.strip()]
    elif "," in text:
        lines = [l.strip() for l in text.split(",") if l.strip()]
    else:
        lines = [text]
    return lines

# ---------------- invite tracking ----------------
invites_cache: Dict[int, List[discord.Invite]] = {}
invite_tracker: Dict[int, Dict[int, int]] = {}  # guild_id -> {inviter_id: count}

# ---------------- background loops (start in on_ready) ----------------
@tasks.loop(minutes=5)
async def boost_loop():
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

# ---------------- gen UI ----------------
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
            await interaction.user.send(f"{'💎' if self.typ == 'EXCLUSIVE' else '🎉'} **Here is your item from {cat}:**\n```{item}```")
        except Exception:
            dm_ok = False

        if dm_ok:
            await interaction.followup.send("✅ Sent to your DMs.", ephemeral=True)
        else:
            await interaction.followup.send(
                ("⚠️ Could not send DM. Please enable DMs from server members or accept direct messages.\n\n"
                 f"Here is your item for now:\n```{item}```"),
                ephemeral=True
            )
        # staff log (best-effort)
        try:
            staff = await bot.fetch_user(STAFF_NOTIFY_USER_ID)
            await staff.send(f"[Generate] {interaction.user} ({interaction.user.id}) got item from {cat} ({self.typ})")
        except Exception:
            pass

class GenView(discord.ui.View):
    def __init__(self, typ: str):
        super().__init__(timeout=60)
        self.add_item(GenSelect(typ))

# ---------------- USER COMMANDS ----------------
@tree.command(name="gen", description="Generate a Free item")
async def cmd_gen(interaction: discord.Interaction):
    await safe_load_stock()
    if not any(r.id == FREE_GEN_ROLE_ID for r in getattr(interaction.user, "roles", [])):
        await interaction.response.send_message(
            ("❌ Free Gen access requires the FreeGen role. You can earn it by inviting friends — run `/invites` to see your progress."),
            ephemeral=True
        )
        return
    await interaction.response.send_message("📦 Select a Free category:", view=GenView("FREE"), ephemeral=True)

@tree.command(name="exclusive-gen", description="Generate an Exclusive item")
async def cmd_exclusive_gen(interaction: discord.Interaction):
    # Exclusive access requires the Exclusive role ONLY — vouches are NOT required to use exclusive commands.
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

# ---------------- ADMIN COMMANDS (category with scope) ----------------
@app_commands.choices(
    scope=[
        app_commands.Choice(name="Free", value="free"),
        app_commands.Choice(name="Exclusive", value="exclusive"),
        app_commands.Choice(name="Both", value="both"),
    ]
)
@tree.command(name="addcategory", description="Add a category (Admin only). Choose Free, Exclusive, or Both.")
@is_admin_check()
async def cmd_addcategory(interaction: discord.Interaction, category: str, scope: str):
    await interaction.response.defer(ephemeral=True)
    await safe_load_stock()

    category = category.strip()
    if not category:
        await interaction.followup.send("❌ Category cannot be empty.", ephemeral=True)
        return

    existed = category in stock_data.get("categories", [])
    responses = []

    if not existed:
        stock_data.setdefault("categories", []).append(category)
        responses.append(f"Added `{category}` to master categories list.")

    if scope in ("free", "both"):
        if category not in stock_data.setdefault("FREE", {}):
            stock_data["FREE"][category] = []
            responses.append("Added to Free stock.")
        else:
            responses.append("Already present in Free stock.")

    if scope in ("exclusive", "both"):
        if category not in stock_data.setdefault("EXCLUSIVE", {}):
            stock_data["EXCLUSIVE"][category] = []
            responses.append("Added to Exclusive stock.")
        else:
            responses.append("Already present in Exclusive stock.")

    await safe_save_stock()
    await interaction.followup.send("✅ " + " ".join(responses), ephemeral=True)

@app_commands.choices(
    scope=[
        app_commands.Choice(name="Free", value="free"),
        app_commands.Choice(name="Exclusive", value="exclusive"),
        app_commands.Choice(name="Both", value="both"),
    ]
)
@tree.command(name="removecategory", description="Remove a category (Admin only). Choose Free, Exclusive, or Both.")
@is_admin_check()
async def cmd_removecategory(interaction: discord.Interaction, category: str, scope: str):
    await interaction.response.defer(ephemeral=True)
    await safe_load_stock()

    category = category.strip()
    if category not in stock_data.get("categories", []):
        warning = True
    else:
        warning = False

    removed_msgs = []

    if scope in ("free", "both"):
        if category in stock_data.get("FREE", {}):
            stock_data["FREE"].pop(category, None)
            removed_msgs.append("Removed from Free stock.")
        else:
            removed_msgs.append("Not found in Free stock.")

    if scope in ("exclusive", "both"):
        if category in stock_data.get("EXCLUSIVE", {}):
            stock_data["EXCLUSIVE"].pop(category, None)
            removed_msgs.append("Removed from Exclusive stock.")
        else:
            removed_msgs.append("Not found in Exclusive stock.")

    if scope == "both":
        if category in stock_data.get("categories", []):
            stock_data["categories"].remove(category)
            removed_msgs.append("Removed from master categories list.")

    await safe_save_stock()
    reply = ("⚠️ Category not present in master list. " if warning else "") + " ".join(removed_msgs)
    await interaction.followup.send(f"✅ {reply}", ephemeral=True)

# ---------------- addstock / removestock / restock ----------------
@tree.command(name="addstock", description="Add stock (Admin only). Provide text or attach a .txt file")
@is_admin_check()
@app_commands.autocomplete(stock_type=stock_type_autocomplete, category=category_autocomplete)
async def cmd_addstock(
    interaction: discord.Interaction,
    stock_type: str,
    category: str,
    items: Optional[str] = None,
    file: Optional[discord.Attachment] = None
):
    """
    Use 'items' (paste multi-line or comma-separated list) OR attach a .txt file.
    Parameter names must match decorator (stock_type, category).
    """
    await interaction.response.defer(ephemeral=True)
    t = stock_type.lower()
    if t not in ("free", "exclusive"):
        await interaction.followup.send("❌ Type must be `free` or `exclusive`.", ephemeral=True)
        return
    key = "FREE" if t == "free" else "EXCLUSIVE"
    await safe_load_stock()
    if category not in stock_data.get("categories", []):
        await interaction.followup.send("❌ Invalid category. Create it first with /addcategory.", ephemeral=True)
        return

    new_items: List[str] = []
    if file:
        try:
            raw = await file.read()
            text = raw.decode(errors="ignore")
            lines = parse_items_from_text(text)
        except Exception:
            await interaction.followup.send("❌ Could not read attached file. Make sure it's a plain .txt file.", ephemeral=True)
            return
        for line in lines:
            if line not in stock_data[key].get(category, []):
                new_items.append(line)
    elif items:
        lines = parse_items_from_text(items)
        for line in lines:
            if line not in stock_data[key].get(category, []):
                new_items.append(line)
    else:
        await interaction.followup.send("❌ Provide stock text (one per line) in the **items** field or attach a .txt file.", ephemeral=True)
        return

    stock_data[key].setdefault(category, []).extend(new_items)
    await safe_save_stock()
    await interaction.followup.send(f"✅ Added {len(new_items)} item(s) to `{category}`.", ephemeral=True)

    # Ping restock channel (best-effort)
    restock_channel = bot.get_channel(RESTOCK_CHANNEL_ID) or (interaction.guild.get_channel(RESTOCK_CHANNEL_ID) if interaction.guild else None)
    role_id = FREE_GEN_ROLE_ID if key == "FREE" else EXCLUSIVE_ROLE_ID
    if restock_channel and new_items:
        try:
            await restock_channel.send(f"<@&{role_id}> 🔔 `{category}` was restocked ({len(new_items)} new item(s)).")
        except Exception:
            pass

@tree.command(name="removestock", description="Remove stock items (Admin only). Provide text or attach .txt")
@is_admin_check()
@app_commands.autocomplete(stock_type=stock_type_autocomplete, category=category_autocomplete)
async def cmd_removestock(
    interaction: discord.Interaction,
    stock_type: str,
    category: str,
    items: Optional[str] = None,
    file: Optional[discord.Attachment] = None
):
    await interaction.response.defer(ephemeral=True)
    t = stock_type.lower()
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
            text = raw.decode(errors="ignore")
            lines = parse_items_from_text(text)
        except Exception:
            await interaction.followup.send("❌ Could not read attached file. Use a plain .txt.", ephemeral=True)
            return
        for line in lines:
            while line in stock_data[key].get(category, []):
                stock_data[key][category].remove(line)
                removed += 1
    elif items:
        lines = parse_items_from_text(items)
        for line in lines:
            while line in stock_data[key].get(category, []):
                stock_data[key][category].remove(line)
                removed += 1
    else:
        await interaction.followup.send("❌ Provide items to remove as text or attach a .txt file.", ephemeral=True)
        return

    await safe_save_stock()
    await interaction.followup.send(f"✅ Removed {removed} item(s) from `{category}`.", ephemeral=True)

@tree.command(name="restock", description="Replace stock for a category (Admin only)")
@is_admin_check()
@app_commands.autocomplete(stock_type=stock_type_autocomplete, category=category_autocomplete)
async def cmd_restock(
    interaction: discord.Interaction,
    stock_type: str,
    category: str,
    items: Optional[str] = None,
    file: Optional[discord.Attachment] = None
):
    await interaction.response.defer(ephemeral=True)
    t = stock_type.lower()
    if t not in ("free", "exclusive"):
        await interaction.followup.send("❌ Type must be `free` or `exclusive`.", ephemeral=True)
        return
    key = "FREE" if t == "free" else "EXCLUSIVE"
    await safe_load_stock()
    if category not in stock_data.get("categories", []):
        await interaction.followup.send("❌ Invalid category.", ephemeral=True)
        return

    new_items: List[str] = []
    if file:
        try:
            raw = await file.read()
            text = raw.decode(errors="ignore")
            lines = parse_items_from_text(text)
            new_items = list(dict.fromkeys(lines))
        except Exception:
            await interaction.followup.send("❌ Could not read attached file. Use a plain .txt.", ephemeral=True)
            return
    elif items:
        lines = parse_items_from_text(items)
        new_items = list(dict.fromkeys(lines))
    else:
        await interaction.followup.send("❌ Provide stock text or attach a .txt file.", ephemeral=True)
        return

    stock_data[key][category] = new_items
    await safe_save_stock()
    await interaction.followup.send(f"♻️ `{category}` fully restocked with {len(new_items)} item(s).", ephemeral=True)

    restock_channel = bot.get_channel(RESTOCK_CHANNEL_ID) or (interaction.guild.get_channel(RESTOCK_CHANNEL_ID) if interaction.guild else None)
    role_id = FREE_GEN_ROLE_ID if key == "FREE" else EXCLUSIVE_ROLE_ID
    if restock_channel:
        try:
            await restock_channel.send(f"<@&{role_id}> 🚀 `{category}` fully restocked with {len(new_items)} item(s).")
        except Exception:
            pass

# ---------------- vouch system ----------------
@tree.command(name="vouch", description="Post a vouch to the vouch channel (Admin only)")
@is_admin_check()
@app_commands.choices(rating=[app_commands.Choice(name=str(i), value=i) for i in range(1,6)])
async def cmd_vouch(interaction: discord.Interaction, username: str, rating: int, reason: str):
    """
    Admins post a vouch: username (string), rating (1-5), reason (string).
    Vouches are recorded in vouches.json and posted to VOUCH_CHANNEL_ID.
    IMPORTANT: Vouches do NOT auto-grant roles. Exclusive users do NOT need to vouch.
    """
    await interaction.response.defer(ephemeral=True)
    embed = discord.Embed(title="🆕 New Vouch", color=discord.Color.green(), timestamp=discord.utils.utcnow())
    embed.add_field(name="User", value=username, inline=True)
    embed.add_field(name="Rating", value=f"{'⭐'*rating} ({rating}/5)", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_footer(text=f"Vouched by {interaction.user} • ID: {interaction.user.id}")

    vouch_channel = bot.get_channel(VOUCH_CHANNEL_ID) or (interaction.guild.get_channel(VOUCH_CHANNEL_ID) if interaction.guild else None)
    try:
        if vouch_channel:
            await vouch_channel.send(embed=embed)
    except Exception:
        pass

    await safe_load_vouches()
    entry = {
        "username": username,
        "rating": rating,
        "reason": reason,
        "vouched_by": f"{interaction.user} ({interaction.user.id})",
        "timestamp": int(time.time())
    }
    vouch_data.setdefault("vouches", []).insert(0, entry)
    vouch_data["vouches"] = vouch_data["vouches"][:500]
    await safe_save_vouches()

    await interaction.followup.send("✅ Vouch posted and saved.", ephemeral=True)

@tree.command(name="vouch-list", description="Show the most recent vouches (Admin only)")
@is_admin_check()
async def cmd_vouch_list(interaction: discord.Interaction, limit: Optional[int] = 5):
    await interaction.response.defer(ephemeral=True)
    await safe_load_vouches()
    vouches = vouch_data.get("vouches", [])[:limit]
    if not vouches:
        await interaction.followup.send("No vouches found.", ephemeral=True)
        return
    lines = []
    for v in vouches:
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v["timestamp"]))
        lines.append(f"**{v['username']}** — {v['rating']}/5 — {v['reason'][:80]} — {t}")
    await interaction.followup.send("\n".join(lines), ephemeral=True)

# ---------------- invites (replaces /verify) ----------------
@tree.command(name="invites", description="See progress toward Free Gen role (5 invites required)")
async def cmd_invites(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    guild = interaction.guild
    if not guild:
        await interaction.followup.send("This command must be used in a server.", ephemeral=True)
        return
    inviter_id = interaction.user.id
    count = invite_tracker.get(guild.id, {}).get(inviter_id, 0)
    needed = max(0, 5 - count)
    embed = discord.Embed(
        title="🎯 Free Gen Invite Progress",
        description=(f"Invite friends to earn the Free Gen role — it's quick and totally free!\n\n"
                     f"**Your progress:** {count} invite(s)\n"
                     f"**Remaining to earn role:** {needed}\n\n"
                     "When you reach 5 invites you'll automatically receive the Free Gen role. Keep sharing the invite link!"),
        color=discord.Color.green()
    )
    embed.set_footer(text="Invites tracked while bot is online — counts are best-effort.")
    await interaction.followup.send(embed=embed, ephemeral=True)

# ---------------- redeem modal ----------------
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
        await interaction.followup.send("✅ Your code has been submitted for verification. Staff will review shortly.", ephemeral=True)

@tree.command(name="redeem-exclusive", description="Redeem Exclusive access via gift card")
async def cmd_redeem(interaction: discord.Interaction):
    await interaction.response.send_modal(RedeemModal())

# ---------------- resync (admin, guarded) ----------------
@tree.command(name="resync-commands", description="(Admin) Register/sync commands to the guild (use only if needed)")
@is_admin_check()
async def cmd_resync(interaction: discord.Interaction):
    global _last_resync_ts
    await interaction.response.defer(ephemeral=True)
    now = now_ts()
    if now - _last_resync_ts < RESYNC_COOLDOWN:
        await interaction.followup.send("❌ Commands were resynced recently. Wait before running again to avoid rate limits.", ephemeral=True)
        return
    try:
        guild = bot.get_guild(GUILD_ID)
        if guild is None:
            await interaction.followup.send("❌ Bot not in configured guild. Cannot resync.", ephemeral=True)
            return
        synced = await tree.sync(guild=guild)
        _last_resync_ts = now_ts()
        await interaction.followup.send(f"✅ Commands synced to guild ({len(synced)} commands).", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ Missing permissions when syncing commands. Ensure bot has applications.commands scope & is in guild.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

# ---------------- global app command error handler ----------------
@bot.tree.error
async def global_appcmd_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingRole) or isinstance(error, app_commands.CheckFailure):
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        except Exception:
            pass
        return
    if isinstance(error, app_commands.CommandNotFound):
        try:
            if not interaction.response.is_done():
                await interaction.response.send_message("⚠️ That command isn't available. Ask an admin to run `/resync-commands`.", ephemeral=True)
        except Exception:
            pass
        return
    # fallback: notify staff and user
    print(f"[AppCommandError] {error!r}")
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message("An unexpected error occurred. Staff has been notified.", ephemeral=True)
    except Exception:
        pass
    try:
        staff = await bot.fetch_user(STAFF_NOTIFY_USER_ID)
        await staff.send(f"[Error] User {interaction.user} triggered an error: {error!r}")
    except Exception:
        pass

# ---------------- invite events ----------------
@bot.event
async def on_member_join(member: discord.Member):
    guild = member.guild
    old_invites = invites_cache.get(guild.id, [])
    try:
        new_invites = await guild.invites()
    except Exception:
        invites_cache[guild.id] = old_invites
        return

    inviter = None
    for invite in new_invites:
        matched = next((old for old in old_invites if old.code == invite.code), None)
        if matched and invite.uses > matched.uses:
            inviter = invite.inviter
            break

    invites_cache[guild.id] = new_invites

    if inviter:
        invite_tracker.setdefault(guild.id, {})
        invite_tracker[guild.id].setdefault(inviter.id, 0)
        invite_tracker[guild.id][inviter.id] += 1

        # Grant FreeGen role at 5 invites
        if invite_tracker[guild.id][inviter.id] >= 5:
            role = guild.get_role(FREE_GEN_ROLE_ID)
            user = guild.get_member(inviter.id)
            if role and user:
                try:
                    await user.add_roles(role)
                except Exception:
                    pass

@bot.event
async def on_member_remove(member: discord.Member):
    guild = member.guild
    trackers = invite_tracker.get(guild.id, {})
    if not trackers:
        return
    for inviter_id in list(trackers.keys()):
        if invite_tracker[guild.id].get(inviter_id, 0) > 0:
            invite_tracker[guild.id][inviter_id] -= 1
            if invite_tracker[guild.id][inviter_id] < 5:
                role = guild.get_role(FREE_GEN_ROLE_ID)
                user = guild.get_member(inviter_id)
                if role and user and role in user.roles:
                    try:
                        await user.remove_roles(role)
                    except Exception:
                        pass
            break

# ---------------- on_ready: one-time HTTP clears + sync ----------------
@bot.event
async def on_ready():
    # populate invites cache
    for g in bot.guilds:
        try:
            invites_cache[g.id] = await g.invites()
        except Exception:
            invites_cache[g.id] = []

    print(f"✅ Logged in as {bot.user} (id: {bot.user.id})")

    # start background loops safely
    if not boost_loop.is_running():
        boost_loop.start()

    # --- ONE-TIME: clear guild commands via HTTP (safe for mobile) ---
    try:
        if not os.path.exists(_CLEARED_MARKER):
            TOKEN = os.getenv(TOKEN_ENV_NAME)
            APP_ID = APPLICATION_ID
            if not TOKEN:
                print("[CLEAR ERROR] TOKEN env var not set; cannot clear commands automatically.")
            else:
                url = f"https://discord.com/api/v10/applications/{APP_ID}/guilds/{GUILD_ID}/commands"
                print("Attempting one-time guild command clear via HTTP...")
                try:
                    async with aiohttp.ClientSession() as session:
                        headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
                        async with session.put(url, json=[], headers=headers, timeout=20) as resp:
                            text = await resp.text()
                            print(f"[CLEAR HTTP - GUILD] status {resp.status}")
                            if resp.status in (200, 204):
                                try:
                                    with open(_CLEARED_MARKER, "w", encoding="utf-8") as fh:
                                        fh.write(str(int(time.time())))
                                    print("✅ One-time guild clear succeeded; marker file created.")
                                except Exception as e:
                                    print(f"[CLEAR ERROR] could not write guild marker file: {e}")
                            else:
                                print(f"[CLEAR ERROR] guild http {resp.status} body: {text}")
                except Exception as e:
                    print(f"[CLEAR ERROR] exception while calling guild API: {e}")
        else:
            print("One-time guild clear already performed (marker found). Skipping guild HTTP clear.")
    except Exception as e:
        print(f"[CLEAR ERROR] unexpected: {e}")

    # --- ONE-TIME: clear GLOBAL commands via HTTP if needed ---
    try:
        if not os.path.exists(_CLEARED_GLOBAL_MARKER):
            TOKEN = os.getenv(TOKEN_ENV_NAME)
            APP_ID = APPLICATION_ID
            if not TOKEN:
                print("[GLOBAL CLEAR ERROR] TOKEN env var not set; cannot clear global commands automatically.")
            else:
                url_global = f"https://discord.com/api/v10/applications/{APP_ID}/commands"
                print("Attempting one-time GLOBAL command clear via HTTP...")
                try:
                    async with aiohttp.ClientSession() as session:
                        headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
                        async with session.put(url_global, json=[], headers=headers, timeout=30) as resp:
                            text = await resp.text()
                            print(f"[CLEAR HTTP - GLOBAL] status {resp.status}")
                            if resp.status in (200, 204):
                                try:
                                    with open(_CLEARED_GLOBAL_MARKER, "w", encoding="utf-8") as fh:
                                        fh.write(str(int(time.time())))
                                    print("✅ One-time global clear succeeded; marker file created.")
                                except Exception as e:
                                    print(f"[GLOBAL CLEAR ERROR] could not write global marker file: {e}")
                            else:
                                print(f"[GLOBAL CLEAR ERROR] http {resp.status} body: {text}")
                except Exception as e:
                    print(f"[GLOBAL CLEAR ERROR] exception while calling global API: {e}")
        else:
            print("One-time global clear already performed (marker found). Skipping global HTTP clear.")
    except Exception as e:
        print(f"[GLOBAL CLEAR ERROR] unexpected: {e}")

    # --- SYNC commands to the guild (safe sync) ---
    try:
        guild = bot.get_guild(GUILD_ID)
        if guild is None:
            print(f"[SYNC ERROR] Guild {GUILD_ID} not present in bot.guilds. Skipping guild sync.")
        else:
            try:
                # guild-sync via object (scoped sync — fast and avoids global rate limits)
                synced = await tree.sync(guild=guild)
                print(f"✅ Commands synced to guild ({len(synced)} commands).")
            except Exception as e:
                print(f"[SYNC ERROR] when syncing to guild: {e}")
    except Exception as e:
        print(f"[SYNC ERROR] unexpected: {e}")

    # Optional one-time global sync (only if SYNC_ON_START true)
    if SYNC_ON_START:
        try:
            all_synced = await tree.sync()
            print(f"[SYNC_ON_START] global sync: {len(all_synced)} commands")
        except Exception as e:
            print(f"[SYNC_ON_START ERROR] {e}")

# ---------------- on_message: auto-delete plain user messages in certain channels ----------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.webhook_id or message.type != discord.MessageType.default:
        await bot.process_commands(message)
        return
    if message.content and message.content.startswith("/"):
        await bot.process_commands(message)
        return
    if message.channel.id in AUTODELETE_CHANNELS:
        try:
            await message.delete()
        except Exception:
            pass
        return
    await bot.process_commands(message)

# ---------------- run ----------------
if __name__ == "__main__":
    TOKEN = os.getenv(TOKEN_ENV_NAME)
    if not TOKEN:
        print("[ERROR] TOKEN env var not set. Please set TOKEN in Railway or your host.")
    else:
        # reload data before starting
        stock_data = _load_json(STOCK_FILE)
        vouch_data = _load_json(VOUCH_FILE)
        bot.run(TOKEN)
