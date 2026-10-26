import os
import time
from PIL import Image, ImageDraw, ImageFont
from playwright.sync_api import sync_playwright

# ==============================================================================
# CONFIGURACIÓN Y SECRETS
# ==============================================================================
AMAZON_TAG = os.getenv("AMAZON_TAG", "librospsico08-21")
X_USER = os.getenv("X_USER", "")
X_PASS = os.getenv("X_PASS", "")

ARCHIVO_HISTORIAL = "historial_publicados.txt"

CHOLLOS_RESPALDO = [
    {"asin": "B09B8W5V33", "descuento": "-35%", "precio_actual": "29,99", "precio_antes": "46,99"},
    {"asin": "B091HWBBNF", "descuento": "-40%", "precio_actual": "24,99", "precio_antes": "41,99"},
    {"asin": "B08C1W5N81", "descuento": "-50%", "precio_actual": "19,99", "precio_antes": "39,99"},
    {"asin": "B07WJYV738", "descuento": "-45%", "precio_actual": "34,99", "precio_antes": "63,99"},
    {"asin": "B09G3HRMVB", "descuento": "-30%", "precio_actual": "49,99", "precio_antes": "71,99"}
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
        r"C:\Windows\Fonts\arialbd.ttf" if negrita else r"C:\Windows\Fonts\arial.ttf"
    ]
    for r in rutas:
        if os.path.exists(r):
            try:
                return ImageFont.truetype(r, tamano)
            except Exception:
                pass
    return ImageFont.load_default()

def crear_imagen_chollo(descuento: str, precio_actual: str, precio_antes: str, ruta_salida: str):
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
# AUTOMATIZACIÓN WEB ROBUSTA EN X
# ==============================================================================
def publicar_en_web_x(ruta_imagen: str, texto_tuit: str):
    if not X_USER or not X_PASS:
        print("❌ Faltan los secretos X_USER o X_PASS en GitHub.")
        return False

    try:
        with sync_playwright() as p:
            # Usar user-agent real para evitar detección de bot básico
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 800}
            )
            page = context.new_page()
            
            print("🌐 Entrando en la página de acceso de X...")
            page.goto("https://x.com/i/flow/login", timeout=60000)
            time.sleep(7)

            print("✍️ Introduciendo usuario...")
            page.wait_for_selector("input[autocomplete='username']", timeout=15000)
            page.fill("input[autocomplete='username']", X_USER)
            page.keyboard.press("Enter")
            time.sleep(4)

            print("🔑 Introduciendo contraseña...")
            page.wait_for_selector("input[autocomplete='current-password']", timeout=15000)
            page.fill("input[autocomplete='current-password']", X_PASS)
            page.keyboard.press("Enter")
            time.sleep(8)

            print("✍️ Abriendo el compositor de tuits...")
            page.goto("https://twitter.com/compose/post", timeout=60000)
            time.sleep(6)

            # Escribir el texto
            editor = page.locator("div[aria-label='Post text'], div[aria-label='Texto del post']").first
            editor.click()
            editor.fill(texto_tuit)
            time.sleep(3)

            # Adjuntar imagen
            print("🖼️ Adjuntando tarjeta de oferta...")
            file_input = page.locator("input[data-testid='fileInput']")
            file_input.set_input_files(ruta_imagen)
            time.sleep(5)

            # Publicar
            print("🚀 Publicando tuit...")
            post_btn = page.locator("[data-testid='tweetButton'], [data-testid='tweetButtonInline']").first
            post_btn.click()
            time.sleep(6)

            print("✅ ¡Tuit publicado con éxito en X!")
            browser.close()
            return True
    except Exception as e:
        print(f"❌ Error en la automatización web de X: {e}")
        return False

def main():
    print("⚡ Iniciando automatización web avanzada de CholloVolt...")
    chollo = obtener_siguiente_chollo()

    asin = chollo["asin"]
    enlace = generar_enlace_afiliado(asin)
    texto_tuit = f"🔥 {chollo['descuento']} de descuento 🔥\n🛒 {enlace}\n\n#chollos #amazon #ofertas #chollo"
    
    ruta_img = crear_imagen_chollo(chollo['descuento'], chollo['precio_actual'], chollo['precio_antes'], "chollo.jpg")
    
    if publicar_en_web_x(ruta_img, texto_tuit):
        guardar_en_historial(asin)

if __name__ == "__main__":
    main()
