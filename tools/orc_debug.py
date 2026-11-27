import asyncio
from game_ai import get_window_region, capture_region, ocr_image

async def main():
    region = get_window_region("No, I am not Human")
    if not region:
        print("Game window not found")
        return

    choice_region = (
        region[0],
        region[1] + int(region[3] * 2 / 3),
        region[2],
        int(region[3] / 3),
    )

    while True:
        img = await capture_region(choice_region)
        text = ocr_image(img)
        print("Detected text:", text)
        await asyncio.sleep(1)

if __name__ == "__main__":
    asyncio.run(main())
