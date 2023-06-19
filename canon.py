import sqlite3
import logging
import random
import time
import discord
import asyncio

import flask
import openai

import config


logging.basicConfig(filename=config.log_file, encoding="utf-8", format="[%(asctime)s] [%(name)s] [%(levelname)s] %(message)s", level=logging.INFO)
app = flask.Flask(__name__)

client = discord.Client(intents=discord.Intents())
loop = asyncio.get_event_loop()
do = loop.run_until_complete
do(client.login(config.token))
esolangs = do(client.fetch_guild(346530916832903169))


def get_db():
    try:
        return flask.g._db
    except AttributeError:
        db = sqlite3.connect("the.db")
        db.row_factory = sqlite3.Row
        flask.g._db = db
        return db

@app.teardown_appcontext
def close_connection(exception):
    try:
        flask.g._db.close()
    except AttributeError:
        pass

def conflicts(name):
    return bool(get_db().execute("SELECT NULL FROM Personas WHERE active AND name = ?", (name,)).fetchone())

with open("names") as f:
    names = f.read().splitlines()

def rand_name():
    while True:
        name = random.choice(names)
        if not conflicts(name):
            return name

@app.route("/users/<int:user>")
def can_play(user):
    try:
        do(esolangs.fetch_member(user))
    except discord.NotFound:
        return {"result": False}
    else:
        return {"result": True}

@app.route("/users/<int:user>/personas")
def get_personas(user):
    db = get_db()
    if not db.execute("SELECT NULL FROM Personas WHERE active AND toki_pona AND user = ?", (user,)).fetchone():
        db.execute("INSERT INTO Personas (user, name, temp, toki_pona) VALUES (?, ?, 1, 1)", (user, rand_name()))
        db.commit()
    return [{"id": id, "name": name, "temp": temp} for id, name, temp in db.execute("SELECT id, name, temp FROM Personas WHERE active AND user = ? ORDER BY last_used DESC", (user,))]

@app.route("/users/<int:user>/personas", methods=["POST"])
def add_persona(user):
    json = flask.request.json
    name = json["name"].strip()
    db = get_db()
    if conflicts(name) or not json.get("sudo") and (name.startswith("jan ") or name.startswith("[") and name.endswith("]")):
        return {"result": "taken"}, 403
    id = db.execute("INSERT INTO Personas (user, name, temp) VALUES (?, ?, ?) RETURNING id", (user, name, json.get("temp", False))).fetchone()[0]
    db.commit()
    return {"result": "success", "id": id}

def get_settings(user):
    db = get_db()
    return dict(
        db.execute("SELECT * FROM Settings WHERE user = ?", (user,)).fetchone()
     or db.execute("INSERT OR IGNORE INTO Settings (user) VALUES (?) RETURNING *", (user,)).fetchone()
    )

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
        "blurb": "Esobot will DM you if someone sends a comment on a code guessing submission you wrote.",
    },
    {
        "name": "notify_replies",
        "display": "Reply notifications",
        "blurb": "Esobot will DM you if someone replies to a comment you made.",
    },
    {
        "name": "dms",
        "display": "Receive DMs",
        "blurb": "Users will be able to send direct messages to you anonymously through Esobot.",
    },
]

@app.route("/users/<int:user>/settings")
def settings(user):
    s = get_settings(user)
    return [{"value": s[d["name"]], **d} for d in blurbs]

@app.route("/users/<int:user>/settings", methods=["POST"])
def set_settings(user):
    settings = flask.request.json
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO Settings (user, gpt, lowercase, punctuation, notify_comments, notify_replies, dms) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user, "gpt" in settings, "lowercase" in settings, "punctuation" in settings, "notify_comments" in settings, "notify_replies" in settings, "dms" in settings)
    )
    db.commit()
    return "", 204

@app.route("/users/<int:user>/transform", methods=["POST"])
def transform(user):
    json = flask.request.json
    settings = get_settings(user)

    db = get_db()
    db.execute("UPDATE Personas SET last_used = ? WHERE id = ?", (time.time(), json["persona"]))
    db.commit()

    text = json["text"]
    if text.startswith("\\"):
        text = text[1:]
    else:
        if settings["gpt"]:
            completion = openai.ChatCompletion.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": """As a bot that helps people remain anonymous, you rewrite messages to sound more generic. Your responses should always have the same meaning, perspective and similar tone to the original message, but with different wording and grammar. Please take care to preserve the meaning of programming- and computer-related terms. "Esolangs" is a proper noun and should never be changed."""},
                    {"role": "user", "content": text},
                ],
            )
            text = completion["choices"][0]["message"]["content"]
        if settings["lowercase"]:
            text = text.lower()
        if settings["punctuation"]:
            text = text.replace(",", "").replace("'", "").replace(".", "").replace("?", "")
    return {"text": text}

@app.route("/notify", methods=["POST"])
def notify():
    json = flask.request.json
    parent = json["parent"]
    reply = json["reply"]
    persona = json["persona"]
    user = json["user"]
    url = json["url"]
    name = persona_name(persona) if persona != -1 else f"<@{user}>"
    send_parent = parent != user and get_settings(parent)["notify_comments"]
    if send_parent:
        do(do(esolangs.fetch_member(parent)).send(f"{name} commented on your submission at <{url}>"))
    if reply and not (send_parent and parent == reply) and get_settings(reply)["notify_replies"]:
        do(do(esolangs.fetch_member(reply)).send(f"{name} replied to your comment at <{url}>"))
    return "", 204

@app.route("/personas/<int:id>", methods=["DELETE"])
def disable_persona(id):
    db = get_db()
    db.execute("UPDATE Personas SET active = 0 WHERE id = ?", (id,))
    db.commit()
    return "", 204

def persona_name(id):
    name, = get_db().execute("SELECT name FROM Personas WHERE id = ?", (id,)).fetchone()
    return name

@app.route("/personas/<int:id>")
def get_persona(id):
    return {"name": persona_name(id)}

@app.route("/personas/who")
def reveal():
    name = flask.request.args["name"]
    user, p = get_db().execute("SELECT user, id FROM Personas WHERE active AND name = ?", (name,)).fetchone()
    if not p:
        return {"result": "missing"}, 404
    return {"result": "success", "id": p, "owner": user}

@app.route("/personas/purge", methods=["POST"])
def clear_temp_personas():
    db = get_db()
    db.execute("UPDATE Personas SET active = 0 WHERE temp")
    db.commit()
    return "", 204
