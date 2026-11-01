import asyncio
import hashlib
import json
import math
import re
import secrets
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from .providers import ASIInterpreter, ProviderError

# Both pilot cities are in this zone; times typed without an offset are read as local.
PILOT_TZ = ZoneInfo("America/Los_Angeles")
CJK = re.compile(r"[㐀-鿿]")
REQUIRED = {"city", "start", "end", "budget", "interest"}


class UserError(ValueError):
    """A message written for the person in the chat, safe to show as-is, optionally with a Chinese version."""

    def __init__(self, en, zh=None):
        super().__init__(en)
        self.zh = zh


def cities(value):
    aliases = {"berkeley": "Berkeley", "伯克利": "Berkeley", "san francisco": "San Francisco",
               "san fran": "San Francisco", "sf": "San Francisco", "旧金山": "San Francisco"}
    parts = value if isinstance(value, list) else re.split(r"\s*(?:/|,|、|\bor\b|\band\b|&)\s*", str(value).lower())
    result = set()
    for part in parts:
        city = aliases.get(str(part).strip().lower())
        if not city:
            raise UserError("Which locations work for you: Berkeley, San Francisco, or both? This pilot searches those two cities.",
                            "你可以在哪里见面：伯克利、旧金山，还是都行？目前只支持这两个城市。")
        result.add(city)
    if not result:
        raise UserError("Choose Berkeley, San Francisco, or both.", "请选择伯克利、旧金山，或两个都行。")
    return sorted(result)


def instant(value):
    text = str(value).strip().replace("Z", "+00:00")
    # Accept "2026-11-03 2pm" / "2026-11-03 2:30 pm" as well as ISO 8601.
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2})(?::(\d{2}))?\s*([ap]m)", text, re.I)
    if m:
        if not 1 <= int(m[2]) <= 12:
            raise UserError("For am/pm times, use an hour from 1 to 12, e.g. 2pm.", "用 am/pm 时，小时请写 1 到 12，例如 2pm。")
        hour = int(m[2]) % 12 + (12 if m[4].lower() == "pm" else 0)
        text = f"{m[1]}T{hour:02d}:{m[3] or '00'}"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise UserError(f"I couldn't read the time '{value}'. Use a date and time like 2026-11-03 14:00 (Pacific time).",
                        f"看不懂时间“{value}”。请写成 2026-11-03 14:00 这样的日期和时间（太平洋时间）。") from None
    if dt.tzinfo is None:
        if dt.replace(tzinfo=PILOT_TZ, fold=0).utcoffset() != dt.replace(tzinfo=PILOT_TZ, fold=1).utcoffset():
            raise UserError("This local time falls during a daylight-saving clock change. Include an explicit UTC offset.",
                            "这个时间正好在夏令时切换期间，请加上明确的时区偏移。")
        dt = dt.replace(tzinfo=PILOT_TZ)
    return dt.astimezone(timezone.utc)


def utc(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def zh_clock(d):
    return f"{'上午' if d.hour < 12 else '下午'}{d.hour % 12 or 12}:{d.minute:02d}"


def local(value, lang="en"):
    d = instant(value).astimezone(PILOT_TZ)
    if lang == "zh":
        return f"{d.month}月{d.day}日 周{'一二三四五六日'[d.weekday()]} {zh_clock(d)}（太平洋时间）"
    return d.strftime("%a, %b %d · %I:%M %p %Z")


def local_range(start, end, lang="en"):
    a, b = instant(start).astimezone(PILOT_TZ), instant(end).astimezone(PILOT_TZ)
    if a.date() != b.date():
        return f"{local(start, lang)} – {local(end, lang)}"
    if lang == "zh":
        return f"{a.month}月{a.day}日 周{'一二三四五六日'[a.weekday()]} {zh_clock(a)} – {zh_clock(b)}（太平洋时间）"
    return f"{a.strftime('%a, %b %d · %I:%M %p')} – {b.strftime('%I:%M %p %Z')}"


def money(amount):
    return f"${amount:.2f}"


def time_windows(pref):
    return pref.get("windows") or ([{"start": pref["start"], "end": pref["end"]}]
                                   if "start" in pref and "end" in pref else [])


def complete(pref):
    return {"city", "budget", "interest"}.issubset(pref) and bool(time_windows(pref))


INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def invite_code():
    # Short and easy to retype; single-use, so 31^6 combinations is plenty.
    return "MORI-" + "".join(secrets.choice(INVITE_ALPHABET) for _ in range(6))


@dataclass
class Reply:
    text: str
    card_kind: str | None = None
    card_payload: dict | None = None


HELP = """Mori Together turns 'sometime' into a shared plan.
Preferences you enter are visible to the other participant. Only share information you want them to see.
Commands (or use cards):
new YourName — create a plan
join INVITE_CODE YourName — join a friend's plan in a separate conversation
preferences {"city":"Berkeley","start":"ISO8601 with offset","end":"ISO8601 with offset","budget":30,"interest":"music"}
search — search after BOTH participants enter their constraints
select EVENT_ID — propose an option (not a booking)
confirm VERSION — approve this exact proposal for yourself only
status — view shared preferences, proposal, approvals and calendar link
forget — clear your preferences and all pending approvals
leave — leave this plan (a confirmed plan stays confirmed for your friend)
delete — delete the current shared plan (creator only)
No automatic purchases, background listening or unsolicited reminders. Two distinct transport sessions are not verified human identities; do not use this prototype for sensitive group data."""

HELP_ZH = """Mori Together 帮两个人把“改天一起玩”变成一个都同意的计划。
你填的偏好对方能看到，只分享你愿意让对方看到的信息。
也可以直接用平常说话的方式告诉我时间、地点和预算。常用命令（或直接用卡片）：
new 你的名字 — 新建计划
join 邀请码 你的名字 — 加入朋友的计划
search — 双方都填好偏好后搜索活动
status — 查看进度
edit — 修改我的偏好
leave — 离开这个计划（已确认的计划对方那边仍然有效）
forget — 清除我的偏好和已有的确认
delete — 删除整个计划（仅创建者）
不会自动买票、不会在后台监听，也不会主动打扰你。"""

COMMANDS = {"help", "status", "edit", "search", "forget", "delete", "leave"}


class Store:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS current_plan(actor TEXT PRIMARY KEY, plan TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS receipts(actor TEXT, msg TEXT, plan TEXT, body TEXT, PRIMARY KEY(actor,msg))")
        # A personal display setting, kept out of the shared plan document.
        self.db.execute("CREATE TABLE IF NOT EXISTS languages(actor TEXT PRIMARY KEY, lang TEXT NOT NULL)")

    def current(self, actor):
        row = self.db.execute("SELECT body FROM plans WHERE id=(SELECT plan FROM current_plan WHERE actor=?)", (actor,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, plan):
        self.db.execute("INSERT OR REPLACE INTO plans VALUES(?,?)", (plan["id"], json.dumps(plan)))

    def bind(self, actor, plan):
        self.db.execute("INSERT OR REPLACE INTO current_plan VALUES(?,?)", (actor, plan["id"]))

    def language(self, actor):
        row = self.db.execute("SELECT lang FROM languages WHERE actor=?", (actor,)).fetchone()
        return row[0] if row else "en"

    def set_language(self, actor, lang):
        self.db.execute("INSERT OR REPLACE INTO languages VALUES(?,?)", (actor, lang))


class Planner:
    def __init__(self, store, provider, interpreter=None, now=None):
        self.store, self.provider = store, provider
        self.interpreter = interpreter or ASIInterpreter()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock = asyncio.Lock()
        self.lang = "en"

    def t(self, en, zh):
        return zh if self.lang == "zh" else en

    async def handle(self, actor, text, message_id=None):
        if len(text) > 8000:
            return Reply("Please shorten your message to 8,000 characters.")
        # Serialize state updates, including network refreshes. One-process prototype.
        async with self.lock:
            if message_id:
                old = self.store.db.execute("SELECT body FROM receipts WHERE actor=? AND msg=?", (actor, message_id)).fetchone()
                if old:
                    return Reply(**json.loads(old[0]))
            # Reply in the language the person last wrote in; commands and card clicks keep it.
            self.lang = "zh" if CJK.search(text) else self.store.language(actor)
            try:
                command = await self.parse(actor, text)
                result = await self.execute(actor, command)
                plan = self.store.current(actor)
                if self.lang != self.store.language(actor):
                    self.store.set_language(actor, self.lang)
                if plan and actor in plan["members"]:
                    result.text = self.catch_up(plan, actor) + result.text
                    self.store.save(plan)
                if message_id and command["action"] not in {"forget", "delete"}:
                    self.store.db.execute("INSERT INTO receipts VALUES(?,?,?,?)", (
                        actor, message_id, plan["id"] if plan else None, json.dumps(asdict(result))))
                self.store.db.commit()
                return result
            except (ValueError, KeyError, TypeError, ProviderError) as error:
                self.store.db.rollback()
                # Only hand-written messages reach the user; Python's own parse errors are not readable.
                if isinstance(error, UserError):
                    return Reply(error.zh if self.lang == "zh" and error.zh else str(error))
                if isinstance(error, ProviderError):
                    return Reply(str(error))
                return Reply(self.t("I couldn't understand that. Use the card, or type help for examples.",
                                    "没看懂这条消息。可以直接用卡片，或发 help 查看示例。"))
            except Exception:
                self.store.db.rollback()
                raise

    async def parse(self, actor, text):
        text = text.strip()
        # ASI:One may retain the direct recipient mention in TextContent.
        # Strip only a leading mention, never extract commands from arbitrary prose.
        text = re.sub(r"^@(?:Mori Together|[A-Za-z0-9_-]+)\s+", "", text, count=1).strip()
        if text.startswith("{"):
            obj = json.loads(text)
            if not isinstance(obj, dict):
                raise UserError("Expected a command object.", "需要一个命令对象。")
            return obj
        if re.fullmatch(r"(?i)(?:please\s+)?(?:create|start|make)(?:\s+(?:a|new|shared|an outing))*\s+plan[.!]?", text) or re.fullmatch(r"(?:请|帮我|帮我们)?(?:创建|新建|开始)(?:一个|个)?(?:双人|共享)?计划[！。]?", text):
            return {"action": "new"}
        if re.fullmatch(r"(?i)(?:please )?(?:start (?:all )?over|start again|restart)(?: to plan another one)?[.!]?", text) or re.fullmatch(r"(?:请|帮我|帮我们)?(?:重新开始|从头开始|重新计划)[！。]?", text):
            return {"action": "restart"}
        if re.fullmatch(r"MORI-[A-Z2-9]{6}", text, re.I):
            return {"action": "join", "code": text.upper()}
        verb, _, rest = text.partition(" ")
        verb = verb.lower()
        if verb in COMMANDS and not rest:
            return {"action": verb, "explicit": True}
        # A command typed with stray spaces ("e di t") is still that command.
        if re.sub(r"\s+", "", text).lower() in COMMANDS:
            return {"action": re.sub(r"\s+", "", text).lower(), "explicit": True}
        if verb == "new":
            return {"action": verb, "name": rest or "Friend A"}
        if verb == "join":
            code, _, name = rest.strip().partition(" ")
            if not code:
                raise UserError("Add the invite code your friend got, e.g. join MORI-7K3P9Q YourName.",
                                "请带上朋友给你的邀请码，例如：join MORI-7K3P9Q 你的名字")
            return {"action": verb, "code": code, "name": name or "Friend B"}
        if verb == "preferences":
            return {**json.loads(rest), "action": verb}
        if verb == "select":
            return {"action": verb, "event_id": rest}
        if verb == "confirm":
            # A bare "confirm" shows the proposal again instead of approving an unseen version.
            return {"action": verb, "version": int(rest)} if rest.isdigit() else {"action": verb}
        plan = self.store.current(actor)
        # Do not send other participants' data, invitations or internal identities to the LLM.
        context = {"now": self.now().isoformat(), "timezone": "America/Los_Angeles",
                   "has_plan": bool(plan),
                   "state": plan["state"] if plan else "no_plan",
                   "options": [{"number": i + 1, "title": e["title"]} for i, e in enumerate(plan["events"])] if plan else [],
                   "proposal_title": plan["selected"]["title"] if plan and plan["selected"] else None,
                   "my_preferences": plan["members"][actor]["preferences"] if plan else {}}
        command = await self.interpreter.interpret(text, context)
        if command.get("language") in {"en", "zh"} and not CJK.search(text):
            self.lang = command["language"]
        # Model-generated dates can be stale (e.g. Saturday midnight for "this weekend").
        # Keep the other facts and ask for selectable times instead of losing onboarding.
        prefs = command.get("preferences")
        if isinstance(prefs, dict) and ("windows" in prefs or "start" in prefs or "end" in prefs):
            try:
                timing = {**context["my_preferences"], **prefs}
                if "windows" not in prefs and ("start" in prefs or "end" in prefs):
                    timing.pop("windows", None)
                windows = time_windows(timing)
                invalid = not windows or not 1 <= len(windows) <= 3 or any(
                    not self.now() < instant(w["start"]) < instant(w["end"]) <= self.now() + timedelta(days=7)
                    for w in windows)
            except (ValueError, TypeError, KeyError):
                invalid = True
            if invalid:
                command["preferences"] = {k: v for k, v in prefs.items() if k not in {"start", "end", "windows"}}
                command["pick_times"] = True
                command.pop("question", None)
        if command.get("action") == "review":
            # Natural-language assent opens the exact proposal; the card records approval.
            return {"action": "confirm"}
        if command.get("action") == "choose":
            number = command.get("number")
            if not plan or type(number) is not int or not 1 <= number <= len(plan["events"]):
                raise UserError("Which option do you mean? Choose an activity from the current cards.",
                                "你指的是哪一个？请从当前的活动卡片里选一个。")
            return {"action": "select", "event_id": plan["events"][number - 1]["id"], "version": plan["version"]}
        return command

    def reset(self, p):
        p["browsing"] = False
        p["version"] += 1
        p["approvals"] = []
        p["selected"] = None
        p["events"] = []
        p["state"] = "draft"

    def slot_options(self):
        options = []
        today = self.now().astimezone(PILOT_TZ).date()
        for offset in range(8):
            day = today + timedelta(days=offset)
            for begin, finish in ((9, 12), (12, 15), (15, 18), (18, 22)):
                a = datetime(day.year, day.month, day.day, begin, tzinfo=PILOT_TZ)
                b = a.replace(hour=finish)
                if self.now() < a and b <= self.now() + timedelta(days=7):
                    options.append({"value": utc(a) + "|" + utc(b),
                                    "label": local_range(utc(a), utc(b), self.lang)})
        return options

    def preferences_card(self, p):
        t = self.t
        return Reply(t("Choose up to three available times below, or tell me custom times in your own words. Preferences are shared with your partner. I will search automatically when both of you are ready.",
                       "点选最多三个空闲时段，也可以直接说你自己的具体时间。偏好对方可见；双方信息齐全后，我会自动搜索。"), "form", {
            "title": t("Make room for a little together-time", "一起留点时间吧"),
            "fields": [
                {"name": "kind", "kind": "select", "label": "What would you like to explore?", "required": False, "options": [{"value": "event", "label": "Activities"}, {"value": "restaurant", "label": "Restaurants"}, {"value": "place", "label": "Places to explore"}]},
                {"name": "name", "kind": "text", "label": t("Your name (shown to your friend)", "你的名字（对方会看到）"), "required": False},
                {"name": "city", "kind": "select", "label": t("City", "城市"), "required": True,
                 "options": [{"value": s, "label": s} for s in ["Berkeley", "San Francisco", "Berkeley / San Francisco"]]},
                *[{"name": f"slot_{i}", "kind": "select",
                   "label": t(f"Available time {i}" + (" (optional)" if i > 1 else ""),
                              f"空闲时段 {i}" + ("（可选）" if i > 1 else "")),
                   "required": i == 1,
                   "options": ([{"value": "none", "label": t("No additional time", "不添加时段")}] if i > 1 else []) + self.slot_options()}
                  for i in range(1, 4)],
                {"name": "budget", "kind": "number", "label": t("Maximum USD per person", "每人预算上限（美元）"), "required": True},
                {"name": "interest", "kind": "text", "label": t("An activity or interest", "想做什么 / 兴趣"), "required": True}],
            # No version: preferences are per person, so a partner's update must not void this card.
            "submit_cta": {"label": t("Save my preferences", "保存我的偏好"), "selection": {
                "action": "preferences", "plan_id": p["id"]}}})

    def constraints(self, p):
        if len(p["members"]) != 2:
            raise UserError("Invite a second participant first. Share the invite code privately.",
                            "请先邀请另一个人加入，把邀请码私下发给对方。")
        people = list(p["members"].values())
        for m in people:
            if not complete(m["preferences"]):
                raise UserError(f"{m['name']} still needs to enter their city, availability, budget and interest.",
                                f"{m['name']} 还需要填写城市、空闲时间、预算和兴趣。")
        prefs = [m["preferences"] for m in people]
        shared_cities = sorted(set(cities(prefs[0]["city"])) & set(cities(prefs[1]["city"])))
        if not shared_cities:
            raise UserError("You chose different cities. Agree on Berkeley or San Francisco, then update your preferences.",
                            "你们选的城市不一样。先商量好伯克利还是旧金山，再更新偏好。")
        overlaps = set()
        for a in time_windows(prefs[0]):
            for b in time_windows(prefs[1]):
                start = max(instant(a["start"]), instant(b["start"]))
                end = min(instant(a["end"]), instant(b["end"]))
                if self.now() < start < end:
                    overlaps.add((start, end))
        if not overlaps:
            raise UserError("No future overlapping time. Choose another time slot.", "你们没有重合的未来时段，请另选时间。")
        windows = [{"start": utc(a), "end": utc(b)} for a, b in sorted(overlaps)]
        start, end = sorted(overlaps)[0]
        kinds = {x["kind"] for x in prefs if x.get("kind")}
        if len(kinds) > 1:
            raise UserError("You chose different outing types. Agree on an event, restaurant or place to explore first.")
        kind = next(iter(kinds), "event")
        if kind in {"restaurant", "place"} and any(b - a > timedelta(hours=6) for a, b in overlaps):
            raise UserError("你们的共同空闲时间范围较长。想在几点出发、几点结束？请各自缩小到具体时段（6 小时以内），我不会把午夜默认当作到访时间。 / Please narrow your availability to a visit window of up to six hours.")
        # Search one common interest if present, otherwise broad search avoids AND-filtering away results.
        interest = prefs[0]["interest"] if prefs[0]["interest"].lower() == prefs[1]["interest"].lower() else ""
        if kind == "restaurant":
            food_interests = {x["interest"].lower() for x in prefs
                              if x.get("kind") == "restaurant" and x["interest"].lower() not in {"any", "restaurants", "restaurant", "food"}}
            if len(food_interests) > 1:
                raise UserError("You suggested different cuisines. Agree on one cuisine, or say restaurants / any for a broad search.")
            interest = next(iter(food_interests), "restaurants")
        if kind == "place":
            terms = list(dict.fromkeys(x["interest"] for x in prefs if x.get("kind") == "place" and x["interest"].lower() != "any"))
            interest = " ".join(terms) or "parks museums attractions"
        if interest.lower() == "any":
            interest = ""
        return dict(city=shared_cities[0], cities=shared_cities, start=utc(start), end=utc(end),
                    budget=min(x["budget"] for x in prefs), interest=interest, kind=kind, windows=windows)

    def eligible(self, event, c):
        if c.get("windows"):
            return any(self.eligible(event, {**c, **w, "windows": []}) for w in c["windows"])
        if event.get("kind") in {"restaurant", "place"}:
            return (c.get("kind") in {"restaurant", "place"} and event["status"] == "candidate"
                    and (c["budget"] > 0 or c.get("kind") == "place")
                    and event["city"].lower() in {city.lower() for city in c.get("cities", [c["city"]])}
                    and self.now() < instant(event["start"])
                    and instant(c["start"]) <= instant(event["start"]) < instant(event["end"]) <= instant(c["end"]))
        return (c.get("kind", "event") == "event"
                and event["city"].lower() in {city.lower() for city in c.get("cities", [c["city"]])}
                and event["status"] == "onsale"
                and instant(c["start"]) <= instant(event["start"]) < instant(c["end"])
                and instant(event["start"]) > self.now()
                and (not event.get("end") or instant(event["end"]) <= instant(c["end"]))
                and event["price_max"] is not None and event["price_max"] <= c["budget"])

    async def execute(self, actor, cmd):
        t = self.t
        action = cmd.get("action")
        if action == "conversation":
            return await self.conversation(actor, cmd)
        if action == "help":
            if cmd.get("explicit"):
                return Reply(t(HELP, HELP_ZH))
            # The interpreter falls back to help for anything it can't place; a wall of commands doesn't help then.
            return Reply(t("I didn't catch that. Try status, search or edit, or just tell me your time, place and budget. Type help for all commands.",
                           "没太看懂。可以发 status、search 或 edit，或者直接告诉我时间、地点和预算。发 help 查看全部命令。"))
        if action == "new":
            self.leave_if_confirmed(actor)
            if self.store.current(actor):
                return await self.execute(actor, {**cmd, "action": "restart"})
            p = {"id": secrets.token_hex(12), "invite": invite_code(),
                 "creator": actor, "mode": self.provider.mode, "members": {actor: {"name": str(cmd.get("name", "Friend A"))[:40], "preferences": {}}},
                 "version": 0, "state": "draft", "events": [], "selected": None, "approvals": []}
            self.store.save(p)
            self.store.bind(actor, p)
            r = self.preferences_card(p)
            r.text = t(f"Created a new {self.provider.mode.upper()} plan. Ask your friend to send Mori this message, "
                       f"followed by their name:\njoin {p['invite']}\n(for example: join {p['invite']} Sam). The code works once.\n\n",
                       f"计划已创建（{self.provider.mode.upper()}）。让朋友给 Mori 发这条消息，后面加上自己的名字：\njoin {p['invite']}\n"
                       f"（例如：join {p['invite']} 小明）。邀请码只能用一次。\n\n") + r.text
            if cmd.get("preferences"):
                await self.execute(actor, {**cmd["preferences"], "action": "preferences", "defer_search": True})
                saved = self.store.current(actor)
                saved["search_requested"] = True
                self.store.save(saved)
                r.text += "\n" + self.details(saved)
            return r
        if action == "join":
            self.leave_if_confirmed(actor)
            if self.store.current(actor):
                raise UserError("You're already in a plan. Type status to see it, or leave to exit it first.",
                                "你已经在一个计划里了。发 status 查看，或先发 leave 离开。")
            code = str(cmd.get("code", "")).strip()
            p = next((json.loads(row[0]) for row in self.store.db.execute("SELECT body FROM plans")
                      if json.loads(row[0]).get("invite") in {code, code.upper()}), None)
            if not p or len(p["members"]) >= 2:
                raise UserError("That invite code doesn't match an open plan. Check it with your friend; each code works once.",
                                "这个邀请码对不上任何可加入的计划。请跟朋友核对一下，每个邀请码只能用一次。")
            p["members"][actor] = {"name": str(cmd.get("name", "Friend B"))[:40], "preferences": {}}
            p["invite"] = None
            self.reset(p)
            p["members"][actor]["seen"] = len(p.get("log", []))
            self.note(p, actor, "joined the plan", "加入了计划")
            self.store.save(p)
            self.store.bind(actor, p)
            return self.preferences_card(p)
        p = self.store.current(actor)
        if not p and action == "restart":
            return await self.execute(actor, {**cmd, "action": "new"})
        if not p or actor not in p["members"]:
            return Reply(t("You're not in a plan yet. Start one with new YourName, or join a friend's with join INVITE_CODE YourName.",
                           "你还没有加入计划。发 new 你的名字 新建一个，或用 join 邀请码 你的名字 加入朋友的计划。"))
        if p["mode"] != self.provider.mode:
            raise UserError("This plan belongs to another mode. Use a separate database for live and demo runs.")
        if cmd.get("plan_id", p["id"]) != p["id"]:
            raise UserError("That card belongs to a different plan. Type status.", "这张卡片属于另一个计划，请发 status 查看当前计划。")
        if "version" in cmd and action != "preferences" and int(cmd["version"]) != p["version"]:
            # Never act on an old card; show what it would be acting on now instead.
            r = await self.execute(actor, {"action": "status"})
            r.text = t("This plan changed since that card, so nothing was recorded. Here's the latest:\n",
                       "这张卡片之后计划有变化，这次点击没有记录。下面是最新情况：\n") + r.text
            return r
        if action == "restart":
            p["members"][actor]["preferences"] = {}
            self.reset(p)
            p["search_requested"] = False
            self.note(p, actor, "started over; their preferences, recommendations and approvals were reset", "重新开始了；自己的偏好、推荐和确认已重置")
            self.store.save(p)
            if cmd.get("preferences"):
                await self.execute(actor, {**cmd["preferences"], "action": "preferences", "defer_search": True})
                p = self.store.current(actor)
            r = self.preferences_card(p)
            r.text = t("Let's start again. I've cleared your preferences and the previous recommendations and approvals. You both remain in this plan; your friend's preferences are kept. No calendar entries or bookings were changed.\n\n",
                       "我们重新开始。已清空你的偏好、旧推荐和确认；你们仍在同一计划中，朋友的偏好已保留。没有修改日历或预订。\n\n") + r.text
            return r
        if action == "match_time":
            name = str(cmd.get("name") or "").strip().casefold()
            others = [m for a, m in p["members"].items() if a != actor]
            matches = [m for m in others if not name or m["name"].strip().casefold() == name]
            if len(matches) != 1:
                raise UserError("I couldn't identify that friend in this plan. Ask them to join and save their name and times first.",
                                "当前计划里无法确定这位朋友，请让对方先加入并保存名字和时间。")
            friend = matches[0]
            windows = time_windows(friend["preferences"])
            if not windows:
                raise UserError(f"{friend['name']} hasn't saved any times yet. They can choose time slots in their preferences card.",
                                f"{friend['name']} 还没有保存时间，可以让对方在偏好卡片里选择时段。")
            r = await self.execute(actor, {"action": "preferences", "windows": windows})
            r.text = t(f"Your time now matches {friend['name']}'s saved availability; your other preferences are unchanged.\n",
                       f"你的时间已改为 {friend['name']} 保存的空闲时段，其他偏好保持不变。\n") + r.text
            return r
        if action == "delete":
            if actor != p["creator"]:
                raise UserError("Only the creator can delete the shared plan. Type leave to exit it yourself.",
                                "只有创建者能删除计划。你可以发 leave 自己离开。")
            self.store.db.execute("DELETE FROM receipts WHERE plan=?", (p["id"],))
            self.store.db.execute("DELETE FROM current_plan WHERE plan=?", (p["id"],))
            self.store.db.execute("DELETE FROM plans WHERE id=?", (p["id"],))
            return Reply(t("Shared plan and stored message receipts deleted. No external calendar events were created or removed.",
                           "计划和相关记录已删除。没有创建或删除任何日历事件。"))
        if action == "leave":
            return self.leave(actor, p)
        if action == "forget":
            p["members"][actor]["preferences"] = {}
            self.reset(p)
            self.note(p, actor, "cleared their preferences; any proposal and approvals were reset", "清除了自己的偏好，方案和确认都已重置")
            self.store.db.execute("DELETE FROM receipts WHERE plan=?", (p["id"],))
            self.store.save(p)
            return Reply(t("Your preferences and cached replies for this plan were cleared. The proposal and approvals were reset.",
                           "你的偏好和相关记录已清除，方案和确认也已重置。"))
        if action == "preferences":
            if any(f"slot_{i}" in cmd for i in range(1, 4)):
                windows = []
                for i in range(1, 4):
                    value = cmd.get(f"slot_{i}")
                    if value and value != "none":
                        a, b = str(value).split("|")
                        windows.append({"start": a, "end": b})
                cmd = {**cmd, "windows": windows}
            name = str(cmd.get("name") or "").strip()[:40]
            if name:
                p["members"][actor]["name"] = name
            if not any(key in cmd for key in ("city", "start", "end", "budget", "interest", "kind", "windows")):
                self.store.save(p)
                return Reply(t(f"Thanks, {name}.", f"好的，{name}。") if name else t("Nothing to save. Fill in the card, or type help.", "没有要保存的内容。请填写卡片，或发 help。"))
            pref = dict(p["members"][actor]["preferences"])
            for key in ("city", "start", "end", "budget", "interest", "kind", "windows"):
                if key in cmd:
                    pref[key] = cmd[key]
            if "windows" in cmd:
                raw = cmd["windows"]
                if not isinstance(raw, list) or not 1 <= len(raw) <= 3:
                    raise UserError("Choose one to three time slots.", "请选择一到三个空闲时段。")
                windows = []
                for w in raw:
                    a, b = instant(w["start"]), instant(w["end"])
                    if not self.now() < a < b <= self.now() + timedelta(days=7):
                        raise UserError("Choose future time slots within seven days, with end after start.", "请选择未来七天内的时段，结束须晚于开始。")
                    window = {"start": utc(a), "end": utc(b)}
                    if window not in windows:
                        windows.append(window)
                pref["windows"] = sorted(windows, key=lambda w: w["start"])
                pref["start"], pref["end"] = pref["windows"][0]["start"], pref["windows"][0]["end"]
            elif "start" in cmd or "end" in cmd:
                pref.pop("windows", None)
            if pref.get("kind", "event") not in {"event", "restaurant", "place"}:
                raise UserError("Choose events, restaurants or places.")
            if "city" in pref:
                pref["city"] = " / ".join(cities(pref["city"]))
            if "budget" in pref:
                try:
                    pref["budget"] = float(re.sub(r"[$,\s]|usd", "", str(pref["budget"]), flags=re.I))
                except ValueError:
                    raise UserError("Enter a budget as a number of US dollars, e.g. 30.", "预算请填美元数字，例如 30。") from None
                if not math.isfinite(pref["budget"]) or not 0 <= pref["budget"] <= 10000:
                    raise UserError("Enter a budget between 0 and 10,000 USD.", "预算请填 0 到 10,000 美元之间。")
            if "interest" in pref:
                pref["interest"] = str(pref["interest"])[:120].strip()
                if not pref["interest"]:
                    raise UserError("Enter an interest (or 'any').", "请填一个兴趣（或 any 表示都行）。")
            for key in ("start", "end"):
                if key in pref:
                    d = instant(pref[key])
                    if d <= self.now() or d > self.now() + timedelta(days=7):
                        raise UserError("Choose a future time within the next seven days.", "请选择未来 7 天内的时间。")
                    pref[key] = utc(d)
            if "start" in pref and "end" in pref and instant(pref["start"]) >= instant(pref["end"]):
                raise UserError("End must be after start.", "结束时间要晚于开始时间。")
            p["members"][actor]["preferences"] = pref
            had_proposal = bool(p["selected"])
            self.reset(p)
            if had_proposal:
                self.note(p, actor, "changed their preferences, so the proposal and approvals were reset", "改了偏好，之前的方案和确认已清空")
            else:
                self.note(p, actor, "saved their preferences", "填好了偏好")
            self.store.save(p)
            text = t("Saved your preferences.", "已保存你的偏好。")
            if had_proposal:
                text += t(" The previous proposal and any approvals were reset.", "之前的方案和已有的确认都已清空。")
            if pref.get("budget") == 0 and pref.get("kind", "event") == "event":
                text += t(" A $0 budget means only free events will show.", "预算为 $0 时只会显示免费活动。")
            if not cmd.get("defer_search") and len(p["members"]) == 2 and all(complete(m["preferences"]) for m in p["members"].values()):
                p["search_requested"] = True
                self.store.save(p)
                try:
                    r = await self.execute(actor, {"action": "search"})
                    r.text = text + "\n" + r.text
                    return r
                except (UserError, ProviderError) as error:
                    # Saving valid preferences must survive a search failure.
                    return Reply(text + "\n" + (error.zh if isinstance(error, UserError) and self.lang == "zh" and error.zh else str(error)))
            return Reply(text + " " + self.next_step(p, actor))
        if action == "search":
            c = self.constraints(p)
            # Looking for alternatives keeps a confirmed plan until one of them is actually proposed.
            keep = p["state"] == "confirmed" and p["selected"]
            found = {}
            for w in c["windows"]:
                single = {**c, **w, "windows": []}
                for e in await self.provider.search(single):
                    if self.eligible(e, single) and not (keep and e["id"] == p["selected"]["id"]):
                        found.setdefault(e["id"], e)
            events = list(found.values())
            if not events and c.get("kind") in {"restaurant", "place"}:
                return Reply("No place candidates found in your shared city. Try another type of place or area. Prices and opening hours require checking; restaurant searches need a positive budget.")
            if not events:
                return Reply(t("No matches. ", "没有找到合适的活动。") + self.explain(p, c) +
                             t("\nEvents without a listed price are left out. Try a wider time window, a higher budget or another interest.",
                               "\n没有标价的活动不会显示。可以放宽时间、提高预算或换个兴趣。"))
            if keep:
                p["events"] = events[:3]
                p["browsing"] = True
                self.note(p, actor, f"is looking at alternatives; your confirmed plan for {p['selected']['title']} stays until one is proposed",
                          f"在看其他选项；在选定新活动之前，{p['selected']['title']} 的确认仍然有效")
            else:
                self.reset(p)
                p["events"] = events[:3]
                p["state"] = "options"
                self.note(p, actor, f"found {len(p['events'])} option(s); type status to see them", f"找到了 {len(p['events'])} 个选项，发 status 查看")
            p["search_requested"] = False
            self.store.save(p)
            r = self.options_card(p)
            r.text += "\n" + self.explain(p, c)
            if keep:
                r.text = t(f"Your confirmed plan for {p['selected']['title']} stays in place unless you propose one of these.\n",
                           f"在你提议其中一个之前，{p['selected']['title']} 的计划仍然保持确认。\n") + r.text
            return r
        if action == "select":
            event = next((e for e in p["events"] if e["id"] == cmd.get("event_id")), None)
            if not event:
                raise UserError("Select an event from your current results. Type search.", "请从当前结果里选一个活动，或发 search 重新搜索。")
            event = await self.provider.refresh(event)
            if not self.eligible(event, self.constraints(p)):
                raise UserError("This event no longer fits. Search for another; no approval was recorded.",
                                "这个活动已经不符合条件了，请重新搜索。这次没有记录任何确认。")
            replaced = p["selected"]["title"] if p["state"] == "confirmed" and p["selected"] else None
            p["version"] += 1
            p["selected"], p["approvals"], p["state"], p["browsing"] = event, [], "proposed", False
            if replaced:
                self.note(p, actor, f"proposed {event['title']} instead of the confirmed {replaced}; both of you need to confirm again",
                          f"提议改成 {event['title']}，替换已确认的 {replaced}；需要你们重新确认")
            else:
                self.note(p, actor, f"proposed {event['title']}; click Works for me if it suits you", f"提议了 {event['title']}，合适的话点“我可以”")
            self.store.save(p)
            return await self.display_summary(p)
        if action == "confirm":
            if not p["selected"]:
                raise UserError("Nothing has been proposed yet. Type search to see options.", "还没有提议任何活动。发 search 看看有什么选项。")
            if "version" not in cmd:
                r = await self.display_summary(p)
                if p["state"] != "confirmed":
                    r.text = t("Not approved yet: check this proposal, then click Works for me.\n", "还没确认：看一下这个方案，合适的话点“我可以”。\n") + r.text
                return r
            fresh = await self.provider.refresh(p["selected"])
            keys = ("title", "start", "end", "venue", "city", "price_min", "price_max", "status", "url", "price_level")
            if not self.eligible(fresh, self.constraints(p)):
                self.reset(p)
                self.store.save(p)
                return Reply(t("The event no longer fits your constraints. Approvals cleared; type search for alternatives.",
                               "这个活动已经不符合你们的条件，确认已清空。发 search 找找其他选项。"))
            if any(fresh.get(k) != p["selected"].get(k) for k in keys):
                p["selected"], p["approvals"] = fresh, []
                p["version"] += 1
                self.store.save(p)
                r = await self.display_summary(p)
                r.text = t("Event details changed. Both participants must review again.\n", "活动信息有变化，你们需要重新确认。\n") + r.text
                return r
            p["selected"] = fresh
            if actor not in p["approvals"]:
                p["approvals"].append(actor)
            p["state"] = "confirmed" if len(p["approvals"]) == 2 else "proposed"
            if p["state"] == "confirmed":
                self.note(p, actor, "approved, so the plan is confirmed!", "也确认了，计划定下来了！")
            else:
                self.note(p, actor, "said the proposal works for them", "表示这个方案可以")
            self.store.save(p)
            return await self.display_summary(p)
        if action == "edit":
            return self.preferences_card(p)
        if action in {"calendar", "event_link"}:
            if not p["selected"] or p["state"] != "confirmed":
                raise UserError("Both people need to confirm the plan first.", "需要两个人都确认后才能打开。")
            # ASI cards can become locked after one selection. Always expose both
            # destinations as ordinary links, independent of the consumed card.
            e = p["selected"]
            if e.get("kind") in {"restaurant", "place"}:
                return Reply("[Open calendar draft](" + self.calendar_url(e) + ")\n\n[View place on Google Maps](" + e["url"] + ")\n\n[Open map](" + e["map_url"] + ")\n\nNot booked. Check the listing for hours, admission or menu prices, accessibility and any booking requirements. The calendar time is a proposed visit, not confirmed availability.")
            links = {
                "calendar": f"[{t('Open calendar draft', '打开日历草稿')}](" + self.calendar_url(p["selected"]) + ")",
                "event_link": f"[{t('View event and tickets', '查看活动和购票页面')}](" + p["selected"]["url"] + ")",
            }
            other = "event_link" if action == "calendar" else "calendar"
            return Reply(links[action] + "\n\n" + links[other] +
                         t("\n\nSave the calendar draft yourself. Tickets aren't booked; check final fees and availability.",
                           "\n\n日历草稿需要你自己保存。门票没有预订，请自己确认最终价格和余票。"))
        if action == "status":
            if p["state"] == "confirmed" and p.get("browsing"):
                r = self.options_card(p)
                r.text = t(f"Your confirmed plan for {p['selected']['title']} still stands. Alternatives being considered:\n",
                           f"{p['selected']['title']} 的计划仍然有效。下面是正在考虑的其他选项：\n") + r.text
                return r
            if p["selected"]:
                return await self.display_summary(p)
            if p["state"] == "options" and p["events"]:
                r = self.options_card(p)
                r.text += "\n" + self.details(p)
                return r
            if complete(p["members"][actor]["preferences"]):
                return Reply(self.details(p) + "\n\n" + self.next_step(p, actor) + t(" Type edit to change yours.", "想修改的话发 edit。"))
            r = self.preferences_card(p)
            r.text = self.details(p) + "\n\n" + r.text
            return r
        raise UserError("Unsupported action. Type help.", "不支持这个操作，发 help 查看命令。")

    @staticmethod
    def photo_caption(e):
        if not e.get("image") or e.get("source") != "Google Maps":
            return ""
        names = ", ".join(a["name"] for a in e.get("photo_credits", []))
        return "Photo: " + (names + " · " if names else "") + "Google Maps"

    @staticmethod
    def photo_links(events):
        lines = []
        for e in events:
            if not e.get("image") or e.get("source") != "Google Maps":
                continue
            authors = []
            for a in e.get("photo_credits", []):
                name = a["name"].replace("[", "").replace("]", "")
                authors.append("[" + name + "](" + a["url"].replace(")", "%29") + ")" if a.get("url") else name)
            lines.append(e["title"] + " — Photo: " + (", ".join(authors) + " · " if authors else "") + "Google Maps")
        return "\n\n" + "\n".join(lines) if lines else ""

    def options_card(self, p):
        t, lang = self.t, self.lang
        return Reply(t("A little time together? Pick an idea to review with your friend.", "找到几个候选，选一个一起看看吧。费用和营业时间尚未核实，地点卡片时间只是建议，并不代表已确认开放或符合预算。" if p["events"][0].get("kind") in {"restaurant", "place"} else "给你们找了几个选项，选一个一起看看。标价不含未核实的手续费。") + self.photo_links(p["events"]), "carousel", {
            "title": t("Your next little adventure", "一起去玩的几个选择"),
            "style": "slide",
            "subtitle": "DEMO · fictional activities" if self.provider.mode == "demo" else ("Places to explore · Google Maps · verify prices & hours" if p["events"][0].get("kind") in {"restaurant", "place"} else "For your shared time & budget · Ticketmaster"),
            "items": [{"id": e["id"], "title": e["title"],
                       **({"image": e["image"]} if e.get("image") else {}),
                       "subtitle": f"{local(e['start'], lang)}\n{e['venue']} · {e['city']}",
                       "badges": [{"label": "Price unverified" if e.get("kind") in {"restaurant", "place"} else "Within shared budget", "variant": "info"},
                                  {"label": self.price_label(e), "variant": "info"}],
                       "secondary_text": ("Suggested visit · check hours & cost. " + self.photo_caption(e)) if e.get("kind") in {"restaurant", "place"} else "Fits your shared availability. Final fees and ticket availability need checking.",
                       "primary_cta": {"label": t("Let's consider this", "提议这个"), "selection": {"action": "select", "event_id": e["id"], "plan_id": p["id"], "version": p["version"]}}}
                      for e in p["events"]]})

    def price(self, amount):
        if not amount:
            return self.t("Free", "免费")
        return self.t(f"Listed up to {money(amount)}", f"标价最高 {money(amount)}")

    def note(self, p, actor, en, zh):
        p.setdefault("log", []).append({"actor": actor, "name": p["members"].get(actor, {}).get("name"), "text": en, "zh": zh})

    def catch_up(self, p, actor):
        # Agents cannot message the partner first, so their news rides on this person's next reply.
        me = p["members"][actor]
        log = p.get("log", [])
        news = [e for e in log[me.get("seen", 0):] if e["actor"] != actor]
        me["seen"] = len(log)
        if not news:
            return ""
        lines = [f"• {p['members'].get(e['actor'], {}).get('name') or e.get('name') or self.t('Your partner', '对方')} "
                 f"{e.get('zh') if self.lang == 'zh' and e.get('zh') else e['text']}" for e in news]
        lines = [line for i, line in enumerate(lines) if i == 0 or line != lines[i - 1]]
        return self.t("Since your last message:\n", "你上次发消息之后：\n") + "\n".join(lines) + "\n\n"

    async def conversation(self, actor, cmd):
        """Accumulate this speaker's facts and resume an explicitly requested search."""
        zh = self.lang == "zh"
        p = self.store.current(actor)
        if not p:
            if cmd.get("search") is True or cmd.get("preferences"):
                return await self.execute(actor, {"action": "new", "preferences": cmd.get("preferences", {})})
            return Reply("说“帮我们安排一次出门”即可开始，或者把朋友的邀请码发给我。" if zh else
                         "Say ‘create a plan’ to get started, or send your friend's invite code.")
        if p["mode"] != self.provider.mode:
            raise UserError("This plan belongs to another mode. Use a separate database for live and demo runs.")
        prefs = cmd.get("preferences", {})
        if not isinstance(prefs, dict):
            raise UserError("Please describe your availability, city or budget again.", "请再说一下你的时间、城市或预算。")
        mine = p["members"][actor]["preferences"]
        # The model restates unchanged facts in other formats; only a real change may reset the proposal.
        updates = {k: v for k, v in prefs.items() if k in REQUIRED | {"kind", "windows"} and not self.same_preference(k, v, mine.get(k))}
        had_proposal = bool(p["selected"])
        if updates:
            await self.execute(actor, {**updates, "action": "preferences", "defer_search": True})
            p = self.store.current(actor)
        if cmd.get("search") is True or updates:
            p["search_requested"] = True
        self.store.save(p)
        if cmd.get("pick_times"):
            reply = self.preferences_card(p)
            reply.text = self.t("Please choose future times below; I kept your other details.\n", "请点选下面的未来时段，其他信息已记下。\n") + reply.text
        else:
            reply = await self.conversation_reply(p, actor, cmd, zh)
        if updates and had_proposal:
            reply.text = ("注意：你的偏好有变化，之前的方案和已有的确认都已清空。\n" if zh else
                          "Heads up: your preferences changed, so the current proposal and any approvals were reset.\n") + reply.text
        return reply

    @staticmethod
    def same_preference(key, new, old):
        if old is None:
            return False
        try:
            if key == "windows":
                normalize = lambda values: sorted(set((utc(instant(w["start"])), utc(instant(w["end"]))) for w in values))
                return normalize(new) == normalize(old)
            if key == "city":
                return cities(new) == cities(old)
            if key in ("start", "end"):
                # Stored times are whole seconds (see utc()).
                return instant(new).replace(microsecond=0) == instant(old)
            if key == "budget":
                return float(re.sub(r"[$,\s]|usd", "", str(new), flags=re.I)) == float(old)
        except (ValueError, TypeError, KeyError):
            return False
        return str(new).strip().lower() == str(old).strip().lower()

    async def conversation_reply(self, p, actor, cmd, zh):
        question = cmd.get("question")
        if isinstance(question, str) and question.strip():
            return Reply(question.strip()[:400])
        labels = {"city": "城市" if zh else "city", "start": "开始有空的时间" if zh else "available from",
                  "end": "结束时间" if zh else "available until", "budget": "每人预算上限" if zh else "budget per person",
                  "interest": "想做什么" if zh else "activity interests"}
        missing = [label for key, label in labels.items() if key not in p["members"][actor]["preferences"]]
        if missing and not time_windows(p["members"][actor]["preferences"]):
            return self.preferences_card(p)
        if missing:
            return Reply(("记下了。还需要你的" + "、".join(missing) + "，直接用平常说话的方式告诉我就好。") if zh else
                         "Got it. I still need your " + ", ".join(missing) + ". You can tell me in your own words.")
        if p.get("search_requested"):
            try:
                self.constraints(p)
            except UserError as error:
                return Reply(("已记下你的信息。" + (error.zh or str(error)) + "对方可以直接用平常说话的方式回复。") if zh else
                             "I've saved your preferences. " + str(error) + " They can reply in their own words.")
            try:
                return await self.execute(actor, {"action": "search"})
            except ProviderError:
                return Reply(self.t("Your preferences are saved, but search is temporarily unavailable. Ask me to try again.",
                                    "偏好已保存，但搜索服务暂时不可用。稍后让我再试一次即可。"))
        if p["selected"]:
            return await self.display_summary(p)
        return Reply("记下你的偏好了。想看推荐时，直接问我附近有什么好玩的就行。" if zh else
                     "I've saved your preferences. Ask me what's on nearby whenever you'd like recommendations.")

    def details(self, p):
        lines = []
        for m in p["members"].values():
            x = m["preferences"]
            parts = [x["city"]] if "city" in x else []
            if "start" in x and "end" in x:
                parts.append(" / ".join(local_range(w["start"], w["end"], self.lang) for w in time_windows(x)))
            if "budget" in x:
                parts.append(self.t(f"up to ${x['budget']:g}", f"每人最多 ${x['budget']:g}"))
            if "interest" in x:
                parts.append(x["interest"])
            lines.append(f"{m['name']}: " + (" · ".join(parts) if parts else self.t("hasn't shared preferences yet", "还没填写偏好")))
        return "\n".join(lines)

    def next_step(self, p, actor):
        t = self.t
        if len(p["members"]) < 2:
            if p.get("invite"):
                return t(f"Waiting for your friend to join with: join {p['invite']}", f"等朋友用这条消息加入：join {p['invite']}")
            return t("Waiting for your friend to join.", "等朋友加入。")
        waiting = [m["name"] for a, m in p["members"].items() if not complete(m["preferences"])]
        if waiting:
            return t(f"Waiting for {', '.join(waiting)} to share their preferences.", f"等 {'、'.join(waiting)} 填写偏好。")
        return t("Your preferences are ready. I will look for shared options when both of you are ready.", "偏好已就绪，双方信息齐全后我会自动找推荐。")

    def explain(self, p, c):
        t, lang = self.t, self.lang
        prefs = [m["preferences"] for m in p["members"].values()]
        where = t(" and ", "和").join(c.get("cities", [c["city"]]))
        text = t(f"Searched {where}, {local_range(c['start'], c['end'])}, up to ${c['budget']:g} per person",
                 f"搜索范围：{where}，{local_range(c['start'], c['end'], lang)}，每人最多 ${c['budget']:g}")
        if len({x["budget"] for x in prefs}) > 1:
            text += t(" (the lower of your two budgets)", "（取你们两人中较低的预算）")
        text += t(".", "。")
        if len(c.get("windows", [])) > 1:
            text += t(" All shared windows: ", "所有共同空闲时段：") + " / ".join(local_range(w["start"], w["end"], lang) for w in c["windows"])
        interests = [x["interest"] for x in prefs]
        if c["interest"]:
            text += t(f" Interest: {c['interest']}.", f"兴趣：{c['interest']}。")
        elif len({i.lower() for i in interests}) > 1 and "any" not in {i.lower() for i in interests}:
            text += t(f" Your interests differ ({' / '.join(interests)}), so all activities were included.",
                      f"你们的兴趣不同（{' / '.join(interests)}），所以搜索了所有类型的活动。")
        if c.get("kind") in {"restaurant", "place"}:
            text += " This is your target budget, not a verified cost. Suggested visit time only; check hours and entry requirements. Google Maps. Search terms do not verify atmosphere or suitability."
        if c["budget"] == 0 and c.get("kind", "event") == "event":
            text += t(" A $0 budget means free events only.", "预算为 $0，只显示免费活动。")
        return text

    def leave_if_confirmed(self, actor):
        # A confirmed plan is finished business; starting or joining another just moves on from it.
        p = self.store.current(actor)
        if p and p["state"] == "confirmed":
            self.store.db.execute("DELETE FROM current_plan WHERE actor=?", (actor,))

    def leave(self, actor, p):
        t = self.t
        self.store.db.execute("DELETE FROM current_plan WHERE actor=?", (actor,))
        others = [m["name"] for a, m in p["members"].items() if a != actor]
        if p["state"] == "confirmed":
            self.note(p, actor, "left this conversation; the confirmed plan still stands", "离开了对话；已确认的计划仍然有效")
            self.store.save(p)
            return Reply(t(f"You've left. The plan stays confirmed for {', '.join(others) or 'your friend'}. Type new YourName to start another.",
                           f"你已离开。{'、'.join(others) or '对方'}那边的计划仍然有效。发 new 你的名字 可以开始新计划。"))
        del p["members"][actor]
        if not p["members"]:
            self.store.db.execute("DELETE FROM receipts WHERE plan=?", (p["id"],))
            self.store.db.execute("DELETE FROM plans WHERE id=?", (p["id"],))
            return Reply(t("You've left, and the empty plan was deleted.", "你已离开，空计划已删除。"))
        if p["creator"] == actor:
            p["creator"] = next(iter(p["members"]))
        self.reset(p)
        p["invite"] = invite_code()
        p.setdefault("log", []).append({"actor": actor, "name": None, "text": f"left the plan. New invite for someone else: join {p['invite']}",
                                        "zh": f"离开了计划。可以邀请别人：join {p['invite']}"})
        p["log"][-1]["name"] = next((e["name"] for e in reversed(p["log"]) if e["actor"] == actor and e.get("name")), None)
        self.store.save(p)
        return Reply(t("You've left the plan. Type new YourName to start another.", "你已离开这个计划。发 new 你的名字 可以开始新计划。"))

    @staticmethod
    def price_label(e):
        if e.get("kind") in {"restaurant", "place"}:
            return e.get("price_level", "Price unknown") + " · exact cost unverified"
        return "Free" if e["price_max"] == 0 else f"Up to ${e['price_max']:.2f} / person"

    async def display_summary(self, p):
        e = p["selected"]
        if e and e["id"].startswith("google:") and not e.get("image"):
            try:
                fresh = await self.provider.refresh(e)
                # Backfill visual metadata only; never alter the agreed facts or approvals.
                if fresh.get("image"):
                    for key in ("image", "photo_credits", "source"):
                        if key in fresh:
                            e[key] = fresh[key]
                    self.store.save(p)
            except ProviderError:
                pass
        return self.summary(p)

    def summary(self, p):
        t, lang = self.t, self.lang
        e = p["selected"]
        approved = [p["members"][a]["name"] for a in p["approvals"]]
        when = local(e["start"], lang)
        members = " · ".join(f"{m['name']} {'✓' if a in p['approvals'] else t('— waiting', '— 等待中')}" for a, m in p["members"].items())
        rows = [{"label": t("Activity", "活动"), "value": e["title"]},
                {"label": t("Suggested visit time", "建议到访时间") if e.get("kind") in {"restaurant", "place"} else t("When", "时间"), "value": when},
                {"label": t("Where", "地点"), "value": e["venue"]},
                {"label": t("Per person", "每人"), "value": self.price_label(e)},
                {"label": t("Who's in", "参与者"), "value": members},
                {"label": t("Visit details", "到访信息") if e.get("kind") in {"restaurant", "place"} else t("Tickets", "门票"), "value": e["availability"] if e.get("kind") in {"restaurant", "place"} else "Not booked · check availability and final price"}]
        if self.photo_caption(e):
            rows.append({"label": t("Photo", "照片"), "value": self.photo_caption(e)})
        text = "DEMO · " if e["demo"] else ""
        if p["state"] == "confirmed":
            if not e.get("end"):
                rows.append({"label": t("Calendar duration", "日历时长"), "value": "1-hour placeholder; event end time is unknown"})
            return Reply(text + t("You're both in! Your shared plan is ready.", "你们都确认了！计划定好了。") + self.photo_links([e]), "detail", {
                "title": t("You're both in ✓", "计划已确认 ✓"), "summary_rows": rows,
                **({"hero_image": e["image"]} if e.get("image") else {}),
                "ctas": [
                    {"label": t("Add to calendar", "加入日历"), "primary": True, "selection": {"action": "calendar", "plan_id": p["id"], "version": p["version"]}},
                    {"label": t("View place & map", "查看地点和地图") if e.get("kind") in {"restaurant", "place"} else t("View event & tickets", "查看活动和购票"), "selection": {"action": "event_link", "plan_id": p["id"], "version": p["version"]}}]})
        waiting = [m["name"] for a, m in p["members"].items() if a not in p["approvals"]]
        if approved:
            text += t(f"{', '.join(approved)} is in. Waiting for {', '.join(waiting)}.", f"{'、'.join(approved)} 已确认，等 {'、'.join(waiting)}。")
        else:
            text += t("Found something for you two. Does this work?", "给你们找到一个活动，看看合不合适？")
        return Reply(text, "review", {
            "title": t(f"{len(approved)}/2 confirmed · Your shared plan", f"已确认 {len(approved)}/2 · 你们的计划"),
            "summary_rows": rows,
            "approve_cta": {"label": t("Works for me", "我可以"), "selection": {"action": "confirm", "plan_id": p["id"], "version": p["version"]}},
            "reject_cta": {"label": t("Edit my preferences", "修改我的偏好"), "selection": {"action": "edit", "plan_id": p["id"]}}})

    @staticmethod
    def calendar_url(e):
        start = instant(e["start"])
        end = instant(e["end"]) if e.get("end") else start + timedelta(hours=1)
        params = {"action": "TEMPLATE", "text": e["title"], "location": e["venue"],
                  "dates": start.strftime("%Y%m%dT%H%M%SZ") + "/" + end.strftime("%Y%m%dT%H%M%SZ"),
                  "details": e["url"] + ("\nNot booked. Suggested visit time; verify opening hours, admission/menu prices and suitability." if e.get("kind") in {"restaurant", "place"} else "\nNot booked. Verify fees and inventory.") +
                  ("\nEnd time is a 1-hour placeholder." if not e.get("end") else "")}
        return "https://calendar.google.com/calendar/render?" + urlencode(params)


def principal(sender, session):
    # Do not trust claimed names/actor IDs in model text or card payloads.
    return hashlib.sha256(f"{sender}:{session}".encode()).hexdigest()
