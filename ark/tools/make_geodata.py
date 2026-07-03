#!/usr/bin/env python3
"""Generate ark/geodata/cities.csv — the bundled offline reverse-geocode dataset.

This ships a curated set of major world cities (enough to resolve most real GPS
coordinates to a sensible nearest place, fully offline). For exhaustive coverage,
replace CITIES with a parse of a GeoNames `cities1000.txt` / `cities15000.txt`
dump — the geocoder code does not change.

Run:  python tools/make_geodata.py
"""
from __future__ import annotations

import csv
from pathlib import Path

# name, admin (state/region), country, country_code, lat, lon
CITIES = [
    # --- India (incl. the demo's Goa) ---
    ("Panaji", "Goa", "India", "IN", 15.4909, 73.8278),
    ("Goa", "Goa", "India", "IN", 15.2993, 74.1240),
    ("Margao", "Goa", "India", "IN", 15.2832, 73.9862),
    ("New Delhi", "Delhi", "India", "IN", 28.6139, 77.2090),
    ("Mumbai", "Maharashtra", "India", "IN", 19.0760, 72.8777),
    ("Bengaluru", "Karnataka", "India", "IN", 12.9716, 77.5946),
    ("Chennai", "Tamil Nadu", "India", "IN", 13.0827, 80.2707),
    ("Kolkata", "West Bengal", "India", "IN", 22.5726, 88.3639),
    ("Hyderabad", "Telangana", "India", "IN", 17.3850, 78.4867),
    ("Pune", "Maharashtra", "India", "IN", 18.5204, 73.8567),
    ("Jaipur", "Rajasthan", "India", "IN", 26.9124, 75.7873),
    ("Ahmedabad", "Gujarat", "India", "IN", 23.0225, 72.5714),
    ("Kochi", "Kerala", "India", "IN", 9.9312, 76.2673),
    ("Varanasi", "Uttar Pradesh", "India", "IN", 25.3176, 82.9739),
    # --- USA ---
    ("San Francisco", "California", "United States", "US", 37.7749, -122.4194),
    ("Los Angeles", "California", "United States", "US", 34.0522, -118.2437),
    ("San Jose", "California", "United States", "US", 37.3382, -121.8863),
    ("Seattle", "Washington", "United States", "US", 47.6062, -122.3321),
    ("New York", "New York", "United States", "US", 40.7128, -74.0060),
    ("Chicago", "Illinois", "United States", "US", 41.8781, -87.6298),
    ("Austin", "Texas", "United States", "US", 30.2672, -97.7431),
    ("Boston", "Massachusetts", "United States", "US", 42.3601, -71.0589),
    ("Denver", "Colorado", "United States", "US", 39.7392, -104.9903),
    ("Miami", "Florida", "United States", "US", 25.7617, -80.1918),
    ("Washington", "District of Columbia", "United States", "US", 38.9072, -77.0369),
    # --- Canada ---
    ("Toronto", "Ontario", "Canada", "CA", 43.6532, -79.3832),
    ("Vancouver", "British Columbia", "Canada", "CA", 49.2827, -123.1207),
    ("Montreal", "Quebec", "Canada", "CA", 45.5019, -73.5674),
    # --- UK / Ireland ---
    ("London", "England", "United Kingdom", "GB", 51.5074, -0.1278),
    ("Manchester", "England", "United Kingdom", "GB", 53.4808, -2.2426),
    ("Edinburgh", "Scotland", "United Kingdom", "GB", 55.9533, -3.1883),
    ("Dublin", "Leinster", "Ireland", "IE", 53.3498, -6.2603),
    # --- Europe ---
    ("Paris", "Ile-de-France", "France", "FR", 48.8566, 2.3522),
    ("Nice", "Provence-Alpes-Cote d'Azur", "France", "FR", 43.7102, 7.2620),
    ("Berlin", "Berlin", "Germany", "DE", 52.5200, 13.4050),
    ("Munich", "Bavaria", "Germany", "DE", 48.1351, 11.5820),
    ("Amsterdam", "North Holland", "Netherlands", "NL", 52.3676, 4.9041),
    ("Madrid", "Madrid", "Spain", "ES", 40.4168, -3.7038),
    ("Barcelona", "Catalonia", "Spain", "ES", 41.3851, 2.1734),
    ("Lisbon", "Lisbon", "Portugal", "PT", 38.7223, -9.1393),
    ("Rome", "Lazio", "Italy", "IT", 41.9028, 12.4964),
    ("Milan", "Lombardy", "Italy", "IT", 45.4642, 9.1900),
    ("Zurich", "Zurich", "Switzerland", "CH", 47.3769, 8.5417),
    ("Vienna", "Vienna", "Austria", "AT", 48.2082, 16.3738),
    ("Prague", "Prague", "Czechia", "CZ", 50.0755, 14.4378),
    ("Copenhagen", "Capital Region", "Denmark", "DK", 55.6761, 12.5683),
    ("Stockholm", "Stockholm", "Sweden", "SE", 59.3293, 18.0686),
    ("Oslo", "Oslo", "Norway", "NO", 59.9139, 10.7522),
    ("Helsinki", "Uusimaa", "Finland", "FI", 60.1699, 24.9384),
    ("Warsaw", "Masovia", "Poland", "PL", 52.2297, 21.0122),
    ("Athens", "Attica", "Greece", "GR", 37.9838, 23.7275),
    ("Istanbul", "Istanbul", "Turkey", "TR", 41.0082, 28.9784),
    ("Moscow", "Moscow", "Russia", "RU", 55.7558, 37.6173),
    # --- Middle East ---
    ("Dubai", "Dubai", "United Arab Emirates", "AE", 25.2048, 55.2708),
    ("Abu Dhabi", "Abu Dhabi", "United Arab Emirates", "AE", 24.4539, 54.3773),
    ("Doha", "Doha", "Qatar", "QA", 25.2854, 51.5310),
    ("Tel Aviv", "Tel Aviv", "Israel", "IL", 32.0853, 34.7818),
    # --- East / SE Asia ---
    ("Tokyo", "Tokyo", "Japan", "JP", 35.6762, 139.6503),
    ("Osaka", "Osaka", "Japan", "JP", 34.6937, 135.5023),
    ("Kyoto", "Kyoto", "Japan", "JP", 35.0116, 135.7681),
    ("Seoul", "Seoul", "South Korea", "KR", 37.5665, 126.9780),
    ("Beijing", "Beijing", "China", "CN", 39.9042, 116.4074),
    ("Shanghai", "Shanghai", "China", "CN", 31.2304, 121.4737),
    ("Hong Kong", "Hong Kong", "Hong Kong", "HK", 22.3193, 114.1694),
    ("Singapore", "Singapore", "Singapore", "SG", 1.3521, 103.8198),
    ("Bangkok", "Bangkok", "Thailand", "TH", 13.7563, 100.5018),
    ("Kuala Lumpur", "Kuala Lumpur", "Malaysia", "MY", 3.1390, 101.6869),
    ("Jakarta", "Jakarta", "Indonesia", "ID", -6.2088, 106.8456),
    ("Bali", "Bali", "Indonesia", "ID", -8.4095, 115.1889),
    ("Manila", "Metro Manila", "Philippines", "PH", 14.5995, 120.9842),
    ("Ho Chi Minh City", "Ho Chi Minh", "Vietnam", "VN", 10.8231, 106.6297),
    ("Colombo", "Western", "Sri Lanka", "LK", 6.9271, 79.8612),
    ("Kathmandu", "Bagmati", "Nepal", "NP", 27.7172, 85.3240),
    ("Dhaka", "Dhaka", "Bangladesh", "BD", 23.8103, 90.4125),
    # --- Oceania ---
    ("Sydney", "New South Wales", "Australia", "AU", -33.8688, 151.2093),
    ("Melbourne", "Victoria", "Australia", "AU", -37.8136, 144.9631),
    ("Auckland", "Auckland", "New Zealand", "NZ", -36.8485, 174.7633),
    # --- Africa ---
    ("Cairo", "Cairo", "Egypt", "EG", 30.0444, 31.2357),
    ("Nairobi", "Nairobi", "Kenya", "KE", -1.2921, 36.8219),
    ("Cape Town", "Western Cape", "South Africa", "ZA", -33.9249, 18.4241),
    ("Johannesburg", "Gauteng", "South Africa", "ZA", -26.2041, 28.0473),
    ("Lagos", "Lagos", "Nigeria", "NG", 6.5244, 3.3792),
    ("Casablanca", "Casablanca-Settat", "Morocco", "MA", 33.5731, -7.5898),
    # --- Latin America ---
    ("Mexico City", "Mexico City", "Mexico", "MX", 19.4326, -99.1332),
    ("Sao Paulo", "Sao Paulo", "Brazil", "BR", -23.5558, -46.6396),
    ("Rio de Janeiro", "Rio de Janeiro", "Brazil", "BR", -22.9068, -43.1729),
    ("Buenos Aires", "Buenos Aires", "Argentina", "AR", -34.6037, -58.3816),
    ("Santiago", "Santiago", "Chile", "CL", -33.4489, -70.6693),
    ("Lima", "Lima", "Peru", "PE", -12.0464, -77.0428),
    ("Bogota", "Bogota", "Colombia", "CO", 4.7110, -74.0721),
]


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "ark" / "geodata" / "cities.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["name", "admin", "country", "country_code", "lat", "lon"])
        for name, admin, country, cc, lat, lon in CITIES:
            w.writerow([name, admin, country, cc, lat, lon])
    print(f"wrote {len(CITIES)} cities -> {out}")


if __name__ == "__main__":
    main()
