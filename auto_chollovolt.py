import asyncio
import html
import json
import os
import re
import sys
from itertools import zip_longest

import requests
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright

# ==============================================================================
# CONFIGURACIÓN
# ==============================================================================
AMAZON_TAG = os.getenv("AMAZON_TAG") or "librospsico08-21"
X_STATE = os.getenv("X_STATE", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# Configuración forzada para publicar "Sí o Sí"
MAX_REVISAR = 80
CONCURRENCIA = 4
ARCHIVO_HISTORIAL = "historial_publicados.txt"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

# Fuentes de SaveMoney
FUENTES_PRIORITARIAS = [
    "https://savemoney.es/",
    "https://savemoney.es/ofertas/informatica",
    "https://savemoney.es/ofertas/electronica",
    "https://savemoney.es/ofertas/hogar",
    "https://savemoney.es/ofertas/juguetes",
]

BLOQUEAR_DOMINIOS = ("amazon-adsystem", "fls-eu.amazon", "google-analytics", "doubleclick")
RE_ASIN = re.compile(r"(B0[A-Z0-9]{8})")

class SinChollo(Exception):
    pass

def cargar_historial():
    if not os.path.exists(ARCHIVO_HISTORIAL):
        return set()
    with open(ARCHIVO_HISTORIAL, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def guardar_en_historial(asin):
    with open(ARCHIVO_HISTORIAL, "a", encoding="utf-8") as f:
        f.write(f"{asin}\n")

def generar_enlace_afiliado(asin):
    return f"https://www.amazon.es/dp/{asin}?tag={AMAZON_TAG}"

def parse_precio(texto):
    if not texto:
        return None
    t = texto.replace("\xa0", " ")
    m = re.search(r"\d+(?:\.\d{3})*,\d{2}", t)
    if m:
        return float(m.group().replace(".", "").replace(",", "."))
    m = re.search(r"\d+\.\d{2}\b", t)
    return float(m.group()) if m else None

def fmt_precio(valor):
    return f"{valor:.2f}".replace(".", ",")

def intercalar(listas):
    resultado, vistos = [], set()
    for grupo in zip_longest(*listas):
        for asin in grupo:
            if asin and asin not in vistos:
                vistos.add(asin)
                resultado.append(asin)
    return resultado

async def _filtrar_recursos(route):
    req = route.request
    if req.resource_type in ("image", "media", "font") or any(d in req.url for d in BLOQUEAR_DOMINIOS):
        await route.abort()
    else:
        await route.continue_()

async def nueva_pagina_amazon(context):
    page = await context.new_page()
    await page.route("**/*", _filtrar_recursos)
    return page

async def recoger_asins(context, url, sem):
    async with sem:
        page = await nueva_pagina_amazon(context)
        try:
            print(f"🌐 Cargando {url} ...")
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            
            # Scroll profundo y pausa larga para forzar la carga de ofertas en SaveMoney
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(2000)
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(2000)
            
            hrefs = await page.eval_on_selector_all("a", "els => els.map(e => e.href)")
            asins = []
            for href in hrefs:
                m = RE_ASIN.search(href or "")
                if m and m.group(1) not in asins:
                    asins.append(m.group(1))
            print(f"🔎 {len(asins):>3} ASINs extraídos de {url}")
            return asins
        except Exception as exc:
            print(f"⚠️ Aviso rastreando {url}: {exc}")
            return []
        finally:
            await page.close()

JS_EXTRAER = """() => {
  const primero = (sels) => {
    for (const s of sels) {
      const e = document.querySelector(s);
      if (e && e.textContent && e.textContent.trim()) return e.textContent.trim();
    }
    return null;
  };
  const img = document.querySelector('#landingImage, #imgBlkFront');
  let imgUrl = null;
  if (img) {
    imgUrl = img.getAttribute('data-old-hires') || img.getAttribute('src');
    if (imgUrl && imgUrl.startsWith('data:')) imgUrl = null;
  }
  return {
    titulo: primero(['#productTitle']),
    actual: primero([
      '#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen',
      '#corePrice_feature_div .priceToPay .a-offscreen',
      '.priceToPay .a-offscreen',
      '#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen',
      '#priceblock_dealprice', '#priceblock_ourprice'
    ]),
    antes: primero([
      '#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen',
      '#corePrice_feature_div .basisPrice .a-offscreen',
      '.basisPrice .a-offscreen',
      '#corePriceDisplay_desktop_feature_div .a-price.a-text-price .a-offscreen',
      '.a-price.a-text-price[data-a-strike="true"] .a-offscreen',
      '#listPrice'
    ]),
    carrito: !!document.querySelector('#add-to-cart-button, #buy-now-button'),
    prime: !!document.querySelector(
      '#buybox i.a-icon-prime, #desktop_buybox i.a-icon-prime, #mir-layout-DELIVERY_BLOCK i.a-icon-prime'
    ),
    imagen: imgUrl
  };
}"""

async def verificar_producto(context, asin, sem):
    async with sem:
        page = await nueva_pagina_amazon(context)
        try:
            await page.goto(f"https://www.amazon.es/dp/{asin}", wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_selector("#productTitle", timeout=8000)
            except Exception:
                return None

            d = await page.evaluate(JS_EXTRAER)
            if not d.get("titulo") or not d.get("carrito"):
                return None 

            actual = parse_precio(d.get("actual"))
            if not actual or actual <= 0:
                return None

            # MODO SÍ O SÍ: Aceptamos todo sin importar el % de descuento
            antes = parse_precio(d.get("antes"))
            
            if antes and antes > actual:
                pct = round((antes - actual) / antes * 100, 1)
                texto_desc = f"-{int(round(pct))}%"
                precio_antes_str = fmt_precio(antes)
            else:
                # Si Amazon oculta el precio antiguo, lo publicamos igual como "OFERTA"
                pct = 0
                texto_desc = "TOP"
                precio_antes_str = None

            print(f"   🎯 ¡Publicando sí o sí! {asin} a {fmt_precio(actual)} €")
            return {
                "asin": asin,
                "titulo_completo": d["titulo"],
                "texto_desc": texto_desc,
                "precio_actual": fmt_precio(actual),
                "precio_antes": precio_antes_str,
                "imagen_original": d.get("imagen"),
                "es_prime": bool(d.get("prime")),
            }
        except Exception:
            return None
        finally:
            await page.close()

async def buscar_chollo(context, historial):
    sem = asyncio.Semaphore(CONCURRENCIA)
    resultados = await asyncio.gather(*(recoger_asins(context, u, sem) for u in FUENTES_PRIORITARIAS))
    
    ordenados = intercalar(resultados)
    pendientes = [a for a in ordenados if a not in historial][:MAX_REVISAR]

    if not pendientes:
        raise SinChollo("No se han extraído productos de SaveMoney. (Puede que la web esté bloqueando temporalmente).")

    print(f"🧪 Verificando en Amazon {len(pendientes)} productos extraídos...")
    validos = []
    for i in range(0, len(pendientes), CONCURRENCIA):
        lote = pendientes[i:i + CONCURRENCIA]
        res = await asyncio.gather(*(verificar_producto(context, a, sem) for a in lote))
        validos += [r for r in res if r]
        if validos:
            break # En cuanto encuentra uno válido con stock, deja de buscar para publicar rápido

    if not validos:
        raise SinChollo(f"Ninguno de los productos extraídos tiene stock disponible en Amazon en este momento.")

    return validos[0]

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

def crear_imagen_chollo(texto_desc, precio_actual, precio_antes, ruta_salida, es_prime=False):
    W, H = 800, 440
    img = Image.new("RGB", (W, H), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    f_gigante = cargar_fuente(115, negrita=False)
    f_super = cargar_fuente(52, negrita=True)
    f_gris = cargar_fuente(36, negrita=False)
    f_prime = cargar_fuente(52, negrita=True)

    draw.text((45, 50), texto_desc, font=f_gigante, fill=(204, 12, 57))
    bbox_desc = draw.textbbox((45, 50), texto_desc, font=f_gigante)
    x_precio = bbox_desc[2] + 40

    partes = precio_actual.replace(".", ",").split(",")
    euros = partes[0]
    centimos = partes[1] if len(partes) > 1 else "00"

    draw.text((x_precio, 50), euros, font=f_gigante, fill=(15, 17, 17))
    bbox_eur = draw.textbbox((x_precio, 50), euros, font=f_gigante)
    draw.text((bbox_eur[2] + 6, 58), f"{centimos} €", font=f_super, fill=(15, 17, 17))

    # Si tenemos precio anterior lo tachamos, si no, ponemos un texto genérico de oferta
    if precio_antes:
        texto_inferior = f"Precio recomendado: {precio_antes}€"
        draw.text((45, 230), texto_inferior, font=f_gris, fill=(86, 89, 89))
        bbox_sp = draw.textbbox((45, 230), "Precio recomendado: ", font=f_gris)
        bbox_total = draw.textbbox((45, 230), texto_inferior, font=f_gris)
        y_linea = (bbox_total[1] + bbox_total[3]) // 2 + 2
        draw.line([(bbox_sp[2], y_linea), (bbox_total[2], y_linea)], fill=(86, 89, 89), width=3)
    else:
        draw.text((45, 230), "Oferta destacada en Amazon", font=f_gris, fill=(86, 89, 89))

    if es_prime:
        draw.line([(48, 348), (62, 366), (92, 326)], fill=(255, 153, 0), width=9)
        draw.text((98, 315), "prime", font=f_prime, fill=(0, 113, 197))

    draw.rectangle([(2, 2), (W - 3, H - 3)], outline=(225, 228, 228), width=3)
    img.save(ruta_salida, quality=95)
    return ruta_salida

def publicar_en_telegram(imagen_url, imagen_local, texto_mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        if imagen_url:
            r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "photo": imagen_url, "caption": texto_mensaje, "parse_mode": "HTML"}, timeout=20)
            if r.status_code == 200: return True
            
        with open(imagen_local, "rb") as foto:
            r = requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "caption": texto_mensaje, "parse_mode": "HTML"}, files={"photo": foto}, timeout=20)
        return r.status_code == 200
    except Exception:
        return False

async def publicar_en_x(context, texto_x, ruta_img):
    page = await context.new_page()
    try:
        await page.goto("https://x.com/compose/post", timeout=60000)
        for texto in ["Aceptar todas las cookies", "Accept all cookies", "Rechazar cookies no esenciales"]:
            try:
                await page.get_by_role("button", name=texto).click(timeout=2000)
                break
            except Exception:
                pass

        editor = page.locator("[data-testid='tweetTextarea_0']").first
        await editor.wait_for(timeout=30000)
        await editor.click(force=True)
        await page.keyboard.insert_text(texto_x)

        await page.locator("input[data-testid='fileInput']").first.set_input_files(ruta_img)
        await page.locator("[data-testid='attachments']").wait_for(timeout=30000)
        await page.wait_for_timeout(3000)

        boton = page.locator("[data-testid='tweetButton'], [data-testid='tweetButtonInline']").first
        await boton.wait_for(timeout=15000)
        
        try:
            await boton.click(timeout=6000)
        except Exception:
            await boton.dispatch_event("click")
            await page.wait_for_timeout(1500)
            if await editor.is_visible():
                await editor.focus()
                await page.keyboard.press("Control+Enter")

        await page.wait_for_timeout(5000)
        return True
    except Exception as exc:
        print(f"::warning::Error X: {exc}")
        return False
    finally:
        await page.close()

async def ejecutar_bot():
    tg_cfg = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
    estado_x = json.loads(X_STATE) if X_STATE else None
    x_cfg = estado_x is not None

    if not tg_cfg and not x_cfg:
        return False

    historial = cargar_historial()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                storage_state=estado_x,
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="es-ES",
            )

            try:
                chollo = await buscar_chollo(context, historial)
            except SinChollo as exc:
                print(f"ℹ️ {exc}")
                return True 

            asin = chollo["asin"]
            enlace = generar_enlace_afiliado(asin)
            titulo = chollo["titulo_completo"]
            
            # Textos condicionales dependiendo de si Amazon muestra el precio tachado oficial
            txt_precio_x = f"💰 Ahora: {chollo['precio_actual']}€"
            txt_precio_tg = f"💰 <b>Precio:</b> {chollo['precio_actual']}€"
            
            if chollo["precio_antes"]:
                txt_precio_x += f" (Antes: {chollo['precio_antes']}€)"
                txt_precio_tg += f" (PVP: <s>{chollo['precio_antes']}€</s>)"

            cabecera = f"🔥 {chollo['texto_desc']} descuento 🔥" if "%" in chollo['texto_desc'] else "⚡ ¡NUEVO CHOLLO! ⚡"
            
            titulo_x = titulo[:70].rstrip() + ("…" if len(titulo) > 70 else "")
            texto_x = (
                f"{cabecera}\n"
                f"📦 {titulo_x}\n"
                f"{txt_precio_x}\n"
                f"🛒 {enlace}\n\n"
                f"#chollos #amazon #ofertas\n"
                f"(Enlace afiliado)"
            )

            texto_telegram = (
                f"{cabecera}\n\n"
                f"📦 <b>Producto:</b> {html.escape(titulo[:180])}\n\n"
                f"{txt_precio_tg}\n\n"
                f"🛒 <b>Comprar aquí:</b> {html.escape(enlace)}\n\n"
                f"<i>Como afiliado de Amazon, obtengo ingresos por las compras adscritas que cumplen los requisitos aplicables.</i>"
            )

            ruta_img = crear_imagen_chollo(
                chollo["texto_desc"], chollo["precio_actual"], chollo["precio_antes"],
                "chollo.jpg", es_prime=chollo["es_prime"]
            )

            resultados = {}
            if tg_cfg: resultados["Telegram"] = publicar_en_telegram(chollo["imagen_original"], ruta_img, texto_telegram)
            if x_cfg: resultados["X"] = await publicar_en_x(context, texto_x, ruta_img)

            if any(resultados.values()):
                guardar_en_historial(asin)

            return all(resultados.values())
        except Exception:
            return False
        finally:
            await browser.close()

if __name__ == "__main__":
    ok = asyncio.run(ejecutar_bot())
    sys.exit(0 if ok else 1)
