# bot.py — Final full working generator bot
# - Use environment variable TOKEN for the bot token
# - Place this file in your repo and deploy (Railway / other)
# - Make sure requirements.txt contains: discord.py, aiohttp
# - After first deploy you can remove or set SYNC_ON_START=0 to avoid repeated syncs.

import os
import json
import time
import asyncio
from typing import Optional, List, Dict

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------- CONFIG (change IDs if needed) ----------------
GUILD_ID = 1452717489656954961          # Your server ID
FREE_GEN_ROLE_ID = 1467913996723032315  # FreeGen role
EXCLUSIVE_ROLE_ID = 1453906576237924603
BOOST_ROLE_ID = 1453187878061478019
ADMIN_ROLE_ID = 1452719764119093388
STAFF_NOTIFY_USER_ID = 884084052854984726
RESTOCK_CHANNEL_ID = 1478792670049599618

# Channels where plain user messages should be auto-deleted (no response)
AUTODELETE_CHANNELS = {1478790217971273788, 1454503001363583019}

# Invite role (defaults to FREE_GEN_ROLE_ID). Change if you want a separate invite reward role.
INVITE_ROLE_ID = FREE_GEN_ROLE_ID

STOCK_FILE = "stock.json"

# cooldowns (seconds)
FREE_COOLDOWN = 180
EXCL_COOLDOWN = 60
RESYNC_COOLDOWN = 60 * 60  # 1 hour between manual resyncs

# Optional: if set to "1" the bot will attempt a one-time guild sync on_ready
SYNC_ON_START = os.getenv("SYNC_ON_START", "0") == "1"

# ---------------- INTENTS & BOT ----------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

# ---------------- storage & lock ----------------
_file_lock = asyncio.Lock()

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

# ---------------- user commands ----------------
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

# ---------------- admin commands ----------------
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
    if restock_channel:
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
        guild_obj = discord.Object(id=GUILD_ID)
        synced = await tree.sync(guild=guild_obj)
        _last_resync_ts = now_ts()
        await interaction.followup.send(f"✅ Commands synced to guild ({len(synced)} commands).", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ Missing access when syncing commands. Ensure bot has applications.commands scope & is in guild.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

# ---------------- global app command error handler ----------------
@bot.tree.error
async def global_appcmd_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    # Handle common app command errors gracefully
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

        # Grant role at 5 invites
        if invite_tracker[guild.id][inviter.id] >= 5:
            role = guild.get_role(INVITE_ROLE_ID)
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
                role = guild.get_role(INVITE_ROLE_ID)
                user = guild.get_member(inviter_id)
                if role and user and role in user.roles:
                    try:
                        await user.remove_roles(role)
                    except Exception:
                        pass
            break

# ---------------- on_ready: populate invite cache, start loops, optionally sync ----------------
@bot.event
async def on_ready():
    # populate invites cache
    for guild in bot.guilds:
        try:
            invites_cache[guild.id] = await guild.invites()
        except Exception:
            invites_cache[guild.id] = []

    print(f"✅ Logged in as {bot.user} (id: {bot.user.id})")

    # start background loops safely
    if not boost_loop.is_running():
        boost_loop.start()

    # Force a guild-only sync to eliminate command signature mismatches (best practice)
    # This sync is scoped to the configured GUILD_ID to avoid global rate limits.
    try:
        guild_obj = discord.Object(id=GUILD_ID)
        synced = await tree.sync(guild=guild_obj)
        print(f"✅ Commands synced to guild ({len(synced)} commands).")
    except Exception as e:
        print(f"[SYNC ERROR] {e}")

    # Optional extra one-time automatic sync (disabled by default); keep OFF once working to avoid rate-limits.
    if SYNC_ON_START:
        try:
            all_synced = await tree.sync()
            print(f"[SYNC_ON_START] global sync: {len(all_synced)} commands")
        except Exception as e:
            print(f"[SYNC_ON_START ERROR] {e}")

# ---------------- on_message: auto-delete plain user messages in certain channels ----------------
@bot.event
async def on_message(message: discord.Message):
    # allow bots and webhooks and application messages
    if message.author.bot:
        await bot.process_commands(message)
        return

    if message.webhook_id is not None:
        await bot.process_commands(message)
        return

    if message.type != discord.MessageType.default:
        await bot.process_commands(message)
        return

    # If the message starts with '/', assume user is invoking a slash command — do not delete
    if message.content and message.content.startswith("/"):
        await bot.process_commands(message)
        return

    # Only auto-delete in configured channels
    if message.channel.id in AUTODELETE_CHANNELS:
        try:
            await message.delete()
        except Exception:
            pass
        return

    await bot.process_commands(message)

# ---------------- run ----------------
if __name__ == "__main__":
    TOKEN = os.getenv("TOKEN")
    if not TOKEN:
        print("[ERROR] TOKEN env var not set. Please set TOKEN in Railway or your host.")
    else:
        _ensure_stock_file()
        stock_data = _load_stock_from_disk()
        bot.run(TOKEN)
