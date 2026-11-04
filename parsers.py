from rest_framework.exceptions import ParseError
from rest_framework.parsers import JSONParser


class JSONObjeto(JSONParser):
    """Toda la API recibe un objeto JSON ({...}): una lista o un valor suelto se responde con 400 en lugar de
    llegar a las vistas (que leen request.data.get(...))."""

    def parse(self, stream, media_type=None, parser_context=None):
        datos = super().parse(stream, media_type, parser_context)
        if not isinstance(datos, dict):
            raise ParseError("Se esperaba un objeto JSON ({...}).")
        return datos
