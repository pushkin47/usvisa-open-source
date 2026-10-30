"""Local console events. This module does not make network requests."""

async def record_event(message: str) -> bool:
    print(message)
    return True
