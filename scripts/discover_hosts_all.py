"""Run host discovery for all cities that have listings but no hosts.json.

Run: cd hostguide && python scripts/discover_hosts_all.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from hostguide.src.host_discovery import HostDiscovery

OUTPUT = Path(__file__).parent.parent / "output"


def main():
    cities = []
    for city_dir in sorted(OUTPUT.iterdir()):
        if not city_dir.is_dir():
            continue
        listings = city_dir / "listings.json"
        hosts = city_dir / "hosts.json"
        if listings.exists() and not hosts.exists():
            cities.append(city_dir.name)

    if not cities:
        print("All cities already have hosts.json. Nothing to do.")
        return

    print(f"Host discovery for {len(cities)} cities: {', '.join(cities)}\n")

    hd = HostDiscovery(headless=False)

    for city in cities:
        print(f"\n{'='*50}")
        print(f"  {city.upper()}")
        print(f"{'='*50}")

        listings_path = str(OUTPUT / city / "listings.json")
        hosts_path = str(OUTPUT / city / "hosts.json")

        try:
            profiles = hd.discover_all(listings_path, max_hosts=15)
            hd.save_profiles(profiles, hosts_path)
        except Exception as e:
            print(f"  ERROR: {e}")

        # Clear singleton lock between cities
        import subprocess
        subprocess.run(["rm", "-f",
                        str(Path(__file__).parent.parent / "chrome_profile_airbnb" / "SingletonLock")],
                       capture_output=True)
        time.sleep(2)

    print(f"\nDone — host discovery complete for {len(cities)} cities.")


if __name__ == "__main__":
    main()
