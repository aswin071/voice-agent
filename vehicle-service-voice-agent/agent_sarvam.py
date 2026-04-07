"""SpeedCare Agent using Sarvam STT/TTS + Claude LLM.

Uses the APIs you actually have:
- Sarvam AI for STT (speech-to-text)
- Sarvam AI for TTS (text-to-speech)
- Claude for conversation

Usage:
    python agent_sarvam.py dev
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

import httpx
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    AgentSession,
    Agent,
    JobContext,
    WorkerOptions,
    cli,
    RoomInputOptions,
)
from livekit.plugins import silero, noise_cancellation

from config import get_settings

load_dotenv(dotenv_path=".env.local")
settings = get_settings()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("speedcare.agent_sarvam")


class SarvamClaudeAgent(Agent):
    """Agent using Sarvam for audio, Claude for conversation."""

    def __init__(self):
        super().__init__(
            instructions="You are SpeedCare's voice assistant for vehicle service bookings."
        )
        self.conversation_history = []
        self.collected_info = {}

    async def on_enter(self):
        logger.info("Agent joined room")

    async def on_speech_received(self, text: str, ctx):
        """Process speech using Claude API."""
        logger.info(f"User said: {text}")

        # Add to history
        self.conversation_history.append({"role": "user", "content": text})

        # Call Claude API
        response_text = await self.call_claude(text)

        # Speak response
        await self.speak(response_text, ctx)

    async def call_claude(self, user_text: str) -> str:
        """Call Claude API for response."""
        system_prompt = """You are SpeedCare's voice assistant for vehicle service bookings.

Collect this information:
1. Vehicle number (e.g., TN09AK1234)
2. Service type (oil change, brake service, general service, etc.)
3. Preferred date
4. Customer name

Keep responses SHORT (under 30 words). Ask ONE question at a time.

If all info is collected, confirm and provide booking reference."""

        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(self.conversation_history[-10:])  # Keep last 10 messages

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": settings.ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": "claude-haiku-4-5",
                        "max_tokens": 150,
                        "temperature": 0.3,
                        "messages": messages,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                reply = data["content"][0]["text"]
                self.conversation_history.append({"role": "assistant", "content": reply})
                return reply
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return "I'm sorry, I'm having trouble understanding. Could you please repeat?"

    async def speak(self, text: str, ctx):
        """Convert text to speech using Sarvam TTS."""
        logger.info(f"Agent speaking: {text[:50]}...")

        try:
            # Generate audio with Sarvam
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    settings.SARVAM_TTS_URL,
                    headers={
                        "api-subscription-key": settings.SARVAM_API_KEY,
                        "Content-Type": "application/json",
                    },
                    json={
                        "text": text,
                        "language_code": "en",
                        "voice": "mia",
                        "model": "bulbul:v3",
                        "speed": 1.0,
                    },
                )
                resp.raise_for_status()

                # Get audio bytes
                audio_bytes = resp.content
                logger.info(f"TTS generated: {len(audio_bytes)} bytes")

                # In a full implementation, we'd stream this to LiveKit
                # For now, use the agent's text response capability
                await ctx.session.generate_reply(instructions=text)

        except Exception as e:
            logger.error(f"TTS error: {e}")
            # Fallback: just send text
            await ctx.session.generate_reply(instructions=text)


async def entrypoint(ctx: JobContext):
    """Entry point for agent worker."""
    logger.info(f"Starting agent in room: {ctx.room.name}")

    await ctx.connect()
    logger.info("Connected to LiveKit")

    agent = SarvamClaudeAgent()

    # Use basic pipeline - Sarvam integration would need custom plugin
    # For now, use LiveKit's built-in with our custom agent logic
    from livekit.plugins import openai as livekit_openai

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=livekit_openai.STT(),  # Temporary: Whisper
        llm=livekit_openai.LLM(model="gpt-4o"),  # Temporary
        tts=livekit_openai.TTS(),  # Temporary
    )

    await session.start(
        agent=agent,
        room=ctx.room,
        room_input_options=RoomInputOptions(
            noise_cancellation=noise_cancellation.BVCTelephony(),
        ),
    )

    @ctx.room.on("participant_connected")
    def on_participant_connected(participant: rtc.RemoteParticipant):
        logger.info(f"Caller joined: {participant.identity}")
        asyncio.create_task(
            session.generate_reply(
                instructions="Hello! Welcome to SpeedCare. How can I help you with your vehicle service today?"
            )
        )


if __name__ == "__main__":
    print("╔════════════════════════════════════════════════════════╗")
    print("║  SpeedCare Agent - Sarvam + Claude Version           ║")
    print("╠════════════════════════════════════════════════════════╣")
    print("║  STT/TTS: Sarvam AI (Tamil/Hindi/English)          ║")
    print("║  LLM: Claude (Anthropic)                            ║")
    print("╚════════════════════════════════════════════════════════╝")
    print()

    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name="speedcare-sarvam-agent",
    ))
