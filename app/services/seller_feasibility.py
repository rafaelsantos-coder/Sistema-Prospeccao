"""Consulta de viabilidade de fibra a partir das coordenadas originais da base."""
import json
import math
import re
import unicodedata

EARTH_METERS = 6371008.8
RADIUS_METERS = 800
ASK_PIN = "Para verificar a disponibilidade da fibra, pode me enviar a localização do imóvel pelo WhatsApp ou a rua, número e cidade?"
REVIEW = "A disponibilidade da fibra neste local precisa de análise técnica. Sua oportunidade continua aberta."


def coordinates(text):
    match = re.search(r"(?<![\d.])(-?\d{1,2}\.\d+)\s*[,;]\s*(-?\d{1,3}\.\d+)(?![\d.])", text or "")
    if match:
        lat, lng = map(float, match.groups())
        if -90 <= lat <= 90 and -180 <= lng <= 180:
            return lat, lng
    return None


def nearest_available(naps, point):
    lat, lng = map(math.radians, point)
    closest = None
    for nap in naps:
        if nap["status"] == "Offline" or nap["livres"] < 1:
            continue
        other_lat = math.radians(nap["original_lat"])
        other_lng = math.radians(nap["original_lng"])
        a = (math.sin((lat - other_lat) / 2) ** 2
             + math.cos(lat) * math.cos(other_lat) * math.sin((lng - other_lng) / 2) ** 2)
        meters = 2 * EARTH_METERS * math.asin(math.sqrt(min(1, a)))
        if meters <= RADIUS_METERS and (closest is None or (meters, nap["id"]) < closest[:2]):
            closest = meters, nap["id"], nap["nome"]
    if closest:
        return {"meters": round(closest[0], 1), "id": closest[1], "name": closest[2]}
    return None


def words(value):
    plain = unicodedata.normalize("NFKD", str(value or "").casefold())
    return " ".join(re.findall(r"[a-z0-9]+", "".join(ch for ch in plain if not unicodedata.combining(ch))))


def address_point(db, text, city):
    from sqlalchemy import select
    from app.models import CreditMapAddress as Address

    body = " " + words(text) + " "
    numbers = re.findall(r"\b\d+[a-z]?\b", body)
    if not numbers:
        return None
    rows = db.execute(select(Address.street, Address.number, Address.city, Address.lat, Address.lng)
                      .where(Address.number.in_(numbers), Address.lat.is_not(None), Address.lng.is_not(None)))
    points = set()
    for street, number, locality, lat, lng in rows:
        if words(locality) != words(city) and f" {words(locality)} " not in body:
            continue
        if f" {words(street)} {words(number)} " in body:
            points.add((lat, lng))
    return points.pop() if len(points) == 1 else None


def locate(db, text, city, message=None):
    if message is not None and message.message_type == "LOCATION":
        from app.services.conversation_engine import location_urls_for
        text = location_urls_for(db, [message]).get(message.id) or text
    return coordinates(text) or address_point(db, text, city)


def check_point(db, point):
    from app.models import CreditMapNetwork
    row = db.get(CreditMapNetwork, 1)
    snapshot = json.loads(row.snapshot_json) if row else {}
    nap = nearest_available(snapshot.get("naps", []), point)
    return {"point": list(point), "nap": nap,
            "reference": snapshot.get("sessions_at") or snapshot.get("registration_at"),
            "status": "available" if nap else ("review" if snapshot.get("naps") else "no_data")}
