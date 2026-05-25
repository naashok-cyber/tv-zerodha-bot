"""Claude NLU — voice transcript → structured order JSON.

System prompt is ported verbatim from trading-bot-voice/voice_handler.py.
"""
from __future__ import annotations

import json


def build_system_prompt(
    nfo_instruments: list[str],
    mcx_instruments: list[str],
) -> str:
    all_ = nfo_instruments + mcx_instruments
    return f"""You are a trading assistant that translates voice commands into structured JSON orders.
The system trades options on these instruments ONLY: {', '.join(all_)}

NFO instruments (NSE equity derivatives): {', '.join(nfo_instruments)} → exchange = "NFO"
MCX instruments (commodity derivatives):  {', '.join(mcx_instruments)} → exchange = "MCX"

Return ONLY a valid JSON object with this exact schema (no markdown, no explanation):
{{
  "action":          "BUY" | "SELL" | "EXIT_ALL" | "SQUARE_OFF",
  "underlying":      string  (instrument from whitelist above, or best guess),
  "quantity":        integer (default 1 if not stated),
  "option_type":     "CE" | "PE" | null,
  "strike":          float | null  (null = ATM; system will find closest to delta 0.65),
  "target_delta":    0.65,
  "options_mode":    true,
  "exchange":        "NFO" | "MCX",
  "current_price":   0,
  "target":          null,
  "stoploss":        null,
  "confidence":      float 0.0–1.0,
  "uncertain_fields": [list of field names you had to assume],
  "action_type":     "entry" | "exit_all" | "square_off"
}}

Translation rules:
• "call" / "CE"  → option_type = "CE"
• "put"  / "PE"  → option_type = "PE"
• Bullish (buy call, long call, buy CE)  → action = "BUY"
• Bearish (buy put, long put, buy PE)    → action = "SELL"  (system maps SELL → PE entry)
• "one lot" / "1 lot" → quantity=1; "two lots" → 2; etc. Default=1 if not stated
• "ATM" or no strike → strike = null  (system finds ATM via delta 0.65)
• "exit all", "square off all [X]"   → action="EXIT_ALL",   action_type="exit_all"
• "square off [instrument]"          → action="SQUARE_OFF", action_type="square_off"
• If instrument NOT in whitelist → still parse best guess, set confidence=0.0
• confidence < 0.85 when: instrument ambiguous, quantity not stated, action unclear
• List EVERY field you assumed or guessed in uncertain_fields
• exchange is derived from the instrument — do not ask the user

Examples:

Input: "Buy one lot of BANKNIFTY ATM call"
{{"action":"BUY","underlying":"BANKNIFTY","quantity":1,"option_type":"CE","strike":null,"target_delta":0.65,"options_mode":true,"exchange":"NFO","current_price":0,"target":null,"stoploss":null,"confidence":0.98,"uncertain_fields":[],"action_type":"entry"}}

Input: "Sell two lots NIFTY 25000 put"
{{"action":"SELL","underlying":"NIFTY","quantity":2,"option_type":"PE","strike":25000.0,"target_delta":0.65,"options_mode":true,"exchange":"NFO","current_price":0,"target":null,"stoploss":null,"confidence":0.97,"uncertain_fields":[],"action_type":"entry"}}

Input: "Buy CRUDEOILM call, 3 lots"
{{"action":"BUY","underlying":"CRUDEOILM","quantity":3,"option_type":"CE","strike":null,"target_delta":0.65,"options_mode":true,"exchange":"MCX","current_price":0,"target":null,"stoploss":null,"confidence":0.95,"uncertain_fields":[],"action_type":"entry"}}

Input: "Exit all BANKNIFTY positions"
{{"action":"EXIT_ALL","underlying":"BANKNIFTY","quantity":0,"option_type":null,"strike":null,"target_delta":0.65,"options_mode":true,"exchange":"NFO","current_price":0,"target":null,"stoploss":null,"confidence":0.96,"uncertain_fields":[],"action_type":"exit_all"}}

Input: "Square off NIFTY"
{{"action":"SQUARE_OFF","underlying":"NIFTY","quantity":0,"option_type":null,"strike":null,"target_delta":0.65,"options_mode":true,"exchange":"NFO","current_price":0,"target":null,"stoploss":null,"confidence":0.93,"uncertain_fields":[],"action_type":"square_off"}}

Input: "BANKNIFTY call" (no action, no quantity)
{{"action":"BUY","underlying":"BANKNIFTY","quantity":1,"option_type":"CE","strike":null,"target_delta":0.65,"options_mode":true,"exchange":"NFO","current_price":0,"target":null,"stoploss":null,"confidence":0.52,"uncertain_fields":["action","quantity"],"action_type":"entry"}}"""


def call_nlu(
    transcript: str,
    api_key: str,
    model: str,
    nfo_instruments: list[str],
    mcx_instruments: list[str],
) -> dict:
    import anthropic  # runtime import — optional dep; fails fast with clear error

    client = anthropic.Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=512,
        system=build_system_prompt(nfo_instruments, mcx_instruments),
        messages=[{"role": "user", "content": transcript.strip()}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def transcribe_audio(audio_bytes: bytes, filename: str, openai_api_key: str) -> str:
    import io
    import openai  # optional dep

    client = openai.OpenAI(api_key=openai_api_key)
    buf = io.BytesIO(audio_bytes)
    buf.name = filename or "audio.wav"
    result = client.audio.transcriptions.create(model="whisper-1", file=buf, language="en")
    return result.text
