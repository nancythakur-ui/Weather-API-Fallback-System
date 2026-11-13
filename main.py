
import os
import time
import json
import logging
import threading
from datetime import datetime, timezone

import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# =====================================================
# CONFIGURATION
# =====================================================

PORT = int(os.environ.get("PORT", 5000))

CACHE_TTL = 300
REQUEST_TIMEOUT = 10
RETRIES = 2
COOLDOWN = 90

CACHE_FILE = "weather_cache.json"

WEATHERAPI_KEY = os.getenv("WEATHERAPI_KEY", "624a04235f0e463f98b71456261311")
OPENWEATHER_KEY = os.getenv("OPENWEATHER_KEY", "7671ab52b56a50f56c17667747d9176e")

HEADERS = {
    "User-Agent": "WeatherDashboard/3.0"
}

logging.basicConfig(
    filename="weather_errors.log",
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

cache = {}
geocode_cache = {}

cache_lock = threading.Lock()
cooldown_lock = threading.Lock()

cooldowns = {}


# =====================================================
# HTTP REQUEST WITH RETRIES
# =====================================================

def fetch_json(url, params=None, headers=None):

    last_error = None

    for attempt in range(RETRIES):

        try:
            response = requests.get(
                url,
                params=params,
                headers=headers or HEADERS,
                timeout=REQUEST_TIMEOUT
            )

            response.raise_for_status()
            return response.json()

        except (requests.RequestException, ValueError) as exc:

            last_error = exc

            logging.warning(
                "Request failed: %s | Attempt: %s",
                exc,
                attempt + 1
            )

            if attempt < RETRIES - 1:
                time.sleep(attempt + 1)

    raise RuntimeError(str(last_error))


# =====================================================
# PERSISTENT CACHE
# =====================================================

def load_cache():

    global cache

    try:
        if os.path.exists(CACHE_FILE):

            with open(CACHE_FILE, "r", encoding="utf-8") as file:
                saved = json.load(file)

            with cache_lock:
                cache.update(saved)

    except Exception:
        logging.exception("Cache loading failed")


def persist_cache():

    try:
        with cache_lock:
            snapshot = dict(cache)

        temp_file = CACHE_FILE + ".tmp"

        with open(temp_file, "w", encoding="utf-8") as file:
            json.dump(snapshot, file)

        os.replace(temp_file, CACHE_FILE)

    except Exception:
        logging.exception("Cache saving failed")


def get_cache(city, allow_expired=False):

    with cache_lock:

        item = cache.get(city.lower())

        if not item:
            return None

        age = int(time.time() - item["saved_at"])

        if age > CACHE_TTL and not allow_expired:
            return None

        result = item["data"].copy()

    result["cache_age_seconds"] = age
    return result


def save_cache(city, data):

    with cache_lock:

        cache[city.lower()] = {
            "saved_at": time.time(),
            "data": data.copy()
        }

    persist_cache()


# =====================================================
# GEOCODING
# =====================================================

def geocode(city):

    key = city.lower()

    if key in geocode_cache:
        return geocode_cache[key]

    errors = []

    # Source 1: Open-Meteo Geocoding
    try:

        data = fetch_json(
            "https://geocoding-api.open-meteo.com/v1/search",
            {
                "name": city,
                "count": 5,
                "language": "en",
                "format": "json"
            }
        )

        results = data.get("results") or []

        if results:

            match = next(
                (
                    p for p in results
                    if p.get("name", "").lower() == city.lower()
                ),
                results[0]
            )

            result = {
                "name": match["name"],
                "country": match.get("country", "Unknown"),
                "latitude": float(match["latitude"]),
                "longitude": float(match["longitude"]),
                "timezone": match.get("timezone", "auto")
            }

            geocode_cache[key] = result
            return result

    except Exception as exc:
        errors.append("Open-Meteo Geocoding: " + str(exc))

    raise RuntimeError(
        "Geocoding failed: " + " | ".join(errors)
    )


# =====================================================
# VALIDATION AND STANDARD FORMAT
# =====================================================

def validate(data):

    for field in (
        "temperature",
        "feels_like",
        "humidity",
        "wind_speed"
    ):

        value = data.get(field)

        if value is None:
            raise ValueError("Missing " + field)

        if not isinstance(value, (int, float)):
            raise ValueError("Invalid " + field)

    if not -90 <= data["temperature"] <= 65:
        raise ValueError("Temperature out of range")

    if not 0 <= data["humidity"] <= 100:
        raise ValueError("Humidity out of range")

    if data["wind_speed"] < 0:
        raise ValueError("Invalid wind speed")

    return data


def make_result(
    location,
    temperature,
    feels_like,
    condition,
    humidity,
    wind_speed,
    precipitation,
    updated_at,
    source,
    timezone_name=None
):

    return validate({

        "city": location.get("name", "Unknown"),
        "country": location.get("country", "Unknown"),

        "temperature": float(temperature),
        "feels_like": float(feels_like),

        "condition": str(condition or "Unknown"),

        "humidity": float(humidity),
        "wind_speed": float(wind_speed),

        "precipitation": float(precipitation or 0),

        "updated_at": updated_at,

        "timezone": timezone_name or location.get("timezone", "auto"),

        "source": source
    })


# =====================================================
# PROVIDER 1: OPEN-METEO
# =====================================================

def open_meteo(location):

    data = fetch_json(
        "https://api.open-meteo.com/v1/forecast",
        {
            "latitude": location["latitude"],
            "longitude": location["longitude"],

            "current": (
                "temperature_2m,"
                "relative_humidity_2m,"
                "apparent_temperature,"
                "precipitation,"
                "weather_code,"
                "wind_speed_10m"
            ),

            "timezone": location["timezone"]
        }
    )

    current = data.get("current")

    if not current:
        raise ValueError("No current weather")

    codes = {
        0: "Clear sky",
        1: "Mainly clear",
        2: "Partly cloudy",
        3: "Overcast",
        45: "Fog",
        48: "Fog",
        51: "Light drizzle",
        53: "Drizzle",
        55: "Heavy drizzle",
        61: "Light rain",
        63: "Moderate rain",
        65: "Heavy rain",
        71: "Light snow",
        73: "Snow",
        75: "Heavy snow",
        80: "Rain showers",
        81: "Rain showers",
        82: "Heavy rain showers",
        95: "Thunderstorm",
        96: "Thunderstorm with hail",
        99: "Heavy thunderstorm"
    }

    return make_result(
        location,
        current.get("temperature_2m"),
        current.get("apparent_temperature"),
        codes.get(current.get("weather_code"), "Unknown"),
        current.get("relative_humidity_2m"),
        current.get("wind_speed_10m"),
        current.get("precipitation"),
        current.get("time"),
        "Open-Meteo",
        data.get("timezone")
    )


# =====================================================
# PROVIDER 2: WTTR.IN
# =====================================================

def wttr(location, city):

    url = "https://wttr.in/" + requests.utils.quote(city)

    data = fetch_json(url, {"format": "j1"})

    current_list = data.get("current_condition") or []

    if not current_list:
        raise ValueError("No WTTR weather")

    current = current_list[0]

    descriptions = current.get("weatherDesc") or []

    return make_result(
        location,
        current["temp_C"],
        current["FeelsLikeC"],
        descriptions[0].get("value", "Unknown") if descriptions else "Unknown",
        current["humidity"],
        current["windspeedKmph"],
        current.get("precipMM", 0),
        current.get("localObsDateTime"),
        "WTTR.in"
    )


# =====================================================
# PROVIDER 3: WEATHERAPI.COM
# =====================================================

def weatherapi(location, city):

    if not WEATHERAPI_KEY:
        raise RuntimeError("WeatherAPI key not configured")

    data = fetch_json(
        "https://api.weatherapi.com/v1/current.json",
        {
            "key": WEATHERAPI_KEY,
            "q": city,
            "aqi": "no"
        }
    )

    current = data["current"]

    api_location = data.get("location", {})

    result_location = location.copy()
    result_location["name"] = api_location.get("name", location["name"])
    result_location["country"] = api_location.get(
        "country", location["country"]
    )

    return make_result(
        result_location,
        current["temp_c"],
        current["feelslike_c"],
        current["condition"]["text"],
        current["humidity"],
        current["wind_kph"],
        current.get("precip_mm", 0),
        current.get("last_updated"),
        "WeatherAPI",
        api_location.get("tz_id")
    )


# =====================================================
# PROVIDER 4: OPENWEATHERMAP
# =====================================================

def openweathermap(location):

    if not OPENWEATHER_KEY:
        raise RuntimeError("OpenWeatherMap key not configured")

    data = fetch_json(
        "https://api.openweathermap.org/data/2.5/weather",
        {
            "lat": location["latitude"],
            "lon": location["longitude"],
            "appid": OPENWEATHER_KEY,
            "units": "metric"
        }
    )

    weather = (data.get("weather") or [{}])[0]
    main = data.get("main", {})
    wind = data.get("wind", {})

    updated = data.get("dt")

    updated_at = (
        datetime.fromtimestamp(
            updated,
            timezone.utc
        ).isoformat()
        if updated else None
    )

    return make_result(
        location,
        main.get("temp"),
        main.get("feels_like"),
        weather.get("description", "Unknown"),
        main.get("humidity"),
        float(wind.get("speed", 0)) * 3.6,
        data.get("rain", {}).get("1h", 0),
        updated_at,
        "OpenWeatherMap"
    )


# =====================================================
# PROVIDER REGISTRY
# =====================================================

PROVIDERS = {
    "Open-Meteo": open_meteo,
    "WTTR.in": wttr,
    "WeatherAPI": weatherapi,
    "OpenWeatherMap": openweathermap
}

PROVIDER_PRIORITY = [
    "Open-Meteo",
    "WTTR.in",
    "WeatherAPI",
    "OpenWeatherMap"
]


def provider_enabled(name):

    if name == "WeatherAPI":
        return bool(WEATHERAPI_KEY)

    if name == "OpenWeatherMap":
        return bool(OPENWEATHER_KEY)

    return True


# =====================================================
# PROVIDER COOLDOWN
# =====================================================

def is_available(name):

    with cooldown_lock:

        failed_at = cooldowns.get(name)

        if failed_at is None:
            return True

        if time.time() - failed_at >= COOLDOWN:
            cooldowns.pop(name, None)
            return True

        return False


def mark_failed(name):

    with cooldown_lock:
        cooldowns[name] = time.time()


def mark_success(name):

    with cooldown_lock:
        cooldowns.pop(name, None)


# =====================================================
# SEQUENTIAL PROVIDER ENGINE
# =====================================================

def run_provider(name, location, city):

    function = PROVIDERS[name]

    if name in ("WTTR.in", "WeatherAPI"):
        return function(location, city)

    return function(location)


def fetch_all(location, city):

    results = {}
    errors = {}

    for name in PROVIDER_PRIORITY:

        if not provider_enabled(name):
            errors[name] = "API key not configured"
            continue

        if not is_available(name):
            errors[name] = "Temporarily in cooldown"
            continue

        if name in (
            "Open-Meteo",
            "OpenWeatherMap"
        ):
            if location.get("latitude") is None:
                errors[name] = "Coordinates unavailable"
                continue

        try:

            print("Checking provider:", name)

            result = run_provider(name, location, city)

            results[name] = result

            mark_success(name)

            print("SUCCESS:", name)

            # Stop after first successful provider.
            return results, errors

        except Exception as exc:

            errors[name] = str(exc)

            mark_failed(name)

            logging.error(
                "%s failed: %s",
                name,
                exc
            )

            print("FAILED:", name, "-", exc)

    return results, errors


# =====================================================
# MAIN WEATHER ENGINE
# =====================================================

def get_weather(city, fresh=False):

    if not fresh:

        saved = get_cache(city)

        if saved:
            saved["cached"] = True
            saved["stale"] = False
            return saved

    errors = {}

    try:

        location = geocode(city)

    except Exception as exc:

        logging.warning("Geocoding failed: %s", exc)

        errors["Geocoding"] = str(exc)

        location = {
            "name": city,
            "country": "Unknown",
            "timezone": "auto"
        }

    results, provider_errors = fetch_all(location, city)

    errors.update(provider_errors)

    primary = None

    for name in PROVIDER_PRIORITY:

        if name in results:
            primary = results[name]
            break

    if primary:

        comparison = {}

        for name, data in results.items():

            comparison[name] = {
                "temperature": data["temperature"],
                "feels_like": data["feels_like"],
                "condition": data["condition"],
                "humidity": data["humidity"],
                "wind_speed": data["wind_speed"],
                "updated_at": data["updated_at"]
            }

        output = primary.copy()

        output["comparison"] = comparison
        output["provider_errors"] = errors

        output["providers_successful"] = list(results.keys())
        output["providers_failed"] = list(errors.keys())

        output["cached"] = False
        output["stale"] = False
        output["cache_age_seconds"] = 0

        save_cache(city, output)

        return output

    old = get_cache(city, allow_expired=True)

    if old:

        old["cached"] = True
        old["stale"] = True

        old["provider_errors"] = errors

        old["providers_successful"] = []
        old["providers_failed"] = list(errors.keys())

        return old

    return None


# =====================================================
# ROUTES
# =====================================================

@app.route("/")
def home():

    return jsonify({
        "status": "running",
        "name": "Multi-Provider Weather API",

        "providers": PROVIDER_PRIORITY,

        "enabled_providers": [
            name for name in PROVIDER_PRIORITY
            if provider_enabled(name)
        ],

        "features": [
            "Four provider support",
            "Sequential provider checking",
            "Automatic fallback",
            "Provider priority",
            "Automatic retries",
            "Provider cooldown",
            "Provider recovery",
            "Open-Meteo geocoding",
            "Persistent cache",
            "Stale cache fallback",
            "Weather comparison",
            "Data validation"
        ],

        "examples": [
            "/weather/Mumbai",
            "/weather/Mumbai?fresh=true",
            "/api/weather?city=Delhi&fresh=true",
            "/health"
        ]
    })


@app.route("/health")
def health():

    return jsonify({

        "status": "healthy",

        "time": datetime.now(timezone.utc).isoformat(),

        "providers": {

            name: {
                "enabled": provider_enabled(name),
                "available": (
                    is_available(name)
                    if provider_enabled(name)
                    else False
                )
            }

            for name in PROVIDER_PRIORITY
        },

        "cache_entries": len(cache)

    })


@app.route("/weather/<path:city>")
def weather_by_city(city):

    city = city.strip()

    if not city:
        return jsonify({
            "status": "error",
            "message": "City required"
        }), 400

    fresh = request.args.get(
        "fresh", ""
    ).lower() == "true"

    try:

        result = get_weather(city, fresh)

        if result is None:

            return jsonify({
                "status": "error",
                "message": (
                    "All providers unavailable "
                    "and no cached data exists"
                )
            }), 503

        return jsonify({
            "status": "success",
            "data": result
        })

    except Exception as exc:

        logging.exception("Weather route failed")

        return jsonify({
            "status": "error",
            "message": str(exc)
        }), 500


@app.route("/api/weather")
def weather_by_query():

    city = request.args.get("city", "").strip()

    if not city:

        return jsonify({
            "status": "error",
            "message": "City parameter required"
        }), 400

    fresh = request.args.get(
        "fresh", ""
    ).lower() == "true"

    try:

        result = get_weather(city, fresh)

        if result is None:

            return jsonify({
                "status": "error",
                "message": (
                    "All providers unavailable "
                    "and no cached data exists"
                )
            }), 503

        return jsonify({
            "status": "success",
            "data": result
        })

    except Exception as exc:

        logging.exception("Weather query failed")

        return jsonify({
            "status": "error",
            "message": str(exc)
        }), 500


# =====================================================
# CORS
# =====================================================

@app.after_request
def cors(response):

    response.headers["Access-Control-Allow-Origin"] = "*"

    response.headers["Access-Control-Allow-Methods"] = (
        "GET, OPTIONS"
    )

    response.headers["Access-Control-Allow-Headers"] = (
        "Content-Type"
    )

    return response


# =====================================================
# START
# =====================================================

load_cache()

if __name__ == "__main__":

    print("=" * 60)
    print("       MULTI-PROVIDER WEATHER API")
    print("=" * 60)

    for name in PROVIDER_PRIORITY:

        status = (
            "ENABLED"
            if provider_enabled(name)
            else "WAITING FOR API KEY"
        )

        print(name + ": " + status)

    print("-" * 60)
    print("Provider checking: SEQUENTIAL")
    print("Automatic fallback: ENABLED")
    print("Retries: ENABLED")
    print("Provider recovery: ENABLED")
    print("Persistent cache: ENABLED")
    print("Weatherbit: REMOVED")
    print("MET Norway: REMOVED")
    print("URL: http://localhost:" + str(PORT))
    print("=" * 60)

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        threaded=True
    )
