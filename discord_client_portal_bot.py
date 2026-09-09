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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

# ----------------------------- CONFIG -----------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_ID      = int(os.getenv("ADMIN_ID", "0"))
GUILD_ID      = int(os.getenv("GUILD_ID", "0"))

WITA = ZoneInfo("Asia/Makassar")            # Bali time = UTC+8
CLOCK_IN_HOUR  = 9                          # earliest clock-in  (WITA)
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
    print(f"✅ Logged in as {bot.user} — WITA time: {wita_now().strftime('%Y-%m-%d %H:%M')}")

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

# ----------------------------- CLOCK IN / OUT -----------------------------
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

@bot.tree.command(name="timesheet", description="View recent clock entries")
async def timesheet(interaction: discord.Interaction):
    if not is_admin(interaction.user.id):
        return await interaction.response.send_message("⛔ Admin only.", ephemeral=True)
    entries = data["clock_entries"][-14:]
    if not entries:
        return await interaction.response.send_message("No entries yet.", ephemeral=True)
    embed = discord.Embed(title="🗓️ Timesheet (last 14 entries)", color=discord.Color.gold())
    for e in entries:
        embed.add_field(
            name=f"{e['date']} — {e['in']} → {e['out']}",
            value=f"{fmt_duration(e['minutes'])}" + (" ⏰ overtime" if e["overtime"] else ""),
            inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ----------------------------- HELP -----------------------------
@bot.tree.command(name="help", description="Show all commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="🤖 Client Portal Bot", color=discord.Color.blurple())
    embed.add_field(name="🔑 Tokens", value="`/gentoken` (+optional `thread_id` to unlock an existing thread) `/revoketoken` — client sends the token in DM to unlock their private thread", inline=False)
    embed.add_field(name="📁 Threads", value="`/close` lock · `/open [minutes]` temporary reopen (auto-closes)", inline=False)
    embed.add_field(name="📝 Feedback", value="Every message in a thread is logged · `/feedback` formal · `/exportfeedback`", inline=False)
    embed.add_field(name="🕒 Clock (WITA/Bali)", value="`/clockin` from 09:00 · `/clockout` · overtime DM after 18:00 · `/timesheet`", inline=False)
    embed.add_field(name="👥 Clients", value="`/addclient` `/clients` `/removeclient` — each client gets a role", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ----------------------------- RUN -----------------------------
if __name__ == "__main__":
    if not DISCORD_TOKEN or not ADMIN_ID or not GUILD_ID:
        raise SystemExit("Set DISCORD_TOKEN, ADMIN_ID and GUILD_ID environment variables first!")
    bot.run(DISCORD_TOKEN)
