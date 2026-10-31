"""Offline demo: explicit simulated identities and fictional events, no credentials."""
import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone

from mori_together.core import Planner, Store
from mori_together.providers import DemoEvents


async def walkthrough():
    planner = Planner(Store(":memory:"), DemoEvents())
    start = datetime.now(timezone.utc) + timedelta(days=1)
    pref = {"city": "Berkeley", "start": start.isoformat(),
            "end": (start + timedelta(hours=4)).isoformat(), "budget": 30, "interest": "creative"}

    async def send(actor, message):
        result = await planner.handle(actor, message)
        print(f"\n[{actor}] {message}\n[Mori] {result.text}")
        if result.card_kind:
            print(f"[ASI:One {result.card_kind} card ready]")
        return result

    print("OFFLINE DEMO — simulated Alice/Bob sessions, fictional events, no live search or booking")
    await send("Alice", "new Alice")
    invite = planner.store.current("Alice")["invite"]
    await send("Bob", f"join {invite} Bob")
    await send("Alice", "preferences " + json.dumps(pref))
    await send("Bob", "preferences " + json.dumps({**pref, "budget": 20}))
    await send("Alice", "search")
    await send("Alice", "select demo-0")
    version = planner.store.current("Alice")["version"]
    await send("Alice", f"confirm {version}")
    await send("Bob", "preferences " + json.dumps({"budget": 10}))
    await send("Alice", f"confirm {version}")
    await send("Bob", "search")
    await send("Bob", "select demo-1")
    version = planner.store.current("Bob")["version"]
    await send("Alice", f"confirm {version}")
    await send("Bob", f"confirm {version}")


async def chat():
    planner = Planner(Store("data/local-demo.sqlite3"), DemoEvents())
    print("LOCAL DEMO ONLY. Simulated speakers: 'Alice: new Alice', 'Bob: join CODE Bob'. Type quit to exit.")
    while True:
        line = await asyncio.to_thread(input, "> ")
        if line == "quit":
            return
        actor, sep, message = line.partition(":")
        if not sep or actor not in {"Alice", "Bob"}:
            print("Prefix your command with Alice: or Bob:")
            continue
        result = await planner.handle(actor, message)
        print(result.text)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", action="store_true")
    asyncio.run(chat() if parser.parse_args().chat else walkthrough())
