"""ASI:One transport. Does not infer human identity from model-generated text."""
import hashlib
import json
import os
from datetime import datetime, timezone
from uuid import uuid4
from pathlib import Path

from dotenv import load_dotenv
from uagents import Agent, Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, TextContent, MetadataContent, ResourceContent,
    EndSessionContent, chat_protocol_spec,
)

from mori_together.core import Planner, Store, principal
from mori_together.providers import DemoEvents, Ticketmaster


def short_hash(value):
    return hashlib.sha256(str(value).encode()).hexdigest()[:10]


def message_shape(sender, session, msg):
    """Structure of an incoming message for channel testing. Never includes text, metadata values or URIs."""
    blocks = []
    for block in msg.content:
        item = {"type": getattr(block, "type", type(block).__name__)}
        if isinstance(block, TextContent):
            item["chars"] = len(block.text)
            item["lines"] = block.text.count("\n") + 1
            item["starts_with_mention"] = block.text.lstrip().startswith("@")
            item["has_url"] = "http" in block.text
        elif isinstance(block, MetadataContent):
            item["keys"] = sorted(block.metadata)
        elif isinstance(block, ResourceContent):
            resources = block.resource if isinstance(block.resource, list) else [block.resource]
            item["resources"] = [{"scheme": r.uri.split(":", 1)[0], "metadata_keys": sorted(r.metadata),
                                  "mime_type": r.metadata.get("mime_type")} for r in resources]
        blocks.append(item)
    return {"sender": short_hash(sender), "session": short_hash(session), "msg": short_hash(msg.msg_id),
            "timestamp": msg.timestamp.isoformat(), "blocks": blocks}


def wire_reply(reply):
    blocks = [TextContent(type="text", text=reply.text)]
    if reply.card_kind:
        blocks.append(MetadataContent(type="metadata", metadata={
            "card_protocol_version": "1", "requires_card_interaction": "true",
            "card_kind": reply.card_kind, "card_payload": json.dumps(reply.card_payload)}))
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=blocks)


def build_agent():
    load_dotenv()
    mode = os.getenv("MORI_MODE", "live")
    if mode not in {"live", "demo"}:
        raise RuntimeError("MORI_MODE must be live or demo")
    seed = os.getenv("AGENT_SEED")
    if not seed or len(seed) < 32:
        raise RuntimeError("Set AGENT_SEED to a private random value (at least 32 characters) in .env.")
    if mode == "live":
        missing = [k for k in ("ASI_ONE_API_KEY", "TICKETMASTER_API_KEY") if not os.getenv(k)]
        if missing:
            raise RuntimeError("Configure these .env values before live startup: " + ", ".join(missing))
    provider = DemoEvents() if mode == "demo" else Ticketmaster()
    planner = Planner(Store(os.getenv("MORI_DB", f"data/{mode}.sqlite3")), provider)
    debug_shape = os.getenv("MORI_DEBUG_SHAPE") == "1"
    agent = Agent(name="Mori Together" + (" DEMO" if mode == "demo" else ""),
                  seed=seed, port=int(os.getenv("AGENT_PORT", "8001")), mailbox=True,
                  publish_agent_details=True, store_message_history=False,
                  readme_path=str(Path(__file__).with_name("AGENT_PROFILE.md")),
                  description="Coordinate two friends' preferences, discover activities and confirm a shared plan. No bookings.")
    protocol = Protocol(spec=chat_protocol_spec)

    @protocol.on_message(ChatMessage)
    async def on_message(ctx: Context, sender: str, msg: ChatMessage):
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=msg.msg_id))
        if debug_shape:
            # Opt-in (MORI_DEBUG_SHAPE=1): logs hashes and block structure only, to learn how channels arrive.
            ctx.logger.info("message shape " + json.dumps(message_shape(sender, ctx.session, msg)))
        text = "\n".join(block.text for block in msg.content if isinstance(block, TextContent))
        if not text.strip():
            return
        # A session identifies a conversation, NOT a verified person. Shared-channel
        # identity support must be validated before claiming two-human verification.
        if ctx.session is None:
            from mori_together.core import Reply
            await ctx.send(sender, wire_reply(Reply("A stable conversation session is required. Please open a direct conversation.")))
            return
        try:
            result = await planner.handle(principal(sender, ctx.session), text, str(msg.msg_id))
            await ctx.send(sender, wire_reply(result))
        except Exception:
            # Do not log messages, credentials, invite codes, or provider URLs with keys.
            ctx.logger.error("Request failed; inspect locally without exposing user data.")
            from mori_together.core import Reply
            await ctx.send(sender, wire_reply(Reply("Couldn't complete this request. Your saved plan is safe; try status.")))

    @protocol.on_message(ChatAcknowledgement)
    async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
        pass

    agent.include(protocol, publish_manifest=True)
    return agent


if __name__ == "__main__":
    build_agent().run()
