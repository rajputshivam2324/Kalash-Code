from google import genai
from google.genai import types

print(types.Content(role="user", parts=[types.Part.from_text(text="hello")]))
print(types.Content(role="model", parts=[types.Part.from_function_call(name="foo", args={"a": 1})]))
print(types.Content(role="user", parts=[types.Part.from_function_response(name="foo", response={"b": 2})]))
