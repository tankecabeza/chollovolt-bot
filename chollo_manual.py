import asyncio
import sys
import re
import html
from playwright.async_api import async_playwright
import requests
# Reutilizamos funciones de tu script principal
from auto_chollovolt import (
    crear_imagen_chollo, parse_precio, generar_enlace_afiliado, 
    USER_AGENT, JS_EXTRAER, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, X_STATE, publicar_en_x
)

async def extraer_datos_amazon(url, page):
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    
    # Extraer ASIN de la URL
    asin_match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    asin = asin_match.group(1) if asin_match else "DESCONOCIDO"

    try:
        if await page.locator("#sp-cc-accept").count():
            await page.locator("#sp-cc-accept").click(timeout=1500)
    except Exception:
        pass

    await page.wait_for_selector("#productTitle", timeout=8000)
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
        "asin": asin,
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

async def main(url_chollo):
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        print(f"🔍 Analizando chollo: {url_chollo}")
        try:
            datos = await extraer_datos_amazon(url_chollo, page)
        except Exception as e:
            print(f"❌ Error al raspar: {e}")
            await browser.close()
            return

        enlace_afiliado = generar_enlace_afiliado(datos["asin"])
        
        # Generar Textos
        cabecera = f"🔥 {datos['descuento']} de descuento 🔥" if "%" in datos['descuento'] else "⚡ ¡OFERTA DESTACADA! ⚡"
        
        txt_precio_x = f"💰 Ahora: {datos['precio_actual']}€"
        txt_precio_tg = f"💰 <b>Precio:</b> {datos['precio_actual']}€"
        if datos['precio_antes']:
            txt_precio_x += f" (Antes: {datos['precio_antes']}€)"
            txt_precio_tg += f" (PVP: <s>{datos['precio_antes']}€</s>)"

        titulo_corto = datos['titulo'][:70].rstrip() + ("…" if len(datos['titulo']) > 70 else "")

        texto_x = (
            f"{cabecera}\n"
            f"📦 {titulo_corto}\n"
            f"{txt_precio_x}\n"
            f"🛒 {enlace_afiliado}\n\n"
            f"#chollos #amazon #ofertas\n(Enlace de afiliado)"
        )

        texto_tg = (
            f"{cabecera}\n\n"
            f"📦 <b>Producto:</b> {html.escape(datos['titulo'][:180])}\n\n"
            f"{txt_precio_tg}\n\n"
            f"🛒 <b>Comprar aquí:</b> {html.escape(enlace_afiliado)}\n\n"
            f"<i>Como afiliado de Amazon, obtengo ingresos por compras adscritas.</i>"
        )

        # Generar Imagen
        ruta_img = "chollo_manual.jpg"
        crear_imagen_chollo(
            datos['descuento'], datos['precio_actual'], datos['precio_antes'],
            ruta_img, es_prime=datos['es_prime']
        )

        print("\n📝 TEXTO GENERADO:")
        print(texto_x)
        print("-" * 40)

        # Publicar
        publicar_telegram_manual(ruta_img, texto_tg)
        print("✅ Enviado a Telegram.")

        if X_STATE:
            import json
            await context.add_cookies(json.loads(X_STATE).get("cookies", []))
            await publicar_en_x(context, texto_x, ruta_img)
            print("✅ Enviado a X.")

        await browser.close()

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python chollo_manual.py <URL_DE_AMAZON>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
