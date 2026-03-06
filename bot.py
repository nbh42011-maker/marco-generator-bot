# bot.py — Updated: vouch behavior configurable (default: NO vouch on !freegenrole)
# Requirements: discord.py, aiohttp
# Env: TOKEN must be set

import os
import json
import time
import asyncio
from typing import Optional, List, Dict, Set

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------- CONFIG ----------------
GUILD_ID = 1452717489656954961          # your server id (int)
APPLICATION_ID = "1478522023696273428"  # app/client id (string)

FREE_GEN_ROLE_ID = 1467913996723032315
EXCLUSIVE_ROLE_ID = 1453906576237924603
BOOST_ROLE_ID = 1453187878061478019
ADMIN_ROLE_ID = 1452719764119093388
STAFF_NOTIFY_USER_ID = 884084052854984726
RESTOCK_CHANNEL_ID = 1478792670049599618

AUTODELETE_CHANNELS = {1478790217971273788, 1454503001363583019}

# VOUCH config (these are the staff who may vouch)
VOUCH_REQUIRED_USERS = {884084052854984726, 1469703951166210223}
VOUCH_CHANNEL_ID = 1452868333383716915
VOUCH_TIMEOUT_SECONDS = 60 * 5  # 5 minutes

STOCK_FILE = "stock.json"

FREE_COOLDOWN = 180
EXCL_COOLDOWN = 60
RESYNC_COOLDOWN = 60 * 60

_CLEARED_MARKER = "commands_cleared.lock"
_CLEARED_GLOBAL_MARKER = "commands_cleared_global.lock"

SYNC_ON_START = os.getenv("SYNC_ON_START", "0") == "1"

# NEW FLAG: if False, !freegenrole grants role immediately and NO vouch timer.
# If True, the bot will grant role and start the 5-minute vouch watch (requires both staff to vouch).
REQUIRE_VOUCH_FOR_FREEGEN = False

# ---------------- INTENTS & BOT ----------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
tree = bot.tree

# ---------------- STORAGE & LOCK ----------------
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

# ---------------- cooldowns/resync guard ----------------
_cooldowns = {}  # {(user_id, "FREE"|"EXCLUSIVE"): timestamp}
_last_resync_ts = 0

def now_ts():
    return time.time()

def check_cooldown(user_id: int, typ: str) -> int:
    key = (user_id, typ)
    last = _cooldowns.get(key, 0)
    limit = FREE_COOLDOWN if typ == "FREE" else EXCL_COOLDOWN
    rem = int(limit - (now_ts() - last)) if now_ts() - last < limit else 0
    return rem

def set_cooldown(user_id: int, typ: str):
    _cooldowns[(user_id, typ)] = now_ts()

# ---------------- admin check helper ----------------
def is_admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        user = interaction.user
        if not hasattr(user, "roles"):
            return False
        return any(r.id == ADMIN_ROLE_ID for r in getattr(user, "roles", []))
    return app_commands.check(predicate)

# ---------------- autocomplete ----------------
async def category_autocomplete(interaction: discord.Interaction, current: str):
    await safe_load_stock()
    cats = stock_data.get("categories", [])
    return [app_commands.Choice(name=c, value=c) for c in cats if current.lower() in c.lower()][:25]

async def type_autocomplete(interaction: discord.Interaction, current: str):
    opts = ["free", "exclusive"]
    return [app_commands.Choice(name=o.capitalize(), value=o) for o in opts if current.lower() in o.lower()][:25]

# ---------------- util / formatting ----------------
def format_stock_embed():
    d = stock_data
    embed = discord.Embed(title="📦 Marcos Gen • Stock Overview", color=discord.Color.blue())
    free_lines = []
    excl_lines = []
    for cat in d.get("categories", []):
        free_lines.append(f"**{cat}** → {len(d.get('FREE', {}).get(cat, []))}")
        excl_lines.append(f"**{cat}** → {len(d.get('EXCLUSIVE', {}).get(cat, []))}")
    embed.add_field(name="🆓 Free Stock", value="\n".join(free_lines) or "No categories", inline=False)
    embed.add_field(name="💎 Exclusive Stock", value="\n".join(excl_lines) or "No categories", inline=False)
    embed.set_footer(text="Professional • Secure • Automated")
    return embed

def parse_items_from_text(text: str) -> List[str]:
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

# ---------------- vouch state (used only if REQUIRE_VOUCH_FOR_FREEGEN=True) ----------------
pending_vouches: Dict[int, Dict] = {}  # user_id -> {expires, vouchers:set, task}

def _make_vouch_task(user_id: int):
    async def waiter():
        await asyncio.sleep(VOUCH_TIMEOUT_SECONDS)
        if pending_vouches.get(user_id):
            await _expire_vouch_request(user_id)
    return asyncio.create_task(waiter())

async def _expire_vouch_request(user_id: int):
    pending = pending_vouches.get(user_id)
    if not pending:
        return
    guild = bot.get_guild(GUILD_ID)
    if guild:
        member = guild.get_member(user_id)
        if member:
            role = guild.get_role(FREE_GEN_ROLE_ID)
            if role and role in member.roles:
                try:
                    await member.remove_roles(role)
                except Exception:
                    pass
            try:
                await member.send(
                    ("⏳ Verification timed out — both staff members did not vouch within the time window.\n\n"
                     "Your Free Gen role was removed. To appeal, open a ticket or contact staff.")
                )
            except Exception:
                pass
    pending_vouches.pop(user_id, None)

# ---------------- background loops ----------------
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

# ---------------- Gen UI ----------------
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
            await interaction.user.send(f"🎉 Here is your item from {cat}:\n```{item}```")
        except Exception:
            dm_ok = False

        if dm_ok:
            await interaction.followup.send("✅ Sent to your DMs.", ephemeral=True)
        else:
            await interaction.followup.send(
                ("⚠️ Could not send DM. Please enable DMs from server members.\n\n"
                 f"Here is your item for now:\n```{item}```"),
                ephemeral=True
            )

class GenView(discord.ui.View):
    def __init__(self, typ: str):
        super().__init__(timeout=60)
        self.add_item(GenSelect(typ))

# ---------------- USER COMMANDS (guild-scoped) ----------------
GUILD_OBJ = discord.Object(id=GUILD_ID)

@tree.command(name="gen", description="Generate a Free item", guild=GUILD_OBJ)
async def cmd_gen(interaction: discord.Interaction):
    await safe_load_stock()
    member_roles = [r.id for r in getattr(interaction.user, "roles", [])]
    if FREE_GEN_ROLE_ID not in member_roles:
        await interaction.response.send_message(
            ("❌ Free Gen access requires the FreeGen role. Type `!freegenrole` to request it."), ephemeral=True
        )
        return
    await interaction.response.send_message("📦 Select a Free category:", view=GenView("FREE"), ephemeral=True)

@tree.command(name="exclusive-gen", description="Generate an Exclusive item", guild=GUILD_OBJ)
async def cmd_exclusive_gen(interaction: discord.Interaction):
    # Exclusive never requires vouch
    if EXCLUSIVE_ROLE_ID not in [r.id for r in getattr(interaction.user, "roles", [])]:
        await interaction.response.send_message("❌ You need the Exclusive role to use this command.", ephemeral=True)
        return
    await safe_load_stock()
    await interaction.response.send_message("💎 Select an Exclusive category:", view=GenView("EXCLUSIVE"), ephemeral=True)

@tree.command(name="stock", description="View current stock", guild=GUILD_OBJ)
async def cmd_stock(interaction: discord.Interaction):
    await safe_load_stock()
    embed = format_stock_embed()
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---------------- ADMIN COMMANDS ----------------
@tree.command(name="addcategory", description="Add a category (Admin only)", guild=GUILD_OBJ)
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

@tree.command(name="removecategory", description="Remove a category (Admin only)", guild=GUILD_OBJ)
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

@tree.command(name="addstock", description="Add stock (Admin only). Provide text or attach a .txt file", guild=GUILD_OBJ)
@is_admin_check()
@app_commands.autocomplete(type=type_autocomplete, category=category_autocomplete)
async def cmd_addstock(
    interaction: discord.Interaction,
    type: str,
    category: str,
    stock: Optional[str] = None,
    file: Optional[discord.Attachment] = None
):
    await interaction.response.defer(ephemeral=True)
    t = type.lower()
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
    elif stock:
        lines = parse_items_from_text(stock)
        for line in lines:
            if line not in stock_data[key].get(category, []):
                new_items.append(line)
    else:
        await interaction.followup.send("❌ Provide stock text (one per line) in the **stock** field or attach a .txt file.", ephemeral=True)
        return

    stock_data[key].setdefault(category, []).extend(new_items)
    await safe_save_stock()
    await interaction.followup.send(f"✅ Added {len(new_items)} item(s) to {category}.", ephemeral=True)

    restock_channel = bot.get_channel(RESTOCK_CHANNEL_ID) or (interaction.guild.get_channel(RESTOCK_CHANNEL_ID) if interaction.guild else None)
    role_id = FREE_GEN_ROLE_ID if key == "FREE" else EXCLUSIVE_ROLE_ID
    if restock_channel:
        try:
            await restock_channel.send(f"<@&{role_id}> 🔔 {category} was restocked — {len(new_items)} new items.")
        except Exception:
            pass

@tree.command(name="removestock", description="Clear stock for a type+category (Admin only)", guild=GUILD_OBJ)
@is_admin_check()
@app_commands.autocomplete(type=type_autocomplete, category=category_autocomplete)
async def cmd_removestock(
    interaction: discord.Interaction,
    type: str,
    category: str
):
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

    removed_count = len(stock_data.get(key, {}).get(category, []))
    stock_data[key][category] = []
    await safe_save_stock()
    await interaction.followup.send(f"✅ Cleared {removed_count} item(s) from {category}.", ephemeral=True)

    restock_channel = bot.get_channel(RESTOCK_CHANNEL_ID) or (interaction.guild.get_channel(RESTOCK_CHANNEL_ID) if interaction.guild else None)
    role_id = FREE_GEN_ROLE_ID if key == "FREE" else EXCLUSIVE_ROLE_ID
    if restock_channel:
        try:
            await restock_channel.send(f"<@&{role_id}> 🗑️ {category} has been cleared.")
        except Exception:
            pass

@tree.command(name="restock", description="Replace stock for a category (Admin only)", guild=GUILD_OBJ)
@is_admin_check()
@app_commands.autocomplete(type=type_autocomplete, category=category_autocomplete)
async def cmd_restock(
    interaction: discord.Interaction,
    type: str,
    category: str,
    stock: Optional[str] = None,
    file: Optional[discord.Attachment] = None
):
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
    elif stock:
        lines = parse_items_from_text(stock)
        new_items = list(dict.fromkeys(lines))
    else:
        await interaction.followup.send("❌ Provide stock text or attach a .txt file.", ephemeral=True)
        return

    stock_data[key][category] = new_items
    await safe_save_stock()
    await interaction.followup.send(f"♻️ {category} fully restocked — {len(new_items)} item(s).", ephemeral=True)

    restock_channel = bot.get_channel(RESTOCK_CHANNEL_ID) or (interaction.guild.get_channel(RESTOCK_CHANNEL_ID) if interaction.guild else None)
    role_id = FREE_GEN_ROLE_ID if key == "FREE" else EXCLUSIVE_ROLE_ID
    if restock_channel:
        try:
            await restock_channel.send(f"<@&{role_id}> 🚀 {category} fully restocked — {len(new_items)} items.")
        except Exception:
            pass

# ---------------- REDEEM ----------------
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

@tree.command(name="redeem-exclusive", description="Redeem Exclusive access via gift card", guild=GUILD_OBJ)
async def cmd_redeem(interaction: discord.Interaction):
    await interaction.response.send_modal(RedeemModal())

# ---------------- RESYNC ----------------
@tree.command(name="resync-commands", description="(Admin) Register/sync commands to the guild", guild=GUILD_OBJ)
@is_admin_check()
async def cmd_resync(interaction: discord.Interaction):
    global _last_resync_ts
    await interaction.response.defer(ephemeral=True)
    now = now_ts()
    if now - _last_resync_ts < RESYNC_COOLDOWN:
        await interaction.followup.send("❌ Commands were resynced recently. Wait before running again to avoid rate limits.", ephemeral=True)
        return
    try:
        guild_obj = bot.get_guild(GUILD_ID)
        if not guild_obj:
            await interaction.followup.send("❌ Bot is not in the configured guild.", ephemeral=True)
            return
        synced = await tree.sync(guild=guild_obj)
        _last_resync_ts = now_ts()
        await interaction.followup.send(f"✅ Commands synced to guild ({len(synced)} commands).", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ Missing access when syncing commands. Ensure bot has applications.commands scope & is in guild.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

# ---------------- global app command error ----------------
@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
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
                await interaction.response.send_message("⚠️ That command isn't available. Ask an admin to run `/resync-commands`.", ephemeral=True)
        except Exception:
            pass
        return
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

# ---------------- on_ready: one-time HTTP clears + sync ----------------
@bot.event
async def on_ready():
    # best-effort invites cache (not used)
    for g in bot.guilds:
        try:
            _ = await g.invites()
        except Exception:
            pass

    print(f"✅ Logged in as {bot.user} (id: {bot.user.id})")

    if not boost_loop.is_running():
        boost_loop.start()

    # One-time guild clear via HTTP
    try:
        if not os.path.exists(_CLEARED_MARKER):
            TOKEN = os.getenv("TOKEN")
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

    # One-time global clear
    try:
        if not os.path.exists(_CLEARED_GLOBAL_MARKER):
            TOKEN = os.getenv("TOKEN")
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

    # Sync commands to the guild
    try:
        guild = bot.get_guild(GUILD_ID)
        if guild is None:
            print(f"[SYNC ERROR] Guild {GUILD_ID} not present in bot.guilds. Skipping guild sync.")
        else:
            try:
                synced = await tree.sync(guild=guild)
                print(f"✅ Commands synced to guild ({len(synced)} commands).")
            except Exception as e:
                print(f"[SYNC ERROR] when syncing to guild: {e}")
    except Exception as e:
        print(f"[SYNC ERROR] unexpected: {e}")

    if SYNC_ON_START:
        try:
            all_synced = await tree.sync()
            print(f"[SYNC_ON_START] global sync: {len(all_synced)} commands")
        except Exception as e:
            print(f"[SYNC_ON_START ERROR] {e}")

# ---------------- on_message: handle !freegenrole, vouches, and auto-delete channels ----------------
@bot.event
async def on_message(message: discord.Message):
    # ignore bots & webhooks
    if message.author.bot:
        await bot.process_commands(message)
        return

    # VOUCH CHANNEL HANDLING (only relevant when REQUIRE_VOUCH_FOR_FREEGEN=True)
    if message.channel.id == VOUCH_CHANNEL_ID:
        if message.author.id in VOUCH_REQUIRED_USERS:
            content = (message.content or "").lower()
            if "vouch" in content:
                mentioned_ids = {m.id for m in message.mentions}
                if mentioned_ids:
                    for target_id in mentioned_ids:
                        if target_id in pending_vouches:
                            pend = pending_vouches[target_id]
                            vouchers: Set[int] = pend.get("vouchers", set())
                            if message.author.id not in vouchers:
                                vouchers.add(message.author.id)
                                pend["vouchers"] = vouchers
                                try:
                                    await message.author.send(f"✅ Vouch recorded for <@{target_id}>. Thank you.")
                                except Exception:
                                    pass
                                if VOUCH_REQUIRED_USERS.issubset(vouchers):
                                    task = pend.get("task")
                                    if task and not task.cancelled():
                                        task.cancel()
                                    pending_vouches.pop(target_id, None)
                                    guild = bot.get_guild(GUILD_ID)
                                    if guild:
                                        member = guild.get_member(target_id)
                                        if member:
                                            try:
                                                await member.send(
                                                    ("🎉 Vouch successful — both staff members have vouched for you.\n\n"
                                                     "You retain the Free Gen role. Enjoy the generator!")
                                                )
                                            except Exception:
                                                pass
        await bot.process_commands(message)
        return

    # TEXT COMMAND: !freegenrole (plain text; auto-deleted)
    if message.content and message.content.strip().lower() == "!freegenrole":
        try:
            await message.delete()
        except Exception:
            pass

        user_id = message.author.id
        guild = bot.get_guild(GUILD_ID)
        member = None
        if guild:
            member = guild.get_member(user_id)

        # Already have role?
        if member and any(r.id == FREE_GEN_ROLE_ID for r in member.roles):
            try:
                await message.author.send("ℹ️ You already have the Free Gen role. Enjoy the generator!")
            except Exception:
                pass
            return

        # If we do NOT require vouch, grant immediately and DM simple message
        if not REQUIRE_VOUCH_FOR_FREEGEN:
            granted = False
            if guild and member:
                role = guild.get_role(FREE_GEN_ROLE_ID)
                if role:
                    try:
                        await member.add_roles(role)
                        granted = True
                    except Exception:
                        granted = False
            try:
                if granted:
                    await message.author.send("🎉 You have been granted Free Gen access. Enjoy the generator! If you lose access for any reason, contact staff.")
                else:
                    await message.author.send("⚠️ We couldn't grant the Free Gen role automatically. Please contact staff.")
            except Exception:
                pass
            return

        # ELSE: REQUIRE_VOUCH_FOR_FREEGEN == True — old flow: grant role, start 5-minute pending vouch
        if user_id in pending_vouches:
            try:
                await message.author.send("ℹ️ You already requested FreeGen. Please wait for staff vouches in the vouch channel.")
            except Exception:
                pass
            return

        granted = False
        if guild and member:
            role = guild.get_role(FREE_GEN_ROLE_ID)
            if role:
                try:
                    await member.add_roles(role)
                    granted = True
                except Exception:
                    granted = False

        pending = {"expires": now_ts() + VOUCH_TIMEOUT_SECONDS, "vouchers": set()}
        pending["task"] = _make_vouch_task(user_id)
        pending_vouches[user_id] = pending

        try:
            await message.author.send(
                (f"🎉 You have unlocked Free Gen! You now have the FreeGen role.\n\n"
                 f"Next: To keep this role, both staff members must vouch for you within {VOUCH_TIMEOUT_SECONDS//60} minutes.\n"
                 f"Please ask staff to vouch in <#{VOUCH_CHANNEL_ID}> by posting **vouch** and mentioning you.\n\n"
                 "If both staff vouch within the time window, you'll receive a confirmation DM and keep the role. "
                 "If they don't, the role will be removed and you'll receive a DM with instructions to appeal.")
            )
        except Exception:
            pass
        return

    # If message starts with '/' assume slash invocation
    if message.content and message.content.startswith("/"):
        await bot.process_commands(message)
        return

    # Auto-delete plain messages in configured channels
    if message.channel.id in AUTODELETE_CHANNELS:
        try:
            await message.delete()
        except Exception:
            pass
        return

    await bot.process_commands(message)

# ---------------- RUN ----------------
if __name__ == "__main__":
    TOKEN = os.getenv("TOKEN")
    if not TOKEN:
        print("[ERROR] TOKEN env var not set. Please set TOKEN in Railway or your host.")
    else:
        _ensure_stock_file()
        stock_data = _load_stock_from_disk()
        bot.run(TOKEN)
