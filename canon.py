import asyncio
import datetime
import logging
import random
import time

import aiosqlite
import aiohttp
import discord
from aiohttp import web
from discord.ext import commands
from bs4 import BeautifulSoup
from openai import AsyncOpenAI

import config

if config.log_file:
    discord.utils.setup_logging(handler=logging.FileHandler(filename=config.log_file, encoding="utf-8"))
else:
    discord.utils.setup_logging()


openai = AsyncOpenAI()
routes = web.RouteTableDef()

with open("names") as f:
    NAMES = f.read().splitlines()

async def rand_name():
    while True:
        name = random.choice(NAMES)
        if not await conflicts(name):
            return name

async def conflicts(name):
    async with db.execute("SELECT EXISTS(SELECT 1 FROM Personas WHERE active AND name = ?)", (name,)) as cur:
        r, = await cur.fetchone()
    return r

@routes.get(r"/users/{user:\d+}")
async def can_play(request):
    user = int(request.match_info["user"])
    if bot.is_ready():
        can_play = bool(bot.get_guild(config.guild_id).get_member(user))
    else:
        can_play = not config.guild_id
    return web.json_response({"can_play": can_play, "is_admin": user in our_staff()})

async def fetch_personas(user):
    async with db.execute("SELECT EXISTS(SELECT 1 FROM Personas WHERE active AND toki_pona AND user = ?)", (user,)) as cur:
        has_toki_pona, = await cur.fetchone()
    if not has_toki_pona:
        await db.execute("INSERT INTO Personas (user, name, temp, toki_pona, last_used) SELECT ?, ?, 1, 1, COALESCE(MAX(last_used), 0) FROM Personas WHERE toki_pona AND user = ?1", (user, await rand_name()))
        await db.commit()

    async with db.execute("SELECT * FROM Personas WHERE active AND user = ? ORDER BY last_used DESC", (user,)) as cur:
        return [Persona(row) for row in await cur.fetchall()]

@routes.get(r"/users/{user:\d+}/personas")
async def get_personas(request):
    user = int(request.match_info["user"])
    return web.json_response([{"id": p.id, "name": p.name, "temp": p.temp} for p in await fetch_personas(user)])

async def parse_user_obj(json):
    name = json["name"].strip()
    if not json.get("sudo") and (
        await conflicts(name)
     or not name
     or not name.isprintable()
     or name.startswith("jan ")
     or name.startswith("[") and name.endswith("]")
    ):
        return None
    return name

@routes.post(r"/users/{user:\d+}/personas")
async def add_persona(request):
    user = int(request.match_info["user"])
    json = await request.json()
    name = await parse_user_obj(json)
    if not name:
        return web.json_response({"result": "taken"}, status=403)

    await db.execute("INSERT INTO Personas (user, name, temp) VALUES (?, ?, ?)", (user, name, json.get("temp", False)))
    await db.commit()
    return web.json_response({"result": "success"})

async def fetch_settings(user):
    async with db.execute("SELECT * FROM Settings WHERE user = ?", (user,)) as cur:
        if r := await cur.fetchone():
            return dict(r)
    async with db.execute("INSERT OR IGNORE INTO Settings (user) VALUES (?) RETURNING *", (user,)) as cur:
        return dict(await cur.fetchone())

blurbs = [
    {
        "name": "gpt",
        "display": "Use GPT",
        "blurb": "Use OpenAI's GPT-4 to transform your writing and make you harder to identify. (Note that this sends your messages to OpenAI's servers.)",
    },
    {
        "name": "lowercase",
        "display": "lowercase everything",
        "blurb": "everything you write anonymously will be made lowercase.",
    },
    {
        "name": "punctuation",
        "display": "Remove punctuation",
        "blurb": "Removes some ASCII punctuation (commas apostrophes periods and question marks) from your anon messages",
    },
    {
        "name": "notify_comments",
        "display": "Comment notifications",
        "blurb": "Canon will DM you if someone sends a comment on a code guessing submission you wrote.",
    },
    {
        "name": "notify_replies",
        "display": "Reply notifications",
        "blurb": "Canon will DM you if someone replies to a comment you made.",
    },
    {
        "name": "dms",
        "display": "Receive DMs",
        "blurb": "Users will be able to send direct messages to you anonymously through Canon.",
    },
    {
        "name": "persona_dms",
        "display": "Receive DMs via personas",
        "blurb": "Users will be able to send direct messages to you using the names of your personas. You'll remain anonymous in these interactions.",
    },
]

@routes.get(r"/users/{user:\d+}/settings")
async def settings(request):
    user = int(request.match_info["user"])
    s = await fetch_settings(user)
    return web.json_response([{"value": s[d["name"]], **d} for d in blurbs])

async def emplace_settings(user, s):
    await db.execute(
        "INSERT OR REPLACE INTO Settings (user, gpt, lowercase, punctuation, notify_comments, notify_replies, dms, persona_dms) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user, "gpt" in s, "lowercase" in s, "punctuation" in s, "notify_comments" in s, "notify_replies" in s, "dms" in s, "persona_dms" in s),
    )
    await db.commit()

@routes.post(r"/users/{user:\d+}/settings")
async def set_settings(request):
    user = int(request.match_info["user"])
    settings = await request.json()
    await emplace_settings(user, settings)
    return web.Response(status=204)

async def transform_text(text, persona, user_id):
    settings = await fetch_settings(user_id)

    await db.execute("UPDATE Personas SET last_used = ? WHERE id = ?", (time.time(), persona))
    await db.commit()

    if text.startswith("\\"):
        text = text[1:]
    else:
        if settings["gpt"]:
            completion = await openai.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": """As a bot that helps people remain anonymous, you rewrite messages to sound more generic. Your responses should always have the same meaning, perspective and similar tone to the original message, but with different wording and grammar. Please take care to preserve the meaning of programming- and computer-related terms. "code guessing" is a proper noun and should never be changed. Discord markup should also be left alone."""},
                    {"role": "user", "content": text},
                ],
            )
            text = completion.choices[0].message.content
        if settings["lowercase"]:
            text = text.lower()
        if settings["punctuation"]:
            text = text.replace(",", "").replace("'", "").replace(".", "").replace("?", "")

    return text

@routes.post(r"/users/{user:\d+}/transform")
async def transform(request):
    user = int(request.match_info["user"])
    json = await request.json()
    return web.json_response({"text": await transform_text(json["text"], json["persona"], user)})

@routes.post("/notify")
async def notify(request):
    if not bot.is_ready() or not config.guild_id:
        return web.Response(status=204)
    json = await request.json()
    parent = json["parent"]
    reply = json["reply"]
    persona = json["persona"]
    user = json["user"]
    url = json["url"]
    content = json["content"]
    name = (await get_persona(persona)).name if persona != -1 else f"<@{user}>"
    messages = {}
    if (await fetch_settings(parent))["notify_comments"]:
        messages[parent] = "commented on your submission"
    if (await fetch_settings(reply))["notify_replies"]:
        messages[reply] = "replied to your comment"
    for k, v in messages.items():
        if True:
            asyncio.create_task(bot.get_guild(config.guild_id).get_member(k).send(f"{name} {v} at <{url}>:\n{content}"))
    return web.Response(status=204)

def our_staff():
    if isinstance(config.admin_ids, list):
        return config.admin_ids
    return [x.id for x in bot.get_guild(config.guild_id).get_role(config.admin_ids).members]

@routes.post("/round-over")
async def round_over(request):
    if config.guild_id and (guild := bot.get_guild(config.guild_id)):
        for admin_id in our_staff():
            if admin := guild.get_member(admin_id):
                asyncio.create_task(admin.send("everyone has finished guessing"))
    return web.Response(status=204)

@routes.get(r"/personas/{persona:\d+}")
async def get_persona(request):
    persona = int(request.match_info["persona"])
    return web.json_response({"name": (await get_persona(persona)).name})

@routes.delete(r"/personas/{persona:\d+}")
async def disable_persona(request):
    persona = int(request.match_info["persona"])
    await db.execute("UPDATE Personas SET active = 0 WHERE id = ?", (persona,))
    await db.commit()
    return web.Response(status=204)

@routes.patch(r"/personas/{persona:\d+}")
async def edit_persona(request):
    json = await request.json()
    name = await parse_user_obj(json)
    if not name:
        return web.json_response({"result": "taken"}, status=403)
    await db.execute("UPDATE Personas SET name = ? WHERE id = ?", (name, id))
    await db.commit()
    return web.json_response({"result": "success"})

@routes.post("/personas/purge")
async def clear_temp_personas(request):
    await db.execute("UPDATE Personas SET active = 0 WHERE temp")
    await db.commit()
    return web.Response(status=204)


intents = discord.Intents(
    guilds=True,
    messages=True,
    message_content=True,
    members=True,
)
bot = commands.Bot(command_prefix="!", intents=intents, allowed_mentions=discord.AllowedMentions.none())

@bot.event
async def on_command_error(ctx, exc, old_command_error=bot.on_command_error):
    if isinstance(exc, commands.CommandNotFound):
        return
    if isinstance(exc, commands.UserInputError):
        return await ctx.send(exc)
    await db.rollback()
    await old_command_error(ctx, exc)

if config.cg_url:
    @bot.command()
    async def cg(ctx):
        """Current information about code guessing."""
        async with session.get(config.cg_url) as resp:
            soup = BeautifulSoup(await resp.text(), "lxml")
        target = datetime.datetime.fromisoformat(soup.find_all("time")[-1]["datetime"])
        when = discord.utils.format_dt(target, "R") if datetime.datetime.now(datetime.timezone.utc) < target else "**when someone wakes up**"
        header = soup.find("h1")
        if not header:
            await ctx.send(f"The next round will start {when}.")
        elif "stage 1" in header.string:
            await ctx.send(f"The uploading stage will end {when}.")
        else:
            await ctx.send(f"The round will end {when}.")


class Persona:
    def __init__(self, row):
        self.id = row["id"]
        self.name = row["name"]
        self.user = bot.get_user(row["user"])
        self.temp = row["temp"]

    def __eq__(self, other):
        return isinstance(other, Persona) and self.id == other.id

    @classmethod
    async def convert(cls, ctx, argument):
        async with db.execute("SELECT * FROM Personas WHERE active AND name = ?", (argument,)) as cur:
            r = await cur.fetchone()
        if r:
            return cls(r)
        raise commands.BadArgument(f"Persona '{argument}' not found.")

    @property
    def mention(self):
        return self.name

async def get_persona(id):
    async with db.execute("SELECT * FROM Personas WHERE id = ?", (id,)) as cur:
        r = await cur.fetchone()
    if r:
        return Persona(r)

async def get_target(id):
    return bot.get_channel(id) or bot.get_user(id) or await get_persona(id)

async def connections(target_id):
    async with db.execute("SELECT a FROM AnonConnections WHERE b = ?1 UNION ALL SELECT b FROM AnonConnections WHERE a = ?1", (target_id,)) as cur:
        return [await get_target(x[0]) for x in await cur.fetchall()]

async def selected_persona(user):
    async with db.execute("SELECT Personas.* FROM SelectedPersona INNER JOIN Personas ON id = persona WHERE SelectedPersona.user = ?", (user.id,)) as cur:
        r = await cur.fetchone()
    return Persona(r) if r else user

@bot.listen()
async def on_message(message):
    if message.author == bot.user or message.content.startswith("!"):
        return

    us = await selected_persona(message.author) if not message.guild else message.channel
    our_name = us.name if isinstance(us, Persona) else message.author.display_name
    text = message.content
    if isinstance(us, Persona):
        text = await transform_text(text, us.id, message.author.id)

    targets = set()
    for conn in await connections(us.id):
        if isinstance(conn, Persona):
            targets.add(conn.user)
            continue
        if isinstance(conn, discord.TextChannel):
            for other_conn in await connections(conn.id):
                if other_conn:
                    targets.add(other_conn.user)
        targets.add(conn)
    targets -= {message.author, None}

    for target in targets:
        await target.send(f"<{our_name}> {text}", files=[await f.to_file() for f in message.attachments])

Target = Persona | discord.TextChannel | discord.User

@commands.dm_only()
@commands.max_concurrency(1, wait=True)
@bot.group(invoke_without_command=True)
async def anon(ctx, target: Target):
    """Anonymously message a user or channel (use in DMs)"""
    try:
        # find which persona we are
        we_are = await selected_persona(ctx.author)
        if we_are == ctx.author:
            for we_are in await fetch_personas(ctx.author.id):
                if await connections(we_are.id):
                    continue
                await db.execute("INSERT INTO SelectedPersona (user, persona) VALUES (?, ?)", (ctx.author.id, we_are.id))
                break

        if await connections(we_are.id):
            return await ctx.send("You are already in a connection.")
        if not isinstance(target, discord.TextChannel) and await connections(target.id):
            return await ctx.send("Target is already in a connection.")

        # tell the target what's happening
        if isinstance(target, discord.TextChannel):
            there = "there"
            member = target.guild.get_member(ctx.author.id)
            if not member or not target.permissions_for(member).send_messages:
                return await ctx.send("You don't have permission to send messages there.")
            await target.send(f"An anonymous user ({we_are.name}) joined the channel.")
        elif isinstance(target, discord.User):
            there = "to them"
            if not (await fetch_settings(target.id))["dms"]:
                return await ctx.send("Target doesn't accept anonymous DMs.")
            if await selected_persona(target) != target:
                await target.send(f"An anonymous user ({we_are.name}) is messaging you. Use `!anon switch` to be able to respond to them.")
            else:
                await target.send(f"An anonymous user ({we_are.name}) is messaging you. Messages you send from now on will be sent to them. Use `!anon stop` to hang up at any time.")
        elif isinstance(target, Persona):
            there = "to them"
            if not (await fetch_settings(target.id))["persona_dms"]:
                return await ctx.send("Target doesn't accept anonymous DMs via persona.")
            if not target.user:
                return await ctx.send(f"A persona called '{target}' exists, but its owner can't be found. (They probably don't share a server with the bot.)")
            if await selected_persona(target.user) != target:
                await target.user.send("An anonymous user ({we_are.name}) is messaging your persona **{target.name}** anonymously. Use `!anon switch {target.name}` to be able to respond to them.")
            else:
                await target.user.send("An anonymous user ({we_are.name}) is messaging your persona **{target.name}** anonymously. They do not know who controls it. Messages you send from now on will be sent to them. Use `!anon stop` to hang up at any time.")

        # form connection
        await db.execute("INSERT INTO AnonConnections (a, b) VALUES (?, ?)", (we_are.id, target.id))
        await db.commit()

        await ctx.send(f"Now connected to {target.mention} as **{we_are.name}**. Use `!anon stop` to disconnect.\nMessages (except commands) sent here will be relayed {there}. Disable automatic normalisation for a single message by prefixing it with `\\`.\n**NOTE**: Full anonymity is not guaranteed. Privileged users can access your identity.")
    finally:
        await db.rollback()

@anon.command(aliases=["ls"])
async def who(ctx):
    """See who is connected to the current channel."""
    if ctx.guild:
        await ctx.send("\n".join(f"- {conn.mention}" for conn in await connections(ctx.channel.id) if conn) or "Nobody!")
    else:
        async with db.execute(
            "WITH us (id) AS (VALUES (?1) UNION ALL SELECT id FROM Personas WHERE user = ?1)"
            "SELECT id, a FROM us INNER JOIN AnonConnections ON b = id "
            "UNION ALL SELECT id, b FROM us INNER JOIN AnonConnections ON a = id",
            (ctx.author.id,),
        ) as cur:
            r = await cur.fetchall()

        selected = await selected_persona(ctx.author)
        main = f"You are not connected to anyone as {selected.mention}."
        alt = []
        for we_are, conn in r:
            we_are = await get_target(we_are)
            conn = await get_target(conn)
            if we_are.id == selected.id:
                main = f"You are connected to {conn.mention} as {selected.mention}."
            else:
                switch = "`!anon switch`" if we_are == ctx.author else f"`!anon switch {we_are.name}`"
                alt.append(f"- {conn.mention} (as {we_are.mention}; {switch})")
        if alt:
            main += f"\n## Other connections\n{"\n".join(alt)}"

        await ctx.send(main)

@commands.dm_only()
@anon.command(aliases=["cd"])
async def switch(ctx, *, target: Target = commands.Author):
    """Change which of your personas is the 'active' one."""
    for to in [target, *await connections(target.id)]:
        if to == ctx.author or isinstance(to, Persona) and to.user == ctx.author:
            break
    else:
        return await ctx.send(f"{target.mention} is not you nor one of your connections.")

    if to == ctx.author:
        await db.execute("DELETE FROM SelectedPersona WHERE user = ?", (ctx.author.id,))
    else:
        await db.execute("INSERT OR REPLACE INTO SelectedPersona (user, persona) VALUES (?, ?)", (ctx.author.id, to.id))

    await db.commit()

    if conns := await connections(to.id):
        await ctx.send(f"Switched to {to.mention}. Your messages are now being sent to {conns[0].mention}. Use `!anon stop` to disconnect.")
    else:
        await ctx.send(f"Switched to {to.mention}.")
 
@commands.dm_only()
@anon.command(aliases=["rm", "leave"])
async def stop(ctx):
    """Disconnect from the current session."""
    we_are = await selected_persona(ctx.author)
    async with db.execute("DELETE FROM AnonConnections WHERE a = ?1 OR b = ?1 RETURNING a, b", (we_are.id,)) as cur:
        r = await cur.fetchone()
    await db.commit()
    if not r:
        return await ctx.send(f"{we_are.mention} is not connected anywhere.")
    for x in r:
        if x != we_are.id:
            break
    victim = await get_target(x)
    await getattr(victim, "user", victim).send(f"{we_are.mention} disconnected.")
    await ctx.send(f"Disconnected from {victim.mention}.")

@commands.dm_only()
@anon.group(aliases=["persona"], invoke_without_command=True)
async def personas(ctx):
    """List your anonymous personas."""
    r = []
    for persona in await fetch_personas(ctx.author.id):
        r.append(f"- **{persona.name}**" + " *(temp)*"*persona.temp)
    await ctx.send("\n".join(r))

@commands.dm_only()
@personas.command(aliases=["new", "create", "make"])
async def add(ctx, *, name):
    if await conflicts(name):
        return await ctx.send("That name is taken or reserved.")
    await db.execute("INSERT INTO Personas (user, name) VALUES (?, ?)", (ctx.author.id, name))
    await db.commit()
    await ctx.send(f"Created a persona named '{name}'.")

@commands.dm_only()
@personas.command(aliases=["delete", "del", "rm", "nix"])
async def remove(ctx, *, name):
    async with db.execute("UPDATE Personas SET active = 0 WHERE active AND user = ? AND name = ? RETURNING 1", (ctx.author.id, name)) as cur:
        if not await cur.fetchone():
            return await ctx.send(f"You have no persona named '{name}'.")
    await db.commit()
    await ctx.send(f"Deleted persona '{name}'.")

def cfg_norm(s):
    return s.replace("_", "-")

@commands.dm_only()
@anon.command(aliases=["settings", "config", "opt", "options"])
async def cfg(ctx, name=None, value: bool = None):
    name = name and cfg_norm(name)
    settings = await fetch_settings(ctx.author.id)
    if value is None:
        embed = discord.Embed()
        for setting in blurbs:
            n = cfg_norm(setting["name"])
            if name and n != name:
                continue
            v = "yneos"[not settings[setting["name"]]::2]
            embed.add_field(name=f"{setting["display"]} (`!anon cfg {n} {v}`)", value=setting["blurb"], inline=False)
        await ctx.send(embed=embed)
    else:
        s = set()
        seen = False
        for setting in blurbs:
            if cfg_norm(setting["name"]) == name:
                seen = True
                if value:
                    s.add(setting["name"])
            elif settings[setting["name"]]:
                s.add(setting["name"])
        if not seen:
            return await ctx.send(f"No option called '{name}' exists.")
        await emplace_settings(ctx.author.id, s)
        await ctx.send(f"Set option '{name}' to {value}.")


async def database(_):
    global db
    async with aiosqlite.connect("the.db", autocommit=False) as db:
        db.row_factory = aiosqlite.Row
        yield

async def the_bot(_):
    global session
    async with aiohttp.ClientSession(headers={"User-Agent": "Canon"}) as session:
        task = asyncio.create_task(bot.start(config.token))
        yield
    await bot.close()

app = web.Application()
app.add_routes(routes)

app.cleanup_ctx.append(database)
if config.token:
    app.cleanup_ctx.append(the_bot)

web.run_app(app, port=40543)
