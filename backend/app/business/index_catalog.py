import asyncio
import json

from app.business.routing import index_catalog
from app.core.config import get_settings
from app.core.resources import Resources


async def main():
    settings = get_settings()
    resources = Resources(settings)
    try:
        print(json.dumps(await index_catalog(settings, resources.engine)))
    finally:
        await resources.close()


if __name__ == "__main__":
    asyncio.run(main())
