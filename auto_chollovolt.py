import os
import sys
import json
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

# ==============================================================================
# CONFIGURACIÓN Y SECRETS
# ==============================================================================
AMAZON_TAG = os.getenv("AMAZON_TAG", "librospsico08-21")
X_STATE = os.getenv("X_STATE", "")  # contenido de x_state.json (secret de GitHub)

ARCHIVO_HISTORIAL = "historial_publicados.txt"

CHOLLOS_RESPALDO = [
    {"asin": "B09B8W5V33", "descuento": "-35%", "precio_actual": "29,99", "precio_antes": "46,99"},
    {"asin": "B091HWBBNF", "descuento": "-40%", "precio_actual": "24,99", "precio_antes": "41,99"},
    {"asin": "B08C1W5N81", "descuento": "-50%", "precio_actual": "19,99", "precio_antes": "39,99"},
    {"asin": "B07WJYV738", "descuento": "-45%", "precio_actual": "34,99", "precio_antes": "63,99"},
    {"asin": "B09G3HRMVB", "descuento": "-30%", "precio_actual": "49,99", "precio_antes": "71,99"},
]


def cargar_historial():
    if not os.path.exists(ARCHIVO_HISTORIAL):
        return set()
    with open(ARCHIVO_HISTORIAL, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def guardar_en_historial(asin: str):
    with open(ARCHIVO_HISTORIAL, "a", encoding="utf-8") as f:
        f.write(f"{asin}\n")


def obtener_siguiente_chollo():
    historial = cargar_historial()
    for item in CHOLLOS_RESPALDO:
        if item["asin"] not in historial:
            return item
    if os.path.exists(ARCHIVO_HISTORIAL):
        os.remove(ARCHIVO_HISTORIAL)
    return CHOLLOS_RESPALDO[0]


def generar_enlace_afiliado(asin: str):
    return f"https://www.amazon.es/dp/{asin}?tag={AMAZON_TAG}"


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
# PUBLICACIÓN EN LA WEB DE X USANDO SESIÓN GUARDADA
# ==============================================================================
def publicar_en_web_x(ruta_imagen: str, texto_tuit: str) -> bool:
    if not X_STATE:
        print("❌ Falta el secret X_STATE (contenido de x_state.json).")
        return False

    try:
        estado = json.loads(X_STATE)
    except json.JSONDecodeError:
        print("❌ X_STATE no es un JSON válido. Copia el contenido COMPLETO de x_state.json.")
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
            print("🌐 Abriendo X con la sesión guardada...")
            page.goto("https://x.com/compose/post", timeout=60000)

            if "login" in page.url or "i/flow" in page.url:
                raise RuntimeError("La sesión ha caducado o X pide verificación. Genera x_state.json de nuevo.")

            # Cerrar banner de cookies si está tapando la interfaz
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
            page.locator("input[data-testid='fileInput']").first.set_input_files(ruta_imagen)
            page.locator("[data-testid='attachments']").wait_for(timeout=30000)
            # Espera necesaria para que X termine de subir la imagen al servidor y active el botón
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

            # Tras publicar, desaparece el cuadro de redacción
            editor.wait_for(state="detached", timeout=30000)

            print("✅ Tuit publicado.")
            return True
        except Exception as e:
            print(f"❌ Error publicando en X: {e}")
            try:
                page.screenshot(path="error.png", full_page=True)
                print("📸 Captura guardada en error.png")
            except Exception:
                pass
            return False
        finally:
            browser.close()


def main():
    print("⚡ Iniciando CholloVolt...")
    chollo = obtener_siguiente_chollo()

    asin = chollo["asin"]
    enlace = generar_enlace_afiliado(asin)
    texto_tuit = f"🔥 {chollo['descuento']} de descuento 🔥\n🛒 {enlace}\n\n#chollos #amazon #ofertas #chollo"

    ruta_img = crear_imagen_chollo(
        chollo["descuento"], chollo["precio_actual"], chollo["precio_antes"], "chollo.jpg"
    )

    if publicar_en_web_x(ruta_img, texto_tuit):
        guardar_en_historial(asin)
    else:
        sys.exit(1)  # que el workflow salga en ROJO si no se publicó


if __name__ == "__main__":
    main()
