import aiomas
import random
import irc.client

import lrrbot.decorators
from lrrbot import storage
from lrrbot.main import bot, log

@bot.command("explain (.*?)")
@lrrbot.decorators.throttle(30, params=[4], count=2)
def explain_response(lrrbot, conn, event, respond_to, command):
	"""
	Command: !explain TOPIC
	Mod-Only: true
	Section: text

	Provide an explanation for a given topic.
	--command
	Command: !explain show
	Mod-Only: true
	Section: text

	Provide an explanation for the currently-live show.
	"""
	command = " ".join(command.split())
	if command.lower() == "show":
		command = lrrbot.show_override or lrrbot.show
		if command is None and lrrbot.is_mod(event):
			conn.privmsg(respond_to, "Current show not set.")
	response_data = storage.data["explanations"].get(command.lower())
	if not response_data:
		return
	if response_data["access"] == "sub":
		if not lrrbot.is_sub(event) and not lrrbot.is_mod(event):
			log.info("Refusing explain %s due to inadequate access" % command)
			source = irc.client.NickMask(event.source)
			conn.privmsg(source.nick, "That is a sub-only command.")
			return
	if response_data["access"] == "mod":
		if not lrrbot.is_mod(event):
			log.info("Refusing explain %s due to inadequate access" % command)
			source = irc.client.NickMask(event.source)
			conn.privmsg(source.nick, "That is a mod-only command.")
			return
	response = response_data['response']
	if isinstance(response, (tuple, list)):
		response = random.choice(response)
	conn.privmsg(respond_to, response)

@aiomas.expose
def modify_explanations(commands):
	log.info("Setting explanations to %r" % commands)
	storage.data["explanations"] = {k.lower(): v for k, v in commands.items()}
	storage.save()

bot.rpc_server.explain = aiomas.rpc.ServiceDict({
	'modify_explanations': modify_explanations,
})
