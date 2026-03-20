import json
import ssl
from datetime import date
from urllib.request import urlopen
from urllib.error import URLError, HTTPError

import certifi
import openmeteo_requests
from flask import Flask, render_template, request

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DAILY_VARS = [
    "temperature_2m_max",
    "temperature_2m_min",
    "precipitation_probability_max",
    "rain_sum",
    "snowfall_sum",
    "windspeed_10m_max",
]

MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]

app = Flask(__name__)
openmeteo = openmeteo_requests.Client()
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

def leap_year(year):
    return (year % 4 == 0 and year % 100 != 0) or (year % 400 == 0)


def days_in_month(year, month):
    if month in (1, 3, 5, 7, 8, 10, 12):
        return 31
    if month in (4, 6, 9, 11):
        return 30
    return 29 if leap_year(year) else 28


def add_days(d, days_to_add):
    year = d.year
    month = d.month
    day = d.day

    for i in range(days_to_add):
        day += 1
        dim = days_in_month(year, month)
        if day > dim:
            day = 1
            month += 1
            if month > 12:
                month = 1
                year += 1

    return date(year, month, day)


@app.route("/")
def index():
    return render_template("index.html")

@app.route("/plan", methods=["POST"])
def plan():
    destination = (request.form.get("destination") or "").strip()
    start_date_str = request.form.get("start-date")
    trip_length_str = request.form.get("trip-length")
    trip_type = request.form.get("trip-type") or "city"
    packing_style = request.form.get("packing-style") or "moderate"

    if not destination:
        error = "Please enter a destination."
        return render_template("index.html", error=error)

    try:
        trip_length = int(trip_length_str)
    except (TypeError, ValueError):
        error = "Please enter a valid trip length (number of days)."
        return render_template("index.html", error=error)

    if trip_length < 1 or trip_length > 14:
        error = "Trip length must be between 1 and 14 days."
        return render_template("index.html", error=error)

    if not start_date_str:
        error = "Please choose a start date."
        return render_template("index.html", error=error)

    try:
        start_date = date.fromisoformat(start_date_str)
    except ValueError:
        error = "Please enter a valid start date."
        return render_template("index.html", error=error)

    today = date.today()
    if start_date < today:
        error = "Please choose a start date that is today or in the future."
        return render_template("index.html", error=error)

    max_start = add_days(today, 14)
    if start_date > max_start:
        error = "This planner works best for trips starting within the next two weeks."
        return render_template("index.html", error=error)

    location = geocode_location(destination)
    if not location:
        error = (
            "We could not find that place. Try a nearby major city or check the spelling."
        )
        return render_template("index.html", error=error)

    try:
        forecast = fetch_forecast(
            location["latitude"],
            location["longitude"],
            start_date_str,
            trip_length,
            location.get("timezone"),
        )
    except Exception:
        error = "Something went wrong while fetching the forecast. Please try again."
        return render_template("index.html", error=error)

    summary = summarize_weather(forecast)
    checklist = generate_packing_list(summary, trip_type, packing_style, trip_length)

    end_date = add_days(start_date, trip_length - 1)

    def format(d):
        return f"{MONTHS[d.month - 1]} {d.day}"

    date_range = format(start_date) if trip_length == 1 else f"{format(start_date)} – {format(end_date)}"
    pretty_location = (
        f"{location['name']}, {location['country']}"
        if location.get("country")
        else location["name"]
    )
    trip_summary = f"{trip_length}-day {trip_type} trip to {pretty_location} ({date_range})"

    descriptor_text = ", ".join(summary["descriptors"])
    if summary["minTemp"] is not None and summary["maxTemp"] is not None:
        temp_text = (
            f"Temperatures range roughly from {round(summary['minTemp'])}° "
            f"to {round(summary['maxTemp'])}°C."
        )
        weather_summary = f"{descriptor_text}. {temp_text}"
    else:
        weather_summary = descriptor_text

    daily_weather = build_daily_weather(forecast, start_date, trip_length, format)

    return render_template(
        "results.html",
        trip_summary=trip_summary,
        weather_summary=weather_summary,
        checklist=checklist,
        daily_weather=daily_weather,
    )


def day_description(tmax, tmin, precip_prob, rain_sum, snow_sum, wind_max):
    parts = []

    if snow_sum and snow_sum > 0:
        parts.append("Snow possible")
    elif rain_sum and rain_sum > 1:
        parts.append("Rain likely")
    elif precip_prob and precip_prob >= 50:
        parts.append("Rain likely")
    elif precip_prob and precip_prob >= 30:
        parts.append("Chance of rain")
    else:
        parts.append("Dry day")

    if wind_max and wind_max >= 30:
        parts.append("Windy")

    if tmax is not None:
        if tmax >= 30:
            parts.append("Hot")
        elif tmax >= 22:
            parts.append("Warm")
        elif tmax >= 15:
            parts.append("Mild")
        else:
            parts.append("Cool")

    return ", ".join(parts)


def build_daily_weather(forecast, start_date, trip_length, format_fn):
    daily = forecast.get("daily")
    if daily is None:
        daily = {}

    max_temps = daily.get("temperature_2m_max")
    if max_temps is None:
        max_temps = []

    min_temps = daily.get("temperature_2m_min")
    if min_temps is None:
        min_temps = []

    precip_prob = daily.get("precipitation_probability_max")
    if precip_prob is None:
        precip_prob = []

    rain_sum = daily.get("rain_sum")
    if rain_sum is None:
        rain_sum = []

    snow_sum = daily.get("snowfall_sum")
    if snow_sum is None:
        snow_sum = []

    wind_max = daily.get("windspeed_10m_max")
    if wind_max is None:
        wind_max = []

    cards = []
    for i in range(trip_length):
        d = add_days(start_date, i)
        tmax = None
        if i < len(max_temps):
            tmax = max_temps[i]

        tmin = None
        if i < len(min_temps):
            tmin = min_temps[i]

        p = None
        if i < len(precip_prob):
            p = precip_prob[i]

        r = None
        if i < len(rain_sum):
            r = rain_sum[i]

        s = None
        if i < len(snow_sum):
            s = snow_sum[i]

        w = None
        if i < len(wind_max):
            w = wind_max[i]

        cards.append(
            {
                "date": format_fn(d),
                "tmax": round(tmax) if tmax is not None else None,
                "tmin": round(tmin) if tmin is not None else None,
                "desc": day_description(tmax, tmin, p, r, s, w),
            }
        )

    return cards

def geocode_location(name):
    url = f"{GEOCODING_URL}?name={name}&count=1&language=en&format=json"
    try:
        with urlopen(url, timeout=10, context=SSL_CONTEXT) as response:
            data = json.loads(response.read())
    except (URLError, HTTPError):
        return None

    results = data.get("results")
    if results is None:
        results = []

    if not results:
        return None
    first = results[0]
    if "latitude" not in first or "longitude" not in first:
        return None

    return {
        "name": first.get("name", ""),
        "country": first.get("country", ""),
        "latitude": first["latitude"],
        "longitude": first["longitude"],
        "timezone": first.get("timezone"),
    }


def fetch_forecast(lat, lon, start_date, days, timezone):
    start = date.fromisoformat(start_date)
    end = add_days(start, days - 1)
    start_iso = start.isoformat()
    end_iso = end.isoformat()

    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": DAILY_VARS,
        "timezone": timezone or "auto",
        "start_date": start_iso,
        "end_date": end_iso,
    }

    responses = openmeteo.weather_api(FORECAST_URL, params=params)
    if not responses:
        raise RuntimeError("No forecast response returned")
    response = responses[0]

    daily = response.Daily()
    if daily is None:
        return {"daily": {}}

    daily_out = {}
    for i, var_name in enumerate(DAILY_VARS):
        values = daily.Variables(i).ValuesAsNumpy()
        daily_out[var_name] = values.tolist()

    return {"daily": daily_out}


def summarize_weather(forecast):
    daily = forecast.get("daily")
    if daily is None:
        daily = {}

    max_temps = daily.get("temperature_2m_max")
    if max_temps is None:
        max_temps = []

    min_temps = daily.get("temperature_2m_min")
    if min_temps is None:
        min_temps = []

    precip_prob = daily.get("precipitation_probability_max")
    if precip_prob is None:
        precip_prob = []

    rain_sum = daily.get("rain_sum")
    if rain_sum is None:
        rain_sum = []

    snow_sum = daily.get("snowfall_sum")
    if snow_sum is None:
        snow_sum = []

    wind_max = daily.get("windspeed_10m_max")
    if wind_max is None:
        wind_max = []

    if not max_temps or not min_temps:
        return {
            "minTemp": None,
            "maxTemp": None,
            "rainyDays": 0,
            "hasSnow": False,
            "hasWindyDays": False,
            "descriptors": ["typical seasonal conditions"],
        }

    min_temp = min(min_temps)
    max_temp = max(max_temps)

    rainy_days = 0
    for i in range(len(precip_prob)):
        rain_val = (rain_sum[i] if i < len(rain_sum) else 0) or 0
        if (precip_prob[i] or 0) >= 50 or rain_val > 1:
            rainy_days += 1

    has_snow = any((s or 0) > 0 for s in snow_sum)
    has_windy = any((w or 0) >= 30 for w in wind_max)

    descriptors = []

    if max_temp >= 80:
        descriptors.append("hot daytime temperatures")
    elif max_temp >= 68:
        descriptors.append("warm daytime temperatures")
    elif max_temp >= 55:
        descriptors.append("mild daytime temperatures")
    else:
        descriptors.append("cool daytime temperatures")

    if min_temp <= 32:
        descriptors.append("freezing nights")
    elif min_temp <= 45:
        descriptors.append("chilly nights")
    elif min_temp <= 60:
        descriptors.append("cool evenings")
    else:
        descriptors.append("mild evenings")

    if rainy_days >= 3:
        descriptors.append("several rainy days")
    elif rainy_days >= 1:
        descriptors.append("a chance of rain")

    if has_snow:
        descriptors.append("possible snow")
    if has_windy:
        descriptors.append("some windy conditions")

    return {
        "minTemp": min_temp,
        "maxTemp": max_temp,
        "rainyDays": rainy_days,
        "hasSnow": has_snow,
        "hasWindyDays": has_windy,
        "descriptors": descriptors,
    }


def base_item_count(days, packing_style):
    if packing_style == "light":
        mult = 0.6
    elif packing_style == "prepared":
        mult = 1.2
    else:
        mult = 1.0
    return max(1, round(days * mult))


def generate_packing_list(summary, trip_type, packing_style, days):
    essentials = []
    weather_specific = []
    trip_specific = []
    extras = []

    tops = base_item_count(days, packing_style)
    bottoms = max(1, round(days / 2))
    socks = max(days + (2 if packing_style == "prepared" else 0), 2)

    essentials.extend([
        {"label": "Underwear", "quantity": f"{days} pairs"},
        {"label": "Socks", "quantity": f"{socks} pairs"},
        {"label": "Everyday tops", "quantity": str(tops)},
        {"label": "Everyday bottoms", "quantity": str(bottoms)},
        {"label": "Sleepwear", "quantity": "1–2 sets"},
        {"label": "Comfortable walking shoes", "quantity": "1 pair"},
        {"label": "Toiletries (toothbrush, toothpaste, etc.)", "quantity": None},
        {"label": "Phone + charger", "quantity": None},
        {"label": "Travel documents & ID", "quantity": None},
    ])

    desc = ", ".join(summary["descriptors"])
    rainy_days = summary["rainyDays"]
    has_snow = summary["hasSnow"]
    has_windy = summary["hasWindyDays"]

    if "hot daytime" in desc:
        weather_specific.extend([
            {"label": "Lightweight, breathable tops", "quantity": "2–4"},
            {"label": "Shorts or airy bottoms", "quantity": "1–3"},
            {"label": "Sunscreen", "quantity": None},
            {"label": "Reusable water bottle", "quantity": None},
        ])
    elif "warm daytime" in desc:
        weather_specific.extend([
            {"label": "Light layers for warm days", "quantity": "2–3"},
            {"label": "Light sweater or cardigan", "quantity": "1"},
        ])
    elif "cool daytime" in desc or "mild daytime" in desc:
        weather_specific.extend([
            {"label": "Long-sleeve layers", "quantity": "2–3"},
            {"label": "Warm sweater or fleece", "quantity": "1–2"},
        ])

    if "freezing nights" in desc or "chilly nights" in desc:
        weather_specific.extend([
            {"label": "Warm jacket or coat", "quantity": "1"},
            {"label": "Thermal base layer", "quantity": "1–2 sets"},
            {"label": "Warm hat", "quantity": "1"},
            {"label": "Gloves", "quantity": "1 pair"},
            {"label": "Scarf", "quantity": "1"},
        ])
    elif "cool evenings" in desc:
        weather_specific.append(
            {"label": "Light jacket or hoodie", "quantity": "1"}
        )

    if rainy_days >= 3:
        weather_specific.extend([
            {"label": "Waterproof rain jacket", "quantity": "1"},
            {"label": "Waterproof shoes or boots", "quantity": "1 pair"},
            {"label": "Compact umbrella", "quantity": "1"},
            {"label": "Extra socks for rainy days", "quantity": "2–3 pairs"},
        ])
    elif rainy_days >= 1:
        weather_specific.extend([
            {"label": "Packable rain layer", "quantity": "1"},
            {"label": "Small umbrella", "quantity": "1"},
        ])

    if has_snow:
        weather_specific.extend([
            {"label": "Insulated boots", "quantity": "1 pair"},
            {"label": "Thick socks", "quantity": "2–3 pairs"},
            {"label": "Water-resistant pants", "quantity": "1"},
        ])
    if has_windy:
        weather_specific.append(
            {"label": "Windproof layer (shell or windbreaker)", "quantity": "1"}
        )

    if trip_type == "outdoor":
        trip_specific.extend([
            {"label": "Hiking shoes or trail runners", "quantity": "1 pair"},
            {"label": "Daypack / small backpack", "quantity": "1"},
            {"label": "Sunscreen and hat", "quantity": None},
            {"label": "Bug repellent", "quantity": None},
            {"label": "Basic first-aid kit", "quantity": None},
            {"label": "Reusable water bottle or hydration pack", "quantity": None},
        ])
    elif trip_type == "business":
        trip_specific.extend([
            {"label": "Business-appropriate outfits", "quantity": "1–3"},
            {"label": "Dress shoes", "quantity": "1 pair"},
            {"label": "Laptop + charger", "quantity": None},
            {"label": "Notebook and pen", "quantity": None},
        ])
    elif trip_type == "family":
        trip_specific.extend([
            {"label": "Comfortable loungewear", "quantity": "1–2 sets"},
            {"label": "Small gift or treats", "quantity": None},
            {"label": "Games or activities", "quantity": None},
        ])
    else:
        trip_specific.extend([
            {"label": "Comfortable day bag", "quantity": "1"},
            {"label": "City-friendly outfit for photos", "quantity": "1–2"},
        ])

    if packing_style == "prepared":
        extras.extend([
            {"label": "Medication + basic pain relief", "quantity": None},
            {"label": "Spare phone charger or power bank", "quantity": None},
            {"label": "Travel-size laundry detergent", "quantity": None},
            {"label": "Small sewing/repair kit", "quantity": None},
        ])
    elif packing_style == "light":
        extras.extend([
            {"label": "Travel laundry bag", "quantity": None},
            {"label": "Compact microfiber towel", "quantity": None},
        ])
    else:
        extras.extend([
            {"label": "Reusable shopping bag", "quantity": None},
            {"label": "Snacks for travel days", "quantity": None},
        ])

    return {
        "essentials": essentials,
        "weatherSpecific": weather_specific,
        "tripSpecific": trip_specific,
        "extras": extras,
    }

if __name__ == "__main__":
    app.run()
