"""Renderizador JSON del API: los importes (Decimal) viajan siempre como texto.

Los serializadores ya lo hacen (COERCE_DECIMAL_TO_STRING). Esto cubre las respuestas armadas a mano
(estado de cuenta, saldo a favor, caja en divisas, resumen...), que el codificador de DRF convertiria
a float: el frontend recibiria un numero donde espera un texto y perderia precision en cifras grandes.
"""
from decimal import Decimal

from rest_framework.renderers import JSONRenderer
from rest_framework.utils.encoders import JSONEncoder


class CodificadorJSON(JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return format(obj, "f")          # "1200.0000", nunca notacion cientifica
        return super().default(obj)


class RenderizadorJSON(JSONRenderer):
    encoder_class = CodificadorJSON
