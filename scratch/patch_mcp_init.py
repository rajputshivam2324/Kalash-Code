with open("src/kalash/mcp/client.py") as f:
    code = f.read()

code = code.replace(
    "        self._stdout: asyncio.StreamReader | None = None",
    """        self._stdout: asyncio.StreamReader | None = None
        self._sse_client: Any = None
        self._sse_endpoint: str = ""
        self._post_endpoint: str = "" """
)

with open("src/kalash/mcp/client.py", "w") as f:
    f.write(code)
