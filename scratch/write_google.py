from google import genai
from google.genai import types

def map_stop_reason(reason):
    if not reason:
        return "unknown"
    r = reason.name.lower()
    if r == "stop": return "end_turn"
    if r == "max_tokens": return "max_tokens"
    if r == "safety": return "content_filter"
    return "unknown"

