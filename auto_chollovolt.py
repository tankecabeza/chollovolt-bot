import os
import sys
import json
import re
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

# ==============================================================================
# CONFIGURACIÓN Y SECRETS
# ==============================================================================
AMAZON_TAG = os.getenv("AMAZON_TAG", "librospsico08-21")
X_STATE = os.getenv("X_STATE", "")

ARCHIVO_HISTORIAL = "historial_publicados.txt"

# Categorías de Amazon España donde hay chollos reales constantemente
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
    if not texto:
        return None
    m = re.search(r"(\d+[.,]\d{2})", texto)
    return m.group(1).replace(".", ",") if m else None


def buscar_chollo_real_amazon(page):
    """Rastrea Amazon.es en tiempo real buscando un producto activo con descuento."""
    historial = cargar_historial()

    for url in URLS_RASTREO:
        print(f"🔎 Buscando ofertas activas en: {url}")
        try:
            page.goto(url, timeout=45000)
            page.wait_for_timeout(2000)

            # Cerrar cookies de Amazon si aparecen
            try:
                btn_cookies = page.locator("#sp-cc-accept")
                if btn_cookies.is_visible(timeout=2000):
                    btn_cookies.click()
            except Exception:
                pass

            # Extraer todos los ASINs visibles en la página de ranking
            enlaces = page.locator("a[href*='/dp/']").all()
            asins_vistos = []
            for a in enlaces:
                href = a.get_attribute("href") or ""
                m = re.search(r"/dp/([A-Z0-9]{10})", href)
                if m:
                    asin = m.group(1)
                    if asin not in historial and asin not in asins_vistos:
                        asins_vistos.append(asin)

            # Analizar los candidatos entrando en su ficha real
            for asin in asins_vistos[:12]:
                url_prod = f"https://www.amazon.es/dp/{asin}"
                page.goto(url_prod, timeout=30000)
                page.wait_for_timeout(1500)

                # Comprobar que la página existe y tiene título
                if page.locator("#productTitle").count() == 0:
                    continue

                titulo = page.locator("#productTitle").first.inner_text().strip()

                # Buscar descuento (ej. -25%)
                descuento = None
                loc_desc = page.locator(".savingsPercentage").first
                if loc_desc.count() > 0 and loc_desc.is_visible():
                    descuento = loc_desc.inner_text().strip()

                # Buscar precio actual
                precio_actual = None
                loc_precio = page.locator("#corePriceDisplay_desktop_feature_div .a-price .a-offscreen, .a-price .a-offscreen").first
                if loc_precio.count() > 0:
                    precio_actual = limpiar_precio(loc_precio.inner_text())

                # Buscar precio anterior / recomendado
                precio_antes = None
                loc_antes = page.locator(".basisPrice .a-offscreen, .a-text-price .a-offscreen").first
                if loc_antes.count() > 0:
                    precio_antes = limpiar_precio(loc_antes.inner_text())

                # Si tiene descuento y ambos precios reales, nos lo quedamos
                if descuento and precio_actual and precio_antes:
                    print(f"🎯 ¡Chollo real encontrado! {asin} | {descuento} | {precio_actual}€ (Antes: {precio_antes}€)")
                    return {
                        "asin": asin,
                        "titulo": titulo[:85] + "..." if len(titulo) > 85 else titulo,
                        "descuento": descuento,
                        "precio_actual": precio_actual,
                        "precio_antes": precio_antes,
                    }
        except Exception as e:
            print(f"⚠️ Aviso rastreando {url}: {e}")
            continue

    raise RuntimeError("No se encontró ningún chollo con descuento activo en este pase.")


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


def ejecutar_bot():
    if not X_STATE:
        print("❌ Falta el secret X_STATE (contenido de x_state.json).")
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
        try:
            # 1. Buscar un chollo real y verificar que existe en Amazon.es
            chollo = buscar_chollo_real_amazon(page)
            asin = chollo["asin"]
            enlace = generar_enlace_afiliado(asin)
            texto_tuit = (
                f"🔥 {chollo['descuento']} de descuento 🔥\n"
                f"📦 {chollo['titulo']}\n"
                f"💰 Ahora: {chollo['precio_actual']}€ (Antes: {chollo['precio_antes']}€)\n"
                f"🛒 {enlace}\n\n"
                f"#chollos #amazon #ofertas #chollo"
            )

            ruta_img = crear_imagen_chollo(
                chollo["descuento"], chollo["precio_actual"], chollo["precio_antes"], "chollo.jpg"
            )

            # 2. Publicar en X
            print("🌐 Abriendo X con la sesión guardada...")
            page.goto("https://x.com/compose/post", timeout=60000)

            if "login" in page.url or "i/flow" in page.url:
                raise RuntimeError("La sesión ha caducado o X pide verificación. Genera x_state.json de nuevo.")

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
            page.keyboard.insert_text(texto_tuit)

            print("🖼️ Adjuntando imagen...")
            page.locator("input[data-testid='fileInput']").first.set_input_files(ruta_img)
            page.locator("[data-testid='attachments']").wait_for(timeout=30000)
            page.wait_for_timeout(4000)

            print("🚀 Publicando...")
            boton = page.locator("[data-testid='tweetButton'], [data-testid='tweetButtonInline']").first
            boton.wait_for(timeout=15000)

            try:
                boton.click(timeout=6000)
            except Exception:
                print("⚠️ Clic normal bloqueado por una capa; lanzando el clic directo...")
                boton.dispatch_event("click")
                page.wait_for_timeout(1500)
                if editor.is_visible():
                    editor.focus()
                    page.keyboard.press("Control+Enter")

            editor.wait_for(state="detached", timeout=30000)
            print("✅ Tuit publicado con enlace 100% verificado.")
            guardar_en_historial(asin)
            return True

        except Exception as e:
            print(f"❌ Error en el proceso: {e}")
            try:
                page.screenshot(path="error.png", full_page=True)
                print("📸 Captura guardada en error.png")
            except Exception:
                pass
            return False
        finally:
            browser.close()


if __name__ == "__main__":
    print("⚡ Iniciando CholloVolt con rastreador en vivo...")
    if not ejecutar_bot():
        sys.exit(1)
