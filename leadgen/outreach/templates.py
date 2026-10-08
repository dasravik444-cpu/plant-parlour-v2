"""Message templates. Plain text, no links, no images, no tracking: what reaches inboxes, not spam folders.

Every template can be replaced in the config ([outreach.email] / [outreach.whatsapp] / [outreach.hooks]).
Placeholders: {greeting} {business} {first_name} {audience} {place} {hook} {sender_name} {sender_first}
{sender_business} {sender_phone} {sender_city}
"""
from __future__ import annotations

import hashlib
import re
import string

SUBJECTS = [
    "plants for {business}",
    "{business} - plants and greenery",
    "quick question for {business}",
]

FIRST_EMAIL = """{greeting}

I came across {business} while looking at {audience} in {place}. {hook}

I'm {sender_first} from {sender_business}, a wholesale plant supplier in {sender_city}. We supply indoor, outdoor and flowering plants, planters and green walls at wholesale prices, and can also take care of styling and regular plant maintenance.

Would it help if I sent you our price list, with a few ideas for {business}? Just reply "yes" and I'll share it here or on WhatsApp.

Thanks,
{sender_name}
{sender_business}, {sender_city}
{sender_phone}

If you'd rather not hear from me, reply "no" and I won't write again."""

FOLLOW_UPS = [
    """{greeting}

Just bringing this back to the top of your inbox. If plants for {business} are on your list, I'd be happy to send our wholesale price list - a one-word "yes" is enough.

{sender_name}
{sender_business}
{sender_phone}

(Reply "no" and I won't follow up again.)""",
    """{greeting}

Last note from me. Whenever you need plants, planters or plant care for {business}, we're a call or a WhatsApp away on {sender_phone}.

Wishing you a great season ahead,
{sender_name}
{sender_business}, {sender_city}""",
]

WHATSAPP_COLD = ("Hi {business} team, I'm {sender_first} from {sender_business} 🌿, a wholesale plant supplier in {sender_city}. "
                 "{hook} Could I send you our price list here? If you'd rather not get messages from us, just reply STOP.")

WHATSAPP_OPTED_IN = ("Hi {first_name_or_team}, this is {sender_first} from {sender_business} 🌿 - thanks for replying to my email! "
                     "Sharing our wholesale price list as promised. Happy to suggest plants that would suit {business} too.")

# One line per category: why plants matter to that kind of business. Keep it true to what you offer.
HOOKS = {
    "cafe": "A few well-placed plants make a cafe feel fresher and more inviting, and they look great in customers' photos.",
    "restaurant": "Greenery at the entrance and around the seating is one of the simplest ways to lift a restaurant's ambience.",
    "banquet_venue": "With the wedding and festive season here, venues are planning plants and planters for entrances and stages.",
    "event_planner": "With the wedding and festive season here, we can supply plants and green décor for your events in bulk.",
    "interior_designer": "We work as a greenery partner for designers and architects - supply, styling and installation for client projects.",
    "landscaper": "We supply landscapers with healthy plants in bulk at wholesale rates.",
    "hotel": "Lobby and room greenery is a quick way to lift the guest experience, and we can look after the upkeep too.",
    "coworking": "Greenery makes a workspace feel calmer and more premium for members, and we can handle the regular upkeep.",
    "nursery_florist": "We supply nurseries and florists with healthy stock at wholesale rates, delivered across {sender_city}.",
    "salon_spa": "Plants make a salon or spa feel calmer and more premium, with very little effort.",
    "gym": "Plants make a gym feel fresher and more welcoming, and we can suggest ones that need very little care.",
    "clinic": "Calm, green waiting areas help patients feel at ease, and low-maintenance plants keep it simple.",
    "office": "Plants in reception and workspaces make an office feel fresher, and we can take care of the regular upkeep.",
    "real_estate": "Model flats and lobbies look far more inviting with plants, and we can supply and maintain them.",
    "education": "Green campuses and classrooms feel calmer, and we can supply and maintain plants across the campus.",
    "default": "Plants are one of the simplest ways to make a space feel fresher and more welcoming.",
}

AUDIENCE = {
    "cafe": "cafes", "restaurant": "restaurants", "banquet_venue": "banquet and event venues",
    "event_planner": "event planners and decorators", "interior_designer": "interior designers and architects",
    "landscaper": "landscapers", "hotel": "hotels", "coworking": "coworking spaces", "nursery_florist": "nurseries and florists",
    "salon_spa": "salons and spas", "gym": "gyms", "clinic": "clinics", "office": "offices",
    "real_estate": "builders and property developers", "education": "schools and colleges", "default": "local businesses",
}

PLACEHOLDERS = {"greeting", "business", "first_name", "first_name_or_team", "audience", "place", "hook", "sender_name",
                "sender_first", "sender_business", "sender_phone", "sender_city"}


def unknown_placeholders(template: str) -> set[str]:
    names = {f for _, f, _, _ in string.Formatter().parse(template) if f}
    return names - PLACEHOLDERS


class _Keep(dict):
    def __missing__(self, key):          # leave unknown {placeholders} visible instead of crashing
        return "{" + key + "}"


def render(template: str, ctx: dict) -> str:
    text = template.format_map(_Keep(ctx))
    return re.sub(r"[ \t]+\n", "\n", text).strip()


def pick(options: list[str], seed: str) -> str:
    """Deterministic choice per lead, so a lead always gets the same variant (and copies vary across leads)."""
    if not options:
        return ""
    h = int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)
    return options[h % len(options)]


def context(lead, oc: dict) -> dict:
    """Template values for one lead. oc = cfg["outreach"]."""
    s = oc["sender"]
    hooks = {**HOOKS, **(oc.get("hooks") or {})}
    audience = {**AUDIENCE, **(oc.get("audience") or {})}
    sender_name = s.get("name", "").strip()
    first = lead.first_name
    ctx = {
        "business": lead.business,
        "first_name": first,
        "first_name_or_team": first or "there",
        "greeting": f"Hi {first}," if first else f"Hello {lead.business} team,",
        "audience": audience.get(lead.category_key) or audience["default"],
        "place": lead.area or s.get("city", ""),
        "sender_name": sender_name,
        "sender_first": sender_name.split(" ")[0] if sender_name else "",
        "sender_business": s.get("business", "").strip(),
        "sender_phone": s.get("phone", "").strip(),
        "sender_city": s.get("city", "").strip(),
    }
    ctx["hook"] = render(hooks.get(lead.category_key) or hooks["default"], ctx)
    return ctx
