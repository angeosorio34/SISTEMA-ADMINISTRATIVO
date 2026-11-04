"""
Motor que reproduce informes de Microsoft Access en PDF.

Lee el diseno exportado con `Application.SaveAsText acReport` (ver
plantillas/LEEME.md), y dibuja cada control (etiquetas, cuadros de texto,
rectangulos, lineas, casillas, logotipos OLE) en las mismas coordenadas, fuentes
y formatos que usaba Access. Asi los documentos impresos (recibo de pago,
solvencia, comprobante de egreso, cheque) salen identicos a los del sistema viejo.

Unidades: Access trabaja en twips (1/1440 pulgada); PDF en puntos (1/72) -> /20.

Uso:
    informe = InformeAccess.cargar("FacturaCons")
    pdf_bytes = informe.render([registro1, registro2])     # una pagina por registro

Cada registro es un `Registro(campos={...}, subinformes={...})`. Las expresiones
`[Forms]![Formulario]![Campo]` se resuelven contra `campos["Campo"]`, igual que
un campo de la tabla origen.
"""
from __future__ import annotations

import io
import re
import struct
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

PLANTILLAS = Path(__file__).resolve().parent / "plantillas"

# Tamanos de papel de DEVMODE (dmPaperSize) en twips (ancho, alto) vertical
PAPELES = {1: (12240, 15840), 5: (12240, 20160), 6: (7920, 12240), 9: (11906, 16838)}

MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre"]
DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


# =============================================================================
# Fuentes: las mismas de Windows que usaba Access; si no existen, Helvetica
# =============================================================================

DIR_FUENTES = [Path("C:/Windows/Fonts"), Path("/usr/share/fonts/truetype/msttcorefonts"),
               Path("/usr/share/fonts/truetype/crosextra"), Path("/usr/share/fonts/truetype/liberation")]
ARCHIVOS_FUENTE = {
    # nombre Access: (normal, negrita, cursiva, negrita cursiva) + alternativas metricas libres
    "calibri": (["calibri.ttf", "Carlito-Regular.ttf"], ["calibrib.ttf", "Carlito-Bold.ttf"],
                ["calibrii.ttf", "Carlito-Italic.ttf"], ["calibriz.ttf", "Carlito-BoldItalic.ttf"]),
    "arial": (["arial.ttf", "LiberationSans-Regular.ttf"], ["arialbd.ttf", "LiberationSans-Bold.ttf"],
              ["ariali.ttf", "LiberationSans-Italic.ttf"], ["arialbi.ttf", "LiberationSans-BoldItalic.ttf"]),
    "arial narrow": (["ARIALN.TTF", "LiberationSansNarrow-Regular.ttf"],
                     ["ARIALNB.TTF", "LiberationSansNarrow-Bold.ttf"],
                     ["ARIALNI.TTF", "LiberationSansNarrow-Italic.ttf"],
                     ["ARIALNBI.TTF", "LiberationSansNarrow-BoldItalic.ttf"]),
    "times new roman": (["times.ttf", "LiberationSerif-Regular.ttf"], ["timesbd.ttf", "LiberationSerif-Bold.ttf"],
                        ["timesi.ttf", "LiberationSerif-Italic.ttf"], ["timesbi.ttf", "LiberationSerif-BoldItalic.ttf"]),
}
BASE14 = {False: {False: "Helvetica", True: "Helvetica-Oblique"}, True: {False: "Helvetica-Bold", True: "Helvetica-BoldOblique"}}


@lru_cache(maxsize=None)
def fuente(nombre: str, negrita: bool, cursiva: bool) -> str:
    variantes = ARCHIVOS_FUENTE.get((nombre or "").lower())
    if variantes:
        for archivo in variantes[(2 if cursiva else 0) + (1 if negrita else 0)]:
            for d in DIR_FUENTES:
                ruta = d / archivo
                if ruta.exists():
                    alias = f"{nombre}-{int(negrita)}{int(cursiva)}"
                    pdfmetrics.registerFont(TTFont(alias, str(ruta)))
                    return alias
    return BASE14[negrita][cursiva]


# =============================================================================
# Parser del formato SaveAsText
# =============================================================================

@dataclass
class Nodo:
    tipo: str
    props: dict = field(default_factory=dict)
    hijos: list = field(default_factory=list)


def parsear(texto: str) -> Nodo:
    lineas = texto.splitlines()
    i = 0

    def bloque(tipo: str) -> Nodo:
        nonlocal i
        nodo = Nodo(tipo)
        while i < len(lineas):
            linea = lineas[i].strip()
            i += 1
            if not linea:
                continue
            if linea == "End":
                return nodo
            if linea == "Begin" or linea.startswith("Begin "):
                nodo.hijos.append(bloque(linea[6:].strip()))
                continue
            m = re.match(r"^(\w+)\s*=\s*Begin$", linea)
            if m:  # valor multilinea: binario (OleData, PrtMip) o macro incrustada con Begin/End anidados
                valores, profundidad = [], 1
                while i < len(lineas):
                    actual = lineas[i].strip()
                    i += 1
                    if actual == "End":
                        profundidad -= 1
                        if profundidad == 0:
                            break
                    elif actual == "Begin" or actual.startswith("Begin ") or re.match(r"^\w+\s*=\s*Begin$", actual):
                        profundidad += 1
                    valores.append(actual)
                nodo.props[m.group(1)] = valores
                continue
            m = re.match(r"^(\w+)\s*=(.*)$", linea)
            if m:
                clave, valor = m.group(1), m.group(2).strip()
                if len(valor) >= 2 and valor[0] == valor[-1] == '"':
                    # Las cadenas largas siguen en lineas que empiezan por comillas
                    while i < len(lineas) and lineas[i].strip().startswith('"'):
                        valor = valor[:-1] + lineas[i].strip()[1:]
                        i += 1
                    valor = valor[1:-1].replace('\\"', '"')   # SaveAsText escapa las comillas internas
                elif re.fullmatch(r"-?\d+", valor):
                    valor = int(valor)
                elif re.fullmatch(r"-?\d+\.\d+", valor):
                    valor = float(valor)
                nodo.props[clave] = valor
        return nodo

    raiz = bloque("ROOT")
    return next(h for h in raiz.hijos if h.tipo == "Report")


def binario(valor) -> bytes:
    if not isinstance(valor, list):
        return b""
    return bytes.fromhex("".join(v.replace("0x", "").replace(",", "").strip() for v in valor))


def imagen_ole(props: dict) -> bytes | None:
    """Extrae el mapa de bits de un objeto OLE 'Paint' o de un control Imagen."""
    datos = binario(props.get("OleData") or props.get("PictureData"))
    if not datos:
        return None
    for firma in (b"\x89PNG", b"\xff\xd8\xff"):
        i = datos.find(firma)
        if i >= 0:
            return datos[i:]
    i = datos.find(b"BM")
    while i >= 0:
        tam = struct.unpack("<I", datos[i + 2:i + 6])[0]
        if 54 < tam <= len(datos) - i and struct.unpack("<I", datos[i + 14:i + 18])[0] in (12, 40, 108, 124):
            return datos[i:i + tam]
        i = datos.find(b"BM", i + 2)
    if datos[:4] == b"\x28\x00\x00\x00":  # DIB sin cabecera de archivo
        return b"BM" + struct.pack("<IHHI", len(datos) + 14, 0, 0, 14 + 40) + datos
    return None


# =============================================================================
# Expresiones de Access
# =============================================================================

TOKEN = re.compile(r'\s*(?:(\[[^\]]*\])|("(?:[^"]|"")*")|(\d+(?:\.\d+)?)|([A-Za-z_][\w$]*)|(<>|<=|>=|[-+*/&=<>(),!.]))')


class Expresion:
    """Interprete minimo de expresiones de Access (lo que usan los informes de SICGA)."""

    def __init__(self, texto: str):
        self.tokens = []
        pos = 0
        texto = texto.strip()
        while pos < len(texto):
            m = TOKEN.match(texto, pos)
            if not m or m.end() == pos:
                raise ValueError(f"Expresion no soportada: {texto!r}")
            corchete, cadena, numero, ident, op = m.groups()
            if corchete is not None:
                self.tokens.append(("nombre", corchete[1:-1]))
            elif cadena is not None:
                self.tokens.append(("cadena", cadena[1:-1].replace('""', '"')))
            elif numero is not None:
                self.tokens.append(("numero", Decimal(numero)))
            elif ident is not None:
                self.tokens.append(("ident", ident))
            else:
                self.tokens.append(("op", op))
            pos = m.end()
        self.i = 0
        self.arbol = self._comparacion()

    # --- parser recursivo descendente -> arbol de tuplas ---
    def _ver(self, *ops):
        return self.i < len(self.tokens) and self.tokens[self.i][0] == "op" and self.tokens[self.i][1] in ops

    def _comparacion(self):
        izq = self._concat()
        while self._ver("=", "<>", "<", ">", "<=", ">="):
            op = self.tokens[self.i][1]; self.i += 1
            izq = ("cmp", op, izq, self._concat())
        return izq

    def _concat(self):
        izq = self._suma()
        while self._ver("&"):
            self.i += 1
            izq = ("&", izq, self._suma())
        return izq

    def _suma(self):
        izq = self._producto()
        while self._ver("+", "-"):
            op = self.tokens[self.i][1]; self.i += 1
            izq = (op, izq, self._producto())
        return izq

    def _producto(self):
        izq = self._unario()
        while self._ver("*", "/"):
            op = self.tokens[self.i][1]; self.i += 1
            izq = (op, izq, self._unario())
        return izq

    def _unario(self):
        if self._ver("-"):
            self.i += 1
            return ("neg", self._unario())
        return self._primario()

    def _primario(self):
        tipo, valor = self.tokens[self.i]
        self.i += 1
        if tipo == "op" and valor == "*":            # Count(*)
            return ("lit", "*")
        if tipo in ("numero", "cadena"):
            return ("lit", valor)
        if tipo == "op" and valor == "(":
            nodo = self._comparacion()
            self.i += 1  # ')'
            return nodo
        if tipo == "ident" and self._ver("("):
            self.i += 1
            args = []
            if not self._ver(")"):
                args.append(self._comparacion())
                while self._ver(","):
                    self.i += 1
                    args.append(self._comparacion())
            self.i += 1  # ')'
            return ("fn", valor.rstrip("$").lower(), args)
        if tipo == "ident" and valor.lower() in ("true", "false", "null"):
            return ("lit", {"true": True, "false": False, "null": None}[valor.lower()])
        if tipo == "ident" and valor.lower() == "date" and not self._ver("!", "."):
            return ("fn", "date", [])
        ruta = [valor]
        while self._ver("!", "."):
            self.i += 1
            ruta.append(self.tokens[self.i][1])
            self.i += 1
        return ("ref", ruta)

    # --- evaluacion ---
    def evaluar(self, ctx: "Contexto"):
        return self._ev(self.arbol, ctx)

    def _ev(self, n, ctx):
        t = n[0]
        if t == "lit":
            return n[1]
        if t == "ref":
            return ctx.referencia(n[1])
        if t == "neg":
            v = self._ev(n[1], ctx)
            return None if v is None else -_num(v)
        if t == "&":
            return f"{_texto(self._ev(n[1], ctx))}{_texto(self._ev(n[2], ctx))}"
        if t in "+-*/":
            a, b = self._ev(n[1], ctx), self._ev(n[2], ctx)
            if a is None or b is None:
                return None
            if t == "+" and (isinstance(a, str) or isinstance(b, str)):
                return f"{a}{b}"
            try:
                a, b = _num(a), _num(b)
                return {"+": a + b, "-": a - b, "*": a * b, "/": a / b if b else None}[t]
            except (TypeError, ArithmeticError):
                return None   # en Access seria #Error: el campo queda vacio y el informe sigue
        if t == "cmp":
            a, b = self._ev(n[2], ctx), self._ev(n[3], ctx)
            if a is None or b is None:                 # en Access, comparar con Null da Null (falso)
                return None
            if isinstance(a, (bool, int, float, Decimal)) and isinstance(b, (bool, int, float, Decimal)):
                a, b = _num(a), _num(b)                # True = -1, como en Access
            elif isinstance(a, str) != isinstance(b, str):
                a, b = _texto(a), _texto(b)
            return {"=": a == b, "<>": a != b, "<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b}[n[1]]
        if t == "fn":
            return self._funcion(n[1], n[2], ctx)
        raise ValueError(t)

    def _funcion(self, nombre, args, ctx):
        if nombre == "iif":   # IIf sin parte "si no" devuelve Null, como en Access
            rama = args[1] if self._ev(args[0], ctx) else (args[2] if len(args) > 2 else None)
            return None if rama is None else self._ev(rama, ctx)
        v = [self._ev(a, ctx) for a in args]
        if nombre in ("sum", "count", "avg", "min", "max"):
            return ctx.agregado(nombre, args[0] if args else None)
        if nombre == "iserror":
            return False  # los valores ausentes ya llegan como None
        if nombre == "isnull":
            return v[0] is None
        if nombre == "nz":
            return v[0] if v[0] is not None else (v[1] if len(v) > 1 else 0)
        if nombre in ("right", "left", "mid"):
            s = _texto(v[0])
            if nombre == "right":
                return s[-int(v[1]):] if int(v[1]) else ""
            if nombre == "left":
                return s[:int(v[1])]
            inicio = int(v[1]) - 1
            return s[inicio:inicio + int(v[2])] if len(v) > 2 else s[inicio:]
        if nombre == "cstr":
            return _texto(v[0])
        if nombre in ("ucase", "lcase", "trim"):
            s = _texto(v[0])
            return {"ucase": s.upper(), "lcase": s.lower(), "trim": s.strip()}[nombre]
        if nombre in ("date", "now"):
            return date.today()
        if nombre == "format":
            return formatear(v[0], _texto(v[1]) if len(v) > 1 else None)
        if nombre in ("int", "fix"):
            return None if v[0] is None else int(_num(v[0]))
        if nombre in ("month", "day", "year", "weekday"):
            f = v[0]
            if not isinstance(f, date):
                return None
            return {"month": f.month, "day": f.day, "year": f.year, "weekday": (f.weekday() + 1) % 7 + 1}[nombre]
        if nombre == "monthname":
            return MESES[int(v[0]) - 1] if v[0] else ""
        if nombre == "len":
            return len(_texto(v[0]))
        if nombre in ("val", "cdbl", "ccur", "clng", "cint", "csng"):
            try:
                return _num(v[0]) if v[0] not in (None, "") else Decimal(0)
            except Exception:
                return Decimal(0)
        if nombre == "abs":
            return None if v[0] is None else abs(_num(v[0]))
        if nombre == "round":
            return None if v[0] is None else round(_num(v[0]), int(v[1]) if len(v) > 1 else 0)
        if nombre == "space":
            return " " * int(v[0] or 0)
        # Funcion desconocida: el informe se imprime igual, ese campo queda vacio
        return None


def _orden(v):
    """Clave de ordenacion: los vacios primero (como Access) y sin mezclar tipos."""
    if v is None or v == "":
        return (0, "")
    if isinstance(v, (int, float, Decimal, bool)):
        return (1, _num(v))
    if isinstance(v, (date, datetime)):
        return (2, v.isoformat())
    return (3, str(v).upper())


def _num(v):
    if isinstance(v, bool):
        return Decimal(-1 if v else 0)
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    if isinstance(v, str):
        return Decimal(v.replace(".", "").replace(",", ".") or 0)
    return v


def _texto(v) -> str:
    return "" if v is None else formatear(v, None)


# =============================================================================
# Formatos (configuracion regional de Venezuela: 1.234,56)
# =============================================================================

def _numero(valor: Decimal, decimales: int, miles: bool) -> str:
    texto = f"{abs(valor):,.{decimales}f}" if miles else f"{abs(valor):.{decimales}f}"
    return texto.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _patron_numerico(patron: str, valor: Decimal) -> str:
    """'"Bs "#,##0.00' -> 'Bs 1.234,56'. Texto literal entre comillas o con \\."""
    salida, i, numero_hecho = [], 0, False
    decimales = len(re.match(r"[0#]*", patron.split(".", 1)[1]).group(0)) if "." in patron else 0
    while i < len(patron):
        c = patron[i]
        if c == '"':
            j = patron.index('"', i + 1)
            salida.append(patron[i + 1:j]); i = j + 1
        elif c == "\\":
            salida.append(patron[i + 1:i + 2]); i += 2
        elif c in "#0,.":
            if not numero_hecho:
                salida.append(_numero(valor, decimales, "," in patron))
                numero_hecho = True
            i += 1
        else:
            salida.append(c); i += 1
    return "".join(salida)


def formatear(valor, formato: str | None) -> str:
    if valor is None or valor == "":
        return ""
    if isinstance(valor, bool):
        return "Sí" if valor else "No"
    if isinstance(valor, datetime):
        valor = valor.date()
    if isinstance(valor, date):
        f = (formato or "").lower()
        if f == "long date":
            return f"{DIAS[valor.weekday()]}, {valor.day} de {MESES[valor.month - 1]} de {valor.year}"
        if f == "medium date":
            return f"{valor.day:02d}-{MESES[valor.month - 1][:3]}-{valor.year % 100:02d}"
        return valor.strftime("%d/%m/%Y")
    if isinstance(valor, (int, float, Decimal)):
        valor = _num(valor)
        f = formato or ""
        if f.lower() in ("standard", "fixed"):
            texto = _numero(valor, 2, f.lower() == "standard")
            return f"-{texto}" if valor < 0 else texto
        if f.lower() == "currency":
            return ("Bs -" if valor < 0 else "Bs ") + _numero(valor, 2, True)
        if f.lower() == "percent":
            return _numero(valor * 100, 2, False) + "%"
        if f and re.search(r"[#0]", f):
            secciones = f.split(";")
            if valor < 0 and len(secciones) > 1:
                return _patron_numerico(secciones[1], valor)
            return _patron_numerico(secciones[0], valor) if valor >= 0 or len(secciones) > 1 else "-" + _patron_numerico(secciones[0], valor)
        # Numero general: sin ceros sobrantes, coma decimal
        texto = format(valor.normalize(), "f") if valor == valor.to_integral() else format(valor, "f").rstrip("0")
        return texto.replace(".", ",")
    return str(valor)


# =============================================================================
# Contexto de evaluacion de un registro
# =============================================================================

class CamposAccess(dict):
    """Diccionario de campos que, como Access, no distingue mayusculas ([IdSocio] = [IDSocio])."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._minus = {str(c).lower(): c for c in self}

    def get(self, clave, defecto=None):
        if dict.__contains__(self, clave):
            return dict.__getitem__(self, clave)
        real = self._minus.get(str(clave).lower())
        return dict.__getitem__(self, real) if real is not None else defecto

    def __contains__(self, clave):
        return dict.__contains__(self, clave) or str(clave).lower() in self._minus


@dataclass
class Registro:
    campos: dict
    subinformes: dict = field(default_factory=dict)   # nombre control -> dict (primera fila)

    def __post_init__(self):
        if not isinstance(self.campos, CamposAccess):
            self.campos = CamposAccess(self.campos)


class Contexto:
    """Evalua controles para un registro. En listados tambien conoce el grupo (para Sum/Count),
    la pagina, los parametros del "formulario" y las sumas acumuladas (RunningSum)."""

    def __init__(self, informe: "InformeAccess", registro: Registro, filas: list | None = None,
                 parametros: dict | None = None, seccion: str = "Section", estado: dict | None = None):
        self.informe, self.registro, self._cache, self._pila = informe, registro, {}, set()
        self.filas = filas if filas is not None else [registro.campos]
        self.parametros = parametros if isinstance(parametros, CamposAccess) else CamposAccess(parametros or {})
        self.seccion = seccion
        self.estado = estado if estado is not None else {"pagina": 1, "paginas": 1, "acumulados": {}}

    def _hijo(self, campos: dict) -> "Contexto":
        return Contexto(self.informe, Registro(campos), [campos], self.parametros, "Section", self.estado)

    def agregado(self, funcion: str, arg):
        valores = []
        for fila in self.filas:
            if arg is None or arg == ("lit", "*"):
                valores.append(1)
                continue
            v = Expresion.__new__(Expresion)._ev(arg, self._hijo(fila))
            if v is not None:
                valores.append(v)
        if funcion == "count":
            return len(valores)
        nums = [_num(v) for v in valores]
        if not nums:
            return None if funcion != "sum" else Decimal(0)
        return {"sum": sum(nums, Decimal(0)), "avg": sum(nums, Decimal(0)) / len(nums),
                "min": min(nums), "max": max(nums)}[funcion]

    def referencia(self, ruta: list[str]):
        campos = self.registro.campos
        nombre = ruta[-1]
        if ruta[0].lower() in ("forms", "formularios") and len(ruta) >= 3:
            return self.parametros.get(nombre, campos.get(nombre))
        if ruta[0].lower() in ("reports", "informes") and len(ruta) >= 3:
            # Valor de otro informe abierto: en Access daba #Error si no estaba abierto (y salia 0)
            return self.parametros.get(f"{ruta[1]}!{nombre}")
        if len(ruta) >= 3 and ruta[1].lower() == "report":   # [Sub].[Report]![Campo]
            return (self.registro.subinformes.get(ruta[0]) or {}).get(nombre)
        if len(ruta) == 2 and ruta[0].upper() in ("SOCIOS", "FACTURAS", "RELSOLV", "MOROSOS", "CHEQUES", "ABONOS"):
            return campos.get(nombre)                          # [TABLA]![Campo]
        if nombre.lower() == "page":
            return self.estado["pagina"]
        if nombre.lower() == "pages":
            return self.estado["paginas"]
        control = self.informe.controles_por_nombre.get(nombre)
        if control is not None:
            # Control de otra seccion con suma acumulada: su ultimo valor (p. ej. contador en el pie)
            if control.props.get("_seccion") != self.seccion and nombre in self.estado["acumulados"]:
                return self.estado["acumulados"][nombre]
            if nombre not in self._pila and control.props.get("ControlSource") != nombre:
                return self.valor(control)
        if nombre in campos:
            return campos.get(nombre)
        return self.parametros.get(nombre)

    def valor(self, control: Nodo):
        nombre = control.props.get("Name")
        if nombre in self._cache:
            return self._cache[nombre]
        origen = control.props.get("ControlSource")
        self._pila.add(nombre)
        try:
            if not origen:
                # Control sin origen: en Access lo rellenaba el VBA; aqui lo calcula el servidor
                v = self.registro.campos.get(nombre)
            elif isinstance(origen, str) and origen.startswith("="):
                v = Expresion(origen[1:]).evaluar(self)
            else:
                v = self.registro.campos.get(origen)
                if v is None and "." in origen:          # "SOCIOS.CI": campo calificado con la tabla
                    v = self.registro.campos.get(origen.split(".")[-1].strip("[]"))
        finally:
            self._pila.discard(nombre)
        if control.props.get("RunningSum") and self.seccion == control.props.get("_seccion") and v is not None:
            acumulados = self.estado["acumulados"]
            acumulados[nombre] = acumulados.get(nombre, Decimal(0)) + _num(v)
            v = acumulados[nombre]
        self._cache[nombre] = v
        return v


# =============================================================================
# Informe
# =============================================================================

TIPOS_CONTROL = {"Label", "TextBox", "Rectangle", "Line", "CheckBox", "Image",
                 "UnboundObjectFrame", "BoundObjectFrame", "Subform", "Subreport", "ComboBox", "OptionGroup"}


def _color(props: dict, clave: str, defecto=(0, 0, 0)):
    v = props.get(clave)
    if not isinstance(v, int) or v < 0:
        return defecto
    return (v & 0xFF) / 255, ((v >> 8) & 0xFF) / 255, ((v >> 16) & 0xFF) / 255   # Access guarda BGR


def _es_verdadero(props: dict, clave: str, defecto: bool) -> bool:
    v = props.get(clave)
    if v is None:
        return defecto
    return not defecto if v == "NotDefault" else bool(v)


@dataclass
class Seccion:
    tipo: str
    alto: int
    controles: list
    props: dict = field(default_factory=dict)
    nombre: str = ""

    @property
    def visible(self) -> bool:
        return self.alto > 0 and _es_verdadero(self.props, "Visible", True)

    @property
    def salto(self) -> int:
        """ForceNewPage: 0 no, 1 antes, 2 despues, 3 antes y despues."""
        return int(self.props.get("ForceNewPage", 0) or 0)


class InformeAccess:
    def __init__(self, raiz: Nodo):
        self.raiz = raiz
        self.props = raiz.props
        self.defectos: dict[str, dict] = {}
        self.secciones: dict[str, Seccion] = {}
        self.encabezados_grupo: list[Seccion] = []
        self.pies_grupo: list[Seccion] = []
        self.niveles: list[dict] = []          # BreakLevel: campo de agrupacion/orden
        self.controles_por_nombre: dict[str, Nodo] = {}
        for hijo in raiz.hijos:
            if hijo.tipo == "":
                for d in hijo.hijos:
                    if d.tipo in TIPOS_CONTROL:
                        self.defectos[d.tipo] = d.props
                    elif d.tipo == "BreakLevel":
                        self.niveles.append(d.props)
                    elif d.tipo in ("PageHeader", "Section", "PageFooter", "FormHeader", "FormFooter",
                                    "BreakHeader", "BreakFooter"):
                        self._seccion(d)

        mip = binario(self.props.get("PrtMip"))
        izq, sup, der, inf = struct.unpack("<4i", mip[:16]) if len(mip) >= 16 else (1440, 1440, 1440, 1440)
        self.margenes = (izq, sup, der, inf)
        dev = binario(self.props.get("PrtDevModeW"))
        orientacion, papel = struct.unpack("<2h", dev[76:80]) if len(dev) >= 80 else (1, 1)
        ancho, alto = PAPELES.get(papel, PAPELES[1])
        self.pagina = (alto, ancho) if orientacion == 2 else (ancho, alto)

    def _seccion(self, nodo: Nodo):
        controles = []

        def recoger(n: Nodo):
            for h in n.hijos:
                if h.tipo in TIPOS_CONTROL:
                    props = {**self.defectos.get(h.tipo, {}), **h.props}
                    control = Nodo(h.tipo, props)
                    controles.append(control)
                    if props.get("Name"):
                        self.controles_por_nombre[props["Name"]] = control
                recoger(h)

        recoger(nodo)
        seccion = Seccion(nodo.tipo, nodo.props.get("Height", 0), controles, nodo.props)
        for ctl in controles:
            ctl.props["_seccion"] = nodo.props.get("Name", nodo.tipo)
        seccion.nombre = nodo.props.get("Name", nodo.tipo)
        if nodo.tipo == "BreakHeader":
            self.encabezados_grupo.append(seccion)
        elif nodo.tipo == "BreakFooter":
            self.pies_grupo.append(seccion)
        else:
            self.secciones[nodo.tipo] = seccion

    @classmethod
    @lru_cache(maxsize=None)
    def cargar(cls, nombre: str) -> "InformeAccess":
        return cls(parsear((PLANTILLAS / f"{nombre}.txt").read_text(encoding="utf-8")))

    # ------------------------------------------------------------------ dibujo
    def render(self, registros: list[Registro], titulo: str = "") -> bytes:
        buffer = io.BytesIO()
        ancho, alto = self.pagina
        c = canvas.Canvas(buffer, pagesize=(ancho / 20, alto / 20))
        c.setTitle(titulo or self.props.get("Caption", "Informe"))
        c.setAuthor("SICGA - Centro Gallego de Puerto La Cruz")
        izq, sup, _, inf = self.margenes
        for registro in registros:
            y = sup
            for tipo in ("FormHeader", "PageHeader", "Section"):
                s = self.secciones.get(tipo)
                if s:
                    self._dibujar_seccion(c, s, izq, y, Contexto(self, registro, seccion=s.nombre))
                    y += s.alto
            pie = self.secciones.get("PageFooter")
            if pie:
                self._dibujar_seccion(c, pie, izq, alto - inf - pie.alto, Contexto(self, registro, seccion=pie.nombre))
            c.showPage()
        c.save()
        return buffer.getvalue()

    # ------------------------------------------------------------------ listados
    def _clave_nivel(self, nivel: dict, fila: dict, parametros: dict):
        origen = nivel.get("ControlSource") or ""
        if origen.startswith("="):
            return Expresion(origen[1:]).evaluar(Contexto(self, Registro(fila), parametros=parametros))
        return fila.get(origen)

    def render_listado(self, filas: list[dict], parametros: dict | None = None, titulo: str = "",
                       pies: dict | None = None, subinformes: dict | None = None) -> bytes:
        """
        Informe de varias filas, como lo imprime Access: encabezado del informe, encabezado de
        pagina, encabezados/pies de grupo (BreakLevel), detalle, pie de pagina y pie del informe.

        filas      -> registros con los nombres de campo de Access, en el orden de la consulta.
        parametros -> valores que Access leia de formularios ([Forms]![Menu]![Fecha1]).
        pies       -> valores para controles sin origen de los pies, por nombre de control:
                      {"TOTAL": lambda filas_del_grupo: ...} (en Access los calculaba el VBA).
        """
        parametros = parametros or {}
        pies = pies or {}
        # Orden de los BreakLevel (estable: respeta el orden de la consulta en empates)
        for nivel in reversed(self.niveles):
            descendente = nivel.get("SortOrder") == "NotDefault"
            filas = sorted(filas, key=lambda f: _orden(self._clave_nivel(nivel, f, parametros)), reverse=descendente)
        con_encabezado = [i for i, n in enumerate(self.niveles) if _es_verdadero(n, "GroupHeader", False)]
        con_pie = [i for i, n in enumerate(self.niveles) if _es_verdadero(n, "GroupFooter", False)]
        enc = dict(zip(con_encabezado, self.encabezados_grupo))
        pie = dict(zip(con_pie, self.pies_grupo))

        # Secuencia de bandas: (seccion, registro actual, filas del alcance)
        bandas = []
        if self.secciones.get("FormHeader"):
            bandas.append((self.secciones["FormHeader"], filas[0] if filas else {}, filas))
        claves = [[self._clave_nivel(n, f, parametros) for n in self.niveles] for f in filas]
        inicio_grupo = [0] * len(self.niveles)
        for i, fila in enumerate(filas):
            nivel_cambio = None
            for k in range(len(self.niveles)):
                if i == 0 or claves[i][k] != claves[i - 1][k]:
                    nivel_cambio = k
                    break
            if nivel_cambio is not None:
                for k in range(nivel_cambio, len(self.niveles)):
                    inicio_grupo[k] = i
                    if k in enc:
                        fin = i
                        while fin + 1 < len(filas) and claves[fin + 1][:k + 1] == claves[i][:k + 1]:
                            fin += 1
                        bandas.append((enc[k], fila, filas[i:fin + 1]))
            if self.secciones.get("Section"):
                bandas.append((self.secciones["Section"], fila, [fila]))
            ultimo = i == len(filas) - 1
            for k in reversed(range(len(self.niveles))):
                if ultimo or claves[i + 1][:k + 1] != claves[i][:k + 1]:
                    if k in pie:
                        bandas.append((pie[k], fila, filas[inicio_grupo[k]:i + 1]))
        if self.secciones.get("FormFooter"):
            bandas.append((self.secciones["FormFooter"], filas[-1] if filas else {}, filas))

        self._subinformes = subinformes or {}
        paginas = self._componer(None, bandas, parametros, pies, 1)   # 1a pasada: contar paginas
        buffer = io.BytesIO()
        ancho, alto = self.pagina
        c = canvas.Canvas(buffer, pagesize=(ancho / 20, alto / 20))
        c.setTitle(titulo or self.props.get("Caption", "Informe"))
        c.setAuthor("SICGA - Centro Gallego de Puerto La Cruz")
        self._componer(c, bandas, parametros, pies, paginas)
        c.save()
        return buffer.getvalue()

    def _componer(self, c, bandas, parametros, pies, total_paginas) -> int:
        izq, sup, _, inf = self.margenes
        alto_pagina = self.pagina[1]
        enc_pag, pie_pag = self.secciones.get("PageHeader"), self.secciones.get("PageFooter")
        limite = alto_pagina - inf - (pie_pag.alto if pie_pag and pie_pag.visible else 0)
        estado = {"pagina": 1, "paginas": total_paginas, "acumulados": {}}
        y = sup
        ultimo = {}

        def dibujar(seccion, registro, alcance, y0):
            if c is None:
                return
            campos = dict(registro)
            if seccion.tipo != "Section":
                for nombre, funcion in pies.items():
                    campos[nombre] = funcion(alcance)
            self._dibujar_seccion(c, seccion, izq, y0,
                                  Contexto(self, Registro(campos, getattr(self, "_subinformes", {})), alcance,
                                           parametros, seccion.nombre, estado))

        def cerrar_pagina():
            if pie_pag and pie_pag.visible:
                dibujar(pie_pag, ultimo.get("registro", {}), ultimo.get("alcance", []), alto_pagina - inf - pie_pag.alto)
            if c is not None:
                c.showPage()

        def abrir_pagina(registro, alcance):
            nonlocal y
            y = sup
            if enc_pag and enc_pag.visible:
                dibujar(enc_pag, registro, alcance, y)
                y += enc_pag.alto

        abierta = False
        for seccion, registro, alcance in bandas:
            # Secciones de menos de 1 mm no imprimen nada legible: en Access dejaban franjas en
            # blanco (p. ej. el detalle de 7 twips del Estado de resultados). Aqui no ocupan espacio.
            if not seccion.visible or seccion.alto < 60:
                continue
            if seccion.tipo == "BreakHeader":
                # "Suma continua sobre el grupo": se reinicia al empezar cada grupo
                for ctl in self.secciones.get("Section", Seccion("", 0, [])).controles:
                    if ctl.props.get("RunningSum") == 1:
                        estado["acumulados"].pop(ctl.props.get("Name"), None)
            if not abierta:
                abrir_pagina(registro, alcance)
                abierta = True
            elif seccion.salto in (1, 3) or y + seccion.alto + self.alto_subinformes(seccion, registro) > limite:
                cerrar_pagina()
                estado["pagina"] += 1
                abrir_pagina(registro, alcance)
            dibujar(seccion, registro, alcance, y)
            ultimo = {"registro": registro, "alcance": alcance}
            y += seccion.alto + self.alto_subinformes(seccion, registro)
            if seccion.salto in (2, 3) and seccion is not bandas[-1][0]:
                cerrar_pagina()
                estado["pagina"] += 1
                abrir_pagina(registro, alcance)
        if not abierta:
            abrir_pagina({}, [])
        cerrar_pagina()
        return estado["pagina"]

    def _dibujar_seccion(self, c, seccion: Seccion, x0: int, y0: int, ctx: Contexto):
        alto_pagina = self.pagina[1]
        for ctl in seccion.controles:
            p = ctl.props
            visible = _es_verdadero(p, "Visible", True)
            regla = ctx.parametros.get("_visibilidad", {}).get(p.get("Name")) if isinstance(ctx.parametros.get("_visibilidad"), dict) else None
            if regla is not None:
                visible = regla(ctx.registro.campos) if callable(regla) else bool(regla)
            if not visible or ctl.tipo == "OptionGroup":
                continue
            x, y = x0 + p.get("Left", 0), y0 + p.get("Top", 0)
            if ctl.tipo in ("Subform", "Subreport"):
                self._subinforme(c, ctl, x, y, ctx)
                continue
            w, h = p.get("Width", 0), p.get("Height", 0)
            # rect en puntos PDF (origen abajo a la izquierda)
            px, py, pw, ph = x / 20, (alto_pagina - y - h) / 20, w / 20, h / 20
            if ctl.tipo == "Rectangle":
                self._rectangulo(c, p, px, py, pw, ph)
            elif ctl.tipo == "Line":
                self._linea(c, p, x, y, w, h, alto_pagina)
            elif ctl.tipo in ("UnboundObjectFrame", "BoundObjectFrame", "Image"):
                ruta = ctx.valor(ctl) if p.get("ControlSource") else None
                self._imagen(c, p, px, py, pw, ph, ruta if isinstance(ruta, str) else None)
            elif ctl.tipo == "CheckBox":
                self._casilla(c, x, y, alto_pagina, bool(ctx.valor(ctl)))
            elif ctl.tipo == "Label":
                self._texto(c, p, px, py, pw, ph, p.get("Caption", ""), es_numero=False)
            elif ctl.tipo in ("TextBox", "ComboBox"):
                if not h:
                    h = 315 if p.get("FontSize", 8) >= 10 else 255
                    py, ph = (alto_pagina - y - h) / 20, h / 20
                valor = ctx.valor(ctl)
                texto = formatear(valor, p.get("Format"))
                es_numero = isinstance(valor, (int, float, Decimal, date)) and not isinstance(valor, bool)
                self._texto(c, p, px, py, pw, ph, texto, es_numero)

    @staticmethod
    def _filas_sub(ctx: "Contexto", ctl: Nodo) -> list | None:
        sub = ctx.registro.campos.get("__sub__") or {}
        return sub.get(ctl.props.get("Name"))

    def alto_subinformes(self, seccion: "Seccion", campos: dict) -> int:
        """Alto extra que necesita la seccion para que quepan sus subinformes (Puede crecer)."""
        sub = (campos or {}).get("__sub__") or {}
        extra = 0
        for ctl in seccion.controles:
            filas = sub.get(ctl.props.get("Name")) if ctl.tipo in ("Subform", "Subreport") else None
            if not filas:
                continue
            hijo = InformeAccess.cargar(str(ctl.props.get("SourceObject", "")).split(".", 1)[-1])
            alto = sum(s.alto for t, s in hijo.secciones.items() if t in ("FormHeader", "FormFooter"))
            alto += len(filas) * hijo.secciones["Section"].alto if "Section" in hijo.secciones else 0
            extra = max(extra, ctl.props.get("Top", 0) + alto - (ctl.props.get("Top", 0) + ctl.props.get("Height", 0)))
        return max(0, extra)

    def _subinforme(self, c, ctl: Nodo, x: int, y: int, ctx: "Contexto"):
        """Dibuja el informe hijo (SourceObject="Report.X") con sus filas, una debajo de otra."""
        filas = self._filas_sub(ctx, ctl)
        if not filas:
            return
        hijo = InformeAccess.cargar(str(ctl.props.get("SourceObject", "")).split(".", 1)[-1])
        yy = y
        if hijo.secciones.get("FormHeader"):
            hijo._dibujar_seccion(c, hijo.secciones["FormHeader"], x, yy, Contexto(hijo, Registro(filas[0]), filas))
            yy += hijo.secciones["FormHeader"].alto
        detalle = hijo.secciones.get("Section")
        for fila in filas:
            if detalle:
                hijo._dibujar_seccion(c, detalle, x, yy, Contexto(hijo, Registro(fila), [fila], seccion=detalle.nombre))
                yy += detalle.alto
        if hijo.secciones.get("FormFooter"):
            hijo._dibujar_seccion(c, hijo.secciones["FormFooter"], x, yy, Contexto(hijo, Registro(filas[-1]), filas))

    @staticmethod
    def _grosor(p: dict) -> float:
        return max(0.5, float(p.get("BorderWidth", 0) or 0))

    def _rectangulo(self, c, p, x, y, w, h):
        rellenar = p.get("BackStyle", 1) != 0
        if rellenar:
            c.setFillColorRGB(*_color(p, "BackColor", (1, 1, 1)))
        borde = p.get("BorderStyle", 1) != 0
        c.setStrokeColorRGB(*_color(p, "BorderColor"))
        c.setLineWidth(self._grosor(p))
        c.rect(x, y, w, h, stroke=int(borde), fill=int(rellenar))

    def _linea(self, c, p, x, y, w, h, alto_pagina):
        c.setStrokeColorRGB(*_color(p, "BorderColor"))
        c.setLineWidth(self._grosor(p))
        if _es_verdadero(p, "LineSlant", False):   # diagonal de abajo-izq a arriba-der
            c.line(x / 20, (alto_pagina - y - h) / 20, (x + w) / 20, (alto_pagina - y) / 20)
        else:
            c.line(x / 20, (alto_pagina - y) / 20, (x + w) / 20, (alto_pagina - y - h) / 20)

    def _imagen(self, c, p, x, y, w, h, ruta: str | None = None):
        """Logotipo incrustado (OLE / PictureData) o, si el control esta ligado a un campo,
        el archivo de imagen de esa ruta (p. ej. la foto del socio)."""
        if ruta:
            try:
                from PIL import Image
                img = Image.open(ruta).convert("RGB")
                c.drawImage(ImageReader(img), x, y, w, h, preserveAspectRatio=True, anchor="c")
            except (OSError, ValueError):
                pass
            return
        datos = imagen_ole(p)
        if datos:
            from PIL import Image
            img = Image.open(io.BytesIO(datos)).convert("RGB")
            c.drawImage(ImageReader(img), x, y, w, h, preserveAspectRatio=p.get("SizeMode", 0) == 3)
        if p.get("OldBorderStyle", 0):
            c.setLineWidth(self._grosor(p))
            c.rect(x, y, w, h, stroke=1, fill=0)

    @staticmethod
    def _casilla(c, x, y, alto_pagina, marcada: bool):
        lado = 180                           # casilla estandar de Access: 0,125" (9 pt)
        px, py = (x + 15) / 20, (alto_pagina - y - 15 - lado) / 20
        c.setStrokeColorRGB(0, 0, 0)
        c.setLineWidth(0.75)
        c.rect(px, py, lado / 20, lado / 20, stroke=1, fill=0)
        if marcada:
            c.setLineWidth(1.4)
            s = lado / 20
            c.lines([(px + s * .2, py + s * .5, px + s * .42, py + s * .25), (px + s * .42, py + s * .25, px + s * .82, py + s * .8)])

    def _texto(self, c, p, x, y, w, h, texto: str, es_numero: bool):
        if p.get("BackStyle", 1) != 0 and p.get("BackColor", 16777215) != 16777215:
            c.setFillColorRGB(*_color(p, "BackColor", (1, 1, 1)))
            c.rect(x, y, w, h, stroke=0, fill=1)
        if p.get("OldBorderStyle", 0) or p.get("BorderStyle", 0):
            c.setStrokeColorRGB(*_color(p, "BorderColor"))
            c.setLineWidth(self._grosor(p))
            c.rect(x, y, w, h, stroke=1, fill=0)
        if not texto:
            return
        nombre = fuente(p.get("FontName", "Calibri"), p.get("FontWeight", 400) >= 600, _es_verdadero(p, "FontItalic", False))
        tam = float(p.get("FontSize", 8))              # Access usa 8 pt si el informe no fija otro
        asc, desc = pdfmetrics.getAscentDescent(nombre, tam)
        alto_linea = asc - desc
        relleno = 1.5                                  # 30 twips de margen interno de Access
        alineacion = p.get("TextAlign", 0) or (3 if es_numero else 1)

        lineas = [texto]
        if h - 2 * relleno >= 2 * alto_linea:          # cuadro alto: Access ajusta el texto
            lineas = self._ajustar(texto, nombre, tam, w - 2 * relleno)

        c.saveState()
        trazo = c.beginPath()
        trazo.rect(x, y, w, h)
        c.clipPath(trazo, stroke=0, fill=0)           # Access recorta lo que no cabe
        c.setFillColorRGB(*_color(p, "ForeColor"))
        c.setFont(nombre, tam)
        base = y + h - relleno - asc
        for linea in lineas:
            ancho_texto = pdfmetrics.stringWidth(linea, nombre, tam)
            if alineacion == 2:
                tx = x + (w - ancho_texto) / 2
            elif alineacion == 3:
                tx = x + w - relleno - ancho_texto
            else:
                tx = x + relleno
            c.drawString(tx, base, linea)
            if _es_verdadero(p, "FontUnderline", False):
                c.setLineWidth(max(0.5, tam / 18))
                c.line(tx, base - 1.2, tx + ancho_texto, base - 1.2)
            base -= alto_linea
        c.restoreState()

    @staticmethod
    def _ajustar(texto: str, nombre: str, tam: float, ancho: float) -> list[str]:
        lineas = []
        for parrafo in texto.splitlines() or [""]:
            actual = ""
            for palabra in parrafo.split(" "):
                prueba = f"{actual} {palabra}".strip()
                if pdfmetrics.stringWidth(prueba, nombre, tam) <= ancho or not actual:
                    actual = prueba
                else:
                    lineas.append(actual)
                    actual = palabra
            lineas.append(actual)
        return lineas
