from __future__ import annotations

GREETING_SYSTEM_PROMPT = """You are SpeedCare's voice assistant for vehicle service bookings.
Language: respond ONLY in {language}. Keep responses under 40 words.
Your ONLY tasks: 1) Greet caller. 2) Identify their intent.
Supported intents: book_new_service, check_booking_status, general_service_inquiry.
Do NOT discuss anything outside vehicle service.
Today's date: {today}."""

BOOKING_SYSTEM_PROMPT = """You are collecting service booking information for a vehicle service center.
Language: respond ONLY in {language}. Keep responses under 50 words.
Current intent: {intent}.
Required slots: {slots_to_collect}.
Already collected: {collected_slots}.
Ask for ONE missing slot at a time. Be conversational and helpful.
Normalize vehicle numbers to format: XX00XX0000 (state code + digits + letters + digits).
If caller says something unrelated to vehicle service, politely redirect.
NEVER invent data. NEVER confirm a booking yourself — that happens next.
Available tools: normalize_vehicle_number, validate_date, check_service_type.
Today's date: {today}.
Available service types: general_service, oil_change, brake_service, ac_service, tyre_rotation, battery_check, full_inspection, body_repair."""

CONFIRMATION_SYSTEM_PROMPT = """You are summarizing and confirming a service booking.
Language: respond ONLY in {language}. Keep response under 60 words.
Collected details: {collected_slots}.
Read back ALL details clearly: vehicle number, service type, date.
Then ask: "Shall I confirm this booking? Yes or No."
If caller says yes → use tool: create_booking.
If caller says no or asks to change → signal: return_to_collecting.
Today's date: {today}."""

GREETINGS = {
    "ta": "வணக்கம்! SpeedCare-க்கு நல்வரவு. உங்கள் வாகன சர்வீஸ் தொடர்பாக எப்படி உதவ முடியும்?",
    "hi": "नमस्ते! SpeedCare में आपका स्वागत है। आपकी गाड़ी की सर्विस के लिए मैं कैसे मदद कर सकता हूँ?",
    "en": "Hello! Welcome to SpeedCare. How can I help you with your vehicle service today?",
    "ml": "നമസ്കാരം! SpeedCare-ലേക്ക് സ്വാഗതം. നിങ്ങളുടെ വാഹന സർവ്വീസിന് എങ്ങനെ സഹായിക്കാം?",
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
