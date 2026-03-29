# URL seeding example: Analyze all documentation
from crawl4ai import AsyncUrlSeeder, SeedingConfig

seeder = AsyncUrlSeeder()
config = SeedingConfig(source="sitemap", extract_head=True)


async def main():
    urls = await seeder.urls("shadcn.com", config)
    print(urls)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
