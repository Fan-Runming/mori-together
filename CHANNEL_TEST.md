# Temporary ASI:One channel delivery test

This branch includes the merged usability fixes. Do not merge diagnostic code into main.

## Start

Stop the old agent with Ctrl+C in its terminal. From this repository run:

```sh
MORI_DEBUG_SHAPE=1 .venv/bin/python agent.py
```

The environment flag applies only to this process; no .env edit is necessary.
Use the same existing AGENT_SEED and database. Never run two copies of this agent.

## Controlled test

Use fictional plans. Note the time and whether a `message shape` log appears after each step:

1. Participant A mentions Mori with `status`.
2. Participant B mentions Mori with `status`.
3. Each participant sends an ordinary message without mentioning anyone.
4. Participant A mentions Mori once more with `status`.
5. Share a public event link, then mention Mori with `status`.

Only share lines containing `message shape`, not the full startup log or .env.
These lines contain hashed identifiers, timestamps, counts, fixed metadata key names,
and allowlisted resource schemes. They do not contain message text, metadata values,
full URLs, or arbitrary metadata keys.

## Interpretation

Separate sender hashes in the same session support distinguishing the two senders.
Logs appearing for unmentioned messages demonstrate delivery of those messages.
A longer message after a later mention does NOT by itself prove history was attached.
Absence of resource blocks for a URL does NOT prove the URL was absent from text.
These logs cannot establish the actual meaning or completeness of any chat history.

## Finish

Stop the debug agent with Ctrl+C, then:

```sh
git switch main
.venv/bin/python agent.py
```

No permanent debug environment setting was added.
