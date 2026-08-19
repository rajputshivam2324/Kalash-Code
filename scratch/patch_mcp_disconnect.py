with open("src/kalash/mcp/client.py") as f:
    code = f.read()

disconnect = """    async def disconnect(self) -> None:
        \"\"\"Close the connection to the MCP server.\"\"\"
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                self._process.kill()
        if self._sse_client:
            await self._sse_client.aclose()
            self._sse_client = None
        self._connected = False
        self._tools.clear()"""

# simple replace
import re
code = re.sub(r'    async def disconnect\(self\) -> None:.*?self\._tools\.clear\(\)', disconnect, code, flags=re.DOTALL)

with open("src/kalash/mcp/client.py", "w") as f:
    f.write(code)
