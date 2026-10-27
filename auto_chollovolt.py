import os
import sys
import json
import re
import requests
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

# ==============================================================================
# CONFIGURACIÓN Y SECRETS
# ==============================================================================
AMAZON_TAG = os.getenv("AMAZON_TAG", "librospsico08-21")
X_STATE = os.getenv("X_STATE", "")

ARCHIVO_HISTORIAL = "historial_publicados.txt"

URLS_RASTREO = [
    "https://www.amazon.es/gp/movers-and-shakers/electronics",
    "https://www.amazon.es/gp/bestsellers/electronics",
    "https://www.amazon.es/gp/bestsellers/kitchen",
    "https://www.amazon.es/gp/bestsellers/computers",
    "https://www.amazon.es/gp/bestsellers/videogames",
]

def cargar_historial():
    if not os.path.exists(ARCHIVO_HISTORIAL):
        return set()
    with open(ARCHIVO_HISTORIAL, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def guardar_en_historial(asin: str):
    with open(ARCHIVO_HISTORIAL, "a", encoding="utf-8") as f:
        f.write(f"{asin}\n")

def generar_enlace_afiliado(asin: str):
    return f"https://www.amazon.es/dp/{asin}?tag={AMAZON_TAG}"

def limpiar_precio(texto: str):
    """Normaliza precios españoles como 1.234,56 €, 1234.56 o 99,99."""
    if not texto:
        return None
    coincidencias = re.findall(r"(?<!\d)(?:\d{1,3}(?:\.\d{3})+|\d+)[,.]\d{2}(?!\d)", texto)
    if not coincidencias:
        return None
    valor = coincidencias[-1].replace(".", "").replace(",", ".")
    try:
        return f"{float(valor):.2f}".replace(".", ",")
    except ValueError:
        return None


def porcentaje_descuento(texto: str):
    if not texto:
        return None
    m = re.search(r"(\d{1,3})\s*%", texto.replace("−", "-"))
    if not m:
        return None
    valor = int(m.group(1))
    return valor if 0 < valor <= 99 else None


def buscar_chollo_real_amazon(page):
    """
    Busca candidatos en varias listas y solo acepta descuentos >= 25 %.
    Los descuentos muy altos se priorizan como posibles errores de precio,
    pero solo un historial de precios externo permite confirmar que lo sean.
    """
    historial = cargar_historial()
    candidatos = []
    vistos = set()

    # Primero recogemos ASIN de las listas; no abrimos todos los productos.
    for url in URLS_RASTREO:
        print(f"🔎 Escaneando lista: {url}")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(500)

            try:
                btn_cookies = page.locator("#sp-cc-accept")
                if btn_cookies.is_visible(timeout=700):
                    btn_cookies.click(timeout=1000)
                    page.wait_for_timeout(250)
            except Exception:
                pass

            enlaces = page.locator("a[href*='/dp/']").evaluate_all(
                "(els) => els.map(a => a.href)"
            )
            añadidos = 0
            for href in enlaces:
                m = re.search(r"/dp/([A-Z0-9]{10})", href or "")
                if not m:
                    continue
                asin = m.group(1)
                if asin in historial or asin in vistos:
                    continue
                vistos.add(asin)
                candidatos.append(asin)
                añadidos += 1
                if añadidos >= 6:
                    break
        except Exception as e:
            print(f"⚠️ Aviso escaneando {url}: {e}")

    if not candidatos:
        raise RuntimeError("No hay productos nuevos en las listas rastreadas.")

    # Límite deliberado para que el workflow no tarde varios minutos revisando
    # decenas de fichas por cada categoría.
    max_productos = 18
    ofertas = []

    for indice, asin in enumerate(candidatos[:max_productos], start=1):
        url_prod = f"https://www.amazon.es/dp/{asin}"
        try:
            print(f"🔬 Revisando producto {indice}/{min(len(candidatos), max_productos)}: {asin}")
            page.goto(url_prod, wait_until="domcontentloaded", timeout=25000)
            page.wait_for_timeout(350)

            loc_titulo = page.locator("#productTitle")
            if loc_titulo.count() == 0:
                continue
            titulo = loc_titulo.first.inner_text(timeout=2500).strip()

            loc_img = page.locator("#landingImage, #imgBlkFront").first
            img_url = loc_img.get_attribute("src", timeout=2000) if loc_img.count() else None

            descuento_texto = None
            loc_desc = page.locator(".savingsPercentage").first
            if loc_desc.count():
                try:
                    descuento_texto = loc_desc.inner_text(timeout=1500).strip()
                except Exception:
                    pass
            descuento_num = porcentaje_descuento(descuento_texto)

            loc_precio = page.locator(
                "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen, "
                "#apex_desktop .a-price .a-offscreen, .a-price .a-offscreen"
            ).first
            precio_actual = limpiar_precio(loc_precio.inner_text(timeout=2000)) if loc_precio.count() else None

            loc_antes = page.locator(
                ".basisPrice .a-offscreen, .a-text-price .a-offscreen, "
                "#corePriceDisplay_desktop_feature_div .a-text-price .a-offscreen"
            ).first
            precio_antes = limpiar_precio(loc_antes.inner_text(timeout=2000)) if loc_antes.count() else None

            if not precio_actual or not precio_antes:
                continue

            val_act = float(precio_actual.replace(",", "."))
            val_ant = float(precio_antes.replace(",", "."))
            if val_act <= 0 or val_ant <= val_act:
                continue

            # Calculamos el descuento también desde los precios por si Amazon
            # no expone el porcentaje en el selector habitual.
            descuento_calculado = round((1 - val_act / val_ant) * 100)
            descuento_num = max(descuento_num or 0, descuento_calculado)
            if descuento_num < 25:
                print(f"⏭️ Descartado: {descuento_num}% (mínimo exigido: 25%).")
                continue

            descuento = f"-{descuento_num}%"
            # Priorización: descuentos extremos primero, luego el resto.
            # >=50 % es una señal de posible precio erróneo, no una confirmación.
            prioridad_error = 1 if descuento_num >= 50 else 0
            ofertas.append({
                "asin": asin,
                "titulo_completo": titulo,
                "titulo_corto": titulo[:85] + "..." if len(titulo) > 85 else titulo,
                "descuento": descuento,
                "descuento_num": descuento_num,
                "precio_actual": precio_actual,
                "precio_antes": precio_antes,
                "imagen_original": img_url,
                "prioridad_error": prioridad_error,
            })
            print(f"✅ Candidato válido: {descuento} | {precio_actual} € (antes {precio_antes} €)")

        except Exception as e:
            print(f"⚠️ No se pudo revisar {asin}: {e}")
            continue

    if not ofertas:
        raise RuntimeError("No se encontró ningún producto nuevo con descuento real de al menos el 25 %.")

    ofertas.sort(key=lambda x: (x["prioridad_error"], x["descuento_num"]), reverse=True)
    mejor = ofertas[0]
    mejor.pop("descuento_num", None)
    mejor.pop("prioridad_error", None)
    print(f"🎯 Mejor candidato seleccionado: {mejor['asin']} | {mejor['descuento']}")
    return mejor

def cargar_fuente(tamano: int, negrita: bool = False):
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

def crear_imagen_chollo(descuento, precio_actual, precio_antes, ruta_salida):
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

    partes = precio_actual.replace(".", ",").split(",")
    euros = partes[0]
    centimos = partes[1] if len(partes) > 1 else "00"

    draw.text((x_precio, 50), euros, font=f_gigante, fill=(15, 17, 17))
    bbox_eur = draw.textbbox((x_precio, 50), euros, font=f_gigante)
    draw.text((bbox_eur[2] + 6, 58), f"{centimos} €", font=f_super, fill=(15, 17, 17))

    texto_antes = f"Precio recomendado: {precio_antes}€"
    draw.text((45, 230), texto_antes, font=f_gris, fill=(86, 89, 89))

    bbox_sp = draw.textbbox((45, 230), "Precio recomendado: ", font=f_gris)
    bbox_total = draw.textbbox((45, 230), texto_antes, font=f_gris)
    y_linea = (bbox_total[1] + bbox_total[3]) // 2 + 2
    draw.line([(bbox_sp[2], y_linea), (bbox_total[2], y_linea)], fill=(86, 89, 89), width=3)

    draw.line([(48, 348), (62, 366), (92, 326)], fill=(255, 153, 0), width=9)
    draw.text((98, 315), "prime", font=f_prime, fill=(0, 113, 197))
    draw.rectangle([(2, 2), (W - 3, H - 3)], outline=(225, 228, 228), width=3)

    img.save(ruta_salida, quality=95)
    return ruta_salida

# ==============================================================================
# PUBLICACIÓN EN TELEGRAM
# ==============================================================================
def publicar_en_telegram(imagen_url: str, imagen_local: str, texto_mensaje: str) -> bool:
    bot_token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")

    if not bot_token or not chat_id:
        print("⚠️ Faltan los secrets de Telegram.")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
    
    try:
        print("📲 Enviando chollo a Telegram...")
        
        # 1º Intento: Mandar la URL de la imagen real de Amazon directamente a Telegram
        if imagen_url:
            respuesta = requests.post(
                url, 
                data={"chat_id": chat_id, "photo": imagen_url, "caption": texto_mensaje, "parse_mode": "HTML"},
                timeout=15
            )
            if respuesta.status_code == 200:
                print("✅ ¡Chollo publicado en Telegram con la imagen original!")
                return True

        # 2º Intento (Fallback): Si falla la original, mandamos la generada (chollo.jpg)
        print("⚠️ Falló la imagen original, enviando la generada...")
        with open(imagen_local, "rb") as foto:
            respuesta = requests.post(
                url, 
                data={"chat_id": chat_id, "caption": texto_mensaje, "parse_mode": "HTML"},
                files={"photo": foto},
                timeout=15
            )
            
        if respuesta.status_code == 200:
            return True
        else:
            print(f"❌ Error de la API de Telegram: {respuesta.text}")
            return False
            
    except Exception as e:
        print(f"❌ Error crítico conectando con Telegram: {e}")
        return False

# ==============================================================================
# EJECUCIÓN PRINCIPAL
# ==============================================================================
def ejecutar_bot():
    if not X_STATE:
        print("❌ Falta el secret X_STATE.")
        return False

    try:
        estado = json.loads(X_STATE)
    except json.JSONDecodeError:
        print("❌ X_STATE no es un JSON válido.")
        return False

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            storage_state=estado,
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 800},
            locale="es-ES",
        )
        page = context.new_page()
        # Reduce carga y tiempos sin tocar el flujo de publicación en X.
        page.route("**/*", lambda route: route.abort()
                   if route.request.resource_type in ("font", "media")
                   else route.continue_())
        try:
            chollo = buscar_chollo_real_amazon(page)
            asin = chollo["asin"]
            enlace = generar_enlace_afiliado(asin)
            
            # --- DISEÑO PARA X (Clickbait visual y corto) ---
            texto_x = (
                f"🔥 {chollo['descuento']} de descuento 🔥\n"
                f"📦 {chollo['titulo_corto']}\n"
                f"💰 Ahora: {chollo['precio_actual']}€ (Antes: {chollo['precio_antes']}€)\n"
                f"🛒 {enlace}\n\n"
                f"#chollos #amazon #ofertas"
            )

            # --- DISEÑO PARA TELEGRAM (Limpio, completo y con formato HTML) ---
            # Limpiamos caracteres HTML que podrían romper la API de Telegram
            titulo_limpio = chollo['titulo_completo'].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            texto_telegram = (
                f"⚡ <b>¡NUEVO CHOLLO DETECTADO!</b> ⚡\n\n"
                f"📦 <b>Producto:</b> {titulo_limpio}\n\n"
                f"📉 <b>Descuento:</b> {chollo['descuento']}\n"
                f"💰 <b>Precio:</b> {chollo['precio_actual']}€ (PVP: <s>{chollo['precio_antes']}€</s>)\n\n"
                f"🛒 <b>Comprar aquí:</b> {enlace}"
            )

            ruta_img = crear_imagen_chollo(
                chollo["descuento"], chollo["precio_actual"], chollo["precio_antes"], "chollo.jpg"
            )
            
            # 1. Enviar a Telegram (con imagen real y texto largo)
            publicar_en_telegram(chollo["imagen_original"], ruta_img, texto_telegram)

            # 2. Enviar a X (con imagen generada y texto corto)
            print("🌐 Abriendo X con la sesión guardada...")
            page.goto("https://x.com/compose/post", timeout=60000)

            if "login" in page.url or "i/flow" in page.url:
                raise RuntimeError("La sesión ha caducado o X pide verificación.")

            for texto in ["Aceptar todas las cookies", "Accept all cookies",
                          "Rechazar cookies no esenciales", "Refuse non-essential cookies"]:
                try:
                    page.get_by_role("button", name=texto).click(timeout=2000)
                    page.wait_for_timeout(1000)
                    break
                except Exception:
                    pass

            print("✍️ Escribiendo tuit...")
            editor = page.locator("[data-testid='tweetTextarea_0']").first
            editor.wait_for(timeout=30000)
            editor.click(force=True)
            page.keyboard.insert_text(texto_x)

            print("🖼️ Adjuntando imagen generada...")
            page.locator("input[data-testid='fileInput']").first.set_input_files(ruta_img)
            page.locator("[data-testid='attachments']").wait_for(timeout=30000)
            page.wait_for_timeout(4000)

            print("🚀 Publicando en X...")
            boton = page.locator("[data-testid='tweetButton'], [data-testid='tweetButtonInline']").first
            boton.wait_for(timeout=15000)

            try:
                boton.click(timeout=6000)
            except Exception:
                boton.dispatch_event("click")
                page.wait_for_timeout(1500)
                if editor.is_visible():
                    editor.focus()
                    page.keyboard.press("Control+Enter")

            page.wait_for_timeout(6000)
            print("✅ Tuit publicado con éxito.")
            guardar_en_historial(asin)
            return True

        except Exception as e:
            print(f"❌ Error en el proceso: {e}")
            return False
        finally:
            browser.close()

if __name__ == "__main__":
    print("⚡ Iniciando CholloVolt (Dual Mode: X + Telegram)...")
    if not ejecutar_bot():
        sys.exit(1)
