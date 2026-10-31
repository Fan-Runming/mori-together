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


def instant(value):
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError("Include a timezone, e.g. 2026-11-03T14:00:00-08:00.")
    return dt.astimezone(timezone.utc)


def utc(value):
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
                if message_id and command["action"] not in {"forget", "delete"}:
                    self.store.db.execute("INSERT INTO receipts VALUES(?,?,?,?)", (
                        actor, message_id, plan["id"] if plan else None, json.dumps(asdict(result))))
                self.store.db.commit()
                return result
            except (ValueError, KeyError, TypeError, ProviderError) as error:
                self.store.db.rollback()
                return Reply(str(error) if isinstance(error, (ValueError, ProviderError)) else "Invalid input. Type help for the supported format.")
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
                raise ValueError("Expected a command object.")
            return obj
        verb, _, rest = text.partition(" ")
        verb = verb.lower()
        if verb in {"help", "status", "edit", "search", "forget", "delete"} and not rest:
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
            return {"action": verb, "version": int(rest)}
        plan = self.store.current(actor)
        # Do not send other participants' data, invitations or internal identities to the LLM.
        context = {"now": self.now().isoformat(), "timezone": "America/Los_Angeles",
                   "has_plan": bool(plan),
                   "my_preferences": plan["members"][actor]["preferences"] if plan else {}}
        return await self.interpreter.interpret(text, context)

    def reset(self, p):
        p["version"] += 1
        p["approvals"] = []
        p["selected"] = None
        p["events"] = []
        p["state"] = "draft"

    def preferences_card(self, p):
        return Reply("Enter YOUR availability and preferences; these are shared with your planning partner. Times require a timezone offset.", "form", {
            "title": "Make room for a little together-time",
            "fields": [
                {"name": "city", "kind": "select", "label": "City", "required": True,
                 "options": [{"value": s, "label": s} for s in ["Berkeley", "San Francisco"]]},
                {"name": "start", "kind": "text", "label": "Available from (ISO date/time + timezone)", "required": True},
                {"name": "end", "kind": "text", "label": "Available until (ISO date/time + timezone)", "required": True},
                {"name": "budget", "kind": "number", "label": "Maximum USD per person", "required": True},
                {"name": "interest", "kind": "text", "label": "An activity or interest", "required": True}],
            "submit_cta": {"label": "Save my preferences", "selection": {
                "action": "preferences", "plan_id": p["id"], "version": p["version"]}}})

    def constraints(self, p):
        if len(p["members"]) != 2:
            raise ValueError("Invite a second participant first. Share the invite code privately.")
        people = list(p["members"].values())
        required = {"city", "start", "end", "budget", "interest"}
        for m in people:
            if not required.issubset(m["preferences"]):
                raise ValueError(f"{m['name']} still needs to enter their city, availability, budget and interest.")
        prefs = [m["preferences"] for m in people]
        if prefs[0]["city"] != prefs[1]["city"]:
            raise ValueError("You chose different cities. Agree on Berkeley or San Francisco, then update your preferences.")
        start = max(instant(x["start"]) for x in prefs)
        end = min(instant(x["end"]) for x in prefs)
        if start <= self.now() or start >= end:
            raise ValueError("No future overlapping time. Update your availability.")
        # Search one common interest if present, otherwise broad search avoids AND-filtering away results.
        interest = prefs[0]["interest"] if prefs[0]["interest"].lower() == prefs[1]["interest"].lower() else ""
        if interest.lower() == "any":
            interest = ""
        return dict(city=prefs[0]["city"], start=utc(start), end=utc(end),
                    budget=min(x["budget"] for x in prefs), interest=interest)

    def eligible(self, event, c):
        return (event["city"].lower() == c["city"].lower()
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
            if self.store.current(actor):
                raise ValueError("You already have a plan. Use status or delete it before creating another.")
            p = {"id": secrets.token_hex(12), "invite": secrets.token_urlsafe(18),
                 "creator": actor, "mode": self.provider.mode, "members": {actor: {"name": str(cmd.get("name", "Friend A"))[:40], "preferences": {}}},
                 "version": 0, "state": "draft", "events": [], "selected": None, "approvals": []}
            self.store.save(p)
            self.store.bind(actor, p)
            r = self.preferences_card(p)
            r.text = f"Created a new {self.provider.mode.upper()} plan. Give only your friend this single-use invite: join {p['invite']} TheirName\n" + r.text
            return r
        if action == "join":
            if self.store.current(actor):
                raise ValueError("This conversation already has a plan. Use status.")
            p = next((json.loads(row[0]) for row in self.store.db.execute("SELECT body FROM plans")
                      if json.loads(row[0]).get("invite") == cmd.get("code")), None)
            if not p or len(p["members"]) >= 2:
                raise ValueError("Invalid or already used invite.")
            p["members"][actor] = {"name": str(cmd.get("name", "Friend B"))[:40], "preferences": {}}
            p["invite"] = None
            self.reset(p)
            self.store.save(p)
            self.store.bind(actor, p)
            return self.preferences_card(p)
        p = self.store.current(actor)
        if not p or actor not in p["members"]:
            return Reply("Start with 'new YourName', or 'join INVITE_CODE YourName'.\n" + HELP)
        if p["mode"] != self.provider.mode:
            raise ValueError("This plan belongs to another mode. Use a separate database for live and demo runs.")
        if cmd.get("plan_id", p["id"]) != p["id"]:
            raise ValueError("That card belongs to a different plan. Type status.")
        if "version" in cmd and int(cmd["version"]) != p["version"]:
            raise ValueError("This plan changed. Type status to review the latest version before acting.")
        if action == "delete":
            if actor != p["creator"]:
                raise ValueError("Only the creator can delete the shared plan. Use forget to clear your preferences.")
            self.store.db.execute("DELETE FROM receipts WHERE plan=?", (p["id"],))
            self.store.db.execute("DELETE FROM current_plan WHERE plan=?", (p["id"],))
            self.store.db.execute("DELETE FROM plans WHERE id=?", (p["id"],))
            return Reply("Shared plan and stored message receipts deleted. No external calendar events were created or removed.")
        if action == "forget":
            p["members"][actor]["preferences"] = {}
            self.reset(p)
            self.store.db.execute("DELETE FROM receipts WHERE plan=?", (p["id"],))
            self.store.save(p)
            return Reply("Your preferences and cached replies for this plan were cleared. The proposal and approvals were reset.")
        if action == "preferences":
            pref = dict(p["members"][actor]["preferences"])
            for key in ("city", "start", "end", "budget", "interest"):
                if key in cmd:
                    pref[key] = cmd[key]
            if "city" in pref and pref["city"] not in {"Berkeley", "San Francisco"}:
                raise ValueError("This pilot supports Berkeley and San Francisco.")
            if "budget" in pref:
                pref["budget"] = float(pref["budget"])
                if not math.isfinite(pref["budget"]) or not 0 <= pref["budget"] <= 10000:
                    raise ValueError("Enter a budget between 0 and 10,000 USD.")
            if "interest" in pref:
                pref["interest"] = str(pref["interest"])[:120].strip()
                if not pref["interest"]:
                    raise ValueError("Enter an interest (or 'any').")
            for key in ("start", "end"):
                if key in pref:
                    d = instant(pref[key])
                    if d <= self.now() or d > self.now() + timedelta(days=7):
                        raise ValueError("Choose a future time within the next seven days.")
                    pref[key] = utc(d)
            if "start" in pref and "end" in pref and instant(pref["start"]) >= instant(pref["end"]):
                raise ValueError("End must be after start.")
            p["members"][actor]["preferences"] = pref
            self.reset(p)
            self.store.save(p)
            return Reply("Saved only your preferences. Previous proposals and approvals were reset. Both participants can type status; type search when ready.")
        if action == "search":
            c = self.constraints(p)
            events = [e for e in await self.provider.search(c) if self.eligible(e, c)]
            if not events:
                return Reply("No verified matches in this source for your shared time and listed budget. Unknown prices are excluded; try a wider time or another interest. No fictional results substituted.")
            self.reset(p)
            p["events"] = events[:3]
            p["state"] = "options"
            self.store.save(p)
            p["search_requested"] = False
            self.store.save(p)
            return Reply("Here are a few options for you two. Choose one to review together. Listed prices exclude unverified fees.", "carousel", {
                "title": "A few ways to spend time together",
                "subtitle": "DEMO fixtures — not real events" if self.provider.mode == "demo" else "Ticketmaster • prices exclude unverified fees",
                "items": [{"id": e["id"], "title": e["title"], "subtitle": f"{e['venue']} · {e['start']}",
                           "secondary_text": f"Listed up to ${e['price_max']} · inventory unverified",
                           "primary_cta": {"label": "Propose this", "selection": {"action": "select", "event_id": e["id"], "plan_id": p["id"], "version": p["version"]}}}
                          for e in p["events"]]})
        if action == "select":
            event = next((e for e in p["events"] if e["id"] == cmd.get("event_id")), None)
            if not event:
                raise ValueError("Select an event from your current results. Type search.")
            event = await self.provider.refresh(event)
            if not self.eligible(event, self.constraints(p)):
                raise ValueError("This event no longer fits. Search for another; no approval was recorded.")
            p["version"] += 1
            p["selected"], p["approvals"], p["state"] = event, [], "proposed"
            self.store.save(p)
            return self.summary(p)
        if action == "confirm":
            if "version" not in cmd:
                raise ValueError("Review status, then confirm VERSION.")
            if not p["selected"]:
                raise ValueError("Propose an activity first.")
            fresh = await self.provider.refresh(p["selected"])
            keys = ("title", "start", "end", "venue", "city", "price_min", "price_max", "status", "url")
            if not self.eligible(fresh, self.constraints(p)):
                self.reset(p)
                self.store.save(p)
                return Reply("The event no longer fits your constraints. Approvals cleared; type search for alternatives.")
            if any(fresh[k] != p["selected"][k] for k in keys):
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
            self.store.save(p)
            return self.summary(p)
        if action == "edit":
            return self.preferences_card(p)
        if action in {"calendar", "event_link"}:
            if not p["selected"] or p["state"] != "confirmed":
                raise ValueError("Both people need to confirm the plan first.")
            # ASI cards can become locked after one selection. Always expose both
            # destinations as ordinary links, independent of the consumed card.
            links = {
                "calendar": "[Open calendar draft](" + self.calendar_url(p["selected"]) + ")",
                "event_link": "[View event and tickets](" + p["selected"]["url"] + ")",
            }
            other = "event_link" if action == "calendar" else "calendar"
            return Reply(links[action] + "\n\n" + links[other] +
                         "\n\nSave the calendar draft yourself. Tickets aren't booked; check final fees and availability.")
        if action == "status":
            if not p["selected"]:
                r = self.preferences_card(p)
                r.text = self.details(p) + "\n" + r.text
                return r
            return self.summary(p)
        raise ValueError("Unsupported action. Type help.")

    async def conversation(self, actor, cmd):
        """Accumulate this speaker's facts and resume an explicitly requested search."""
        zh = cmd.get("language") == "zh"
        p = self.store.current(actor)
        if not p:
            return Reply("可以帮你们找活动。先创建一个双人计划，或用朋友的邀请码加入；加入后直接说时间、地点和预算即可。" if zh else
                         "I can help you find something to do. First create a shared plan or join your friend's invite; then tell me your availability, city and budget in your own words.")
        if p["mode"] != self.provider.mode:
            raise ValueError("This plan belongs to another mode. Use a separate database for live and demo runs.")
        prefs = cmd.get("preferences", {})
        if not isinstance(prefs, dict):
            raise ValueError("Please describe your availability, city or budget again.")
        allowed = {"city", "start", "end", "budget", "interest"}
        updates = {k: v for k, v in prefs.items() if k in allowed and v != p["members"][actor]["preferences"].get(k)}
        if updates:
            await self.execute(actor, {"action": "preferences", **updates})
            p = self.store.current(actor)
        if cmd.get("search") is True:
            p["search_requested"] = True
        self.store.save(p)
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
        return Reply("记下你的偏好了。想看推荐时，直接问我附近有什么好玩的就行。" if zh else
                     "I've saved your preferences. Ask me what's on nearby whenever you'd like recommendations.")

    def details(self, p):
        return "\n".join(f"{m['name']}: {json.dumps(m['preferences'], ensure_ascii=False)}" for m in p["members"].values())

    def summary(self, p):
        e = p["selected"]
        approved = [p["members"][a]["name"] for a in p["approvals"]]
        when = instant(e["start"]).astimezone(ZoneInfo("America/Los_Angeles")).strftime("%a, %b %d · %I:%M %p %Z")
        members = " · ".join(f"{m['name']} {'✓' if a in p['approvals'] else '— waiting'}" for a, m in p["members"].items())
        rows = [{"label": "Activity", "value": e["title"]},
                {"label": "When", "value": when},
                {"label": "Where", "value": e["venue"]},
                {"label": "Per person", "value": f"Up to ${e['price_max']:.2f} listed · fees extra"},
                {"label": "Who's in", "value": members},
                {"label": "Tickets", "value": "Not booked · check availability and final price"}]
        text = "DEMO · " if e["demo"] else ""
        if p["state"] == "confirmed":
            if not e.get("end"):
                rows.append({"label": "Calendar duration", "value": "1-hour placeholder; event end time is unknown"})
            return Reply(text + "You're both in! Your shared plan is ready.", "detail", {
                "title": "Your plan is confirmed ✓", "summary_rows": rows,
                "ctas": [
                    {"label": "Add to calendar", "primary": True, "selection": {"action": "calendar", "plan_id": p["id"], "version": p["version"]}},
                    {"label": "View event & tickets", "selection": {"action": "event_link", "plan_id": p["id"], "version": p["version"]}}]})
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
                  "details": e["url"] + "\nNot booked. Verify fees and inventory." +
                  ("\nEnd time is a 1-hour placeholder." if not e.get("end") else "")}
        return "https://calendar.google.com/calendar/render?" + urlencode(params)


def principal(sender, session):
    # Do not trust claimed names/actor IDs in model text or card payloads.
    return hashlib.sha256(f"{sender}:{session}".encode()).hexdigest()
