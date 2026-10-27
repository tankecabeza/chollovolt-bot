import os
import sys
import json
import re
import random
import html
import requests
from datetime import datetime, timezone, timedelta
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


# ==============================================================================
# CONFIGURACIÓN
# ==============================================================================
def _entero_env(nombre, defecto):
    try:
        return int(os.getenv(nombre) or defecto)
    except ValueError:
        return defecto


# "or" en lugar del valor por defecto de getenv: si el secret no existe,
# GitHub pasa la variable vacía y getenv devolvería "".
AMAZON_TAG = os.getenv("AMAZON_TAG") or "librospsico08-21"
X_STATE = os.getenv("X_STATE", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

ARCHIVO_HISTORIAL = Path("historial_estados.json")
ARCHIVO_HISTORIAL_LEGACY = Path("historial_publicados.txt")
TIMEOUT_NAVEGACION = 45000

# --- Criterios de oferta -------------------------------------------------------
MIN_DESCUENTO = _entero_env("MIN_DESCUENTO", 25)               # % mínimo para publicar
MAX_DESCUENTO = _entero_env("MAX_DESCUENTO", 95)               # por encima suele ser un fallo de lectura
UMBRAL_ERROR_PRECIO = _entero_env("UMBRAL_ERROR_PRECIO", 60)   # desde aquí se etiqueta "posible error de precio"

# --- Límites de rastreo (menos páginas = menos riesgo de bloqueo) ---------------
FICHAS_POR_CATEGORIA = 12
FICHAS_POR_EJECUCION = 40
MAX_BLOQUEOS = 3

# --- Memoria del bot -----------------------------------------------------------
HORAS_REINTENTO = 6      # un ASIN a medias solo se reintenta durante este tiempo
HORAS_DESCARTE = 24      # un ASIN revisado y sin oferta no se vuelve a abrir en este tiempo

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

AVISO_AFILIADO_TG = (
    "Como afiliado de Amazon, obtengo ingresos por las compras adscritas "
    "que cumplen los requisitos aplicables."
)
AVISO_AFILIADO_X = "Como afiliado de Amazon, obtengo ingresos por compras que cumplen los requisitos."

# Los rankings "Más vendidos" casi nunca contienen errores de precio; las ofertas del día
# y los "Más ascendidos" son las fuentes con más probabilidad de traerlos.
URLS_RASTREO = [
    "https://www.amazon.es/gp/goldbox",
    "https://www.amazon.es/gp/movers-and-shakers/electronics",
    "https://www.amazon.es/gp/movers-and-shakers/kitchen",
    "https://www.amazon.es/gp/movers-and-shakers/computers",
    "https://www.amazon.es/gp/movers-and-shakers/videogames",
    "https://www.amazon.es/gp/movers-and-shakers/toys",
    "https://www.amazon.es/gp/bestsellers/electronics",
    "https://www.amazon.es/gp/bestsellers/kitchen",
    "https://www.amazon.es/gp/bestsellers/computers",
    "https://www.amazon.es/gp/bestsellers/videogames",
]

# Se prueban en orden y se usa el primero que devuelva un precio válido.
# Se evita a propósito un selector genérico ".a-price .a-offscreen": podría
# coger precios de carruseles, de productos relacionados o por unidad.
SELECTORES_PRECIO_ACTUAL = [
    "#corePriceDisplay_desktop_feature_div .priceToPay .a-offscreen",
    "#corePrice_feature_div .priceToPay .a-offscreen",
    ".apex-pricetopay-value .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-price:not(.a-text-price) .a-offscreen",
    "#corePrice_feature_div .a-price:not(.a-text-price) .a-offscreen",
]
SELECTORES_PRECIO_ANTES = [
    "#corePriceDisplay_desktop_feature_div .basisPrice .a-offscreen",
    "#corePrice_feature_div .basisPrice .a-offscreen",
    "#corePriceDisplay_desktop_feature_div .a-text-price .a-offscreen",
    "#corePrice_feature_div .a-text-price .a-offscreen",
]


class SinChollo(Exception):
    """No se encontró un producto nuevo o pendiente con descuento válido."""


# ==============================================================================
# UTILIDADES DE FECHA E HISTORIAL
# ==============================================================================
FORMATO_FECHA = "%Y-%m-%dT%H:%M:%SZ"


def ahora_iso():
    return datetime.now(timezone.utc).strftime(FORMATO_FECHA)


def parsear_iso(texto):
    try:
        return datetime.strptime(texto, FORMATO_FECHA).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def cargar_historial():
    """Carga estados por plataforma y migra ASIN del historial antiguo como ya publicados."""
    datos = {}
    if ARCHIVO_HISTORIAL.exists():
        try:
            contenido = json.loads(ARCHIVO_HISTORIAL.read_text(encoding="utf-8"))
            if isinstance(contenido, dict):
                datos = contenido
        except (json.JSONDecodeError, OSError) as exc:
            print(f"⚠️ No se pudo leer {ARCHIVO_HISTORIAL}: {exc}")

    # Compatibilidad: los ASIN del fichero antiguo se consideran procesados en ambos canales.
    if ARCHIVO_HISTORIAL_LEGACY.exists():
        try:
            for linea in ARCHIVO_HISTORIAL_LEGACY.read_text(encoding="utf-8").splitlines():
                asin = linea.strip()
                if re.fullmatch(r"[A-Z0-9]{10}", asin) and asin not in datos:
                    datos[asin] = {"telegram_ok": True, "x_ok": True, "legacy": True}
        except OSError as exc:
            print(f"⚠️ No se pudo leer el historial antiguo: {exc}")

    # Sección especial: ASIN revisados recientemente que no cumplían los criterios.
    descartados = datos.get("_descartados")
    if not isinstance(descartados, dict):
        descartados = {}
    limite = datetime.now(timezone.utc) - timedelta(hours=HORAS_DESCARTE)
    datos["_descartados"] = {
        asin: fecha for asin, fecha in descartados.items()
        if (parsear_iso(fecha) or limite) > limite
    }
    return datos


def guardar_historial(historial):
    """Escritura atómica para reducir el riesgo de corromper el JSON."""
    temporal = ARCHIVO_HISTORIAL.with_suffix(".json.tmp")
    temporal.write_text(
        json.dumps(historial, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8"
    )
    temporal.replace(ARCHIVO_HISTORIAL)


def estado_asin(historial, asin):
    estado = historial.get(asin, {})
    return {
        "telegram_ok": bool(estado.get("telegram_ok", False)),
        "x_ok": bool(estado.get("x_ok", False)),
    }


def pendiente_vigente(estado):
    """
    Un ASIN con un canal pendiente solo se reintenta durante HORAS_REINTENTO desde su
    última publicación (o desde que se creó). Así un canal caído (p. ej. la sesión de X)
    no hace que el bot se quede reintentando productos viejos en lugar de buscar nuevos.
    """
    referencias = [
        parsear_iso(estado.get(clave))
        for clave in ("telegram_fecha_utc", "x_fecha_utc", "creado_utc")
        if estado.get(clave)
    ]
    referencias = [r for r in referencias if r]
    if not referencias:
        return True  # ASIN nuevo o migrado sin fechas
    return datetime.now(timezone.utc) - max(referencias) < timedelta(hours=HORAS_REINTENTO)


def generar_enlace_afiliado(asin):
    return f"https://www.amazon.es/dp/{asin}?tag={AMAZON_TAG}"


# ==============================================================================
# PRECIOS Y DESCUENTOS
# ==============================================================================
def limpiar_precio(texto):
    if not texto:
        return None
    limpio = texto.replace("\xa0", " ").strip()
    # El precio suele aparecer como 1.234,56 € o 123,45 €.
    m = re.search(r"(\d{1,3}(?:\.\d{3})*[.,]\d{2}|\d+[.,]\d{2})", limpio)
    if not m:
        return None
    valor = m.group(1)
    # Normaliza separadores de miles y decimal a coma.
    if "," in valor:
        return valor.replace(".", "")
    return valor.replace(".", ",")


def precio_float(texto):
    if not texto:
        return None
    try:
        return float(texto.replace(".", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None


def porcentaje_desde_texto(texto):
    if not texto:
        return None
    m = re.search(r"(\d{1,3})\s*%", texto)
    return int(m.group(1)) if m else None


def leer_precio(page, selectores):
    for selector in selectores:
        try:
            loc = page.locator(selector).first
            if loc.count() == 0:
                continue
            precio = limpiar_precio(loc.text_content(timeout=2000))
            if precio:
                return precio
        except Exception:
            continue
    return None


# ==============================================================================
# AMAZON
# ==============================================================================
def detectar_bloqueo_amazon(page):
    try:
        # La señal más fiable del captcha de Amazon es su formulario.
        if page.locator("form[action*='validateCaptcha']").count() > 0:
            return True
        titulo = page.title().lower()
        if "robot check" in titulo or "lo sentimos" in titulo or "sorry" in titulo:
            return True
        contenido = page.content().lower()
        return any(
            frase in contenido
            for frase in ("introduce los caracteres", "escribe los caracteres", "enter the characters you see")
        )
    except Exception:
        return False


def extraer_oferta(page, asin):
    """
    Lee la ficha abierta y devuelve la oferta si cumple los criterios; si no, None.
    El porcentaje se calcula a partir de los precios y se contrasta con la etiqueta
    de descuento que muestra Amazon: si no coinciden, se descarta por poco fiable.
    """
    loc_titulo = page.locator("#productTitle")
    if loc_titulo.count() == 0:
        return None
    titulo = re.sub(r"\s+", " ", (loc_titulo.first.text_content() or "")).strip()
    if not titulo:
        return None

    precio_actual = leer_precio(page, SELECTORES_PRECIO_ACTUAL)
    precio_antes = leer_precio(page, SELECTORES_PRECIO_ANTES)
    actual_num = precio_float(precio_actual)
    antes_num = precio_float(precio_antes)
    if not actual_num or not antes_num or actual_num <= 0 or antes_num <= actual_num:
        return None

    porcentaje = round((antes_num - actual_num) / antes_num * 100)
    if porcentaje < MIN_DESCUENTO or porcentaje > MAX_DESCUENTO:
        return None

    # Exigimos que Amazon muestre su propia etiqueta de descuento y que sea coherente.
    etiqueta = None
    loc_desc = page.locator(".savingsPercentage").first
    if loc_desc.count():
        try:
            if loc_desc.is_visible():
                etiqueta = loc_desc.inner_text().strip()
        except Exception:
            pass
    porcentaje_etiqueta = porcentaje_desde_texto(etiqueta)
    if porcentaje_etiqueta is None or abs(porcentaje_etiqueta - porcentaje) > 3:
        return None

    img_url = None
    loc_img = page.locator("#landingImage, #imgBlkFront").first
    if loc_img.count():
        img_url = loc_img.get_attribute("data-old-hires") or loc_img.get_attribute("src")
        if img_url and not img_url.startswith("http"):
            img_url = None  # descarta placeholders tipo data:image

    return {
        "asin": asin,
        "titulo_completo": titulo,
        "porcentaje": porcentaje,
        "descuento": f"-{porcentaje}%",
        "precio_actual": precio_actual,
        "precio_antes": precio_antes,
        "imagen_original": img_url,
    }


def buscar_chollo_real_amazon(page, historial, telegram_disponible, x_disponible):
    urls = URLS_RASTREO[:]
    random.shuffle(urls)
    descartados = historial.setdefault("_descartados", {})

    fichas_totales = 0
    bloqueos = 0
    candidatos = []

    for url in urls:
        if fichas_totales >= FICHAS_POR_EJECUCION or bloqueos >= MAX_BLOQUEOS:
            break

        print(f"🔎 Buscando ofertas activas en: {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_NAVEGACION)
            page.wait_for_timeout(2200)

            if detectar_bloqueo_amazon(page):
                bloqueos += 1
                print("⚠️ Amazon podría estar mostrando un CAPTCHA; paso a otra categoría.")
                continue

            try:
                boton_cookies = page.locator("#sp-cc-accept")
                if boton_cookies.is_visible(timeout=1200):
                    boton_cookies.click(timeout=1500)
            except Exception:
                pass

            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(1000)

            enlaces = page.locator("a[href*='/dp/']").evaluate_all(
                """els => els.map(a => a.href).filter(Boolean)"""
            )
        except Exception as exc:
            print(f"⚠️ Aviso rastreando {url}: {exc}")
            continue

        asins_vistos = []
        for href in enlaces:
            match = re.search(r"/dp/([A-Z0-9]{10})", href)
            if match and match.group(1) not in asins_vistos:
                asins_vistos.append(match.group(1))
        print(f"   → {len(asins_vistos)} ASIN distintos encontrados")

        fichas_categoria = 0
        for asin in asins_vistos:
            if fichas_categoria >= FICHAS_POR_CATEGORIA or fichas_totales >= FICHAS_POR_EJECUCION:
                break
            if bloqueos >= MAX_BLOQUEOS:
                break

            estado = estado_asin(historial, asin)
            # Solo exigir los destinos configurados en esta ejecución.
            telegram_resuelto = (not telegram_disponible) or estado["telegram_ok"]
            x_resuelto = (not x_disponible) or estado["x_ok"]
            if telegram_resuelto and x_resuelto:
                continue
            if asin in historial and not pendiente_vigente(historial[asin]):
                continue  # pendiente demasiado antiguo: ya no es una oferta fresca
            if asin in descartados:
                continue  # revisado hace poco y no cumplía los criterios

            fichas_categoria += 1
            fichas_totales += 1
            try:
                page.goto(f"https://www.amazon.es/dp/{asin}", wait_until="domcontentloaded", timeout=TIMEOUT_NAVEGACION)
                page.wait_for_timeout(random.randint(1200, 2600))

                if detectar_bloqueo_amazon(page):
                    bloqueos += 1
                    print(f"⚠️ Posible bloqueo al consultar {asin}; continúo.")
                    continue

                oferta = extraer_oferta(page, asin)
            except Exception as exc:
                print(f"⚠️ No se pudo leer {asin}: {exc}")
                continue

            if oferta is None:
                descartados[asin] = ahora_iso()
                continue

            print(
                f"🎯 Oferta candidata: {asin} | {oferta['descuento']} | "
                f"Telegram pendiente={not estado['telegram_ok']} | X pendiente={not estado['x_ok']}"
            )
            candidatos.append(oferta)
            if oferta["porcentaje"] >= UMBRAL_ERROR_PRECIO:
                break  # candidata muy fuerte: no hace falta seguir mirando esta categoría

        # Con una candidata clara de este rastreo, no hace falta abrir más categorías.
        if candidatos:
            break

    if candidatos:
        return max(candidatos, key=lambda c: c["porcentaje"])

    if bloqueos:
        print(f"::warning::Amazon mostró bloqueo/CAPTCHA {bloqueos} vez/veces en esta ejecución.")
    raise SinChollo(
        f"No se encontró ningún chollo de {MIN_DESCUENTO}% o más ni productos pendientes de reintento."
    )


# ==============================================================================
# IMAGEN
# ==============================================================================
def cargar_fuente(tamano, negrita=False):
    from PIL import ImageFont
    rutas = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if negrita else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if negrita else r"C:\Windows\Fonts\arial.ttf",
    ]
    for ruta in rutas:
        if os.path.exists(ruta):
            try:
                return ImageFont.truetype(ruta, tamano)
            except Exception:
                pass
    return ImageFont.load_default()


def crear_imagen_chollo(descuento, precio_actual, precio_antes, ruta_salida, es_prime=False):
    from PIL import Image, ImageDraw
    W, H = 800, 440
    img = Image.new("RGB", (W, H), color=(255, 255, 255))
    draw = ImageDraw.Draw(img)

    f_gigante = cargar_fuente(105, negrita=True)
    f_super = cargar_fuente(48, negrita=True)
    f_gris = cargar_fuente(30, negrita=False)
    f_prime = cargar_fuente(46, negrita=True)

    descuento = descuento.strip()
    draw.text((38, 45), descuento, font=f_gigante, fill=(204, 12, 57))
    bbox_desc = draw.textbbox((38, 45), descuento, font=f_gigante)
    x_precio = min(bbox_desc[2] + 28, 470)

    partes = precio_actual.replace(".", ",").split(",")
    euros = partes[0]
    centimos = partes[1] if len(partes) > 1 else "00"
    draw.text((x_precio, 52), euros, font=f_gigante, fill=(15, 17, 17))
    bbox_eur = draw.textbbox((x_precio, 52), euros, font=f_gigante)
    draw.text((bbox_eur[2] + 4, 65), f"{centimos} €", font=f_super, fill=(15, 17, 17))

    texto_antes = f"Precio anterior: {precio_antes} €"
    draw.text((42, 220), texto_antes, font=f_gris, fill=(86, 89, 89))
    bbox_sp = draw.textbbox((42, 220), "Precio anterior: ", font=f_gris)
    bbox_total = draw.textbbox((42, 220), texto_antes, font=f_gris)
    y_linea = (bbox_total[1] + bbox_total[3]) // 2 + 2
    draw.line([(bbox_sp[2], y_linea), (bbox_total[2], y_linea)], fill=(86, 89, 89), width=3)

    if es_prime:
        draw.text((45, 330), "✓ Prime", font=f_prime, fill=(0, 113, 197))
    draw.rectangle([(2, 2), (W - 3, H - 3)], outline=(225, 228, 228), width=3)
    img.save(ruta_salida, quality=95)
    return ruta_salida


# ==============================================================================
# TEXTOS
# ==============================================================================
def es_error_precio(chollo):
    return chollo["porcentaje"] >= UMBRAL_ERROR_PRECIO


def construir_texto_telegram(chollo, enlace):
    titulo = html.escape(chollo["titulo_completo"][:200])
    if es_error_precio(chollo):
        cabecera = "🚨 <b>¡POSIBLE ERROR DE PRECIO!</b> 🚨"
        nota = "\n\n⏳ <i>Los errores de precio pueden corregirse o cancelarse en cualquier momento.</i>"
    else:
        cabecera = "⚡ <b>¡NUEVO CHOLLO DETECTADO!</b> ⚡"
        nota = ""
    return (
        f"{cabecera}\n\n"
        f"📦 <b>Producto:</b> {titulo}\n\n"
        f"📉 <b>Descuento:</b> {html.escape(chollo['descuento'])}\n"
        f"💰 <b>Precio:</b> {html.escape(chollo['precio_actual'])} € "
        f"(Precio anterior: <s>{html.escape(chollo['precio_antes'])} €</s>)\n\n"
        f"🛒 <b>Comprar aquí:</b> {html.escape(enlace)}"
        f"{nota}\n\n"
        f"<i>{AVISO_AFILIADO_TG}</i>"
    )


def _peso_caracter_x(caracter):
    """Ponderación de caracteres de X: latín básico y puntuación común pesan 1; el resto (emojis, €...) 2."""
    cp = ord(caracter)
    if (cp <= 0x10FF or 0x2000 <= cp <= 0x200D or 0x2010 <= cp <= 0x201F
            or 0x2032 <= cp <= 0x2037):
        return 1
    return 2


def longitud_x(texto):
    """Longitud efectiva en X: cada URL cuenta 23 y los emojis cuentan doble."""
    sin_urls = re.sub(r"https?://\S+", "x" * 23, texto)
    return sum(_peso_caracter_x(c) for c in sin_urls)


def construir_texto_x(chollo, enlace):
    etiqueta = "🚨 POSIBLE ERROR DE PRECIO" if es_error_precio(chollo) else "🔥 CHOLLO"

    def componer(titulo):
        return (
            f"{etiqueta} {chollo['descuento']}\n"
            f"📦 {titulo}\n"
            f"💰 {chollo['precio_actual']} € (antes {chollo['precio_antes']} €)\n"
            f"🛒 {enlace}\n"
            f"{AVISO_AFILIADO_X}\n"
            "#chollos #amazon"
        )

    titulo_completo = chollo["titulo_completo"]
    for limite in (70, 60, 50, 40, 30, 20, 10):
        recortado = titulo_completo[:limite].rstrip()
        if len(titulo_completo) > limite:
            recortado += "…"
        texto = componer(recortado)
        if longitud_x(texto) <= 275:  # margen de seguridad sobre 280
            return texto
    return componer(titulo_completo[:5])


# ==============================================================================
# PUBLICACIÓN
# ==============================================================================
def publicar_en_telegram(imagen_url, imagen_local, texto_mensaje):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("::warning::Faltan TELEGRAM_TOKEN o TELEGRAM_CHAT_ID.")
        return False

    endpoint = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendPhoto"
    try:
        print("📲 Enviando chollo a Telegram...")
        if imagen_url:
            try:
                respuesta = requests.post(
                    endpoint,
                    data={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "photo": imagen_url,
                        "caption": texto_mensaje,
                        "parse_mode": "HTML",
                    },
                    timeout=25,
                )
                datos = respuesta.json()
                if respuesta.ok and datos.get("ok") is True:
                    print("✅ Telegram confirmó la publicación con la imagen original.")
                    return True
                print(f"⚠️ Telegram rechazó la imagen original: {datos.get('description', respuesta.status_code)}")
            except (requests.RequestException, ValueError) as exc:
                print(f"⚠️ Falló el envío con imagen original: {type(exc).__name__}")

        try:
            with open(imagen_local, "rb") as foto:
                respuesta = requests.post(
                    endpoint,
                    data={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "caption": texto_mensaje,
                        "parse_mode": "HTML",
                    },
                    files={"photo": foto},
                    timeout=25,
                )
            try:
                datos = respuesta.json()
            except ValueError:
                datos = {}
            if respuesta.ok and datos.get("ok") is True:
                print("✅ Telegram confirmó la publicación con la imagen generada.")
                return True
            print(f"::warning::Telegram no confirmó el envío: {datos.get('description', respuesta.status_code)}")
            return False
        except (requests.RequestException, OSError) as exc:
            print(f"::warning::Error enviando a Telegram: {type(exc).__name__}")
            return False
    except Exception as exc:
        print(f"::warning::Error inesperado en Telegram: {type(exc).__name__}")
        return False


def capturar_error(page):
    try:
        page.screenshot(path="error.png", full_page=True)
        print("📸 Captura guardada en error.png")
    except Exception:
        pass


def publicar_en_x(page, texto, ruta_img):
    """Publica en X e intenta confirmar mediante la notificación de éxito de la interfaz."""
    if not X_STATE:
        print("::warning::Falta el secret X_STATE; se deja X pendiente.")
        return False

    try:
        print("🌐 Abriendo el compositor de X...")
        page.goto("https://x.com/compose/post", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

        if "login" in page.url.lower() or "i/flow" in page.url.lower():
            print("::warning::La sesión de X ha caducado o requiere verificación.")
            capturar_error(page)
            return False

        for nombre in [
            "Aceptar todas las cookies", "Accept all cookies",
            "Rechazar cookies no esenciales", "Refuse non-essential cookies"
        ]:
            try:
                page.get_by_role("button", name=nombre).click(timeout=1200)
                page.wait_for_timeout(500)
                break
            except Exception:
                pass

        editor = page.locator("[data-testid='tweetTextarea_0']").first
        editor.wait_for(state="visible", timeout=30000)
        editor.click()
        page.keyboard.insert_text(texto)

        entrada_archivo = page.locator("input[data-testid='fileInput']").first
        entrada_archivo.set_input_files(ruta_img)
        page.locator("[data-testid='attachments']").wait_for(state="visible", timeout=30000)
        page.wait_for_timeout(2500)

        boton = page.locator("[data-testid='tweetButton'], [data-testid='tweetButtonInline']").first
        boton.wait_for(state="visible", timeout=15000)
        if boton.is_disabled():
            print("::warning::El botón de publicar en X está deshabilitado.")
            capturar_error(page)
            return False
        boton.click(timeout=10000)

        # No afirmar éxito solo por haber hecho clic: buscar señales positivas en la interfaz.
        try:
            page.get_by_text(re.compile(r"(Your post was sent|Post sent|Tu post se envió|Se envió tu post)", re.I)).first.wait_for(
                state="visible", timeout=12000
            )
            print("✅ X mostró una confirmación de publicación.")
            return True
        except PlaywrightTimeoutError:
            pass

        # Señal secundaria: el editor se vacía/cierra después de enviar.
        page.wait_for_timeout(3000)
        try:
            editor_actual = page.locator("[data-testid='tweetTextarea_0']").first
            if editor_actual.count() == 0 or not editor_actual.is_visible():
                print("✅ El compositor de X se cerró tras publicar.")
                return True
            texto_restante = editor_actual.inner_text().strip()
            if not texto_restante:
                print("✅ El editor de X quedó vacío tras el envío.")
                return True
        except Exception:
            pass

        print("::warning::No se pudo confirmar la publicación en X; queda pendiente para reintento.")
        capturar_error(page)
        return False

    except Exception as exc:
        print(f"::warning::Error publicando en X: {exc}")
        capturar_error(page)
        return False


# ==============================================================================
# EJECUCIÓN
# ==============================================================================
def ejecutar_bot():
    historial = cargar_historial()

    telegram_disponible = bool(TELEGRAM_TOKEN and TELEGRAM_CHAT_ID)
    x_disponible = bool(X_STATE)

    if not telegram_disponible:
        print("::warning::Telegram no está configurado; se omitirá ese destino.")
    if not x_disponible:
        print("::warning::X_STATE no está configurado; se omitirá X.")

    estado_x = None
    if x_disponible:
        try:
            estado_x = json.loads(X_STATE)
        except json.JSONDecodeError:
            print("::warning::X_STATE no es un JSON válido; se omitirá X en esta ejecución.")
            x_disponible = False

    if not telegram_disponible and not x_disponible:
        print("❌ No hay ninguna plataforma configurada.")
        return False

    print(f"🎚️ Criterio: descuento mínimo {MIN_DESCUENTO}% | etiqueta de error de precio desde {UMBRAL_ERROR_PRECIO}%")

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = None
        try:
            # Amazon se consulta con un contexto independiente de la sesión de X.
            context = browser.new_context(
                user_agent=USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="es-ES",
            )
            page = context.new_page()
            chollo = buscar_chollo_real_amazon(
                page, historial, telegram_disponible, x_disponible
            )
            asin = chollo["asin"]
            estado = historial.setdefault(
                asin, {"telegram_ok": False, "x_ok": False, "creado_utc": ahora_iso()}
            )
            enlace = generar_enlace_afiliado(asin)

            texto_telegram = construir_texto_telegram(chollo, enlace)
            texto_x = construir_texto_x(chollo, enlace)

            ruta_img = crear_imagen_chollo(
                chollo["descuento"], chollo["precio_actual"], chollo["precio_antes"], "chollo.jpg"
            )

            # Cada canal se intenta de forma independiente y su estado se guarda inmediatamente.
            if telegram_disponible and not estado.get("telegram_ok", False):
                tg_ok = publicar_en_telegram(chollo["imagen_original"], ruta_img, texto_telegram)
                if tg_ok:
                    estado["telegram_ok"] = True
                    estado["telegram_fecha_utc"] = ahora_iso()
                    guardar_historial(historial)
            elif estado.get("telegram_ok", False):
                print("ℹ️ Telegram ya publicó este ASIN; no se repetirá.")

            if x_disponible and not estado.get("x_ok", False):
                # Se crea un contexto de X con su estado de sesión guardado.
                x_ok = False
                contexto_x = None
                pagina_x = None
                try:
                    contexto_x = browser.new_context(
                        storage_state=estado_x,
                        user_agent=USER_AGENT,
                        viewport={"width": 1280, "height": 800},
                        locale="es-ES",
                    )
                    pagina_x = contexto_x.new_page()
                    x_ok = publicar_en_x(pagina_x, texto_x, ruta_img)
                except Exception as exc:
                    print(f"::warning::No se pudo preparar la sesión de X: {exc}")
                    if pagina_x is not None:
                        capturar_error(pagina_x)
                finally:
                    try:
                        if contexto_x:
                            contexto_x.close()
                    except Exception:
                        pass

                if x_ok:
                    estado["x_ok"] = True
                    estado["x_fecha_utc"] = ahora_iso()
                    guardar_historial(historial)
            elif estado.get("x_ok", False):
                print("ℹ️ X ya publicó este ASIN; no se repetirá.")

            estado["ultima_comprobacion_utc"] = ahora_iso()
            guardar_historial(historial)

            final = estado_asin(historial, asin)
            telegram_resuelto = (not telegram_disponible) or final["telegram_ok"]
            x_resuelto = (not x_disponible) or final["x_ok"]
            if telegram_resuelto and x_resuelto:
                print("✅ El ASIN se ha publicado en todos los destinos configurados.")
                return True
            if final["telegram_ok"] or final["x_ok"]:
                # Aviso visible en el resumen del run: el workflow sale en verde pero un canal falla.
                if telegram_disponible and not final["telegram_ok"]:
                    print("::warning::Telegram quedó pendiente en esta publicación.")
                if x_disponible and not final["x_ok"]:
                    print("::warning::X quedó pendiente en esta publicación.")
                print(f"⚠️ Publicación parcial: se reintentará el canal pendiente durante {HORAS_REINTENTO} h.")
                return True

            print("⚠️ No se confirmó ninguna publicación. El producto queda pendiente para reintento.")
            return False

        except SinChollo as exc:
            print(f"ℹ️ {exc}")
            # Se guardan los ASIN descartados para no volver a abrirlos en las próximas horas.
            try:
                guardar_historial(historial)
            except OSError as err:
                print(f"⚠️ No se pudo guardar el historial: {err}")
            # No es un error técnico: no había una oferta publicable en este pase.
            return True
        except Exception as exc:
            print(f"❌ Error general del bot: {exc}")
            try:
                if "page" in locals():
                    page.screenshot(path="error.png", full_page=True)
            except Exception:
                pass
            return False
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            browser.close()


if __name__ == "__main__":
    print("⚡ Iniciando CholloVolt (X + Telegram)...")
    if not ejecutar_bot():
        sys.exit(1)
