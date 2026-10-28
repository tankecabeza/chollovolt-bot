import asyncio
import html
import json
import os
import random
import re
import sys
import traceback
from collections import Counter
from itertools import zip_longest

import requests
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright

# ==============================================================================
# CONFIGURACIÓN
# ==============================================================================
def _env_float(nombre, defecto):
    try:
        return float(os.getenv(nombre) or defecto)
    except ValueError:
        return float(defecto)

AMAZON_TAG = os.getenv("AMAZON_TAG") or "librospsico08-21"
X_STATE = os.getenv("X_STATE", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MIN_DESCUENTO = _env_float("MIN_DESCUENTO", 15)
UMBRAL_ERROR_PRECIO = _env_float("UMBRAL_ERROR_PRECIO", 50)
MAX_DESCUENTO = _env_float("MAX_DESCUENTO", 95)
MAX_REVISAR = int(_env_float("MAX_REVISAR", 15))
CONCURRENCIA = int(_env_float("CONCURRENCIA", 1))

# ESCALERA KAMIKAZE: Baja hasta el 0% para publicar SÍ o SÍ
PISO_ABSOLUTO = 0.0
ESCALERA = [MIN_DESCUENTO, 10.0, 5.0, PISO_ABSOLUTO]

ARCHIVO_HISTORIAL = "historial_publicados.txt"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

FUENTES_PRIORITARIAS = [
    "https://savemoney.es/",
    "https://savemoney.es/ofertas/informatica",
    "https://savemoney.es/ofertas/electronica",
    "https://savemoney.es/ofertas/hogar",
    "https://savemoney.es/ofertas/juguetes",
]
FUENTES_SECUNDARIAS = [
    "https://www.amazon.es/gp/goldbox",
    "https://www.amazon.es/gp/movers-and-shakers/electronics",
    "https://www.amazon.es/gp/movers-and-shakers/kitchen",
    "https://www.amazon.es/gp/movers-and-shakers/computers",
]

BLOQUEAR_DOMINIOS = ("amazon-adsystem", "fls-eu.amazon", "google-analytics", "doubleclick")

RE_ASIN = re.compile(r"(B0[A-Z0-9]{8})")
RE_ASIN_HTML = re.compile(r"(?:/dp/|/gp/product/|/gp/aw/d/|asin[\"'=:\s]{1,6})(B0[A-Z0-9]{8})", re.I)

MOTIVOS = Counter()

class SinChollo(Exception):
    pass

def rechazo(motivo):
    MOTIVOS[motivo] += 1
    return None

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

async def hay_captcha(page):
    try:
        return await page.locator("form[action*='validateCaptcha'], input#captchacharacters").count() > 0
    except Exception:
        return False

async def recoger_asins(context, url, sem):
    async with sem:
        page = await nueva_pagina_amazon(context)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(2500)
            await page.mouse.wheel(0, 4000)
            await page.wait_for_timeout(1500)

            titulo = await page.title()
            if await hay_captcha(page):
                print(f"🤖 Captcha en {url} (título: {titulo!r})")
                return []

            hrefs = await page.eval_on_selector_all("a", "els => els.map(e => e.href)")
            contenido = await page.content()

            asins = []
            for href in hrefs:
                m = RE_ASIN.search(href or "")
                if m and m.group(1) not in asins:
                    asins.append(m.group(1))
            for m in RE_ASIN_HTML.finditer(contenido):
                a = m.group(1).upper()
                if a not in asins:
                    asins.append(a)

            print(f"🔎 {len(asins):>3} ASINs | {url}")
            return asins
        except Exception as exc:
            print(f"⚠️ Error rastreando {url}: {exc}")
            return []
        finally:
            await page.close()

JS_EXTRAER = """() => {
  const centro = document.querySelector('#centerCol') || document.body;
  const primero = (sels) => {
    for (const s of sels) {
      const e = centro.querySelector(s);
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
      '#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen',
      '#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen',
      '#priceblock_dealprice', '#priceblock_ourprice'
    ]),
    antes: primero([
      '#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen',
      '#corePrice_feature_div .basisPrice .a-offscreen',
      '#corePriceDisplay_desktop_feature_div .a-price.a-text-price .a-offscreen',
      '#corePrice_feature_div .a-price.a-text-price .a-offscreen',
      '.a-price.a-text-price[data-a-strike="true"] .a-offscreen',
      '#listPrice'
    ]),
    badge: primero([
      '#corePriceDisplay_desktop_feature_div .savingsPercentage',
      '.savingsPercentage'
    ]),
    carrito: !!document.querySelector('#add-to-cart-button, #buy-now-button'),
    prime: !!document.querySelector(
      '#buybox i.a-icon-prime, #desktop_buybox i.a-icon-prime, #mir-layout-DELIVERY_BLOCK i.a-icon-prime'
    ),
    centro: centro.innerText ? centro.innerText.slice(0, 8000) : '',
    imagen: imgUrl
  };
}"""

RE_MINIMO_30D = re.compile(
    r"(?:más|mas)\s+bajo.{0,100}?30\s*d[ií]as.{0,40}?(\d+(?:\.\d{3})*,\d{2})\s*€",
    re.I | re.S,
)

async def verificar_producto(context, asin, sem):
    async with sem:
        espera = random.uniform(2.5, 5.5)
        await asyncio.sleep(espera)
        
        page = await nueva_pagina_amazon(context)
        try:
            await page.goto(f"https://www.amazon.es/dp/{asin}", wait_until="domcontentloaded", timeout=30000)
            if await hay_captcha(page):
                return rechazo("captcha_amazon")
            try:
                await page.wait_for_selector("#productTitle", timeout=8000)
            except Exception:
                return rechazo("producto_no_existe_o_no_carga")

            d = await page.evaluate(JS_EXTRAER)
            if not d.get("titulo"):
                return rechazo("sin_titulo")
            if not d.get("carrito"):
                return rechazo("sin_boton_comprar")

            actual = parse_precio(d.get("actual"))
            if not actual or actual <= 0:
                return rechazo("sin_precio_actual")
                
            antes = parse_precio(d.get("antes"))

            # MODO SÍ O SÍ: Si no hay precio anterior válido, publicamos igual pero marcamos como 0%
            if antes and antes > actual:
                pct = round((antes - actual) / antes * 100, 1)
                txt_desc = f"-{int(round(pct))}%"
                txt_antes = fmt_precio(antes)
            else:
                pct = 0.0
                txt_desc = "TOP"
                txt_antes = None

            if pct > MAX_DESCUENTO:
                return rechazo("descuento_sospechoso_muy_alto")

            if txt_antes:
                m_badge = re.search(r"(\d{1,2})\s*%", d.get("badge") or "")
                if m_badge and abs(int(m_badge.group(1)) - pct) > 8:
                    return rechazo("etiqueta_descuento_no_coincide")

                m30 = RE_MINIMO_30D.search(d.get("centro") or "")
                if m30:
                    minimo_30d = parse_precio(m30.group(1))
                    if minimo_30d and actual >= minimo_30d * 0.92:
                        return rechazo("no_es_minimo_30_dias")

            flag = pct >= UMBRAL_ERROR_PRECIO
            print(f"   {'🚨' if flag else '🎯'} {asin}: {pct}% ({fmt_precio(actual)} €)")
            return {
                "asin": asin,
                "titulo_completo": d["titulo"],
                "descuento": txt_desc,
                "descuento_porcentaje": pct,
                "posible_error_precio": flag,
                "precio_actual": fmt_precio(actual),
                "precio_antes": txt_antes,
                "imagen_original": d.get("imagen"),
                "es_prime": bool(d.get("prime")),
            }
        except Exception as exc:
            print(f"   ⚠️ {asin}: error verificando: {exc}")
            return rechazo("excepcion_verificando")
        finally:
            await page.close()

def elegir_con_escalera(validos):
    for umbral in ESCALERA:
        cand = [c for c in validos if c["descuento_porcentaje"] >= umbral]
        if cand:
            cand.sort(key=lambda c: (c["posible_error_precio"], c["descuento_porcentaje"]), reverse=True)
            if umbral != ESCALERA[0]:
                print(f"ℹ️ Sin chollos ≥ {ESCALERA[0]:.0f}%; usando el listón de {umbral:.0f}%.")
            return cand[0]
    return None

async def buscar_chollo(context, historial):
    sem = asyncio.Semaphore(CONCURRENCIA)

    res_prio = await asyncio.gather(*(recoger_asins(context, u, sem) for u in FUENTES_PRIORITARIAS))
    ordenados = intercalar(res_prio)

    if len(ordenados) < 10:
        print(f"ℹ️ Solo {len(ordenados)} ASINs de SaveMoney; añado fuentes de respaldo de Amazon.")
        res_sec = await asyncio.gather(*(recoger_asins(context, u, sem) for u in FUENTES_SECUNDARIAS))
        ya = set(ordenados)
        ordenados += [a for a in intercalar(res_sec) if a not in ya]

    pendientes = [a for a in ordenados if a not in historial][:MAX_REVISAR]
    print(f"📦 {len(ordenados)} ASINs recogidos, {len(pendientes)} nuevos (no publicados antes).")

    if not pendientes:
        raise SinChollo("No hay productos nuevos que revisar.")

    print(f"🧪 Verificando en Amazon {len(pendientes)} productos...")
    validos = []
    captchas_consecutivos = 0

    for i in range(0, len(pendientes), CONCURRENCIA):
        lote = pendientes[i:i + CONCURRENCIA]
        res = await asyncio.gather(*(verificar_producto(context, a, sem) for a in lote))
        
        for r in res:
            if r is None and MOTIVOS["captcha_amazon"] > captchas_consecutivos:
                captchas_consecutivos = MOTIVOS["captcha_amazon"]
            else:
                captchas_consecutivos = 0

        if captchas_consecutivos >= 4:
            print("::warning::Amazon ha puesto CAPTCHA 4 veces seguidas. Abortando revisión.")
            break

        validos += [r for r in res if r]
        buenos = [r for r in validos if r["descuento_porcentaje"] >= ESCALERA[0]]
        if len(buenos) >= 3 or any(r["posible_error_precio"] for r in validos):
            break

    print(f"📊 Motivos de descarte: {dict(MOTIVOS) if MOTIVOS else 'ninguno'}")

    elegido = elegir_con_escalera(validos)
    if not elegido:
        raise SinChollo(f"Ningún producto revisado tiene stock.")
    return elegido

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

def crear_imagen_chollo(descuento, precio_actual, precio_antes, ruta_salida, es_prime=False, posible_error=False):
    W, H = 800, 440
    img = Image.new("RGB", (W, H), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    f_gigante = cargar_fuente(115, negrita=False)
    f_super = cargar_fuente(52, negrita=True)
    f_gris = cargar_fuente(36, negrita=False)
    f_prime = cargar_fuente(52, negrita=True)
    f_alerta = cargar_fuente(36, negrita=True)

    draw.text((45, 50), descuento, font=f_gigante, fill=(204, 12, 57))
    bbox_desc = draw.textbbox((45, 50), descuento, font=f_gigante)
    x_precio = bbox_desc[2] + 40

    partes = precio_actual.replace(".", ",").split(",")
    euros = partes[0]
    centimos = partes[1] if len(partes) > 1 else "00"

    draw.text((x_precio, 50), euros, font=f_gigante, fill=(15, 17, 17))
    bbox_eur = draw.textbbox((x_precio, 50), euros, font=f_gigante)
    draw.text((bbox_eur[2] + 6, 58), f"{centimos} €", font=f_super, fill=(15, 17, 17))

    if precio_antes:
        texto_antes = f"Precio recomendado: {precio_antes}€"
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
    if posible_error:
        draw.text((160, 335), "¡POSIBLE ERROR DE PRECIO!", font=f_alerta, fill=(204, 12, 57))

    draw.rectangle([(2, 2), (W - 3, H - 3)], outline=(225, 228, 228), width=3)
    img.save(ruta_salida, quality=95)
    return ruta_salida

def publicar_en_telegram(imagen_url, imagen_local, texto_mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("::warning::Faltan TELEGRAM_TOKEN / TELEGRAM_CHAT_ID.")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        print("📲 Enviando a Telegram...")
        if imagen_url:
            try:
                r = requests.post(
                    url,
                    data={"chat_id": TELEGRAM_CHAT_ID, "photo": imagen_url,
                          "caption": texto_mensaje, "parse_mode": "HTML"},
                    timeout=20,
                )
                if r.status_code == 200:
                    print("✅ Telegram: publicado con la imagen original.")
                    return True
            except requests.RequestException as exc:
                print(f"⚠️ Telegram (imagen original): {exc}")

        with open(imagen_local, "rb") as foto:
            r = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": texto_mensaje, "parse_mode": "HTML"},
                files={"photo": foto},
                timeout=20,
            )
        if r.status_code == 200:
            print("✅ Telegram: publicado con la imagen generada.")
            return True
        return False
    except Exception as exc:
        print(f"::warning::Error con Telegram: {exc}")
        return False

async def publicar_en_x(context, texto_x, ruta_img):
    page = await context.new_page()
    try:
        print("🌐 Abriendo X con la sesión guardada...")
        await page.goto("https://x.com/compose/post", timeout=60000)

        if "login" in page.url or "i/flow" in page.url:
            print("::error::La sesión de X ha caducado. Genera un X_STATE nuevo.")
            return False

        for texto in ["Aceptar todas las cookies", "Accept all cookies",
                      "Rechazar cookies no esenciales", "Refuse non-essential cookies"]:
            try:
                await page.get_by_role("button", name=texto).click(timeout=2000)
                await page.wait_for_timeout(1000)
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

        try:
            await editor.wait_for(state="hidden", timeout=8000)
            print("✅ X: publicado (el compositor se cerró).")
        except Exception:
            print("::warning::X: no pude confirmar que el compositor se cerrara; revisa el perfil.")
        return True

    except Exception as exc:
        print(f"::warning::Error publicando en X: {exc}")
        return False
    finally:
        await page.close()

async def ejecutar_bot():
    tg_cfg = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
    estado_x = None
    if X_STATE:
        try:
            estado_x = json.loads(X_STATE)
        except json.JSONDecodeError:
            print("::error::X_STATE no es un JSON válido.")
    x_cfg = estado_x is not None

    print(f"⚙️ Canales: Telegram={'sí' if tg_cfg else 'NO'} | X={'sí' if x_cfg else 'NO'}")
    if not tg_cfg and not x_cfg:
        print("::error::No hay ningún canal configurado (faltan los secrets).")
        return False

    historial = cargar_historial()
    print(f"🗂️ Historial: {len(historial)} productos ya publicados.")

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
                print(f"::warning::NADA PUBLICADO en este pase: {exc}")
                return True

            asin = chollo["asin"]
            enlace = generar_enlace_afiliado(asin)
            flag = chollo["posible_error_precio"]
            titulo = chollo["titulo_completo"]

            txt_precio_x = f"💰 Ahora: {chollo['precio_actual']}€"
            txt_precio_tg = f"💰 <b>Precio:</b> {chollo['precio_actual']}€"
            
            if chollo["precio_antes"]:
                txt_precio_x += f" (Antes: {chollo['precio_antes']}€)"
                txt_precio_tg += f" (PVP: <s>{chollo['precio_antes']}€</s>)"

            if flag:
                cabecera = f"🚨 POSIBLE ERROR DE PRECIO {chollo['descuento']} 🚨"
            elif "%" in chollo['descuento']:
                cabecera = f"🔥 {chollo['descuento']} de descuento 🔥"
            else:
                cabecera = "⚡ ¡OFERTA DESTACADA! ⚡"

            titulo_x = titulo[:70].rstrip() + ("…" if len(titulo) > 70 else "")
            texto_x = (
                f"{cabecera}\n"
                f"📦 {titulo_x}\n"
                f"{txt_precio_x}\n"
                f"🛒 {enlace}\n\n"
                f"#chollos #amazon #ofertas\n"
                f"(Enlace de afiliado)"
            )

            aviso = ("\n⚠️ <i>Puede corregirse o cancelarse en cualquier momento.</i>\n" if flag else "")
            texto_telegram = (
                f"{cabecera}\n\n"
                f"📦 <b>Producto:</b> {html.escape(titulo[:180])}\n\n"
                f"{txt_precio_tg}\n"
                f"{aviso}\n"
                f"🛒 <b>Comprar aquí:</b> {html.escape(enlace)}\n\n"
                f"<i>Como afiliado de Amazon, obtengo ingresos por las compras adscritas que cumplen los requisitos aplicables.</i>"
            )

            ruta_img = crear_imagen_chollo(
                chollo["descuento"], chollo["precio_actual"], chollo["precio_antes"],
                "chollo.jpg", es_prime=chollo["es_prime"], posible_error=flag,
            )

            resultados = {}
            if tg_cfg:
                resultados["Telegram"] = publicar_en_telegram(chollo["imagen_original"], ruta_img, texto_telegram)
            if x_cfg:
                resultados["X"] = await publicar_en_x(context, texto_x, ruta_img)

            print(f"📬 Resultado: {resultados}")
            if any(resultados.values()):
                guardar_en_historial(asin)

            return all(resultados.values())
        except Exception:
            print("::error::Fallo inesperado en el bot:")
            traceback.print_exc()
            return False
        finally:
            await browser.close()

if __name__ == "__main__":
    ok = asyncio.run(ejecutar_bot())
    sys.exit(0 if ok else 1)
