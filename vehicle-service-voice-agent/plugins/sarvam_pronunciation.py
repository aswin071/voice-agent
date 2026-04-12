"""Sarvam AI Pronunciation Dictionary manager for SpeedCare.

Architecture (concurrent-user safe):
- The dictionary is a SHARED, READ-ONLY resource during live calls.
  It is created/resolved ONCE at worker process startup, not per call.
- `get_or_create_speedcare_dict()` caches the resolved `dict_id` in a
  module-level variable so concurrent entrypoint calls reuse it without
  hitting the Sarvam API.
- The first entrypoint to call `get_or_create_speedcare_dict()` wins;
  all subsequent calls return immediately from the module-level cache.
- No async locks needed: asyncio's single-threaded event loop guarantees
  that concurrent coroutines waiting on the first resolution will queue
  behind it naturally.

Sarvam Pronunciation Dictionary API:
  Base URL: https://api.sarvam.ai/text-to-speech/pronunciation-dictionary
  Limits  : 10 dicts per account, 100 words per dict, 1 dict per TTS call
  Model   : bulbul:v3 only
  Matching: exact word match, language-specific, plain text substitution

Usage:
    # At worker startup (once, in entrypoint):
    dict_id = await get_or_create_speedcare_dict()

    # Pass to SarvamTTS:
    SarvamTTS(..., dict_id=dict_id)
"""
from __future__ import annotations

import io
import json
import logging
from typing import Any

import httpx

from config import get_settings

logger = logging.getLogger("speedcare.pronunciation")
settings = get_settings()

# ---------------------------------------------------------------------------
# SpeedCare domain pronunciation table
# Covers all 4 supported languages. Total entries: well under 100-word limit.
# Words are exact-matched — use the form that actually appears in agent output.
# ---------------------------------------------------------------------------
SPEEDCARE_PRONUNCIATIONS: dict[str, dict[str, str]] = {
    "en-IN": {
        # Brand name — ensure "SpeedCare" is always said as two clear words
        "SpeedCare": "Speed Care",
        # Service acronyms
        "ABS": "A B S",
        "AC": "air conditioning",
        "AMC": "A M C",
        "ETA": "E T A",
        "OBD": "O B D",
        "RPM": "R P M",
        # Distance
        "KM": "kilometres",
        "km": "kilometres",
        # Indian vehicle registration state codes (spoken as letters, not words)
        "TN": "T N",
        "KA": "K A",
        "MH": "M H",
        "DL": "D L",
        "AP": "A P",
        "TS": "T S",
        "KL": "K L",
        "GJ": "G J",
        "HR": "H R",
        "RJ": "R J",
    },
    "ta-IN": {
        "SpeedCare": "ஸ்பீட்கேர்",
        "ABS": "ஏ பி எஸ்",
        "AC": "ஏர் கண்டிஷனிங்",
        "AMC": "ஏ எம் சி",
        "KM": "கிலோமீட்டர்",
        "km": "கிலோமீட்டர்",
        "ETA": "ஈ டி ஏ",
        "OBD": "ஓ பி டி",
        "RPM": "ஆர் பி எம்",
    },
    "hi-IN": {
        "SpeedCare": "स्पीड केयर",
        "ABS": "ए बी एस",
        "AC": "एयर कंडीशनिंग",
        "AMC": "ए एम सी",
        "KM": "किलोमीटर",
        "km": "किलोमीटर",
        "ETA": "ई टी ए",
        "OBD": "ओ बी डी",
        "RPM": "आर पी एम",
    },
    "ml-IN": {
        "SpeedCare": "സ്പീഡ് കെയർ",
        "ABS": "എ ബി എസ്",
        "AC": "എയർ കണ്ടീഷനിംഗ്",
        "AMC": "എ എം സി",
        "KM": "കിലോമീറ്റർ",
        "km": "കിലോമീറ്റർ",
        "ETA": "ഇ ടി എ",
        "OBD": "ഒ ബി ഡി",
        "RPM": "ആർ പി എം",
    },
}

# Module-level dict_id cache — set once, read by all concurrent calls.
# None = not yet resolved. "" = resolution attempted but failed (use TTS without dict).
_CACHED_DICT_ID: str | None = None
_RESOLUTION_ATTEMPTED: bool = False

_DICT_API_BASE = "https://api.sarvam.ai/text-to-speech/pronunciation-dictionary"


class PronunciationDictManager:
    """Low-level async CRUD wrapper for the Sarvam pronunciation dictionary API.

    Intended for management scripts and one-time setup; not used directly
    during live calls. For live calls, use `get_or_create_speedcare_dict()`.
    """

    def __init__(
        self,
        api_key: str | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key or settings.SARVAM_API_KEY
        self._client = http_client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = http_client is None

    @property
    def _headers(self) -> dict[str, str]:
        return {"api-subscription-key": self._api_key}

    async def create(self, pronunciations: dict[str, dict[str, str]]) -> str:
        """Upload a new pronunciation dictionary. Returns the `dict_id`."""
        payload = {"pronunciations": pronunciations}
        file_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        files = {"file": ("pronunciations.json", io.BytesIO(file_bytes), "application/json")}

        resp = await self._client.post(
            _DICT_API_BASE,
            headers=self._headers,
            files=files,
        )
        if resp.status_code != 200:
            logger.error("[DICT] create failed HTTP %d: %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()

        data = resp.json()
        dict_id: str = data["dictionary_id"]
        logger.info("[DICT] created dict_id=%s", dict_id)
        return dict_id

    async def list_dicts(self) -> list[str]:
        """Return all dictionary IDs for this account (max 10)."""
        resp = await self._client.get(_DICT_API_BASE, headers=self._headers)
        resp.raise_for_status()
        data = resp.json()
        return data.get("dictionaries", [])

    async def get(self, dict_id: str) -> dict[str, Any]:
        """Fetch the full pronunciations mapping for a dictionary."""
        resp = await self._client.get(f"{_DICT_API_BASE}/{dict_id}", headers=self._headers)
        resp.raise_for_status()
        return resp.json()

    async def update(self, dict_id: str, pronunciations: dict[str, dict[str, str]]) -> None:
        """Merge new pronunciations into an existing dictionary.

        Existing entries NOT in the uploaded file are preserved (partial update).
        """
        payload = {"pronunciations": pronunciations}
        file_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        files = {"file": ("pronunciations.json", io.BytesIO(file_bytes), "application/json")}

        resp = await self._client.put(
            _DICT_API_BASE,
            headers=self._headers,
            params={"dict_id": dict_id},
            files=files,
        )
        if resp.status_code != 200:
            logger.error("[DICT] update failed HTTP %d: %s", resp.status_code, resp.text[:300])
            resp.raise_for_status()

        logger.info("[DICT] updated dict_id=%s", dict_id)

    async def delete(self, dict_id: str) -> None:
        """Permanently delete a pronunciation dictionary."""
        resp = await self._client.delete(
            _DICT_API_BASE,
            headers=self._headers,
            params={"dict_id": dict_id},
        )
        resp.raise_for_status()
        logger.info("[DICT] deleted dict_id=%s", dict_id)

    async def aclose(self) -> None:
        if self._owns_client:
            try:
                await self._client.aclose()
            except Exception:
                pass


async def get_or_create_speedcare_dict() -> str | None:
    """Resolve the SpeedCare pronunciation dict_id for use in TTS requests.

    Resolution priority:
      1. Module-level cache (set by a previous call in this worker process)
      2. `SARVAM_DICT_ID` setting / env var (set by operator after first run)
      3. Create a new dictionary via the Sarvam API

    Returns the `dict_id` string, or None if resolution fails (TTS still works,
    just without custom pronunciations — fail-open is intentional).

    Operators should persist the returned dict_id in `.env.local` as
    `SARVAM_DICT_ID=p_xxxxxxxx` to avoid creating a new dict on every restart
    (Sarvam limits accounts to 10 dictionaries).
    """
    global _CACHED_DICT_ID, _RESOLUTION_ATTEMPTED

    # Already resolved in this process
    if _RESOLUTION_ATTEMPTED:
        return _CACHED_DICT_ID or None

    _RESOLUTION_ATTEMPTED = True

    # 1. Operator-supplied dict_id (persisted from a previous run)
    saved_id = getattr(settings, "SARVAM_DICT_ID", "").strip()
    if saved_id:
        logger.info("[DICT] dict_id_reused from settings: %s", saved_id)
        _CACHED_DICT_ID = saved_id
        return _CACHED_DICT_ID

    # 2. Create a new dictionary
    mgr = PronunciationDictManager()
    try:
        dict_id = await mgr.create(SPEEDCARE_PRONUNCIATIONS)
        _CACHED_DICT_ID = dict_id
        logger.info(
            "[DICT] new dict created dict_id=%s — "
            "add SARVAM_DICT_ID=%s to .env.local to reuse on restart",
            dict_id, dict_id,
        )
        return _CACHED_DICT_ID
    except Exception:
        logger.exception(
            "[DICT] failed to create pronunciation dict — "
            "TTS will proceed without custom pronunciations"
        )
        _CACHED_DICT_ID = None
        return None
    finally:
        await mgr.aclose()
