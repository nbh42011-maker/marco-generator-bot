# bot.py — Generator + stock + invite + no-vouch + emoji + restock UI + boost fix + auto-remove exclusive
# Requirements (requirements.txt): discord.py, aiohttp
# Env: set TOKEN to your bot token in Railway env vars.

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
APPLICATION_ID = "1478522023696273428"  # bot application/client id (string)

FREE_GEN_ROLE_ID = 1467913996723032315
EXCLUSIVE_ROLE_ID = 1453906576237924603
BOOST_ROLE_ID = 1453187878061478019
ADMIN_ROLE_ID = 1452719764119093388
STAFF_NOTIFY_USER_ID = 884084052854984726
RESTOCK_CHANNEL_ID = 1478792670049599618

AUTODELETE_CHANNELS = {1478790217971273788, 1454503001363583019}

# NOTE: vouch system removed per request

STOCK_FILE = "stock.json"
ASSIGNED_FILE = "assigned_roles.json"  # stores which users were auto-assigned exclusive by the bot

FREE_COOLDOWN = 180
EXCL_COOLDOWN = 60
RESYNC_COOLDOWN = 60 * 60

_CLEARED_MARKER = "commands_cleared.lock"

SYNC_ON_START = os.getenv("SYNC_ON_START", "0") == "1"

# ---------------- INTENTS & BOT ----------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.presences = True

# Provide application_id for stable command registration
_app_id_int = int(APPLICATION_ID) if APPLICATION_ID and APPLICATION_ID.isdigit() else None
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None, application_id=_app_id_int)
tree = bot.tree
GUILD_OBJ = discord.Object(id=GUILD_ID)

# ---------------- storage & locks ----------------
_file_lock = asyncio.Lock()
_assigned_lock = asyncio.Lock()

def _ensure_stock_file():
    if not os.path.exists(STOCK_FILE):
        with open(STOCK_FILE, "w", encoding="utf-8") as f:
            json.dump({"FREE": {}, "EXCLUSIVE": {}, "categories": [], "category_emojis": {}}, f, indent=4)

def _load_stock_from_disk() -> Dict:
    _ensure_stock_file()
    with open(STOCK_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    # ensure keys exist
    if "FREE" not in data: data["FREE"] = {}
    if "EXCLUSIVE" not in data: data["EXCLUSIVE"] = {}
    if "categories" not in data: data["categories"] = []
    if "category_emojis" not in data: data["category_emojis"] = {}
    return data

def _save_stock_to_disk(data: Dict):
    with open(STOCK_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def _ensure_assigned_file():
    if not os.path.exists(ASSIGNED_FILE):
        with open(ASSIGNED_FILE, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=4)

def _load_assigned_from_disk() -> Dict:
    _ensure_assigned_file()
    with open(ASSIGNED_FILE, "r", encoding="utf-8") as f:
        return json.load(f)

def _save_assigned_to_disk(data: Dict):
    with open(ASSIGNED_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

stock_data = _load_stock_from_disk()
assigned_data = _load_assigned_from_disk()  # maps str(user_id) -> {"exclusive_assigned": True/False, "boost_assigned": True/False}

async def safe_load_stock():
    global stock_data
    async with _file_lock:
        stock_data = _load_stock_from_disk()
        return stock_data

async def safe_save_stock():
    async with _file_lock:
        _save_stock_to_disk(stock_data)

async def load_assigned():
    global assigned_data
    async with _assigned_lock:
        assigned_data = _load_assigned_from_disk()
        return assigned_data

async def save_assigned():
    async with _assigned_lock:
        _save_assigned_to_disk(assigned_data)

def mark_exclusive_assigned(user_id: int):
    assigned_data[str(user_id)] = assigned_data.get(str(user_id), {})
    assigned_data[str(user_id)]['exclusive_assigned'] = True

def unmark_exclusive_assigned(user_id: int):
    rec = assigned_data.get(str(user_id))
    if rec and 'exclusive_assigned' in rec:
        rec.pop('exclusive_assigned', None)
    # remove entry if empty
    if rec and not rec:
        assigned_data.pop(str(user_id), None)

def is_exclusive_assigned(user_id: int) -> bool:
    return bool(assigned_data.get(str(user_id), {}).get('exclusive_assigned', False))

def mark_boost_assigned(user_id: int):
    assigned_data[str(user_id)] = assigned_data.get(str(user_id), {})
    assigned_data[str(user_id)]['boost_assigned'] = True

def unmark_boost_assigned(user_id: int):
    rec = assigned_data.get(str(user_id))
    if rec and 'boost_assigned' in rec:
        rec.pop('boost_assigned', None)
    if rec and not rec:
        assigned_data.pop(str(user_id), None)

def is_boost_assigned(user_id: int) -> bool:
    return bool(assigned_data.get(str(user_id), {}).get('boost_assigned', False))

# ---------------- util / cooldowns ----------------
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

def is_admin_check():
    async def predicate(interaction: discord.Interaction) -> bool:
        user = interaction.user
        if not hasattr(user, "roles"): return False
        return any(r.id == ADMIN_ROLE_ID for r in getattr(user, "roles", []))
    return app_commands.check(predicate)

# ---------------- parsing & formatting ----------------
def parse_items_from_text(text: str) -> List[str]:
    if not text:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines: List[str] = []
    for raw_line in text.split("\n"):
        if not raw_line:
            continue
        # support comma-separated lists inside a line
        if "," in raw_line and len(raw_line.strip()) > 0:
            for part in raw_line.split(","):
                s = part.strip()
                if s: lines.append(s)
        else:
            s = raw_line.strip()
            if s: lines.append(s)
    # dedupe preserving order
    seen = set()
    out = []
    for l in lines:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out

def get_category_emoji(category: str) -> Optional[str]:
    return (stock_data.get("category_emojis") or {}).get(category)

def format_stock_embed():
    d = stock_data or {}
    free_map = d.get("FREE", {}) or {}
    excl_map = d.get("EXCLUSIVE", {}) or {}
    master = list(d.get("categories", []) or [])
    emojis = d.get("category_emojis", {}) or {}

    free_cats = sorted(free_map.keys())
    excl_cats = sorted(excl_map.keys())
    assigned = set(free_cats) | set(excl_cats)
    unassigned = [c for c in sorted(master) if c not in assigned]

    embed = discord.Embed(title="📦 Stock Overview", color=discord.Color.blue())
    if free_cats:
        free_lines = []
        for cat in free_cats:
            cnt = len(free_map.get(cat, []))
            emoji = emojis.get(cat, "")
            display = f"{emoji} {cat}" if emoji else f"{cat}"
            free_lines.append(f"**{display}** → {cnt}")
        embed.add_field(name="🆓 Free Stock", value="\n".join(free_lines), inline=False)
    else:
        embed.add_field(name="🆓 Free Stock", value="No Free categories", inline=False)

    if excl_cats:
        excl_lines = []
        for cat in excl_cats:
            cnt = len(excl_map.get(cat, []))
            emoji = emojis.get(cat, "")
            display = f"{emoji} {cat}" if emoji else f"{cat}"
            excl_lines.append(f"**{display}** → {cnt}")
        embed.add_field(name="💎 Exclusive Stock", value="\n".join(excl_lines), inline=False)
    else:
        embed.add_field(name="💎 Exclusive Stock", value="No Exclusive categories", inline=False)

    if unassigned:
        embed.add_field(name="⚠️ Unassigned Categories", value=", ".join(unassigned), inline=False)

    embed.set_footer(text="Automated • Marcos Gen")
    return embed

# ---------------- boost loop (auto assign + auto-remove) ----------------
@tasks.loop(minutes=5)
async def boost_loop():
    # Load assigned_data fresh
    await load_assigned()
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    # Use guild.premium_subscription_count for total server boosts
    try:
        server_boosts = getattr(guild, "premium_subscription_count", None)
        if server_boosts is None:
            # fallback to attribute name variation
            server_boosts = getattr(guild, "premium_tier", 0) or 0
        server_boosts = int(server_boosts or 0)
    except Exception:
        server_boosts = 0

    boost_role = guild.get_role(BOOST_ROLE_ID)
    exclusive_role = guild.get_role(EXCLUSIVE_ROLE_ID)

    for member in guild.members:
        try:
            is_boosting = bool(getattr(member, "premium_since", None))
            # assign or remove boost role
            if is_boosting:
                if boost_role and boost_role not in member.roles:
                    try:
                        await member.add_roles(boost_role)
                        mark_boost_assigned(member.id)
                    except Exception:
                        pass
                # only auto-assign Exclusive role when server has at least 2 boosts
                if server_boosts >= 2:
                    if exclusive_role and exclusive_role not in member.roles:
                        try:
                            await member.add_roles(exclusive_role)
                            mark_exclusive_assigned(member.id)
                        except Exception:
                            pass
                # if server has <2 boosts, do not auto-assign exclusive (and do not remove if present)
            else:
                # not currently boosting -> remove boost role if bot assigned it
                if boost_role and boost_role in member.roles and is_boost_assigned(member.id):
                    try:
                        await member.remove_roles(boost_role)
                        unmark_boost_assigned(member.id)
                    except Exception:
                        pass
                # if this user was auto-assigned Exclusive by the bot, remove it now
                if exclusive_role and exclusive_role in member.roles and is_exclusive_assigned(member.id):
                    try:
                        await member.remove_roles(exclusive_role)
                        unmark_exclusive_assigned(member.id)
                    except Exception:
                        pass
        except Exception:
            continue

    # save assigned_data after loop
    await save_assigned()

# ---------------- autocomplete helpers ----------------
async def category_autocomplete(interaction: discord.Interaction, current: str):
    await safe_load_stock()
    cats = stock_data.get("categories", [])
    emojis = stock_data.get("category_emojis", {}) or {}
    choices = []
    cur = (current or "").lower()
    for c in cats:
        if cur and cur not in c.lower(): continue
        emoji = emojis.get(c, "")
        name = f"{emoji} {c}" if emoji else c
        choices.append(app_commands.Choice(name=name[:100], value=c))
        if len(choices) >= 25: break
    return choices

async def stock_type_autocomplete(interaction: discord.Interaction, current: str):
    opts = ["free", "exclusive"]
    return [app_commands.Choice(name=o.capitalize(), value=o) for o in opts if current.lower() in o.lower()][:25]

# ---------------- Gen UI ----------------
class GenSelect(discord.ui.Select):
    def __init__(self, typ: str):
        opts = []
        for cat in stock_data.get("categories", []):
            cnt = len(stock_data.get(typ, {}).get(cat, []))
            emoji = get_category_emoji(cat) or ""
            display = f"{emoji} {cat}" if emoji else cat
            label = f"{display} — {cnt}"
            opts.append(discord.SelectOption(label=label[:100], value=cat, description=f"{cnt} in stock" if cnt else "Out of stock"))
        super().__init__(placeholder="Choose a category", min_values=1, max_values=1, options=opts[:25])
        self.typ = typ

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await safe_load_stock()
        cat = self.values[0]
        items = stock_data.get(self.typ, {}).get(cat, [])
        if not items:
            await interaction.followup.send("⚠️ That category is out of stock.", ephemeral=True); return
        rem = check_cooldown(interaction.user.id, self.typ)
        if rem > 0:
            await interaction.followup.send(f"⏳ Please wait {rem}s before generating again.", ephemeral=True); return
        item = items.pop(0)
        await safe_save_stock()
        set_cooldown(interaction.user.id, self.typ)

        # send DM with item
        dm_ok = True
        try:
            await interaction.user.send(f"🎉 **Here is your item from {cat}:**\n```{item}```")
        except Exception:
            dm_ok = False

        if dm_ok:
            await interaction.followup.send("✅ Sent to your DMs.", ephemeral=True)
        else:
            await interaction.followup.send(("⚠️ Could not send DM. Enable DMs or accept direct messages.\n\n"
                                            f"Here is your item for now:\n```{item}```"), ephemeral=True)

class GenView(discord.ui.View):
    def __init__(self, typ: str):
        super().__init__(timeout=60)
        self.add_item(GenSelect(typ))

# ---------------- USER COMMANDS ----------------
@tree.command(name="gen", description="Generate a Free item", guild=GUILD_OBJ)
async def cmd_gen(interaction: discord.Interaction):
    await safe_load_stock()
    member_roles = [r.id for r in getattr(interaction.user, "roles", [])]
    if FREE_GEN_ROLE_ID not in member_roles:
        await interaction.response.send_message(("❌ Free Gen access requires the FreeGen role. Type `!freegenrole` to request it."), ephemeral=True); return
    await interaction.response.send_message("📦 Select a Free category:", view=GenView("FREE"), ephemeral=True)

@tree.command(name="exclusive-gen", description="Generate an Exclusive item", guild=GUILD_OBJ)
async def cmd_exclusive_gen(interaction: discord.Interaction):
    if EXCLUSIVE_ROLE_ID not in [r.id for r in getattr(interaction.user, "roles", [])]:
        await interaction.response.send_message("❌ You need the Exclusive role to use this command.", ephemeral=True); return
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
@app_commands.choices(scope=[
    app_commands.Choice(name="Free", value="free"),
    app_commands.Choice(name="Exclusive", value="exclusive"),
    app_commands.Choice(name="Both", value="both"),
])
async def cmd_addcategory(interaction: discord.Interaction, category: str, scope: app_commands.Choice[str], emoji: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)
    await safe_load_stock()
    category = category.strip()
    if not category:
        await interaction.followup.send("❌ Category cannot be empty.", ephemeral=True); return

    responses = []
    existed = category in stock_data.get("categories", [])
    if not existed:
        stock_data.setdefault("categories", []).append(category)
        responses.append(f"Added `{category}` to master categories list.")

    s = scope.value.lower()
    if s in ("free", "both"):
        if category not in stock_data.setdefault("FREE", {}):
            stock_data["FREE"][category] = []
            responses.append("Added to Free stock.")
        else:
            responses.append("Already present in Free stock.")
    if s in ("exclusive", "both"):
        if category not in stock_data.setdefault("EXCLUSIVE", {}):
            stock_data["EXCLUSIVE"][category] = []
            responses.append("Added to Exclusive stock.")
        else:
            responses.append("Already present in Exclusive stock.")

    if emoji:
        stock_data.setdefault("category_emojis", {})[category] = emoji.strip()
        responses.append("Set emoji for category.")

    await safe_save_stock()
    await interaction.followup.send("✅ " + " ".join(responses), ephemeral=True)

@tree.command(name="removecategory", description="Remove a category (Admin only)", guild=GUILD_OBJ)
@is_admin_check()
@app_commands.choices(scope=[
    app_commands.Choice(name="Free", value="free"),
    app_commands.Choice(name="Exclusive", value="exclusive"),
    app_commands.Choice(name="Both", value="both"),
])
async def cmd_removecategory(interaction: discord.Interaction, category: str, scope: app_commands.Choice[str], remove_emoji: Optional[bool] = False):
    await interaction.response.defer(ephemeral=True)
    await safe_load_stock()
    category = category.strip()
    if not category:
        await interaction.followup.send("❌ Category cannot be empty.", ephemeral=True); return

    s = scope.value.lower()
    msgs = []
    if s in ("free", "both"):
        if category in stock_data.get("FREE", {}):
            stock_data["FREE"].pop(category, None)
            msgs.append("Removed from Free stock.")
        else:
            msgs.append("Not found in Free stock.")
    if s in ("exclusive", "both"):
        if category in stock_data.get("EXCLUSIVE", {}):
            stock_data["EXCLUSIVE"].pop(category, None)
            msgs.append("Removed from Exclusive stock.")
        else:
            msgs.append("Not found in Exclusive stock.")
    if s == "both":
        if category in stock_data.get("categories", []):
            stock_data["categories"].remove(category)
            msgs.append("Removed from master categories list.")
    if remove_emoji:
        if category in stock_data.get("category_emojis", {}):
            stock_data["category_emojis"].pop(category, None)
            msgs.append("Removed emoji for category.")
        else:
            msgs.append("No emoji set for that category.")
    await safe_save_stock()
    await interaction.followup.send("✅ " + " ".join(msgs), ephemeral=True)

@tree.command(name="setcategoryemoji", description="Set or remove emoji for a category", guild=GUILD_OBJ)
@is_admin_check()
@app_commands.autocomplete(category=category_autocomplete)
async def cmd_setcategoryemoji(interaction: discord.Interaction, category: str, emoji: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)
    await safe_load_stock()
    if category not in stock_data.get("categories", []):
        await interaction.followup.send("❌ Invalid category.", ephemeral=True); return

    current = stock_data.get("category_emojis", {}).get(category)
    new_emoji = emoji.strip() if emoji else None

    preview_embed = discord.Embed(title="🎯 Set Category Emoji", color=discord.Color.blurple())
    preview_embed.add_field(name="Category", value=f"`{category}`", inline=False)
    preview_embed.add_field(name="Current", value=current or "(none)", inline=True)
    preview_embed.add_field(name="New", value=new_emoji or "(will remove emoji)", inline=True)
    preview_embed.set_footer(text="Confirm to apply the change.")

    class EmojiConfirmView(discord.ui.View):
        def __init__(self, author_id:int, timeout:int=120):
            super().__init__(timeout=timeout)
            self.author_id = author_id

        async def interaction_check(self, inter: discord.Interaction) -> bool:
            if inter.user.id != self.author_id:
                await inter.response.send_message("❌ Only the invoking admin may confirm.", ephemeral=True); return False
            return True

        @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
        async def confirm(self, button: discord.ui.Button, inter: discord.Interaction):
            try:
                if new_emoji:
                    stock_data.setdefault("category_emojis", {})[category] = new_emoji
                else:
                    stock_data.setdefault("category_emojis", {}).pop(category, None)
                await safe_save_stock()
                await inter.response.edit_message(content=f"✅ Emoji for `{category}` updated.", embed=None, view=None)
            except Exception as e:
                await inter.response.edit_message(content=f"❌ Failed to save emoji: {e}", embed=None, view=None)
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
        async def cancel(self, button: discord.ui.Button, inter: discord.Interaction):
            await inter.response.edit_message(content="❌ Operation canceled.", embed=None, view=None)
            self.stop()

    view = EmojiConfirmView(author_id=interaction.user.id)
    await interaction.followup.send(embed=preview_embed, view=view, ephemeral=True)

@tree.command(name="getcategoryemoji", description="Get stored emoji for a category", guild=GUILD_OBJ)
@is_admin_check()
@app_commands.autocomplete(category=category_autocomplete)
async def cmd_getcategoryemoji(interaction: discord.Interaction, category: str):
    await interaction.response.defer(ephemeral=True)
    await safe_load_stock()
    if category not in stock_data.get("categories", []):
        await interaction.followup.send("❌ Invalid category.", ephemeral=True); return
    current = stock_data.get("category_emojis", {}).get(category)
    await interaction.followup.send(f"📌 Emoji for `{category}`: {current or '(none)'}", ephemeral=True)

# ---------------- categories UI ----------------
class CategorySelect(discord.ui.Select):
    def __init__(self):
        options = []
        for c in stock_data.get("categories", []):
            emoji = get_category_emoji(c) or ""
            label = f"{emoji} {c}" if emoji else c
            desc = f"Free: {len(stock_data.get('FREE', {}).get(c, []))} • Excl: {len(stock_data.get('EXCLUSIVE', {}).get(c, []))}"
            options.append(discord.SelectOption(label=label[:100], value=c, description=desc[:100]))
        super().__init__(placeholder="Select a category to manage", min_values=1, max_values=1, options=options[:25])

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        await safe_load_stock()
        cat = self.values[0]
        emoji = stock_data.get("category_emojis", {}).get(cat, "(none)")
        free_count = len(stock_data.get("FREE", {}).get(cat, []))
        excl_count = len(stock_data.get("EXCLUSIVE", {}).get(cat, []))
        embed = discord.Embed(title=f"🧾 Category: {cat}", color=discord.Color.green())
        embed.add_field(name="Emoji", value=emoji or "(none)", inline=True)
        embed.add_field(name="Free items", value=str(free_count), inline=True)
        embed.add_field(name="Exclusive items", value=str(excl_count), inline=True)
        embed.set_footer(text="Use the buttons to edit the emoji or manage stock.")
        class CatManageView(discord.ui.View):
            def __init__(self, author_id:int, category_name:str, timeout:int=120):
                super().__init__(timeout=timeout)
                self.author_id = author_id
                self.category_name = category_name

            async def interaction_check(self, inter: discord.Interaction) -> bool:
                if inter.user.id != self.author_id:
                    await inter.response.send_message("❌ Only the invoker can manage this category.", ephemeral=True); return False
                return True

            @discord.ui.button(label="Set Emoji", style=discord.ButtonStyle.primary)
            async def set_emoji(self, button: discord.ui.Button, inter: discord.Interaction):
                class EmojiModal(discord.ui.Modal, title=f"Set emoji for {self.category_name}"):
                    emoji_input = discord.ui.TextInput(label="Emoji", placeholder="e.g. <:xbox:123> or 🎮", required=True, max_length=100)
                    async def on_submit(self, modal_inter: discord.Interaction):
                        await safe_load_stock()
                        val = self.emoji_input.value.strip()
                        stock_data.setdefault("category_emojis", {})[self.category_name] = val
                        await safe_save_stock()
                        try:
                            await modal_inter.response.send_message(f"✅ Emoji for `{self.category_name}` set.", ephemeral=True)
                        except Exception:
                            pass
                await inter.response.send_modal(EmojiModal())

            @discord.ui.button(label="Remove Emoji", style=discord.ButtonStyle.danger)
            async def remove_emoji(self, button: discord.ui.Button, inter: discord.Interaction):
                await safe_load_stock()
                if self.category_name in stock_data.get("category_emojis", {}):
                    stock_data["category_emojis"].pop(self.category_name, None)
                    await safe_save_stock()
                    await inter.response.send_message(f"✅ Emoji removed for `{self.category_name}`.", ephemeral=True)
                else:
                    await inter.response.send_message("ℹ️ No emoji set for this category.", ephemeral=True)

            @discord.ui.button(label="Close", style=discord.ButtonStyle.secondary)
            async def close(self, button: discord.ui.Button, inter: discord.Interaction):
                await inter.response.send_message("Closed.", ephemeral=True)

        view = CatManageView(author_id=interaction.user.id, category_name=cat)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

class CategoriesView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(CategorySelect())

@tree.command(name="categories", description="Open category manager (Admin only)", guild=GUILD_OBJ)
@is_admin_check()
async def cmd_categories(interaction: discord.Interaction):
    await safe_load_stock()
    if not stock_data.get("categories"):
        await interaction.response.send_message("No categories configured.", ephemeral=True); return
    await interaction.response.send_message("Select a category to manage:", view=CategoriesView(), ephemeral=True)

# ---------------- stock management ----------------
@tree.command(name="addstock", description="Add stock items (Admin only)", guild=GUILD_OBJ)
@is_admin_check()
@app_commands.autocomplete(stock_type=stock_type_autocomplete, category=category_autocomplete)
async def cmd_addstock(interaction: discord.Interaction, stock_type: str, category: str, items: Optional[str] = None, file: Optional[discord.Attachment] = None):
    await interaction.response.defer(ephemeral=True)
    t = stock_type.lower()
    if t not in ("free", "exclusive"):
        await interaction.followup.send("❌ stock_type must be `free` or `exclusive`.", ephemeral=True); return
    key = "FREE" if t == "free" else "EXCLUSIVE"
    await safe_load_stock()
    if category not in stock_data.get("categories", []):
        await interaction.followup.send("❌ Invalid category. Create it with /addcategory.", ephemeral=True); return

    new_items: List[str] = []
    exist_set = set(stock_data.get(key, {}).get(category, []))

    if file:
        try:
            raw = await file.read()
            text = raw.decode(errors="ignore")
            lines = parse_items_from_text(text)
        except Exception:
            await interaction.followup.send("❌ Could not read attached file.", ephemeral=True); return
        for line in lines:
            if line not in exist_set and line not in new_items: new_items.append(line)
    elif items:
        lines = parse_items_from_text(items)
        for line in lines:
            if line not in exist_set and line not in new_items: new_items.append(line)
    else:
        await interaction.followup.send("❌ Provide items in `items` or attach a .txt file.", ephemeral=True); return

    stock_data.setdefault(key, {}).setdefault(category, []).extend(new_items)
    await safe_save_stock()
    await interaction.followup.send(f"✅ Added {len(new_items)} item(s) to `{category}`.", ephemeral=True)

    # restock ping format:
    # <@&role_id> 🔔 Category was restocked — N new items.
    try:
        restock_channel = bot.get_channel(RESTOCK_CHANNEL_ID) or (interaction.guild.get_channel(RESTOCK_CHANNEL_ID) if interaction.guild else None)
        role_id = FREE_GEN_ROLE_ID if key == "FREE" else EXCLUSIVE_ROLE_ID
        emoji = stock_data.get("category_emojis", {}).get(category, "")
        prefix = f"{emoji} " if emoji else ""
        if restock_channel and new_items:
            await restock_channel.send(f"{prefix}<@&{role_id}> 🔔 {category} was restocked — {len(new_items)} new items.")
    except Exception:
        pass

@tree.command(name="removestock", description="Remove stock items (Admin only)", guild=GUILD_OBJ)
@is_admin_check()
@app_commands.autocomplete(stock_type=stock_type_autocomplete, category=category_autocomplete)
async def cmd_removestock(interaction: discord.Interaction, stock_type: str, category: str, items: Optional[str] = None, file: Optional[discord.Attachment] = None):
    await interaction.response.defer(ephemeral=True)
    t = stock_type.lower()
    if t not in ("free", "exclusive"):
        await interaction.followup.send("❌ stock_type must be `free` or `exclusive`.", ephemeral=True); return
    key = "FREE" if t == "free" else "EXCLUSIVE"
    await safe_load_stock()
    if category not in stock_data.get("categories", []):
        await interaction.followup.send("❌ Invalid category.", ephemeral=True); return

    removed = 0
    if file:
        try:
            raw = await file.read()
            text = raw.decode(errors="ignore")
            lines = parse_items_from_text(text)
        except Exception:
            await interaction.followup.send("❌ Could not read attached file.", ephemeral=True); return
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
        await interaction.followup.send("❌ Provide items to remove or attach a .txt file.", ephemeral=True); return

    await safe_save_stock()
    await interaction.followup.send(f"✅ Removed {removed} item(s) from `{category}`.", ephemeral=True)

@tree.command(name="restock", description="Replace stock with new items (Admin only)", guild=GUILD_OBJ)
@is_admin_check()
@app_commands.autocomplete(stock_type=stock_type_autocomplete, category=category_autocomplete)
async def cmd_restock(interaction: discord.Interaction, stock_type: str, category: str, items: Optional[str] = None, file: Optional[discord.Attachment] = None):
    await interaction.response.defer(ephemeral=True)
    t = stock_type.lower()
    if t not in ("free", "exclusive"):
        await interaction.followup.send("❌ stock_type must be `free` or `exclusive`.", ephemeral=True); return
    key = "FREE" if t == "free" else "EXCLUSIVE"
    await safe_load_stock()
    if category not in stock_data.get("categories", []):
        await interaction.followup.send("❌ Invalid category.", ephemeral=True); return

    new_items: List[str] = []
    if file:
        try:
            raw = await file.read()
            text = raw.decode(errors="ignore")
            lines = parse_items_from_text(text)
            new_items = list(dict.fromkeys(lines))
        except Exception:
            await interaction.followup.send("❌ Could not read attached file.", ephemeral=True); return
    elif items:
        lines = parse_items_from_text(items)
        new_items = list(dict.fromkeys(lines))
    else:
        await interaction.followup.send("❌ Provide items or attach a .txt file.", ephemeral=True); return

    preview_embed = discord.Embed(title="🔁 Restock Preview", description=f"Replace `{category}` in **{key}** with {len(new_items)} item(s).", color=discord.Color.green())
    sample = new_items[:10]
    if sample:
        preview_embed.add_field(name="Sample (first 10)", value="\n".join(f"`{s}`" for s in sample), inline=False)
    else:
        preview_embed.add_field(name="Sample", value="(no items)", inline=False)
    preview_embed.set_footer(text="Confirm to overwrite existing stock for this category.")

    class RestockConfirmView(discord.ui.View):
        def __init__(self, author_id: int, timeout: int = 300):
            super().__init__(timeout=timeout)
            self.author_id = author_id

        async def interaction_check(self, inter: discord.Interaction) -> bool:
            if inter.user.id != self.author_id:
                await inter.response.send_message("❌ Only the invoking admin can confirm/cancel.", ephemeral=True); return False
            return True

        @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
        async def confirm(self, button: discord.ui.Button, inter: discord.Interaction):
            try:
                stock_data.setdefault(key, {})[category] = new_items
                await safe_save_stock()
            except Exception as e:
                await inter.response.send_message(f"❌ Failed to save new stock: {e}", ephemeral=True); self.stop(); return

            try:
                restock_channel = bot.get_channel(RESTOCK_CHANNEL_ID) or (inter.guild.get_channel(RESTOCK_CHANNEL_ID) if inter.guild else None)
                role_id = FREE_GEN_ROLE_ID if key == "FREE" else EXCLUSIVE_ROLE_ID
                emoji = stock_data.get("category_emojis", {}).get(category, "")
                prefix = f"{emoji} " if emoji else ""
                if restock_channel:
                    await restock_channel.send(f"{prefix}<@&{role_id}> 🔔 {category} was restocked — {len(new_items)} new items.")
            except Exception:
                pass

            await inter.response.edit_message(content=f"✅ `{category}` restocked with {len(new_items)} item(s).", embed=None, view=None)
            self.stop()

        @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
        async def cancel(self, button: discord.ui.Button, inter: discord.Interaction):
            await inter.response.edit_message(content="❌ Restock cancelled.", embed=None, view=None)
            self.stop()

    view = RestockConfirmView(author_id=interaction.user.id)
    await interaction.followup.send(embed=preview_embed, view=view, ephemeral=True)

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
@tree.command(name="resync-commands", description="Register/sync commands to the guild (Admin only)", guild=GUILD_OBJ)
@is_admin_check()
async def cmd_resync(interaction: discord.Interaction):
    global _last_resync_ts
    await interaction.response.defer(ephemeral=True)
    now = now_ts()
    if now - _last_resync_ts < RESYNC_COOLDOWN:
        await interaction.followup.send("❌ Commands were resynced recently. Wait before running again.", ephemeral=True); return
    try:
        guild_obj = bot.get_guild(GUILD_ID)
        if not guild_obj:
            await interaction.followup.send("❌ Bot is not in the configured guild.", ephemeral=True); return
        synced = await tree.sync(guild=guild_obj)
        _last_resync_ts = now_ts()
        await interaction.followup.send(f"✅ Commands synced to guild ({len(synced)} commands).", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ Missing access when syncing commands.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Sync failed: {e}", ephemeral=True)

# ---------------- global app command error ----------------
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

# ---------------- on_ready ----------------
@bot.event
async def on_ready():
    # ensure assigned data loaded
    await load_assigned()

    try:
        if not boost_loop.is_running(): boost_loop.start()
    except Exception as e:
        print(f"[BOOST LOOP ERROR] {e}")

    print(f"✅ Logged in as {bot.user} (id: {bot.user.id})")

    # Optional HTTP clear (disabled by default). Set DO_HTTP_CLEAR=1 only if you know what you're doing.
    if os.getenv("DO_HTTP_CLEAR", "0") == "1":
        TOKEN = os.getenv("TOKEN")
        APP_ID = APPLICATION_ID
        if TOKEN and APP_ID:
            url = f"https://discord.com/api/v10/applications/{APP_ID}/guilds/{GUILD_ID}/commands"
            try:
                async with aiohttp.ClientSession() as session:
                    headers = {"Authorization": f"Bot {TOKEN}", "Content-Type": "application/json"}
                    async with session.put(url, json=[], headers=headers, timeout=20) as resp:
                        text = await resp.text()
                        print(f"[HTTP CLEAR] status {resp.status} body: {text[:400]}")
            except Exception as e:
                print(f"[HTTP CLEAR ERROR] {e}")
        else:
            print("[HTTP CLEAR] TOKEN or APP_ID missing; skipping.")

    # Sync to guild (scoped) and print results
    try:
        guild = bot.get_guild(GUILD_ID)
        if guild is None:
            print(f"[SYNC ERROR] Bot not in configured guild ({GUILD_ID}); skipping guild sync.")
        else:
            synced = await tree.sync(guild=guild)
            print(f"✅ Commands synced to guild ({len(synced)} commands).")
    except Exception as e:
        print(f"[SYNC ERROR] {e}")

    if SYNC_ON_START:
        try:
            all_synced = await tree.sync()
            print(f"[SYNC_ON_START] global sync: {len(all_synced)} commands")
        except Exception as e:
            print(f"[SYNC_ON_START ERROR] {e}")

# ---------------- on_message ----------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.webhook_id or message.type != discord.MessageType.default:
        await bot.process_commands(message); return

    # TEXT command: !freegenrole -> request FreeGen; immediate role grant
    if message.content and message.content.strip().lower() == "!freegenrole":
        try: await message.delete()
        except Exception: pass
        user_id = message.author.id
        guild = bot.get_guild(GUILD_ID)
        member = None
        if guild:
            member = guild.get_member(user_id)
            if not member:
                try: member = await guild.fetch_member(user_id)
                except Exception: member = None
        if member and any(r.id == FREE_GEN_ROLE_ID for r in member.roles):
            try: await message.author.send("ℹ️ You already have the Free Gen role. Enjoy the generator!")
            except Exception: pass
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
        try:
            if granted:
                await message.author.send("🎉 You have been granted Free Gen access. Enjoy the generator!")
            else:
                await message.author.send("⚠️ Could not grant Free Gen role automatically. Check bot permissions.")
        except Exception:
            pass
        return

    if message.content and message.content.startswith("/"):
        await bot.process_commands(message); return

    if message.channel.id in AUTODELETE_CHANNELS:
        try: await message.delete()
        except Exception: pass
        return

    await bot.process_commands(message)

# ---------------- RUN ----------------
if __name__ == "__main__":
    TOKEN = os.getenv("TOKEN")
    if not TOKEN:
        print("[ERROR] TOKEN env var not set. Set TOKEN in Railway env variables.")
    else:
        _ensure_stock_file()
        _ensure_assigned_file()
        stock_data = _load_stock_from_disk()
        assigned_data = _load_assigned_from_disk()
        bot.run(TOKEN)
