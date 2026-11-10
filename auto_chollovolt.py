import asyncio
import html
import json
import os
import random
import re
import sys
import traceback
from collections import Counter
from datetime import datetime
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


def _env_bool(nombre, defecto=False):
    valor = (os.getenv(nombre) or "").strip().lower()
    if not valor:
        return defecto
    return valor in ("1", "true", "si", "sí", "yes")


AMAZON_TAG = os.getenv("AMAZON_TAG") or "librospsico08-21"
X_STATE = os.getenv("X_STATE", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MIN_DESCUENTO = _env_float("MIN_DESCUENTO", 15)
UMBRAL_ERROR_PRECIO = _env_float("UMBRAL_ERROR_PRECIO", 50)
MAX_DESCUENTO = _env_float("MAX_DESCUENTO", 95)
MAX_REVISAR = int(_env_float("MAX_REVISAR", 80))
CONCURRENCIA = int(_env_float("CONCURRENCIA", 4))

# Hasta qué descuento está dispuesto a bajar el bot si no encuentra el mínimo.
SUELO_DESCUENTO = _env_float("SUELO_DESCUENTO", 10)
# Publicar productos SIN descuento verificable ("producto destacado"). Por defecto NO.
PUBLICAR_SIN_DESCUENTO = _env_bool("PUBLICAR_SIN_DESCUENTO", False)
# Cuántos candidatos se reconfirman (segunda lectura) antes de publicar.
MAX_CONFIRMAR = 3

# ESCALERA: [15, 10] por defecto. Los escalones 5% y 0% solo si el suelo lo permite.
ESCALERA = [MIN_DESCUENTO] + [u for u in (10.0, 5.0) if SUELO_DESCUENTO <= u < MIN_DESCUENTO]
if PUBLICAR_SIN_DESCUENTO:
    ESCALERA.append(0.0)

ARCHIVO_HISTORIAL = "historial_publicados.txt"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

FUENTES_PRIORITARIAS = [
    "https://www.amazon.es/gp/goldbox",
    "https://www.amazon.es/gp/movers-and-shakers/electronics",
    "https://www.amazon.es/gp/movers-and-shakers/computers",
    "https://www.amazon.es/gp/movers-and-shakers/videogames",
    "https://www.amazon.es/gp/movers-and-shakers/kitchen",
    "https://www.amazon.es/gp/movers-and-shakers/toys",
    "https://www.amazon.es/gp/movers-and-shakers/diy",
    "https://www.amazon.es/s?i=electronics&rh=p_8%3A50-99&s=date-desc-rank",
    "https://www.amazon.es/s?i=computers&rh=p_8%3A50-99&s=date-desc-rank",
    "https://www.amazon.es/s?i=videogames&rh=p_8%3A50-99&s=date-desc-rank",
]
FUENTES_SECUNDARIAS = [
    "https://www.amazon.es/gp/bestsellers/electronics",
    "https://www.amazon.es/gp/bestsellers/kitchen",
    "https://www.amazon.es/gp/bestsellers/computers",
]

BLOQUEAR_DOMINIOS = ("amazon-adsystem", "fls-eu.amazon", "google-analytics", "doubleclick")
RE_ASIN = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})")
RE_PRECIO = re.compile(r"\d+(?:\.\d{3})*,\d{2}")
# Etiquetas que delatan un precio SIN IVA (vista "Amazon Business" / empresas)
RE_SIN_IVA = re.compile(r"sin\s+iva|iva\s+excl|excl\w*\.?\s+(?:de\s+)?iva|iva\s+no\s+incluido", re.I)
IVA = 1.21  # IVA general en España

MOTIVOS = Counter()  # por qué se descarta cada producto (diagnóstico)


class SinChollo(Exception):
    pass


def rechazo(motivo):
    MOTIVOS[motivo] += 1
    return None


# ==============================================================================
# HISTORIAL
# ==============================================================================
def cargar_historial():
    if not os.path.exists(ARCHIVO_HISTORIAL):
        return set()
    with open(ARCHIVO_HISTORIAL, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def guardar_en_historial(asin):
    with open(ARCHIVO_HISTORIAL, "a", encoding="utf-8") as f:
        f.write(f"{asin}\n")


# ==============================================================================
# UTILIDADES
# ==============================================================================
def generar_enlace_afiliado(asin):
    return f"https://www.amazon.es/dp/{asin}?tag={AMAZON_TAG}"


def parse_precio(texto):
    if not texto:
        return None
    t = str(texto).replace("\xa0", " ")
    m = re.search(r"\d+(?:\.\d{3})*,\d{2}", t)
    if m:
        return float(m.group().replace(".", "").replace(",", "."))
    m = re.search(r"\d+\.\d{2}\b", t)
    return float(m.group()) if m else None


def fmt_precio(valor):
    return f"{valor:.2f}".replace(".", ",")


def sello_hora():
    """Hora de España en la que se leyó el precio, p. ej. '14:32 del 10/11'."""
    try:
        from zoneinfo import ZoneInfo
        ahora = datetime.now(ZoneInfo("Europe/Madrid"))
    except Exception:
        ahora = datetime.now()
    return ahora.strftime("%H:%M del %d/%m")


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


# ==============================================================================
# BÚSQUEDA DE CHOLLOS
# ==============================================================================
async def recoger_asins(context, url, sem):
    async with sem:
        page = await nueva_pagina_amazon(context)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if await hay_captcha(page):
                print(f"⚠️ CAPTCHA en {url}; la salto.")
                return []
            try:
                if await page.locator("#sp-cc-accept").count():
                    await page.locator("#sp-cc-accept").click(timeout=1500)
            except Exception:
                pass
            await page.mouse.wheel(0, 2500)
            await page.wait_for_timeout(700)
            hrefs = await page.eval_on_selector_all(
                "a[href*='/dp/'], a[href*='/gp/product/']", "els => els.map(e => e.href)"
            )
            asins = []
            for href in hrefs:
                m = RE_ASIN.search(href or "")
                if m and m.group(1) not in asins:
                    asins.append(m.group(1))
            print(f"🔎 {len(asins):>3} productos en {url}")
            return asins
        except Exception as exc:
            print(f"⚠️ Aviso rastreando {url}: {exc}")
            return []
        finally:
            await page.close()


# Todas las lecturas de precio se limitan al BLOQUE DE PRECIO de la ficha
# (nada de buscar en toda la página: podría coger un patrocinado o una variante).
JS_EXTRAER = """() => {
  const txt = (e) => (e && e.textContent ? e.textContent.trim() : '');
  const primero = (sels, raiz) => {
    for (const s of sels) {
      const e = (raiz || document).querySelector(s);
      if (e && txt(e)) return txt(e);
    }
    return null;
  };
  const img = document.querySelector('#landingImage, #imgBlkFront');
  let imgUrl = null;
  if (img) {
    imgUrl = img.getAttribute('data-old-hires') || img.getAttribute('src');
    if (imgUrl && imgUrl.startsWith('data:')) imgUrl = null;
  }
  const centro = document.querySelector('#centerCol');
  const bloque = document.querySelector('#corePriceDisplay_desktop_feature_div')
              || document.querySelector('#corePrice_feature_div')
              || document.querySelector('#apex_desktop');
  const buybox = document.querySelector('#desktop_buybox') || document.querySelector('#buybox');

  let actual = null, antes = null, badge = null, preciosBloque = [];
  if (bloque) {
    actual = primero(['.priceToPay .a-offscreen',
                      '.a-price:not(.a-text-price) .a-offscreen'], bloque);
    antes = primero(['.basisPrice .a-offscreen',
                     '.a-price.a-text-price[data-a-strike="true"] .a-offscreen',
                     '.a-price.a-text-price .a-offscreen'], bloque);
    badge = primero(['.savingsPercentage'], bloque);
    preciosBloque = [...bloque.querySelectorAll('.a-offscreen')].map(txt);
  } else {
    actual = primero(['#priceblock_dealprice', '#priceblock_ourprice']);
    antes = primero(['#listPrice']);
  }

  const inputAsin = document.querySelector('#ASIN, input[name="ASIN"]');
  const inputPrecio = document.querySelector('input[name$="[customerVisiblePrice][amount]"]');

  return {
    titulo: primero(['#productTitle']),
    actual: actual,
    antes: antes,
    badge: badge,
    precios_bloque: preciosBloque,
    bloque_texto: bloque ? bloque.innerText.slice(0, 2000) : '',
    buybox_texto: buybox ? buybox.innerText.slice(0, 3000) : '',
    precio_form: inputPrecio ? inputPrecio.value : null,
    asin_pagina: inputAsin ? inputAsin.value : null,
    carrito: !!document.querySelector('#add-to-cart-button, #buy-now-button'),
    prime: !!document.querySelector(
      '#buybox i.a-icon-prime, #desktop_buybox i.a-icon-prime, #mir-layout-DELIVERY_BLOCK i.a-icon-prime'
    ),
    centro: centro ? centro.innerText.slice(0, 8000) : '',
    imagen: imgUrl
  };
}"""

RE_MINIMO_30D = re.compile(
    r"(?:más|mas)\s+bajo.{0,100}?30\s*d[ií]as.{0,40}?(\d+(?:\.\d{3})*,\d{2})\s*€",
    re.I | re.S,
)


def _es_precio_sin_iva(actual, otros):
    """True si en la ficha aparece otro precio igual a actual * 1,21 (±2 céntimos).

    Es la firma de leer el precio SIN IVA: p. ej. 114,00 € / 1,21 = 94,21 €.
    """
    esperado = actual * IVA
    return any(abs(esperado - p) <= 0.02 for p in otros if p)


async def verificar_producto(context, asin, sem):
    async with sem:
        # Micro-pausa para evitar los CAPTCHAs que saltan al entrar a Amazon
        await asyncio.sleep(random.uniform(1.0, 2.5))
        page = await nueva_pagina_amazon(context)
        try:
            await page.goto(f"https://www.amazon.es/dp/{asin}", wait_until="domcontentloaded", timeout=30000)
            if await hay_captcha(page):
                return rechazo("captcha_amazon")
            try:
                await page.wait_for_selector("#productTitle", timeout=8000)
            except Exception:
                return rechazo("producto_no_carga")

            d = await page.evaluate(JS_EXTRAER)
            if not d.get("titulo"):
                return rechazo("sin_titulo")
            if not d.get("carrito"):
                return rechazo("sin_boton_comprar")

            # La ficha debe ser exactamente el ASIN pedido (si no, es otra variante
            # y el precio leído podría no ser el del enlace que publicamos).
            asin_pagina = (d.get("asin_pagina") or "").strip().upper()
            if asin_pagina and asin_pagina != asin:
                return rechazo("variante_distinta")

            actual = parse_precio(d.get("actual"))
            if not actual or actual <= 0:
                return rechazo("sin_precio_actual")

            # --- FIDELIDAD DEL PRECIO -------------------------------------------
            # 1) Etiquetas explícitas de "sin IVA" (vista de empresas)
            zona_iva = f"{d.get('bloque_texto') or ''} {d.get('buybox_texto') or ''}"
            if RE_SIN_IVA.search(zona_iva):
                return rechazo("precio_sin_iva")

            # 2) El precio del formulario de compra debe coincidir con el mostrado
            precio_form = parse_precio(d.get("precio_form"))
            if precio_form and abs(precio_form - actual) > 0.02:
                return rechazo("precio_incoherente")

            antes = parse_precio(d.get("antes"))

            # 3) Firma matemática de precio sin IVA: otro precio de la ficha = actual * 1,21
            otros = [antes, precio_form]
            otros += [parse_precio(t) for t in (d.get("precios_bloque") or [])]
            otros += [parse_precio(m) for m in RE_PRECIO.findall(d.get("centro") or "")]
            if _es_precio_sin_iva(actual, otros):
                return rechazo("posible_precio_sin_iva")
            # ---------------------------------------------------------------------

            if antes and antes > actual:
                pct = round((antes - actual) / antes * 100, 1)
                txt_antes = fmt_precio(antes)
                txt_desc = f"-{int(round(pct))}%"
            else:
                pct = 0.0
                txt_antes = None
                txt_desc = None  # sin descuento verificable: NO se inventa ninguna etiqueta

            if pct > MAX_DESCUENTO:
                print(f"   🗑️  {asin}: {pct}% descartado (lectura sospechosa)")
                return rechazo("descuento_sospechoso")

            if txt_antes:
                m_badge = re.search(r"(\d{1,2})\s*%", d.get("badge") or "")
                if m_badge and abs(int(m_badge.group(1)) - pct) > 8:
                    return rechazo("etiqueta_no_coincide")

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
                "precio_actual_num": actual,
                "precio_antes": txt_antes,
                "imagen_original": d.get("imagen"),
                "es_prime": bool(d.get("prime")),
            }
        except Exception as exc:
            print(f"   ⚠️ {asin}: error verificando: {exc}")
            return rechazo("excepcion")
        finally:
            await page.close()


async def buscar_chollo(context, historial):
    """Devuelve una lista ORDENADA de candidatos (el mejor primero)."""
    sem = asyncio.Semaphore(CONCURRENCIA)
    todas = FUENTES_PRIORITARIAS + FUENTES_SECUNDARIAS
    resultados = await asyncio.gather(*(recoger_asins(context, u, sem) for u in todas))
    n_prio = len(FUENTES_PRIORITARIAS)

    ordenados = intercalar(resultados[:n_prio])
    ya = set(ordenados)
    ordenados += [a for a in intercalar(resultados[n_prio:]) if a not in ya]
    pendientes = [a for a in ordenados if a not in historial][:MAX_REVISAR]
    print(f"📦 {len(ordenados)} productos recogidos, {len(pendientes)} nuevos.")

    if not pendientes:
        raise SinChollo("No se han recopilado productos nuevos de las fuentes consultadas.")

    print(f"🧪 Verificando {len(pendientes)} productos ({CONCURRENCIA} en paralelo)...")
    validos = []

    for i in range(0, len(pendientes), CONCURRENCIA):
        lote = pendientes[i:i + CONCURRENCIA]
        res = await asyncio.gather(*(verificar_producto(context, a, sem) for a in lote))
        validos += [r for r in res if r]

        # Parar cuando ya hay un par de candidatos que cumplen la exigencia inicial
        # (así queda alguno de reserva por si falla la reconfirmación).
        buenos = [r for r in validos if r["descuento_porcentaje"] >= ESCALERA[0]]
        if len(buenos) >= 2:
            break

    print(f"📊 Motivos de descarte: {dict(MOTIVOS) if MOTIVOS else 'ninguno'}")

    if not validos:
        raise SinChollo("Ningún producto revisado se pudo verificar (ver motivos de descarte arriba).")

    ranking, vistos = [], set()
    for umbral in ESCALERA:
        cand = [c for c in validos if c["descuento_porcentaje"] >= umbral and c["asin"] not in vistos]
        cand.sort(key=lambda c: (c["posible_error_precio"], c["descuento_porcentaje"]), reverse=True)
        if cand and umbral != ESCALERA[0] and not ranking:
            print(f"ℹ️ Bajando el listón a {umbral:.0f}%.")
        for c in cand:
            c["umbral"] = umbral
            ranking.append(c)
            vistos.add(c["asin"])

    if not ranking:
        raise SinChollo(
            f"Ningún producto verificado llega al descuento mínimo ({ESCALERA[-1]:.0f}%). "
            "Mejor no publicar que publicar una oferta que no lo es."
        )
    return ranking


# ==============================================================================
# IMAGEN
# ==============================================================================
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


def crear_imagen_chollo(descuento, precio_actual, precio_antes, ruta_salida,
                        es_prime=False, posible_error=False, sello=None):
    """descuento: texto tipo '-27%' o None si no hay descuento verificable."""
    W, H = 800, 440
    img = Image.new("RGB", (W, H), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    f_gigante = cargar_fuente(115, negrita=False)
    f_super = cargar_fuente(52, negrita=True)
    f_gris = cargar_fuente(36, negrita=False)
    f_prime = cargar_fuente(52, negrita=True)
    f_alerta = cargar_fuente(36, negrita=True)
    f_peq = cargar_fuente(20, negrita=False)

    x_precio = 45
    if descuento:
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
        draw.text((45, 230), "Producto destacado en Amazon", font=f_gris, fill=(86, 89, 89))

    if es_prime:
        draw.line([(48, 348), (62, 366), (92, 326)], fill=(255, 153, 0), width=9)
        draw.text((98, 315), "prime", font=f_prime, fill=(0, 113, 197))

    if posible_error:
        draw.text((160, 335), "¡POSIBLE ERROR DE PRECIO!", font=f_alerta, fill=(204, 12, 57))

    if sello:
        texto_sello = f"Precio a las {sello} · puede variar"
        bb = draw.textbbox((0, 0), texto_sello, font=f_peq)
        draw.text((W - 25 - (bb[2] - bb[0]), H - 42), texto_sello, font=f_peq, fill=(120, 123, 123))

    draw.rectangle([(2, 2), (W - 3, H - 3)], outline=(225, 228, 228), width=3)
    img.save(ruta_salida, quality=95)
    return ruta_salida


# ==============================================================================
# TELEGRAM Y X
# ==============================================================================
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
            except requests.RequestException:
                pass

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
        print(f"::warning::Telegram falló ({r.status_code}): {r.text[:300]}")
        return False
    except Exception as exc:
        print(f"::warning::Error con Telegram: {exc}")
        return False


async def publicar_en_x(context, texto_x, ruta_img):
    page = await context.new_page()
    try:
        print("🌐 Abriendo X...")
        await page.goto("https://x.com/compose/post", timeout=60000)

        if "login" in page.url or "i/flow" in page.url:
            print("::error::La sesión de X ha caducado. Genera un X_STATE nuevo.")
            return False

        for texto in ["Aceptar todas las cookies", "Accept all cookies", "Rechazar cookies no esenciales"]:
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
            print("✅ X: publicado.")
        except Exception:
            print("::warning::X: no pude confirmar que el compositor se cerrara; revisa el perfil.")
        return True

    except Exception as exc:
        print(f"::warning::Error publicando en X: {exc}")
        try:
            await page.screenshot(path="error.png", full_page=True)
        except Exception:
            pass
        return False
    finally:
        await page.close()


# ==============================================================================
# EJECUCIÓN PRINCIPAL
# ==============================================================================
async def reconfirmar(ctx_amazon, ranking):
    """Segunda lectura independiente del precio justo antes de publicar.

    Solo se publica un producto si, al volver a cargar su ficha, el precio sigue
    siendo el mismo y el descuento sigue cumpliendo el umbral con el que se eligió.
    """
    sem = asyncio.Semaphore(1)
    for cand in ranking[:MAX_CONFIRMAR]:
        print(f"🔁 Reconfirmando {cand['asin']} antes de publicar...")
        await asyncio.sleep(random.uniform(3.0, 6.0))
        nuevo = await verificar_producto(ctx_amazon, cand["asin"], sem)
        if not nuevo:
            print("   ❌ La segunda lectura no supera las comprobaciones; paso al siguiente.")
            continue
        if abs(nuevo["precio_actual_num"] - cand["precio_actual_num"]) > 0.01:
            print(
                f"   ❌ El precio ha cambiado ({cand['precio_actual']} → {nuevo['precio_actual']}); "
                "paso al siguiente."
            )
            continue
        if nuevo["descuento_porcentaje"] < cand["umbral"]:
            print("   ❌ El descuento ya no cumple el umbral; paso al siguiente.")
            continue
        return nuevo
    return None


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
    print(f"⚙️ Escalera de descuentos: {ESCALERA} | publicar sin descuento: {'sí' if PUBLICAR_SIN_DESCUENTO else 'NO'}")
    if not tg_cfg and not x_cfg:
        print("::error::No hay ningún canal configurado (faltan los secrets).")
        return False

    historial = cargar_historial()
    print(f"🗂️ Historial: {len(historial)} productos ya publicados.")

    opciones_ctx = dict(
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 800},
        locale="es-ES",
        timezone_id="Europe/Madrid",
        extra_http_headers={"Accept-Language": "es-ES,es;q=0.9"},
    )

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            # Contexto de Amazon LIMPIO (sin las cookies de X): navegación de visitante normal.
            ctx_amazon = await browser.new_context(**opciones_ctx)
            ctx_x = await browser.new_context(storage_state=estado_x, **opciones_ctx) if x_cfg else None

            try:
                ranking = await buscar_chollo(ctx_amazon, historial)
            except SinChollo as exc:
                print(f"::warning::NADA PUBLICADO en este pase: {exc}")
                return True

            chollo = await reconfirmar(ctx_amazon, ranking)
            if not chollo:
                print("::warning::NADA PUBLICADO en este pase: ningún candidato superó la reconfirmación del precio.")
                return True

            asin = chollo["asin"]
            print(f"🏆 Elegido {asin} para publicar.")
            enlace = generar_enlace_afiliado(asin)
            flag = chollo["posible_error_precio"]
            titulo = chollo["titulo_completo"]
            desc = chollo["descuento"]  # '-27%' o None
            sello = sello_hora()

            txt_precio_x = f"💰 Ahora: {chollo['precio_actual']}€"
            txt_precio_tg = f"💰 <b>Precio:</b> {chollo['precio_actual']}€"

            if chollo["precio_antes"]:
                txt_precio_x += f" (Antes: {chollo['precio_antes']}€)"
                txt_precio_tg += f" (PVP: <s>{chollo['precio_antes']}€</s>)"

            if flag:
                cabecera = f"🚨 POSIBLE ERROR DE PRECIO {desc} 🚨"
            elif desc:
                cabecera = f"🔥 {desc} de descuento 🔥"
            else:
                cabecera = "⭐ PRODUCTO DESTACADO ⭐"  # solo si PUBLICAR_SIN_DESCUENTO=1

            hashtags = "#chollos #amazon #ofertas" if desc else "#amazon #recomendado"

            titulo_x = titulo[:70].rstrip() + ("…" if len(titulo) > 70 else "")
            texto_x = (
                f"{cabecera}\n"
                f"📦 {titulo_x}\n"
                f"{txt_precio_x}\n"
                f"🕒 Precio a las {sello}; puede variar\n"
                f"🛒 {enlace}\n\n"
                f"{hashtags}\n"
                f"(Enlace de afiliado)"
            )

            aviso = ("\n⚠️ <i>Puede corregirse o cancelarse en cualquier momento.</i>\n" if flag else "")
            texto_telegram = (
                f"{cabecera}\n\n"
                f"📦 <b>Producto:</b> {html.escape(titulo[:180])}\n\n"
                f"{txt_precio_tg}\n"
                f"🕒 <i>Precio a las {sello}; puede variar.</i>\n"
                f"{aviso}\n"
                f"🛒 <b>Comprar aquí:</b> {html.escape(enlace)}\n\n"
                f"<i>Como afiliado de Amazon, obtengo ingresos por las compras adscritas que cumplen los requisitos aplicables.</i>"
            )

            ruta_img = crear_imagen_chollo(
                desc, chollo["precio_actual"], chollo["precio_antes"],
                "chollo.jpg", es_prime=chollo["es_prime"], posible_error=flag, sello=sello,
            )

            resultados = {}
            if tg_cfg:
                resultados["Telegram"] = publicar_en_telegram(chollo["imagen_original"], ruta_img, texto_telegram)
            if ctx_x is not None:
                resultados["X"] = await publicar_en_x(ctx_x, texto_x, ruta_img)

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
    print("⚡ Iniciando CholloVolt (X + Telegram)...")
    ok = asyncio.run(ejecutar_bot())
    sys.exit(0 if ok else 1)
