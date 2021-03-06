import queue
import json
import re
import pytz
import datetime
import logging
import asyncio

import irc.client
from jinja2.utils import Markup, escape, urlize as real_urlize
import sqlalchemy

import common.http
import common.url
from common import utils
from common import space
from common.config import config
import lrrbot.main

__all__ = ["log_chat", "clear_chat_log", "exitthread"]

log = logging.getLogger('chatlog')

CACHE_EXPIRY = 7*24*60*60
PURGE_PERIOD = datetime.timedelta(minutes=5)

queue = asyncio.Queue()

space.monkey_patch_urlize()

re_cheer = re.compile(r"(?:^|(?<=\s))(cheer0*)([1-9][0-9]*)(?:$|(?=\s))", re.IGNORECASE)

def urlize(text):
	return real_urlize(text).replace('<a ', '<a target="_blank" rel="noopener nofollow" ')

# Chat-log handling functions live in an asyncio task, so that functions that take
# a long time to run, like downloading the emote list, don't block the bot... but
# one master task, with a message queue, so that things still happen in the right order.
@asyncio.coroutine
def run_task():
	while True:
		ev, params = yield from queue.get()
		if ev == "log_chat":
			yield from do_log_chat(*params)
		elif ev == "clear_chat_log":
			yield from do_clear_chat_log(*params)
		elif ev == "rebuild_all":
			yield from do_rebuild_all()
		elif ev == "exit":
			break

def log_chat(event, metadata):
	queue.put_nowait(("log_chat", (datetime.datetime.now(pytz.utc), event, metadata)))

def clear_chat_log(nick):
	queue.put_nowait(("clear_chat_log", (datetime.datetime.now(pytz.utc), nick)))

def rebuild_all():
	queue.put_nowait(("rebuild_all", ()))

def stop_task():
	queue.put_nowait(("exit", ()))

@utils.swallow_errors
@asyncio.coroutine
def do_log_chat(time, event, metadata):
	"""
	Add a new message to the chat log.
	"""
	# Don't log server commands like .timeout
	message = event.arguments[0]
	if message[0] in "./" and message[1:4].lower() != "me ":
		return

	source = irc.client.NickMask(event.source).nick
	html = yield from build_message_html(time, source, event.target, event.arguments[0], metadata.get('specialuser', []), metadata.get('usercolor'), metadata.get('emoteset', []), metadata.get('emotes'), metadata.get('display-name'))
	with lrrbot.main.bot.engine.begin() as conn:
		conn.execute(lrrbot.main.bot.metadata.tables["log"].insert(),
			time=time,
			source=source,
			target=event.target,
			message=event.arguments[0],
			specialuser=list(metadata.get('specialuser', [])),
			usercolor=metadata.get('usercolor'),
			emoteset=list(metadata.get('emoteset', [])),
			emotes=metadata.get('emotes'),
			displayname=metadata.get('display-name'),
			messagehtml=html,
		)

@utils.swallow_errors
@asyncio.coroutine
def do_clear_chat_log(time, nick):
	"""
	Mark a user's earlier posts as "deleted" in the chat log, for when a user is banned/timed out.
	"""
	log = lrrbot.main.bot.metadata.tables["log"]
	with lrrbot.main.bot.engine.begin() as conn:
		query = sqlalchemy.select([
			log.c.id, log.c.time, log.c.source, log.c.target, log.c.message, log.c.specialuser,
			log.c.usercolor, log.c.emoteset, log.c.emotes, log.c.displayname
		]).where((log.c.source == nick) & (log.c.time >= time - PURGE_PERIOD))
		rows = conn.execute(query).fetchall()
	new_rows = []
	for key, time, source, target, message, specialuser, usercolor, emoteset, emotes, displayname in rows:
		specialuser = set(specialuser) if specialuser else set()
		emoteset = set(emoteset) if emoteset else set()

		specialuser.add("cleared")

		html = yield from build_message_html(time, source, target, message, specialuser, usercolor, emoteset, emotes, displayname)
		new_rows.append({
			"specialuser": list(specialuser),
			"messagehtml": html,
			"_key": key,
		})
	with lrrbot.main.bot.engine.begin() as conn:
		conn.execute(log.update().where(log.c.id == sqlalchemy.bindparam("_key")), *new_rows)

@utils.swallow_errors
@asyncio.coroutine
def do_rebuild_all():
	"""
	Rebuild all the message HTML blobs in the database.
	"""
	log = lrrbot.main.bot.metadata.tables["log"]
	conn_select = lrrbot.main.bot.engine.connect()
	count, = conn_select.execute(log.count()).first()
	rows = conn_select.execute(sqlalchemy.select([
		log.c.id, log.c.time, log.c.source, log.c.target, log.c.message, log.c.specialuser,
		log.c.usercolor, log.c.emoteset, log.c.emotes, log.c.displayname
	]).execution_options(stream_results=True))

	conn_update = lrrbot.main.bot.engine.connect()
	trans = conn_update.begin()

	try:
		for i, (key, time, source, target, message, specialuser, usercolor, emoteset, emotes, displayname) in enumerate(rows):
			if i % 100 == 0:
				print("\r%d/%d" % (i, count), end='')
			specialuser = set(specialuser) if specialuser else set()
			emoteset = set(emoteset) if emoteset else set()
			html = yield from build_message_html(time, source, target, message, specialuser, usercolor, emoteset, emotes, displayname)
			conn_update.execute(log.update().where(log.c.id == key), messagehtml=html)
		print("\r%d/%d" % (count, count))
		trans.commit()
	except:
		trans.rollback()
		raise
	finally:
		conn_select.close()
		conn_update.close()

@asyncio.coroutine
def format_message(message, emotes, emoteset, size="1", cheer=False):
	if emotes is not None:
		return format_message_explicit_emotes(message, emotes, size=size, cheer=cheer)
	else:
		return format_message_emoteset(message, (yield from get_filtered_emotes(emoteset)), cheer=cheer)

def format_message_emoteset(message, emotes, size="1", cheer=False):
	ret = ""
	stack = [(message, "")]
	while len(stack) != 0:
		prefix, suffix = stack.pop()
		for emote in emotes:
			parts = emote["regex"].split(prefix, 1)
			if len(parts) >= 3:
				stack.append((parts[-1], suffix))
				stack.append((parts[0], Markup(emote["html"].format(escape(parts[1])))))
				break
		else:
			ret += Markup(format_message_cheer(prefix, size=size, cheer=cheer)) + suffix
	return ret

def format_message_explicit_emotes(message, emotes, size="1", cheer=False):
	if not emotes:
		return Markup(format_message_cheer(message, size=size, cheer=cheer))

	# emotes format is
	# <emoteid>:<start>-<end>[,<start>-<end>,...][/<emoteid>:<start>-<end>,.../...]
	# eg:
	# 123:0-2/456:3-6,7-10
	# means that chars 0-2 (inclusive, 0-based) are emote 123,
	# and chars 3-6 and 7-10 are two copies of emote 456
	parsed_emotes = []
	for emote in emotes.split('/'):
		emoteid, positions = emote.split(':')
		emoteid = int(emoteid)
		for position in positions.split(','):
			start, end = position.split('-')
			start = int(start)
			end = int(end) + 1 # make it left-inclusive, to be more consistent with how Python does things
			parsed_emotes.append((start, end, emoteid))
	parsed_emotes.sort(key=lambda x:x[0])

	bits = []
	prev = 0
	for start, end, emoteid in parsed_emotes:
		if prev < start:
			bits.append(format_message_cheer(message[prev:start], size=size, cheer=cheer))
		url = escape("https://static-cdn.jtvnw.net/emoticons/v1/%d/%s.0" % (emoteid, size))
		command = escape(message[start:end])
		bits.append('<img src="%s" alt="%s" title="%s">' % (url, command, command))
		prev = end
	if prev < len(message):
		bits.append(format_message_cheer(message[prev:], size=size, cheer=cheer))
	return Markup(''.join(bits))

def format_message_cheer(message, size="1", cheer=False):
	if not cheer:
		return urlize(message)
	else:
		bits = []
		splits = re_cheer.split(message)
		for i in range(0, len(splits), 3):
			bits.append(urlize(splits[i]))
			if i + 1 < len(splits):
				count = int(splits[i + 2])
				level = lrrbot.twitchcheer.TwitchCheer.get_level(count)
				url = escape("https://static-cdn.jtvnw.net/bits/light/static/%s/%s" % (level, size))
				bits.append('<span class="cheer %s"><img src="%s" alt="%s" title="cheer %d">%d</span>' % (escape(level), url, escape(splits[i + 1]), count, count))
		return ''.join(bits)

@asyncio.coroutine
def build_message_html(time, source, target, message, specialuser, usercolor, emoteset, emotes, displayname):
	if source.lower() == config['notifyuser']:
		return '<div class="notification line" data-timestamp="%d">%s</div>' % (time.timestamp(), escape(message))

	if message[:4].lower() in (".me ", "/me "):
		is_action = True
		message = message[4:]
	else:
		is_action = False

	ret = []
	ret.append('<div class="line" data-timestamp="%d">' % time.timestamp())
	if 'staff' in specialuser:
		ret.append('<span class="badge staff"></span> ')
	if 'admin' in specialuser:
		ret.append('<span class="badge admin"></span> ')
	if "#" + source.lower() == target.lower():
		ret.append('<span class="badge broadcaster"></span> ')
	if 'mod' in specialuser:
		ret.append('<span class="badge mod"></span> ')
	if 'turbo' in specialuser:
		ret.append('<span class="badge turbo"></span> ')
	if 'subscriber' in specialuser:
		ret.append('<span class="badge subscriber"></span> ')
	ret.append('<span class="nick"')
	if usercolor:
		ret.append(' style="color:%s"' % escape(usercolor))
	ret.append('>%s</span>' % escape(displayname or (yield from get_display_name(source))))

	if is_action:
		ret.append(' <span class="action"')
		if usercolor:
			ret.append(' style="color:%s"' % escape(usercolor))
		ret.append('>')
	else:
		ret.append(": ")

	if 'cleared' in specialuser:
		ret.append('<span class="deleted">&lt;message deleted&gt;</span>')
		# Use escape() rather than urlize() so as not to have live spam links
		# either for users to accidentally click, or for Google to see
		ret.append('<span class="message cleared">%s</span>' % escape(message))
	else:
		messagehtml = yield from format_message(message, emotes, emoteset, cheer='cheer' in specialuser)
		ret.append('<span class="message">%s</span>' % messagehtml)

	if is_action:
		ret.append('</span>')
	ret.append('</div>')
	return ''.join(ret)

@utils.cache(CACHE_EXPIRY, params=[0])
@asyncio.coroutine
def get_display_name(nick):
	try:
		headers = {
			"Client-ID": config['twitch_clientid'],
		}
		data = yield from common.http.request_coro("https://api.twitch.tv/kraken/users/%s" % nick, headers=headers)
		data = json.loads(data)
		return data['display_name']
	except utils.PASSTHROUGH_EXCEPTIONS:
		raise
	except Exception:
		return nick

re_just_words = re.compile("^\w+$")
@asyncio.coroutine
def get_twitch_emoticons():
	headers = {
		"Client-ID": config['twitch_clientid'],
	}
	data = yield from common.http.request_coro("https://api.twitch.tv/kraken/chat/emoticons", headers=headers)
	data = json.loads(data)['emoticons']
	emotesets = {}
	for emote in data:
		regex = emote['regex']
		if regex == r"\:-?[\\/]": # Don't match :/ inside URLs
			regex = r"\:-?[\\/](?![\\/])"
		regex = regex.replace(r"\&lt\;", "<").replace(r"\&gt\;", ">").replace(r"\&quot\;", '"').replace(r"\&amp\;", "&")
		if re_just_words.match(regex):
			regex = r"\b%s\b" % regex
		regex = re.compile("(%s)" % regex)
		for image in emote['images']:
			if image['url'] is None:
				continue
			html = '<img src="%s" width="%d" height="%d" alt="{0}" title="{0}">' % (
			common.url.https(image['url']), image['width'], image['height'])
			emotesets.setdefault(image.get("emoticon_set"), {})[emote['regex']] = {
				"regex": regex,
				"html": html,
			}
	return emotesets

@asyncio.coroutine
def get_twitch_emoticon_images():
	headers = {
		"Client-ID": config['twitch_clientid'],
	}
	data = yield from common.http.request_coro("https://api.twitch.tv/kraken/chat/emoticon_images", headers=headers)
	data = json.loads(data)["emoticons"]
	emotesets = {}
	for emote in data:
		regex = emote["code"]
		if regex == r"\:-?[\\/]": # Don't match :/ inside URLs
			regex = r"\:-?[\\/](?![\\/])"
		regex = regex.replace(r"\&lt\;", "<").replace(r"\&gt\;", ">").replace(r"\&quot\;", '"').replace(r"\&amp\;", "&")
		if re_just_words.match(regex):
			regex = r"\b%s\b" % regex
		emotesets.setdefault(emote["emoticon_set"], {})[emote["code"]] = {
			"regex": re.compile("(%s)" % regex),
			"html": '<img src="https://static-cdn.jtvnw.net/emoticons/v1/%s/1.0" alt="{0}" title="{0}">' % emote["id"]
		}
	return emotesets

@utils.cache(CACHE_EXPIRY)
@asyncio.coroutine
def get_twitch_emotes():
	try:
		return (yield from get_twitch_emoticons())
	except utils.PASSTHROUGH_EXCEPTIONS:
		raise
	except Exception:
		return (yield from get_twitch_emoticon_images())

@asyncio.coroutine
def get_filtered_emotes(setids):
	try:
		emotesets = yield from get_twitch_emotes()
		emotes = dict(emotesets[None])
		for setid in setids:
			emotes.update(emotesets.get(setid, {}))
		return emotes.values()
	except utils.PASSTHROUGH_EXCEPTIONS:
		raise
	except Exception:
		log.exception("Error fetching emotes")
		return []
