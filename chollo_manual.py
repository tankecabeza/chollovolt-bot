import asyncio
import sys
import html
import re
import random
import urllib.parse
import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

# Reutilizamos las funciones limpias de tu script principal
from auto_chollovolt import (
    crear_imagen_chollo, parse_precio,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, X_STATE, publicar_en_x
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0"
]

def expandir_enlace(url_corta):
    try:
        print(f"🔗 Resolviendo destino de: {url_corta}")
        headers = {"User-Agent": random.choice(USER_AGENTS), "Accept-Language": "es-ES,es;q=0.9"}
        r = requests.get(url_corta, headers=headers, allow_redirects=True, timeout=15)
        print(f"📍 Redirigido a: {r.url}")
        return r.url
    except Exception as e:
        print(f"⚠️ Fallo al expandir enlace: {e}")
        return url_corta

def parse_html_robusto(html_text):
    """Extrae datos del HTML en crudo como un cirujano, ignorando bloqueos de JavaScript"""
    if "captchacharacters" in html_text or "Escribe los caracteres" in html_text:
        raise Exception("CAPTCHA_DETECTED")
        
    soup = BeautifulSoup(html_text, "html.parser")
    
    # TITULO
    titulo_el = soup.select_one("#productTitle")
    if not titulo_el:
        raise Exception("No se encontró el #productTitle")
    titulo = titulo_el.get_text(strip=True)
    
    # PRECIO ACTUAL (Barrido intensivo por posibles clases CSS)
    actual = None
    for selector in ['.priceToPay .a-offscreen', '#corePriceDisplay_desktop_feature_div .a-price .a-offscreen', '#corePrice_feature_div .a-price .a-offscreen', '#priceblock_ourprice', '.apexPriceToPay .a-offscreen']:
        el = soup.select_one(selector)
        if el:
            actual = parse_precio(el.get_text(strip=True))
            if actual: break
            
    # Fallback brutal: buscar en los datos ocultos de Amazon si falla el DOM
    if not actual:
        m = re.search(r'"priceAmount":\s*([\d\.]+)', html_text)
        if m: actual = float(m.group(1))
            
    if not actual:
        raise Exception("No se pudo extraer el precio del HTML")
        
    # PRECIO ANTERIOR
    antes = None
    for selector in ['.basisPrice .a-offscreen', '[data-a-strike="true"] .a-offscreen', '#listPrice', '.a-text-strike']:
        el = soup.select_one(selector)
        if el:
            antes = parse_precio(el.get_text(strip=True))
            if antes: break
            
    # IMAGEN
    img_url = None
    img_el = soup.select_one('#landingImage, #imgBlkFront')
    if img_el:
        img_url = img_el.get("data-old-hires") or img_el.get("src")
        if img_url and img_url.startswith("data:"):
            img_url = None
        
    es_prime = bool(soup.select_one('i.a-icon-prime'))
    
    descuento = "TOP"
    if antes and antes > actual:
        pct = round((antes - actual) / antes * 100)
        descuento = f"-{pct}%"
        
    return {
        "titulo": titulo,
        "precio_actual": f"{actual:.2f}".replace(".", ","),
        "precio_antes": f"{antes:.2f}".replace(".", ",") if antes else None,
        "descuento": descuento,
        "es_prime": es_prime,
        "imagen_original": img_url
    }

def extraer_amazon(asin):
    url_base = f"https://www.amazon.es/dp/{asin}"
    url_codificada = urllib.parse.quote(url_base)
    
    # Red de nodos para saltar el baneo de IP (Sin abrir navegadores, solo red pura)
    rutas = [
        f"https://api.allorigins.win/raw?url={url_codificada}",
        f"https://api.codetabs.com/v1/proxy?quest={url_codificada}",
        url_base, # Intento directo a pecho descubierto
        f"https://corsproxy.io/?{url_codificada}"
    ]
    
    for i, url in enumerate(rutas, 1):
        print(f"\n🔄 Intento {i}/4 (Extracción fantasma): Probando ruta {url[:50]}...")
        try:
            headers = {
                "User-Agent": random.choice(USER_AGENTS),
                "Accept-Language": "es-ES,es;q=0.9",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
            }
            r = requests.get(url, headers=headers, timeout=15)
            
            if r.status_code != 200:
                print(f"⚠️ Ruta caída. Código de error HTTP: {r.status_code}")
                continue
                
            try:
                datos = parse_html_robusto(r.text)
                print("✅ ¡BARRERA SUPERADA! Datos extraídos como un ninja.")
                return datos
            except Exception as e:
                if str(e) == "CAPTCHA_DETECTED":
                    print("🛡️ Muro de Amazon detectado en esta IP. Rotando nodo...")
                else:
                    print(f"⚠️ Estructura web ilegible en esta ruta: {e}")
                continue
                
        except Exception as e:
            print(f"⚠️ Fallo de conexión de red: {str(e)[:40]}")
            continue
            
    raise Exception("Infranqueable: Todas las rutas de extracción fallaron o pidieron CAPTCHA.")

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
    print(f"✨ ASIN detectado ({asin}). Preparando el asalto en crudo...")

    try:
        # 1. RASPAMOS SIN NAVEGADOR (Velocidad absurda e indetectable)
        datos = extraer_amazon(asin)
    except Exception as e:
        print(f"\n❌ FRACASO TOTAL: {e}")
        sys.exit(1)
        
    # 2. MONTAMOS EL FORMATO
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

    # 3. PUBLICAMOS
    if publicar_telegram_manual(ruta_img, texto_tg):
        print("✅ Telegram: Publicado con éxito.")
    else:
        print("❌ Telegram: Error al publicar.")
        
    if X_STATE:
        print("🚀 Lanzando Playwright SOLO para publicar en X...")
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
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
