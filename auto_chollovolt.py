import os
import sys
import json
import re
import random
import html
import time
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ==============================================================================
# CONFIGURACIÓN
# ==============================================================================
AMAZON_TAG = os.getenv("AMAZON_TAG", "librospsico08-21")
X_STATE = os.getenv("X_STATE", "")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

ARCHIVO_HISTORIAL = Path("historial_estados.json")
ARCHIVO_HISTORIAL_LEGACY = Path("historial_publicados.txt")
TIMEOUT_NAVEGACION = 45000

URLS_RASTREO = [
    "https://www.amazon.es/gp/goldbox",
    "https://www.amazon.es/gp/movers-and-shakers/electronics",
    "https://www.amazon.es/gp/bestsellers/electronics",
    "https://www.amazon.es/gp/bestsellers/kitchen",
    "https://www.amazon.es/gp/bestsellers/computers",
    "https://www.amazon.es/gp/bestsellers/videogames",
]


class SinChollo(Exception):
    """No se encontró un producto nuevo o pendiente con descuento válido."""


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


def generar_enlace_afiliado(asin):
    return f"https://www.amazon.es/dp/{asin}?tag={AMAZON_TAG}"


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
        valor = valor.replace(".", "")
        return valor
    return valor.replace(".", ",")


def precio_float(texto):
    if not texto:
        return None
    try:
        return float(texto.replace(".", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None


def detectar_bloqueo_amazon(page):
    try:
        titulo = page.title().lower()
        html_bajo = page.content().lower()
        return (
            "captcha" in html_bajo
            or "introduce los caracteres" in html_bajo
            or "robot" in titulo
            or "sorry" in titulo
        )
    except Exception:
        return False


def buscar_chollo_real_amazon(page, historial, telegram_disponible, x_disponible):
    urls = URLS_RASTREO[:]
    random.shuffle(urls)

    for url in urls:
        print(f"🔎 Buscando ofertas activas en: {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT_NAVEGACION)
            page.wait_for_timeout(2200)

            if detectar_bloqueo_amazon(page):
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
            asins_vistos = []
            for href in enlaces:
                match = re.search(r"/dp/([A-Z0-9]{10})", href)
                if match:
                    asin = match.group(1)
                    if asin not in asins_vistos:
                        asins_vistos.append(asin)

            print(f"   → {len(asins_vistos)} ASIN distintos encontrados")

            for asin in asins_vistos[:20]:
                estado = estado_asin(historial, asin)
                # Solo exigir los destinos configurados en esta ejecución.
                telegram_resuelto = (not telegram_disponible) or estado["telegram_ok"]
                x_resuelto = (not x_disponible) or estado["x_ok"]
                if telegram_resuelto and x_resuelto:
                    continue

                page.goto(f"https://www.amazon.es/dp/{asin}", wait_until="domcontentloaded", timeout=TIMEOUT_NAVEGACION)
                page.wait_for_timeout(1300)

                if detectar_bloqueo_amazon(page):
                    print(f"⚠️ Posible bloqueo al consultar {asin}; continúo.")
                    continue

                loc_titulo = page.locator("#productTitle")
                if loc_titulo.count() == 0:
                    continue
                titulo = loc_titulo.first.inner_text().strip()
                if not titulo:
                    continue

                loc_img = page.locator("#landingImage, #imgBlkFront").first
                img_url = None
                if loc_img.count():
                    img_url = (
                        loc_img.get_attribute("src")
                        or loc_img.get_attribute("data-old-hires")
                    )

                descuento = None
                loc_desc = page.locator(".savingsPercentage").first
                if loc_desc.count():
                    try:
                        if loc_desc.is_visible():
                            descuento = loc_desc.inner_text().strip()
                    except Exception:
                        pass

                loc_actual = page.locator(
                    "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen, "
                    "#corePrice_feature_div .a-price .a-offscreen, .a-price .a-offscreen"
                ).first
                precio_actual = limpiar_precio(loc_actual.inner_text()) if loc_actual.count() else None

                loc_antes = page.locator(
                    ".basisPrice .a-offscreen, .a-text-price .a-offscreen, "
                    "#corePriceDisplay_desktop_feature_div .a-text-price .a-offscreen"
                ).first
                precio_antes = limpiar_precio(loc_antes.inner_text()) if loc_antes.count() else None

                actual_num = precio_float(precio_actual)
                antes_num = precio_float(precio_antes)

                if descuento and actual_num is not None and antes_num is not None and antes_num > actual_num:
                    print(f"🎯 Oferta candidata: {asin} | Telegram pendiente={not estado['telegram_ok']} | X pendiente={not estado['x_ok']}")
                    return {
                        "asin": asin,
                        "titulo_completo": titulo,
                        "titulo_corto": titulo[:85] + ("..." if len(titulo) > 85 else ""),
                        "descuento": descuento,
                        "precio_actual": precio_actual,
                        "precio_antes": precio_antes,
                        "imagen_original": img_url,
                    }

        except Exception as exc:
            print(f"⚠️ Aviso rastreando {url}: {exc}")
            continue

    raise SinChollo("No se encontró ningún chollo válido ni productos pendientes de reintento.")


def cargar_fuente(tamano, negrita=False):
    rutas = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if negrita else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        r"C:\Windows\Fonts\arialbd.ttf" if negrita else r"C:\Windows\Fonts\arial.ttf",
    ]
    for ruta in rutas:
        if os.path.exists(ruta):
            try:
                from PIL import ImageFont
                return ImageFont.truetype(ruta, tamano)
            except Exception:
                pass
    from PIL import ImageFont
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
                print(f"⚠️ Falló el envío con imagen original: {exc}")

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
            print(f"::warning::Error enviando a Telegram: {exc}")
            return False
    except Exception as exc:
        print(f"::warning::Error inesperado en Telegram: {exc}")
        return False


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
        return False

    except Exception as exc:
        print(f"::warning::Error publicando en X: {exc}")
        try:
            page.screenshot(path="error.png", full_page=True)
            print("📸 Captura guardada en error.png")
        except Exception:
            pass
        return False


def ejecutar_bot():
    historial = cargar_historial()

    # Solo abrir Playwright si existe una plataforma que pueda utilizarse.
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("::warning::Telegram no está configurado; se omitirá ese destino.")
    if not X_STATE:
        print("::warning::X_STATE no está configurado; se omitirá X.")

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        telegram_disponible = False
    else:
        telegram_disponible = True
    x_disponible = bool(X_STATE)

    if not telegram_disponible and not x_disponible:
        print("❌ No hay ninguna plataforma configurada.")
        return False

    estado_x = None
    if x_disponible:
        try:
            estado_x = json.loads(X_STATE)
        except json.JSONDecodeError:
            print("::warning::X_STATE no es un JSON válido; se omitirá X en esta ejecución.")
            x_disponible = False

    if not telegram_disponible and not x_disponible:
        return False

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = None
        try:
            # Amazon se consulta con un contexto independiente de la sesión de X.
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800},
                locale="es-ES",
            )
            page = context.new_page()
            chollo = buscar_chollo_real_amazon(
                page, historial, telegram_disponible, x_disponible
            )
            asin = chollo["asin"]
            estado = historial.setdefault(asin, {"telegram_ok": False, "x_ok": False})
            enlace = generar_enlace_afiliado(asin)

            titulo_limpio = html.escape(chollo["titulo_completo"][:200])
            texto_telegram = (
                "⚡ <b>¡NUEVO CHOLLO DETECTADO!</b> ⚡\n\n"
                f"📦 <b>Producto:</b> {titulo_limpio}\n\n"
                f"📉 <b>Descuento:</b> {html.escape(chollo['descuento'])}\n"
                f"💰 <b>Precio:</b> {html.escape(chollo['precio_actual'])} € "
                f"(Precio anterior: <s>{html.escape(chollo['precio_antes'])} €</s>)\n\n"
                f"🛒 <b>Comprar aquí:</b> {html.escape(enlace)}\n\n"
                "<i>Como afiliado de Amazon, obtengo ingresos por las compras adscritas que cumplen los requisitos aplicables.</i>"
            )
            # Texto deliberadamente compacto para dejar margen al límite de X.
            titulo_x = chollo["titulo_completo"][:65]
            texto_x = (
                f"🔥 {chollo['descuento']} 🔥\n"
                f"📦 {titulo_x}\n"
                f"💰 {chollo['precio_actual']} € (antes {chollo['precio_antes']} €)\n"
                f"🛒 {enlace}\n"
                "Como afiliado de Amazon, obtengo ingresos por compras que cumplen los requisitos.\n"
                "#chollos #amazon"
            )

            ruta_img = crear_imagen_chollo(
                chollo["descuento"], chollo["precio_actual"], chollo["precio_antes"], "chollo.jpg"
            )

            # Cada canal se intenta de forma independiente y su estado se guarda inmediatamente.
            if telegram_disponible and not estado.get("telegram_ok", False):
                tg_ok = publicar_en_telegram(chollo["imagen_original"], ruta_img, texto_telegram)
                if tg_ok:
                    estado["telegram_ok"] = True
                    estado["telegram_fecha_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    guardar_historial(historial)
            elif estado.get("telegram_ok", False):
                print("ℹ️ Telegram ya publicó este ASIN; no se repetirá.")

            if x_disponible and not estado.get("x_ok", False):
                # Se crea un contexto de X con su estado de sesión guardado.
                try:
                    contexto_x = browser.new_context(
                        storage_state=estado_x,
                        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                        viewport={"width": 1280, "height": 800},
                        locale="es-ES",
                    )
                    pagina_x = contexto_x.new_page()
                    x_ok = publicar_en_x(pagina_x, texto_x, ruta_img)
                    contexto_x.close()
                except Exception as exc:
                    print(f"::warning::No se pudo preparar la sesión de X: {exc}")
                    x_ok = False

                if x_ok:
                    estado["x_ok"] = True
                    estado["x_fecha_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                    guardar_historial(historial)
            elif estado.get("x_ok", False):
                print("ℹ️ X ya publicó este ASIN; no se repetirá.")

            estado["ultima_comprobacion_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            guardar_historial(historial)

            final = estado_asin(historial, asin)
            telegram_resuelto = (not telegram_disponible) or final["telegram_ok"]
            x_resuelto = (not x_disponible) or final["x_ok"]
            if telegram_resuelto and x_resuelto:
                print("✅ El ASIN se ha publicado en todos los destinos configurados.")
                return True
            if final["telegram_ok"] or final["x_ok"]:
                print("⚠️ Publicación parcial: se conservará el estado y se reintentará el canal pendiente.")
                return True

            print("⚠️ No se confirmó ninguna publicación. El producto queda pendiente para reintento.")
            return False

        except SinChollo as exc:
            print(f"ℹ️ {exc}")
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
