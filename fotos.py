"""Fotos de socios y beneficiarios: solo se leen y escriben dentro de MEDIA_ROOT.

Las rutas que vienen de Access apuntan a la computadora vieja
(\\\\DESKTOP-2GPI84A\\Users\\AIO S7100\\Desktop\\FOTOS SICGA\\P043.jpg) o a C:\\Documents and Settings\\...
Nunca se abren: Windows intentaria conectarse por la red a esa maquina (lento, y le enviaria las
credenciales del servidor) y una ruta absoluta permitiria leer cualquier archivo del servidor.
Para traer esas fotos: python manage.py importar_fotos "<copia de la carpeta FOTOS SICGA>".
"""
import re
from pathlib import Path, PurePosixPath, PureWindowsPath

from django.conf import settings


def es_ruta_legada(ruta: str | None) -> bool:
    """True si la ruta es absoluta (unidad, carpeta de red o raiz) o sale de la carpeta con "..": la de Access."""
    if not ruta:
        return False
    w = PureWindowsPath(ruta)
    return bool(w.drive or w.root or PurePosixPath(ruta).is_absolute() or ".." in w.parts)


def ruta_media(relativa: str | None) -> Path | None:
    """Archivo dentro de MEDIA_ROOT para una ruta relativa ("fotos/socios/P041.jpg"), o None."""
    if not relativa or es_ruta_legada(relativa):
        return None
    base = Path(settings.MEDIA_ROOT).resolve()
    ruta = (base / relativa).resolve()
    if not ruta.is_relative_to(base) or not ruta.is_file():
        return None
    return ruta


def relativa_para(carpeta: str, codigo: str, extension: str) -> str:
    """Ruta relativa con la que se guarda la foto: fotos/socios/P041.jpg (sin caracteres de ruta en el codigo)."""
    return f"fotos/{carpeta}/{re.sub(r'[^A-Za-z0-9_-]', '_', codigo)}{extension.lower()}"


# -----------------------------------------------------------------------------
# Fotos subidas desde la ficha: solo imágenes reales, re-codificadas y sin metadatos
# -----------------------------------------------------------------------------

MAX_BYTES = 8 * 1024 * 1024
MAX_LADO = 1600                       # una foto de carnet no necesita más; reduce peso y datos guardados
FORMATOS = {"JPEG", "PNG", "GIF", "BMP", "WEBP"}


class FotoInvalida(Exception):
    pass


def normalizar(archivo) -> tuple[bytes, str]:
    """Foto subida desde la ficha (archivo de Django). Ver normalizar_bytes."""
    if archivo.size > MAX_BYTES:
        raise FotoInvalida("La imagen supera 8 MB.")
    return normalizar_bytes(archivo.read())


def normalizar_bytes(datos: bytes) -> tuple[bytes, str]:
    """Comprueba que sea de verdad una imagen (por su contenido, no por el nombre) y la vuelve a guardar desde
    cero: así no se guarda nada escondido en el archivo y se quitan los metadatos (EXIF con la ubicación GPS, el
    modelo del teléfono, la fecha...). Devuelve (bytes, extensión)."""
    import io
    import warnings

    from PIL import Image, ImageOps, UnidentifiedImageError

    if len(datos) > MAX_BYTES:
        raise FotoInvalida("La imagen supera 8 MB.")
    Image.MAX_IMAGE_PIXELS = 40_000_000                 # imágenes «bomba» (enormes al descomprimir)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(datos)) as prueba:
                if prueba.format not in FORMATOS:
                    raise FotoInvalida("Formato no admitido (use JPG o PNG).")
                prueba.verify()
            imagen = Image.open(io.BytesIO(datos))
            imagen.load()
    except FotoInvalida:
        raise
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning, OSError,
            SyntaxError, ValueError):
        raise FotoInvalida("El archivo no es una imagen válida (use JPG o PNG).")
    imagen = ImageOps.exif_transpose(imagen)            # respeta la orientación antes de quitar el EXIF
    imagen.thumbnail((MAX_LADO, MAX_LADO))
    salida = io.BytesIO()
    transparente = imagen.mode in ("RGBA", "LA") or (imagen.mode == "P" and "transparency" in imagen.info)
    if transparente:
        imagen.convert("RGBA").save(salida, format="PNG", optimize=True)
        return salida.getvalue(), ".png"
    imagen.convert("RGB").save(salida, format="JPEG", quality=88, optimize=True)
    return salida.getvalue(), ".jpg"


# -----------------------------------------------------------------------------
# Fotos del sistema anterior (carpeta FOTOS SICGA de Access)
# -----------------------------------------------------------------------------
# En Access el botón de la foto la guardaba como <código>.jpg en una carpeta compartida de otra computadora
# (\\DESKTOP-2GPI84A\Users\AIO S7100\Desktop\FOTOS SICGA). Aquí cada foto se busca por el código exacto del socio
# o beneficiario (P043.jpg, P423-4.jpg) y queda dentro de MEDIA_ROOT, sin depender de la red. Las rutas guardadas en
# Access NO se usan: están copiadas mal en muchos casos (151 socios apuntan a P01.jpg) y darían a uno la foto de otro.

EXTENSIONES = (".jpg", ".jpeg", ".png", ".bmp", ".gif", ".webp")
MAX_ARCHIVOS_ZIP = 5000
MAX_ZIP_DESCOMPRIMIDO = 1024 * 1024 * 1024


def _nombre(ruta: str) -> PurePosixPath:
    """Solo el nombre del archivo, sin carpetas (en un .zip pueden venir con / o \\)."""
    return PurePosixPath(PurePosixPath(ruta.replace("\\", "/")).name)


def _prioridad(nombre: PurePosixPath) -> int:
    return EXTENSIONES.index(nombre.suffix.lower()) if nombre.suffix.lower() in EXTENSIONES else len(EXTENSIONES)


def leer_zip(ruta_zip) -> dict[str, bytes]:
    """Imágenes de un .zip de la carpeta FOTOS SICGA: {CÓDIGO: bytes}. Solo cuenta el nombre de cada archivo (no se
    crean carpetas ni rutas del .zip en el disco) y se descartan los demasiado grandes."""
    import zipfile

    try:
        z = zipfile.ZipFile(ruta_zip)
    except (zipfile.BadZipFile, OSError):
        raise FotoInvalida("El archivo no es un .zip válido.")
    with z:
        entradas = [e for e in z.infolist() if not e.is_dir()]
        if len(entradas) > MAX_ARCHIVOS_ZIP or sum(e.file_size for e in entradas) > MAX_ZIP_DESCOMPRIMIDO:
            raise FotoInvalida("El .zip es demasiado grande (máximo 5.000 archivos y 1 GB).")
        archivos: dict[str, bytes] = {}
        for e in sorted(entradas, key=lambda e: _prioridad(_nombre(e.filename))):
            nombre = _nombre(e.filename)
            if nombre.suffix.lower() not in EXTENSIONES or e.file_size > MAX_BYTES:
                continue
            codigo = nombre.stem.strip().upper()
            if codigo and codigo not in archivos:
                try:
                    archivos[codigo] = z.read(e)
                except (zipfile.BadZipFile, OSError, ValueError):
                    continue                      # archivo dañado dentro del .zip: se informa como sin foto
    return archivos


def leer_carpeta(carpeta: Path) -> dict[str, bytes]:
    """Imágenes de una carpeta (copia de FOTOS SICGA): {CÓDIGO: bytes}."""
    archivos: dict[str, bytes] = {}
    for p in sorted((p for p in carpeta.iterdir() if p.is_file() and p.suffix.lower() in EXTENSIONES),
                    key=lambda p: _prioridad(PurePosixPath(p.name))):
        if p.stat().st_size <= MAX_BYTES:
            archivos.setdefault(p.stem.strip().upper(), p.read_bytes())
    return archivos


def importar(archivos: dict[str, bytes], reemplazar: bool = False, simular: bool = False) -> dict:
    """Pone cada foto en su socio (con titular) o beneficiario, revisada y sin metadatos. No toca a quien ya tiene
    una foto cargada en el sistema nuevo, salvo reemplazar=True."""
    from . import models as m

    base = Path(settings.MEDIA_ROOT).resolve()
    resumen: dict = {"imagenes": len(archivos), "simulacion": simular}
    usados: set[str] = set()
    for consulta, carpeta in ((m.Socio.objects.filter(nombres__isnull=False), "socios"),
                              (m.Beneficiario.objects.all(), "beneficiarios")):
        importadas, ya_tenian, invalidas = 0, 0, []
        for obj in consulta.order_by("codigo"):
            codigo = obj.codigo.strip().upper()
            datos = archivos.get(codigo)
            if datos is None:
                continue
            usados.add(codigo)
            if obj.ruta_foto and not es_ruta_legada(obj.ruta_foto) and not reemplazar:
                ya_tenian += 1
                continue
            try:
                limpia, extension = normalizar_bytes(datos)
            except FotoInvalida as e:
                invalidas.append(f"{codigo}: {e}")
                continue
            if not simular:
                relativa = relativa_para(carpeta, obj.codigo, extension)
                destino = (base / relativa).resolve()
                if not destino.is_relative_to(base):
                    continue
                destino.parent.mkdir(parents=True, exist_ok=True)
                destino.write_bytes(limpia)
                type(obj).objects.filter(pk=obj.pk).update(ruta_foto=relativa)
            importadas += 1
        resumen[carpeta] = {"importadas": importadas, "ya_tenian": ya_tenian, "invalidas": invalidas[:50]}
    sin_dueno = sorted(set(archivos) - usados)
    resumen["sin_dueno"] = len(sin_dueno)
    resumen["ejemplos_sin_dueno"] = sin_dueno[:30]
    return resumen
