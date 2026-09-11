"""
============================================================
 CLIENT PORTAL BOT  —  token tickets, threads, feedback,
 clock in/out (Bali/WITA time), overtime alerts, client roles
============================================================
 Requires: discord.py 2.x  (pip install -U discord.py)

 Env vars:
   DISCORD_TOKEN   -> your bot token
   ADMIN_ID        -> YOUR discord user id (only you can manage)
   GUILD_ID        -> your server id

 Remember to enable the "Server Members Intent" and
 "Message Content Intent" in the Developer Portal!
============================================================
"""

import os
import json
import secrets
import aiohttp
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

import discord
from discord.ext import commands, tasks
from discord import app_commands

# ----------------------------- CONFIG -----------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_ID      = int(os.getenv("ADMIN_ID", "0"))
GUILD_ID      = int(os.getenv("GUILD_ID", "0"))
OLLAMA_URL    = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL  = os.getenv("OLLAMA_MODEL", "gemma2")

WITA = ZoneInfo("Asia/Makassar")            # Bali time = UTC+8
CLOCK_IN_HOUR  = 8                          # earliest clock-in  (WITA)
CLOCK_OUT_HOUR = 18                         # scheduled clock-out (WITA)
DEFAULT_REOPEN_MINUTES = 30
MAX_REOPEN_MINUTES     = 180

DATA_FILE = "bot_data.json"

# ----------------------------- STORAGE -----------------------------
DEFAULT_DATA = {
    "clients":   {},   # name -> {name, logo, role_id}
    "tokens":    {},   # TOKEN  -> {client, content, attachment, thread_id, user_id, used, created_at}
    "threads":   {},   # thread_id -> {client, user_id, closed, reopen_until}
    "feedback":  [],   # [{thread_id, client, author, content, at}]
    "clock_state": {}, # user_id -> {in, overtime_prompted, overtime_confirmed}
    "clock_entries": [],  # [{user_id, date, in, out, minutes, overtime}]
    "deliver_channel_id": None,
    "authorized_timesheet_roles": [], # list of role IDs
}

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        for k, v in DEFAULT_DATA.items():
            d.setdefault(k, v)
        return d
    return json.loads(json.dumps(DEFAULT_DATA))

def save_data():
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, DATA_FILE)

data = load_data()

# ----------------------------- BOT SETUP -----------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def wita_now() -> datetime:
    return datetime.now(WITA)

def fmt_duration(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m"

async def get_deliver_channel(guild: discord.Guild) -> discord.TextChannel:
    """Find or create the hidden channel that hosts all client threads."""
    ch = guild.get_channel(data["deliver_channel_id"]) if data["deliver_channel_id"] else None
    if ch is None:
        cat = discord.utils.get(guild.categories, name="CLIENT PORTAL")
        if cat is None:
            cat = await guild.create_category("CLIENT PORTAL")
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            guild.me: discord.PermissionOverwrite(view_channel=True, manage_channels=True),
        }
        ch = await guild.create_text_channel("deliveries", category=cat, overwrites=overwrites)
        data["deliver_channel_id"] = ch.id
        save_data()
    return ch

def thread_of(interaction: discord.Interaction):
    tid = str(interaction.channel.id)
    return data["threads"].get(tid)

# ----------------------------- OVERTIME VIEW -----------------------------
class OvertimeView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    @discord.ui.button(label="✅ Confirm Overtime", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This prompt is not for you.", ephemeral=True)
        st = data["clock_state"].get(str(self.user_id))
        if st:
            st["overtime_confirmed"] = True
            save_data()
        await interaction.response.edit_message(
            content="⏰ Overtime confirmed — your clock keeps running. Use `/clockout` when done!", view=None)

    @discord.ui.button(label="🕕 Clock Out Now", style=discord.ButtonStyle.danger)
    async def checkout(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This prompt is not for you.", ephemeral=True)
        entry = do_clockout(self.user_id)
        if entry:
            await interaction.response.edit_message(
                content=f"🕕 Clocked out at **{entry['out']}** WITA — worked **{fmt_duration(entry['minutes'])}**"
                        + (" (overtime)" if entry["overtime"] else ""), view=None)
        else:
            await interaction.response.edit_message(content="You were not clocked in.", view=None)

def do_clockout(user_id: int) -> dict | None:
    st = data["clock_state"].pop(str(user_id), None)
    if not st:
        return None
    now = wita_now()
    t_in = datetime.fromisoformat(st["in"])
    minutes = max(0, int((now - t_in).total_seconds() // 60))
    entry = {
        "user_id": user_id,
        "date": now.strftime("%Y-%m-%d"),
        "in": t_in.strftime("%H:%M"),
        "out": now.strftime("%H:%M"),
        "minutes": minutes,
        "overtime": bool(st.get("overtime_confirmed")) or now.hour >= CLOCK_OUT_HOUR,
    }
    data["clock_entries"].append(entry)
    save_data()
    return entry

# ----------------------------- BACKGROUND LOOPS -----------------------------
@tasks.loop(minutes=1)
async def overtime_watcher():
    """Ping anyone still clocked-in past 18:00 WITA for overtime confirmation."""
    now = wita_now()
    if (now.hour, now.minute) < (CLOCK_OUT_HOUR, 0):
        return
    for uid_str, st in list(data["clock_state"].items()):
        if st.get("overtime_prompted"):
            continue
        st["overtime_prompted"] = True
        save_data()
        user = bot.get_user(int(uid_str)) or await bot.fetch_user(int(uid_str))
        try:
            await user.send(
                f"⏰ **Overtime confirmation**\nIt's **{now.strftime('%H:%M')} WITA** and you are still clocked in "
                f"(since {datetime.fromisoformat(st['in']).strftime('%H:%M')}).\n"
                f"Are you working overtime, or clocking out?",
                view=OvertimeView(int(uid_str)))
        except discord.Forbidden:
            print(f"[overtime] could not DM user {uid_str}")

@tasks.loop(seconds=30)
async def reopen_watcher():
    """Auto re-close threads that were temporarily reopened."""
    now = wita_now()
    for tid_str, th in list(data["threads"].items()):
        until = th.get("reopen_until")
        if not until or th.get("closed"):
            continue
        if datetime.fromisoformat(until) <= now:
            th["closed"] = True
            th["reopen_until"] = None
            save_data()
            channel = bot.get_channel(int(tid_str))
            if isinstance(channel, discord.Thread):
                try:
                    await channel.send("🔒 Temporary access expired — closing this thread again.")
                    await channel.edit(locked=True, archived=True)
                except discord.HTTPException:
                    pass

# ----------------------------- EVENTS -----------------------------
@bot.event
async def on_ready():
    guild = bot.get_guild(GUILD_ID)
    if guild:
        bot.tree.copy_global_to(guild=discord.Object(id=GUILD_ID))
        await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    overtime_watcher.start()
    reopen_watcher.start()
    print(f"[OK] Logged in as {bot.user} — WITA time: {wita_now().strftime('%Y-%m-%d %H:%M')}")

@bot.event
async def on_member_join(member: discord.Member):
    try:
        await member.send(
            f"👋 Welcome to **{member.guild.name}**!\n\n"
            f"You need an access token to receive your private content.\n"
            f"👉 **Just reply here with the token** your provider gave you.")
    except discord.Forbidden:
        pass

@bot.event
async def on_message(message: discord.Message):
    # ---- TOKEN REDEMPTION (works in DM or any channel) ----
    if message.author.bot:
        return
    token = message.content.strip().upper()
    tinfo = data["tokens"].get(token)
    if not tinfo or tinfo.get("used"):
        await bot.process_commands(message)
        return

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return
    member = guild.get_member(message.author.id) or await guild.fetch_member(message.author.id)

    tinfo["used"] = True
    tinfo["user_id"] = member.id
    client = data["clients"][tinfo["client"]]

    # role per client
    role = guild.get_role(client["role_id"])
    if role is None:
        role = await guild.create_role(name=f"Client · {client['name']}", mentionable=True)
        client["role_id"] = role.id
    await member.add_roles(role, reason="Token redeemed")

    if tinfo.get("thread_id"):
        # ===== TOKEN UNLOCKS AN EXISTING PRIVATE THREAD =====
        thread = guild.get_thread(int(tinfo["thread_id"]))
        if thread is None:
            tinfo["used"] = False  # refund the token
            save_data()
            try:
                await message.author.send("❌ This token's thread no longer exists. Please contact the admin.")
            except discord.Forbidden:
                pass
            return
        await thread.add_user(member)
        try:
            await thread.send(f"🔓 {member.mention} joined via access token `{token}`.")
        except discord.HTTPException:
            pass
    else:
        # ===== TOKEN CREATES A FRESH PRIVATE THREAD WITH THEIR CONTENT =====
        channel = await get_deliver_channel(guild)
        thread = await channel.create_thread(
            name=f"{client['name']} · {member.name}",
            type=discord.ChannelType.private_thread,
            invitable=False)
        await thread.add_user(member)

        embed = discord.Embed(
            title=f"📦 Delivery — {client['name']}",
            description=tinfo.get("content") or "Your content is attached below.",
            color=discord.Color.blurple(),
            timestamp=wita_now())
        if client.get("logo"):
            embed.set_thumbnail(url=client["logo"])
        embed.set_footer(text=f"Token {token} · redeemed by {member}")
        await thread.send(content=member.mention, embed=embed)
        if tinfo.get("attachment"):
            await thread.send(tinfo["attachment"])

        data["threads"][str(thread.id)] = {
            "client": client["name"], "user_id": member.id,
            "closed": False, "reopen_until": None}
    tinfo["thread_id"] = thread.id
    save_data()

    try:
        await message.author.send(f"✅ Token accepted! Your private thread is ready: {thread.jump_url}")
    except discord.Forbidden:
        pass
    await message.add_reaction("✅")

@bot.event
async def on_thread_remove(thread: discord.Thread):
    data["threads"].pop(str(thread.id), None)
    save_data()

# ----------------------------- ADMIN: UTILITY -----------------------------
@bot.tree.command(name="setnickname", description="[Admin] Change any member's nickname")
@app_commands.describe(member="The member to change", nickname="The new nickname")
async def setnickname(interaction: discord.Interaction, member: discord.Member, nickname: str):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("⛔ Admin only.", ephemeral=True)
    try:
        await member.edit(nick=nickname)
        await interaction.response.send_message(f"✅ Changed nickname of {member.mention} to **{nickname}**.")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to change this user's nickname. Check my role position in the server settings.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="resetnickname", description="[Admin] Reset a member's nickname to default")
@app_commands.describe(member="The member to reset")
async def resetnickname(interaction: discord.Interaction, member: discord.Member):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("⛔ Admin only.", ephemeral=True)
    try:
        await member.edit(nick=None)
        await interaction.response.send_message(f"✅ Reset nickname for {member.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to reset this user's nickname.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="purge", description="[Admin] Delete a specified amount of messages")
@app_commands.describe(amount="Number of messages to delete")
async def purge(interaction: discord.Interaction, amount: int):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("⛔ Admin only.", ephemeral=True)

    if amount < 1:
        return await interaction.response.send_message("Amount must be at least 1.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    try:
        deleted = await interaction.channel.purge(limit=amount)
        await interaction.followup.send(f"🗑️ Deleted {len(deleted)} messages.", ephemeral=True)
    except discord.Forbidden:
        await interaction.followup.send("❌ I don't have permission to delete messages here.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {str(e)}", ephemeral=True)

# ----------------------------- ADMIN: CLIENTS -----------------------------
@bot.tree.command(name="addclient", description="[Admin] Register a client (creates their role)")
@app_commands.describe(name="Client name/label (e.g. A, B, Acme Corp)", logo="Logo image URL (optional)")
async def addclient(interaction: discord.Interaction, name: str, logo: str = None):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("⛔ Admin only.", ephemeral=True)
    name = name.strip()
    if name.lower() in (k.lower() for k in data["clients"]):
        return await interaction.response.send_message(f"⚠️ Client **{name}** already exists.", ephemeral=True)
    guild = interaction.guild
    role = discord.utils.get(guild.roles, name=f"Client · {name}") or await guild.create_role(name=f"Client · {name}", mentionable=True)
    data["clients"][name] = {"name": name, "logo": logo, "role_id": role.id}
    save_data()
    embed = discord.Embed(title=f"✅ Client added: {name}", color=discord.Color.green())
    if logo:
        embed.set_thumbnail(url=logo)
    embed.add_field(name="Role", value=role.mention)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="clients", description="List all registered clients")
async def clients(interaction: discord.Interaction):
    if not data["clients"]:
        return await interaction.response.send_message("No clients yet. Use `/addclient`.", ephemeral=True)
    embed = discord.Embed(title="📋 Clients", color=discord.Color.blurple())
    for c in data["clients"].values():
        embed.add_field(name=c["name"], value=f"Role: <@&{c['role_id']}>", inline=True)
        if c.get("logo"):
            embed.set_thumbnail(url=c["logo"])
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="removeclient", description="[Admin] Remove a client")
async def removeclient(interaction: discord.Interaction, name: str):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("⛔ Admin only.", ephemeral=True)
    if name not in data["clients"]:
        return await interaction.response.send_message("Client not found.", ephemeral=True)
    data["clients"].pop(name)
    save_data()
    await interaction.response.send_message(f"🗑️ Client **{name}** removed.", ephemeral=True)

# ----------------------------- ADMIN: TOKENS -----------------------------
@bot.tree.command(name="gentoken", description="[Admin] Generate an access token for a client")
@app_commands.describe(client="Client name", content="The content/ideas to deliver in their thread",
                       attachment="Optional file URL to attach",
                       thread_id="Optional: ID of an EXISTING private thread this token should unlock (right-click thread -> Copy Thread ID)")
async def gentoken(interaction: discord.Interaction, client: str, content: str = None,
                   attachment: str = None, thread_id: str = None):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("⛔ Admin only.", ephemeral=True)
    if client not in data["clients"]:
        return await interaction.response.send_message(f"⚠️ Unknown client. Registered: {', '.join(data['clients']) or 'none'}", ephemeral=True)
    linked_thread = None
    if thread_id:
        try:
            linked_thread = interaction.guild.get_thread(int(thread_id))
        except ValueError:
            linked_thread = None
        if linked_thread is None:
            return await interaction.response.send_message(
                "⚠️ Thread not found. Open the thread in this server, enable Developer Mode, "
                "right-click it -> **Copy Thread ID**.", ephemeral=True)
    token = secrets.token_hex(4).upper()  # e.g. 9F3A1C7B
    data["tokens"][token] = {
        "client": client, "content": content, "attachment": attachment,
        "thread_id": linked_thread.id if linked_thread else None,
        "user_id": None, "used": False,
        "created_at": wita_now().isoformat()}
    save_data()
    mode = f"unlocks existing thread `{linked_thread.name}`" if linked_thread else "creates a new private thread"
    await interaction.response.send_message(
        f"🔑 **Token for `{client}`**: `{token}` ({mode})\n"
        f"Send this to your client with your server invite link. It works once, in DM or in the server.",
        ephemeral=True)

@bot.tree.command(name="revoketoken", description="[Admin] Revoke a token")
async def revoketoken(interaction: discord.Interaction, token: str):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("⛔ Admin only.", ephemeral=True)
    t = token.strip().upper()
    if data["tokens"].pop(t, None):
        save_data()
        return await interaction.response.send_message(f"🗑️ Token `{t}` revoked.", ephemeral=True)
    await interaction.response.send_message("Token not found.", ephemeral=True)

# ----------------------------- THREAD CONTROL -----------------------------
def thread_permission(interaction) -> bool:
    th = thread_of(interaction)
    return th and (is_admin(interaction.user.id) or interaction.user.id == th["user_id"])

@bot.tree.command(name="close", description="Close your delivery thread (client or admin)")
async def close(interaction: discord.Interaction):
    th = thread_of(interaction)
    if not th or not isinstance(interaction.channel, discord.Thread):
        return await interaction.response.send_message("⚠️ Use this inside a delivery thread.", ephemeral=True)
    if not thread_permission(interaction):
        return await interaction.response.send_message("⛔ Only the thread owner or admin.", ephemeral=True)
    th["closed"] = True
    th["reopen_until"] = None
    save_data()
    await interaction.response.send_message("🔒 Closing this thread. Message the server (or admin) to reopen temporarily.")
    await interaction.channel.edit(locked=True, archived=True)

@bot.tree.command(name="open", description="Temporarily reopen a closed delivery thread")
@app_commands.describe(minutes="How long to stay open (default 30, max 180)")
async def open_thread(interaction: discord.Interaction, minutes: int = DEFAULT_REOPEN_MINUTES):
    th = thread_of(interaction)
    if not th or not isinstance(interaction.channel, discord.Thread):
        return await interaction.response.send_message("⚠️ Use this inside a delivery thread.", ephemeral=True)
    if not thread_permission(interaction):
        return await interaction.response.send_message("⛔ Only the thread owner or admin.", ephemeral=True)
    minutes = max(1, min(minutes, MAX_REOPEN_MINUTES))
    until = wita_now() + timedelta(minutes=minutes)
    th["closed"] = False
    th["reopen_until"] = until.isoformat()
    save_data()
    await interaction.response.send_message(f"🔓 Reopened for **{minutes} minutes** (until {until.strftime('%H:%M')} WITA).")
    await interaction.channel.edit(archived=False, locked=False)

# ----------------------------- FEEDBACK -----------------------------
def log_feedback(thread_id: str, client: str, author, content: str):
    data["feedback"].append({
        "thread_id": int(thread_id), "client": client,
        "author": f"{author} ({author.id})", "content": content,
        "at": wita_now().isoformat()})
    save_data()

@bot.tree.command(name="feedback", description="Submit formal feedback in your delivery thread")
@app_commands.describe(message="Your feedback")
async def feedback(interaction: discord.Interaction, message: str):
    th = thread_of(interaction)
    if not th:
        return await interaction.response.send_message("⚠️ Use this inside your delivery thread.", ephemeral=True)
    log_feedback(str(interaction.channel.id), th["client"], interaction.user, message)
    await interaction.response.send_message("📝 Feedback recorded — thank you!", ephemeral=True)

@bot.tree.command(name="exportfeedback", description="[Admin] Export all feedback (optionally one client)")
async def exportfeedback(interaction: discord.Interaction, client: str = None):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("⛔ Admin only.", ephemeral=True)
    rows = [f for f in data["feedback"] if client is None or f["client"].lower() == client.lower()]
    if not rows:
        return await interaction.response.send_message("No feedback found.", ephemeral=True)
    lines = [f"[{r['at']}] ({r['client']}) {r['author']}: {r['content']}" for r in rows]
    fname = f"feedback_{client or 'all'}.txt"
    with open(fname, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    await interaction.response.send_message(f"📤 {len(rows)} feedback entries:", file=discord.File(fname), ephemeral=True)
    os.remove(fname)

@bot.event
async def on_message_feedback(message: discord.Message):
    pass  # placeholder (real hook below)

# capture every client message inside delivery threads as informal feedback
_orig_on_message = on_message
async def _wrapped_on_message(message: discord.Message):
    if not message.author.bot:
        th = data["threads"].get(str(message.channel.id)) if isinstance(message.channel, discord.Thread) else None
        if th and not message.content.startswith("/"):
            log_feedback(str(message.channel.id), th["client"], message.author, message.content)
            try:
                await message.add_reaction("📝")
            except discord.HTTPException:
                pass
    await _orig_on_message(message)
bot.on_message = _wrapped_on_message

# ----------------------------- BREAKS -----------------------------
@bot.tree.command(name="break", description="Mark yourself as on a break")
@app_commands.describe(break_type="Type of break")
@app_commands.choices(break_type=[
    app_commands.Choice(name="Lunch Break", value="Lunch Break"),
    app_commands.Choice(name="Small Break", value="Small Break"),
])
async def break_cmd(interaction: discord.Interaction, break_type: app_commands.Choice[str]):
    member = interaction.user
    current_nick = member.display_name

    if "[BREAK]" in current_nick:
        return await interaction.response.send_message("⚠️ You are already marked as being on a break!", ephemeral=True)

    new_nick = f"[BREAK] {current_nick}"
    try:
        await member.edit(nick=new_nick)
        await interaction.response.send_message(f"☕ You are now on a **{break_type.name}**. Enjoy!", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to change your nickname. (My role might be lower than yours)", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

@bot.tree.command(name="endbreak", description="End your break and revert your nickname")
async def endbreak(interaction: discord.Interaction):
    member = interaction.user
    current_nick = member.display_name

    if "[BREAK]" not in current_nick:
        return await interaction.response.send_message("⚠️ You aren't currently marked as being on a break!", ephemeral=True)

    new_nick = current_nick.replace("[BREAK]", "").strip()
    try:
        await member.edit(nick=new_nick if new_nick else None)
        await interaction.response.send_message("✅ Welcome back! Your nickname has been restored.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to change your nickname.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"❌ Error: {str(e)}", ephemeral=True)

# ----------------------------- CLOCK IN / OUT -----------------------------
@bot.tree.command(name="addtimesheetrole", description="[Admin] Allow a specific role to view the timesheet")
@app_commands.describe(role="The role to authorize")
async def addtimesheetrole(interaction: discord.Interaction, role: discord.Role):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("⛔ Admin only.", ephemeral=True)

    if role.id in data["authorized_timesheet_roles"]:
        return await interaction.response.send_message(f"Role {role.name} is already authorized.", ephemeral=True)

    data["authorized_timesheet_roles"].append(role.id)
    save_data()
    await interaction.response.send_message(f"✅ Role {role.mention} can now view the timesheet.", ephemeral=True)

@bot.tree.command(name="removetimesheetrole", description="[Admin] Remove a role's access to the timesheet")
@app_commands.describe(role="The role to remove")
async def removetimesheetrole(interaction: discord.Interaction, role: discord.Role):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("⛔ Admin only.", ephemeral=True)

    if role.id not in data["authorized_timesheet_roles"]:
        return await interaction.response.send_message(f"Role {role.name} is not authorized.", ephemeral=True)

    data["authorized_timesheet_roles"].remove(role.id)
    save_data()
    await interaction.response.send_message(f"🗑️ Role {role.mention} can no longer view the timesheet.", ephemeral=True)

@bot.tree.command(name="clockin", description="Clock in (available from 09:00 WITA)")
async def clockin(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("⛔ Only you may use the time clock.", ephemeral=True)
    uid = str(interaction.user.id)
    if uid in data["clock_state"]:
        return await interaction.response.send_message("⚠️ You are already clocked in.", ephemeral=True)
    now = wita_now()
    if now.hour < CLOCK_IN_HOUR:
        return await interaction.response.send_message(
            f"⏰ Too early — clock-in opens at **{CLOCK_IN_HOUR:02d}:00 WITA** (now {now.strftime('%H:%M')} WITA).", ephemeral=True)
    data["clock_state"][uid] = {"in": now.isoformat(), "overtime_prompted": False, "overtime_confirmed": False}
    save_data()
    await interaction.response.send_message(f"🟢 Clocked in at **{now.strftime('%H:%M')} WITA**. Have a productive day!", ephemeral=True)

@bot.tree.command(name="clockout", description="Clock out")
async def clockout(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("⛔ Only you may use the time clock.", ephemeral=True)
    entry = do_clockout(interaction.user.id)
    if not entry:
        return await interaction.response.send_message("⚠️ You are not clocked in.", ephemeral=True)
    await interaction.response.send_message(
        f"🔴 Clocked out at **{entry['out']} WITA** — total **{fmt_duration(entry['minutes'])}**"
        + (" *(overtime)*" if entry["overtime"] else ""), ephemeral=True)

@bot.tree.command(name="cleartimesheet", description="[Admin] Clear all timesheet clock entries")
async def cleartimesheet(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("⛔ Admin only.", ephemeral=True)

    count = len(data["clock_entries"])
    data["clock_entries"] = []
    save_data()
    await interaction.response.send_message(f"🗑️ Timesheet cleared. Deleted {count} entries.", ephemeral=True)

@bot.tree.command(name="timesheet", description="View recent clock entries")
async def timesheet(interaction: discord.Interaction):
    # Check if user is admin OR has an authorized role
    is_authorized = is_admin(interaction.user.id)
    if not is_authorized:
        user_role_ids = [r.id for r in interaction.user.roles]
        if any(role_id in data["authorized_timesheet_roles"] for role_id in user_role_ids):
            is_authorized = True

    if not is_authorized:
        return await interaction.response.send_message("⛔ You do not have permission to view the timesheet.", ephemeral=True)

    thirty_days_ago = (wita_now() - timedelta(days=30)).strftime("%Y-%m-%d")
    entries = [e for e in data["clock_entries"] if e["date"] >= thirty_days_ago]
    if not entries:
        return await interaction.response.send_message("No entries yet.", ephemeral=True)
    embed = discord.Embed(title="🗓️ Timesheet (last 30 days)", color=discord.Color.gold())
    for e in entries:
        embed.add_field(
            name=f"{e['date']} — {e['in']} → {e['out']}",
            value=f"{fmt_duration(e['minutes'])}" + (" ⏰ overtime" if e["overtime"] else ""),
            inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ----------------------------- AI: OLLAMA -----------------------------
@bot.tree.command(name="ollama", description="Ask the local AI for help with coding or general questions")
@app_commands.describe(prompt="What do you want to ask the AI?")
async def ollama(interaction: discord.Interaction, prompt: str):
    await interaction.response.defer()  # AI takes time to think

    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": "You are an expert software engineer and helpful assistant. Provide concise, high-quality code and clear explanations."},
            {"role": "user", "content": prompt}
        ],
        "stream": False
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{OLLAMA_URL}/api/chat", json=payload, timeout=60) as resp:
                if resp.status == 200:
                    result = await resp.json()
                    response_text = result["message"]["content"]

                    # Discord message limit is 2000 chars
                    if len(response_text) <= 2000:
                        await interaction.followup.send(response_text)
                    else:
                        # Split into chunks
                        chunks = [response_text[i:i+2000] for i in range(0, len(response_text), 2000)]
                        for chunk in chunks:
                            await interaction.followup.send(chunk)
                else:
                    await interaction.followup.send(f"❌ Ollama Error: {resp.status} {await resp.text()}")
    except Exception as e:
        await interaction.followup.send(f"❌ AI Error: {str(e)}")

# ----------------------------- HELP -----------------------------
@bot.tree.command(name="help", description="Show all commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="🤖 Client Portal Bot", color=discord.Color.blurple())
    embed.add_field(name="🔑 Tokens", value="`/gentoken` (+optional `thread_id` to unlock an existing thread) `/revoketoken` — client sends the token in DM to unlock their private thread", inline=False)
    embed.add_field(name="📁 Threads", value="`/close` lock · `/open [minutes]` temporary reopen (auto-closes)", inline=False)
    embed.add_field(name="📝 Feedback", value="Every message in a thread is logged · `/feedback` formal · `/exportfeedback`", inline=False)
    embed.add_field(name="🕒 Clock (WITA/Bali)", value="`/clockin` from 08:00 · `/clockout` · overtime DM after 18:00 · `/timesheet` · `/cleartimesheet` (Admin)", inline=False)
    embed.add_field(name="☕ Breaks", value="`/break [type]` mark break · `/endbreak` return to work", inline=False)
    embed.add_field(name="👥 Clients", value="`/addclient` `/clients` `/removeclient` — each client gets a role", inline=False)
    embed.add_field(name="🤖 AI", value="`/ollama [prompt]` — ask the local AI for coding help or general questions", inline=False)
    embed.add_field(name="🛠️ Admin Utility", value="`/setnickname` · `/resetnickname` · `/purge [amount]` — delete messages · `/addtimesheetrole` / `/removetimesheetrole` — manage timesheet access", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ----------------------------- RUN -----------------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN or not ADMIN_ID or not GUILD_ID:
        raise SystemExit("Set DISCORD_TOKEN, ADMIN_ID and GUILD_ID environment variables first!")
    bot.run(DISCORD_TOKEN)
