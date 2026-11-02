import asyncio
import sys
import html
from playwright.async_api import async_playwright
import requests
# Importamos la lógica de imagen y configuración de tu script principal
from auto_chollovolt import (
    crear_imagen_chollo, parse_precio,
    USER_AGENT, JS_EXTRAER, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, X_STATE, publicar_en_x
)

async def extraer_datos_amazon(url, page):
    # Entra a tu enlace acortado y deja que Amazon lo redirija al producto real
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    
    try:
        if await page.locator("#sp-cc-accept").count():
            await page.locator("#sp-cc-accept").click(timeout=1500)
    except Exception:
        pass

    await page.wait_for_selector("#productTitle", timeout=8000)
    d = await page.evaluate(JS_EXTRAER)
    
    if not d.get("titulo"):
        raise Exception("No se pudo extraer el título de la página.")

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
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(user_agent=USER_AGENT, viewport={"width": 1280, "height": 800})
        page = await context.new_page()

        print(f"🔍 Analizando tu enlace: {url_acortada}")
        try:
            datos = await extraer_datos_amazon(url_acortada, page)
        except Exception as e:
            print(f"❌ Error extrayendo datos: {e}")
            await browser.close()
            return
        
        # ---------------------------------------------------------
        # GENERACIÓN DE TEXTOS Y FORMATO
        # ---------------------------------------------------------
        cabecera = f"🔥 {datos['descuento']} de descuento 🔥" if "%" in datos['descuento'] else "⚡ ¡OFERTA DESTACADA! ⚡"
        
        txt_precio_x = f"💰 Ahora: {datos['precio_actual']}€"
        txt_precio_tg = f"💰 <b>Precio actual:</b> {datos['precio_actual']}€"
        
        if datos['precio_antes']:
            txt_precio_x += f" (Antes: {datos['precio_antes']}€)"
            txt_precio_tg += f" (Precio anterior: <s>{datos['precio_antes']}€</s>)"

        # Formato X (Twitter)
        titulo_corto = datos['titulo'][:80].rstrip() + ("…" if len(datos['titulo']) > 80 else "")
        texto_x = (
            f"{cabecera}\n"
            f"📦 {titulo_corto}\n\n"
            f"{txt_precio_x}\n\n"
            f"🛒 {url_acortada}\n\n"
            f"#chollo #ofertas #amazon #descuentos\n"
            f"(Enlace de afiliado)"
        )

        # Formato Telegram
        texto_tg = (
            f"{cabecera}\n\n"
            f"📦 <b>Producto:</b> {html.escape(datos['titulo'][:150])}...\n\n"
            f"{txt_precio_tg}\n\n"
            f"🛒 <b>Ver oferta aquí:</b> {html.escape(url_acortada)}\n\n"
            f"<i>#chollo #ofertas #amazon #descuentos</i>\n"
            f"<i>(Enlace de afiliado)</i>"
        )

        # ---------------------------------------------------------
        # GENERACIÓN DE IMAGEN
        # ---------------------------------------------------------
        ruta_img = "chollo_manual.jpg"
        crear_imagen_chollo(
            datos['descuento'], datos['precio_actual'], datos['precio_antes'],
            ruta_img, es_prime=datos['es_prime']
        )

        print("\n📝 TEXTO GENERADO PARA X:")
        print(texto_x)
        print("-" * 40)

        # ---------------------------------------------------------
        # PUBLICACIÓN
        # ---------------------------------------------------------
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
