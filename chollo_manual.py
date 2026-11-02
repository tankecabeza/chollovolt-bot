import asyncio
import sys
import html
import re
from playwright.async_api import async_playwright
import requests

from auto_chollovolt import (
    crear_imagen_chollo, parse_precio,
    JS_EXTRAER, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, X_STATE, publicar_en_x
)

def expandir_enlace(url_corta):
    try:
        print(f"🔗 Resolviendo destino de: {url_corta}")
        # Resolvemos a dónde apunta el enlace corto simulando ser un móvil real
        r = requests.get(
            url_corta, 
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36"}, 
            allow_redirects=True, 
            timeout=15
        )
        print(f"📍 Redirigido a: {r.url.split('?')[0]}")
        return r.url
    except Exception as e:
        print(f"⚠️ Fallo al expandir enlace: {e}")
        return url_corta

async def extraer_datos_amazon_troya(asin, browser):
    # EL CABALLO DE TROYA: Usamos el dominio fantasma de Google Translate.
    # Amazon JAMÁS bloquea estas peticiones porque confía ciegamente en las IPs de Google.
    url_proxy_google = f"https://www-amazon-es.translate.goog/dp/{asin}?_x_tr_sl=auto&_x_tr_tl=es&_x_tr_hl=es&_x_tr_pto=wapp"
    url_directa = f"https://www.amazon.es/dp/{asin}"
    
    rutas = [
        ("Caballo_Troya_Google", url_proxy_google),
        ("Ataque_Frontal", url_directa)
    ]
    
    for nombre, url_scraping in rutas:
        print(f"\n🔄 Iniciando infiltración vía: {nombre}...")
        
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = await context.new_page()
        
        try:
            # Vamos a la página y damos margen para que Google cargue la estructura
            await page.goto(url_scraping, wait_until="domcontentloaded", timeout=30000)
            
            # Quitar cookies si aparecen en el proxy
            if await page.locator("#sp-cc-accept").count():
                await page.locator("#sp-cc-accept").click(timeout=2000)

            # Esperamos a que Google renderice el título de Amazon
            try:
                await page.wait_for_selector("#productTitle", timeout=12000)
            except Exception:
                print(f"⏳ El muro de Amazon rechazó esta vía ({nombre}).")
                await page.screenshot(path=f"error_{nombre}.png")
                await context.close()
                continue

            # Inyectamos el bisturí JavaScript para sacar los precios
            d = await page.evaluate(JS_EXTRAER)
            
            if not d.get("titulo"):
                raise Exception("El título existe pero el proxy lo ocultó.")

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
            print(f"⚠️ Fallo en {nombre}: {str(e)[:50]}")
            await context.close()

    raise Exception("Imposible atravesar el muro tras intentar el bypass de Google.")

def publicar_telegram_manual(imagen_ruta, texto):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    with open(imagen_ruta, "rb") as foto:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": texto, "parse_mode": "HTML"}, files={"photo": foto})
    return r.status_code == 200

async def main(url_acortada):
    url_real = expandir_enlace(url_acortada)
    
    m = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url_real)
    if not m:
        print("❌ No se detectó un ASIN válido en el enlace.")
        sys.exit(1)
        
    asin = m.group(1)
    print(f"✨ ASIN detectado ({asin}). Sacando el Modo PRO...")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        
        try:
            datos = await extraer_datos_amazon_troya(asin, browser)
            print("✅ ¡BARRERA SUPERADA! Datos extraídos como un ninja.")
        except Exception as e:
            print(f"\n❌ FRACASO TOTAL: {e}")
            await browser.close()
            sys.exit(1)
        
        # ---------------------------------------------------------
        # COMPOSICIÓN DEL MENSAJE (RESPETANDO TU ENLACE ACORTADO)
        # ---------------------------------------------------------
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

        # ---------------------------------------------------------
        # PUBLICACIÓN FINAL
        # ---------------------------------------------------------
        if publicar_telegram_manual(ruta_img, texto_tg):
            print("✅ Telegram: Publicado con éxito.")
        else:
            print("❌ Telegram: Falló.")
        
        if X_STATE:
            print("🚀 Conectando a X (Twitter)...")
            context_x = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36")
            import json
            await context_x.add_cookies(json.loads(X_STATE).get("cookies", []))
            await publicar_en_x(context_x, texto_x, ruta_img)
            await context_x.close()
            print("✅ X: Publicado con éxito.")
        
        await browser.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python chollo_manual.py <URL_ACORTADA>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
