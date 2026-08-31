"""Find nearby Movesense sensors and print their BLE address.

Usage:
    python scan.py

Copy the printed address into your .env file as MOVESENSE_ADDRESS.
"""

import asyncio

from movesense_ble import MovesenseBLE


async def main():
    print("Scanning for 8 seconds... make sure the sensor is powered on and nearby.")
    devices = await MovesenseBLE.discover()
    if not devices:
        print("No Movesense sensor found. Check that it's on and not already")
        print("connected to another app (e.g. the Movesense Showcase phone app).")
        return
    print("\nFound:")
    for d in devices:
        print(f"  {d.name}  ->  {d.address}")


if __name__ == "__main__":
    asyncio.run(main())
