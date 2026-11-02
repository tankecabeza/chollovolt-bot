import asyncio
import sys
import html
import re
from playwright.async_api import async_playwright
import requests
from auto_chollovolt import (
    crear_imagen_chollo, parse_precio,
    USER_AGENT, JS_EXTRAER, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, X_STATE, publicar_en_x
)

def expandir_enlace(url_corta):
    """Sigue la redirección del enlace móvil para extraer la URL real web"""
    try:
        print(f"🔗 Resolviendo destino de: {url_corta}")
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept-Language": "es-ES,es;q=0.9"
        }
        r = requests.get(url_corta, headers=headers, allow_redirects=True, timeout=15)
        print(f"📍 Redirigido a: {r.url}")
        return r.url
    except Exception as e:
        print(f"⚠️ Fallo al expandir enlace: {e}")
        return url_corta

async def extraer_datos_amazon(url, page):
    print(f"🌐 Cargando página de extracción: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    
    try:
        if await page.locator("#sp-cc-accept").count():
            await page.locator("#sp-cc-accept").click(timeout=2000)
    except Exception:
        pass

    try:
        await page.wait_for_selector("#productTitle", timeout=15000)
    except Exception:
        await page.screenshot(path="error_manual.png")
        raise Exception("No se encontró el título en 15s. Captura guardada como error_manual.png.")

    d = await page.evaluate(JS_EXTRAER)
    
    if not d.get("titulo"):
        raise Exception("No se pudo extraer el título.")

    actual = parse_precio(d.get("actual"))
    antes = parse_precio(d.get("antes"))

    if not actual:
        raise Exception("No se pudo leer el precio actual.")

    descuento_txt = "TOP"
    if antes and antes > actual:
        pct = round((antes - actual) / antes * 100)
        descuento_txt = f"-{pct}%"

    return {
        "titulo": d["titulo"],
        "precio_actual": f"{actual:.2f}".replace(".", ","),
        "precio_antes": f"{antes:.2f}".replace(".", ",") if antes else None,
        "descuento": descuento_txt,
        "es_prime": bool(d.get("prime")),
        "imagen_original": d.get("imagen")
    }

def publicar_telegram_manual(imagen_ruta, texto):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("❌ Faltan credenciales de Telegram.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    with open(imagen_ruta, "rb") as foto:
        r = requests.post(
            url,
            data={"chat_id": TELEGRAM_CHAT_ID, "caption": texto, "parse_mode": "HTML"},
            files={"photo": foto}
        )
    return r.status_code == 200

async def main(url_acortada):
    # 1. Expandimos el enlace acortado para evitar bloqueos de la app móvil
    url_real = expandir_enlace(url_acortada)
    
    # 2. Extraemos el ASIN (código de producto) limpio
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url_real)
    if m:
        asin = m.group(1)
        url_scraping = f"https://www.amazon.es/dp/{asin}"
        print(f"✨ ASIN detectado ({asin}). Usaremos URL limpia para raspar: {url_scraping}")
    else:
        url_scraping = url_real
        print("⚠️ No se detectó ASIN puro, intentaremos raspar la URL redirigida.")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        try:
            datos = await extraer_datos_amazon(url_scraping, page)
        except Exception as e:
            print(f"❌ Error extrayendo datos: {e}")
            await browser.close()
            sys.exit(1)
        
        # Textos
        cabecera = f"🔥 {datos['descuento']} de descuento 🔥" if "%" in datos['descuento'] else "⚡ ¡OFERTA DESTACADA! ⚡"
        txt_precio_x = f"💰 Ahora: {datos['precio_actual']}€"
        txt_precio_tg = f"💰 <b>Precio actual:</b> {datos['precio_actual']}€"
        
        if datos['precio_antes']:
            txt_precio_x += f" (Antes: {datos['precio_antes']}€)"
            txt_precio_tg += f" (Precio anterior: <s>{datos['precio_antes']}€</s>)"

        titulo_corto = datos['titulo'][:80].rstrip() + ("…" if len(datos['titulo']) > 80 else "")
        
        # 3. PUBLICAMOS USANDO EL ENLACE ACORTADO ORIGINAL (url_acortada)
        texto_x = (
            f"{cabecera}\n"
            f"📦 {titulo_corto}\n\n"
            f"{txt_precio_x}\n\n"
            f"🛒 {url_acortada}\n\n"
            f"#chollo #ofertas #amazon #descuentos\n(Enlace de afiliado)"
        )

        texto_tg = (
            f"{cabecera}\n\n"
            f"📦 <b>Producto:</b> {html.escape(datos['titulo'][:150])}...\n\n"
            f"{txt_precio_tg}\n\n"
            f"🛒 <b>Ver oferta aquí:</b> {html.escape(url_acortada)}\n\n"
            f"<i>#chollo #ofertas #amazon #descuentos</i>\n<i>(Enlace de afiliado)</i>"
        )

        ruta_img = "chollo_manual.jpg"
        crear_imagen_chollo(
            datos['descuento'], datos['precio_actual'], datos['precio_antes'],
            ruta_img, es_prime=datos['es_prime']
        )

        print("\n📝 TEXTO GENERADO PARA X:")
        print(texto_x)
        print("-" * 40)

        if publicar_telegram_manual(ruta_img, texto_tg):
            print("✅ Publicado con éxito en Telegram.")
        else:
            print("❌ Falló Telegram.")
        
        if X_STATE:
            import json
            await context.add_cookies(json.loads(X_STATE).get("cookies", []))
            await publicar_en_x(context, texto_x, ruta_img)
            print("✅ Publicado con éxito en X.")
        
        await browser.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python chollo_manual.py <URL_ACORTADA>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
