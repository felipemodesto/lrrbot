from lrrbot import bot
import twitch
import utils
import time
import storage
import irc.client

@bot.command("highlight (.*?)")
@utils.sub_only
@utils.throttle(60)
def highlight(lrrbot, conn, event, respond_to, description):
	"""
	Command: !highlight DESCRIPTION

	For use when something particularly awesome happens onstream, adds an entry on the Highlight Reel spreadsheet: https://docs.google.com/spreadsheets/d/1yrf6d7dPyTiWksFkhISqEc-JR71dxZMkUoYrX4BR40Y

	Note that the highlights won't appear on the spreadsheet immediately, as the link won't be available until the stream finishes and the video is in the archive. It should appear within a day.
	"""
	if not twitch.get_info()["live"]:
		conn.privmsg(respond_to, "Not currently streaming.")
		return
	storage.data.setdefault("staged_highlights", [])
	storage.data["staged_highlights"] += [{
		"time": time.time(),
		"user": irc.client.NickMask(event.source).nick,
		"description": description,
	}]
	storage.save()
	conn.privmsg(respond_to, "Highlight added.")