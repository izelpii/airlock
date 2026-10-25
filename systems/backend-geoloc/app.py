from flask import Flask, request, jsonify
from flask_cors import CORS
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut, GeocoderServiceError
import osmnx as ox
import time
import pandas as pd

app = Flask(__name__)
CORS(app)


def get_pois_nominatim(lat, lon, limit=5):
    """
    Method 1 (from test2.py): Use Nominatim reverse geocoding with small offsets
    to discover nearby POIs. Returns a list of dicts with 'name' and 'address'.
    """
    geolocator = Nominatim(user_agent="geo_loc_poi_finder")
    pois = []

    offsets = [0.000, 0.001, -0.001, 0.002, -0.002]

    try:
        for i in range(min(limit, len(offsets))):
            search_lat = lat + offsets[i]
            search_lon = lon + offsets[i]

            location = geolocator.reverse(f"{search_lat}, {search_lon}", zoom=18)

            if location and location.address not in [p['address'] for p in pois]:
                details = location.raw.get('address', {})
                # Build a human-readable name from the address details
                name = (
                    details.get('amenity')
                    or details.get('shop')
                    or details.get('tourism')
                    or details.get('leisure')
                    or details.get('building')
                    or details.get('road')
                    or location.address.split(',')[0]
                )
                pois.append({
                    "name": name,
                    "address": location.address,
                    "latitude": location.latitude,
                    "longitude": location.longitude,
                    "source": "nominatim"
                })

            time.sleep(1)  # Nominatim usage policy: max 1 request/second

        return pois

    except (GeocoderTimedOut, GeocoderServiceError) as e:
        print(f"Nominatim error: {e}")
        return []


def get_pois_osmnx(lat, lon, dist=500):
    """
    Method 2 (from test4.py): Use OSMnx to fetch amenity features within
    `dist` metres of the given point. Returns a list of dicts with 'name' and 'address'.
    """
    point = (lat, lon)
    tags = {'amenity': True}

    try:
        nearby_places = ox.features_from_point(point, tags, dist=dist)
        named_places = nearby_places[nearby_places['name'].notnull()]

        pois = []
        for _, row in named_places.iterrows():
            name = row.get('name', '')
            amenity = row.get('amenity', '')

            # Build address from available OSM address tags
            addr_parts = []
            for field in ['addr:housenumber', 'addr:street', 'addr:city',
                          'addr:state', 'addr:postcode', 'addr:country']:
                val = row.get(field)
                if val and not (isinstance(val, float) and pd.isna(val)):
                    addr_parts.append(str(val))

            address = ', '.join(addr_parts) if addr_parts else (str(amenity) if amenity else '')

            pois.append({
                "name": str(name),
                "address": address,
                "amenity": str(amenity) if amenity else None,
                "source": "osmnx"
            })

        return pois

    except Exception as e:
        print(f"OSMnx error: {e}")
        return []


def combine_pois(nominatim_pois, osmnx_pois):
    """
    Merge results from both methods, deduplicating by name (case-insensitive).
    OSMnx results are preferred when a name collision occurs because they carry
    richer structured data.
    """
    seen_names = set()
    combined = []

    # Add OSMnx results first (richer structured data)
    for poi in osmnx_pois:
        key = poi['name'].strip().lower()
        if key and key not in seen_names:
            seen_names.add(key)
            combined.append({
                "name": poi['name'],
                "address": poi['address'],
                "source": poi['source']
            })

    # Add Nominatim results that aren't already represented
    for poi in nominatim_pois:
        key = poi['name'].strip().lower()
        if key and key not in seen_names:
            seen_names.add(key)
            combined.append({
                "name": poi['name'],
                "address": poi['address'],
                "source": poi['source']
            })

    return combined


@app.route('/poi', methods=['GET'])
def get_poi():
    """
    GET /poi?lat=<latitude>&lon=<longitude>

    Optional query params:
      - limit : max Nominatim offset probes (default 5)
      - dist  : OSMnx search radius in metres (default 500)

    Returns JSON:
    {
      "lat": float,
      "lon": float,
      "count": int,
      "pois": [
        { "name": str, "address": str, "source": str },
        ...
      ]
    }
    """
    lat_str = request.args.get('lat')
    lon_str = request.args.get('lon')

    if not lat_str or not lon_str:
        return jsonify({"error": "Both 'lat' and 'lon' query parameters are required."}), 400

    try:
        lat = float(lat_str)
        lon = float(lon_str)
    except ValueError:
        return jsonify({"error": "'lat' and 'lon' must be valid floating-point numbers."}), 400

    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({"error": "Coordinates out of range."}), 400

    limit = int(request.args.get('limit', 5))
    dist  = int(request.args.get('dist', 500))

    # --- Method 1: Nominatim ---
    nominatim_results = get_pois_nominatim(lat, lon, limit=limit)

    # --- Method 2: OSMnx ---
    osmnx_results = get_pois_osmnx(lat, lon, dist=dist)

    # --- Combine & deduplicate ---
    combined = combine_pois(nominatim_results, osmnx_results)

    return jsonify({
        "lat": lat,
        "lon": lon,
        "count": len(combined),
        "pois": combined
    })


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5002)

