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


class UserError(ValueError):
    """A message written for the person in the chat, safe to show as-is."""


def cities(value):
    aliases = {"berkeley": "Berkeley", "伯克利": "Berkeley", "san francisco": "San Francisco",
               "san fran": "San Francisco", "sf": "San Francisco", "旧金山": "San Francisco"}
    parts = value if isinstance(value, list) else re.split(r"\s*(?:/|,|、|\bor\b|\band\b|&)\s*", str(value).lower())
    result = set()
    for part in parts:
        city = aliases.get(str(part).strip().lower())
        if not city:
            raise UserError("Which locations work for you: Berkeley, San Francisco, or both? This pilot searches those two cities.")
        result.add(city)
    if not result:
        raise UserError("Choose Berkeley, San Francisco, or both.")
    return sorted(result)


def instant(value):
    text = str(value).strip().replace("Z", "+00:00")
    # Accept "2026-11-03 2pm" / "2026-11-03 2:30 pm" as well as ISO 8601.
    m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})[ T](\d{1,2})(?::(\d{2}))?\s*([ap]m)", text, re.I)
    if m:
        if not 1 <= int(m[2]) <= 12:
            raise UserError("For am/pm times, use an hour from 1 to 12, e.g. 2pm.")
        hour = int(m[2]) % 12 + (12 if m[4].lower() == "pm" else 0)
        text = f"{m[1]}T{hour:02d}:{m[3] or '00'}"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise UserError(f"I couldn't read the time '{value}'. Use a date and time like 2026-11-03 14:00 (Pacific time).") from None
    if dt.tzinfo is None:
        if dt.replace(tzinfo=PILOT_TZ, fold=0).utcoffset() != dt.replace(tzinfo=PILOT_TZ, fold=1).utcoffset():
            raise UserError("This local time falls during a daylight-saving clock change. Include an explicit UTC offset.")
        dt = dt.replace(tzinfo=PILOT_TZ)
    return dt.astimezone(timezone.utc)


def utc(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local(value):
    return instant(value).astimezone(PILOT_TZ).strftime("%a, %b %d · %I:%M %p %Z")


def local_range(start, end):
    a, b = instant(start).astimezone(PILOT_TZ), instant(end).astimezone(PILOT_TZ)
    if a.date() == b.date():
        return f"{a.strftime('%a, %b %d · %I:%M %p')} – {b.strftime('%I:%M %p %Z')}"
    return f"{local(start)} – {local(end)}"


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


class Store:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, body TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS current_plan(actor TEXT PRIMARY KEY, plan TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS receipts(actor TEXT, msg TEXT, plan TEXT, body TEXT, PRIMARY KEY(actor,msg))")

    def current(self, actor):
        row = self.db.execute("SELECT body FROM plans WHERE id=(SELECT plan FROM current_plan WHERE actor=?)", (actor,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, plan):
        self.db.execute("INSERT OR REPLACE INTO plans VALUES(?,?)", (plan["id"], json.dumps(plan)))

    def bind(self, actor, plan):
        self.db.execute("INSERT OR REPLACE INTO current_plan VALUES(?,?)", (actor, plan["id"]))


class Planner:
    def __init__(self, store, provider, interpreter=None, now=None):
        self.store, self.provider = store, provider
        self.interpreter = interpreter or ASIInterpreter()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock = asyncio.Lock()

    async def handle(self, actor, text, message_id=None):
        if len(text) > 8000:
            return Reply("Please shorten your message to 8,000 characters.")
        # Serialize state updates, including network refreshes. One-process prototype.
        async with self.lock:
            if message_id:
                old = self.store.db.execute("SELECT body FROM receipts WHERE actor=? AND msg=?", (actor, message_id)).fetchone()
                if old:
                    return Reply(**json.loads(old[0]))
            try:
                command = await self.parse(actor, text)
                result = await self.execute(actor, command)
                plan = self.store.current(actor)
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
                return Reply(str(error) if isinstance(error, (UserError, ProviderError)) else "I couldn't understand that. Use the card, or type help for examples.")
            except Exception:
                self.store.db.rollback()
                raise

    async def parse(self, actor, text):
        text = text.strip()
        # ASI:One may retain the direct recipient mention in TextContent.
        # Strip only a leading mention, never extract commands from arbitrary prose.
        text = re.sub(r"^@[A-Za-z0-9_-]+\s+", "", text, count=1).strip()
        if text.startswith("{"):
            obj = json.loads(text)
            if not isinstance(obj, dict):
                raise UserError("Expected a command object.")
            return obj
        verb, _, rest = text.partition(" ")
        verb = verb.lower()
        if verb in {"help", "status", "edit", "search", "forget", "delete", "leave"} and not rest:
            return {"action": verb}
        if verb == "new":
            return {"action": verb, "name": rest or "Friend A"}
        if verb == "join":
            code, _, name = rest.partition(" ")
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
        if command.get("action") == "review":
            # Natural-language assent opens the exact proposal; the card records approval.
            return {"action": "confirm"}
        if command.get("action") == "choose":
            number = command.get("number")
            if not plan or type(number) is not int or not 1 <= number <= len(plan["events"]):
                raise UserError("Which option do you mean? Choose an activity from the current cards.")
            return {"action": "select", "event_id": plan["events"][number - 1]["id"], "version": plan["version"]}
        return command

    def reset(self, p):
        p["version"] += 1
        p["approvals"] = []
        p["selected"] = None
        p["events"] = []
        p["state"] = "draft"

    def preferences_card(self, p):
        return Reply("Enter YOUR availability and preferences; these are shared with your planning partner. Times are Pacific unless you add an offset.", "form", {
            "title": "Make room for a little together-time",
            "fields": [
                {"name": "kind", "kind": "select", "label": "What would you like to explore?", "required": False, "options": [{"value": "event", "label": "Activities"}, {"value": "restaurant", "label": "Restaurants"}, {"value": "place", "label": "Places to explore"}]},
                {"name": "name", "kind": "text", "label": "Your name (shown to your friend)", "required": False},
                {"name": "city", "kind": "select", "label": "City", "required": True,
                 "options": [{"value": s, "label": s} for s in ["Berkeley", "San Francisco", "Berkeley / San Francisco"]]},
                {"name": "start", "kind": "text", "label": "Available from (e.g. 2026-11-03 14:00, Pacific time)", "required": True},
                {"name": "end", "kind": "text", "label": "Available until (e.g. 2026-11-03 18:00, Pacific time)", "required": True},
                {"name": "budget", "kind": "number", "label": "Maximum USD per person", "required": True},
                {"name": "interest", "kind": "text", "label": "Activity, cuisine or interest", "required": True}],
            # No version: preferences are per person, so a partner's update must not void this card.
            "submit_cta": {"label": "Save my preferences", "selection": {
                "action": "preferences", "plan_id": p["id"]}}})

    def constraints(self, p):
        if len(p["members"]) != 2:
            raise UserError("Invite a second participant first. Share the invite code privately.")
        people = list(p["members"].values())
        required = {"city", "start", "end", "budget", "interest"}
        for m in people:
            if not required.issubset(m["preferences"]):
                raise UserError(f"{m['name']} still needs to enter their city, availability, budget and interest.")
        prefs = [m["preferences"] for m in people]
        shared_cities = sorted(set(cities(prefs[0]["city"])) & set(cities(prefs[1]["city"])))
        if not shared_cities:
            raise UserError("You chose different cities. Agree on Berkeley or San Francisco, then update your preferences.")
        start = max(instant(x["start"]) for x in prefs)
        end = min(instant(x["end"]) for x in prefs)
        if start <= self.now() or start >= end:
            raise UserError("No future overlapping time. Update your availability.")
        kinds = {x["kind"] for x in prefs if x.get("kind")}
        if len(kinds) > 1:
            raise UserError("You chose different outing types. Agree on an event, restaurant or place to explore first.")
        kind = next(iter(kinds), "event")
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
                    budget=min(x["budget"] for x in prefs), interest=interest, kind=kind)

    def eligible(self, event, c):
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
        action = cmd.get("action")
        if action == "conversation":
            return await self.conversation(actor, cmd)
        if action == "help":
            return Reply(HELP)
        if action == "new":
            self.leave_if_confirmed(actor)
            if self.store.current(actor):
                raise UserError("You're already in a plan. Type status to see it, or leave to exit it first.")
            p = {"id": secrets.token_hex(12), "invite": invite_code(),
                 "creator": actor, "mode": self.provider.mode, "members": {actor: {"name": str(cmd.get("name", "Friend A"))[:40], "preferences": {}}},
                 "version": 0, "state": "draft", "events": [], "selected": None, "approvals": []}
            self.store.save(p)
            self.store.bind(actor, p)
            r = self.preferences_card(p)
            r.text = (f"Created a new {self.provider.mode.upper()} plan. Ask your friend to send Mori this message, "
                      f"followed by their name:\njoin {p['invite']}\n(for example: join {p['invite']} Sam). The code works once.\n\n") + r.text
            return r
        if action == "join":
            self.leave_if_confirmed(actor)
            if self.store.current(actor):
                raise UserError("You're already in a plan. Type status to see it, or leave to exit it first.")
            code = str(cmd.get("code", "")).strip()
            p = next((json.loads(row[0]) for row in self.store.db.execute("SELECT body FROM plans")
                      if json.loads(row[0]).get("invite") in {code, code.upper()}), None)
            if not p or len(p["members"]) >= 2:
                raise UserError("Invalid or already used invite.")
            p["members"][actor] = {"name": str(cmd.get("name", "Friend B"))[:40], "preferences": {}}
            p["invite"] = None
            self.reset(p)
            p["members"][actor]["seen"] = len(p.get("log", []))
            self.note(p, actor, "joined the plan")
            self.store.save(p)
            self.store.bind(actor, p)
            return self.preferences_card(p)
        p = self.store.current(actor)
        if not p or actor not in p["members"]:
            return Reply("Start with 'new YourName', or 'join INVITE_CODE YourName'.\n" + HELP)
        if p["mode"] != self.provider.mode:
            raise UserError("This plan belongs to another mode. Use a separate database for live and demo runs.")
        if cmd.get("plan_id", p["id"]) != p["id"]:
            raise UserError("That card belongs to a different plan. Type status.")
        if "version" in cmd and action != "preferences" and int(cmd["version"]) != p["version"]:
            # Never act on an old card; show what it would be acting on now instead.
            r = await self.execute(actor, {"action": "status"})
            r.text = "This plan changed since that card, so nothing was recorded. Here's the latest:\n" + r.text
            return r
        if action == "delete":
            if actor != p["creator"]:
                raise UserError("Only the creator can delete the shared plan. Type leave to exit it yourself.")
            self.store.db.execute("DELETE FROM receipts WHERE plan=?", (p["id"],))
            self.store.db.execute("DELETE FROM current_plan WHERE plan=?", (p["id"],))
            self.store.db.execute("DELETE FROM plans WHERE id=?", (p["id"],))
            return Reply("Shared plan and stored message receipts deleted. No external calendar events were created or removed.")
        if action == "leave":
            return self.leave(actor, p)
        if action == "forget":
            p["members"][actor]["preferences"] = {}
            self.reset(p)
            self.note(p, actor, "cleared their preferences; any proposal and approvals were reset")
            self.store.db.execute("DELETE FROM receipts WHERE plan=?", (p["id"],))
            self.store.save(p)
            return Reply("Your preferences and cached replies for this plan were cleared. The proposal and approvals were reset.")
        if action == "preferences":
            name = str(cmd.get("name") or "").strip()[:40]
            if name:
                p["members"][actor]["name"] = name
            if not any(key in cmd for key in ("city", "start", "end", "budget", "interest", "kind")):
                self.store.save(p)
                return Reply(f"Thanks, {name}." if name else "Nothing to save. Fill in the card, or type help.")
            pref = dict(p["members"][actor]["preferences"])
            for key in ("city", "start", "end", "budget", "interest", "kind"):
                if key in cmd:
                    pref[key] = cmd[key]
            if pref.get("kind", "event") not in {"event", "restaurant", "place"}:
                raise UserError("Choose events, restaurants or places.")
            if "city" in pref:
                pref["city"] = " / ".join(cities(pref["city"]))
            if "budget" in pref:
                try:
                    pref["budget"] = float(re.sub(r"[$,\s]|usd", "", str(pref["budget"]), flags=re.I))
                except ValueError:
                    raise UserError("Enter a budget as a number of US dollars, e.g. 30.") from None
                if not math.isfinite(pref["budget"]) or not 0 <= pref["budget"] <= 10000:
                    raise UserError("Enter a budget between 0 and 10,000 USD.")
            if "interest" in pref:
                pref["interest"] = str(pref["interest"])[:120].strip()
                if not pref["interest"]:
                    raise UserError("Enter an interest (or 'any').")
            for key in ("start", "end"):
                if key in pref:
                    d = instant(pref[key])
                    if d <= self.now() or d > self.now() + timedelta(days=7):
                        raise UserError("Choose a future time within the next seven days.")
                    pref[key] = utc(d)
            if "start" in pref and "end" in pref and instant(pref["start"]) >= instant(pref["end"]):
                raise UserError("End must be after start.")
            p["members"][actor]["preferences"] = pref
            had_proposal = bool(p["selected"])
            self.reset(p)
            self.note(p, actor, "changed their preferences, so the proposal and approvals were reset" if had_proposal else "saved their preferences")
            self.store.save(p)
            text = "Saved your preferences."
            if had_proposal:
                text += " The previous proposal and any approvals were reset."
            if pref.get("budget") == 0:
                text += " A $0 budget means only free events will show."
            return Reply(text + " " + self.next_step(p, actor))
        if action == "search":
            c = self.constraints(p)
            events = [e for e in await self.provider.search(c) if self.eligible(e, c)]
            if not events and c.get("kind") in {"restaurant", "place"}:
                return Reply("No place candidates found in your shared city. Try another type of place or area. Prices and opening hours require checking; restaurant searches need a positive budget.")
            if not events:
                return Reply("No matches. " + self.explain(p, c) + "\nEvents without a listed price are left out. Try a wider time window, a higher budget or another interest.")
            self.reset(p)
            p["events"] = events[:3]
            p["state"] = "options"
            self.note(p, actor, f"found {len(p['events'])} option(s); type status to see them")
            self.store.save(p)
            p["search_requested"] = False
            self.store.save(p)
            r = self.options_card(p)
            r.text += "\n" + self.explain(p, c)
            return r
        if action == "select":
            event = next((e for e in p["events"] if e["id"] == cmd.get("event_id")), None)
            if not event:
                raise UserError("Select an event from your current results. Type search.")
            event = await self.provider.refresh(event)
            if not self.eligible(event, self.constraints(p)):
                raise UserError("This event no longer fits. Search for another; no approval was recorded.")
            p["version"] += 1
            p["selected"], p["approvals"], p["state"] = event, [], "proposed"
            self.note(p, actor, f"proposed {event['title']}; click Works for me if it suits you")
            self.store.save(p)
            return self.summary(p)
        if action == "confirm":
            if not p["selected"]:
                raise UserError("Nothing has been proposed yet. Type search to see options.")
            if "version" not in cmd:
                r = self.summary(p)
                r.text = "Not approved yet: check this proposal, then click Works for me.\n" + r.text
                return r
            fresh = await self.provider.refresh(p["selected"])
            keys = ("title", "start", "end", "venue", "city", "price_min", "price_max", "status", "url", "price_level")
            if not self.eligible(fresh, self.constraints(p)):
                self.reset(p)
                self.store.save(p)
                return Reply("The event no longer fits your constraints. Approvals cleared; type search for alternatives.")
            if any(fresh.get(k) != p["selected"].get(k) for k in keys):
                p["selected"], p["approvals"] = fresh, []
                p["version"] += 1
                self.store.save(p)
                r = self.summary(p)
                r.text = "Event details changed. Both participants must review again.\n" + r.text
                return r
            p["selected"] = fresh
            if actor not in p["approvals"]:
                p["approvals"].append(actor)
            p["state"] = "confirmed" if len(p["approvals"]) == 2 else "proposed"
            self.note(p, actor, "approved, so the plan is confirmed!" if p["state"] == "confirmed" else "said the proposal works for them")
            self.store.save(p)
            return self.summary(p)
        if action == "edit":
            return self.preferences_card(p)
        if action in {"calendar", "event_link"}:
            if not p["selected"] or p["state"] != "confirmed":
                raise UserError("Both people need to confirm the plan first.")
            # ASI cards can become locked after one selection. Always expose both
            # destinations as ordinary links, independent of the consumed card.
            e = p["selected"]
            if e.get("kind") in {"restaurant", "place"}:
                return Reply("[Open calendar draft](" + self.calendar_url(e) + ")\n\n[View place on Google Maps](" + e["url"] + ")\n\n[Open map](" + e["map_url"] + ")\n\nNot booked. Check the listing for hours, admission or menu prices, accessibility and any booking requirements. The calendar time is a proposed visit, not confirmed availability.")
            links = {
                "calendar": "[Open calendar draft](" + self.calendar_url(p["selected"]) + ")",
                "event_link": "[View event and tickets](" + p["selected"]["url"] + ")",
            }
            other = "event_link" if action == "calendar" else "calendar"
            return Reply(links[action] + "\n\n" + links[other] +
                         "\n\nSave the calendar draft yourself. Tickets aren't booked; check final fees and availability.")
        if action == "status":
            if p["selected"]:
                return self.summary(p)
            if p["state"] == "options" and p["events"]:
                return self.options_card(p)
            if {"city", "start", "end", "budget", "interest"}.issubset(p["members"][actor]["preferences"]):
                return Reply(self.details(p) + "\n\n" + self.next_step(p, actor) + " Type edit to change yours.")
            r = self.preferences_card(p)
            r.text = self.details(p) + "\n\n" + r.text
            return r
        raise UserError("Unsupported action. Type help.")

    def options_card(self, p):
        return Reply("A little time together? Pick an idea to review with your friend.", "carousel", {
            "title": "Your next little adventure",
            "style": "slide",
            "subtitle": "DEMO · fictional activities" if self.provider.mode == "demo" else ("Places to explore · Google Maps · verify prices & hours" if p["events"][0].get("kind") in {"restaurant", "place"} else "For your shared time & budget · Ticketmaster"),
            "items": [{"id": e["id"], "title": e["title"],
                       **({"image": e["image"]} if e.get("image") else {}),
                       "subtitle": f"{local(e['start'])}\n{e['venue']} · {e['city']}",
                       "badges": [{"label": "Price unverified" if e.get("kind") in {"restaurant", "place"} else "Within shared budget", "variant": "info"},
                                  {"label": self.price_label(e), "variant": "info"}],
                       "secondary_text": e.get("availability") if e.get("kind") in {"restaurant", "place"} else "Fits your shared availability. Final fees and ticket availability need checking.",
                       "primary_cta": {"label": "Let's consider this", "selection": {"action": "select", "event_id": e["id"], "plan_id": p["id"], "version": p["version"]}}}
                      for e in p["events"]]})

    def note(self, p, actor, text):
        p.setdefault("log", []).append({"actor": actor, "name": p["members"].get(actor, {}).get("name"), "text": text})

    def catch_up(self, p, actor):
        # Agents cannot message the partner first, so their news rides on this person's next reply.
        me = p["members"][actor]
        log = p.get("log", [])
        news = [e for e in log[me.get("seen", 0):] if e["actor"] != actor]
        me["seen"] = len(log)
        if not news:
            return ""
        lines = [f"• {p['members'].get(e['actor'], {}).get('name') or e.get('name') or 'Your partner'} {e['text']}" for e in news]
        lines = [line for i, line in enumerate(lines) if i == 0 or line != lines[i - 1]]
        return "Since your last message:\n" + "\n".join(lines) + "\n\n"

    async def conversation(self, actor, cmd):
        """Accumulate this speaker's facts and resume an explicitly requested search."""
        zh = cmd.get("language") == "zh"
        p = self.store.current(actor)
        if not p:
            return Reply("可以帮你们找活动或餐厅。先创建一个双人计划，或用朋友的邀请码加入；加入后直接说时间、地点和预算即可。" if zh else
                         "I can help you find something to do. First create a shared plan or join your friend's invite; then tell me your availability, city and budget in your own words.")
        if p["mode"] != self.provider.mode:
            raise UserError("This plan belongs to another mode. Use a separate database for live and demo runs.")
        prefs = cmd.get("preferences", {})
        if not isinstance(prefs, dict):
            raise UserError("Please describe your availability, city or budget again.")
        allowed = {"city", "start", "end", "budget", "interest", "kind"}
        mine = p["members"][actor]["preferences"]
        # The model restates unchanged facts in other formats; only a real change may reset the proposal.
        updates = {k: v for k, v in prefs.items() if k in allowed and not self.same_preference(k, v, mine.get(k))}
        had_proposal = bool(p["selected"])
        revising_results = bool(p["selected"] or p["events"])
        if updates:
            await self.execute(actor, {"action": "preferences", **updates})
            p = self.store.current(actor)
        if cmd.get("search") is True or (updates and revising_results):
            p["search_requested"] = True
        self.store.save(p)
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
            if key == "city":
                return cities(new) == cities(old)
            if key in ("start", "end"):
                # Stored times are whole seconds (see utc()).
                return instant(new).replace(microsecond=0) == instant(old)
            if key == "budget":
                return float(re.sub(r"[$,\s]|usd", "", str(new), flags=re.I)) == float(old)
        except (ValueError, TypeError):
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
        if missing:
            return Reply(("记下了。还需要你的" + "、".join(missing) + "，直接用平常说话的方式告诉我就好。") if zh else
                         "Got it. I still need your " + ", ".join(missing) + ". You can tell me in your own words.")
        if p.get("search_requested"):
            try:
                self.constraints(p)
            except ValueError as error:
                return Reply(("已记下你的信息。还需要你们双方有一致的城市、重合的空闲时间，以及各自的预算和兴趣，才能找到共同合适的活动。" if zh else
                              "I've saved your preferences. " + str(error) + " They can reply in their own words."))
            result = await self.execute(actor, {"action": "search"})
            if zh and result.card_kind:
                result.text = "找到几个符合你们共同时间、地点和预算的活动，选一个一起看看吧。标价不含未核实的手续费。"
            return result
        if p["selected"]:
            return self.summary(p)
        return Reply("记下你的偏好了。想看推荐时，直接问我附近有什么好玩的就行。" if zh else
                     "I've saved your preferences. Ask me what's on nearby whenever you'd like recommendations.")

    def details(self, p):
        lines = []
        for m in p["members"].values():
            x = m["preferences"]
            parts = [x["city"]] if "city" in x else []
            if "start" in x and "end" in x:
                parts.append(local_range(x["start"], x["end"]))
            if "budget" in x:
                parts.append(f"up to ${x['budget']:g}")
            if "interest" in x:
                parts.append(x["interest"])
            lines.append(f"{m['name']}: " + (" · ".join(parts) if parts else "hasn't shared preferences yet"))
        return "\n".join(lines)

    def next_step(self, p, actor):
        required = {"city", "start", "end", "budget", "interest"}
        if len(p["members"]) < 2:
            return f"Waiting for your friend to join with: join {p['invite']}" if p.get("invite") else "Waiting for your friend to join."
        waiting = [m["name"] for a, m in p["members"].items() if not required.issubset(m["preferences"])]
        if waiting:
            return f"Waiting for {', '.join(waiting)} to share their preferences."
        return "You're both set. Type search to see options."

    def explain(self, p, c):
        prefs = [m["preferences"] for m in p["members"].values()]
        text = f"Searched {' and '.join(c.get('cities', [c['city']]))}, {local_range(c['start'], c['end'])}, up to ${c['budget']:g} per person"
        if len({x["budget"] for x in prefs}) > 1:
            text += " (the lower of your two budgets)"
        text += "."
        interests = [x["interest"] for x in prefs]
        if c["interest"]:
            text += f" Interest: {c['interest']}."
        elif len({i.lower() for i in interests}) > 1 and "any" not in {i.lower() for i in interests}:
            text += f" Your interests differ ({' / '.join(interests)}), so all activities were included."
        if c.get("kind") in {"restaurant", "place"}:
            text += " This is your target budget, not a verified cost. Suggested visit time only; check hours and entry requirements. Google Maps. Search terms do not verify atmosphere or suitability."
        if c["budget"] == 0 and c.get("kind", "event") == "event":
            text += " A $0 budget means free events only."
        return text

    def leave_if_confirmed(self, actor):
        # A confirmed plan is finished business; starting or joining another just moves on from it.
        p = self.store.current(actor)
        if p and p["state"] == "confirmed":
            self.store.db.execute("DELETE FROM current_plan WHERE actor=?", (actor,))

    def leave(self, actor, p):
        self.store.db.execute("DELETE FROM current_plan WHERE actor=?", (actor,))
        others = [m["name"] for a, m in p["members"].items() if a != actor]
        if p["state"] == "confirmed":
            self.note(p, actor, "left this conversation; the confirmed plan still stands")
            self.store.save(p)
            return Reply(f"You've left. The plan stays confirmed for {', '.join(others) or 'your friend'}. Type new YourName to start another.")
        self.note(p, actor, "left the plan")
        del p["members"][actor]
        if not p["members"]:
            self.store.db.execute("DELETE FROM receipts WHERE plan=?", (p["id"],))
            self.store.db.execute("DELETE FROM plans WHERE id=?", (p["id"],))
            return Reply("You've left, and the empty plan was deleted.")
        if p["creator"] == actor:
            p["creator"] = next(iter(p["members"]))
        self.reset(p)
        p["invite"] = invite_code()
        p["log"][-1]["text"] += f". New invite for someone else: join {p['invite']}"
        self.store.save(p)
        return Reply("You've left the plan. Type new YourName to start another.")

    @staticmethod
    def price_label(e):
        if e.get("kind") in {"restaurant", "place"}:
            return e.get("price_level", "Price unknown") + " · exact cost unverified"
        return "Free" if e["price_max"] == 0 else f"Up to ${e['price_max']:.2f} / person"

    def summary(self, p):
        e = p["selected"]
        approved = [p["members"][a]["name"] for a in p["approvals"]]
        when = local(e["start"])
        members = " · ".join(f"{m['name']} {'✓' if a in p['approvals'] else '— waiting'}" for a, m in p["members"].items())
        rows = [{"label": "Activity", "value": e["title"]},
                {"label": "Suggested visit time" if e.get("kind") in {"restaurant", "place"} else "When", "value": when},
                {"label": "Where", "value": e["venue"]},
                {"label": "Per person", "value": self.price_label(e)},
                {"label": "Who's in", "value": members},
                {"label": "Visit details" if e.get("kind") in {"restaurant", "place"} else "Tickets", "value": e["availability"] if e.get("kind") in {"restaurant", "place"} else "Not booked · check availability and final price"}]
        text = "DEMO · " if e["demo"] else ""
        if p["state"] == "confirmed":
            if not e.get("end"):
                rows.append({"label": "Calendar duration", "value": "1-hour placeholder; event end time is unknown"})
            return Reply(text + "You're both in! Your shared plan is ready.", "detail", {
                "title": "You're both in ✓", "summary_rows": rows,
                **({"hero_image": e["image"]} if e.get("image") else {}),
                "ctas": [
                    {"label": "Add to calendar", "primary": True, "selection": {"action": "calendar", "plan_id": p["id"], "version": p["version"]}},
                    {"label": "View place & map" if e.get("kind") in {"restaurant", "place"} else "View event & tickets", "selection": {"action": "event_link", "plan_id": p["id"], "version": p["version"]}}]})
        waiting = [m["name"] for a, m in p["members"].items() if a not in p["approvals"]]
        text += (f"{', '.join(approved)} is in. Waiting for {', '.join(waiting)}." if approved else "Found something for you two. Does this work?")
        return Reply(text, "review", {
            "title": f"{len(approved)}/2 confirmed · Your shared plan",
            "summary_rows": rows,
            "approve_cta": {"label": "Works for me", "selection": {"action": "confirm", "plan_id": p["id"], "version": p["version"]}},
            "reject_cta": {"label": "Edit my preferences", "selection": {"action": "edit", "plan_id": p["id"]}}})

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
