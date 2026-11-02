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
    publicar_en_x, parse_precio, JS_EXTRAER, hay_captcha, generar_enlace_afiliado,
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
]

RE_PRECIO = r"(\d+(?:\.\d{3})*,\d{2})"

# th=1&psc=1 le dice a Amazon que respete la variante (talla/color) del ASIN
PARAMS_VARIANTE = "th=1&psc=1"


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
    """descuento: texto tipo '-27%' o None si no hay descuento verificable."""
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
        draw.text((45, 230), "Producto destacado en Amazon", font=f_gris, fill=(86, 89, 89))

    if es_prime:
        draw.line([(48, 348), (62, 366), (92, 326)], fill=(255, 153, 0), width=9)
        draw.text((98, 315), "prime", font=f_prime, fill=(0, 113, 197))

    draw.rectangle([(2, 2), (W - 3, H - 3)], outline=(225, 228, 228), width=3)
    img.save(ruta_salida, quality=95)


# ==============================================================================
# ENLACE
# ==============================================================================
def expandir_enlace(url_corta):
    try:
        r = requests.get(
            url_corta,
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36"},
            allow_redirects=True,
            timeout=15,
        )
        return urllib.parse.unquote(r.url)
    except Exception as e:
        print(f"⚠️ No pude expandir el enlace: {e}")
        return ""


def num_usuario(txt):
    """Convierte lo que escribe el usuario ('107,49', '107.49€', '1.299,99') en número."""
    if not txt:
        return None
    t = re.sub(r"[^\d,.]", "", txt)
    if not t:
        return None
    if "," in t and "." in t:
        t = t.replace(".", "").replace(",", ".")
    elif "," in t:
        t = t.replace(",", ".")
    try:
        v = float(t)
    except ValueError:
        return None
    return v if v > 0 else None


# ==============================================================================
# EXTRACCIÓN 1: NAVEGADOR REAL (la más fiable, ve el precio de TU variante)
# ==============================================================================
async def extraer_con_navegador(asin):
    print("🧭 Leyendo la ficha con un navegador real...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        try:
            ctx = await browser.new_context(user_agent=USER_AGENTS[0], locale="es-ES",
                                            viewport={"width": 1280, "height": 800})
            page = await ctx.new_page()
            await page.goto(f"https://www.amazon.es/dp/{asin}?{PARAMS_VARIANTE}",
                            wait_until="domcontentloaded", timeout=30000)
            if await hay_captcha(page):
                print("   🤖 Amazon pide captcha al navegador.")
                return None
            await page.wait_for_selector("#productTitle", timeout=8000)
            await page.wait_for_timeout(1200)
            return await page.evaluate(JS_EXTRAER)
        except Exception as e:
            print(f"   ⚠️ Navegador: {e}")
            return None
        finally:
            await browser.close()


def datos_desde_navegador(d):
    badge = None
    mb = re.search(r"(\d{1,2})\s*%", d.get("badge") or "")
    if mb:
        badge = int(mb.group(1))
    actual = parse_precio(d.get("actual")) or 0.0
    antes = parse_precio(d.get("antes")) or 0.0
    if antes <= actual:
        antes = 0.0
    return {
        "titulo": d.get("titulo"),
        "precio_actual_num": actual,
        "precio_antes_num": antes,
        "badge_pct": badge,
        "es_prime": bool(d.get("prime")),
        "fuente": "navegador",
    }


# ==============================================================================
# EXTRACCIÓN 2: HTML (plan B si el navegador falla)
# ==============================================================================
def _descargar_html(asin):
    url_directa = f"https://www.amazon.es/dp/{asin}?{PARAMS_VARIANTE}"
    url_proxy = (f"https://www-amazon-es.translate.goog/dp/{asin}?{PARAMS_VARIANTE}"
                 f"&_x_tr_sl=auto&_x_tr_tl=es&_x_tr_hl=es&_x_tr_pto=wapp")
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
    datos = {"titulo": None, "precio_actual_num": 0.0, "precio_antes_num": 0.0,
             "badge_pct": None, "es_prime": False, "fuente": "html"}
    if not html_text:
        return datos

    soup = BeautifulSoup(html_text, "html.parser")
    texto_plano = soup.get_text(" ", strip=True).replace("\xa0", " ")

    el = soup.select_one("#productTitle")
    if el:
        datos["titulo"] = el.get_text(strip=True)
    else:
        m = re.search(r'"title":\s*"([^"]+)"', html_text)
        if m:
            datos["titulo"] = m.group(1)

    # Solo miramos DENTRO de la caja de precio principal (no en las variantes)
    caja = soup.select_one("#corePriceDisplay_desktop_feature_div, #corePrice_feature_div, #desktop_buybox")

    # Precio actual: primero el de la caja principal
    if caja:
        el_act = caja.select_one(".priceToPay .a-offscreen, .apexPriceToPay .a-offscreen, .priceToPay, .apexPriceToPay")
        if el_act:
            v = parse_precio(el_act.get_text(" ", strip=True))
            if v:
                datos["precio_actual_num"] = v
    if datos["precio_actual_num"] == 0.0:
        m = re.search(r'name="twister-plus-price-data-price"[^>]*value="([\d.]+)"', html_text)
        if m:
            datos["precio_actual_num"] = float(m.group(1))

    # Precio anterior (PVP)
    ambito = caja if caja else soup
    for sel in (".basisPrice .a-offscreen",
                ".a-price.a-text-price .a-offscreen",
                "[data-a-strike='true'] .a-offscreen"):
        for el in ambito.select(sel):
            v = parse_precio(el.get_text(" ", strip=True))
            if v and v > datos["precio_actual_num"] > 0:
                datos["precio_antes_num"] = v
                break
        if datos["precio_antes_num"]:
            break
    if datos["precio_antes_num"] == 0.0:
        m = re.search(r'name="twister-plus-price-data-basisPrice"[^>]*value="([\d.]+)"', html_text)
        if m and float(m.group(1)) > datos["precio_actual_num"] > 0:
            datos["precio_antes_num"] = float(m.group(1))
    if datos["precio_antes_num"] == 0.0:
        m = re.search(
            r"(?:Precio recomendado|Precio de venta recomendado|Precio anterior|Precio de lista|PVP)\s*:?\s*" + RE_PRECIO,
            texto_plano, re.I)
        if m:
            v = parse_precio(m.group(1))
            if v and v > datos["precio_actual_num"] > 0:
                datos["precio_antes_num"] = v

    el_badge = (caja.select_one(".savingsPercentage") if caja else None) or soup.select_one(".savingsPercentage")
    if el_badge:
        mb = re.search(r"(\d{1,2})\s*%", el_badge.get_text())
        if mb:
            datos["badge_pct"] = int(mb.group(1))

    datos["es_prime"] = bool(re.search(r'class="[^"]*a-icon-prime[^"]*"', html_text)) or bool(soup.select_one("i.a-icon-prime"))
    return datos


# ==============================================================================
# VALIDACIÓN Y DESCUENTO
# ==============================================================================
def calcular_descuento(datos):
    """Descuento REAL (entero) o None."""
    act, ant, badge = datos["precio_actual_num"], datos["precio_antes_num"], datos["badge_pct"]
    if ant > act > 0:
        pct = round((ant - act) / ant * 100)
        return pct if 1 <= pct <= 95 else None
    if badge and 1 <= badge <= 95:
        return badge
    return None


def comprobar_fiabilidad(datos):
    """Devuelve (ok, motivo). Evita publicar un precio que no sea el de tu variante."""
    act, ant, badge = datos["precio_actual_num"], datos["precio_antes_num"], datos["badge_pct"]

    if act <= 0:
        return False, "No se pudo leer el precio actual."
    if datos["fuente"] == "manual":
        return True, ""

    if ant > act:
        pct = round((ant - act) / ant * 100)
        if badge and abs(badge - pct) > 3:
            return False, (f"El descuento calculado ({pct}%) no coincide con la etiqueta de Amazon ({badge}%): "
                           f"probablemente se ha leído el precio de otra variante.")
    if datos["fuente"] == "html" and not badge:
        return False, "Leído solo del HTML y sin etiqueta de descuento para contrastar."
    return True, ""


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
    # Si llega texto de "Compartir" (frase + enlace), nos quedamos solo con la primera URL
    m_url = re.search(r"https?://[^\s\"'<>]+", url_acortada)
    if m_url:
        url_acortada = m_url.group(0)
    print(f"🔗 Enlace recibido: {url_acortada}")

    url_real = expandir_enlace(url_acortada)
    m = re.search(r"(?:/dp/|/gp/product/|%2Fdp%2F|%2Fproduct%2F)([A-Z0-9]{10})", url_real)
    if not m:
        print(f"❌ ASIN no detectado en: {url_real!r}")
        sys.exit(1)
    asin = m.group(1)
    print(f"🔗 ASIN: {asin}")

    # Si pegas un enlace corto de SiteStripe (link.amazon / amzn.to) se usa tal cual.
    # Si pegas cualquier otro (p. ej. el de "Compartir" de la app de Amazon, que NO lleva
    # tu etiqueta de afiliado), se construye uno propio con tu etiqueta.
    if any(d in url_acortada for d in ("link.amazon", "amzn.to")):
        enlace = url_acortada
    else:
        enlace = generar_enlace_afiliado(asin) + f"&{PARAMS_VARIANTE}"
        print("ℹ️ Enlace sin etiqueta de afiliado detectado: se ha generado uno con tu etiqueta.")

    # Precios opcionales escritos por ti (los que ves en la pantalla)
    ov_act = num_usuario(os.getenv("PRECIO_ACTUAL", ""))
    ov_ant = num_usuario(os.getenv("PRECIO_ANTES", ""))

    # 1) Navegador real; 2) HTML como plan B
    d = await extraer_con_navegador(asin)
    datos = datos_desde_navegador(d) if d and d.get("titulo") else None
    if datos is None or datos["precio_actual_num"] <= 0:
        h = extraer_datos_html(asin)
        if datos and datos.get("titulo") and not h["titulo"]:
            h["titulo"] = datos["titulo"]
        datos = h

    # 3) Tus precios mandan sobre cualquier lectura automática
    if ov_act:
        datos["precio_actual_num"] = ov_act
        datos["fuente"] = "manual"
        datos["badge_pct"] = None
        if datos["precio_antes_num"] <= ov_act:
            datos["precio_antes_num"] = 0.0
    if ov_ant:
        datos["precio_antes_num"] = ov_ant
        datos["fuente"] = "manual"
        datos["badge_pct"] = None
    if datos["precio_antes_num"] and datos["precio_antes_num"] <= datos["precio_actual_num"]:
        datos["precio_antes_num"] = 0.0

    print(f"📊 Fuente={datos['fuente']} | Actual={datos['precio_actual_num']} | "
          f"Antes={datos['precio_antes_num']} | Etiqueta={datos['badge_pct']}")

    ok, motivo = comprobar_fiabilidad(datos)
    if not ok:
        print(f"::error::NO PUBLICO: {motivo}")
        print("👉 Vuelve a lanzar el workflow escribiendo en los campos opcionales el "
              "'Precio actual' y el 'Precio anterior' tal como los ves en la página de Amazon.")
        sys.exit(2)

    pct = calcular_descuento(datos)
    descuento_txt = f"-{pct}%" if pct else None
    print(f"✅ Descuento: {descuento_txt}")

    titulo = datos["titulo"] or "Producto destacado en Amazon"
    actual, antes = datos["precio_actual_num"], datos["precio_antes_num"]
    hay_antes = antes > actual > 0
    precio_txt = f"{actual:.2f}".replace(".", ",")
    antes_txt = f"{antes:.2f}".replace(".", ",") if hay_antes else None

    # ---- Texto de X ----
    cab_x = f"🔥 {descuento_txt} de descuento 🔥" if descuento_txt else "⭐ PRODUCTO DESTACADO ⭐"
    linea_x = f"💰 Ahora: {precio_txt}€"
    if antes_txt:
        linea_x += f" (Antes: {antes_txt}€)"
    titulo_x = titulo[:80].rstrip() + ("…" if len(titulo) > 80 else "")
    hashtags = "#chollo #ofertas #amazon #descuentos" if descuento_txt else "#amazon #recomendado"
    texto_x = (
        f"{cab_x}\n"
        f"📦 {titulo_x}\n\n"
        f"{linea_x}\n\n"
        f"🛒 {enlace}\n\n"
        f"{hashtags}\n(Enlace de afiliado)"
    )

    # ---- Texto de Telegram ----
    cab_tg = f"🔥 <b>¡{html.escape(descuento_txt)} DE DESCUENTO!</b> 🔥" if descuento_txt else "⭐ <b>PRODUCTO DESTACADO</b> ⭐"
    linea_tg = f"💰 <b>Precio actual:</b> {precio_txt}€"
    if antes_txt:
        linea_tg += f" (PVP: <s>{antes_txt}€</s>)"
    titulo_tg = html.escape(titulo[:150]) + ("…" if len(titulo) > 150 else "")
    texto_tg = (
        f"{cab_tg}\n\n"
        f"📦 <b>Producto:</b> {titulo_tg}\n\n"
        f"{linea_tg}\n\n"
        f"🛒 <b>Ver oferta aquí:</b> {html.escape(enlace)}\n\n"
        f"<i>{html.escape(hashtags)}</i>\n<i>(Enlace de afiliado)</i>"
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
