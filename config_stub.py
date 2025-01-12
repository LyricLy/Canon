# Optional file to write logs to
log_file = None

# Optional bot token
# Without this, features that would require a connection to Discord become no-ops
token = ""

# ID of the Discord guild the game is being played on
# Anyone not on this server will be barred from playing
guild_id = 0

# Discord IDs of the people running the server
# Used to send messages when everyone has pressed the "finished" button in cg, and can be synced with the main server for admin panel access 
# Can also be set to a role ID, in which case anyone with that role will be considered an admin
admin_ids = []

# URL to code guessing server
# Used for !cg command that displays game status
cg_url = ""
