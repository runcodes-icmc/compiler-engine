import asyncio

from rcc import main

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # asyncio.run() re-raises KeyboardInterrupt after gracefully stopping
        # the workers: nothing left to do.
        pass
