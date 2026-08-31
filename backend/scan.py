"""Find nearby Movesense sensors and print their BLE address.

Usage:
    python scan.py

Copy the printed address into your .env file as MOVESENSE_ADDRESS.
"""

import asyncio

from bleak import BleakScanner

from movesense_ble import MovesenseBLE


async def main():
    print("Scanning for 8 seconds... make sure the sensor is powered on and nearby.")
    devices = await MovesenseBLE.discover()
    if devices:
        print("\nFound:")
        for d in devices:
            print(f"  {d.name}  ->  {d.address}")
        return

    print("No device named 'Movesense...' found. Showing every BLE device seen,")
    print("in case the sensor is advertising under a different name:\n")
    all_devices = await BleakScanner.discover(timeout=8.0)
    if not all_devices:
        print("  (nothing at all - this points to a scanning problem, not a naming")
        print("  one: check macOS Bluetooth permission for this terminal/IDE in")
        print("  System Settings -> Privacy & Security -> Bluetooth.)")
        return
    for d in all_devices:
        print(f"  {d.name or '(no name)'}  ->  {d.address}")


if __name__ == "__main__":
    asyncio.run(main())
