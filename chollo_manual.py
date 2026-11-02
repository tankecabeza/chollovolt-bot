import asyncio
import sys
import html
import re
import random
import urllib.parse
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from auto_chollovolt import (
    crear_imagen_chollo, parse_precio,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, X_STATE, publicar_en_x
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
]

def expandir_enlace(url_corta):
    try:
        print(f"🔗 Resolviendo destino de: {url_corta}")
        r = requests.get(
            url_corta, 
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36"}, 
            allow_redirects=True, 
            timeout=15
        )
        url_final = urllib.parse.unquote(r.url.split('?')[0])
        print(f"📍 Redirigido a: {url_final}")
        return url_final
    except Exception as e:
        return urllib.parse.unquote(str(e))

def extraer_datos_amazon_definitivo(asin):
    # Usamos el proxy de Google Translate para bajar el HTML crudo y burlar la IP
    url_proxy = f"https://www-amazon-es.translate.goog/dp/{asin}?_x_tr_sl=auto&_x_tr_tl=es&_x_tr_hl=es&_x_tr_pto=wapp"
    
    print(f"🔄 Descargando código fuente vía Google Proxy...")
    r = requests.get(url_proxy, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=20)
    
    if r.status_code != 200:
        raise Exception(f"Google Proxy bloqueado: Error {r.status_code}")
        
    html_text = r.text
    soup = BeautifulSoup(html_text, "html.parser")
    
    # 1. TÍTULO
    titulo_el = soup.select_one("#productTitle")
    if not titulo_el:
        # Búsqueda en metadatos si falla el DOM
        m_title = re.search(r'"title":\s*"([^"]+)"', html_text)
        if m_title: 
            titulo = m_title.group(1)
        else:
            raise Exception("No se encontró el título del producto.")
    else:
        titulo = titulo_el.get_text(strip=True)
        
    # 2. PRECIO ACTUAL
    actual = None
    
    # Búsqueda clásica
    selectores = ['.priceToPay .a-offscreen', '#priceblock_ourprice', '#priceblock_dealprice', '.apexPriceToPay .a-offscreen', '.a-price .a-offscreen']
    for sel in selectores:
        for el in soup.select(sel):
            val = parse_precio(el.get_text(strip=True))
            if val:
                actual = val
                break
        if actual: break
        
    # Búsqueda quirúrgica en variables de Javascript (Ideal para ropa y zapatos)
    if not actual:
        m = re.search(r'"priceAmount":\s*([\d\.]+)', html_text)
        if m: actual = float(m.group(1))
        
    if not actual:
        print("⚠️ No se detectó precio (requiere elegir talla/variación). Usando precio genérico.")
        actual = 0.00
        
    # 3. PRECIO ANTERIOR
    antes = None
    selectores_antes = ['.basisPrice .a-offscreen', '[data-a-strike="true"] .a-offscreen', '#listPrice']
    for sel in selectores_antes:
        for el in soup.select(sel):
            val = parse_precio(el.get_text(strip=True))
            if val:
                antes = val
                break
        if antes: break

    # 4. IMAGEN
    img_url = None
    img_el = soup.select_one('#landingImage, #imgBlkFront')
    if img_el:
        img_url = img_el.get("data-old-hires") or img_el.get("src")
        
    if not img_url:
        m_img = re.search(r'"large":"([^"]+)"', html_text)
        if m_img: img_url = m_img.group(1)
        
    # 5. DESCUENTO
    descuento = "TOP"
    if antes and actual > 0 and antes > actual:
        pct = round((antes - actual) / antes * 100)
        descuento = f"-{pct}%"
        
    return {
        "titulo": titulo,
        "precio_actual": f"{actual:.2f}".replace(".", ",") if actual > 0 else "N/A",
        "precio_antes": f"{antes:.2f}".replace(".", ",") if antes else None,
        "descuento": descuento,
        "es_prime": bool(soup.select_one('i.a-icon-prime')),
        "imagen_original": img_url
    }

def publicar_telegram_manual(imagen_ruta, texto):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    with open(imagen_ruta, "rb") as foto:
        r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": texto, "parse_mode": "HTML"}, files={"photo": foto})
    return r.status_code == 200

async def main(url_acortada):
    url_real = expandir_enlace(url_acortada)
    
    m = re.search(r"(?:/dp/|/gp/product/|%2Fdp%2F|%2Fproduct%2F)([A-Z0-9]{10})", url_real)
    if not m:
        print("❌ No se detectó un ASIN válido en el enlace.")
        sys.exit(1)
        
    asin = m.group(1)
    print(f"✨ ASIN detectado ({asin}). Escaneando código interno...")

    try:
        datos = extraer_datos_amazon_definitivo(asin)
        print("✅ ¡Datos extraídos como un fantasma!")
    except Exception as e:
        print(f"\n❌ FRACASO TOTAL: {e}")
        sys.exit(1)
        
    # GENERAR TEXTOS
    cabecera = f"🔥 {datos['descuento']} de descuento 🔥" if "%" in datos['descuento'] else "⚡ ¡OFERTA DESTACADA! ⚡"
    
    txt_precio_x = f"💰 Ahora: {datos['precio_actual']}€" if datos['precio_actual'] != "N/A" else "💰 Precio: Según talla/modelo"
    txt_precio_tg = f"💰 <b>Precio actual:</b> {datos['precio_actual']}€" if datos['precio_actual'] != "N/A" else "💰 <b>Precio:</b> Según modelo"
    
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
    # Ajuste gráfico para precios ocultos
    precio_img = datos['precio_actual'] if datos['precio_actual'] != "N/A" else "0,00"
    crear_imagen_chollo(
        datos['descuento'], precio_img, datos['precio_antes'],
        ruta_img, es_prime=datos['es_prime']
    )

    print("\n📝 TEXTO GENERADO PARA X:")
    print(texto_x)
    print("-" * 40)

    # PUBLICAR
    if publicar_telegram_manual(ruta_img, texto_tg):
        print("✅ Telegram: Publicado con éxito.")
    else:
        print("❌ Telegram: Falló.")
        
    if X_STATE:
        print("🚀 Encendiendo navegador SOLO para publicar en X (Twitter)...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context_x = await browser.new_context(user_agent=USER_AGENTS[0])
            import json
            await context_x.add_cookies(json.loads(X_STATE).get("cookies", []))
            await publicar_en_x(context_x, texto_x, ruta_img)
            await context_x.close()
            await browser.close()
        print("✅ X: Publicado con éxito.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python chollo_manual.py <URL_ACORTADA>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
