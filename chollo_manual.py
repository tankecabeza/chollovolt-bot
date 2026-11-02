import asyncio
import sys
import html
import re
import os
import json
import random
import urllib.parse

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright

# Requiere la versión actual de auto_chollovolt.py (con estas funciones)
from auto_chollovolt import (
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, X_STATE,
    publicar_en_x, parse_precio, JS_EXTRAER, hay_captcha,
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

RE_PRECIO = r"(\d+(?:\.\d{3})*,\d{2})"


# ==============================================================================
# IMAGEN
# ==============================================================================
def cargar_fuente(tamano, negrita=False):
    rutas = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if negrita else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if negrita else r"C:\Windows\Fonts\arial.ttf",
    ]
    for r in rutas:
        if os.path.exists(r):
            try:
                return ImageFont.truetype(r, tamano)
            except Exception:
                pass
    return ImageFont.load_default()


def crear_imagen_definitiva(descuento, precio_actual_num, precio_antes_str, ruta_salida, es_prime=False):
    """descuento: texto tipo '-27%' o None si no se ha podido verificar."""
    W, H = 800, 440
    img = Image.new("RGB", (W, H), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    f_gigante = cargar_fuente(115, negrita=False)
    f_super = cargar_fuente(52, negrita=True)
    f_gris = cargar_fuente(36, negrita=False)
    f_prime = cargar_fuente(52, negrita=True)

    x_precio = 45
    if descuento:
        draw.text((45, 50), descuento, font=f_gigante, fill=(204, 12, 57))
        bbox_desc = draw.textbbox((45, 50), descuento, font=f_gigante)
        x_precio = bbox_desc[2] + 40

    if precio_actual_num and precio_actual_num > 0:
        partes = f"{precio_actual_num:.2f}".replace(".", ",").split(",")
        euros, centimos = partes[0], partes[1]
        draw.text((x_precio, 50), euros, font=f_gigante, fill=(15, 17, 17))
        bbox_eur = draw.textbbox((x_precio, 50), euros, font=f_gigante)
        draw.text((bbox_eur[2] + 6, 58), f"{centimos} €", font=f_super, fill=(15, 17, 17))
    else:
        draw.text((x_precio, 50), "¡OFERTA!", font=f_gigante, fill=(15, 17, 17))

    if precio_antes_str:
        texto_antes = f"Precio recomendado: {precio_antes_str}€"
        draw.text((45, 230), texto_antes, font=f_gris, fill=(86, 89, 89))
        bbox_sp = draw.textbbox((45, 230), "Precio recomendado: ", font=f_gris)
        bbox_total = draw.textbbox((45, 230), texto_antes, font=f_gris)
        y_linea = (bbox_total[1] + bbox_total[3]) // 2 + 2
        draw.line([(bbox_sp[2], y_linea), (bbox_total[2], y_linea)], fill=(86, 89, 89), width=3)
    else:
        draw.text((45, 230), "Oferta destacada en Amazon", font=f_gris, fill=(86, 89, 89))

    if es_prime:
        draw.line([(48, 348), (62, 366), (92, 326)], fill=(255, 153, 0), width=9)
        draw.text((98, 315), "prime", font=f_prime, fill=(0, 113, 197))

    draw.rectangle([(2, 2), (W - 3, H - 3)], outline=(225, 228, 228), width=3)
    img.save(ruta_salida, quality=95)


# ==============================================================================
# ENLACE Y EXTRACCIÓN DE DATOS
# ==============================================================================
def expandir_enlace(url_corta):
    try:
        r = requests.get(
            url_corta,
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36"},
            allow_redirects=True,
            timeout=15,
        )
        return urllib.parse.unquote(r.url.split("?")[0])
    except Exception as e:
        print(f"⚠️ No pude expandir el enlace: {e}")
        return ""


def _descargar_html(asin):
    url_directa = f"https://www.amazon.es/dp/{asin}"
    url_proxy = f"https://www-amazon-es.translate.goog/dp/{asin}?_x_tr_sl=auto&_x_tr_tl=es&_x_tr_hl=es&_x_tr_pto=wapp"
    ua_googlebot = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

    try:
        r = requests.get(url_directa, headers={"User-Agent": ua_googlebot}, timeout=15)
        if r.status_code == 200 and "captchacharacters" not in r.text.lower():
            print("🌐 HTML obtenido (directo).")
            return r.text
    except Exception as e:
        print(f"⚠️ Descarga directa falló: {e}")

    try:
        r = requests.get(url_proxy, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=15)
        if r.status_code == 200:
            print("🌐 HTML obtenido (vía proxy de traducción).")
            return r.text
    except Exception as e:
        print(f"⚠️ Descarga vía proxy falló: {e}")

    print("⚠️ No se pudo descargar el HTML de Amazon.")
    return ""


def extraer_datos_html(asin):
    html_text = _descargar_html(asin)
    datos = {
        "titulo": None,
        "precio_actual_num": 0.0,
        "precio_antes_num": 0.0,
        "badge_pct": None,
        "es_prime": False,
    }
    if not html_text:
        return datos

    soup = BeautifulSoup(html_text, "html.parser")
    texto_plano = soup.get_text(" ", strip=True).replace("\xa0", " ")

    # 1. Título
    el = soup.select_one("#productTitle")
    if el:
        datos["titulo"] = el.get_text(strip=True)
    else:
        m = re.search(r'"title":\s*"([^"]+)"', html_text)
        if m:
            datos["titulo"] = m.group(1)

    # 2. Variables ocultas de la página (no dependen del idioma)
    m = re.search(r'name="twister-plus-price-data-price"[^>]*value="([\d.]+)"', html_text)
    if m:
        datos["precio_actual_num"] = float(m.group(1))
    m = re.search(r'name="twister-plus-price-data-basisPrice"[^>]*value="([\d.]+)"', html_text)
    if m:
        datos["precio_antes_num"] = float(m.group(1))
        print("   ✔ Precio anterior: variable oculta.")

    # 3. Caja de compra: precio actual
    caja = soup.select_one("#corePriceDisplay_desktop_feature_div, #corePrice_feature_div, #desktop_buybox, #price")
    if datos["precio_actual_num"] == 0.0 and caja:
        el_act = caja.select_one(".priceToPay, .apexPriceToPay, .a-price")
        if el_act:
            v = parse_precio(el_act.get_text(" ", strip=True))
            if v:
                datos["precio_actual_num"] = v

    # 4. Precio actual fragmentado
    if datos["precio_actual_num"] == 0.0:
        ent, dec = soup.select_one(".a-price-whole"), soup.select_one(".a-price-fraction")
        if ent and dec:
            e, d = re.sub(r"\D", "", ent.get_text()), re.sub(r"\D", "", dec.get_text())
            if e and d:
                datos["precio_actual_num"] = float(f"{e}.{d}")

    # 5. Precio anterior (PVP) por selectores
    if datos["precio_antes_num"] == 0.0:
        for sel in (".basisPrice .a-offscreen",
                    ".a-price.a-text-price .a-offscreen",
                    "[data-a-strike='true'] .a-offscreen",
                    "#listPrice", ".a-text-strike"):
            for el in soup.select(sel):
                v = parse_precio(el.get_text(" ", strip=True))
                if v and v > datos["precio_actual_num"] > 0:
                    datos["precio_antes_num"] = v
                    print(f"   ✔ Precio anterior: selector {sel}.")
                    break
            if datos["precio_antes_num"]:
                break

    # 6. Precio anterior por el texto "Precio recomendado: 89,95€"
    if datos["precio_antes_num"] == 0.0:
        m = re.search(
            r"(?:Precio recomendado|Precio de venta recomendado|Precio anterior|Precio de lista|PVP)\s*:?\s*" + RE_PRECIO,
            texto_plano, re.I)
        if m:
            v = parse_precio(m.group(1))
            if v and v > datos["precio_actual_num"] > 0:
                datos["precio_antes_num"] = v
                print("   ✔ Precio anterior: texto 'Precio recomendado'.")

    # 7. Etiqueta de descuento de Amazon (-27 %)
    el_badge = soup.select_one(".savingsPercentage")
    if el_badge:
        mb = re.search(r"(\d{1,2})\s*%", el_badge.get_text())
        if mb:
            datos["badge_pct"] = int(mb.group(1))

    # 8. Prime
    datos["es_prime"] = bool(re.search(r'class="[^"]*a-icon-prime[^"]*"', html_text)) or bool(soup.select_one("i.a-icon-prime"))

    return datos


async def extraer_con_navegador(asin):
    """Plan B: abre la ficha con un navegador real (renderiza el JavaScript)."""
    print("🧭 Completando datos con un navegador real...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            ctx = await browser.new_context(user_agent=USER_AGENTS[0], locale="es-ES",
                                            viewport={"width": 1280, "height": 800})
            page = await ctx.new_page()
            await page.goto(f"https://www.amazon.es/dp/{asin}", wait_until="domcontentloaded", timeout=30000)
            if await hay_captcha(page):
                print("   🤖 Amazon pide captcha al navegador.")
                return None
            await page.wait_for_selector("#productTitle", timeout=8000)
            return await page.evaluate(JS_EXTRAER)
        except Exception as e:
            print(f"   ⚠️ Navegador: {e}")
            return None
        finally:
            await browser.close()


def fusionar_navegador(datos, d):
    if not d:
        return
    if not datos["titulo"] and d.get("titulo"):
        datos["titulo"] = d["titulo"]
    act, ant = parse_precio(d.get("actual")), parse_precio(d.get("antes"))
    if datos["precio_actual_num"] <= 0 and act:
        datos["precio_actual_num"] = act
    if datos["precio_antes_num"] <= 0 and ant and ant > datos["precio_actual_num"] > 0:
        datos["precio_antes_num"] = ant
        print("   ✔ Precio anterior: navegador real.")
    mb = re.search(r"(\d{1,2})\s*%", d.get("badge") or "")
    if mb and not datos["badge_pct"]:
        datos["badge_pct"] = int(mb.group(1))
    if d.get("prime"):
        datos["es_prime"] = True


def calcular_descuento(datos):
    """Devuelve el descuento REAL como número entero, o None si no se puede verificar."""
    act, ant, badge = datos["precio_actual_num"], datos["precio_antes_num"], datos["badge_pct"]
    if ant > act > 0:
        pct = round((ant - act) / ant * 100)
        if badge and abs(badge - pct) > 3:
            print(f"⚠️ El descuento calculado ({pct}%) no coincide con la etiqueta de Amazon ({badge}%).")
        return pct if 1 <= pct <= 95 else None
    if badge and 1 <= badge <= 95:
        print("   ℹ️ Uso la etiqueta de descuento de Amazon (no se encontró el precio anterior).")
        return badge
    return None


# ==============================================================================
# TELEGRAM
# ==============================================================================
def publicar_telegram(imagen_ruta, texto):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("::warning::Faltan los secrets de Telegram.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        with open(imagen_ruta, "rb") as foto:
            r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": texto, "parse_mode": "HTML"},
                              files={"photo": foto}, timeout=30)
        if r.status_code == 200:
            return True
        print(f"::warning::Telegram falló ({r.status_code}): {r.text[:300]}")
    except Exception as e:
        print(f"::warning::Error con Telegram: {e}")
    return False


# ==============================================================================
# PRINCIPAL
# ==============================================================================
async def main(url_acortada):
    url_real = expandir_enlace(url_acortada)
    m = re.search(r"(?:/dp/|/gp/product/|%2Fdp%2F|%2Fproduct%2F)([A-Z0-9]{10})", url_real)
    if not m:
        print(f"❌ ASIN no detectado en: {url_real!r}")
        sys.exit(1)
    asin = m.group(1)
    print(f"🔗 ASIN: {asin}")

    datos = extraer_datos_html(asin)

    # Si falta el precio actual o no hay forma de calcular el descuento, plan B
    falta = datos["precio_actual_num"] <= 0 or (datos["precio_antes_num"] <= 0 and not datos["badge_pct"])
    if falta:
        fusionar_navegador(datos, await extraer_con_navegador(asin))

    pct = calcular_descuento(datos)
    descuento_txt = f"-{pct}%" if pct else None
    print(f"📊 Actual={datos['precio_actual_num']} | Antes={datos['precio_antes_num']} | "
          f"Etiqueta={datos['badge_pct']} | Descuento={descuento_txt}")

    titulo = datos["titulo"] or "Oferta destacada en Amazon"
    actual, antes = datos["precio_actual_num"], datos["precio_antes_num"]
    hay_antes = antes > actual > 0
    precio_txt = f"{actual:.2f}".replace(".", ",") if actual > 0 else None
    antes_txt = f"{antes:.2f}".replace(".", ",") if hay_antes else None

    # ---- Texto de X ----
    cab_x = f"🔥 {descuento_txt} de descuento 🔥" if descuento_txt else "🔥 ¡OFERTA DESTACADA! 🔥"
    linea_x = f"💰 Ahora: {precio_txt}€" if precio_txt else "💰 Precio: consultar en Amazon"
    if antes_txt:
        linea_x += f" (Antes: {antes_txt}€)"
    titulo_x = titulo[:80].rstrip() + ("…" if len(titulo) > 80 else "")
    texto_x = (
        f"{cab_x}\n"
        f"📦 {titulo_x}\n\n"
        f"{linea_x}\n\n"
        f"🛒 {url_acortada}\n\n"
        f"#chollo #ofertas #amazon #descuentos\n(Enlace de afiliado)"
    )

    # ---- Texto de Telegram ----
    cab_tg = f"🔥 <b>¡{html.escape(descuento_txt)} DE DESCUENTO!</b> 🔥" if descuento_txt else "🔥 <b>¡OFERTA DESTACADA!</b> 🔥"
    linea_tg = f"💰 <b>Precio actual:</b> {precio_txt}€" if precio_txt else "💰 <b>Precio:</b> consultar en Amazon"
    if antes_txt:
        linea_tg += f" (PVP: <s>{antes_txt}€</s>)"
    titulo_tg = html.escape(titulo[:150]) + ("…" if len(titulo) > 150 else "")
    texto_tg = (
        f"{cab_tg}\n\n"
        f"📦 <b>Producto:</b> {titulo_tg}\n\n"
        f"{linea_tg}\n\n"
        f"🛒 <b>Ver oferta aquí:</b> {html.escape(url_acortada)}\n\n"
        f"<i>#chollo #ofertas #amazon #descuentos</i>\n<i>(Enlace de afiliado)</i>"
    )

    crear_imagen_definitiva(descuento_txt, actual, antes_txt, "chollo_manual.jpg", es_prime=datos["es_prime"])

    resultados = {}
    resultados["Telegram"] = publicar_telegram("chollo_manual.jpg", texto_tg)
    print("✅ Telegram: publicado." if resultados["Telegram"] else "❌ Telegram: no se publicó.")

    if X_STATE:
        try:
            estado = json.loads(X_STATE)
        except json.JSONDecodeError:
            print("::error::X_STATE no es un JSON válido.")
            estado = None
        if estado:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
                try:
                    ctx = await browser.new_context(storage_state=estado, user_agent=USER_AGENTS[0],
                                                    viewport={"width": 1280, "height": 800}, locale="es-ES")
                    resultados["X"] = await publicar_en_x(ctx, texto_x, "chollo_manual.jpg")
                finally:
                    await browser.close()
            print("✅ X: publicado." if resultados["X"] else "❌ X: no se publicó.")

    if not any(resultados.values()):
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python chollo_manual.py <URL>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
