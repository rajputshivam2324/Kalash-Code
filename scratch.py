from kalash.sdk import KalashClient
import asyncio

async def main():
    async with KalashClient().session() as sess:
        print(sess._agent.host.registry.list_tools()[0].name)

asyncio.run(main())
