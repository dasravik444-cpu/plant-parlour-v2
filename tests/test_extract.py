from leadgen.enrich.emails import decode_cfemail, find_emails_in_text, normalize_email
from leadgen.enrich.extract import canonical_social, extract_page
from leadgen.enrich.phones import find_phones_in_text, parse_phone, whatsapp_number_from_link


def cf_encode(email: str, key: int = 0x42) -> str:
    return f"{key:02x}" + "".join(f"{ord(c) ^ key:02x}" for c in email)


PAGE = """<html><head><title>Green Leaf Cafe | Salt Lake Kolkata</title>
<meta name="description" content="Cosy cafe in Salt Lake">
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Restaurant","name":"Green Leaf Cafe",
"telephone":"+91 98300 12345","email":"hello@greenleafcafe.in",
"sameAs":["https://www.instagram.com/greenleafcafe.kol/","https://www.facebook.com/greenleafcafekol"]}</script>
</head><body>
<a href="mailto:Bookings@GreenLeafCafe.in?subject=hi">Email</a>
<a href="tel:+913340001234">Call landline</a>
<a href="https://wa.me/919830012345?text=hi">WhatsApp</a>
<a href="https://instagram.com/p/xyz">a post</a>
<a href="https://www.linkedin.com/company/green-leaf-cafe/">LinkedIn</a>
<a href="/contact-us">Contact</a> <a href="/about">About us</a> <a href="/menu.pdf">Menu</a>
<span class="__cf_email__" data-cfemail="CFEMAIL">[email protected]</span>
<p>Call us: 033 4000 5678 for reservations. FSSAI No 12345678901234. Pin code 700091.</p>
<p>WhatsApp: +91 98765 43210</p>
<footer><p>Designed by WebCo, call 98111 22233 or mail info@webco.com</p></footer>
</body></html>""".replace("CFEMAIL", cf_encode("events@greenleafcafe.in"))


def test_extract_page_finds_only_literal_contacts():
    pe = extract_page(PAGE, "https://www.greenleafcafe.in/")
    got = {(f.kind, f.value) for f in pe.found}
    assert ("phone", "+919830012345") in got            # JSON-LD
    assert ("email", "hello@greenleafcafe.in") in got   # JSON-LD
    assert ("email", "bookings@greenleafcafe.in") in got  # mailto, lowercased, query stripped
    assert ("phone", "+913340001234") in got            # tel: landline
    assert ("whatsapp", "+919830012345") in got         # wa.me link
    assert ("email", "events@greenleafcafe.in") in got  # Cloudflare-protected
    assert ("phone", "+913340005678") in got            # text with "Call us" context
    assert ("whatsapp", "+919876543210") in got         # "WhatsApp: ..." text
    assert ("instagram", "https://www.instagram.com/greenleafcafe.kol/") in got
    assert ("linkedin", "https://www.linkedin.com/company/green-leaf-cafe/") in got
    # agency credits, licence numbers, pincodes and post links are not contacts
    assert not any("webco" in v or v.endswith("22233") for _, v in got)
    assert not any(v.endswith("700091") or "12345678901234" in v for _, v in got)
    assert not any("/p/" in v for _, v in got)
    assert pe.title.startswith("Green Leaf Cafe")
    assert [u for u, _ in pe.internal_links] == ["https://www.greenleafcafe.in/contact-us", "https://www.greenleafcafe.in/about"]


def test_homepage_text_phone_requires_context():
    html = "<html><body><p>Open since 1998. 9830012345</p><p>Phone: 98300 99999</p></body></html>"
    home = {f.value for f in extract_page(html, "https://x.in/").found}
    assert "+919830099999" in home and "+919830012345" not in home
    contact = {f.value for f in extract_page(html, "https://x.in/contact", contact_page=True).found}
    assert "+919830012345" in contact


def test_email_normalisation_and_junk():
    assert normalize_email("mailto:Info@Cafe.IN") == "info@cafe.in"
    assert normalize_email("logo@2x.png") is None
    assert normalize_email("user@example.com") is None
    assert normalize_email("abc@sentry.wixpress.com") is None
    assert normalize_email("orders@zomato.com") is None
    assert find_emails_in_text("write to bookings [at] myvenue [dot] in today") == ["bookings@myvenue.in"]
    assert decode_cfemail(cf_encode("a@b.in")) == "a@b.in"


def test_phone_parsing_and_classification():
    assert parse_phone("+91 98300 12345") == ("+919830012345", "mobile")
    assert parse_phone("033 2287 1234")[1] == "landline"
    assert parse_phone("12345") is None
    assert whatsapp_number_from_link("https://api.whatsapp.com/send?phone=919830012345&text=hi") == "+919830012345"
    assert whatsapp_number_from_link("https://wa.me/9830012345") == "+919830012345"
    assert find_phones_in_text("Pin code 700091 call 98300 12345", require_context=True)[0][0] == "+919830012345"


def test_canonical_social():
    assert canonical_social("https://instagram.com/the.cafe_kol?igsh=x") == ("instagram", "https://www.instagram.com/the.cafe_kol/")
    assert canonical_social("https://www.instagram.com/popular/flurys-park-street/") is None
    assert canonical_social("https://www.instagram.com/reel/abc/") is None
    assert canonical_social("https://www.facebook.com/sharer/sharer.php?u=x") is None
    assert canonical_social("https://m.facebook.com/profile.php?id=1000123") == ("facebook", "https://www.facebook.com/profile.php?id=1000123")
    assert canonical_social("https://in.linkedin.com/company/abc-ltd") == ("linkedin", "https://www.linkedin.com/company/abc-ltd/")
    assert canonical_social("https://x.com/intent/tweet") is None
