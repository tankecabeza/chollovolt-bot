import asyncio
import sys
import html
import re
import random
from playwright.async_api import async_playwright
import requests
from auto_chollovolt import (
    crear_imagen_chollo, parse_precio,
    JS_EXTRAER, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, X_STATE, publicar_en_x
)

# Arsenal de identidades falsas para saltar los bloqueos de Amazon
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:122.0) Gecko/20100101 Firefox/122.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Edge/122.0.0.0"
]

def expandir_enlace(url_corta):
    """Sigue la redirección del enlace móvil para extraer la URL real web"""
    try:
        print(f"🔗 Resolviendo destino de: {url_corta}")
        headers = {"User-Agent": random.choice(USER_AGENTS), "Accept-Language": "es-ES,es;q=0.9"}
        r = requests.get(url_corta, headers=headers, allow_redirects=True, timeout=15)
        print(f"📍 Redirigido a: {r.url}")
        return r.url
    except Exception as e:
        print(f"⚠️ Fallo al expandir enlace: {e}")
        return url_corta

async def extraer_datos_amazon_con_reintentos(url_scraping, browser):
    """Bucle de reintentos con cambio de identidad si detecta CAPTCHA o bloqueo"""
    for intento in range(1, 6):
        print(f"\n🔄 Intento {intento}/5: Entrando a Amazon con identidad encubierta...")
        
        # Crear un contexto completamente nuevo en cada intento con un User-Agent distinto
        ua = random.choice(USER_AGENTS)
        context = await browser.new_context(user_agent=ua, viewport={"width": 1920, "height": 1080})
        page = await context.new_page()
        
        try:
            await page.goto(url_scraping, wait_until="domcontentloaded", timeout=25000)
            
            # Detectar Captcha explícito
            if await page.locator("form[action*='validateCaptcha'], input#captchacharacters").count() > 0:
                print("🛡️ ¡Muro de Amazon detectado! (CAPTCHA). Abortando este intento...")
                await context.close()
                await asyncio.sleep(random.uniform(1.5, 3.5))
                continue

            # Aceptar cookies si salen
            if await page.locator("#sp-cc-accept").count():
                await page.locator("#sp-cc-accept").click(timeout=2000)

            # Esperar a que el título aparezca
            try:
                await page.wait_for_selector("#productTitle", timeout=8000)
            except Exception:
                print("⏳ El producto no cargó a tiempo o la estructura es rara. Abortando este intento...")
                await context.close()
                await asyncio.sleep(random.uniform(1.5, 3.5))
                continue

            # Si llegamos aquí, la página cargó bien. Extraemos los datos.
            d = await page.evaluate(JS_EXTRAER)
            if not d.get("titulo"):
                raise Exception("El título existe pero vino vacío.")

            actual = parse_precio(d.get("actual"))
            antes = parse_precio(d.get("antes"))

            if not actual:
                raise Exception("No se pudo leer el precio actual.")

            descuento_txt = "TOP"
            if antes and antes > actual:
                pct = round((antes - actual) / antes * 100)
                descuento_txt = f"-{pct}%"

            await context.close()
            return {
                "titulo": d["titulo"],
                "precio_actual": f"{actual:.2f}".replace(".", ","),
                "precio_antes": f"{antes:.2f}".replace(".", ",") if antes else None,
                "descuento": descuento_txt,
                "es_prime": bool(d.get("prime")),
                "imagen_original": d.get("imagen")
            }

        except Exception as e:
            print(f"⚠️ Fallo general en el intento {intento}: {str(e)[:50]}...")
            await context.close()
            await asyncio.sleep(random.uniform(1.5, 3.5))

    # Si tras 5 intentos falla, hacemos captura del último desastre
    context = await browser.new_context(user_agent=USER_AGENTS[0])
    page = await context.new_page()
    await page.goto(url_scraping, timeout=15000)
    await page.screenshot(path="error_manual.png")
    await context.close()
    raise Exception("Imposible saltar el bloqueo de Amazon tras 5 intentos. Captura guardada.")

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
    url_real = expandir_enlace(url_acortada)
    
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
        
        try:
            datos = await extraer_datos_amazon_con_reintentos(url_scraping, browser)
        except Exception as e:
            print(f"\n❌ FRACASO TOTAL: {e}")
            await browser.close()
            sys.exit(1)
        
        cabecera = f"🔥 {datos['descuento']} de descuento 🔥" if "%" in datos['descuento'] else "⚡ ¡OFERTA DESTACADA! ⚡"
        txt_precio_x = f"💰 Ahora: {datos['precio_actual']}€"
        txt_precio_tg = f"💰 <b>Precio actual:</b> {datos['precio_actual']}€"
        
        if datos['precio_antes']:
            txt_precio_x += f" (Antes: {datos['precio_antes']}€)"
            txt_precio_tg += f" (Precio anterior: <s>{datos['precio_antes']}€</s>)"

        titulo_corto = datos['titulo'][:80].rstrip() + ("…" if len(datos['titulo']) > 80 else "")
        
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
            # Solo si vamos a publicar en X, creamos contexto y le pasamos cookies
            context_x = await browser.new_context(user_agent=USER_AGENTS[0])
            import json
            await context_x.add_cookies(json.loads(X_STATE).get("cookies", []))
            await publicar_en_x(context_x, texto_x, ruta_img)
            await context_x.close()
            print("✅ Publicado con éxito en X.")
        
        await browser.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python chollo_manual.py <URL_ACORTADA>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
