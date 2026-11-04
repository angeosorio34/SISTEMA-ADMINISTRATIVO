#!/usr/bin/env python
"""
Reconstruye la plantilla de los informes de Access que estaban DANADOS en SICGA.accdb.

En la base original, 42 objetos (32 formularios y 10 informes) no se pueden exportar con
SaveAsText ("La clave de busqueda no se encontro en ningun registro" / "No se encontro la
propiedad"), ni siquiera tras Compactar y reparar. Si se pueden abrir en vista Diseno, asi que
se leen propiedad a propiedad (secciones, grupos, margenes, papel y controles) a un JSON, y este
script convierte ese JSON al mismo formato de plantilla que usan los demas informes
(backend/core/reportes/plantillas/), para que el motor de PDF los imprima igual.

Uso:
    python 03_reconstruir_informes.py <carpeta_json> <carpeta_plantillas>
"""
import json
import re
import struct
import sys
from pathlib import Path

TIPOS = {100: "Label", 101: "Rectangle", 102: "Line", 103: "Image", 106: "CheckBox", 108: "BoundObjectFrame",
         109: "TextBox", 111: "ComboBox", 112: "Subform", 114: "UnboundObjectFrame", 107: "OptionGroup"}
# Indices de Section() en Access
SECCION = {0: "Section", 1: "FormHeader", 2: "FormFooter", 3: "PageHeader", 4: "PageFooter"}
PROPS_TEXTO = {"Name", "Caption", "ControlSource", "FontName", "Format", "SourceObject", "LinkChildFields",
               "LinkMasterFields"}


def valor(clave, v):
    if isinstance(v, bool):
        return "-1" if v else "0"
    if clave in PROPS_TEXTO or isinstance(v, str):
        return '"' + str(v).replace('"', '\\"').replace("\r", "").replace("\n", " ") + '"'
    return str(int(v)) if isinstance(v, float) and v.is_integer() else str(v)


def hex_lineas(datos: bytes, ancho=32):
    return [f"0x{datos[i:i + ancho].hex()}" for i in range(0, len(datos), ancho)]


def logo(plantillas: Path):
    """PictureData del escudo, tomado de un informe sano que usa el mismo logotipo."""
    texto = (plantillas / "ListaSocios.txt").read_text(encoding="utf-8")
    m = re.search(r"Name =\"OLEIndependiente48\".*?(PictureData = Begin\n.*?\n\s*End)", texto, re.S)
    return m.group(1) if m else None


def convertir(info: dict, plantillas: Path) -> str:
    out = ["Version =21", "VersionRequired =20", "Begin Report"]
    out.append(f"    Width ={int(info['Width'])}")
    out.append(f"    Caption ={valor('Caption', info.get('Caption') or info['nombre'])}")
    out.append(f"    RecordSource ={valor('RecordSource', info.get('RecordSource') or '')}")
    izq, sup, der, inf = (info.get("margenes") or [720, 720, 720, 720])
    mip = struct.pack("<4i", int(izq), int(sup), int(der), int(inf)) + bytes(40)
    out += ["    PrtMip = Begin", *("        " + h for h in hex_lineas(mip)), "    End"]
    dev = bytearray(84)
    struct.pack_into("<2h", dev, 76, int(info.get("Orientation") or 1), int(info.get("PaperSize") or 1))
    out += ["    PrtDevModeW = Begin", *("        " + h for h in hex_lineas(bytes(dev))), "    End"]
    out.append("    Begin")
    for nivel in info.get("niveles") or []:
        out.append("        Begin BreakLevel")
        if nivel.get("GroupHeader"):
            out.append("            GroupHeader = NotDefault")
        if nivel.get("GroupFooter"):
            out.append("            GroupFooter = NotDefault")
        if nivel.get("SortOrder"):
            out.append("            SortOrder = NotDefault")
        out.append(f"            ControlSource ={valor('ControlSource', nivel.get('ControlSource') or '')}")
        out.append("        End")

    secciones = {s["indice"]: s for s in info["secciones"]}
    orden = [1, 3] + sorted(i for i in secciones if i >= 5 and i % 2 == 1) + [0] \
            + sorted(i for i in secciones if i >= 5 and i % 2 == 0) + [4, 2]
    escudo = logo(plantillas)
    for indice in orden:
        s = secciones.get(indice)
        if s is None:
            continue
        tipo = SECCION.get(indice) or ("BreakHeader" if indice % 2 == 1 else "BreakFooter")
        out.append(f"        Begin {tipo}")
        for k in ("Height", "Name", "Visible", "ForceNewPage", "CanShrink"):
            if k in s and s[k] is not None:
                out.append(f"            {k} ={valor(k, s[k])}")
        out.append("            Begin")
        for c in info["controles"]:
            if c.get("Section") != indice or c["ControlType"] not in TIPOS:
                continue
            tipo_ctl = TIPOS[c["ControlType"]]
            out.append(f"                Begin {tipo_ctl}")
            for k, v in c.items():
                if k in ("ControlType", "Section") or v is None or v == "":
                    continue
                if k == "BorderStyle" and tipo_ctl in ("TextBox", "Label", "CheckBox"):
                    k = "OldBorderStyle"
                out.append(f"                    {k} ={valor(k, v)}")
            if tipo_ctl in ("Image", "UnboundObjectFrame") and not c.get("ControlSource") and escudo:
                out.append("                    " + escudo.strip())
            out.append("                End")
        out.append("            End")
        out.append("        End")
    out += ["    End", "End"]
    return "\n".join(out) + "\n"


def main():
    origen, destino = Path(sys.argv[1]), Path(sys.argv[2])
    for archivo in sorted(origen.glob("*.json")):
        info = json.loads(archivo.read_text(encoding="utf-8-sig"))
        (destino / f"{info['nombre']}.txt").write_text(convertir(info, destino), encoding="utf-8")
        print(f"reconstruido {info['nombre']}: {len(info['controles'])} controles")


if __name__ == "__main__":
    main()
