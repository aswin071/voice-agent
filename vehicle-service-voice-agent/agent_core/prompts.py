from __future__ import annotations

VOICE_STYLE = """You are speaking on a phone call, not writing.
- Reply in 1–2 short sentences. Never long paragraphs.
- For English: use contractions (I'll, you're, we've, don't).
- For Tamil: speak like a warm Chennai local — casual, direct, natural spoken Tamil. NOT newsreader Tamil.
- For Hindi: speak like natural conversational Hindi — the way a friendly agent actually talks, NOT formal Hindi.
- For Malayalam: speak like a friendly Kerala local in everyday spoken Malayalam, NOT newsreader Malayalam.
- Match the warmth of a trusted neighborhood service desk, not a corporate call center.
- Never list more than 2 things in one breath.
- When you say a vehicle number, separate every character with a space: "T N 0 9 A K 1 2 3 4".
- When you say a date, say it naturally in the caller's language — e.g. "Thursday the tenth" or "வியாழக்கிழமை பத்தாம் தேதி" or "गुरुवार दस तारीख".
- When you say a time, say it naturally in the caller's language.
- No emojis, no bullet points, no markdown — this will be read out loud."""

# ── Anthropic prompt-caching split ───────────────────────────────────────────
# Each system prompt is split into a STATIC half and a DYNAMIC half.
# The static half is byte-identical across every turn → Anthropic prefix
# caching kicks in (cache reads cost ~10% of fresh tokens AND are ~3-5x faster
# to process). The dynamic half carries per-turn state (slots, date, intent)
# and is NOT cached.
#
# The format() placeholders are intentionally only in the *_DYNAMIC pieces,
# never in *_STATIC, otherwise the cache key would change each turn and we'd
# get nothing — which was the bug before this refactor.

GREETING_SYSTEM_STATIC = """You are SpeedCare's voice assistant for vehicle service bookings.
""" + VOICE_STYLE + """
Your ONLY tasks: 1) Greet the caller briefly. 2) Identify their intent.
Supported intents: book_new_service, check_booking_status, general_service_inquiry.
Do NOT discuss anything outside vehicle service."""

GREETING_SYSTEM_DYNAMIC = """Language: respond ONLY in {language}.
Use natural, local, everyday {language} — not formal or textbook style.
For Malayalam: speak like a friendly Kerala local, not a newsreader.
For Tamil: speak like a Chennai local, casual and warm.
For Hindi: speak like a natural conversational Hindi, not formal.
Today's date: {today}."""

BOOKING_SYSTEM_STATIC = """You are collecting service booking information for a vehicle service center.
""" + VOICE_STYLE + """
Ask for ONE missing slot at a time. Be conversational and helpful.
Normalize vehicle numbers to format: XX00XX0000 (state code + digits + letters + digits).
If caller says something unrelated to vehicle service, politely redirect.
NEVER invent data. NEVER confirm a booking yourself — that happens next.
CRITICAL MEMORY RULE: The "Already collected" section below is ground truth.
NEVER ask for a slot that already has a value in "Already collected" — not even to confirm.
Only ask for slots listed under "Still needed".
Available service types: general_service, oil_change, brake_service, ac_service, tyre_rotation, battery_check, full_inspection, body_repair."""

BOOKING_SYSTEM_DYNAMIC = """Language: respond ONLY in {language}.
Use natural, local, everyday {language} — not formal or textbook style.
Current intent: {intent}.
Already collected (DO NOT ask for these again): {collected_slots}.
Still needed (ask for ONE of these): {slots_to_collect}.
Today's date: {today}."""

CONFIRMATION_SYSTEM_STATIC = """You are summarizing and confirming a service booking.
""" + VOICE_STYLE + """
Read back the details naturally — vehicle number character-by-character, date as a weekday, service type in plain words.
Then ask: "Shall I confirm this booking? Yes or no."
If caller says yes → use tool: create_booking.
If caller says no or asks to change → signal: return_to_collecting."""

CONFIRMATION_SYSTEM_DYNAMIC = """Language: respond ONLY in {language}.
Use natural, local, everyday {language} — not formal or textbook style.
Collected details: {collected_slots}.
Today's date: {today}."""

# Backwards-compat aliases — old code that still does .format() on a single
# string keeps working until we migrate every call site.
GREETING_SYSTEM_PROMPT = GREETING_SYSTEM_STATIC + "\n" + GREETING_SYSTEM_DYNAMIC
BOOKING_SYSTEM_PROMPT = BOOKING_SYSTEM_STATIC + "\n" + BOOKING_SYSTEM_DYNAMIC
CONFIRMATION_SYSTEM_PROMPT = CONFIRMATION_SYSTEM_STATIC + "\n" + CONFIRMATION_SYSTEM_DYNAMIC

GREETINGS = {
    "ta": "வணக்கம்! SpeedCare-க்கு நல்வரவு. உங்கள் வாகன சர்வீஸ் தொடர்பாக எப்படி உதவ முடியும்?",
    "hi": "नमस्ते! SpeedCare में आपका स्वागत है। आपकी गाड़ी की सर्विस के लिए मैं कैसे मदद कर सकता हूँ?",
    "en": "Hello! Welcome to SpeedCare. How can I help you with your vehicle service today?",
    "ml": "നമസ്കാരം! SpeedCare-ലേക്ക് സ്വാഗതം. നിങ്ങളുടെ വാഹനസർവീസിനായി എന്ത് help ആണ് വേണ്ടത്?",
}

FALLBACK_MESSAGES = {
    "ta": "மன்னிக்கவும், தொழில்நுட்ப சிக்கல் ஏற்பட்டுள்ளது. தயவுசெய்து மீண்டும் அழைக்கவும்.",
    "hi": "माफ़ कीजिए, तकनीकी समस्या हो रही है। कृपया दोबारा कॉल करें।",
    "en": "I'm sorry, I'm having trouble right now. Please call back shortly.",
    "ml": "ക്ഷമിക്കണം, സാങ്കേതിക പ്രശ്നം ഉണ്ട്. ദയവായി വീണ്ടും വിളിക്കുക.",
}

SILENCE_PROMPTS = {
    "ta": "நீங்கள் இன்னும் இருக்கிறீர்களா?",
    "hi": "क्या आप अभी भी हैं?",
    "en": "Are you still there?",
    "ml": "നിങ്ങൾ ഇപ്പോഴും ഉണ്ടോ?",
}

SILENCE_GOODBYE = {
    "ta": "பதில் இல்லாததால் call நிறுத்தப்படுகிறது. நன்றி!",
    "hi": "कोई response न मिलने के कारण call समाप्त हो रही है। धन्यवाद!",
    "en": "No response received, ending the call. Thank you!",
    "ml": "പ്രതികരണം ലഭിക്കാത്തതിനാൽ കോൾ അവസാനിപ്പിക്കുന്നു. നന്ദി!",
}

CLARIFICATION_MESSAGES = {
    "ta": "மன்னிக்கவும், சரியாக புரியவில்லை. மீண்டும் சொல்ல முடியுமா?",
    "hi": "माफ़ कीजिए, समझ नहीं आया। क्या आप दोबारा बता सकते हैं?",
    "en": "Sorry, I didn't catch that. Could you please repeat?",
    "ml": "ക്ഷമിക്കണം, മനസ്സിലായില്ല. ദയവായി വീണ്ടും പറയാമോ?",
}

DENIAL_MESSAGES = {
    "en": "No problem. What would you like to change?",
    "ta": "பரவாயில்லை. என்ன மாற்ற வேண்டும்?",
    "hi": "कोई बात नहीं। क्या बदलना चाहते हैं?",
    "ml": "പ്രശ്നമില്ല. എന്ത് മാറ്റണം?",
}

BOOKING_CONFIRMED_TEMPLATE = {
    "en": "Booked! Your reference is {ref}. We'll see you on {date} at {slot}. Thank you for choosing SpeedCare!",
    "ta": "பதிவு ஆகிவிட்டது! உங்கள் reference number {ref}. {date} அன்று {slot} மணிக்கு வரவும். SpeedCare-ஐ தேர்ந்தெடுத்ததற்கு நன்றி!",
    "hi": "बुकिंग हो गई! आपका reference number {ref} है। {date} को {slot} बजे आइए। SpeedCare को चुनने के लिए धन्यवाद!",
    "ml": "ബുക്കിംഗ് ആയി! നിങ്ങളുടെ reference number {ref} ആണ്. {date} ന് {slot} ന് വരൂ. SpeedCare തിരഞ്ഞെടുത്തതിന് നന്ദി!",
}

BOOKING_DB_ERROR_MESSAGES = {
    "en": "Sorry, I couldn't save the booking just now. Please try again.",
    "ta": "மன்னிக்கவும், இப்போது booking சேமிக்க முடியவில்லை. மீண்டும் முயற்சிக்கவும்.",
    "hi": "माफ़ कीजिए, अभी booking save नहीं हो सकी। कृपया दोबारा कोशिश करें।",
    "ml": "ക്ഷമിക്കണം, ഇപ്പോൾ ബുക്കിംഗ് സേവ് ചെയ്യാൻ കഴിഞ്ഞില്ല. വീണ്ടും ശ്രമിക്കൂ.",
}

BOOKING_ERROR_MESSAGES = {
    "en": "Sorry, I had a problem saving your booking. Please call back shortly.",
    "ta": "மன்னிக்கவும், booking சேமிக்கும்போது பிரச்சினை ஏற்பட்டது. சற்று நேரத்தில் மீண்டும் அழைக்கவும்.",
    "hi": "माफ़ कीजिए, booking save करते समय कोई समस्या आई। थोड़ी देर में दोबारा call करें।",
    "ml": "ക്ഷമിക്കണം, ബുക്കിംഗ് സേവ് ചെയ്യുമ്പോൾ പ്രശ്നം ഉണ്ടായി. അൽപ്പ സമയത്തിനുള്ളിൽ വീണ്ടും വിളിക്കൂ.",
}

GOODBYE_MESSAGES = {
    "en": "Thank you for calling SpeedCare. Goodbye!",
    "ta": "SpeedCare-ஐ அழைத்ததற்கு நன்றி. வணக்கம்!",
    "hi": "SpeedCare को call करने के लिए धन्यवाद। अलविदा!",
    "ml": "SpeedCare-ൽ വിളിച்ചതിന് നന്ദി. ഗുഡ്ബൈ!",
}
