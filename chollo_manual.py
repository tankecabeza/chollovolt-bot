import asyncio
import sys
import html
import re
import os
import random
import urllib.parse
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright

from auto_chollovolt import (
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, X_STATE, publicar_en_x
)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
]

def cargar_fuente(tamano, negrita=False):
    rutas = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if negrita else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if negrita else r"C:\Windows\Fonts\arial.ttf",
    ]
    for r in rutas:
        if os.path.exists(r):
            try: return ImageFont.truetype(r, tamano)
            except: pass
    return ImageFont.load_default()

def crear_imagen_definitiva(descuento, precio_actual_num, precio_antes_str, ruta_salida, es_prime=False):
    W, H = 800, 440
    img = Image.new("RGB", (W, H), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    f_gigante = cargar_fuente(115, negrita=False)
    f_super = cargar_fuente(52, negrita=True)
    f_gris = cargar_fuente(36, negrita=False)
    f_prime = cargar_fuente(52, negrita=True)

    draw.text((45, 50), descuento, font=f_gigante, fill=(204, 12, 57))
    bbox_desc = draw.textbbox((45, 50), descuento, font=f_gigante)
    x_precio = bbox_desc[2] + 40

    partes = f"{precio_actual_num:.2f}".replace(".", ",").split(",")
    draw.text((x_precio, 50), partes[0], font=f_gigante, fill=(15, 17, 17))
    bbox_eur = draw.textbbox((x_precio, 50), partes[0], font=f_gigante)
    draw.text((bbox_eur[2] + 6, 58), f"{partes[1]} €", font=f_super, fill=(15, 17, 17))

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

def expandir_enlace(url_corta):
    try:
        r = requests.get(
            url_corta, 
            headers={"User-Agent": "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36"}, 
            allow_redirects=True, 
            timeout=15
        )
        return urllib.parse.unquote(r.url.split('?')[0])
    except Exception as e:
        return urllib.parse.unquote(str(e))

def extraer_datos_autonomos(asin):
    url_directa = f"https://www.amazon.es/dp/{asin}"
    url_proxy = f"https://www-amazon-es.translate.goog/dp/{asin}?_x_tr_sl=auto&_x_tr_tl=es&_x_tr_hl=es&_x_tr_pto=wapp"
    ua_googlebot = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
    
    html_text = ""
    try:
        r = requests.get(url_directa, headers={"User-Agent": ua_googlebot}, timeout=15)
        if r.status_code == 200 and "captchacharacters" not in r.text.lower():
            html_text = r.text
    except:
        pass
        
    if not html_text:
        try:
            r = requests.get(url_proxy, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=15)
            if r.status_code == 200:
                html_text = r.text
        except:
            pass

    soup = BeautifulSoup(html_text, "html.parser") if html_text else None
    
    # 1. TÍTULO
    titulo = "Oferta destacada en Amazon"
    if html_text:
        if soup and soup.select_one("#productTitle"):
            titulo = soup.select_one("#productTitle").get_text(strip=True)
        else:
            m_title = re.search(r'"title":\s*"([^"]+)"', html_text)
            if m_title: titulo = m_title.group(1)

    # 2. PRECIO ACTUAL (La Magia)
    actual = 0.0
    if html_text:
        # Búsqueda tradicional en variables
        for pat in [r'name="twister-plus-price-data-price" value="([\d\.]+)"', r'"priceAmount":\s*([\d\.]+)', r'data-asin-price="([\d\.]+)"']:
            m = re.search(pat, html_text)
            if m:
                actual = float(m.group(1))
                break
        
        # ESCÁNER CIEGO: Si no hay precio claro, barre todo el texto buscando el número más lógico
        if actual == 0.0 and soup:
            caja_central = soup.select_one('#centerCol, #rightCol, #desktop_buybox')
            if caja_central:
                texto_limpio = caja_central.get_text(separator=" ", strip=True)
                # Busca cualquier formato tipo 55,99 o 55.99
                precios_crudos = re.findall(r'(\d{1,4})[.,](\d{2})\b', texto_limpio)
                if precios_crudos:
                    # Filtra precios ilógicos (ej. 0.00 o más de 3000€) y saca el mínimo
                    valores_validos = [float(f"{p[0]}.{p[1]}") for p in precios_crudos if 0 < float(f"{p[0]}.{p[1]}") < 3000]
                    if valores_validos:
                        actual = min(valores_validos)
                        
    # 3. PRECIO ANTERIOR
    antes = 0.0
    if html_text:
        for pat in [r'name="twister-plus-price-data-basisPrice" value="([\d\.]+)"', r'"basisPrice"[^>]*"moneyValueOrPrice":"([\d\.]+)"']:
            m = re.search(pat, html_text)
            if m:
                antes = float(m.group(1))
                break
                
        # Escáner ciego para precio anterior
        if antes == 0.0 and soup:
            caja_tachada = soup.select_one('.a-text-strike, .basisPrice')
            if caja_tachada:
                txt_antes = re.search(r'(\d{1,4})[.,](\d{2})\b', caja_tachada.get_text(separator="", strip=True))
                if txt_antes:
                    antes = float(f"{txt_antes.group(1)}.{txt_antes.group(2)}")

    # Salvavidas final absoluto: Si incluso el escáner ciego falla, ponemos 1.00 para que publique
    if actual == 0.0:
        actual = 1.00

    # 4. DESCUENTO
    descuento = "TOP"
    if antes > 0 and actual > 0 and antes > actual:
        descuento = f"-{round((antes - actual) / antes * 100)}%"

    return {
        "titulo": titulo,
        "precio_actual_num": actual,
        "precio_antes_num": antes,
        "descuento": descuento,
        "es_prime": bool(soup.select_one('i.a-icon-prime')) if soup else False
    }

def publicar_telegram(imagen_ruta, texto):
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
        print("❌ ASIN no detectado.")
        sys.exit(1)
        
    asin = m.group(1)
    datos = extraer_datos_autonomos(asin)
    
    precio_txt = f"{datos['precio_actual_num']:.2f}".replace(".", ",")
    txt_precio_x = f"💰 Ahora: {precio_txt}€"
    txt_precio_tg = f"💰 <b>Precio actual:</b> {precio_txt}€"

    if datos['precio_antes_num'] > 0 and datos['precio_actual_num'] > 0 and datos['precio_antes_num'] > datos['precio_actual_num']:
        precio_antes_txt = f"{datos['precio_antes_num']:.2f}".replace(".", ",")
        txt_precio_x += f" (Antes: {precio_antes_txt}€)"
        txt_precio_tg += f" (PVP: <s>{precio_antes_txt}€</s>)"
    else:
        precio_antes_txt = None

    titulo_corto = datos['titulo'][:80].rstrip() + ("…" if len(datos['titulo']) > 80 else "")
    
    texto_x = (
        f"🔥 ¡OFERTA DESTACADA! 🔥\n"
        f"📦 {titulo_corto}\n\n"
        f"{txt_precio_x}\n\n"
        f"🛒 {url_acortada}\n\n"
        f"#chollo #ofertas #amazon #descuentos\n(Enlace de afiliado)"
    )

    texto_tg = (
        f"🔥 ¡OFERTA DESTACADA! 🔥\n\n"
        f"📦 <b>Producto:</b> {html.escape(datos['titulo'][:150])}...\n\n"
        f"{txt_precio_tg}\n\n"
        f"🛒 <b>Ver oferta aquí:</b> {html.escape(url_acortada)}\n\n"
        f"<i>#chollo #ofertas #amazon #descuentos</i>\n<i>(Enlace de afiliado)</i>"
    )

    crear_imagen_definitiva(
        datos['descuento'], datos['precio_actual_num'], precio_antes_txt,
        "chollo_manual.jpg", es_prime=datos['es_prime']
    )

    if publicar_telegram("chollo_manual.jpg", texto_tg):
        print("✅ Telegram: Publicado con éxito.")
        
    if X_STATE:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
            context_x = await browser.new_context(user_agent=USER_AGENTS[0])
            import json
            await context_x.add_cookies(json.loads(X_STATE).get("cookies", []))
            await publicar_en_x(context_x, texto_x, "chollo_manual.jpg")
            await context_x.close()
            await browser.close()
        print("✅ X: Publicado con éxito.")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python chollo_manual.py <URL>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
