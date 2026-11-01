# Mori Together

Turn “we should hang out sometime” into a plan you both say yes to.

Independent Stranger Builds agent prototype, created October 31, 2026. This
repository contains new code, not a fork or a copy of the existing Mori app.
The original Mori product is separate and is not required for this workflow.

## Current status

- Implemented: Python planning engine, SQLite persistence, single-use invitation,
  separate participant preferences, overlap/price filtering, proposal versioning,
  individual confirmations, calendar draft, forget/delete, idempotent message handling.
- Implemented adapters: Agent Chat Protocol 0.3.0, ASI:One interactive form/carousel/
  review cards, ASI-1 natural-language intent interpretation, Ticketmaster Discovery.
- Verified locally: 38 automated tests and an end-to-end OFFLINE demo. SDK imports
  and outgoing ACP message/card serialization checked against installed uAgents.
- Live walkthrough completed: Agentverse connection, Ticketmaster results, ASI:One
  cards and approvals from two accounts in one shared conversation.
- Natural-language planning now accumulates the speaker's preferences, asks for
  missing facts and automatically searches when both participants are ready.
  These orchestration paths have mocked-interpreter regression coverage; the new
  interpreter prompt still needs a live ASI:One walkthrough after restart.
- Ordinary group messages are not proven to reach Mori. ASI:One currently routes
  unmentioned messages to the user's personal AI. Do not claim background listening.
- Public source: https://github.com/Fan-Runming/mori-together. The agent still runs
  locally; always-on hosting and hackathon submission remain to be completed.

**Demo mode uses fictional fixtures and simulated identities. It is not evidence of
live search, ticket inventory, independent human approvals, or real user testing.**

## Quick start

Python 3.12 recommended. A local `.venv` is already installed in the working copy.

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python demo.py
```

`demo.py` runs two simulated friends through a proposal, one approval, a budget
change that invalidates that approval, a cheaper alternative and two confirmations.
For a local interactive command session: `.venv/bin/python demo.py --chat`.

## Configure live mode

Copy `.env.example` to `.env` if no `.env` exists. The prepared local working copy
already contains a random private AGENT_SEED, with API keys left blank.
Never paste keys into a chat, commit `.env`, or upload the data directory.

- `ASI_ONE_API_KEY`: https://asi1.ai/dashboard/api-keys
- `TICKETMASTER_API_KEY`: https://developer.ticketmaster.com/
- `AGENT_SEED`: private random value, minimum 32 characters. Keep stable to preserve
  the agent address. Example local generator: `python -c 'import secrets; print(secrets.token_urlsafe(48))'`.
- `MORI_MODE=live`, `MORI_DB=data/live.sqlite3`.

The implementation uses the SDK's mailbox flow, not an invented Agentverse REST
registration API. An Agentverse account and mailbox connection are needed; an
Agentverse API key is not required by this implementation's constructor.

```bash
.venv/bin/python agent.py
```

Startup stops with a clear error if live API keys or the seed are missing.
When running, use the inspector URL emitted by the SDK to connect the mailbox to
your Agentverse account. Configure an available handle and agent profile there.
Keep the process running, then use Agentverse's Chat with Agent link to open ASI:One.
Do not claim discovery or hosting is complete until this has been tested live.
For deployment, the Dockerfile is included; inject environment secrets and mount
`/app/data` on persistent storage. Keep one process/replica for this SQLite prototype.

## First live acceptance test

Use direct agent mentions and separate ASI:One conversations. The safest first
trial is two different accounts, with one person in each direct conversation.

1. Alice: “create a plan” (or `new Alice`). Share the single-use invitation privately with Bob.
2. Bob: `join INVITE_CODE Bob`.
3. Each enters their own preferences using the form or a command. If the other
   participant changed the plan, type `status` for a fresh card.
4. After both save complete preferences, Mori searches automatically; select an option.
5. Both: `status`, review the current proposal and click “Works for me”.
6. Verify the two sessions create two distinct participants. One participant
   confirming repeatedly must never confirm for the other.
7. Update one person's budget, verify confirmations disappear, search again.
8. Confirm the final plan; open the calendar draft only if you want to save it.

Example preference syntax (substitute future times within seven days):

```text
preferences {"city":"Berkeley","start":"2026-11-03T14:00:00-08:00","end":"2026-11-03T18:00:00-08:00","budget":30,"interest":"music"}
```

Natural-language commands use ASI-1 to extract only the speaker's stated fields.
Final approval, selection and deletion require explicit commands or direct card
payloads. The interpreter cannot manufacture approval on behalf of another person.

## Important limitations

- **Identity:** ACP `(sender, session)` is a transport/conversation principal, not a
  verified human account. ASI:One may mediate sender identities; session continuity
  and separation MUST be verified before claiming real two-person approval. Do not
  put this prototype in a shared group and infer human identity from display names.
  Two-session demos are explicitly marked. Stable verified account binding is a
  launch requirement, not something to fake in a demo.
- **Card routing:** supports direct-mention JSON card selections. Planner-mediated
  natural-language card responses are not accepted as approval; the user is directed
  to explicit commands. Card rendering in ASI:One remains to be tested live.
- **Search coverage:** Ticketmaster only, Berkeley/San Francisco, next seven days.
  This is not a comprehensive catalog of workshops, free community activities, or
  campus events. Unknown USD price ranges are excluded rather than treated as free.
  Different interests use a broad search; this version does not semantically rank
  compromises. It filters shared hard constraints and returns at most three options.
- **Booking:** an “onsale” event does not prove two available tickets. Discovery
  prices may exclude fees. Confirmation means willingness to meet, not a purchase,
  reserved place or guarantee of the total cost. Unknown end times are labeled; the
  calendar draft uses an explicitly labeled one-hour placeholder in that case.
- **Memory/privacy:** shared per-plan preferences only; no chat-history import,
  private side-channel preferences, background listening or proactive reminders.
  `forget` removes one's preferences plus proposal/approval/cached-response data;
  `delete` (creator) removes the shared plan. Database files are not encrypted.
  Do not use sensitive information in this prototype. Third-party data-retention
  policies are separate; deleting local memory cannot retract earlier API requests.
- No automatic purchase, unsolicited invitation, calendar write, location tracking,
  weather integration, notifications, or existing Mori-app integration.

## Architecture

`agent.py` handles ACP messages and cards. `core.py` owns authoritative state and
validates all actions, independently of the LLM. `providers.py` has ASI-1 intent
interpretation, Ticketmaster live search/refresh, and separate demo fixtures.
SQLite stores plans, current session memberships and message receipts. A single
async lock serializes writes; duplicate message IDs replay the saved response.
Changing constraints or a proposal clears both approvals. Confirmation rechecks
the source; cancellation invalidates the proposal, changed details require review.
Requests time out and retry at most three times. Logs omit chat text and secrets.

## Hackathon checklist still to complete

- Create a NEW public GitHub repository (not a fork); review source before pushing.
- Agentverse mailbox registered on 2026-10-31; profile reports Active / ASI Available.
  Address: `agent1q287kqknwatnyjlenzjclu2xnnkm5t0pfqqe8y5xh85fe2r4dm6wvsucxgg`.
  Live ASI:One retest passed at 2026-10-31 19:32 UTC after fixing leading-mention
  parsing (18 automated tests passed). Verified: form submission, preserved state
  after restart, two separate conversation sessions joining the same plan,
  Ticketmaster live search returning three events, carousel selection, review-card
  approvals progressing from 0/2 to 1/2 to 2/2, and a Google Calendar draft link.
  Test selection: Cardinal Bloom, The Gontiks at Brick and Mortar Music Hall,
  2026-11-02 04:00 UTC, listed USD 30.14. No booking or calendar write occurred.
  Source supplied no end time; calendar draft explicitly labels its one-hour
  duration as a placeholder. Fees and two-ticket inventory remain unverified.
- Repeat with two actual people: this test used two conversations in one account,
  not two verified humans. It is not external user feedback or a real reservation.
- Obtain three assigned Trick or Feedback tries through ASI:One with written notes;
  implement and document at least one change. Do not count this offline demo as use.
- Provide launch evidence and submission materials; keep service online for judging.

## References

- https://strangerbuilds.com/rules
- https://www.fetch.ai/events/hackathons/stranger-builds-1/hackpack
- https://innovationlab.fetch.ai/resources/docs/examples/chat-protocol/asi-compatible-uagents
- https://innovationlab.fetch.ai/resources/docs/interactive-cards/asi-interactive-cards
- https://docs.asi1.ai/documentation/build-with-asi-one/chat-completions
- https://developer.ticketmaster.com/products-and-docs/apis/discovery-api/v2/

## Conversational planning

Mention Mori in ASI:One, then use ordinary language. Examples:

- “我们现在计划到哪一步了？” — review the current plan or existing results.
- “选第一个” — propose a numbered option from current results.
- “这个可以” — redisplay the exact proposal for confirmation with a button.
- “重新计划” — open preference editing without deleting the plan.
- “我的预算还是 $30” — restating an equivalent preference preserves approvals.

Final approval remains a card click. Natural-language assent alone never records
approval. Interpreter output is tested with simulated responses; these new phrases
still need a live language-model walkthrough after agent restart. Unmentioned group
messages and attached chat history are not established capabilities.


### Places and restaurant discovery

Set optional `GOOGLE_PLACES_API_KEY` in local `.env`, enable Places API (New) in a
Google Cloud project with billing, restrict the key to that API, and restart the
agent. Configure request quotas in Cloud Console. Without a key, event search
still works and place requests explain the missing configuration.

Examples: “想找 Berkeley 的火锅”, “想去公园散步”, “想找好看的拍照地点”.
Mori extracts restaurant/place/event intent and search terms, asks for missing
shared availability, city and budget, then offers up to three candidates.
Both people must confirm. Confirmation provides reusable Google Maps and calendar
links. Visit duration is a proposed one-hour block bounded by shared availability.
Hours, atmosphere, admission costs, dietary suitability and reservations are NOT
verified. Unknown cost is not free, including for parks. A zero-budget place search
can return candidates, clearly marked price unknown. No booking or route is made.
Ticketmaster remains the source for scheduled events. Yelp is no longer used.

Place cards now request photo metadata and resolve one real cover per displayed
candidate (up to three). The server sends the key only in the request header and
passes a key-free Google image URL to the card. Photographer names appear beside
the photo and author profile links accompany the reply. Missing/failed photos
fall back to text cards. Photo resource names are not saved; old image URLs may
expire, so a new search refreshes them. Photo requests may incur additional usage.
Rating, price and review fields are not requested. Demo fixtures are fictional.

Before public release, review Google Places display, storage and privacy policies:
https://developers.google.com/maps/documentation/places/web-service/policies .
The current planner persists candidate payloads in SQLite and chat receipts; its
storage must be adapted to Google's requirements before deploying Places publicly.
This integration is for local evaluation until that work and live API validation
are complete. Multi-stop itineraries and proximity/travel-time ranking are not yet
implemented.

### Natural onboarding and multiple available times

A concrete first outing request creates a shared plan and retains extracted preferences.
Explicit short startup phrases work without an LLM call; other phrasing uses ASI:One.
Users can paste an invite code directly to join. The form offers up to three time-slot
dropdowns for the next seven days; custom intervals can also be expressed in natural
language. Stored `windows` are separate intervals; legacy `start`/`end` remains supported.
Search queries each actual shared intersection and deduplicates results by provider ID.
Both form and conversational preference completion trigger search automatically.
Search outages preserve saved preferences for retry. Group messages still require @mentions.
