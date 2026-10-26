import os
import re
import json
import time
import random
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
import tweepy

# ==============================================================================
# CONFIGURACIÓN Y VARIABLES DE ENTORNO (DESDE GITHUB SECRETS)
# ==============================================================================
AMAZON_TAG = os.getenv("AMAZON_TAG", "librospsico08-21")
AMAZON_COOKIE = os.getenv("AMAZON_COOKIE", "")

# Credenciales de X (Twitter) API v2 + v1.1 (para subir imágenes)
X_API_KEY = os.getenv("X_API_KEY", "")
X_API_SECRET = os.getenv("X_API_SECRET", "")
X_ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN", "")
X_ACCESS_TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET", "")

# Credenciales de Telegram (Opcional)
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")

ARCHIVO_HISTORIAL = "historial_publicados.txt"

HEADERS_WEB = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8"
}


# ==============================================================================
# 1. GESTIÓN DE HISTORIAL (EVITAR DUPLICADOS)
# ==============================================================================
def cargar_historial():
    if not os.path.exists(ARCHIVO_HISTORIAL):
        return set()
    with open(ARCHIVO_HISTORIAL, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())


def guardar_en_historial(asin: str):
    with open(ARCHIVO_HISTORIAL, "a", encoding="utf-8") as f:
        f.write(f"{asin}\n")


# ==============================================================================
# 2. SCRAPER DE SAVEMONEY.ES Y EXTRACCIÓN DE DATOS DE AMAZON
# ==============================================================================
def resolver_asin_desde_url(url: str):
    """Sigue redirecciones si es necesario y extrae el ASIN de 10 caracteres."""
    match = re.search(r"(?:/dp/|/gp/product/|/exec/obidos/ASIN/|asin=)([A-Z0-9]{10})", url, re.I)
    if match:
        return match.group(1).upper()
    try:
        r = requests.get(url, headers=HEADERS_WEB, allow_redirects=True, timeout=12)
        match = re.search(r"(?:/dp/|/gp/product/|/exec/obidos/ASIN/)([A-Z0-9]{10})", r.url, re.I)
        if match:
            return match.group(1).upper()
    except Exception:
        pass
    return None


def obtener_datos_reales_amazon(asin: str):
    """Consulta la ficha de Amazon.es para sacar descuento y precios exactos."""
    url = f"https://www.amazon.es/dp/{asin}"
    try:
        r = requests.get(url, headers=HEADERS_WEB, timeout=12)
        if r.status_code != 200:
            return None
        soup = BeautifulSoup(r.text, "lxml")

        # Descuento (ej. -45%)
        desc_el = soup.select_one("span.savingsPercentage")
        descuento = desc_el.get_text(strip=True) if desc_el else None

        # Precio actual
        entero_el = soup.select_one("span.a-price-whole")
        fraccion_el = soup.select_one("span.a-price-fraction")
        if entero_el:
            entero = re.sub(r"[^\d]", "", entero_el.get_text(strip=True))
            fraccion = re.sub(r"[^\d]", "", fraccion_el.get_text(strip=True)) if fraccion_el else "00"
            precio_actual = f"{entero},{fraccion}"
        else:
            precio_actual = None

        # Precio recomendado / anterior
        antes_el = soup.select_one("span.a-text-price span.a-offscreen")
        if antes_el:
            precio_antes = antes_el.get_text(strip=True).replace("€", "").strip()
        else:
            precio_antes = None

        if precio_actual and (descuento or precio_antes):
            # Si falta el porcentaje pero tenemos ambos precios, lo calculamos
            if not descuento and precio_antes:
                try:
                    p_act = float(precio_actual.replace(".", "").replace(",", "."))
                    p_ant = float(precio_antes.replace(".", "").replace(",", "."))
                    if p_ant > p_act:
                        pct = int(round((1 - (p_act / p_ant)) * 100))
                        descuento = f"-{pct} %"
                except Exception:
                    descuento = "-35 %"
            return {
                "asin": asin,
                "descuento": descuento or "-40 %",
                "precio_actual": precio_actual,
                "precio_antes": precio_antes or "49,90"
            }
    except Exception as e:
        print(f"Error leyendo ficha Amazon {asin}: {e}")
    return None


def rastrear_chollos_savemoney():
    """Rastrea la sección de chollos de SaveMoney.es y devuelve candidatos nuevos."""
    urls_fuente = [
        "https://savemoney.es/chollos",
        "https://savemoney.es/"
    ]
    historial = cargar_historial()
    asins_encontrados = []

    for url in urls_fuente:
        try:
            r = requests.get(url, headers=HEADERS_WEB, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "lxml")

            # Buscamos cualquier enlace o imagen que contenga un ASIN de Amazon
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if any(k in href.lower() for k in ["amazon.", "amzn.to", "/dp/", "asin"]):
                    asin = resolver_asin_desde_url(href)
                    if asin and asin not in historial and asin not in asins_encontrados:
                        asins_encontrados.append(asin)

            # También buscamos ASINs en imágenes de producto de Amazon (habitual en SaveMoney)
            for img in soup.find_all("img", src=True):
                match = re.search(r"/images/P/([A-Z0-9]{10})\.", img["src"], re.I)
                if match:
                    asin = match.group(1).upper()
                    if asin not in historial and asin not in asins_encontrados:
                        asins_encontrados.append(asin)
        except Exception as e:
            print(f"Error accediendo a {url}: {e}")

    print(f"Detectados {len(asins_encontrados)} posibles ASINs nuevos en SaveMoney.")

    for asin in asins_encontrados:
        datos = obtener_datos_reales_amazon(asin)
        if datos:
            return datos
        time.sleep(2)

    return None


# ==============================================================================
# 3. ACORTADOR OFICIAL DE AMAZON Y ENLACE DE AFILIADO
# ==============================================================================
def generar_enlace_afiliado(asin: str):
    url_larga = f"https://www.amazon.es/dp/{asin}?tag={AMAZON_TAG}"

    if not AMAZON_COOKIE.strip():
        return url_larga

    cookies_dict = {}
    for item in AMAZON_COOKIE.split(";"):
        if "=" in item:
            k, v = item.strip().split("=", 1)
            cookies_dict[k] = v

    headers = {
        "User-Agent": HEADERS_WEB["User-Agent"],
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Referer": url_larga,
        "X-Requested-With": "XMLHttpRequest"
    }

    endpoints = [
        {
            "url": "https://www.amazon.es/associates/sitestripe/getShortUrl",
            "params": {
                "longUrl": url_larga,
                "marketplaceId": "44551",
                "storeId": AMAZON_TAG,
                "trackingId": AMAZON_TAG
            }
        }
    ]

    for ep in endpoints:
        try:
            resp = requests.get(ep["url"], params=ep["params"], headers=headers, cookies=cookies_dict, timeout=10)
            if resp.status_code == 200:
                match = re.search(r"https://(?:link\.amazon|amzn\.to)/[A-Za-z0-9]+", resp.text)
                if match:
                    return match.group(0)
        except Exception:
            pass

    return url_larga


# ==============================================================================
# 4. GENERADOR DE LA TARJETA VISUAL DE DESCUENTO (ESTILO CHOLLOVOLT)
# ==============================================================================
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

    color_rojo = (204, 12, 57)
    draw.text((45, 50), descuento, font=f_gigante, fill=color_rojo)
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

    texto_sin_precio = "Precio recomendado: "
    bbox_sp = draw.textbbox((45, 230), texto_sin_precio, font=f_gris)
    bbox_total = draw.textbbox((45, 230), texto_antes, font=f_gris)
    y_linea = (bbox_total[1] + bbox_total[3]) // 2 + 2
    draw.line([(bbox_sp[2], y_linea), (bbox_total[2], y_linea)], fill=(86, 89, 89), width=3)

    # Sello Prime
    draw.line([(48, 348), (62, 366), (92, 326)], fill=(255, 153, 0), width=9)
    draw.text((98, 315), "prime", font=f_prime, fill=(0, 113, 197))

    draw.rectangle([(2, 2), (W - 3, H - 3)], outline=(225, 228, 228), width=3)
    img.save(ruta_salida, quality=95)
    return ruta_salida


# ==============================================================================
# 5. PUBLICACIÓN AUTOMÁTICA EN X (TWITTER) Y TELEGRAM
# ==============================================================================
def publicar_en_x(ruta_imagen: str, texto_tuit: str):
    if not all([X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET]):
        print("⚠️ Faltan las claves de API de X en las variables de entorno.")
        return False

    try:
        # API v1.1 para subir el archivo de imagen
        auth = tweepy.OAuth1UserHandler(X_API_KEY, X_API_SECRET, X_ACCESS_TOKEN, X_ACCESS_TOKEN_SECRET)
        api_v1 = tweepy.API(auth)
        media = api_v1.media_upload(filename=ruta_imagen)

        # API v2 para publicar el tuit con la imagen adjunta
        client_v2 = tweepy.Client(
            consumer_key=X_API_KEY,
            consumer_secret=X_API_SECRET,
            access_token=X_ACCESS_TOKEN,
            access_token_secret=X_ACCESS_TOKEN_SECRET
        )
        resp = client_v2.create_tweet(text=texto_tuit, media_ids=[media.media_id])
        print(f"✅ Tuit publicado en @CholloVolt con éxito! ID: {resp.data['id']}")
        return True
    except Exception as e:
        print(f"❌ Error publicando en X: {e}")
        return False


def publicar_en_telegram(ruta_imagen: str, texto_post: str):
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return False
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto"
    with open(ruta_imagen, "rb") as f:
        r = requests.post(url, data={"chat_id": TG_CHAT_ID, "caption": texto_post}, files={"photo": f}, timeout=20)
    return r.status_code == 200


# ==============================================================================
# FLUJO PRINCIPAL
# ==============================================================================
def main():
    print("⚡ Iniciando barrido automático de CholloVolt...")
    chollo = rastrear_chollos_savemoney()

    if not chollo:
        print("No se encontraron chollos nuevos en esta pasada.")
        return

    asin = chollo["asin"]
    descuento = chollo["descuento"]
    precio_actual = chollo["precio_actual"]
    precio_antes = chollo["precio_antes"]

    enlace_final = generar_enlace_afiliado(asin)
    ruta_img = "tarjeta_chollo.jpg"
    crear_imagen_chollo(descuento, precio_actual, precio_antes, ruta_img)

    texto_tuit = f"🔥 {descuento} 🔥\n{enlace_final}\n\n#chollos #amazon #chollo #ofertas"
    print(f"Preparado chollo ASIN {asin}:\n{texto_tuit}")

    publicado_x = publicar_en_x(ruta_img, texto_tuit)
    publicar_en_telegram(ruta_img, texto_tuit)

    if publicado_x:
        guardar_en_historial(asin)


if __name__ == "__main__":
    main()
